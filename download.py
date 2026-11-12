#!/usr/bin/env python3
"""دانلود یک ویدیو یا صدای یوتیوب و فرستادن آن برای نیایش.

این فایل را workflow دانلود اجرا می‌کند و بعد تمام می‌شود.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import requests

import bot

ROOT = bot.ROOT
TELEGRAM_LIMIT = 49 * 1024 * 1024
PART_BYTES = 45 * 1024 * 1024
RELEASE_LIMIT = 1800 * 1024 * 1024
QUALITY_ORDER = ["best", "1080", "720", "360"]
DISK_SAFE = 8 * 1024 * 1024 * 1024
GUESSED_BITRATE = {"best": 8_000_000, "1080": 5_000_000, "720": 2_500_000, "360": 800_000}


def guessed_bytes(duration: int, quality: str) -> int:
    if duration <= 0:
        return 0
    return int(duration * GUESSED_BITRATE.get(quality, 2_500_000) / 8)


def video_format(quality: str) -> str:
    # A single https file first. Split streams are only the backup, so the bar
    # is the whole video and YouTube's tiny pieces are not used.
    if quality == "best":
        return video_format("1080")
    height = quality if quality in {"360", "720", "1080"} else "720"
    floor = {"720": "480", "1080": "720"}.get(height)
    tall = f"[height>={floor}]" if floor else ""
    return (
        f"bv*[height<={height}]{tall}[ext=mp4][vcodec^=avc1][protocol=https]+ba[ext=m4a][protocol=https]/"
        f"b[height<={height}]{tall}[ext=mp4][vcodec^=avc1][protocol=https]/"
        f"bv*[height<={height}]{tall}[ext=mp4][protocol=https]+ba[ext=m4a][protocol=https]/"
        f"b[height<={height}][ext=mp4][protocol=https]/"
        f"bv*[height<={height}][ext=mp4]+ba[ext=m4a]/"
        f"b[height<={height}]/b"
    )


def audio_format(_duration: int = 0) -> str:
    return "ba[ext=m4a][protocol=https]/ba[ext=m4a]/ba[protocol=https]/ba/b"


def step_down(quality: str) -> str | None:
    if quality not in QUALITY_ORDER:
        return "720"
    index = QUALITY_ORDER.index(quality)
    if index + 1 < len(QUALITY_ORDER):
        return QUALITY_ORDER[index + 1]
    return None


def work_dir(video_id: str) -> Path:
    path = ROOT / "downloads" / video_id
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


def newest_media(folder: Path) -> Path | None:
    files = [
        path
        for path in folder.rglob("*")
        if path.is_file() and path.suffix.lower() in {".mp4", ".mkv", ".webm", ".mp3", ".m4a"}
        and path.stat().st_size > 0
    ]
    if not files:
        return None
    return max(files, key=lambda path: path.stat().st_mtime)


PROGRESS_RE = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")
FORMATS_RE = re.compile(r"Downloading\s+(\d+)\s+format")


class Bar:
    def __init__(self, token: str, chat_id: int, message_id: object, line: str, url: str):
        self.token = token
        self.chat_id = chat_id
        self.message_id = message_id
        self.line = line
        self.url = url
        self.last_at = 0.0
        self.display = 0.0
        self.formats = 1
        self.part = 0
        self._real = False
        self._closed = False
        self._floor = 8.0
        threading.Thread(target=self._creep, daemon=True).start()

    def close(self) -> None:
        self._real = True
        self._closed = True

    def notice(self, line: str) -> None:
        if self._closed:
            return
        found = FORMATS_RE.search(line)
        if found:
            self.formats = max(self.formats, int(found.group(1)))
        if "Destination:" in line and "Frag" not in line:
            self.part += 1

    def _show(self, percent: float) -> None:
        if self._closed:
            return
        bot.edit_message(
            self.token,
            self.chat_id,
            self.message_id,
            bot.progress_text(percent, self.line, self.url),
            preview=bool(self.url),
        )

    def _creep(self) -> None:
        percent = 8.0
        while not self._real and percent < 36:
            time.sleep(2.2)
            if self._real:
                return
            percent = min(36.0, percent + 4)
            self._floor = percent
            try:
                self._show(percent)
            except Exception:
                print("progress update failed")

    def __call__(self, percent: float) -> None:
        self._real = True
        part = max(self.part, 1)
        whole = ((part - 1) + percent / 100.0) / max(self.formats, part) * 100.0
        shown = min(99.0, max(self.display, whole))
        now = time.time()
        if shown < 99 and now - self.last_at < 3 and shown - self.display < 2:
            return
        self.last_at = now
        self.display = shown
        self._show(shown)


_reporter: Bar | None = None


def run_ytdlp(command: list[str]) -> subprocess.CompletedProcess[str]:
    print("yt-dlp start", flush=True)
    proc = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    kept: list[str] = []

    def read() -> None:
        if proc.stdout is None:
            return
        for line in proc.stdout:
            if _reporter is not None:
                _reporter.notice(line)
            match = PROGRESS_RE.search(line)
            if match and _reporter is not None:
                try:
                    _reporter(float(match.group(1)))
                except Exception:
                    print("progress update failed")
                continue
            kept.append(line)
            if line.strip():
                print(bot.scrub(line.rstrip())[:300], flush=True)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        code = proc.wait(timeout=5 * 60 * 60)
    except subprocess.TimeoutExpired:
        proc.kill()
        code = 1
    reader.join(5)
    bot.clear_cookies()
    detail = "".join(kept)[-4000:]
    return subprocess.CompletedProcess(command, code, detail, detail)


def base_command(url: str, folder: Path, proxy: str = "", clients: str = "") -> list[str]:
    command = [
        *bot.ytdlp_base(url, clients),
        "--newline",
        "--progress",
        "--fragment-retries",
        "5",
        "-o",
        str(folder / "%(id)s.%(ext)s"),
    ]
    if proxy:
        command.extend(["--proxy", proxy])
    return command


def _routes() -> list[str]:
    proxy = os.environ.get("YT_PROXY", "").strip()
    if proxy:
        os.environ["ALL_PROXY"] = proxy
        os.environ["all_proxy"] = proxy
        return [proxy]
    return [""]


def _clear_partials(folder: Path) -> None:
    if not folder.exists():
        return
    for path in folder.rglob("*"):
        if path.is_file():
            path.unlink(missing_ok=True)


def _client_order(kind: str, quality: str) -> tuple[str, ...]:
    return ("web_embedded,android", "", "android", "tv")


def _attempt(kind: str, url: str, folder: Path, quality: str, duration: int) -> tuple[Path | None, str]:
    last = "unknown"
    saw_age = False
    for clients in _client_order(kind, quality):
        for route in _routes():
            label = clients or "default"
            print(f"try {kind} client={label} proxy={'yes' if route else 'no'}", flush=True)
            _clear_partials(folder)
            if kind == "audio":
                path, error = _ytdlp_audio(url, folder, duration, route, clients)
            else:
                path, error = _ytdlp_video(url, folder, quality, route, clients)
            if path is not None:
                return path, ""
            if error in {"private", "unavailable"}:
                return None, error
            if error == "age":
                saw_age = True
                continue
            if error:
                last = error
    if saw_age and last == "unknown":
        return None, "age"
    return None, last


def save_stream(file_url: str, dest: Path) -> Path | None:
    try:
        response = requests.get(
            file_url,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*"},
            stream=True,
            timeout=180,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        print(bot.scrub(exc.__class__.__name__))
        return None
    total = 0
    for header in ("content-length", "estimated-content-length"):
        raw = response.headers.get(header) or ""
        if raw.isdigit() and int(raw) > 0:
            total = int(raw)
            break
    got = 0
    last = 0.0
    try:
        with dest.open("wb") as handle:
            for chunk in response.iter_content(256 * 1024):
                if not chunk:
                    continue
                handle.write(chunk)
                got += len(chunk)
                now = time.time()
                if _reporter is not None and total > 0 and now - last >= 4:
                    last = now
                    _reporter(min(99, got * 100 / total))
    except requests.RequestException as exc:
        print(bot.scrub(exc.__class__.__name__))
        dest.unlink(missing_ok=True)
        return None
    finally:
        response.close()
    if not dest.exists() or dest.stat().st_size < 1024:
        dest.unlink(missing_ok=True)
        return None
    return dest


def cobalt_download(url: str, folder: Path, video_id: str, mode: str, quality: str) -> Path | None:
    hosts = ("https://rue-cobalt.xenon.zone", "https://cobaltapi.cjs.nz")
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json", "Content-Type": "application/json"}
    chosen = {"best": "max", "1080": "1080", "720": "720", "360": "360"}.get(quality, "720")
    for host in hosts:
        if mode == "audio":
            body = {
                "url": url,
                "downloadMode": "audio",
                "audioFormat": "mp3",
                "audioBitrate": "192",
                "alwaysProxy": True,
                "filenameStyle": "basic",
            }
            ext = ".mp3"
        else:
            body = {
                "url": url,
                "downloadMode": "auto",
                "videoQuality": chosen,
                "youtubeVideoCodec": "h264",
                "youtubeVideoContainer": "mp4",
                "alwaysProxy": True,
                "filenameStyle": "basic",
            }
            ext = ".mp4"
        try:
            response = requests.post(host + "/", headers=headers, json=body, timeout=70)
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            print("backup failed", exc.__class__.__name__)
            continue
        if payload.get("status") not in {"tunnel", "redirect"} or not payload.get("url"):
            code = ""
            if isinstance(payload.get("error"), dict):
                code = str(payload["error"].get("code") or "")
            print("backup status", payload.get("status"), code)
            continue
        print("backup accepted", host)
        saved = save_stream(str(payload["url"]), folder / f"{video_id}{ext}")
        if saved is not None:
            return saved
    return None


def download_video(url: str, folder: Path, quality: str) -> tuple[Path | None, str]:
    path, error = _attempt("video", url, folder, quality, 0)
    if path is not None:
        return path, ""
    if error in {"private", "unavailable"}:
        return None, error
    saved = cobalt_download(url, folder, bot.stable_id(url), "video", quality)
    if saved is not None:
        return saved, ""
    return None, error or "unknown"


def _ytdlp_video(url: str, folder: Path, quality: str, proxy: str, clients: str = "") -> tuple[Path | None, str]:
    command = [
        *base_command(url, folder, proxy, clients),
        "-f",
        video_format(quality),
        "--merge-output-format",
        "mp4",
        url,
    ]
    completed = run_ytdlp(command)
    if completed.returncode != 0:
        print(bot.scrub((completed.stderr or "")[-1500:]))
        return None, bot.classify_ytdlp(completed.stderr or "")
    return newest_media(folder), ""


def download_audio(url: str, folder: Path, duration: int = 0) -> tuple[Path | None, str]:
    path, error = _attempt("audio", url, folder, "audio", duration)
    if path is not None:
        return path, ""
    if error in {"private", "unavailable"}:
        return None, error
    saved = cobalt_download(url, folder, bot.stable_id(url), "audio", "audio")
    if saved is not None:
        return saved, ""
    return None, error or "unknown"


def _ytdlp_audio(url: str, folder: Path, duration: int, proxy: str, clients: str = "") -> tuple[Path | None, str]:
    command = [
        *base_command(url, folder, proxy, clients),
        "-x",
        "--audio-format",
        "mp3",
        "--audio-quality",
        "192K",
        "-f",
        audio_format(duration),
        url,
    ]
    completed = run_ytdlp(command)
    if completed.returncode != 0:
        print(bot.scrub((completed.stderr or "")[-1500:]))
        return None, bot.classify_ytdlp(completed.stderr or "")
    return newest_media(folder), ""


def ensure_mp4(path: Path) -> Path:
    if path.suffix.lower() == ".mp4":
        return path
    dest = path.with_suffix(".mp4")
    completed = subprocess.run(
        ["ffmpeg", "-y", "-i", str(path), "-c", "copy", str(dest)],
        check=False,
        text=True,
        timeout=60 * 60,
        capture_output=True,
    )
    if completed.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
        path.unlink(missing_ok=True)
        return dest
    print("remux failed; keeping the original file")
    return path


def media_duration(path: Path, fallback: int) -> int:
    if fallback > 0:
        return fallback
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        check=False,
        text=True,
        timeout=60,
        capture_output=True,
    )
    try:
        return int(float((completed.stdout or "").strip()))
    except ValueError:
        return 0


def compress_audio(path: Path, max_bytes: int, duration: int, min_kbps: int = 32) -> Path | None:
    length = media_duration(path, duration)
    if length <= 0:
        return None
    kbps = int((max_bytes * 8 * 0.90) / length / 1000)
    if kbps < min_kbps:
        return None
    kbps = min(kbps, 192)
    dest = path.with_name(f"{path.stem}-{kbps}.mp3")
    completed = subprocess.run(
        ["ffmpeg", "-y", "-i", str(path), "-vn", "-c:a", "libmp3lame", "-b:a", f"{kbps}k", str(dest)],
        check=False,
        text=True,
        timeout=5 * 60 * 60,
        capture_output=True,
    )
    if completed.returncode != 0 or not dest.exists() or dest.stat().st_size > max_bytes:
        print(bot.scrub((completed.stderr or "")[-1500:]))
        return None
    return dest


def compress_video_fast(path: Path, video_bits: int) -> Path | None:
    if video_bits < 80_000:
        return None
    dest = path.with_name(f"{path.stem}-fit.mp4")
    completed = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(path),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-b:v",
            str(video_bits),
            "-maxrate",
            str(video_bits),
            "-bufsize",
            str(video_bits * 2),
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(dest),
        ],
        check=False,
        text=True,
        timeout=3 * 60 * 60,
        capture_output=True,
    )
    if completed.returncode != 0 or not dest.exists() or dest.stat().st_size > TELEGRAM_LIMIT:
        print(bot.scrub((completed.stderr or "")[-1500:]))
        return None
    return dest


def split_media(path: Path, depth: int = 0) -> list[Path]:
    if path.stat().st_size <= TELEGRAM_LIMIT:
        return [path]
    if depth >= 3:
        length = max(media_duration(path, 0), 1)
        if path.suffix.lower() == ".mp3":
            fitted = compress_audio(path, PART_BYTES, length, min_kbps=32)
            return [fitted] if fitted is not None else []
        bits = max(80_000, int((PART_BYTES * 8) / length) - 96_000)
        fitted = compress_video_fast(path, bits)
        return [fitted] if fitted is not None else []
    size = path.stat().st_size
    length = max(media_duration(path, 0), 1)
    seconds = max(12.0, (PART_BYTES / size) * length * 0.75)
    dest_dir = path.parent / f"parts-{path.stem}-{depth}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(dest_dir / f"p%03d{path.suffix}")
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(path),
        "-map",
        "0",
        "-c",
        "copy",
        "-f",
        "segment",
        "-segment_time",
        f"{seconds:.2f}",
        "-reset_timestamps",
        "1",
    ]
    if path.suffix.lower() == ".mp4":
        command.extend(["-segment_format_options", "movflags=+faststart"])
    command.append(pattern)
    completed = subprocess.run(
        command,
        check=False,
        text=True,
        timeout=60 * 60,
        capture_output=True,
    )
    parts = sorted(dest_dir.glob(f"p*{path.suffix.lower()}"))
    if completed.returncode != 0 or not parts:
        print(bot.scrub((completed.stderr or "")[-1500:]))
        return []
    packed: list[Path] = []
    for part in parts:
        packed.extend(split_media(part, depth + 1))
    return [part for part in packed if part.exists() and part.stat().st_size > 0]


def media_height(path: Path) -> int:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=height",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        check=False,
        text=True,
        timeout=60,
        capture_output=True,
    )
    try:
        return int(float((completed.stdout or "").strip()))
    except ValueError:
        return 0


def _encode_video(path: Path, height: int, video_bits: int, audio_k: int) -> Path | None:
    dest = path.with_name(f"{path.stem}-h{height}-{video_bits}.mp4")
    command = ["ffmpeg", "-y", "-i", str(path)]
    source_h = media_height(path)
    if source_h <= 0 or height < source_h:
        command.extend(["-vf", f"scale=-2:{height}"])
    command.extend(
        [
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        "-b:v",
        str(video_bits),
        "-maxrate",
        str(video_bits),
        "-bufsize",
        str(video_bits * 2),
        "-c:a",
        "aac",
        "-b:a",
        f"{audio_k}k",
        "-movflags",
        "+faststart",
        str(dest),
        ]
    )
    completed = subprocess.run(
        command,
        check=False,
        text=True,
        timeout=3 * 60 * 60,
        capture_output=True,
    )
    if completed.returncode != 0 or not dest.exists() or dest.stat().st_size <= 0:
        print(bot.scrub((completed.stderr or "")[-800:]))
        return None
    return dest


def compress_until_fit(path: Path, kind: str) -> Path | None:
    """Shrink until one Telegram message can carry the file."""
    if path.stat().st_size <= TELEGRAM_LIMIT:
        return path
    length = max(media_duration(path, 0), 1)
    if kind == "audio":
        for kbps in (160, 128, 96, 64, 48, 32, 24):
            dest = path.with_name(f"{path.stem}-fit{kbps}.mp3")
            completed = subprocess.run(
                ["ffmpeg", "-y", "-i", str(path), "-vn", "-c:a", "libmp3lame", "-b:a", f"{kbps}k", str(dest)],
                check=False,
                text=True,
                timeout=3 * 60 * 60,
                capture_output=True,
            )
            if completed.returncode == 0 and dest.exists() and dest.stat().st_size <= TELEGRAM_LIMIT:
                return dest
        return None
    audio_k = 64 if length < 3600 else 32
    budget = max(30_000, int((PART_BYTES * 8) / length) - audio_k * 1000)
    for height in (1080, 720, 480, 360, 240, 144):
        fitted = _encode_video(path, height, budget, audio_k)
        if fitted is not None and fitted.stat().st_size <= TELEGRAM_LIMIT:
            return fitted
        budget = max(30_000, int(budget * 0.62))
    return None


def pieces_for(path: Path, kind: str) -> list[Path]:
    """Pack a finished file into Telegram messages without crushing a long video."""
    if path.stat().st_size <= TELEGRAM_LIMIT:
        return [path]
    length = media_duration(path, 0)
    if kind == "audio":
        fitted = compress_audio(path, PART_BYTES, length, min_kbps=96)
        if fitted is not None:
            return [fitted]
        return split_media(path)
    if length > 0:
        video_bits = int((PART_BYTES * 8) / length) - 128_000
        if video_bits >= 700_000:
            fitted = compress_video_fast(path, video_bits)
            if fitted is not None:
                return [fitted]
    return split_media(path)


def send_media(
    token: str,
    chat_id: int,
    path: Path,
    kind: str,
    title: str,
    index: int = 1,
    total: int = 1,
) -> None:
    method = "sendAudio" if kind == "audio" else "sendVideo"
    field = "audio" if kind == "audio" else "video"
    head = "تقدیم با عشق نیایشم ❤️"
    if total > 1:
        count = f"{index} از {total}".translate(bot.FA_DIGITS)
        caption = f"{head}\n{count}\n{title}"
    else:
        caption = f"{head}\n{title}"
    data = {"chat_id": str(chat_id), "caption": caption[:1000]}
    if kind == "audio":
        data["title"] = title[:64]
    else:
        data["supports_streaming"] = "true"
    body: dict = {}
    response = None
    for _attempt in range(5):
        with path.open("rb") as handle:
            response = requests.post(
                f"https://api.telegram.org/bot{token}/{method}",
                data=data,
                files={field: handle},
                timeout=900,
            )
        if response.status_code in (401, 404):
            raise bot.FatalTokenError()
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code == 429:
            retry = 3
            params = body.get("parameters") if isinstance(body, dict) else None
            if isinstance(params, dict):
                try:
                    retry = int(params.get("retry_after") or 3)
                except (TypeError, ValueError):
                    retry = 3
            time.sleep(min(retry, 30) + 1)
            continue
        break
    if response is None or response.status_code >= 400 or not body.get("ok"):
        print(f"upload failed status={getattr(response, 'status_code', 0)}")
        raise bot.TelegramError(method, str(body.get("description") or "upload failed"), getattr(response, "status_code", 0))


def upload_release(path: Path, video_id: str, title: str) -> str:
    tag = f"yt-{video_id}-{int(time.time())}"
    asset_name = f"{video_id}{path.suffix.lower()}"
    renamed = path.with_name(asset_name)
    if renamed != path:
        path.replace(renamed)
    notes = "فایل موقت برای نیایش. تا ۲۴ ساعت دیگر پاک می‌شود. هر کسی لینک را داشته باشد می‌تواند آن را ببیند."
    completed = subprocess.run(
        [
            "gh",
            "release",
            "create",
            tag,
            str(renamed),
            "--title",
            title[:80] or video_id,
            "--notes",
            notes,
        ],
        cwd=ROOT,
        check=False,
        text=True,
        timeout=60 * 60,
        capture_output=True,
    )
    if completed.returncode != 0:
        print(bot.scrub((completed.stderr or completed.stdout or "")[-2000:]))
        raise RuntimeError("release failed")
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    return f"https://github.com/{repo}/releases/download/{tag}/{asset_name}"


def too_big_message(token: str, chat_id: int, message_id: object, video_id: str) -> None:
    text = (
        "عزیزدلم، این فایل برای تلگرام بزرگ است.\n"
        "می‌توانم کیفیت را پایین‌تر بیاورم، یا لینک دانلود بگذارم.\n"
        "اگر لینک را بخواهی، تا ۲۴ ساعت می‌ماند و هر کسی لینک را داشته باشد می‌تواند آن را ببیند."
    )
    bot.edit_message(token, chat_id, message_id, text, bot.delivery_keyboard(video_id))


def impossible_message(token: str, chat_id: int, message_id: object) -> None:
    bot.edit_message(
        token,
        chat_id,
        message_id,
        "نیایش جانم، این یکی آن‌قدر بزرگ است که حتی با کیفیت پایین هم این‌جا جا نمی‌شود. "
        "اگر یک ویدیوی کوتاه‌تر بفرستی، برات می‌گیرم ❤️",
    )


def mark(key: str, **fields: object) -> None:
    with bot.STATE_LOCK:
        state = bot.load_state()
        bot.put_job(state, key, **fields)
        bot.save_state(state)
    bot.publish_state()


def claim_send(key: str) -> bool:
    claim = uuid.uuid4().hex
    with bot.STATE_LOCK:
        state = bot.load_state()
        job = (state.get("jobs") or {}).get(key)
        if isinstance(job, dict) and job.get("status") in {"sending", "done"}:
            print("skip duplicate send")
            return False
        bot.put_job(state, key, status="sending", claim=claim)
        bot.save_state(state)
    bot.publish_state()
    with bot.STATE_LOCK:
        state = bot.load_state()
        job = (state.get("jobs") or {}).get(key)
        if not isinstance(job, dict) or job.get("claim") != claim or job.get("status") == "done":
            print("another send already owns this video")
            return False
    return True


def offer_choice(
    token: str,
    chat_id: int,
    message_id: object,
    path: Path,
    kind: str,
    title: str,
    video_id: str,
    key: str,
    quality: str,
) -> None:
    if _reporter is not None:
        _reporter.close()
    mark(key, status="ready", quality=quality, mode=kind, local_file=path.name)
    word = "صدا" if kind == "audio" else "ویدیو"
    bot.edit_message(
        token,
        chat_id,
        message_id,
        f"گرفتمش نیایش جانم ❤️\n{title}\nخود {word} را همین‌جا بفرستم، یا لینک؟",
        bot.delivery_keyboard(video_id, kind),
    )


def saved_file(video_id: str, name: str = "") -> Path | None:
    folder = ROOT / "downloads" / video_id
    if name:
        candidate = folder / Path(name).name
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    if not folder.exists():
        return None
    return newest_media(folder)


def send_fitted(
    token: str,
    chat_id: int,
    message_id: object,
    path: Path,
    kind: str,
    title: str,
    key: str,
    quality: str,
) -> bool:
    if not claim_send(key):
        return False
    bot.edit_message(token, chat_id, message_id, "دارم آماده‌اش می‌کنم که همین‌جا پخش شود ❤️")
    fitted = compress_until_fit(path, kind)
    parts = [fitted] if fitted is not None else []
    if not parts or any(part.stat().st_size > TELEGRAM_LIMIT for part in parts):
        parts = pieces_for(path, kind)
    if not parts or any(part.stat().st_size > TELEGRAM_LIMIT for part in parts):
        mark(key, status="error", quality=quality, claim="")
        impossible_message(token, chat_id, message_id)
        return True
    total = len(parts)
    for index, part in enumerate(parts, start=1):
        send_media(token, chat_id, part, kind, title, index, total)
        if index != total:
            time.sleep(1)
    bot.edit_message(token, chat_id, message_id, "تقدیم با عشق نیایشم ❤️")
    mark(key, status="done", quality=quality, claim="")
    return True


def send_link(
    token: str,
    chat_id: int,
    message_id: object,
    path: Path,
    title: str,
    video_id: str,
    key: str,
    quality: str,
    kind: str,
) -> bool:
    if path.stat().st_size > RELEASE_LIMIT:
        mark(key, status="ready", quality=quality)
        bot.edit_message(
            token,
            chat_id,
            message_id,
            "عزیزدلم، فایل اصلی برای لینک خیلی بزرگ است. خود ویدیو را بزن تا برایت بفرستمش ❤️",
            bot.delivery_keyboard(video_id, kind),
        )
        return False
    if not claim_send(key):
        return False
    bot.edit_message(token, chat_id, message_id, "فایل آماده است عزیزدلم، دارم لینکش را می‌گذارم ❤️")
    link = upload_release(path, video_id, title)
    bot.send_message(
        token,
        chat_id,
        "قربونت برم، لینک دانلود را گذاشتم.\n"
        "تا ۲۴ ساعت می‌ماند و بعد پاک می‌شود.\n"
        "اگر این نشانی را برای کسی بفرستی، او هم می‌تواند بازش کند.\n"
        f"{link}",
    )
    bot.edit_message(token, chat_id, message_id, "لینک را فرستادم نیایش جانم ❤️")
    mark(key, status="done", quality=quality, claim="")
    return True


def deliver_choice(
    chat_id: int,
    url: str,
    mode: str,
    quality: str,
    video_id: str,
    message_id: int,
    choice: str,
    local_name: str = "",
) -> None:
    token = bot.load_token()
    if not token or not video_id:
        return
    path = saved_file(video_id, local_name)
    if path is None:
        run(
            chat_id=chat_id,
            url=url,
            mode="release" if choice == "link" else "deliver-file",
            quality=quality or ("audio" if mode == "audio" else "best"),
            video_id=video_id,
            message_id=message_id,
            status_line="چشم نیایش جانم، دارم برات می‌گیرمش ❤️",
        )
        return
    key = bot.job_key(chat_id, video_id)
    kind = "audio" if mode == "audio" or quality == "audio" else "video"
    title = bot.public_title(url) or video_id
    owned = False
    try:
        if choice == "link":
            owned = send_link(token, chat_id, message_id, path, title, video_id, key, quality, kind)
        else:
            owned = send_fitted(token, chat_id, message_id, path, kind, title, key, quality)
    except bot.FatalTokenError:
        print("BOT_TOKEN was rejected by Telegram.", file=sys.stderr)
    except Exception as exc:
        print(bot.scrub(f"{exc.__class__.__name__}: {exc}"), file=sys.stderr)
        try:
            mark(key, status="error", claim="")
            bot.edit_message(token, chat_id, message_id, bot.friendly_probe_error("unknown"))
        except Exception as nested:
            print(bot.scrub(nested.__class__.__name__), file=sys.stderr)
        owned = True
    finally:
        if owned:
            shutil.rmtree(ROOT / "downloads" / video_id, ignore_errors=True)


def download_requested(
    url: str,
    folder: Path,
    mode: str,
    quality: str,
    duration: int,
) -> tuple[Path | None, str, str]:
    if mode == "audio":
        path, error = download_audio(url, folder, duration)
        if path is None and error not in {"bot", "private", "age", "unavailable", "forbidden"}:
            path, error = download_audio(url, folder, duration)
        return path, "audio", error
    current = quality if quality in QUALITY_ORDER else "best"
    attempts = 0
    while current:
        if duration and guessed_bytes(duration, current) > DISK_SAFE:
            nxt = step_down(current)
            if nxt:
                print(f"skip quality={current}")
                current = nxt
                continue
            return None, "video", "huge"
        path, error = download_video(url, folder, current)
        if path is not None:
            return ensure_mp4(path), "video", ""
        if error in {"bot", "private", "age", "unavailable", "forbidden"}:
            return None, "video", error
        attempts += 1
        nxt = step_down(current)
        if attempts >= 2 or not nxt:
            return None, "video", error or "unknown"
        print(f"retry quality={nxt}")
        current = nxt
    return None, "video", "unknown"


def reset_folder(folder: Path) -> None:
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True, exist_ok=True)


def shrink(url: str, folder: Path, quality: str, duration: int) -> tuple[Path | None, str]:
    current = step_down(quality) or "360"
    if duration and guessed_bytes(duration, current) > DISK_SAFE:
        return None, "huge"
    reset_folder(folder)
    path, error = download_video(url, folder, current)
    if error in {"bot", "private", "age", "unavailable", "forbidden"}:
        return None, error
    if path is None:
        return None, error or "unknown"
    return ensure_mp4(path), current


def release_file(
    url: str,
    folder: Path,
    mode: str,
    quality: str,
    duration: int,
) -> tuple[Path | None, str, str]:
    if mode == "audio" or quality == "audio":
        reset_folder(folder)
        path, error = download_audio(url, folder, duration)
        if path is None:
            return None, "audio", error or "unknown"
        if path.stat().st_size > RELEASE_LIMIT:
            smaller = compress_audio(path, RELEASE_LIMIT, duration)
            if smaller is None:
                return path, "audio", "huge"
            path = smaller
        return path, "audio", "audio"

    current = quality if quality in QUALITY_ORDER else "best"
    while current:
        if duration and guessed_bytes(duration, current) > RELEASE_LIMIT:
            nxt = step_down(current)
            if not nxt:
                return None, "video", "huge"
            current = nxt
            continue
        reset_folder(folder)
        path, error = download_video(url, folder, current)
        if error in {"bot", "private", "age", "unavailable", "forbidden"}:
            return None, "video", error
        if path is None:
            nxt = step_down(current)
            if not nxt:
                return None, "video", error or "unknown"
            current = nxt
            continue
        path = ensure_mp4(path)
        if path.stat().st_size <= RELEASE_LIMIT:
            return path, "video", current
        nxt = step_down(current)
        if not nxt:
            return path, "video", "huge"
        current = nxt
    return None, "video", "huge"


def finish_error(token: str, chat_id: int, message_id: object, key: str, kind: str) -> None:
    mark(key, status="error")
    bot.edit_message(token, chat_id, message_id, bot.friendly_probe_error(kind))


def run(
    chat_id: int | None = None,
    url: str | None = None,
    mode: str | None = None,
    quality: str | None = None,
    video_id: str | None = None,
    message_id: int | None = None,
    status_line: str = "",
) -> None:
    token = bot.load_token()
    if not token:
        print("BOT_TOKEN is not set.", file=sys.stderr)
        sys.exit(1)
    if chat_id is None:
        chat_raw = os.environ.get("CHAT_ID", "").strip()
        if not chat_raw.lstrip("-").isdigit():
            print("bad download request")
            sys.exit(1)
        chat_id = int(chat_raw)
    if url is None:
        url = os.environ.get("VIDEO_URL", "")
    url = url.strip()
    if mode is None:
        mode = os.environ.get("MODE", "")
    mode = mode.strip()
    if quality is None:
        quality = os.environ.get("QUALITY", "")
    quality = quality.strip() or "best"
    if video_id is None:
        video_id = os.environ.get("VIDEO_ID", "").strip()
    if message_id is None:
        message_raw = os.environ.get("MESSAGE_ID", "").strip()
        message_id = int(message_raw) if message_raw.lstrip("-").isdigit() else 0
    if not bot.youtube_urls(url):
        print("bad download request")
        sys.exit(1)
    if not video_id:
        video_id = bot.video_id_from_url(url)
    key = bot.job_key(chat_id, video_id)
    folder = work_dir(video_id)
    mark(key, status="running", mode=mode, quality=quality, chat_id=chat_id, url=url, video_id=video_id)
    global _reporter
    line = status_line or "چشم نیایش جانم، دارم برات می‌گیرمش ❤️ چند دقیقه صبر کن"
    _reporter = Bar(token, chat_id, message_id, line, url)
    title = bot.public_title(url) or video_id
    duration = 0
    keep = False
    try:
        if mode == "deliver-file":
            path, kind, error = download_requested(
                url,
                folder,
                "audio" if quality == "audio" or mode == "audio" else "video",
                quality,
                duration,
            )
            if path is None:
                finish_error(token, chat_id, message_id, key, error or "unknown")
                return
            send_fitted(token, chat_id, message_id, path, kind, title, key, quality)
            return

        if mode == "shrink-audio":
            reset_folder(folder)
            path, error = download_audio(url, folder, duration)
            if path is None:
                finish_error(token, chat_id, message_id, key, error or "unknown")
                return
            offer_choice(token, chat_id, message_id, path, "audio", title, video_id, key, "audio")
            keep = True
            return

        if mode == "shrink":
            path, outcome = shrink(url, folder, quality, duration)
            if path is None:
                if outcome == "huge":
                    mark(key, status="too_big", quality=quality)
                    too_big_message(token, chat_id, message_id, video_id)
                    return
                finish_error(token, chat_id, message_id, key, outcome or "unknown")
                return
            offer_choice(token, chat_id, message_id, path, "video", title, video_id, key, str(outcome))
            keep = True
            return

        if mode == "release":
            wanted = "audio" if quality == "audio" or mode == "audio" else "video"
            path, kind, outcome = release_file(url, folder, wanted, quality, duration)
            if wanted == "video" and outcome == "huge":
                bot.edit_message(
                    token,
                    chat_id,
                    message_id,
                    "عزیزدلم، خود ویدیو آن‌قدر بزرگ است که این‌جا جا نمی‌شود. صداش را برات می‌گذارم ❤️",
                )
                path, kind, outcome = release_file(url, folder, "audio", "audio", duration)
            if path is None or outcome == "huge" or path.stat().st_size > RELEASE_LIMIT:
                if outcome in {"bot", "private", "age", "unavailable"}:
                    finish_error(token, chat_id, message_id, key, outcome)
                    return
                mark(key, status="error")
                impossible_message(token, chat_id, message_id)
                return
            note = ""
            if kind == "audio" and quality != "audio":
                note = "خود تصویر این‌جا جا نشد، برای همین صدا را گذاشتم.\n"
            bot.edit_message(token, chat_id, message_id, "فایل آماده است عزیزدلم، دارم لینکش را می‌گذارم ❤️")
            link = upload_release(path, video_id, title)
            bot.send_message(
                token,
                chat_id,
                "قربونت برم، لینک دانلود را گذاشتم.\n"
                f"{note}"
                "چون ریپو عمومی است، هر کسی این لینک را داشته باشد می‌تواند فایل را ببیند.\n"
                "تا ۲۴ ساعت دیگر خودبه‌خود پاک می‌شود.\n"
                f"{link}",
            )
            bot.edit_message(token, chat_id, message_id, "لینک را فرستادم نیایش جانم ❤️")
            mark(key, status="done", quality=str(outcome), mode=kind)
            return

        path, kind, error = download_requested(url, folder, mode, quality, duration)
        if path is None and error == "huge":
            mark(key, status="too_big", quality=quality)
            too_big_message(token, chat_id, message_id, video_id)
            return
        if path is None:
            finish_error(token, chat_id, message_id, key, error or "unknown")
            return
        offer_choice(token, chat_id, message_id, path, kind, title, video_id, key, quality)
        keep = True
    except bot.FatalTokenError:
        print("BOT_TOKEN was rejected by Telegram.", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(bot.scrub(f"{exc.__class__.__name__}: {exc}"), file=sys.stderr)
        try:
            mark(key, status="error")
            bot.edit_message(token, chat_id, message_id, bot.friendly_probe_error("unknown"))
        except Exception as nested:
            print(bot.scrub(nested.__class__.__name__), file=sys.stderr)
    finally:
        if not keep:
            shutil.rmtree(folder, ignore_errors=True)
        bot.clear_cookies()
        _reporter = None


if __name__ == "__main__":
    run()
