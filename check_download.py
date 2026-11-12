#!/usr/bin/env python3
"""Download the sample at every quality, plus audio. Nothing is sent."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import bot
import download

URL = "https://www.youtube.com/watch?v=X0AqMgJmbNU"
OUT = Path("/tmp/yt-sample")
CLIENT = "web_embedded,android"
HEIGHT_RE = re.compile(r"(\d{2,4})x(\d{2,4})")


def activate_proxy(proxy: str) -> None:
    if not proxy:
        return
    os.environ["ALL_PROXY"] = proxy
    os.environ["all_proxy"] = proxy


def looks_like_media(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 20_000:
        return False
    head = path.read_bytes()[:16]
    if path.suffix.lower() == ".mp3" and (head[:3] == b"ID3" or head[:2] == b"\xff\xfb"):
        return True
    if b"ftyp" in head:
        return True
    return head[:4] == b"\x1a\x45\xdf\xa3"


def run_yt(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    command = [*bot.ytdlp_base(URL, CLIENT), *args, URL]
    return subprocess.run(command, check=False, text=True, timeout=timeout, capture_output=True)


def available_heights(proxy: str) -> list[int]:
    command = [*bot.ytdlp_base(URL, CLIENT), "-F", URL]
    if proxy:
        command.insert(-1, "--proxy")
        command.insert(-1, proxy)
    completed = subprocess.run(command, check=False, text=True, timeout=120, capture_output=True)
    text = (completed.stdout or "") + "\n" + (completed.stderr or "")
    heights = sorted({int(match.group(2)) for match in HEIGHT_RE.finditer(text) if int(match.group(2)) >= 144})
    print("heights", heights)
    interesting = [line for line in text.splitlines() if "x" in line and "mp4" in line.lower()]
    for line in interesting[:30]:
        print(line[:180])
    return heights


def download_mode(label: str, proxy: str, audio: bool) -> Path | None:
    folder = OUT / label
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True, exist_ok=True)
    if audio:
        extra = ["-x", "--audio-format", "mp3", "--audio-quality", "192K", "-f", download.audio_format(0)]
    else:
        extra = ["-f", download.video_format(label), "--merge-output-format", "mp4"]
    command = [
        *bot.ytdlp_base(URL, CLIENT),
        "--newline",
        *extra,
        "--max-filesize",
        "400M",
        "-o",
        str(folder / "%(id)s.%(ext)s"),
    ]
    if proxy:
        command.extend(["--proxy", proxy])
    command.append(URL)
    completed = subprocess.run(command, check=False, text=True, timeout=420, capture_output=True)
    files = [path for path in folder.rglob("*") if looks_like_media(path)]
    if not files:
        detail = ((completed.stderr or "") + (completed.stdout or ""))[-800:]
        print(detail)
        return None
    return max(files, key=lambda path: path.stat().st_size)


def probe(path: Path) -> tuple[int, float]:
    height = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=height",
            "-of",
            "csv=p=0",
            str(path),
        ],
        check=False,
        text=True,
        timeout=60,
        capture_output=True,
    )
    duration = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(path),
        ],
        check=False,
        text=True,
        timeout=60,
        capture_output=True,
    )
    try:
        seen = int((height.stdout or "0").strip().splitlines()[0])
    except (ValueError, IndexError):
        seen = 0
    try:
        seconds = float((duration.stdout or "0").strip())
    except ValueError:
        seconds = 0.0
    return seen, seconds


def height_ok(label: str, seen: int, heights: list[int]) -> bool:
    if label == "audio":
        return seen == 0
    if seen < 144:
        return False
    if not heights:
        return True
    want = 1080 if label == "best" else int(label)
    choices = [item for item in heights if item <= want + 30]
    target = max(choices) if choices else want
    return seen + 40 >= min(want, target)


def _make_media(command: list[str], dest: Path) -> bool:
    made = subprocess.run(command, check=False, text=True, timeout=180, capture_output=True)
    if made.returncode != 0 or not dest.exists() or dest.stat().st_size <= 0:
        print("FAIL pack build", dest.name)
        print((made.stderr or "")[-500:])
        return False
    return True


def prove_pack() -> bool:
    folder = Path("/tmp/yt-pack")
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True, exist_ok=True)
    long_video = folder / "long.mp4"
    short_video = folder / "short.mp4"
    audio = folder / "clip.mp3"
    old_limit = download.TELEGRAM_LIMIT
    old_part = download.PART_BYTES
    try:
        download.TELEGRAM_LIMIT = 1_500_000
        download.PART_BYTES = 1_100_000
        if not _make_media(
            [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", "color=c=black:s=640x360:r=30,noise=alls=80:allf=t",
                "-f", "lavfi", "-i", "sine=frequency=440",
                "-t", "30",
                "-c:v", "libx264", "-b:v", "2500k", "-minrate", "1800k", "-maxrate", "2500k", "-bufsize", "2500k",
                "-c:a", "aac", "-shortest",
                str(long_video),
            ],
            long_video,
        ):
            return False
        if long_video.stat().st_size <= download.TELEGRAM_LIMIT:
            print("FAIL pack split source was already small", long_video.stat().st_size)
            return False
        parts = download.pieces_for(long_video, "video")
        sizes = [part.stat().st_size for part in parts]
        print("pack split", sizes)
        if len(parts) < 2 or any(size > download.TELEGRAM_LIMIT or size <= 0 for size in sizes):
            print("FAIL pack split")
            return False
        fitted = download.compress_until_fit(long_video, "video")
        if fitted is None:
            print("PASS compress kept the picture")
        else:
            fit_size = fitted.stat().st_size
            print("pack compress", fit_size)
            if fit_size > download.TELEGRAM_LIMIT or fit_size <= 0:
                print("FAIL pack compress")
                return False
        if not _make_media(
            [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", "color=c=black:s=640x360:r=30,noise=alls=80:allf=t",
                "-f", "lavfi", "-i", "sine=frequency=440",
                "-t", "4",
                "-c:v", "libx264", "-b:v", "5000k", "-minrate", "4000k", "-maxrate", "5000k", "-bufsize", "5000k",
                "-c:a", "aac", "-shortest",
                str(short_video),
            ],
            short_video,
        ):
            return False
        if short_video.stat().st_size <= download.TELEGRAM_LIMIT:
            print("FAIL pack fit source was already small", short_video.stat().st_size)
            return False
        fitted = download.pieces_for(short_video, "video")
        print("pack fit", [part.stat().st_size for part in fitted])
        if len(fitted) != 1 or fitted[0].stat().st_size > download.TELEGRAM_LIMIT:
            print("FAIL pack fit")
            return False
        download.TELEGRAM_LIMIT = 400_000
        download.PART_BYTES = 320_000
        if not _make_media(
            [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", "sine=frequency=440",
                "-t", "20",
                "-c:a", "libmp3lame", "-b:a", "192k",
                str(audio),
            ],
            audio,
        ):
            return False
        packed = download.pieces_for(audio, "audio")
        print("pack audio", [part.stat().st_size for part in packed])
        if len(packed) != 1 or packed[0].stat().st_size > download.TELEGRAM_LIMIT:
            print("FAIL pack audio")
            return False
        print("PASS pack")
        return True
    finally:
        download.TELEGRAM_LIMIT = old_limit
        download.PART_BYTES = old_part
        shutil.rmtree(folder, ignore_errors=True)


def prove_short(proxy: str) -> bool:
    url = "https://www.youtube.com/shorts/X0AqMgJmbNU"
    if bot.video_id_from_url(url) != "X0AqMgJmbNU":
        print("FAIL short id")
        return False
    command = [
        *bot.ytdlp_base(url, CLIENT),
        "--skip-download",
        "-O",
        "id:%(id)s",
        "--proxy",
        proxy,
        url,
    ]
    completed = subprocess.run(command, check=False, text=True, timeout=90, capture_output=True)
    text = (completed.stdout or "") + (completed.stderr or "")
    print(text.strip()[-240:])
    if "id:X0AqMgJmbNU" not in text:
        print("FAIL short")
        return False
    print("PASS short")
    return True


def main() -> int:
    if not prove_pack():
        return 1
    proxy = os.environ.get("YT_PROXY", "").strip()
    print("proxy", "yes" if proxy else "no")
    if not proxy:
        print("clean network is required")
        return 1
    activate_proxy(proxy)
    if not prove_short(proxy):
        return 1
    if OUT.exists():
        shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True, exist_ok=True)
    heights = available_heights(proxy)
    failed = False
    for label in ("360", "720", "1080", "best", "audio"):
        print("download", label, flush=True)
        try:
            saved = download_mode(label, proxy, label == "audio")
        except subprocess.TimeoutExpired:
            print("FAIL", label, "timeout")
            failed = True
            continue
        if saved is None:
            print("FAIL", label, "no file")
            failed = True
            continue
        seen, seconds = probe(saved)
        ok = height_ok(label, seen, heights) and seconds > 1
        print(
            f"{'PASS' if ok else 'FAIL'} {label} bytes={saved.stat().st_size} "
            f"height={seen} seconds={seconds:.1f}"
        )
        if not ok:
            failed = True
    shutil.rmtree(OUT, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
