#!/usr/bin/env python3
"""Prove one real media file can be saved for the sample video. Nothing is sent."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import bot

URL = "https://www.youtube.com/watch?v=X0AqMgJmbNU"
OUT = Path("/tmp/yt-sample")
CLIENTS = ("web_embedded,android", "", "android", "tv", "ios")


def looks_like_media(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 20_000:
        return False
    head = path.read_bytes()[:16]
    if b"ftyp" in head:
        return True
    return head[:4] == b"\x1a\x45\xdf\xa3"


def activate_proxy(proxy: str) -> None:
    if not proxy:
        return
    os.environ["ALL_PROXY"] = proxy
    os.environ["all_proxy"] = proxy
    os.environ["HTTPS_PROXY"] = proxy
    os.environ["https_proxy"] = proxy


def grab(clients: str, proxy: str) -> Path | None:
    if OUT.exists():
        shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True, exist_ok=True)
    command = [
        *bot.ytdlp_base(URL, clients),
        "--newline",
        "-f",
        "bv*[height<=360]+ba/b[height<=360]/b",
        "--merge-output-format",
        "mp4",
        "--max-filesize",
        "80M",
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
        timeout=240,
        capture_output=True,
    )
    files = [path for path in OUT.rglob("*") if path.is_file() and not path.name.endswith(".part")]
    good = [path for path in files if looks_like_media(path)]
    detail = ((completed.stderr or "") + (completed.stdout or ""))[-900:]
    if good:
        if completed.returncode != 0:
            print(detail)
        return max(good, key=lambda path: path.stat().st_size)
    print(detail)
    return None


def range_ok(clients: str, proxy: str) -> bool:
    command = [
        *bot.ytdlp_base(URL, clients),
        "-g",
        "-f",
        "bv*[height<=360]+ba/b[height<=360]/b",
    ]
    if proxy:
        command.extend(["--proxy", proxy])
    command.append(URL)
    completed = subprocess.run(command, check=False, text=True, timeout=90, capture_output=True)
    urls = [line.strip() for line in (completed.stdout or "").splitlines() if line.startswith("http")]
    if not urls:
        print((completed.stderr or completed.stdout or "")[-500:])
        return False
    if OUT.exists():
        shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True, exist_ok=True)
    dest = OUT / "range.bin"
    fetched = subprocess.run(
        [
            "curl",
            "-L",
            "--fail",
            "--silent",
            "--show-error",
            "--max-time",
            "45",
            "--proxy",
            proxy.replace("socks5://", "socks5h://"),
            "-r",
            "0-1048575",
            "-A",
            "Mozilla/5.0",
            "-o",
            str(dest),
            urls[0],
        ],
        check=False,
        text=True,
        timeout=60,
        capture_output=True,
    )
    if not dest.exists():
        print((fetched.stderr or "")[-400:])
        return False
    print(f"range bytes={dest.stat().st_size} head={dest.read_bytes()[:16].hex()} curl={fetched.returncode}")
    return looks_like_media(dest)


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
    proxy = os.environ.get("YT_PROXY", "").strip()
    print("proxy", "yes" if proxy else "no")
    activate_proxy(proxy)
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
            try:
                if range_ok(clients, proxy):
                    print(f"WINNER {label} range")
                    shutil.rmtree(OUT, ignore_errors=True)
                    return 0
            except subprocess.TimeoutExpired:
                print("range timeout", label)
            continue
        head = saved.read_bytes()[:16].hex()
        print(f"WINNER {label} bytes={saved.stat().st_size} head={head}")
        shutil.rmtree(OUT, ignore_errors=True)
        return 0
    print("NO_FILE")
    return 1


if __name__ == "__main__":
    sys.exit(main())
