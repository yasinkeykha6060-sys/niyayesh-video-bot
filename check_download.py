#!/usr/bin/env python3
"""یک ویدیوی عمومی کوتاه را فقط برای امتحان مسیر دانلود می‌پرسد. فایلی نمی‌فرستد."""

from __future__ import annotations

import json
import os
import subprocess
import sys

URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"


def ask(proxy: str) -> tuple[int, str, str]:
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "-J",
        "--no-warnings",
        "--no-playlist",
        "--extractor-args",
        "youtube:player_client=android_vr",
        "--socket-timeout",
        "20",
        "--retries",
        "1",
    ]
    if proxy:
        command.extend(["--proxy", proxy])
    command.append(URL)
    completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=90)
    title = ""
    if completed.returncode == 0 and (completed.stdout or "").strip():
        try:
            title = str(json.loads(completed.stdout).get("title") or "")
        except json.JSONDecodeError:
            title = ""
    return completed.returncode, title, (completed.stderr or "")[-800:]


def main() -> int:
    code, title, error = ask("")
    print("direct", code, title)
    if code != 0:
        print(error)
    proxy = os.environ.get("YT_PROXY", "").strip()
    if code != 0 and proxy:
        code, title, error = ask(proxy)
        print("proxy", code, title)
        if code != 0:
            print(error)
    if code != 0 or not title:
        return 1
    print("ok", title)
    return 0


if __name__ == "__main__":
    sys.exit(main())
