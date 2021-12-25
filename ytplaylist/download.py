"""Download or update a youtube playlist"""

from __future__ import annotations

import argparse
import glob
import itertools
import json
import logging
import os
import re
import subprocess
import shutil
import tempfile
import urllib.parse
from dataclasses import dataclass
from datetime import timedelta
from os import path
from typing import IO, Iterator, Mapping, Optional, TypedDict

import isodate  # type: ignore
import requests

logging.basicConfig(
    format='%(levelname)s: %(message)s',
    level=logging.NOTSET,
)
logger = logging.getLogger(__name__)


class LocalDBElement(TypedDict):
    """Typing for locale database item"""
    id: str
    duration: int
    title: str
    locale: dict[str, str]


def get_api_key() -> Optional[str]:
    """Get Youtube API key from environment"""
    try:
        return os.environ['YOUTUBE_API_KEY']
    except KeyError:
        logger.warning("YOUTUBE_API_KEY is not set")
        return None


def get_parser() -> argparse.ArgumentParser:
    """Create CLI argument parser"""
    parser = argparse.ArgumentParser(
        description=__doc__
    )
    parser.add_argument(
        '--verbose', '-v', action='store_true',
        help="be verbose"
    )
    parser.add_argument(
        '--output', '-o',
        help="output directory for media files with pretty names, "
             + "default to working directory"
    )
    parser.add_argument(
        '--output-raw', '-O',
        help="output directory for raw media files, default to {output}-raw"
    )
    parser.add_argument(
        '--output-locale', '-e',
        help="output directory for media files with localized names, "
             + "%%(locale)s will be replaced by the locale name, "
             + "default to {output}-%%(locale)s"
    )
    parser.add_argument(
        '--locale', '-l', default=[], action='append',
        help="set localization files to create, default to none"
    )
    parser.add_argument(
        '--audio-only', '-a', action='store_true',
        help="only download the audio"
    )
    parser.add_argument(
        '--playlist', '-p', default=None,
        help="create or update M3U file"
    )
    parser.add_argument(
        '--playlist-fmt', '-P', choices=('normal', 'vlc'), default='normal',
        help="playlist format (VLC need a special file format "
             + "if path contains spaces or special characters), "
             + "defaults to normal"
    )
    parser.add_argument(
        '--playlist-locale', '-L',
        help="select M3U playlist localization"
    )
    parser.add_argument(
        '--playlist-abs', '-A', action='store_true',
        help="use absolute paths rather than relative"
    )
    parser.add_argument(
        '--playlist-overwrite', '-f', action='store_true',
        help="don't read current M3U playlist content, overwrite it"
    )
    parser.add_argument(
        '--local-db', '-d',
        help="local video information database: json with the format: "
             + '[{"id": x, "title": x, "locale": {"en": x, ...}}, ...]; '
             + 'warning: if you manually added data, backup the db'
    )
    parser.add_argument(
        '--update-db', '-u', action='store_true',
        help="update local video database with playlist info"
    )
    parser.add_argument(
        '--update-all', '-U', action='store_true',
        help="fetch video information for video already present in the db"
    )
    parser.add_argument(
        '--ytdl-extra', '-x', action='append', default=[],
        help="extra youtube-dl/youtube-dlp argument"
    )
    parser.add_argument(
        'playlists', metavar='PLAYLIST', nargs='+',
        help="playlists to download"
    )
    return parser


def sanitize_name(name: str):
    """Sanitize file name"""
    return name.replace('/', '∕')


@dataclass(frozen=True)
class VideoInfo():
    """Standard video information dataclass"""
    vid: str
    duration: timedelta
    __title: str
    __locale_title: frozenset[tuple[str, str]]
    missing: bool

    def __hash__(self) -> int:
        return hash(self.vid)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, VideoInfo):
            return hash(self) == hash(other)
        return False

    def title(self, locale: str = None) -> str:
        """Get localized video title"""
        if locale is None:
            return self.__title
        iterator = iter(b for a, b in self.__locale_title if a == locale)
        return next(iterator, self.__title)
        # return self.__locale_title.get(locale, self.__title)

    def export_db(self) -> LocalDBElement:
        """Create element ready to be exported to JSON database"""
        return {
            'id': self.vid,
            'duration': self.duration.seconds,
            'title': self.__title,
            'locale': dict(self.__locale_title),
            # 'locale': self.__locale_title,
        }

    @staticmethod
    def from_youtube(vid: str) -> VideoInfo:
        """Create from Youtube metadata"""
        base_url = 'https://www.googleapis.com/youtube/v3/videos'
        params = {
            'key': get_api_key(),
            'part': 'contentDetails,id,localizations,snippet',
            'maxResults': 1,
            'id': vid
        }

        logger.debug("Youtube request for info: %s", vid)
        req = requests.get(base_url, params)
        logger.debug("Got response for %s", req.url)
        if not req.ok:
            logger.error("Failed to send request %s for video %s", req, vid)
            raise Exception(f"Request {req.url} failed")
        data = req.json()
        video = data['items'][0]

        duration: timedelta = isodate.parse_duration(
            video['contentDetails']['duration']
        )
        title = video['snippet']['title']
        locale_title: dict[str, str] = {}
        for locale, info in video.get('localizations', {}).items():
            locale_title[locale[:2]] = info['title']

        return VideoInfo(vid, duration, title,
                         frozenset(locale_title.items()), False)

    @staticmethod
    def from_local(item: LocalDBElement) -> VideoInfo:
        """Create from database item"""
        return VideoInfo(item['id'], timedelta(seconds=item['duration']),
                         item['title'], frozenset(item['locale'].items()),
                         False)

    @staticmethod
    def from_missing(vid: str) -> VideoInfo:
        """Create placeholder for missing video"""
        return VideoInfo(vid, timedelta(), vid, frozenset(), True)


def m3u_get_ids(stream: IO[str], url: bool = False) -> Iterator[str]:
    """Get video IDs from M3U playlist"""
    logger.debug("Parsing M3U playlist")

    for line in stream:
        if line[0] == '#':
            continue
        vid = path.basename(line).split('.')[0]
        if url:
            vid = urllib.parse.unquote(vid)
        logger.debug("Found video %s", vid)
        yield vid


def m3u_create(
        stream: IO[str],
        items: Mapping[VideoInfo, Optional[str]],
        url: bool = False,
        basepath: Optional[str] = None,
        locale: Optional[str] = None,
        ) -> None:
    """Write M3U playlist"""
    logger.debug("Creating M3U playlist")

    stream.write('#EXTM3U\n')
    for vidinfo, vid_path in items.items():
        if vid_path is None:
            logger.error("Video not found: %s", vidinfo)
            continue

        if basepath is None:
            vid_path = path.abspath(vid_path)
        else:
            vid_path = path.relpath(vid_path, basepath)
        if url:
            vid_path = urllib.parse.quote(vid_path)
        stream.writelines((
            f'#EXTINF:{vidinfo.duration.seconds},{vidinfo.title(locale)}\n',
            f'{vid_path}\n',
        ))
        logger.debug("M3U item: %s", vid_path)


def id_from_path(file: str) -> Optional[str]:
    """Get youtube video ID from a file path"""
    id_exp = r'([A-Za-z0-9_\-]{8,})\.[a-z0-9]{1,4}'
    file = path.basename(path.realpath(file))
    match = re.match(id_exp, file)
    if not match:
        return None
    return match.group(1)


def listdir_abs(dir_: str) -> Iterator[str]:
    """os.listdir but yields full paths"""
    yield from (path.normpath(path.join(dir_, k)) for k in os.listdir(dir_))


def youtube_get_ids(playlist_id: str) -> Iterator[str]:
    """Get video IDs from YouTube playlist"""
    logger.debug("Getting YouTube playlist: %s", playlist_id)

    base_url = 'https://youtube.googleapis.com/youtube/v3/playlistItems'
    params = {
        'key': get_api_key(),
        'part': 'contentDetails',
        'playlistId': playlist_id,
        'maxResults': 50,
        'pageToken': None,
    }

    while True:
        req = requests.get(base_url, params)
        logger.debug("Got response for %s", req.url)
        if not req.ok:
            logger.error("Failed to send request %s for %s", req, playlist_id)
            raise Exception(f"Request {req.url} failed")
        data = req.json()

        for video in data['items']:
            vid = video['contentDetails']['videoId']
            logger.debug("Found playlist item: %s", vid)
            yield vid

        if data.get('nextPageToken') is None:
            break
        params['pageToken'] = data['nextPageToken']


def create_symlinks_locale(videos: Mapping[VideoInfo, Optional[str]],
                           dst: str, locale: str = None) -> None:
    """Create symlinks with pretty names"""
    def find_video_link(dir_: str, target: str) -> Iterator[str]:
        """Find files which are symlinks pointing to target"""
        for absfile in listdir_abs(dir_):
            if path.isfile(absfile) and path.islink(absfile) \
                    and path.realpath(absfile) == path.realpath(target):
                yield absfile

    if not path.isdir(dst):
        os.mkdir(dst)

    # Check dst directory content
    for file in listdir_abs(dst):
        if not path.islink(file) or id_from_path(file) is None:
            logger.warning("Found non valid file: %s", file)
        elif not path.exists(file):
            logger.debug("Removing dead symlink: %s", file)

    for vidinfo, raw_path in videos.items():
        if raw_path is None:
            logger.error("Video not found: %s", vidinfo)
            continue
        ext = path.basename(raw_path).removeprefix(f'{vidinfo.vid}.')
        title_filename = sanitize_name(vidinfo.title(locale)) + f'.{ext}'

        found = 0
        for link in find_video_link(dst, raw_path):
            if path.basename(link) == title_filename:
                logger.debug("Found video: %s = %s", vidinfo, link)
                found += 1
            else:
                logger.debug("Removing invalid link: %s", link)
                os.unlink(link)

        if not found:
            target = path.relpath(raw_path, dst)
            name = f"{dst}/{title_filename}"
            if path.islink(name):
                logger.debug("Removing invalid link: %s", name)
                os.unlink(name)
            logger.debug("Creating link: %s -> %s", name, target)
            os.symlink(target, name)


def youtube_dl(vid: str, output_dir: str,
               *args: str, audio_only: bool = False) -> None:
    """Download a Youtube video"""
    if shutil.which('yt-dlp'):
        exe = 'yt-dlp'
    elif shutil.which('youtube-dl'):
        exe = 'youtube-dl'
    else:
        raise FileNotFoundError("Neither yt-dlp nor youtube-dl is found")

    if not path.isdir(output_dir):
        os.mkdir(output_dir)

    with tempfile.TemporaryDirectory(prefix='ytplaylist.') as tmpdir:
        opts = [exe, '--ignore-errors', '--embed-subs', '--embed-thumbnail',
                '--output', path.join(tmpdir, '%(id)s.%(ext)s')]
        if audio_only:
            opts.append('--extract-audio')
        if exe == 'yt-dlp':
            opts.extend(('--embed-metadata', '--embed-chapters'))
        else:
            opts.append('--add-metadata')
        opts.extend(args)
        opts.append('--')
        opts.append(vid)

        logger.debug("Calling: %s", opts)
        subprocess.run(opts, capture_output=False, check=True)

        files = glob.glob(path.join(tmpdir, '*'))
        if not files:
            raise Exception(f"Failed to download video {vid}")
        if len(files) > 1:
            logger.warning("Downloaded %s files, expected 1", len(files))

        for file in files:
            if path.basename(file).count('.') == 1:
                break
        else:
            raise Exception(f"Did not find downloaoded file in {files}")

        new_name = path.join(output_dir, path.basename(file))
        shutil.move(file, new_name)
    logger.debug("Downloaded %s", new_name)


def read_db(db_path: str) -> list[VideoInfo]:
    """Read database from JSON"""
    with open(db_path, 'r', encoding='UTF-8') as stream:
        # TODO: type check json content
        json_db: list[LocalDBElement] = json.load(stream)
    return [VideoInfo.from_local(k) for k in json_db]


def main(argv: list[str] = None) -> None:
    """CLI entry point"""

    def abspath_default_suffix(arg: Optional[str],
                               default: str = '.', suffix: str = '') -> str:
        """Default value is default directory with a suffix"""
        if arg is not None:
            return path.abspath(arg)
        abs_default = path.abspath(default)
        return path.join(
            path.dirname(abs_default), path.basename(abs_default) + suffix
        )

    # Parse arguments
    parser = get_parser()
    args = parser.parse_args(argv)

    if args.verbose:
        level = logging.DEBUG
    else:
        level = logging.INFO
    logging.getLogger().setLevel(level)

    logger.debug("Arguments: %s", args)

    output = abspath_default_suffix(args.output)
    output_raw = abspath_default_suffix(args.output_raw, output, '-raw')
    output_locale = abspath_default_suffix(args.output_locale,
                                           output, '-%(locale)s')

    # Read local database
    local_db: list[VideoInfo]
    if args.local_db is not None:
        logger.info("Reading local database: %s", args.local_db)
        local_db = read_db(args.local_db)
    else:
        local_db = []

    # Read local playlist
    if args.playlist is not None and not args.playlist_overwrite \
            and path.isfile(args.playlist):
        logger.info("Parsing M3U playlist: %s", args.playlist)
        with open(args.playlist, 'r', encoding='UTF-8') as playlist_stream:
            playlist_vids = list(
                m3u_get_ids(playlist_stream, args.playlist_fmt)
            )
    else:
        playlist_vids = []
    logger.debug("Initial playlist from %s: %s", args.playlist, playlist_vids)

    # Update playlist with Youtube playlist
    logger.info("Getting playlist info: %s", args.playlists)
    playlist_vids.extend((
        vid for vid in itertools.chain.from_iterable(
            map(youtube_get_ids, args.playlists)
        ) if vid not in playlist_vids
    ))
    logger.debug("Updated playlist: %s", playlist_vids)

    # Get video info
    vidinfo: VideoInfo
    vid_path: dict[VideoInfo, Optional[str]] = {}
    for vid in playlist_vids:
        vidinfo = VideoInfo.from_missing(vid)
        if local_db is not None and vidinfo in local_db:
            vidinfo = local_db[local_db.index(vidinfo)]
            logger.debug("Found video in database: %s", vidinfo)
        else:
            logger.debug("Not found in database: %s", vid)

        if vidinfo.missing or args.update_all:
            try:
                vidinfo = VideoInfo.from_youtube(vid)
                logger.debug("Got video info from Youtube: %s", vidinfo)
            except Exception:
                logger.debug("Video info not found on Youtube: %s", vid)

        if vidinfo.missing:
            logger.error("Failed to get video information for %s", vid)
        vid_path[vidinfo] = None
    # logger.debug("Playlist metadata: %s", vid_path.keys())

    # Get video info from Youtube
    # vid_path: dict[YoutubeInfo, Optional[str]] = {
    #     YoutubeInfo(k): None for k in playlist_vids
    # }

    def find_video_raw(dir_: str, vid: str) -> Optional[str]:
        """Find the path of the raw video"""
        return next((
            k for k in listdir_abs(dir_)
            if id_from_path(k) == vid and path.isfile(k)
        ), None)
        # name = glob.escape(f'{dir_}/{vid}') + '.*'
        # return next(glob.iglob(name), None)

    # Download missing videos
    logger.info("Downloading missing videos")
    for vidinfo in vid_path:
        file = find_video_raw(output_raw, vidinfo.vid)
        if file is None:
            logger.info("Downloading video: %s", vidinfo)
            try:
                youtube_dl(
                    vidinfo.vid,
                    output_raw,
                    *args.ytdl_extra,
                    audio_only=args.audio_only,
                )
            except subprocess.CalledProcessError:
                logger.exception("Failed to download video: %s", vidinfo)
                continue
            file = find_video_raw(output_raw, vidinfo.vid)
            if file is None:
                raise Exception(f"Downloaded video not found: {vidinfo}")
        vid_path[vidinfo] = file
    logger.debug("Playlist content: %s", vid_path)

    # Create pretty names symlinks
    for locale in itertools.chain((None,), args.locale):
        if locale is not None:
            dest_dir = output_locale % {'locale': locale}
        else:
            dest_dir = output
        logger.info("Creating video links in %s", dest_dir)
        create_symlinks_locale(vid_path, dest_dir, locale)

    # Create locale playlist
    if args.playlist is not None:
        logger.info("Creating M3U playlist %s", args.playlist)
        with open(args.playlist, 'w', encoding='UTF-8') as playlist_stream:
            m3u_create(
                playlist_stream,
                vid_path,
                args.playlist_fmt == 'vlc',
                None if args.playlist_abs else path.dirname(args.playlist),
                args.playlist_locale,
            )

    # Update db
    if args.update_db:
        logger.info("Updating local database: %s", args.local_db)
        for vidinfo in vid_path:
            if vidinfo.missing:
                continue
            if vidinfo not in local_db:
                local_db.append(vidinfo)
            elif args.update_all:
                local_db[local_db.index(vidinfo)] = vidinfo
        with open(args.local_db, 'w', encoding='UTF-8') as stream:
            json.dump([k.export_db() for k in local_db], stream, indent=2)


if __name__ == '__main__':
    main()
