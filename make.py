from concurrent.futures import ThreadPoolExecutor
from threading import Semaphore
import asyncio
from urllib import request
import os
import sys
import shutil
import re
import hashlib
import lzma
import gzip
import bz2
import pickle

import repo

DOWNLOAD = True
REMAKE = False

BUILD_DIR = './build'
OUTPUT = './repo/Packages'


MIRRORS = {
    'deepin': 'https://community-packages.deepin.com/deepin',
    'deepin-store': 'https://com-store-packages.uniontech.com/appstore',
    'debian': 'https://mirrors.tuna.tsinghua.edu.cn/debian',
    'ubuntu': 'https://mirrors.tuna.tsinghua.edu.cn/ubuntu'
}
DEEPIN_SITE_SOURCE = '''
    deepin apricot main non-free i386
    deepin apricot main non-free amd64
    deepin-store deepin appstore i386
    deepin-store deepin appstore amd64
'''
SITE_SOURCES = {
    'debian-stable': 'debian stable main amd64',
    'debian-testing': 'debian testing main amd64',
    'ubuntu-focal': 'ubuntu focal main amd64',
    'ubuntu-hirsute': 'ubuntu hirsute main amd64',
    'ubuntu-impish': 'ubuntu impish main amd64'
}
EXTRA_PACKAGES = [
    ('ubuntu focal universe amd64', ('python-gi',))
]

CACHE = {}
print_lock = Semaphore()


def log(*args, file=sys.stdout):
    with print_lock:
        print(' '.join(args), file=file)


class DeleteOnError:
    def __init__(self, path, *args, **kwargs):
        self.path = path
        self.f = open(path, *args, **kwargs)

    def __enter__(self):
        self.f.__enter__()
        return self.f

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.f.__exit__(exc_type, exc_val, exc_tb)
        if exc_type is not None:
            os.remove(self.path)


def download(mirror, *paths, size=None, sha256=None):
    url = '/'.join((MIRRORS[mirror], *paths))
    path = os.path.join(BUILD_DIR, '/'.join((mirror, *paths)).replace('/', '#'))
    if os.path.exists(path):
        if not DOWNLOAD:
            return False, path
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            if f.tell() == size:
                f.seek(0)
                hasher = hashlib.sha256()
                while True:
                    data = f.read(hasher.block_size)
                    if not data:
                        break
                    hasher.update(data)
                if sha256 is not None and hasher.hexdigest().lower() == sha256:
                    return False, path
    log('Downloading: %s\n\tto %s' % (url, path))
    with DeleteOnError(path, 'wb') as f:
        try:
            with request.urlopen(url, timeout=30) as resp:
                shutil.copyfileobj(resp, f)
        except Exception as e:
            print(e, url)
            raise
        return True, path


def get_release(mirror, dist):
    _, path = download(mirror, 'dists', dist, 'Release')
    with open(path, 'rt') as f:
        release = f.read()
    return re.search(r'\nSHA256:\s*((?:\n\s+.+)+)', release).group(1)


def get_packages(mirror, dist, path, size, sha256):
    updated, download_path = download(mirror, 'dists', dist, path, size=size, sha256=sha256)
    file_path, ext = os.path.splitext(download_path)
    meta_path = file_path + '.meta'
    if updated or not os.path.exists(file_path) or not os.path.exists(meta_path):
        log('Updating Packages:', download_path)
        if file_path != download_path:
            uncompressor = {'.xz': lzma, '.bz2': bz2, '.gz': gzip}
            with uncompressor[ext].open(download_path) as fin:
                with DeleteOnError(file_path, 'wb') as fout:
                    shutil.copyfileobj(fin, fout)
        meta = repo.make_repo_meta(file_path)
        with DeleteOnError(meta_path, 'wb') as f:
            pickle.dump(meta, f)
    else:
        with open(meta_path, 'rb') as f:
            meta = pickle.load(f)
    return updated, file_path, meta


def get_diff(src, dest, apps):
    diff_path = os.path.join(BUILD_DIR, 'diff-' + dest.name)
    if not src.updated and not dest.updated and os.path.exists(diff_path) and not REMAKE:
        with open(diff_path, 'rb') as f:
            return False, pickle.load(f)

    log('Diff:', dest.name)
    src = src.open(True)
    dest = dest.open(False)
    broken_trains = src.diff_site(dest, apps)
    src.close()
    dest.close()

    if broken_trains:
        log('Bad dependencies:\n' + '\n'.join(broken_trains), file=sys.stderr)
    diff = src.visited - src.broken
    with DeleteOnError(diff_path, 'wb') as f:
        pickle.dump(diff, f)
    return True, diff


async def thread_run(*func_args, use_cache=True):
    if use_cache and func_args in CACHE:
        future = CACHE[func_args]
    else:
        future = asyncio.wrap_future(pool.submit(*func_args))
        if use_cache:
            CACHE[func_args] = future
    return await future


async def add_source_line(source_line):
    if not source_line.strip():
        return None, ()
    mirror, dist, *comps, arch = source_line.split()
    release = await thread_run(get_release, mirror, dist)
    tasks = []
    for comp in comps:
        for suffix in ('.xz', '.bz2', '.gz', ''):
            match = re.search(r'^.+\s+%s/binary-%s/Packages%s\s*$' % (comp, arch, suffix), release, re.M)
            if match:
                sha256, size, path = match.group(0).split()
                tasks.append(thread_run(get_packages, mirror, dist, path, int(size), sha256))
                break
        else:
            raise Exception('No Packages indices: ' + source_line)
    return mirror, await asyncio.gather(*tasks)


async def create_site(site_source, name=None):
    site = repo.Site(name)
    tasks = asyncio.gather(*[add_source_line(x) for x in site_source.splitlines()])
    for mirror, comp_result in await tasks:
        for updated, file_path, meta in comp_result:
            site.add(file_path, MIRRORS[mirror], updated, meta)
    return site


async def main():
    other_sites = [asyncio.create_task(create_site(v, k)) for k, v in SITE_SOURCES.items()]
    deepin_site = await create_site(DEEPIN_SITE_SOURCE)

    apps = ', '.join(x for meta in deepin_site.meta_list for x in meta if x.endswith('.deepin'))

    tasks = []
    for site in asyncio.as_completed(other_sites):
        tasks.append(asyncio.create_task(thread_run(get_diff, deepin_site, await site, apps, use_cache=False)))
    result = await asyncio.gather(*tasks)

    if any(x[0] for x in result) or not os.path.exists(OUTPUT) or REMAKE:
        log('Dumping:', OUTPUT)
        deepin_site.open(False)
        with DeleteOnError(OUTPUT, 'wt') as f:
            deepin_site.dump(set.union(*[x[1] for x in result]), f)

            for site_source, pkg_names in EXTRA_PACKAGES:
                site = await create_site(site_source)
                site.open(False)
                for pkg_name in pkg_names:
                    site.dump([x[0] for x in site.get_packages(pkg_name)], f)
                site.close()
        deepin_site.close()


if __name__ == '__main__':
    pool = ThreadPoolExecutor()
    asyncio.run(main())
