#!/usr/bin/env python3
"""Download a YouTube playlist as FLAC audio files.

Requirements:
    pip install yt-dlp
    ffmpeg must be installed and on PATH (https://ffmpeg.org)

Usage:
    python yt_playlist_flac.py "https://www.youtube.com/playlist?list=..."
    python yt_playlist_flac.py URL -o ./music --quality 8
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

import yt_dlp


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Download a YouTube playlist as FLAC files."
    )
    p.add_argument("url", help="YouTube playlist (or single video) URL")
    p.add_argument(
        "-o", "--output-dir", default="flac_downloads",
        help="Directory for downloaded files (default: flac_downloads)",
    )
    p.add_argument(
        "--quality", type=int, default=0, choices=range(0, 13),
        metavar="0-12",
        help="FLAC compression level, 0=fastest ... 12=smallest (default: 0)",
    )
    p.add_argument(
        "--archive", default="download_archive.txt",
        help="File tracking already-downloaded videos so re-runs skip them",
    )
    p.add_argument(
        "--no-thumbnail", action="store_true",
        help="Do not embed the video thumbnail into the FLAC",
    )
    p.add_argument(
        "--playlist-start", type=int, default=1, metavar="N",
        help="First playlist item to download (1-based, default: 1)",
    )
    p.add_argument(
        "--playlist-end", type=int, default=None, metavar="N",
        help="Last playlist item to download (default: until the end)",
    )
    p.add_argument(
        "--cookies-from-browser", default=None, metavar="BROWSER",
        help="Load cookies from a browser (e.g. chrome, firefox) for "
             "age-restricted or members-only videos",
    )
    return p.parse_args()


def safe_filename(title: str, max_len: int = 180) -> str:
    """Make a title filesystem-safe; yt-dlp applies its own sanitizing too."""
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", title).strip(" .")
    return cleaned[:max_len] or "untitled"


def build_opts(args: argparse.Namespace) -> dict:
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    postprocessors = [
        # Convert the best available audio stream to FLAC
        {
            "key": "FFmpegExtractAudio",
            "preferredcodec": "flac",
            "preferredquality": str(args.quality),
        },
        # Copy title/artist/date/etc. from YouTube into FLAC tags
        {"key": "FFmpegMetadata"},
    ]
    if not args.no_thumbnail:
        postprocessors.append({"key": "EmbedThumbnail"})

    opts = {
        # Keep the playlist index in the filename so track order is preserved
        "outtmpl": {
            "default": str(out_dir / "%(playlist_index)03d - %(title)s.%(ext)s"),
            # Fallback for single videos (no playlist index)
            "chapter": str(out_dir / "%(playlist_index)03d - %(title)s - %(section_number)s %(section_title)s.%(ext)s"),
        },
        "format": "bestaudio/best",
        "postprocessors": postprocessors,
        # Embed thumbnails without needing mutagen
        "writethumbnail": not args.no_thumbnail,
        "ffmpeg_location": None,  # resolved automatically from PATH
        "download_archive": args.archive,
        "playliststart": args.playlist_start,
        "playlistend": args.playlist_end,
        "ignoreerrors": True,          # skip unavailable/private items, keep going
        "noplaylist": False,
        "overwrites": False,
        "retries": 5,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 4,
        "postprocessor_args": {
            "FFmpegExtractAudio": ["-compression_level", str(args.quality)],
        },
        "quiet": False,
        "no_warnings": False,
        "progress_hooks": [progress_hook],
        "match_filter": None,
    }

    if args.cookies_from_browser:
        opts["cookiesfrombrowser"] = (args.cookies_from_browser,)

    return opts


def progress_hook(d: dict) -> None:
    if d["status"] == "finished":
        print(f"  -> Downloaded: {d.get('filename', 'unknown')}")


def check_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        sys.exit(
            "Error: ffmpeg not found on PATH. Install it first:\n"
            "  macOS:   brew install ffmpeg\n"
            "  Ubuntu:  sudo apt install ffmpeg\n"
            "  Windows: winget install ffmpeg  (or download from ffmpeg.org)"
        )


def main() -> int:
    args = parse_args()
    check_ffmpeg()

    opts = build_opts(args)
    print(f"Downloading playlist -> {Path(args.output_dir).resolve()}")

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([args.url])
    except yt_dlp.utils.DownloadError as e:
        print(f"Download error: {e}", file=sys.stderr)
        return 1

    print("Done. FLAC files (and embedded tags/thumbnails) are ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())