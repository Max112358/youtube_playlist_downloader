#!/usr/bin/env python3
"""Sync a YouTube playlist's native audio to a local folder.

- Files go into a folder named after the playlist.
- Re-runs download only new videos, rename renamed/reordered tracks
  without re-downloading, and delete files for videos that were
  removed from the playlist.
- Audio stays in YouTube's native format (lossless stream copy) by
  default; --mp3 re-encodes once for players that need it.

Requirements:
    pip install yt-dlp
    ffmpeg must be installed and on PATH (https://ffmpeg.org)

Usage:
    python yt_playlist_sync.py "https://www.youtube.com/playlist?list=..."
    python yt_playlist_sync.py URL -o ~/music --mp3
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import yt_dlp
from yt_dlp.utils import sanitize_filename

MAP_FILE = ".ytsync_map.json"
ARCHIVE_FILE = ".download_archive.txt"
MEDIA_EXTS = {".opus", ".m4a", ".mp3", ".webm", ".ogg", ".aac", ".flac", ".wav"}
THUMB_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Sync a YouTube playlist's audio to a local folder."
    )
    p.add_argument("url", help="YouTube playlist (or single video) URL")
    p.add_argument(
        "-o", "--output-dir", default=".",
        help="Parent directory; a folder named after the playlist is "
             "created inside it (default: current directory)",
    )
    p.add_argument(
        "--mp3", action="store_true",
        help="Re-encode to MP3 (VBR ~245 kbps) for old players. Default "
             "is a lossless stream copy of YouTube's native audio.",
    )
    p.add_argument(
        "--no-thumbnail", action="store_true",
        help="Do not save thumbnail images alongside the audio",
    )
    p.add_argument(
        "--playlist-start", type=int, default=1, metavar="N",
        help="First playlist item to sync (1-based, default: 1)",
    )
    p.add_argument(
        "--playlist-end", type=int, default=None, metavar="N",
        help="Last playlist item to sync (default: until the end)",
    )
    p.add_argument(
        "--prune", action="store_true",
        help="Also delete media files in the folder that this script did "
             "not create (e.g. leftovers from earlier runs)",
    )
    p.add_argument(
        "--cookies-from-browser", default=None, metavar="BROWSER",
        help="Load cookies from a browser (e.g. chrome, firefox) for "
             "age-restricted or members-only videos",
    )
    return p.parse_args()


def fetch_playlist(args) -> tuple[str, list[tuple[str, str]]]:
    """Return (playlist title, [(video id, title), ...]) in order."""
    opts = {
        "extract_flat": "in_playlist",  # fast: no per-video page fetches
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "skip_download": True,
        "playliststart": args.playlist_start,
        "playlistend": args.playlist_end,
    }
    if args.cookies_from_browser:
        opts["cookiesfrombrowser"] = (args.cookies_from_browser,)

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(args.url, download=False)
    if info is None:
        sys.exit("Error: could not read the playlist (is the URL correct?)")

    if info.get("_type") == "playlist":
        name = info.get("title") or "playlist"
        raw = [e for e in (info.get("entries") or []) if e]
        if not raw:
            sys.exit("Error: playlist appears empty; refusing to delete "
                     "anything. Re-run once you can see items in it.")
        entries = [(e["id"], e.get("title") or "untitled") for e in raw]
    else:  # single video URL
        name = info.get("title") or info.get("id") or "video"
        entries = [(info["id"], info.get("title") or "untitled")]
    return name, entries


def desired_stem(index: int, title: str) -> str:
    """Final filename (without extension) for a playlist entry."""
    return f"{index:03d} - {sanitize_filename(title)}"[:200]


def load_map(folder: Path) -> dict[str, str]:
    p = folder / MAP_FILE
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_map(folder: Path, mapping: dict[str, str]) -> None:
    (folder / MAP_FILE).write_text(
        json.dumps(mapping, indent=2, sort_keys=True), encoding="utf-8"
    )


def purge_archive(archive: Path, ids: list[str]) -> None:
    """Drop ids from the archive so they re-download next time."""
    if not ids or not archive.exists():
        return
    drop = set(ids)
    lines = archive.read_text(encoding="utf-8").splitlines()
    keep = [l for l in lines if l.rsplit(" ", 1)[-1] not in drop]
    archive.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")


def delete_tracked(folder: Path, fname: str) -> None:
    f = folder / fname
    if f.exists():
        f.unlink()
        print(f"  deleted: {fname}")
    stem = Path(fname).stem
    for ext in THUMB_EXTS:
        t = folder / (stem + ext)
        if t.exists():
            t.unlink()


def rename_file(folder: Path, old: str, new_stem: str) -> str | None:
    old_path = folder / old
    if not old_path.exists():
        return None
    new_name = new_stem + old_path.suffix
    old_path.rename(folder / new_name)
    for ext in THUMB_EXTS:
        t = folder / (Path(old).stem + ext)
        if t.exists():
            t.rename(folder / (new_stem + ext))
    return new_name


def sync_local(
    folder: Path, entries: list[tuple[str, str]], mapping: dict[str, str], prune: bool
) -> tuple[dict[str, str], list[str]]:
    """Reconcile the folder with the playlist. Returns (desired stems by
    id, ids that still need downloading)."""
    desired = {vid: desired_stem(i, t) for i, (vid, t) in enumerate(entries, 1)}
    archive = folder / ARCHIVE_FILE

    # Videos removed from the playlist -> delete local files
    gone = [vid for vid in mapping if vid not in desired]
    for vid in gone:
        delete_tracked(folder, mapping.pop(vid))
    purge_archive(archive, gone)

    # Title change / reorder -> rename in place; vanished file -> re-download
    missing: list[str] = []
    for vid, stem in desired.items():
        if vid not in mapping:
            missing.append(vid)
            continue
        fname = mapping[vid]
        if not (folder / fname).exists():
            missing.append(vid)
            del mapping[vid]
            continue
        if Path(fname).stem != stem:
            new_name = rename_file(folder, fname, stem)
            if new_name:
                mapping[vid] = new_name
                print(f"  renamed: {fname} -> {new_name}")
    purge_archive(archive, missing)

    if prune:
        keep = {Path(v).stem for v in mapping.values()}
        for f in folder.iterdir():
            if f.is_file() and f.suffix.lower() in MEDIA_EXTS and f.stem not in keep:
                f.unlink()
                print(f"  deleted (untracked): {f.name}")

    return desired, missing


def build_download_opts(args, folder: Path) -> dict:
    postprocessors = [{"key": "FFmpegMetadata"}]
    opts = {
        # Temporary name; sync renames it to "NNN - Title.<ext>" afterwards.
        # Matching by [%(id)s] is reliable no matter how titles are sanitized.
        "outtmpl": str(folder / "%(title)s [%(id)s].%(ext)s"),
        "format": "bestaudio/best",
        "postprocessors": postprocessors,
        "download_archive": str(folder / ARCHIVE_FILE),
        "playliststart": args.playlist_start,
        "playlistend": args.playlist_end,
        "ignoreerrors": True,
        "noplaylist": False,
        "overwrites": False,
        "retries": 5,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 4,
        "progress_hooks": [progress_hook],
    }

    if args.mp3:
        postprocessors.insert(0, {
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "0",  # LAME VBR highest (~245 kbps)
        })
        opts["postprocessor_args"] = {"FFmpegExtractAudio": ["-q:a", "0"]}
        if not args.no_thumbnail:
            postprocessors.append({"key": "EmbedThumbnail"})
            opts["writethumbnail"] = True
    else:
        # Lossless remux into a friendly container, zero re-encoding
        opts["remux_video"] = "m4a/opus"
        opts["writethumbnail"] = not args.no_thumbnail

    if args.cookies_from_browser:
        opts["cookiesfrombrowser"] = (args.cookies_from_browser,)
    return opts


def claim_downloads(
    folder: Path, desired: dict[str, str], mapping: dict[str, str]
) -> None:
    """Rename freshly downloaded 'Title [id].ext' files to final names."""
    for vid, stem in desired.items():
        if vid in mapping and (folder / mapping[vid]).exists():
            continue
        matches = [
            f for f in folder.iterdir()
            if f.is_file() and f.stem.endswith(f" [{vid}]")
        ]
        media = [m for m in matches if m.suffix.lower() in MEDIA_EXTS]
        for m in matches:
            m.rename(folder / (stem + m.suffix))
        if media:
            mapping[vid] = stem + media[0].suffix


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

    print("Reading playlist...")
    playlist_name, entries = fetch_playlist(args)

    parent = Path(args.output_dir)
    parent.mkdir(parents=True, exist_ok=True)
    folder = parent / sanitize_filename(playlist_name)[:150]
    folder.mkdir(parents=True, exist_ok=True)

    mapping = load_map(folder)
    desired, missing = sync_local(folder, entries, mapping, args.prune)
    save_map(folder, mapping)

    if missing:
        print(f"Downloading {len(missing)} new/updated item(s) -> {folder}")
        opts = build_download_opts(args, folder)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([args.url])
        except yt_dlp.utils.DownloadError as e:
            print(f"Download error: {e}", file=sys.stderr)
            return 1
        claim_downloads(folder, desired, mapping)
        save_map(folder, mapping)
    else:
        print(f"Already in sync: {folder}")

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())