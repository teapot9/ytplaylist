"""M3U utils"""

import argparse
import enum
import logging
import os
import shutil
import tempfile
from typing import Iterator, Optional
from urllib.parse import quote, unquote, urlparse
from os import path
from xml.etree import ElementTree

logger = logging.getLogger(__name__)


class Encoding(enum.Enum):
    """Encoding format for paths"""
    #: Normal UTF-8 path encoding
    NORMAL = enum.auto()
    #: Paths are encoded in URL format (``%20``)
    URL = enum.auto()


def encode(uri: str, dirname: str, absolute: bool = False,
           encoding: Encoding = Encoding.NORMAL, resolve: bool = False) -> str:
    """Encode a given URI"""
    scheme = urlparse(uri, scheme='file').scheme
    uri_orig = uri

    if scheme == 'file':
        uri = uri.removeprefix('file://')
        prefix = '' if path.isabs(uri) else dirname

        if not path.exists(path.join(prefix, uri)):
            uri = unquote(uri)
        if not path.exists(path.join(prefix, uri)):
            logger.error("Parsed %s as %s: file not found",
                         uri_orig, path.join(prefix, uri))
            raise Exception(f"Cannot parse URI {uri_orig}")

        uri = path.join(prefix, uri)
        if resolve:
            uri = path.realpath(uri)
        if not absolute:
            uri = path.relpath(uri, start=dirname)
        if encoding is Encoding.URL:
            uri = quote(uri)

    logger.debug("Parsed %s as %s", uri_orig, uri)
    return uri


def main_sanitize() -> None:
    """m3u_sanitize entry point"""
    parser = argparse.ArgumentParser(
        description="Convert filenames format of M3U8 playlists"
    )
    parser.add_argument(
        '--verbose', '-v', action='count', default=0,
        help="be verbose",
    )
    parser.add_argument(
        '--encoding', '-e', type=str,
        choices=('normal', 'url'), default='normal',
        help="select encoding of file paths",
    )
    parser.add_argument(
        '--absolute', '-a', action='store_true',
        help="use absolute paths rather than relative",
    )
    parser.add_argument(
        '--symlinks', '-s', action='store_true',
        help="expand symlinks",
    )
    parser.add_argument(
        'playlists', type=str, nargs='+',
        help="playlists to update",
    )
    args = parser.parse_args()

    if args.verbose >= 2:
        logger.setLevel(logging.DEBUG)
    elif args.verbose >= 1:
        logger.setLevel(logging.INFO)
    else:
        logger.setLevel(logging.WARNING)

    encoding = Encoding.URL if args.encoding == 'url' \
        else Encoding.NORMAL if args.encoding == 'normal' \
        else None
    assert encoding is not None

    for playlist_path in args.playlists:
        logger.info("Reading %s", playlist_path)
        dirname = path.abspath(path.dirname(playlist_path))

        with tempfile.TemporaryFile(mode='w+') as fdst:
            with open(playlist_path, 'r', encoding='UTF-8') as fsrc:
                for line in fsrc:
                    if line.startswith('#'):
                        fdst.write(line)
                        continue

                    line = line.removesuffix('\n')
                    line = encode(line, dirname, args.absolute, encoding,
                                  args.symlinks)
                    fdst.write(line)
                    fdst.write('\n')

            fdst.seek(0)
            with open(playlist_path, 'w', encoding='UTF-8') as output:
                shutil.copyfileobj(fdst, output)


def compat(playlist: str, dest_dir: str) -> None:
    """Create compatibility symlinks for playlist"""
    base = path.dirname(playlist)
    with open(playlist, 'r', encoding='UTF-8') as stream:
        lines = stream.readlines()

    for index, line in enumerate(lines):
        if not line.startswith('#'):
            line = line.removesuffix('\n')
            file_prefix = False
            if line.startswith('file://'):
                file_prefix = True
                line = line.removeprefix('file://')

            if path.isabs(line):
                abs_path = True
            else:
                abs_path = False
                line = path.join(base, line)

            url_encoded = False
            if not path.isfile(line):
                url_encoded = True
                line = unquote(line)
            if not path.isfile(line):
                raise FileNotFoundError(f"{line}: file not found")

            ext = line.split('.')[-1]
            link = path.join(dest_dir, f"{index}.{ext}")
            target = path.normpath(path.relpath(line, dest_dir))
            os.symlink(target, link)
            line = path.normpath(path.relpath(link, base))

            if abs_path:
                path.normpath(path.join(base, line))
            if url_encoded:
                line = quote(line)
            if file_prefix:
                line = 'file://' + line
            lines[index] = line + '\n'

    with open(playlist, 'w', encoding='UTF-8') as stream:
        stream.writelines(lines)


def main_compat() -> None:
    """m3u_compat entry point"""
    parser = argparse.ArgumentParser(
        description="Create compatibility layer for a M3U playlist"
    )
    parser.add_argument('playlist', help="playlist to edit")
    parser.add_argument('dest', help="directory for symlinks")
    args = parser.parse_args()

    if not path.isdir(args.dest):
        os.mkdir(args.dest)
    compat(args.playlist, args.dest)


def m3u_list(src: str) -> Iterator[str]:
    """List paths of an M3U playlist"""
    with open(src, 'r', encoding='UTF-8') as stream:
        for line in stream:
            if not line.startswith('#'):
                line = line.removeprefix('file://').removesuffix('\n')
                file = line
                if not path.isabs(line):
                    file = path.join(path.dirname(path.abspath(src)), file)
                if not path.isfile(file):
                    file = unquote(file)
                    line = unquote(line)
                if not path.isfile(file):
                    raise FileNotFoundError(f"{file}")
                yield line


def main_list() -> None:
    """m3u_list entry point"""
    parser = argparse.ArgumentParser(
        description="list playlist file paths"
    )
    parser.add_argument('playlist', help="playlist to print")
    args = parser.parse_args()

    for item in m3u_list(args.playlist):
        print(item)


def move(file: str, dst: str) -> None:
    """Update relative paths"""
    diff = path.relpath(path.dirname(dst), path.dirname(file))

    with open(file, 'r', encoding='UTF-8') as stream:
        lines = stream.readlines()

    for index, line in enumerate(lines):
        if not line.startswith('#'):
            file_prefix = False
            if line.startswith('file://'):
                file_prefix = True
                line = line.removeprefix('file://')

            if not path.isabs(line):
                line = path.normpath(path.join(diff, line))

            if file_prefix:
                file = 'file://' + line
            lines[index] = line

    with open(dst, 'w', encoding='UTF-8') as stream:
        stream.writelines(lines)


def main_move() -> None:
    """m3u_move entry point"""
    parser = argparse.ArgumentParser(
        description="Move a M3U playlist, updating any relative file paths"
    )
    parser.add_argument('file', nargs='+', help="source playlists")
    parser.add_argument('dst', help="destination file or directory")
    args = parser.parse_args()

    for file in args.file:
        fdst = args.dst
        if path.isdir(fdst):
            fdst = path.join(fdst, path.basename(file))
        move(file, fdst)
        shutil.move(file, fdst)


def xspf_to_m3u(src: str, dst: str) -> None:
    """Convert to M3U"""
    root: Optional[ElementTree.Element] = ElementTree.parse(src).getroot()
    assert root is not None
    root = root.find("{http://xspf.org/ns/0/}trackList")
    assert root is not None
    with open(dst, 'w', encoding='UTF-8') as stream:
        stream.write('#EXTM3U\n')
        track: Optional[ElementTree.Element]
        for track in root.findall("{http://xspf.org/ns/0/}track"):
            search = track.find("{http://xspf.org/ns/0/}location")
            assert search is not None and search.text is not None
            file = search.text
            file = unquote(file).removeprefix('file://')
            if not path.isfile(file):
                raise FileNotFoundError(f"{file}")

            search = track.find("{http://xspf.org/ns/0/}title")
            assert search is not None
            title = search.text
            search = track.find("{http://xspf.org/ns/0/}duration")
            assert search is not None and search.text is not None
            duration = int(search.text)
            stream.write(f'#EXTINF:{duration},{title}\n')
            stream.write(f'{file}\n')


def main_xspf2m3u() -> None:
    """xspf2m3u entry point"""
    parser = argparse.ArgumentParser(
        description="Convert a XSPF playlist to a M3U"
    )
    parser.add_argument('src', help="XSPF playlist to convert")
    parser.add_argument('dest', nargs='?', help="output M3U file")
    args = parser.parse_args()

    if args.dest is None:
        dst = args.src.removesuffix('.xspf') + '.m3u8'
    else:
        dst = args.dest

    xspf_to_m3u(args.src, dst)


def xspf_list(src: str) -> Iterator[str]:
    """List XSPF playlist items"""
    root: Optional[ElementTree.Element] = ElementTree.parse(src).getroot()
    assert root is not None
    root = root.find('{http://xspf.org/ns/0/}trackList')
    assert root is not None
    item: Optional[ElementTree.Element]
    for item in root.findall('{http://xspf.org/ns/0/}track'):
        item = item.find('{http://xspf.org/ns/0/}location')
        assert item is not None and item.text is not None
        yield unquote(item.text).removeprefix('file://')


def main_xspf_list() -> None:
    """xspf_list entry point"""
    parser = argparse.ArgumentParser(
        description="List file paths of a XSPF playlist"
    )
    parser.add_argument('src', help="XSPF file to read")
    args = parser.parse_args()

    for item in xspf_list(args.src):
        print(item)


def copy_files(src: str, dst: str, force: bool = False) -> None:
    """Copy files from an M3U playlist into a directory"""
    for file in m3u_list(src):
        if not path.isabs(file):
            file = path.join(path.dirname(src), file)
        dst_file = path.join(dst, path.basename(file))
        if not force and not path.isfile(dst_file):
            logger.info("Copying %s -> %s", file, dst_file)
            shutil.copy(file, dst_file, follow_symlinks=True)
        else:
            logger.debug("%s: already present", dst_file)


def main_copy() -> None:
    """m3u_copy entry point"""
    parser = argparse.ArgumentParser(
        description="Copy playlist files into a directory"
    )
    parser.add_argument('src', nargs='+', help="playlists to copy files")
    parser.add_argument('dst', help="destination directory")
    args = parser.parse_args()

    for src in args.src:
        copy_files(src, args.dst)
