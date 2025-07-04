import json
from os.path import islink
import os, re, sys, subprocess, platform
import tarfile
import time
from distutils import log
from contextlib import closing, contextmanager
from ftplib import FTP

import urllib.error
from urllib.parse import urljoin, unquote, urlparse
from urllib.request import urlretrieve, urlopen, Request

multi_make_options = []
try:
    import multiprocessing
    cpus = multiprocessing.cpu_count()
    if cpus > 1:
        if cpus > 5:
            cpus = 5
        multi_make_options = ['-j%d' % (cpus+1)]
except:
    pass


# overridable to control script usage
sys_platform = sys.platform


# use pre-built libraries on Windows

def download_and_extract_windows_binaries(destdir):
    url = "https://api.github.com/repos/lxml/libxml2-win-binaries/releases?per_page=5"
    releases, _ = read_url(
        url,
        accept="application/vnd.github+json",
        as_json=True,
        github_api_token=os.environ.get("GITHUB_API_TOKEN"),
    )

    max_release = {'tag_name': ''}
    for release in releases:
        if max_release['tag_name'] < release.get('tag_name', ''):
            max_release = release

    url = "https://github.com/lxml/libxml2-win-binaries/releases/download/%s/" % max_release['tag_name']
    filenames = [asset['name'] for asset in max_release.get('assets', ())]

    # Check for native ARM64 build or the environment variable that is set by
    # Visual Studio for cross-compilation (same variable as setuptools uses)
    if platform.machine() == 'ARM64' or os.getenv('VSCMD_ARG_TGT_ARCH') == 'arm64':
        arch = "win-arm64"
    elif sys.maxsize > 2**32:
        arch = "win64"
    else:
        arch = "win32"

    arch_part = '.' + arch + '.'
    filenames = [filename for filename in filenames if arch_part in filename]

    libs = {}
    for libname in ['libxml2', 'libxslt', 'zlib', 'iconv']:
        libs[libname] = "%s-%s.%s.zip" % (
            libname,
            find_max_version(libname, filenames),
            arch,
        )

    if not os.path.exists(destdir):
        os.makedirs(destdir)

    for libname, libfn in libs.items():
        srcfile = urljoin(url, libfn)
        destfile = os.path.join(destdir, libfn)
        if os.path.exists(destfile + ".keep"):
            print('Using local copy of  "{}"'.format(srcfile))
        else:
            print('Retrieving "%s" to "%s"' % (srcfile, destfile))
            urlretrieve(srcfile, destfile)
        d = unpack_zipfile(destfile, destdir)
        libs[libname] = d

    return libs


def find_top_dir_of_zipfile(zipfile):
    topdir = None
    files = [f.filename for f in zipfile.filelist]
    dirs = [d for d in files if d.endswith('/')]
    if dirs:
        dirs.sort(key=len)
        topdir = dirs[0]
        topdir = topdir[:topdir.index("/")+1]
        for path in files:
            if not path.startswith(topdir):
                topdir = None
                break
    assert topdir, (
        "cannot determine single top-level directory in zip file %s" %
        zipfile.filename)
    return topdir.rstrip('/')


def unpack_zipfile(zipfn, destdir):
    assert zipfn.endswith('.zip')
    import zipfile
    print('Unpacking %s into %s' % (os.path.basename(zipfn), destdir))
    f = zipfile.ZipFile(zipfn)
    try:
        extracted_dir = os.path.join(destdir, find_top_dir_of_zipfile(f))
        f.extractall(path=destdir)
    finally:
        f.close()
    assert os.path.exists(extracted_dir), 'missing: %s' % extracted_dir
    return extracted_dir


def get_prebuilt_libxml2xslt(download_dir, static_include_dirs, static_library_dirs):
    assert sys_platform.startswith('win')
    libs = download_and_extract_windows_binaries(download_dir)
    for libname, path in libs.items():
        i = os.path.join(path, 'include')
        l = os.path.join(path, 'lib')
        assert os.path.exists(i), 'does not exist: %s' % i
        assert os.path.exists(l), 'does not exist: %s' % l
        static_include_dirs.append(i)
        static_library_dirs.append(l)


## Routines to download and build libxml2/xslt from sources:

LIBXML2_LOCATION = 'https://download.gnome.org/sources/libxml2/'
LIBXSLT_LOCATION = 'https://download.gnome.org/sources/libxslt/'
LIBICONV_LOCATION = 'https://ftp.gnu.org/pub/gnu/libiconv/'
ZLIB_LOCATION = 'https://zlib.net/'
match_libfile_version = re.compile('^[^-]*-([.0-9-]+)[.].*').match


def _find_content_encoding(response, default='iso8859-1'):
    from email.message import Message
    content_type = response.headers.get('Content-Type')
    if content_type:
        msg = Message()
        msg.add_header('Content-Type', content_type)
        charset = msg.get_content_charset(default)
    else:
        charset = default
    return charset


def remote_listdir(url):
    try:
        return _list_dir_urllib(url)
    except IOError:
        assert url.lower().startswith('ftp://')
        print("Requesting with urllib failed. Falling back to ftplib. "
              "Proxy argument will be ignored for %s" % url)
        return _list_dir_ftplib(url)


def _list_dir_ftplib(url):
    parts = urlparse(url)
    ftp = FTP(parts.netloc)
    try:
        ftp.login()
        ftp.cwd(parts.path)
        data = []
        ftp.dir(data.append)
    finally:
        ftp.quit()
    return parse_text_ftplist("\n".join(data))


def read_url(url, decode=True, accept=None, as_json=False, github_api_token=None):
    headers = {'User-Agent': 'https://github.com/lxml/lxml'}
    if accept:
        headers['Accept'] = accept
    if github_api_token:
        headers['authorization'] = "Bearer " + github_api_token
    request = Request(url, headers=headers)

    with closing(urlopen(request)) as res:
        charset = _find_content_encoding(res)
        content_type = res.headers.get('Content-Type')
        data = res.read()

    if decode:
        data = data.decode(charset)
    if as_json:
        data = json.loads(data)
    return data, content_type


def _list_dir_urllib(url):
    data, content_type = read_url(url)
    if content_type and content_type.startswith('text/html'):
        files = parse_html_filelist(data)
    else:
        files = parse_text_ftplist(data)
    return files


def http_find_latest_version_directory(url, version=None):
    data, _ = read_url(url)
    # e.g. <a href="1.0/">
    directories = [
        (int(v[0]), int(v[1]))
        for v in re.findall(r' href=["\']([0-9]+)\.([0-9]+)/?["\']', data)
    ]
    if not directories:
        return url
    best_version = max(directories)
    if version:
        major, minor, _ = version.split(".", 2)
        major, minor = int(major), int(minor)
        if (major, minor) in directories:
            best_version = (major, minor)
    latest_dir = "%s.%s" % best_version
    return urljoin(url, latest_dir) + "/"


def http_listfiles(url, re_pattern):
    data, _ = read_url(url)
    files = re.findall(re_pattern, data)
    return files


def parse_text_ftplist(s):
    for line in s.splitlines():
        if not line.startswith('d'):
            # -rw-r--r--   1 ftp      ftp           476 Sep  1  2011 md5sum.txt
            # Last (9th) element is 'md5sum.txt' in the above example, but there
            # may be variations, so we discard only the first 8 entries.
            yield line.split(None, 8)[-1]


def parse_html_filelist(s):
    re_href = re.compile(
        r'''<a[^>]*\shref=["']([^;?"']+?)[;?"']''',
        re.I|re.M)
    links = set(re_href.findall(s))
    for link in links:
        if not link.endswith('/'):
            yield unquote(link)


def tryint(s):
    try:
        return int(s)
    except ValueError:
        return s


@contextmanager
def py2_tarxz(filename):
    import tempfile
    with tempfile.TemporaryFile() as tmp:
        subprocess.check_call(["xz", "-dc", filename], stdout=tmp.fileno())
        tmp.seek(0)
        with closing(tarfile.TarFile(fileobj=tmp)) as tf:
            yield tf


def download_libxml2(dest_dir, version=None):
    """Downloads libxml2, returning the filename where the library was downloaded"""
    #version_re = re.compile(r'LATEST_LIBXML2_IS_([0-9.]+[0-9](?:-[abrc0-9]+)?)')
    version_re = re.compile(r'libxml2-([0-9.]+[0-9])[.]tar[.]xz')
    filename = 'libxml2-%s.tar.xz'

    if version == "2.9.12":
        # Temporarily using the latest master (2.9.12+) until there is a release that supports lxml again.
        from_location = "https://gitlab.gnome.org/GNOME/libxml2/-/archive/dea91c97debeac7c1aaf9c19f79029809e23a353/"
        version = "dea91c97debeac7c1aaf9c19f79029809e23a353"
    else:
        from_location = http_find_latest_version_directory(LIBXML2_LOCATION, version=version)

    return download_library(dest_dir, from_location, 'libxml2',
                            version_re, filename, version=version)


def download_libxslt(dest_dir, version=None):
    """Downloads libxslt, returning the filename where the library was downloaded"""
    #version_re = re.compile(r'LATEST_LIBXSLT_IS_([0-9.]+[0-9](?:-[abrc0-9]+)?)')
    version_re = re.compile(r'libxslt-([0-9.]+[0-9])[.]tar[.]xz')
    filename = 'libxslt-%s.tar.xz'
    from_location = http_find_latest_version_directory(LIBXSLT_LOCATION, version=version)
    return download_library(dest_dir, from_location, 'libxslt',
                            version_re, filename, version=version)


def download_libiconv(dest_dir, version=None):
    """Downloads libiconv, returning the filename where the library was downloaded"""
    version_re = re.compile(r'libiconv-([0-9.]+[0-9])[.]tar[.]gz')
    filename = 'libiconv-%s.tar.gz'
    return download_library(dest_dir, LIBICONV_LOCATION, 'libiconv',
                            version_re, filename, version=version)


def download_zlib(dest_dir, version):
    """Downloads zlib, returning the filename where the library was downloaded"""
    version_re = re.compile(r'zlib-([0-9.]+[0-9])[.]tar[.]gz')
    filename = 'zlib-%s.tar.gz'
    return download_library(dest_dir, ZLIB_LOCATION, 'zlib',
                            version_re, filename, version=version)


def find_max_version(libname, filenames, version_re=None):
    if version_re is None:
        version_re = re.compile(r'%s-([0-9.]+[0-9](?:-[abrc0-9]+)?)' % libname)
    versions = []
    for fn in filenames:
        match = version_re.search(fn)
        if match:
            version_string = match.group(1)
            versions.append((tuple(map(tryint, version_string.replace("-", ".-").split('.'))),
                             version_string))
    if not versions:
        raise Exception(
            "Could not find the most current version of %s from the files: %s" % (
                libname, filenames))
    versions.sort()
    version_string = versions[-1][-1]
    print('Latest version of %s is %s' % (libname, version_string))
    return version_string


def download_library(dest_dir, location, name, version_re, filename, version=None):
    if version is None:
        try:
            if location.startswith('ftp://'):
                fns = list(remote_listdir(location))
            else:
                fns = http_listfiles(location, '(%s)' % filename.replace('%s', '(?:[0-9.]+[0-9])'))
            print(f"Found {len(fns)} links at {location}")
            version = find_max_version(name, fns, version_re)
        except IOError:
            # network failure - maybe we have the files already?
            latest = (0,0,0)
            fns = os.listdir(dest_dir)
            for fn in fns:
                if fn.startswith(name+'-'):
                    match = match_libfile_version(fn)
                    if match:
                        version_tuple = tuple(map(tryint, match.group(1).split('.')))
                        if version_tuple > latest:
                            latest = version_tuple
                            filename = fn
                            version = None
            if latest == (0,0,0):
                raise
    if version:
        filename = filename % version

    full_url = urljoin(location, filename)
    dest_filename = os.path.join(dest_dir, filename)
    if os.path.exists(dest_filename):
        print(('Using existing %s downloaded into %s '
               '(delete this file if you want to re-download the package)') % (
            name, dest_filename))
        return dest_filename

    print('Downloading %s into %s from %s' % (name, dest_filename, full_url))
    try:
        urlretrieve(full_url, dest_filename)
    except urllib.error.URLError as exc:
        # retry once
        retry_after_seconds = 2
        print(f"Download failed: {exc}, retrying in {int(retry_after_seconds)} seconds…")
        time.sleep(retry_after_seconds)
        urlretrieve(full_url, dest_filename)

    return dest_filename


def unpack_tarball(tar_filename, dest) -> str:
    print('Unpacking %s into %s' % (os.path.basename(tar_filename), dest))
    os_path = os.path
    abs_dest = os_path.abspath(dest)

    tar_cm = tarfile.open(tar_filename)

    if hasattr(tarfile, 'data_filter'):
        tar_cm.extraction_filter = tarfile.data_filter

    base_dir = None
    with closing(tar_cm) as tar:
        directories = []
        for member in tar:
            # Guard against malicious tar file content.
            path = os_path.join(dest, member.name)
            abs_path = os_path.abspath(path)
            if not os_path.commonpath([abs_dest, abs_path]).startswith(abs_dest):
                raise RuntimeError('Unexpected path in %s: %s' % (tar_filename, member.name))

            if member.isdir():
                directories.append(member)
                continue
            elif member.issym() or member.islnk():
                link_path = os_path.abspath(os_path.join(
                    os_path.dirname(abs_path) if member.issym() else abs_dest,
                    member.linkname))
                if not os_path.commonpath([abs_dest, link_path]).startswith(abs_dest):
                    raise RuntimeError('Unexpected path in %s: %s' % (tar_filename, member.name))
            elif member.islnk():
                link_path = os_path.abspath(os_path.join(abs_dest, member.linkname))
            elif not member.isfile():
                raise RuntimeError('Unexpected path in %s: %s' % (tar_filename, member.name))

            # Find common base directory.
            first_dir = member.name.split('/')[0]
            if base_dir is None:
                base_dir = first_dir
            elif base_dir != first_dir:
                print('Unexpected path in %s: %s' % (tar_filename, first_dir))
                continue

            # Extract only new files.
            if os_path.exists(abs_path) and os_path.getsize(abs_path) == member.size:
                continue
            tar.extract(member, abs_dest)

        # Update directory properties/times/etc.
        for member in directories:
            tar.extract(member, abs_dest)

    return os_path.join(dest, base_dir)


def call_subprocess(cmd, **kw):
    import subprocess
    cwd = kw.get('cwd', '.')
    cmd_desc = ' '.join(cmd)
    log.info('Running "%s" in %s' % (cmd_desc, cwd))
    returncode = subprocess.call(cmd, **kw)
    if returncode:
        raise Exception('Command "%s" returned code %s' % (cmd_desc, returncode))


def safe_mkdir(dir):
    if not os.path.exists(dir):
        os.makedirs(dir)


def cmmi(configure_cmd, build_dir, multicore=None, **call_setup):
    print('Starting build in %s' % build_dir)
    call_subprocess(configure_cmd, cwd=build_dir, **call_setup)
    if not multicore:
        make_jobs = multi_make_options
    elif int(multicore) > 1:
        make_jobs = ['-j%s' % multicore]
    else:
        make_jobs = []
    call_subprocess(
        ['make'] + make_jobs,
        cwd=build_dir, **call_setup)
    call_subprocess(
        ['make'] + make_jobs + ['install'],
        cwd=build_dir, **call_setup)


def configure_darwin_env(env_setup):
    import platform
    # configure target architectures on MacOS-X (x86_64 + Arm64, by default)
    major_version, minor_version = tuple(map(int, platform.mac_ver()[0].split('.')[:2]))
    if major_version >= 11:
        env_default = {
            'CFLAGS': "-arch x86_64 -arch arm64 -O3",
            'LDFLAGS': "-arch x86_64 -arch arm64",
            'MACOSX_DEPLOYMENT_TARGET': "11.0"
        }
        env_default.update(os.environ)
        env_setup['env'] = env_default


def build_libxml2xslt(download_dir, build_dir,
                      static_include_dirs, static_library_dirs,
                      static_cflags, static_binaries,
                      libxml2_version=None,
                      libxslt_version=None,
                      libiconv_version=None,
                      zlib_version=None,
                      multicore=None,
                      with_zlib=True):
    safe_mkdir(download_dir)
    safe_mkdir(build_dir)

    zlib_dir = None
    if with_zlib:
        zlib_dir = unpack_tarball(download_zlib(download_dir, zlib_version), build_dir)

    libiconv_dir = unpack_tarball(download_libiconv(download_dir, libiconv_version), build_dir)
    libxml2_dir  = unpack_tarball(download_libxml2(download_dir, libxml2_version), build_dir)
    libxslt_dir  = unpack_tarball(download_libxslt(download_dir, libxslt_version), build_dir)

    prefix = os.path.join(os.path.abspath(build_dir), 'libxml2')
    lib_dir = os.path.join(prefix, 'lib')
    safe_mkdir(prefix)

    lib_names = ['libxml2', 'libexslt', 'libxslt', 'iconv'] + (['libz'] if with_zlib else [])
    existing_libs = {
        lib: os.path.join(lib_dir, filename)
        for lib in lib_names
        for filename in os.listdir(lib_dir)
        if lib in filename and filename.endswith('.a')
    } if os.path.isdir(lib_dir) else {}

    def has_current_lib(name, build_dir, _build_all_following=[False]):
        if _build_all_following[0]:
            return False  # a dependency was rebuilt => rebuilt this lib as well
        lib_file = existing_libs.get(name)
        found = lib_file and os.path.getmtime(lib_file) > os.path.getmtime(build_dir)
        if found:
            print("Found pre-built '%s'" % name)
        else:
            # also rebuild all following libs (which may depend on this one)
            _build_all_following[0] = True
        return found

    call_setup = {}
    if sys_platform == 'darwin':
        configure_darwin_env(call_setup)

    configure_cmd = ['./configure',
                     '--disable-dependency-tracking',
                     '--disable-shared',
                     '--prefix=%s' % prefix,
                     ]

    # build zlib
    if with_zlib:
        zlib_configure_cmd = [
            './configure',
            '--prefix=%s' % prefix,
        ]
        if not has_current_lib("libz", zlib_dir):
            cmmi(zlib_configure_cmd, zlib_dir, multicore, **call_setup)

    # build libiconv
    if not has_current_lib("iconv", libiconv_dir):
        cmmi(configure_cmd, libiconv_dir, multicore, **call_setup)

    # build libxml2
    libxml2_configure_cmd = configure_cmd + [
        '--without-python',
        '--with-iconv=%s' % prefix,
        ('--with-zlib=%s' % prefix) if with_zlib else '--without-zlib',
    ]

    if not libxml2_version:
        libxml2_version = os.path.basename(libxml2_dir).split('-', 1)[-1]

    if tuple(map(tryint, libxml2_version.split('-', 1)[0].split('.'))) >= (2, 9, 5):
        libxml2_configure_cmd.append('--without-lzma')  # can't currently build that

    try:
        if tuple(map(tryint, libxml2_version.split('-', 1)[0].split('.'))) >= (2, 7, 3):
            libxml2_configure_cmd.append('--enable-rebuild-docs=no')
    except Exception:
        pass # this isn't required, so ignore any errors
    if not has_current_lib("libxml2", libxml2_dir):
        if not os.path.exists(os.path.join(libxml2_dir, "configure")):
            # Allow building from git sources by running autoconf etc.
            libxml2_configure_cmd[0] = "./autogen.sh"
        cmmi(libxml2_configure_cmd, libxml2_dir, multicore, **call_setup)

    # Fix up libxslt configure script (needed up to and including 1.1.34)
    # https://gitlab.gnome.org/GNOME/libxslt/-/commit/90c34c8bb90e095a8a8fe8b2ce368bd9ff1837cc
    with open(os.path.join(libxslt_dir, "configure"), 'rb') as f:
        config_script = f.read()
    if b' --libs print ' in config_script:
        config_script = config_script.replace(b' --libs print ', b' --libs ')
        with open(os.path.join(libxslt_dir, "configure"), 'wb') as f:
            f.write(config_script)

    # build libxslt
    libxslt_configure_cmd = configure_cmd + [
        '--without-python',
        '--with-libxml-prefix=%s' % prefix,
        '--without-crypto',
    ]
    if not (has_current_lib("libxslt", libxslt_dir) and has_current_lib("libexslt", libxslt_dir)):
        cmmi(libxslt_configure_cmd, libxslt_dir, multicore, **call_setup)

    # collect build setup for lxml
    xslt_config = os.path.join(prefix, 'bin', 'xslt-config')
    xml2_config = os.path.join(prefix, 'bin', 'xml2-config')

    static_include_dirs.extend([
            os.path.join(prefix, 'include'),
            os.path.join(prefix, 'include', 'libxml2'),
            os.path.join(prefix, 'include', 'libxslt'),
            os.path.join(prefix, 'include', 'libexslt')])
    static_library_dirs.append(lib_dir)

    listdir = os.listdir(lib_dir)
    static_binaries += [os.path.join(lib_dir, filename)
        for lib in lib_names
        for filename in listdir
        if lib in filename and filename.endswith('.a')]

    return xml2_config, xslt_config


def main():
    static_include_dirs = []
    static_library_dirs = []
    download_dir = "libs"

    if sys_platform.startswith('win'):
        return get_prebuilt_libxml2xslt(
            download_dir, static_include_dirs, static_library_dirs)
    else:
        return build_libxml2xslt(
            download_dir, 'build/tmp',
            static_include_dirs, static_library_dirs,
            static_cflags=[],
            static_binaries=[]
        )


if __name__ == '__main__':
    if len(sys.argv) > 1:
        # change global sys_platform setting
        sys_platform = sys.argv[1]
    main()
