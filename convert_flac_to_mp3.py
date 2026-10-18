#!/usr/bin/env python3
"""convert_flac_to_mp3.py - convert a folder of FLACs into a parallel
output folder (`<name>-converted`). Originals are never modified.

Ways to run:
    drag a folder onto this script
    python convert_flac_to_mp3.py path\to\folder
    python convert_flac_to_mp3.py          (opens a folder picker)
"""

from __future__ import annotations

import argparse
import ctypes
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

FORMATS: dict[str, dict] = {
    "mp3": {
        "ext": ".mp3",
        "codec": "libmp3lame",
        "flag": "-b:a",
        "default_quality": "190k",
        "extra": ["-id3v2_version", "3", "-write_id3v1", "1"],
        "pictures": True,
    },
    "ogg": {
        "ext": ".ogg",
        "codec": "libvorbis",
        "flag": "-q:a",
        "default_quality": "5",
        "extra": [],
        "pictures": False,
    },
    "opus": {
        "ext": ".opus",
        "codec": "libopus",
        "flag": "-b:a",
        "default_quality": "112k",
        "extra": [],
        "pictures": False,
    },
    "m4a": {
        "ext": ".m4a",
        "codec": "aac",
        "flag": "-b:a",
        "default_quality": "160k",
        "extra": ["-movflags", "+faststart"],
        "pictures": True,
    },
}


@dataclass
class Result:
    src: Path
    dst: Path | None
    status: str  # converted | skipped | failed | dry-run
    in_bytes: int = 0
    out_bytes: int = 0
    seconds: float = 0.0
    error: str = ""


# ---------------------------------------------------------------- conversion

def quality_args(fmt: str, quality: str | None) -> list[str]:
    spec = FORMATS[fmt]
    q = (quality or spec["default_quality"]).strip().lower()
    if spec["flag"] == "-q:a":
        q = q.rstrip("k")          # vorbis: 0-10 scale
        return ["-q:a", q]
    if not q.endswith("k"):
        q += "k"
    return ["-b:a", q]


MUXERS = {"mp3": "mp3", "ogg": "ogg", "opus": "opus", "m4a": "mp4"}


def build_ffmpeg_cmd(src: Path, dst: Path, fmt: str,
                     quality: str | None) -> list[str]:
    spec = FORMATS[fmt]
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-i", str(src), "-map", "0:a:0", "-map_metadata", "0"]
    if spec["pictures"]:
        cmd += ["-map", "0:v?", "-c:v", "mjpeg"]   # keep cover art
    else:
        cmd += ["-vn"]
    cmd += ["-c:a", spec["codec"]] + quality_args(fmt, quality)
    cmd += spec["extra"]
    # .part suffix hides the real extension, so name the muxer explicitly
    cmd += ["-f", MUXERS[fmt], str(dst)]
    return cmd


def convert_with_ffmpeg(src: Path, dst: Path, fmt: str,
                        quality: str | None) -> None:
    cmd = build_ffmpeg_cmd(src, dst, fmt, quality)
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                          stderr=subprocess.PIPE, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[-400:] or "ffmpeg failed")


def parse_bitrate(quality: str | None) -> int:
    text = (quality or FORMATS["mp3"]["default_quality"]).lower()
    return int(text.rstrip("k"))


def convert_with_python(src: Path, dst: Path, fmt: str,
                        quality: str | None) -> None:
    if fmt not in ("mp3", "ogg"):
        raise RuntimeError("the pure-Python backend handles only mp3 and ogg")

    import numpy as np  # noqa: F401  (required by soundfile)
    import soundfile as sf

    data, rate = sf.read(str(src), dtype="int16", always_2d=True)

    if fmt == "ogg":
        sf.write(str(dst), data, rate)   # Ogg Vorbis from extension
        return

    import lameenc
    enc = lameenc.Encoder()
    enc.set_bit_rate(parse_bitrate(quality))
    enc.set_in_sample_rate(rate)
    enc.set_channels(data.shape[1])
    enc.set_quality(2)
    raw = data.astype("<i2", copy=False).tobytes()
    dst.write_bytes(bytes(enc.encode(raw) + enc.flush()))


# ------------------------------------------------------------------- helpers

def clean_path(raw: str) -> Path:
    """Accept paths the way Windows hands them over (quotes and all)."""
    text = raw.strip().strip('"').strip("'")
    text = os.path.expandvars(text)
    return Path(text).expanduser().resolve()


def collect_files(src_dir: Path, recursive: bool) -> list[Path]:
    it = src_dir.rglob("*") if recursive else src_dir.glob("*")
    return sorted(p for p in it
                  if p.is_file() and p.suffix.lower() == ".flac")


def dest_path(src: Path, src_dir: Path, out_dir: Path, fmt: str) -> Path:
    rel = src.relative_to(src_dir)
    return (out_dir / rel).with_suffix(FORMATS[fmt]["ext"])


def human(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num) < 1024:
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


def convert_one(src: Path, src_dir: Path, out_dir: Path,
                args, ffmpeg: str | None) -> Result:
    dst = dest_path(src, src_dir, out_dir, args.fmt)
    in_size = src.stat().st_size

    if dst.exists() and not args.overwrite:
        return Result(src, dst, "skipped", in_size, dst.stat().st_size)

    if args.dry_run:
        return Result(src, dst, "dry-run", in_size,
                      error=ffmpeg or "python")

    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".part")
    start = time.perf_counter()
    try:
        if ffmpeg:
            convert_with_ffmpeg(src, tmp, args.fmt, args.quality)
        else:
            convert_with_python(src, tmp, args.fmt, args.quality)
        tmp.replace(dst)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        return Result(src, dst, "failed", in_size, error=str(exc))

    elapsed = time.perf_counter() - start
    return Result(src, dst, "converted", in_size, dst.stat().st_size, elapsed)


# ------------------------------------------------------- input / UI helpers

def pick_folder_gui() -> str:
    """Small folder-picker dialog; returns '' if unavailable or cancelled."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        chosen = filedialog.askdirectory(title="Folder to convert")
        root.destroy()
        return chosen or ""
    except Exception:
        return ""


def standalone_console() -> bool:
    """True when this console was created for us (double-click / drag-drop)."""
    if os.name != "nt":
        return False
    try:
        buf = (ctypes.c_uint * 2)()
        count = ctypes.windll.kernel32.GetConsoleProcessList(buf, 2)
        return count <= 1
    except Exception:
        return False


# --------------------------------------------------------------------- main

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Convert FLAC folders into matching '<name>-converted' "
                    "folders. Originals are never modified or deleted.")
    p.add_argument("paths", nargs="*",
                   help="folder(s) to convert (drag & drop works too)")
    p.add_argument("-o", "--out-dir", type=Path, default=None,
                   help="output folder (only with a single input folder; "
                        "default: <input>-converted next to the input)")
    p.add_argument("-f", "--format", dest="fmt", choices=sorted(FORMATS),
                   default="mp3",
                   help="output format (default: mp3)")
    p.add_argument("-q", "--quality", default=None,
                   help="bitrate like 190k, or Vorbis quality 0-10 "
                        "(defaults per format)")
    p.add_argument("--no-recursive", dest="recursive", action="store_false",
                   help="only convert FLAC files in the top level "
                        "(default: include subfolders)")
    p.add_argument("-j", "--jobs", type=int, default=4,
                   help="parallel conversions (default: 4)")
    p.add_argument("--overwrite", action="store_true",
                   help="replace existing files in the output folder")
    p.add_argument("--backend", choices=["auto", "ffmpeg", "python"],
                   default="auto", help="conversion backend (default: auto)")
    p.add_argument("--dry-run", action="store_true",
                   help="list what would be done, convert nothing")
    p.add_argument("--no-pause", action="store_true",
                   help="never wait for Enter when finished")
    return p.parse_args()


def collect_input_dirs(args) -> list[Path]:
    raw_paths = [clean_path(p) for p in args.paths]

    if not raw_paths:
        chosen = pick_folder_gui()
        if chosen:
            raw_paths = [clean_path(chosen)]
        else:
            typed = input("Folder to convert (Enter to quit): ").strip()
            if typed:
                raw_paths = [clean_path(typed)]

    dirs: list[Path] = []
    for p in raw_paths:
        if p.is_dir():
            dirs.append(p)
        else:
            print(f"warning: '{p}' is not a folder - skipped",
                  file=sys.stderr)
    return dirs


def convert_folder(src_dir: Path, out_dir: Path, args,
                   ffmpeg: str | None) -> list[Result]:
    print(f"\n=== {src_dir} ===")
    print(f"output: {out_dir}")

    files = collect_files(src_dir, args.recursive)
    if not files:
        print("No FLAC files found, moving on.")
        return []

    print(f"{len(files)} FLAC file(s), format: {args.fmt} "
          f"({args.quality or FORMATS[args.fmt]['default_quality']}), "
          f"backend: {'ffmpeg' if ffmpeg else 'python'}")

    results: list[Result] = []
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {pool.submit(convert_one, src, src_dir, out_dir,
                               args, ffmpeg): src
                   for src in files}
        done = 0
        for fut in as_completed(futures):
            r = fut.result()
            done += 1
            results.append(r)
            tag = {"converted": " ok ", "skipped": "skip",
                   "failed": "FAIL", "dry-run": "dry "}[r.status]
            msg = f"[{done}/{len(files)}] {tag} {r.src.name}"
            if r.status == "converted":
                saved = 100 * (1 - r.out_bytes / max(r.in_bytes, 1))
                msg += (f"  {human(r.in_bytes)} -> {human(r.out_bytes)} "
                        f"({saved:.0f}% smaller, {r.seconds:.1f}s)")
            elif r.status == "failed":
                msg += f"  {r.error}"
            print(msg)
    return results


def main() -> int:
    args = parse_args()

    ffmpeg = shutil.which("ffmpeg")
    if args.backend == "ffmpeg" and not ffmpeg:
        print("error: ffmpeg requested but not found on PATH", file=sys.stderr)
        return 2
    if args.backend == "python":
        ffmpeg = None

    dirs = collect_input_dirs(args)
    if not dirs:
        print("Nothing to do (no valid folders).")
        return 1

    if args.out_dir and len(dirs) > 1:
        print("error: --out-dir works with a single input folder only",
              file=sys.stderr)
        return 2

    all_results: list[Result] = []
    for src_dir in dirs:
        if args.out_dir:
            out_dir = args.out_dir.expanduser().resolve()
        else:
            out_dir = src_dir.parent / (src_dir.name + "-converted")

        if out_dir == src_dir:
            print(f"error: output folder would equal input folder "
                  f"({src_dir}) - skipping", file=sys.stderr)
            continue
        if src_dir in out_dir.parents:
            print(f"error: output folder would be inside the source "
                  f"({out_dir}) - skipping", file=sys.stderr)
            continue

        all_results += convert_folder(src_dir, out_dir, args, ffmpeg)

    converted = [r for r in all_results if r.status == "converted"]
    failed = [r for r in all_results if r.status == "failed"]
    skipped = [r for r in all_results if r.status == "skipped"]

    print("\n--- summary ---")
    print(f"converted: {len(converted)}   skipped: {len(skipped)}"f"failed: {len(failed)}")
    if converted:
        total_in = sum(r.in_bytes for r in converted)
        total_out = sum(r.out_bytes for r in converted)
        saved = 100 * (1 - total_out / max(total_in, 1))
        print(f"size: {human(total_in)} -> {human(total_out)} "
              f"({saved:.0f}% smaller)")
        if saved < 0:
            print("note: outputs are larger than the sources - "
                  "try a lower --quality value")
    if failed:
        print("\nfailures:")
        for r in failed:
            print(f"  {r.src}: {r.error}")
            
    input("Press Enter to continue...")


    return 1 if failed else 0


if __name__ == "__main__":
    try:  # Windows consoles often use a legacy code page
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    try:
        code = main()
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        code = 130

    # keep the window open when launched by drag & drop / double-click
    if "--no-pause" not in sys.argv and standalone_console():
        try:
            input("\nPress Enter to close...")
        except EOFError:
            pass
    raise SystemExit(code)