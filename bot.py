#!/usr/bin/env python3
"""ربات دانلود یوتیوب نیایش.

پروسهٔ همیشه‌روشن روی کامپیوتر نیست. گیت‌هاب اکشنز این فایل را بیدار نگه می‌دارد،
چند ساعت گوش می‌دهد، بعد اجرا را به نوبت بعدی می‌سپارد. توکن فقط از محیط خوانده می‌شود.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state.json"
ACTIVE = {"queued", "running", "sending"}
STARTED = {"queued", "running", "sending", "done"}
STALE_SECONDS = 6 * 60 * 60
FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
TOKEN_LIKE_RE = re.compile(r"\d{6,}\s*:\s*[A-Za-z0-9_-]{20,}")
YOUTUBE_RE = re.compile(
    r"https?://(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)/\S+",
    re.IGNORECASE,
)

COMMANDS = [
    {"command": "start", "description": "سلام و طرز کار"},
    {"command": "help", "description": "راهنمای کوتاه"},
]
STATE_LOCK = threading.RLock()


class FatalTokenError(Exception):
    """تلگرام توکن را نپذیرفت. متن خالی می‌ماند تا توکن در لاگ نیاید."""


class TelegramError(Exception):
    def __init__(self, method: str, description: str, status: int = 0):
        super().__init__(description)
        self.method = method
        self.description = description
        self.status = status


def fa(text: str) -> str:
    return str(text).translate(FA_DIGITS)


def scrub(text: str) -> str:
    secret_values = [
        os.environ.get("BOT_TOKEN", ""),
        os.environ.get("YT_COOKIES", ""),
        os.environ.get("GITHUB_TOKEN", ""),
        os.environ.get("GH_TOKEN", ""),
    ]
    for value in secret_values:
        if value:
            text = text.replace(value, "[secret]")
    return TOKEN_LIKE_RE.sub("[secret]", text)


def blank_state() -> dict:
    return {"allowed_chat_ids": [], "jobs": {}, "last_update_id": 0}


def load_state() -> dict:
    if not STATE_PATH.exists():
        return blank_state()
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return blank_state()
    if not isinstance(raw, dict):
        return blank_state()
    raw.setdefault("allowed_chat_ids", [])
    raw.setdefault("jobs", {})
    raw.setdefault("last_update_id", 0)
    return raw


def save_state(state: dict) -> None:
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def env_allowed() -> set[int]:
    found: set[int] = set()
    for part in os.environ.get("ALLOWED_CHAT_IDS", "").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            found.add(int(part))
    return found


def chat_allowed(state: dict, chat_id: int) -> bool:
    allowed = env_allowed()
    if allowed:
        return chat_id in allowed
    saved = [int(item) for item in state.get("allowed_chat_ids") or [] if str(item).lstrip("-").isdigit()]
    if not saved:
        return True
    return chat_id in saved


def remember_chat(state: dict, chat_id: int) -> None:
    if env_allowed():
        return
    saved = state.setdefault("allowed_chat_ids", [])
    if chat_id not in saved:
        saved.append(chat_id)


def git_run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=ROOT,
        check=False,
        text=True,
        timeout=40,
        capture_output=True,
    )


def merge_state(local: dict, remote: dict) -> dict:
    allowed = []
    for item in list(local.get("allowed_chat_ids") or []) + list(remote.get("allowed_chat_ids") or []):
        if str(item).lstrip("-").isdigit() and int(item) not in allowed:
            allowed.append(int(item))
    jobs: dict = {}
    keys = set((local.get("jobs") or {})) | set((remote.get("jobs") or {}))
    for key in keys:
        left = (local.get("jobs") or {}).get(key)
        right = (remote.get("jobs") or {}).get(key)
        if isinstance(left, dict) and not isinstance(right, dict):
            jobs[key] = left
        elif isinstance(right, dict) and not isinstance(left, dict):
            jobs[key] = right
        elif isinstance(left, dict) and isinstance(right, dict):
            jobs[key] = left if float(left.get("updated") or 0) >= float(right.get("updated") or 0) else right
    now = time.time()
    kept = {}
    for key, job in jobs.items():
        updated = float(job.get("updated") or 0)
        if job.get("status") in ACTIVE and now - updated < STALE_SECONDS:
            kept[key] = job
        elif now - updated < 3 * 24 * 60 * 60:
            kept[key] = job
    if len(kept) > 40:
        ordered = sorted(kept.items(), key=lambda item: float(item[1].get("updated") or 0), reverse=True)
        kept = dict(ordered[:40])
    return {
        "allowed_chat_ids": allowed,
        "jobs": kept,
        "last_update_id": max(int(local.get("last_update_id") or 0), int(remote.get("last_update_id") or 0)),
    }


def publish_state() -> None:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    with STATE_LOCK:
        _publish_state()


def _publish_state() -> None:
    local = load_state()
    for _ in range(5):
        fetched = git_run(["git", "fetch", "origin", "main"])
        if fetched.returncode != 0:
            print("state fetch failed")
            return
        remote_raw = git_run(["git", "show", "origin/main:state.json"])
        if remote_raw.returncode == 0:
            try:
                remote = json.loads(remote_raw.stdout)
            except json.JSONDecodeError:
                remote = blank_state()
            local = merge_state(local, remote if isinstance(remote, dict) else blank_state())
        checkout = git_run(["git", "checkout", "-f", "-B", "main", "origin/main"])
        if checkout.returncode != 0:
            print("state checkout failed")
            return
        save_state(local)
        git_run(["git", "add", "state.json"])
        quiet = git_run(["git", "diff", "--cached", "--quiet", "state.json"])
        if quiet.returncode == 0:
            return
        committed = git_run(["git", "commit", "-m", "Update bot state"])
        if committed.returncode != 0:
            print("state commit failed")
            return
        pushed = git_run(["git", "push", "origin", "HEAD:main"])
        if pushed.returncode == 0:
            return
        local = load_state()
    print("state publish failed")


def api(token: str, method: str, payload: dict | None = None, timeout: int = 70) -> object:
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        response = requests.post(url, json=payload or {}, timeout=timeout)
    except requests.RequestException as exc:
        raise TelegramError(method, scrub(exc.__class__.__name__), 0) from None
    status = response.status_code
    try:
        body = response.json()
    except ValueError:
        if status in (401, 404):
            raise FatalTokenError()
        raise TelegramError(method, "bad response", status)
    if status in (401, 404) or not isinstance(body, dict):
        raise FatalTokenError()
    if not body.get("ok"):
        raise TelegramError(method, str(body.get("description") or "error"), status)
    return body.get("result")


def send_message(
    token: str,
    chat_id: int,
    text: str,
    reply_markup: dict | None = None,
    preview: bool = False,
) -> int | None:
    payload: dict = {
        "chat_id": chat_id,
        "text": text[:4000],
        "disable_web_page_preview": not preview,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    result = api(token, "sendMessage", payload)
    if isinstance(result, dict) and isinstance(result.get("message_id"), int):
        return result["message_id"]
    return None


def edit_message(
    token: str,
    chat_id: int,
    message_id: object,
    text: str,
    reply_markup: dict | None = None,
    preview: bool = False,
) -> None:
    if not isinstance(message_id, int):
        return
    payload: dict = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text[:4000],
        "disable_web_page_preview": not preview,
        "reply_markup": reply_markup if reply_markup is not None else {"inline_keyboard": []},
    }
    try:
        api(token, "editMessageText", payload)
    except TelegramError as exc:
        if "not modified" in exc.description.lower():
            return
        print(f"edit failed status={exc.status}")


def answer_callback(token: str, callback_id: str, text: str) -> None:
    if not callback_id:
        return
    try:
        api(
            token,
            "answerCallbackQuery",
            {"callback_query_id": callback_id, "text": text[:180]},
        )
    except FatalTokenError:
        raise
    except TelegramError:
        print("answerCallbackQuery failed")


def prepare_bot(token: str) -> None:
    api(token, "deleteWebhook", {"drop_pending_updates": False})
    try:
        api(token, "setMyCommands", {"commands": COMMANDS})
    except FatalTokenError:
        raise
    except TelegramError:
        print("could not refresh the command menu")


def clean_url(url: str) -> str:
    return url.rstrip(").,،؛»\"'")


def youtube_urls(text: str) -> list[str]:
    return [clean_url(match.group(0)) for match in YOUTUBE_RE.finditer(text or "")]


def video_id_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    if host.endswith("youtu.be"):
        part = parsed.path.strip("/").split("/")[0]
        return part[:32]
    query = urllib.parse.parse_qs(parsed.query)
    if query.get("v") and query["v"][0]:
        return query["v"][0][:32]
    parts = [part for part in parsed.path.split("/") if part]
    for key in ("shorts", "embed", "live", "v"):
        if key in parts:
            index = parts.index(key)
            if index + 1 < len(parts):
                return parts[index + 1][:32]
    return ""


def stable_id(url: str) -> str:
    found = video_id_from_url(url)
    if re.fullmatch(r"[A-Za-z0-9_-]{6,32}", found):
        return found
    import hashlib

    return "v" + hashlib.sha1(url.encode()).hexdigest()[:10]


def public_title(url: str) -> str:
    try:
        response = requests.get(
            "https://www.youtube.com/oembed",
            params={"url": url, "format": "json"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=20,
        )
        if response.status_code != 200:
            return ""
        title = response.json().get("title")
        return str(title or "").replace("\n", " ")[:300]
    except (requests.RequestException, ValueError):
        return ""


def playlist_only(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    has_list = bool(query.get("list"))
    return has_list and not video_id_from_url(url) or parsed.path.rstrip("/").endswith("/playlist")


def format_duration(seconds: object) -> str:
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return "نامشخص"
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        text = f"{hours} ساعت و {minutes} دقیقه"
    elif minutes:
        text = f"{minutes} دقیقه و {secs} ثانیه"
    else:
        text = f"{secs} ثانیه"
    return fa(text)


def filling_bar(percent: float, width: int = 12) -> str:
    bounded = max(0.0, min(100.0, float(percent)))
    filled = int(round(bounded / 100 * width))
    if bounded > 0 and filled == 0:
        filled = 1
    if bounded < 100 and filled == width:
        filled = width - 1
    return "▰" * filled + "▱" * (width - filled)


def progress_text(percent: float, line: str, url: str = "") -> str:
    shown = int(round(max(0.0, min(100.0, float(percent)))))
    body = f"{line}\n{filling_bar(shown)}  {fa(str(shown))}٪"
    if url:
        body = f"{body}\n{url}"
    return body


def ytdlp_base(url: str, clients: str = "") -> list[str]:
    playlist = ["--playlist-items", "1"] if playlist_only(url) else ["--no-playlist"]
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--no-warnings",
        "--socket-timeout",
        "25",
        "--retries",
        "2",
        "--fragment-retries",
        "5",
        *playlist,
        *cookies_args(),
    ]
    if clients:
        command.extend(["--extractor-args", f"youtube:player_client={clients}"])
    deno = shutil.which("deno")
    if deno:
        command.extend(["--js-runtimes", f"deno:{deno}", "--remote-components", "ejs:github"])
    return command


def cookies_args() -> list[str]:
    raw = os.environ.get("YT_COOKIES", "")
    if not raw.strip():
        return []
    path = ROOT / "cookies.txt"
    path.write_text(raw, encoding="utf-8", newline="\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return ["--cookies", str(path)]


def clear_cookies() -> None:
    path = ROOT / "cookies.txt"
    if path.exists():
        path.unlink()


def read_info(stdout: str) -> dict:
    info = json.loads(stdout)
    if isinstance(info, dict) and info.get("_type") == "playlist":
        entries = [entry for entry in info.get("entries") or [] if isinstance(entry, dict)]
        if not entries:
            raise RuntimeError("unavailable")
        info = entries[0]
    if not isinstance(info, dict) or not info.get("id"):
        raise RuntimeError("unknown")
    return info


def probe(url: str) -> dict:
    last_kind = "unknown"
    for clients in ("mweb,android_vr,web_embedded,tv", "android_vr", "web_safari,tv"):
        command = [*ytdlp_base(url, clients), "-J", "--retries", "1", url]
        try:
            completed = subprocess.run(
                command,
                cwd=ROOT,
                check=False,
                text=True,
                timeout=75,
                capture_output=True,
            )
        finally:
            clear_cookies()
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        if completed.returncode == 0 and stdout.strip():
            try:
                return read_info(stdout)
            except (json.JSONDecodeError, RuntimeError) as exc:
                last_kind = str(exc) if isinstance(exc, RuntimeError) else "unknown"
                print(scrub(stderr[-2000:]))
                continue
        print(scrub(stderr[-2000:]))
        last_kind = classify_ytdlp(stderr) or "unknown"
        if last_kind in {"private", "age", "unavailable"}:
            raise RuntimeError(last_kind)
    raise RuntimeError(last_kind)


def classify_ytdlp(stderr: str) -> str:
    text = (stderr or "").lower()
    if (
        "confirm your age" in text
        or "age-restricted" in text
        or "age restricted" in text
        or "inappropriate for some users" in text
    ):
        return "age"
    if "not a bot" in text or "sign in to confirm" in text:
        return "bot"
    if "private video" in text or "video is private" in text or "this video is private" in text:
        return "private"
    if "members-only" in text or "join this channel" in text:
        return "private"
    if "http error 403" in text or "403: forbidden" in text or "403 forbidden" in text:
        return "forbidden"
    if "video unavailable" in text or "has been removed" in text or "has been deleted" in text:
        return "unavailable"
    return ""


def friendly_probe_error(kind: str) -> str:
    if kind in {"bot", "forbidden"}:
        return "یوتیوب این بار اجازه نداد عزیزدلم، یه کم دیگه دوباره امتحان کن"
    if kind == "private":
        return "این ویدیو خصوصی یا محدود است قربونت برم، لینکش را ندارم"
    if kind == "age":
        return "این ویدیو محدودیت سنی دارد عزیزدلم. اگر کوکی یوتیوب را بگذاری، شاید دفعهٔ بعد بشود"
    if kind == "unavailable":
        return "این ویدیو پاک شده یا دیگر در دسترس نیست، نیایش جانم"
    return "این بار نشد عشقم. لینک را یک کم بعد دوباره بفرست، من اینجام 💙"


def choice_keyboard(video_id: str) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "🎬 ۳۶۰", "callback_data": f"v:360:{video_id}"},
                {"text": "🎬 ۷۲۰", "callback_data": f"v:720:{video_id}"},
            ],
            [
                {"text": "🎬 ۱۰۸۰", "callback_data": f"v:1080:{video_id}"},
                {"text": "🎬 بهترین", "callback_data": f"v:best:{video_id}"},
            ],
            [{"text": "🎵 فقط صدا", "callback_data": f"a:mp3:{video_id}"}],
        ]
    }


def delivery_keyboard(video_id: str) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "📉 کیفیت پایین‌تر", "callback_data": f"q:low:{video_id}"},
                {"text": "🔗 لینک دانلود", "callback_data": f"q:link:{video_id}"},
            ]
        ]
    }


def job_key(chat_id: int, video_id: str) -> str:
    return f"{chat_id}:{video_id}"


def already_started(status: object) -> bool:
    return status in STARTED


def newer_job(left: object, right: object) -> dict | None:
    if not isinstance(left, dict):
        return right if isinstance(right, dict) else None
    if not isinstance(right, dict):
        return left
    if float(left.get("updated") or 0) >= float(right.get("updated") or 0):
        return left
    return right


def adopt_disk_jobs(state: dict) -> None:
    with STATE_LOCK:
        disk = load_state()
    merged = merge_state(state, disk)
    state["jobs"] = merged["jobs"]
    state["allowed_chat_ids"] = merged["allowed_chat_ids"]
    state["last_update_id"] = max(
        int(state.get("last_update_id") or 0),
        int(merged.get("last_update_id") or 0),
    )


def busy(state: dict, chat_id: int, ignore_key: str | None = None) -> bool:
    now = time.time()
    for key, job in (state.get("jobs") or {}).items():
        if key == ignore_key or not isinstance(job, dict):
            continue
        if job.get("chat_id") != chat_id:
            continue
        if job.get("status") in ACTIVE and now - float(job.get("updated") or 0) < STALE_SECONDS:
            return True
    return False


def put_job(state: dict, key: str, **fields: object) -> dict:
    job = dict((state.setdefault("jobs", {})).get(key) or {})
    job.update(fields)
    job["updated"] = time.time()
    state["jobs"][key] = job
    return job


def offer_video(token: str, state: dict, chat_id: int, url: str) -> None:
    adopt_disk_jobs(state)
    if busy(state, chat_id):
        send_message(
            token,
            chat_id,
            "نیایش جانم، هنوز یکی را دارم برات می‌گیرم. تموم که شد دوباره بفرست، عشقم 💙",
        )
        return
    status_id = send_message(
        token,
        chat_id,
        progress_text(6, "دارم از یوتیوب می‌پرسم نیایش جانم 💙"),
    )
    stop = threading.Event()

    def tick() -> None:
        percent = 6.0
        while not stop.is_set():
            if stop.wait(2.5):
                return
            percent = min(88.0, percent + 7)
            edit_message(
                token,
                chat_id,
                status_id,
                progress_text(percent, "دارم از یوتیوب می‌پرسم نیایش جانم 💙"),
            )

    worker = threading.Thread(target=tick, daemon=True)
    worker.start()
    info = None
    error_kind = ""
    try:
        info = {
            "id": stable_id(url),
            "title": public_title(url) or "ویدیوی یوتیوب",
            "duration": None,
            "is_live": False,
        }
    except Exception as exc:
        print(scrub(f"{exc.__class__.__name__}: {exc}"))
        info = {"id": stable_id(url), "title": "ویدیوی یوتیوب", "duration": None, "is_live": False}
    finally:
        stop.set()
        worker.join(4)
    if error_kind or info is None:
        show = friendly_probe_error(error_kind or "unknown")
        if status_id:
            edit_message(token, chat_id, status_id, show)
        else:
            send_message(token, chat_id, show)
        return
    if info.get("is_live"):
        live = "این یکی پخش زنده است عزیزدلم. لینک ویدیوی ضبط‌شده را بفرست تا برات بگیرم 💙"
        if status_id:
            edit_message(token, chat_id, status_id, live)
        else:
            send_message(token, chat_id, live)
        return
    video_id = str(info.get("id") or video_id_from_url(url) or "")[:32]
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,32}", video_id):
        show = friendly_probe_error("unknown")
        if status_id:
            edit_message(token, chat_id, status_id, show)
        else:
            send_message(token, chat_id, show)
        return
    title = str(info.get("title") or "بدون عنوان").replace("\n", " ")[:300]
    duration = info.get("duration")
    key = job_key(chat_id, video_id)
    note = ""
    if isinstance(duration, (int, float)) and duration >= 2 * 60 * 60:
        note = "\n\nاین یکی طولانی است نیایش جانم. محدودش نمی‌کنم؛ فقط ممکن است کمی بیشتر طول بکشد 💙"
    duration_line = ""
    if isinstance(duration, (int, float)) and duration > 0:
        duration_line = f"\nمدت: {format_duration(duration)}"
    text = (
        f"نیایش جانم 💙\n{title}"
        f"{duration_line}\n{url}\n\n"
        f"چطور برات بفرستم، عشقم؟{note}"
    )
    if status_id:
        edit_message(token, chat_id, status_id, text, choice_keyboard(video_id), preview=True)
        message_id = status_id
    else:
        message_id = send_message(token, chat_id, text, choice_keyboard(video_id), preview=True)
    put_job(
        state,
        key,
        chat_id=chat_id,
        url=url,
        video_id=video_id,
        title=title,
        duration=int(duration) if isinstance(duration, (int, float)) else 0,
        status="choosing",
        message_id=message_id or 0,
    )


def dispatch_download(job: dict, mode: str, quality: str) -> bool:
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not repo or not github_token:
        print("dispatch unavailable")
        return False
    response = requests.post(
        f"https://api.github.com/repos/{repo}/actions/workflows/download.yml/dispatches",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {github_token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={
            "ref": "main",
            "inputs": {
                "chat_id": str(job["chat_id"]),
                "url": str(job["url"]),
                "mode": mode,
                "quality": quality,
                "message_id": str(job.get("message_id") or 0),
                "video_id": str(job["video_id"]),
            },
        },
        timeout=30,
    )
    if response.status_code not in (200, 204):
        print(f"dispatch failed status={response.status_code}")
        return False
    return True


def _shift_left() -> float:
    raw = os.environ.get("WATCH_DEADLINE", "").strip()
    if not raw:
        return 10**9
    try:
        return float(raw) - time.time()
    except ValueError:
        return 10**9


def _can_download_here() -> bool:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return False
    if not os.environ.get("YT_PROXY", "").strip():
        return False
    return _shift_left() > 25 * 60


def _nudge(token: str, chat_id: int, message_id: object, line: str, url: str) -> None:
    percent = 8
    for _ in range(6):
        time.sleep(3)
        percent = min(32, percent + 4)
        edit_message(token, chat_id, message_id, progress_text(percent, line, url), preview=bool(url))


_INFLIGHT: set[str] = set()


def _run_local(job: dict, mode: str, quality: str, ack: str) -> None:
    try:
        import download

        download.run(
            chat_id=int(job["chat_id"]),
            url=str(job["url"]),
            mode=mode,
            quality=quality,
            video_id=str(job["video_id"]),
            message_id=int(job.get("message_id") or 0),
            status_line=ack,
        )
    except SystemExit:
        print("local download stopped")
    except Exception as exc:
        print(scrub(f"local download {exc.__class__.__name__}: {exc}"))


def begin_download(
    token: str,
    state: dict,
    chat_id: int,
    job: dict,
    key: str,
    mode: str,
    quality: str,
    ack: str,
) -> str:
    if busy(state, chat_id, ignore_key=key):
        send_message(
            token,
            chat_id,
            "نیایش جانم، هنوز یکی را دارم برات می‌گیرم. تموم که شد دوباره بفرست، عشقم 💙",
        )
        return "busy"
    message_id = job.get("message_id")
    link = str(job.get("url") or "")
    with STATE_LOCK:
        disk = load_state()
        current = newer_job((disk.get("jobs") or {}).get(key), (state.get("jobs") or {}).get(key))
        status = current.get("status") if isinstance(current, dict) else ""
        if key in _INFLIGHT or already_started(status):
            if isinstance(current, dict):
                state.setdefault("jobs", {})[key] = current
            return "done" if status == "done" else "duplicate"
        _INFLIGHT.add(key)
        saved = put_job(
            disk,
            key,
            chat_id=chat_id,
            url=str(job.get("url") or ""),
            video_id=str(job.get("video_id") or ""),
            title=str(job.get("title") or ""),
            duration=job.get("duration") or 0,
            status="queued",
            mode=mode,
            quality=quality,
            message_id=message_id or 0,
        )
        state.setdefault("jobs", {})[key] = dict(saved)
        save_state(disk)
    edit_message(token, chat_id, message_id, progress_text(6, ack, link), preview=bool(link))
    if _can_download_here():
        def runner() -> None:
            try:
                _run_local(state["jobs"][key], mode, quality, ack)
            finally:
                _INFLIGHT.discard(key)

        threading.Thread(target=runner, daemon=True).start()
        return "started"
    if dispatch_download(state["jobs"][key], mode, quality):
        _INFLIGHT.discard(key)
        threading.Thread(
            target=_nudge,
            args=(token, chat_id, message_id, ack, link),
            daemon=True,
        ).start()
        return "started"
    _INFLIGHT.discard(key)
    put_job(state, key, status="error")
    edit_message(
        token,
        chat_id,
        message_id,
        "این بار نشد عشقم. یک کم دیگر دوباره لینک را بفرست، من اینجام 💙",
    )
    return "busy"


def lower_quality(quality: str) -> str | None:
    order = ["best", "1080", "720", "360"]
    if quality not in order:
        return "360"
    index = order.index(quality)
    if index + 1 < len(order):
        return order[index + 1]
    return None


def _button_reply(outcome: str, started: str) -> str:
    if outcome == "started":
        return started
    if outcome == "done":
        return "تقدیم با عشق نیایشم"
    if outcome == "duplicate":
        return "همین یکی را دارم می‌فرستم"
    return ""


def handle_callback(token: str, state: dict, callback: dict) -> None:
    callback_id = str(callback.get("id") or "")
    message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    chat_id = chat.get("id")
    data = callback.get("data") if isinstance(callback.get("data"), str) else ""
    if chat.get("type") != "private" or not isinstance(chat_id, int):
        answer_callback(token, callback_id, "")
        return
    if not chat_allowed(state, chat_id):
        answer_callback(token, callback_id, "")
        return
    remember_chat(state, chat_id)
    parts = data.split(":", 2)
    if len(parts) != 3:
        answer_callback(token, callback_id, "دوباره لینک را بفرست نیایش جانم")
        return
    kind, choice, video_id = parts
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,32}", video_id):
        answer_callback(token, callback_id, "دوباره لینک را بفرست نیایش جانم")
        return
    key = job_key(chat_id, video_id)
    job = (state.get("jobs") or {}).get(key)
    if not isinstance(job, dict) or job.get("chat_id") != chat_id or not job.get("url"):
        answer_callback(token, callback_id, "لینک را یک بار دیگر بفرست نیایش جانم")
        return
    if isinstance(message.get("message_id"), int):
        job["message_id"] = message["message_id"]
    if kind == "v" and choice in {"360", "720", "1080", "best"}:
        outcome = begin_download(
            token,
            state,
            chat_id,
            job,
            key,
            "video",
            choice,
            "چشم نیایش جانم، دارم برات می‌گیرمش 💙 چند دقیقه صبر کن",
        )
        answer_callback(token, callback_id, _button_reply(outcome, "چشم نیایش جانم"))
        return
    if kind == "a" and choice == "mp3":
        outcome = begin_download(
            token,
            state,
            chat_id,
            job,
            key,
            "audio",
            "audio",
            "چشم عشقم، دارم صداش را برات جدا می‌کنم 💙 چند دقیقه صبر کن",
        )
        answer_callback(token, callback_id, _button_reply(outcome, "چشم عشقم"))
        return
    if kind == "q" and choice == "low":
        shrink_mode = "shrink-audio" if job.get("mode") == "audio" else "shrink"
        outcome = begin_download(
            token,
            state,
            chat_id,
            job,
            key,
            shrink_mode,
            "audio" if shrink_mode == "shrink-audio" else str(job.get("quality") or "best"),
            "چشم قربونت برم، کیفیت را پایین می‌آورم تا بتواند برایت بیاید 💙",
        )
        answer_callback(token, callback_id, _button_reply(outcome, "کیفیت را پایین‌تر می‌آورم"))
        return
    if kind == "q" and choice == "link":
        outcome = begin_download(
            token,
            state,
            chat_id,
            job,
            key,
            "release",
            str(job.get("quality") or "best"),
            "چشم عزیزدلم، دارم لینک دانلود را برات آماده می‌کنم 💙",
        )
        answer_callback(token, callback_id, _button_reply(outcome, "لینک را آماده می‌کنم"))
        return
    answer_callback(token, callback_id, "این دکمه دیگر تازه نیست")


def handle_message(token: str, state: dict, message: dict) -> None:
    if message.get("from", {}).get("is_bot"):
        return
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    chat_id = chat.get("id")
    if chat.get("type") != "private" or not isinstance(chat_id, int):
        return
    if not chat_allowed(state, chat_id):
        return
    remember_chat(state, chat_id)
    text = message.get("text") if isinstance(message.get("text"), str) else ""
    stripped = text.strip()
    if not stripped:
        send_message(token, chat_id, "نیایش جانم، لینک یوتیوب را برام بفرست 💙")
        return
    command = stripped.split()[0].split("@")[0].lower()
    if command in {"/start", "/help"} or stripped in {"سلام", "سلم"}:
        if command == "/help":
            send_message(
                token,
                chat_id,
                "نیایش جانم، لینک یوتیوب را بفرست.\n"
                "بعدش دکمه‌ها را نشانت می‌دهم: ویدیو با کیفیت‌های مختلف، یا فقط صدا.\n"
                "هر کدام را بخواهی، چشم عشقم 💙",
            )
            return
        send_message(
            token,
            chat_id,
            "سلام نیایش جانم 💙\n"
            "لینک یوتیوب رو برام بفرست.\n"
            "بعدش خودم می‌پرسم ویدیو باشه یا فقط صدا، و با چه کیفیتی.\n"
            "قربونت برم.",
        )
        return
    urls = youtube_urls(stripped)
    if urls:
        offer_video(token, state, chat_id, urls[0])
        return
    if "http://" in stripped or "https://" in stripped:
        send_message(token, chat_id, "عزیزدلم، من فقط لینک یوتیوب را می‌توانم برات بگیرم 💙")
        return
    send_message(token, chat_id, "نیایش جانم، لینک یوتیوب را بفرست تا بپرسم چطور می‌خواهی 💙")


def pull_updates(token: str, offset: int, timeout: int) -> list[dict]:
    result = api(
        token,
        "getUpdates",
        {
            "allowed_updates": ["message", "callback_query"],
            "offset": offset,
            "timeout": timeout,
        },
        timeout=timeout + 20,
    )
    if not isinstance(result, list):
        return []
    return [item for item in result if isinstance(item, dict)]


def process_updates(token: str, state: dict, poll_timeout: int) -> int:
    try:
        updates = pull_updates(token, int(state.get("last_update_id") or 0) + 1, poll_timeout)
    except FatalTokenError:
        raise
    except TelegramError as exc:
        if exc.status == 409:
            raise
        print(f"could not read updates status={exc.status}")
        return 0
    handled = 0
    for update in updates:
        update_id = update.get("update_id")
        if not isinstance(update_id, int):
            continue
        try:
            if isinstance(update.get("callback_query"), dict):
                handle_callback(token, state, update["callback_query"])
            elif isinstance(update.get("message"), dict):
                handle_message(token, state, update["message"])
        except FatalTokenError:
            raise
        except TelegramError as exc:
            print(f"update failed status={exc.status}")
        except Exception as exc:
            print(scrub(f"update failed {exc.__class__.__name__}: {exc}"))
        state["last_update_id"] = update_id
        handled += 1
    return handled


def run_once(token: str, poll_timeout: int = 0, prepare: bool = True) -> int:
    with STATE_LOCK:
        state = load_state()
    before = json.dumps(state, ensure_ascii=False, sort_keys=True)
    try:
        if prepare:
            prepare_bot(token)
        handled = process_updates(token, state, poll_timeout)
    except FatalTokenError:
        print("BOT_TOKEN was rejected by Telegram.", file=sys.stderr)
        return 1
    except TelegramError as exc:
        if exc.status == 409:
            print("another instance is polling; skip")
            time.sleep(15)
            return 0
        raise
    with STATE_LOCK:
        merged = merge_state(state, load_state())
        after = json.dumps(merged, ensure_ascii=False, sort_keys=True)
        changed = after != before
        if changed:
            save_state(merged)
    if changed:
        publish_state()
    print(f"done updates={handled}", flush=True)
    return 0


def load_token() -> str:
    token = os.environ.get("BOT_TOKEN", "").strip()
    if token:
        return token
    env_file = ROOT / ".env"
    if not env_file.exists():
        return ""
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("BOT_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def self_check() -> None:
    sample = "ببین https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123."
    urls = youtube_urls(sample)
    assert urls == ["https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123"]
    assert video_id_from_url(urls[0]) == "dQw4w9WgXcQ"
    assert video_id_from_url("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert video_id_from_url("https://m.youtube.com/shorts/abcdefghijk") == "abcdefghijk"
    assert playlist_only("https://www.youtube.com/playlist?list=PL123")
    assert not playlist_only(urls[0])
    assert format_duration(3661).startswith("۱")
    fresh = time.time()
    merged = merge_state(
        {"allowed_chat_ids": [1], "jobs": {"a": {"updated": fresh, "status": "done"}}, "last_update_id": 5},
        {"allowed_chat_ids": [2], "jobs": {"a": {"updated": fresh + 1, "status": "running"}}, "last_update_id": 4},
    )
    assert merged["last_update_id"] == 5
    assert merged["jobs"]["a"]["status"] == "running"
    assert merged["allowed_chat_ids"] == [1, 2]
    assert stable_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert filling_bar(0) == "▱" * 12
    assert filling_bar(100) == "▰" * 12
    assert filling_bar(50).count("▰") == 6
    assert classify_ytdlp("Sign in to confirm your age") == "age"
    assert classify_ytdlp("Sign in to confirm you're not a bot") == "bot"
    webpage = "[youtube] X0AqMgJmbNU: Downloading webpage\nERROR: unable to download video data: HTTP Error 403: Forbidden"
    assert classify_ytdlp(webpage) == "forbidden"
    assert progress_text(0, "چشم", "https://youtu.be/X0AqMgJmbNU").endswith("https://youtu.be/X0AqMgJmbNU")
    assert lower_quality("best") == "1080"
    assert lower_quality("360") is None
    assert already_started("queued")
    assert already_started("running")
    assert already_started("sending")
    assert already_started("done")
    assert not already_started("choosing")
    assert not already_started("error")
    assert newer_job({"updated": 10, "status": "done"}, {"updated": 9, "status": "queued"})["status"] == "done"
    assert newer_job({"updated": 9, "status": "done"}, {"updated": 11, "status": "choosing"})["status"] == "choosing"
    print("check ok")


def main() -> None:
    if "--check" in sys.argv:
        self_check()
        return
    token = load_token()
    if not token:
        print("BOT_TOKEN is not set.", file=sys.stderr)
        sys.exit(1)
    if "--watch" in sys.argv:
        limit = int(os.environ.get("WATCH_SECONDS", "0") or "0")
        deadline = time.time() + limit if limit else None
        if deadline is not None:
            os.environ["WATCH_DEADLINE"] = str(deadline)
        print("watching", flush=True)
        prepare_bot(token)
        while deadline is None or time.time() < deadline:
            try:
                run_once(token, poll_timeout=50, prepare=False)
            except FatalTokenError:
                print("BOT_TOKEN was rejected by Telegram.", file=sys.stderr)
                sys.exit(1)
            except Exception as exc:
                print(scrub(f"{exc.__class__.__name__}: {exc}"), file=sys.stderr)
                time.sleep(3)
        print("watch shift ended", flush=True)
        return
    sys.exit(run_once(token))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(scrub(f"{exc.__class__.__name__}: {exc}"), file=sys.stderr)
        sys.exit(1)
