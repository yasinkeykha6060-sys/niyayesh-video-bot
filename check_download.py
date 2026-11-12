#!/usr/bin/env python3
"""Prove one real media file can be saved for the sample video. Nothing is sent."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import bot

URL = "https://www.youtube.com/watch?v=X0AqMgJmbNU"
OUT = Path("/tmp/yt-sample")
CLIENTS = ("web_embedded,android", "", "android", "tv")


def looks_like_media(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 20_000:
        return False
    head = path.read_bytes()[:16]
    if b"ftyp" in head:
        return True
    return head[:4] == b"\x1a\x45\xdf\xa3"


def grab(clients: str, proxy: str) -> Path | None:
    if OUT.exists():
        shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True, exist_ok=True)
    command = [
        *bot.ytdlp_base(URL, clients),
        "--newline",
        "-f",
        "18/bv*[height<=360]+ba/b[height<=480]/b",
        "--merge-output-format",
        "mp4",
        "--download-sections",
        "*0:00-0:08",
        "--max-filesize",
        "40M",
        "-o",
        str(OUT / "%(id)s.%(ext)s"),
    ]
    if proxy:
        command.extend(["--proxy", proxy])
    command.append(URL)
    completed = subprocess.run(
        command,
        check=False,
        text=True,
        timeout=180,
        capture_output=True,
    )
    files = [path for path in OUT.rglob("*") if path.is_file()]
    good = [path for path in files if looks_like_media(path)]
    if completed.returncode == 0 and good:
        return max(good, key=lambda path: path.stat().st_size)
    detail = ((completed.stderr or "") + (completed.stdout or ""))[-700:]
    print(detail)
    if good:
        return max(good, key=lambda path: path.stat().st_size)
    return None


def main() -> int:
    version = subprocess.run(
        [sys.executable, "-m", "yt_dlp", "--version"],
        check=False,
        text=True,
        capture_output=True,
        timeout=30,
    )
    print("yt-dlp", (version.stdout or "").strip())
    print("deno", shutil.which("deno") or "missing")
    proxy = __import__("os").environ.get("YT_PROXY", "").strip()
    print("proxy", "yes" if proxy else "no")
    if not proxy:
        print("clean network is required for this test")
        return 1
    for clients in CLIENTS:
        label = clients or "default"
        print("try", label, flush=True)
        try:
            saved = grab(clients, proxy)
        except subprocess.TimeoutExpired:
            print("timeout", label)
            saved = None
        if saved is None:
            continue
        head = saved.read_bytes()[:16].hex()
        print(f"WINNER {label} bytes={saved.stat().st_size} head={head}")
        shutil.rmtree(OUT, ignore_errors=True)
        return 0
    print("NO_FILE")
    return 1


if __name__ == "__main__":
    sys.exit(main())
