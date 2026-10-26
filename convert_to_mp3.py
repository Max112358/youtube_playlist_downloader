#!/usr/bin/env python3
"""Convert audio files to MP3 for car stereos, recursively.

Scans the given folder and every subfolder. Handles .webm, .opus,
.flac, .m4a, .aac, .ogg — anything ffmpeg can read.
LAME VBR V0 (~245 kbps).

Usage:
    python convert_to_mp3.py "Some Folder"
    python convert_to_mp3.py "Some Folder" --keep

- Embeds cover art (sidecar jpg/png if present, otherwise the
  picture stream stored inside the FLAC).
- Deletes originals unless --keep.
- Updates .ytsync_map.json in each subfolder when present, so the
  sync tool stays consistent for synced playlists.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

QSCALE = "0"  # LAME VBR V0 (~245 kbps); "2" ≈ 190 kbps
FORMATS = {".webm", ".opus", ".flac", ".m4a", ".aac", ".ogg"}
MAP_FILE = ".ytsync_map.json"
THUMB_EXTS = (".jpg", ".jpeg", ".png", ".webp")


def find_sidecover(src: Path) -> Path | None:
    for ext in THUMB_EXTS:
        p = src.with_suffix(ext)
        if p.exists():
            return p
    return None


def has_embedded_cover(src: Path) -> bool:
    """True if the file carries a picture stream (FLACs often do)."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v",
         "-show_entries", "stream=codec_type", "-of", "csv=p=0",
         str(src)],
        capture_output=True, text=True,
    )
    return "video" in (r.stdout or "")


def main() -> int:
    ap = argparse.ArgumentParser(description="Convert audio files to MP3.")
    ap.add_argument("folder", help="folder containing the audio files")
    ap.add_argument(
        "--keep", action="store_true",
        help="keep the original files instead of deleting them",
    )
    args = ap.parse_args()

    if shutil.which("ffmpeg") is None:
        sys.exit("Error: ffmpeg not found on PATH.")

    root = Path(args.folder)
    if not root.is_dir():
        sys.exit(f"not a folder: {root.resolve()}")

    # recursive scan: this folder and every subfolder, any depth
    all_files = [f for f in root.rglob("*") if f.is_file()]
    media = sorted(f for f in all_files if f.suffix.lower() in FORMATS)

    print(f"folder: {root.resolve()}")
    print(f"found {len(media)} convertible file(s), {len(all_files)} file(s) total")
    if not media:
        print("nothing to convert. Subfolders/files found:")
        for f in sorted(root.rglob("*"))[:30]:
            print(f"  {f.relative_to(root)}")
        return 0

    # map files are per-directory (one per playlist folder)
    maps: dict[Path, dict] = {}
    dirty_maps: set[Path] = set()

    def load_map(d: Path) -> tuple[dict, dict]:
        if d not in maps:
            mapping = {}
            p = d / MAP_FILE
            if p.exists():
                try:
                    mapping = json.loads(p.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    pass
            maps[d] = mapping
        reverse = {fname: vid for vid, fname in maps[d].items()}
        return maps[d], reverse

    ok = failed = skipped = 0
    for src in media:
        dst = src.with_suffix(".mp3")
        if dst.exists():
            print(f"skip (mp3 already exists): {src.relative_to(root)}")
            skipped += 1
        else:
            cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src)]
            cmd += ["-map", "0:a", "-c:a", "libmp3lame", "-q:a", QSCALE]
            cover = find_sidecover(src)
            if cover is not None:
                cmd += ["-i", str(cover), "-map", "1:v", "-c:v", "mjpeg"]
            elif has_embedded_cover(src):
                cmd += ["-map", "0:v", "-c:v", "mjpeg"]
            cmd += ["-id3v2_version", "3", str(dst)]
            if subprocess.run(cmd).returncode != 0:
                print(f"FAILED: {src.relative_to(root)}", file=sys.stderr)
                failed += 1
                continue
            print(f"mp3: {src.relative_to(root)}")
            ok += 1
        if not args.keep:
            src.unlink()

        mapping, reverse = load_map(src.parent)
        vid = reverse.get(src.name)
        if vid:
            mapping[vid] = dst.name
            dirty_maps.add(src.parent)

    for d in dirty_maps:
        (d / MAP_FILE).write_text(
            json.dumps(maps[d], indent=2, sort_keys=True), encoding="utf-8"
        )

    print(f"done: {ok} converted, {skipped} skipped, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())