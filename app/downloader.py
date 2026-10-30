from __future__ import annotations

import hashlib
import html
from html.parser import HTMLParser
import http.cookiejar
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from importlib.util import find_spec
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse

import httpx
from PIL import Image, ImageOps

from . import bilibili_images

from .live_photo import (
    AlbumItem, LivePhotoError, douyin_album, render_live_album,
    xiaohongshu_album, xiaohongshu_note, xiaohongshu_video_urls,
)


BILIBILI_HOSTS = {
    "bilibili.com",
    "www.bilibili.com",
    "m.bilibili.com",
    "t.bilibili.com",
    "b23.tv",
}
X_HOSTS = {
    "x.com",
    "www.x.com",
    "mobile.x.com",
    "twitter.com",
    "www.twitter.com",
    "mobile.twitter.com",
}
XIAOHEIHE_HOSTS = {
    "api.xiaoheihe.cn",
    "www.xiaoheihe.cn",
    "xiaoheihe.cn",
}
DOUYIN_HOSTS = {
    "douyin.com",
    "www.douyin.com",
    "v.douyin.com",
    "iesdouyin.com",
    "www.iesdouyin.com",
}
XIAOHONGSHU_HOSTS = {
    "xiaohongshu.com",
    "www.xiaohongshu.com",
    "xhslink.com",
    "www.xhslink.com",
    "xhslink.cn",
    "www.xhslink.cn",
}
XIAOHONGSHU_SHORT_HOSTS = {
    "xhslink.com",
    "www.xhslink.com",
    "xhslink.cn",
    "www.xhslink.cn",
}
DOUYIN_MEDIA_HOST_SUFFIXES = (
    "douyin.com",
    "douyinpic.com",
    "douyinstatic.com",
    "douyinvod.com",
    "amemv.com",
    "bytecdn.cn",
    "byteimg.com",
    "snssdk.com",
)
XIAOHEIHE_DETAIL_PATH = "/bbs/app/link/tree"
logger = logging.getLogger(__name__)


def _log_douyin_failure(stage: str, url: str, error: str | Exception, job: str = "") -> None:
    """Keep diagnostic categories, never signed URLs, Cookie values or raw logs."""
    value = str(error).casefold()
    causes = []
    cause = error if isinstance(error, Exception) else None
    for _ in range(4):
        if cause is None:
            break
        causes.append(type(cause).__name__)
        if isinstance(cause, httpx.HTTPStatusError):
            value += f" http {cause.response.status_code}"
        cause = cause.__cause__
    kinds = []
    for label, markers in (
        ("ssl", ("ssl", "unexpected_eof", "eof occurred")),
        ("reset", ("connection reset", "connectionreset", "10054")),
        ("timeout", ("timed out", "timeout", "超时")),
        ("http403", ("http error 403", "http 403")),
        ("cookies", ("fresh cookies",)),
        ("missing_id", ("未能取得视频编号",)),
        ("invalid_detail", ("非 json", "未返回当前作品",)),
    ):
        if any(marker in value for marker in markers):
            kinds.append(label)
    logger.warning(
        "Douyin failure stage=%s job=%s host=%s kinds=%s exceptions=%s",
        stage, job, urlparse(url).hostname or "", ",".join(kinds) or "other",
        ",".join(causes) or "downloader",
    )


class DownloadError(RuntimeError):
    pass


class NoVideoError(DownloadError):
    """A supported social post that contains no downloadable video."""


class _DouyinAlbum(DownloadError):
    def __init__(self, detail: dict):
        super().__init__("该作品为抖音图文")
        self.detail = detail


class _XiaoheiheAlbum(DownloadError):
    def __init__(self, images: list[str], title: str = ""):
        super().__init__("该作品为小黑盒图文")
        self.images = images
        self.title = title


class DownloadedImages(list[Path]):
    """Ordered image paths with optional post metadata; legacy lists still work."""

    def __init__(self, images: list[Path], title: str = ""):
        super().__init__(images)
        self.title = html.unescape(title).strip() if isinstance(title, str) else ""


def _terminate_process_tree(process: subprocess.Popen[object]) -> None:
    """Terminate a timed-out downloader and its child processes.

    yt-dlp and the Douyin note worker can launch ffmpeg.  ``Popen.kill`` (and
    therefore ``subprocess.run(timeout=...)``) only targets the direct child
    on Windows, leaving ffmpeg behind.  ``taskkill /T`` is the Windows-native
    way to terminate the complete process tree; the direct kill is retained as
    a fallback if taskkill cannot find the process.
    """
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
    else:
        process.kill()


def _run_subprocess(
    arguments: Sequence[str], *, timeout: float | None = None, **kwargs: object
) -> subprocess.CompletedProcess:
    """Run a command and kill its complete process tree on timeout."""
    check = bool(kwargs.pop("check", False))
    popen_kwargs = dict(kwargs)
    if popen_kwargs.pop("capture_output", False):
        popen_kwargs.setdefault("stdout", subprocess.PIPE)
        popen_kwargs.setdefault("stderr", subprocess.PIPE)
    if os.name == "nt":
        popen_kwargs["creationflags"] = int(
            popen_kwargs.get("creationflags", 0)
        ) | int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))

    process = subprocess.Popen(list(arguments), **popen_kwargs)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_process_tree(process)
        stdout, stderr = process.communicate()
        raise

    result = subprocess.CompletedProcess(list(arguments), process.returncode, stdout, stderr)
    if check and result.returncode:
        raise subprocess.CalledProcessError(
            result.returncode,
            list(arguments),
            output=stdout,
            stderr=stderr,
        )
    return result


_TASK_COOKIE_FILENAMES = (
    "x-cookies.txt",
    "douyin-cookies.txt",
    "xiaohongshu-cookies.txt",
)


def _cleanup_task_cookies(job_dir: Path) -> None:
    """Remove task-local Cookie copies while leaving user exports untouched."""
    for filename in _TASK_COOKIE_FILENAMES:
        try:
            (job_dir / filename).unlink()
        except FileNotFoundError:
            continue
        except OSError:
            # The job directory is still removed by the service cleanup path;
            # do not mask the actual download result with a cleanup failure.
            continue


def _copy_cookie_to_job(path: Path, job_dir: Path, filename: str) -> Path:
    runtime_path = job_dir / filename
    if path.resolve() != runtime_path.resolve():
        shutil.copyfile(path, runtime_path)
    return runtime_path


def _parsed_http_url(raw_url: str):
    value = raw_url.strip()
    if not value or len(value) > 2048:
        raise ValueError("链接为空或过长")

    parsed = urlparse(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("仅支持 HTTP(S) 链接")
    if parsed.username or parsed.password:
        raise ValueError("链接中不能包含用户名或密码")
    return value, parsed, host


def normalize_bilibili_url(raw_url: str) -> str:
    value, _parsed, host = _parsed_http_url(raw_url)
    if host not in BILIBILI_HOSTS:
        raise ValueError("仅支持 bilibili.com 和 b23.tv 的 HTTP(S) 链接")
    return value


def normalize_x_url(raw_url: str) -> str:
    value, parsed, host = _parsed_http_url(raw_url)
    if host not in X_HOSTS:
        raise ValueError("仅支持 x.com 和 twitter.com 的推文链接")
    parts = [part for part in parsed.path.split("/") if part]
    has_status_id = any(
        part.lower() == "status" and index + 1 < len(parts) and parts[index + 1].isdigit()
        for index, part in enumerate(parts)
    )
    if not has_status_id:
        raise ValueError("X 链接必须是形如 https://x.com/用户/status/数字 的推文链接")
    return value


def normalize_douyin_url(raw_url: str) -> str:
    value, parsed, host = _parsed_http_url(raw_url)
    if host not in DOUYIN_HOSTS:
        raise ValueError("仅支持 douyin.com 和 v.douyin.com 的视频链接")
    parts = [part for part in parsed.path.split("/") if part]
    if host == "v.douyin.com":
        if len(parts) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]{3,128}", parts[0]):
            raise ValueError("抖音短链接格式不正确")
        return value
    has_item_id = any(
        part.casefold() in {"video", "note"}
        and index + 1 < len(parts)
        and parts[index + 1].isdigit()
        for index, part in enumerate(parts)
    )
    modal_id = parse_qs(parsed.query).get("modal_id", [""])[0]
    if not has_item_id and not modal_id.isdigit():
        raise ValueError("抖音链接必须指向具体视频或图文")
    return value


def normalize_xiaohongshu_url(raw_url: str) -> str:
    value, parsed, host = _parsed_http_url(raw_url)
    if host not in XIAOHONGSHU_HOSTS:
        raise ValueError("仅支持 xiaohongshu.com、xhslink.com 和 xhslink.cn 的笔记链接")
    parts = [part for part in parsed.path.split("/") if part]
    if host in XIAOHONGSHU_SHORT_HOSTS:
        if (
            len(parts) != 2
            or not re.fullmatch(r"[A-Za-z]{1,8}", parts[0])
            or not re.fullmatch(r"[A-Za-z0-9_-]{3,128}", parts[1])
        ):
            raise ValueError("小红书分享短链接格式不正确")
        return value

    note_id = ""
    if len(parts) == 2 and parts[0].casefold() == "explore":
        note_id = parts[1]
    elif (
        len(parts) == 3
        and parts[0].casefold() == "discovery"
        and parts[1].casefold() == "item"
    ):
        note_id = parts[2]
    if not re.fullmatch(r"[0-9a-fA-F]{16,32}", note_id):
        raise ValueError("小红书链接必须指向具体笔记")
    # yt-dlp 的小红书提取器只匹配 www 主机，统一为它可识别的形式。
    return parsed._replace(scheme="https", netloc="www.xiaohongshu.com").geturl()


def _recover_xiaohongshu_note_url(url: str, depth: int = 0) -> str | None:
    parsed = urlparse(url)
    if (parsed.hostname or "").casefold().rstrip(".") not in {"xiaohongshu.com", "www.xiaohongshu.com"}:
        return None
    try:
        return normalize_xiaohongshu_url(url)
    except ValueError:
        pass
    query = parse_qs(parsed.query)
    if depth < 2:
        for key in ("redirectPath", "redirect", "redirect_url"):
            for target in query.get(key, []):
                # parse_qs already decodes one level. Decode only when the
                # target itself is still URL-encoded; keep signed note query
                # values unchanged once a URL is available.
                if not target.startswith(("/", "http://", "https://")):
                    target = unquote(target)
                recovered = _recover_xiaohongshu_note_url(urljoin(url, target), depth + 1)
                if recovered:
                    return recovered
    note_id = next((query[key][0] for key in ("noteId", "note_id", "noteid") if query.get(key)), "")
    if not re.fullmatch(r"[0-9a-fA-F]{16,32}", note_id):
        return None
    preserved = {
        key: values for key, values in query.items()
        if key not in {"noteId", "note_id", "noteid", "redirectPath", "redirect", "redirect_url"}
    }
    suffix = f"?{urlencode(preserved, doseq=True)}" if preserved else ""
    return f"https://www.xiaohongshu.com/explore/{note_id}{suffix}"


def resolve_xiaohongshu_share_url(url: str, timeout_seconds: int = 20) -> str:
    normalized = normalize_xiaohongshu_url(url)
    if (urlparse(normalized).hostname or "").casefold() not in XIAOHONGSHU_SHORT_HOSTS:
        return normalized
    agents = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
    )
    received_response = False
    landing_path = ""
    for agent in agents:
        try:
            response = httpx.get(
                normalized,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Referer": "https://www.xiaohongshu.com/", "User-Agent": agent,
                },
                follow_redirects=True, timeout=max(5, min(timeout_seconds, 30)),
            )
        except httpx.HTTPError:
            continue
        received_response = True
        final_url = str(response.url)
        landing_path = urlparse(final_url).path
        history = response.history if isinstance(response.history, (list, tuple)) else []
        candidates = [final_url]
        for step in reversed(history):
            location = step.headers.get("location", "")
            if location:
                candidates.append(urljoin(str(step.url), location))
            candidates.append(str(step.url))
        for candidate in candidates:
            if (urlparse(candidate).hostname or "").casefold().rstrip(".") not in XIAOHONGSHU_HOSTS:
                raise DownloadError("小红书短链接跳转到了不受信任的地址")
        for candidate in candidates:
            recovered = _recover_xiaohongshu_note_url(candidate)
            if recovered:
                return recovered
    if not received_response:
        # Let yt-dlp retry a transient short-link transport failure.
        return normalized
    if landing_path.rstrip("/") == "/login":
        raise DownloadError("小红书分享链接跳转到登录页且未提供笔记地址，请检查完整分享链接或 Cookie")
    raise DownloadError("小红书分享链接未能解析到具体笔记")


def _is_douyin_note_url(url: str) -> bool:
    parts = [part.casefold() for part in urlparse(url).path.split("/") if part]
    return any(part == "note" for part in parts)


def resolve_douyin_share_url(url: str, timeout_seconds: int = 20) -> str:
    normalized = normalize_douyin_url(url)
    parsed = urlparse(normalized)
    if (parsed.hostname or "").casefold() != "v.douyin.com":
        return normalized
    deadline = time.monotonic() + max(0.1, min(timeout_seconds, 30))
    agents = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1",
    )
    for agent in agents:
        current = normalized
        for _ in range(6):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return normalized
            try:
                response = httpx.get(
                    current, headers={"User-Agent": agent},
                    follow_redirects=False, timeout=remaining,
                )
            except httpx.HTTPError as exc:
                _log_douyin_failure("short_link", current, exc)
                break
            location = response.headers.get("location", "")
            target = urljoin(str(response.url), location) if isinstance(location, str) and location else str(response.url)
            try:
                _value, target_parsed, target_host = _parsed_http_url(target)
            except ValueError as exc:
                raise DownloadError("抖音短链接返回了无效跳转地址") from exc
            if target_host not in DOUYIN_HOSTS:
                raise DownloadError("抖音短链接跳转到了不受信任的地址")
            try:
                resolved = normalize_douyin_url(target)
            except ValueError:
                resolved = None
            if resolved and target_host != "v.douyin.com":
                # Stop once the first redirect provides an item ID. Following
                # the share webpage adds a second TLS connection unnecessarily.
                match = re.fullmatch(r"/share/(video|note)/(\d+)/?", target_parsed.path)
                if match:
                    return target_parsed._replace(
                        scheme="https", netloc="www.douyin.com",
                        path=f"/{match.group(1)}/{match.group(2)}",
                    ).geturl()
                return resolved
            if isinstance(location, str) and location:
                current = target
                continue
            break
    # Preserve the ordinary downloader as a final route, without dropping its
    # SSL/transport failures out of the compatible metadata retry below.
    return normalized


def _is_douyin_transport_failure(logs: str) -> bool:
    return any(marker in logs.casefold() for marker in (
        "unexpected_eof_while_reading", "sslerror", "connectionreseterror",
        "connection reset", "remote end closed", "transporterror", "timed out",
    ))


def _xiaoheihe_link_id(parsed) -> str:
    host = (parsed.hostname or "").lower().rstrip(".")
    link_id = ""
    if host == "api.xiaoheihe.cn" and parsed.path.rstrip("/") == "/v3/bbs/app/api/web/share":
        link_id = parse_qs(parsed.query).get("link_id", [""])[0]
    elif host in {"xiaoheihe.cn", "www.xiaoheihe.cn"}:
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) == 4 and parts[:3] == ["app", "bbs", "link"]:
            link_id = parts[3]
    if not link_id or not (6 <= len(link_id) <= 64) or not all(
        character.isalnum() or character in "-_" for character in link_id
    ):
        raise ValueError("小黑盒链接必须是有效的视频分享链接")
    return link_id


def normalize_xiaoheihe_url(raw_url: str) -> str:
    value, parsed, host = _parsed_http_url(raw_url)
    if host not in XIAOHEIHE_HOSTS:
        raise ValueError("仅支持 xiaoheihe.cn 的分享链接")
    _xiaoheihe_link_id(parsed)
    return value


def normalize_video_url(raw_url: str) -> str:
    _value, _parsed, host = _parsed_http_url(raw_url)
    if host in BILIBILI_HOSTS:
        return normalize_bilibili_url(raw_url)
    if host in X_HOSTS:
        return normalize_x_url(raw_url)
    if host in XIAOHEIHE_HOSTS:
        return normalize_xiaoheihe_url(raw_url)
    if host in DOUYIN_HOSTS:
        return normalize_douyin_url(raw_url)
    if host in XIAOHONGSHU_HOSTS:
        return normalize_xiaohongshu_url(raw_url)
    raise ValueError("仅支持 Bilibili、b23.tv、X、Twitter、小黑盒、抖音和小红书的视频链接")


def video_platform(url: str) -> str:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    if host in BILIBILI_HOSTS:
        return "bilibili"
    if host in XIAOHEIHE_HOSTS:
        return "xiaoheihe"
    if host in DOUYIN_HOSTS:
        return "douyin"
    if host in XIAOHONGSHU_HOSTS:
        return "xiaohongshu"
    return "x"


def _xiaoheihe_hkey(path: str, timestamp: int, nonce: str) -> str:
    alphabet = "AB45STUVWZEFGJ6CH01D237IXYPQRKLMN89"

    def map_chars(value: object, end: int | None = None) -> str:
        chars = alphabet if end is None else alphabet[:end]
        return "".join(chars[ord(character) % len(chars)] for character in str(value))

    def double(value: int) -> int:
        return ((value << 1) ^ 27) & 255 if value & 128 else value << 1

    def mc(value: int) -> int:
        return double(value) ^ value

    def f4(value: int) -> int:
        return mc(double(value))

    def lh(value: int) -> int:
        return f4(mc(double(value)))

    def sg(value: int) -> int:
        return lh(value) ^ f4(value) ^ mc(value)

    normalized_path = "/" + "/".join(part for part in path.split("/") if part) + "/"
    sequences = [
        map_chars(timestamp + 1, -2),
        map_chars(normalized_path),
        map_chars(nonce),
    ]
    interleaved = "".join(
        sequence[index]
        for index in range(max(map(len, sequences)))
        for sequence in sequences
        if index < len(sequence)
    )[:20]
    digest = hashlib.md5(interleaved.encode("ascii")).hexdigest()
    tail = [ord(character) for character in digest[-6:]]
    tail[:4] = [
        sg(tail[0]) ^ lh(tail[1]) ^ f4(tail[2]) ^ mc(tail[3]),
        mc(tail[0]) ^ sg(tail[1]) ^ lh(tail[2]) ^ f4(tail[3]),
        f4(tail[0]) ^ mc(tail[1]) ^ sg(tail[2]) ^ lh(tail[3]),
        lh(tail[0]) ^ f4(tail[1]) ^ mc(tail[2]) ^ sg(tail[3]),
    ]
    checksum = str(sum(tail) % 100).zfill(2)
    return map_chars(digest[:5], -4) + checksum


def resolve_xiaoheihe_video_url(url: str, timeout_seconds: int = 20) -> str:
    parsed = urlparse(normalize_xiaoheihe_url(url))
    link_id = _xiaoheihe_link_id(parsed)
    timestamp = int(time.time())
    nonce = hashlib.md5(
        f"{timestamp}:{time.time_ns()}:{secrets.token_hex(16)}".encode("ascii")
    ).hexdigest().upper()
    params = {
        "link_id": link_id,
        "is_first": 1,
        "page": 1,
        "index": 1,
        "limit": 20,
        "owner_only": 0,
        "os_type": "web",
        "app": "heybox",
        "x_client_type": "weboutapp",
        "x_os_type": "Windows",
        "x_app": "heybox_website",
        "x_client_version": "999.999.999",
        "version": "999.0.4",
        "hkey": _xiaoheihe_hkey(XIAOHEIHE_DETAIL_PATH, timestamp, nonce),
        "_time": timestamp,
        "nonce": nonce,
    }
    cookies = _xiaoheihe_cookies()
    try:
        response = httpx.get(
            f"https://api.xiaoheihe.cn{XIAOHEIHE_DETAIL_PATH}",
            params=params,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://www.xiaoheihe.cn",
                "Referer": "https://www.xiaoheihe.cn/",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            },
            **({"cookies": cookies} if cookies is not None else {}),
            follow_redirects=False,
            timeout=max(5, min(timeout_seconds, 30)),
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise DownloadError("小黑盒服务暂时无法解析该视频，请稍后再试") from exc

    if isinstance(payload, dict) and payload.get("status") != "ok":
        # These are terminal session/verification states, not a transient
        # downloader failure. Do not retry a CAPTCHA as ordinary traffic.
        state = payload.get("status")
        if state == "show_captcha":
            raise DownloadError(
                "小黑盒要求验证码（show_captcha）。请在本机浏览器打开该帖子手动完成验证；"
                "如需登录，可通过管理后台配置 XIAOHEIHE_COOKIES_FILE 后重启视频 API。"
                "验证码未解除前，自动重试不会解决。"
            )
        if state in {"login", "relogin", "lack_token"}:
            raise DownloadError(
                "小黑盒要求登录或登录状态已失效。请在浏览器登录后导出 Netscape 格式 Cookie，"
                "配置 XIAOHEIHE_COOKIES_FILE 后重启视频 API。"
            )
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise DownloadError("小黑盒服务暂时无法解析该帖子，请稍后再试")

    result = payload.get("result")
    link = result.get("link") if isinstance(result, dict) else None
    media_url = link.get("video_url") if isinstance(link, dict) else None
    if not isinstance(media_url, str) or not media_url:
        images = _xiaoheihe_image_urls(link) if isinstance(link, dict) else []
        if images:
            raise _XiaoheiheAlbum(images, link.get("title", ""))
        raise NoVideoError("该小黑盒帖子中没有视频")

    _value, media_parsed, media_host = _parsed_http_url(media_url)
    if media_parsed.scheme != "https" or not (
        media_host == "max-c.com" or media_host.endswith(".max-c.com")
    ):
        raise DownloadError("小黑盒返回了不受信任的视频地址")
    return media_url


def _xiaoheihe_cookies() -> http.cookiejar.MozillaCookieJar | None:
    configured = os.getenv("XIAOHEIHE_COOKIES_FILE", "").strip()
    if not configured:
        return None
    cookies = http.cookiejar.MozillaCookieJar()
    try:
        cookies.load(str(Path(configured).expanduser()), ignore_discard=True)
    except (OSError, http.cookiejar.LoadError) as exc:
        raise DownloadError("XIAOHEIHE_COOKIES_FILE 无法读取，请配置有效的 Netscape 格式 Cookie 文件") from exc
    cookies.clear_expired_cookies()
    for cookie in list(cookies):
        host = cookie.domain.lstrip(".").lower()
        if host != "xiaoheihe.cn" and not host.endswith(".xiaoheihe.cn"):
            cookies.clear(cookie.domain, cookie.path, cookie.name)
    if not list(cookies):
        raise DownloadError("小黑盒 Cookie 中没有有效的 xiaoheihe.cn 登录数据，请重新从浏览器导出")
    return cookies


def _xiaoheihe_image_urls(link: dict) -> list[str]:
    """Read post images only; never recurse into authors or comments."""
    urls: list[str] = []
    def add_image(image):
        if isinstance(image, dict):
            image = next((image[key] for key in ("original_url", "original", "url", "src") if isinstance(image.get(key), str) and image[key]), None)
        if isinstance(image, str) and image.strip():
            urls.append(html.unescape(image.strip()))

    for field in ("imgs", "images"):
        values = link.get(field)
        if not isinstance(values, list):
            continue
        for image in values:
            add_image(image)
    if not urls:
        class PostImages(HTMLParser):
            def handle_starttag(self, tag, attrs):
                if tag.lower() == "img":
                    attributes = dict(attrs)
                    source = attributes.get("data-original") or attributes.get("data-src") or attributes.get("src")
                    if source:
                        add_image(source)
        parser = PostImages()
        def body_images(body):
            if isinstance(body, str):
                # The live web API sends text as a JSON-encoded block list,
                # not HTML. Decode it before looking for post image blocks.
                if body.lstrip().startswith(("[", "{")):
                    try:
                        blocks = json.loads(body)
                    except ValueError:
                        pass
                    else:
                        if isinstance(blocks, (list, dict)):
                            body_images(blocks)
                            return
                parser.feed(body)
            elif isinstance(body, list):
                for block in body:
                    body_images(block)
            elif isinstance(body, dict):
                if body.get("type") in {"img", "image"}:
                    add_image(body)
                elif body.get("type") in {"text", "html"}:
                    body_images(body.get("text"))
        for field in ("text", "description"):
            body_images(link.get(field))
    return list(dict.fromkeys(urls))


def _trusted_xiaoheihe_image_url(url: str) -> str:
    if url.startswith("//"):
        url = "https:" + url
    try:
        value, parsed, host = _parsed_http_url(url)
        if host != "max-c.com" and not host.endswith(".max-c.com"):
            raise ValueError("untrusted host")
        if parsed.port not in {None, 80, 443}:
            raise ValueError("unexpected port")
        return parsed._replace(scheme="https", netloc=host).geturl()
    except ValueError as exc:
        raise DownloadError("小黑盒返回了不受信任的图片地址") from exc


def _download_xiaoheihe_image(url: str, output: Path, timeout_seconds: float) -> None:
    url = _trusted_xiaoheihe_image_url(url)
    deadline = time.monotonic() + timeout_seconds
    for _ in range(6):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DownloadError("小黑盒图片下载超时")
        with httpx.stream(
            "GET", url, headers={"Referer": "https://www.xiaoheihe.cn/", "User-Agent": "Mozilla/5.0"},
            follow_redirects=False, timeout=min(remaining, 30),
        ) as response:
            if response.is_redirect:
                url = _trusted_xiaoheihe_image_url(urljoin(url, response.headers.get("location", "")))
                continue
            response.raise_for_status()
            with output.open("wb") as file:
                for chunk in response.iter_bytes():
                    if time.monotonic() >= deadline:
                        raise DownloadError("小黑盒图片下载超时")
                    file.write(chunk)
            return
    raise DownloadError("小黑盒图片重定向次数过多")


def _download_xiaoheihe_images(images: list[str], job_dir: Path, timeout_seconds: float) -> tuple[list[Path], str]:
    deadline = time.monotonic() + timeout_seconds
    def asset(url, output, _timeout):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DownloadError("小黑盒图文下载超时")
        _download_xiaoheihe_image(url, output, remaining)
    return _download_douyin_images(images, job_dir, timeout_seconds, download_asset=asset, label="小黑盒")


def get_yutto_command() -> list[str]:
    configured = os.getenv("YUTTO_COMMAND")
    if configured:
        if shutil.which(configured) is None and not Path(configured).is_file():
            raise DownloadError(f"找不到配置的 yutto：{configured}")
        return [configured]

    # 优先使用服务当前虚拟环境中的 yutto，不依赖 Scripts/bin 是否在 PATH。
    if find_spec("yutto") is not None:
        return [sys.executable, "-m", "yutto"]
    if shutil.which("yutto") is not None:
        return ["yutto"]
    raise DownloadError("当前 Python 环境中没有安装 yutto，请执行 pip install yutto")


def get_ytdlp_command() -> list[str]:
    configured = os.getenv("YTDLP_COMMAND")
    if configured:
        if shutil.which(configured) is None and not Path(configured).is_file():
            raise DownloadError(f"找不到配置的 yt-dlp：{configured}")
        return [configured]
    if find_spec("yt_dlp") is not None:
        return [sys.executable, "-m", "yt_dlp"]
    if shutil.which("yt-dlp") is not None:
        return ["yt-dlp"]
    raise DownloadError("当前 Python 环境中没有安装 yt-dlp，请执行 pip install yt-dlp")


def ensure_runtime(platform: str) -> list[str]:
    command = get_yutto_command() if platform == "bilibili" else get_ytdlp_command()
    if shutil.which("ffmpeg") is None:
        raise DownloadError("找不到 ffmpeg，请先安装并加入 PATH")
    return command


def _is_bilibili_transport_failure(logs: str) -> bool:
    return any(
        marker in logs.casefold()
        for marker in ("httptransporterror", "媒体大小探测失败", "cannot connect to host")
    )


def _x_cookie_arguments(job_dir: Path | None = None) -> list[str]:
    cookie_file = os.getenv("X_COOKIES_FILE", "").strip()
    if cookie_file:
        path = Path(cookie_file).expanduser()
        if not path.is_file():
            raise DownloadError(f"X_COOKIES_FILE 不存在：{path}")
        if job_dir is not None:
            path = _copy_cookie_to_job(path, job_dir, "x-cookies.txt")
        return ["--cookies", str(path)]
    browser = os.getenv("X_COOKIES_FROM_BROWSER", "").strip()
    if browser:
        return ["--cookies-from-browser", browser]
    return []


def _is_valid_netscape_cookie_file(path: Path) -> bool:
    """Check that a cookie export contains at least one usable Netscape row."""
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
                    continue
                fields = line.split("\t")
                if len(fields) >= 7 and fields[0].strip() and fields[5].strip():
                    return True
    except OSError:
        return False
    return False


def _douyin_cookie_candidates() -> tuple[list[Path], bool]:
    configured = os.getenv("DOUYIN_COOKIES_FILE", "").strip()
    if configured:
        return [Path(configured).expanduser()], True

    project_root = Path(__file__).resolve().parent.parent
    candidates = [project_root / "secrets" / "douyin-cookies.txt"]
    public_dir = os.getenv("PUBLIC", "").strip()
    if public_dir:
        candidates.append(Path(public_dir) / "www.douyin.com_cookies.txt")
    return candidates, False


def _find_douyin_cookie_file(*, required: bool = False) -> Path | None:
    candidates, explicitly_configured = _douyin_cookie_candidates()
    invalid_files: list[Path] = []
    for path in candidates:
        if not path.is_file():
            continue
        if _is_valid_netscape_cookie_file(path):
            return path
        invalid_files.append(path)

    if explicitly_configured:
        path = candidates[0]
        if not path.is_file():
            raise DownloadError(f"DOUYIN_COOKIES_FILE 不存在：{path}")
        raise DownloadError("抖音 Cookie 文件中没有有效 Cookie，请重新导出")
    if invalid_files:
        raise DownloadError("检测到抖音 Cookie 文件，但其中没有有效 Cookie，请重新导出")
    if required:
        raise DownloadError(
            "抖音图文需要 Cookie，请将导出文件保存为 "
            r"C:\Users\Public\www.douyin.com_cookies.txt"
        )
    return None


def _douyin_cookie_arguments(job_dir: Path | None = None) -> list[str]:
    path = _find_douyin_cookie_file()
    if path is not None:
        # yt-dlp 会在退出时回写 --cookies 文件。始终使用任务级副本，避免
        # 用户导出的原始 Cookie 被清空或改写。
        if job_dir is not None:
            path = _copy_cookie_to_job(path, job_dir, "douyin-cookies.txt")
        return ["--cookies", str(path)]
    browser = os.getenv("DOUYIN_COOKIES_FROM_BROWSER", "").strip()
    if browser:
        return ["--cookies-from-browser", browser]
    return []


def _request_douyin_detail(video_id: str, cookies: http.cookiejar.CookieJar, timeout_seconds: float):
    endpoint = "https://www.douyin.com/aweme/v1/web/aweme/detail/"
    deadline = time.monotonic() + max(0.1, min(timeout_seconds, 30))
    for attempt in range(3):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise httpx.ReadTimeout("Douyin detail retry budget exhausted")
        try:
            response = httpx.get(
                endpoint, params={"aweme_id": video_id}, cookies=cookies,
                headers={
                    "x-tt-argus": "1", "Referer": "https://www.douyin.com/",
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/130.0.0.0 Safari/537.36"
                    ),
                },
                timeout=min(remaining, 10),
            )
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            # Refusals and missing works need a useful error, not repeated traffic.
            if exc.response.status_code not in {502, 503, 504} or attempt == 2:
                raise
            _log_douyin_failure("detail_retry", endpoint, exc)
        except httpx.RequestError as exc:
            if attempt == 2:
                raise
            _log_douyin_failure("detail_retry", endpoint, exc)


def _resolve_douyin_detail(url: str, job_dir: Path, timeout_seconds: int) -> dict:
    """Read Douyin detail with the lowercase header its Argus gateway requires.

    yt-dlp/urllib title-cases custom headers, which still produces HTTP 403.
    Keep cookies scoped to the detail request; only the media URL goes to yt-dlp.
    """
    deadline = time.monotonic() + timeout_seconds
    normalized = resolve_douyin_share_url(url, timeout_seconds)
    parsed = urlparse(normalized)
    match = re.search(r"/(?:share/)?(?:video|note)/(\d+)(?:/|$)", parsed.path)
    video_id = match.group(1) if match else parse_qs(parsed.query).get("modal_id", [""])[0]
    if not video_id.isdigit():
        raise DownloadError("抖音兼容解析未能取得视频编号")

    cookie_path = job_dir / "douyin-cookies.txt"
    if not cookie_path.is_file():
        cookie_path = _find_douyin_cookie_file()
    cookies = http.cookiejar.MozillaCookieJar()
    if cookie_path is not None:
        try:
            cookies.load(str(cookie_path), ignore_discard=True)
        except (OSError, http.cookiejar.LoadError) as exc:
            raise DownloadError("抖音 Cookie 文件读取失败") from exc
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise httpx.ReadTimeout("Douyin detail retry budget exhausted")
        response = _request_douyin_detail(video_id, cookies, remaining)
        detail = response.json().get("aweme_detail")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {401, 403}:
            raise DownloadError(
                f"抖音详情接口拒绝访问（HTTP {exc.response.status_code}）。"
                "请在浏览器确认该作品能正常播放，并重新导出抖音 Cookie；"
                "如仍失败，请检查代理和平台风控状态。"
            ) from exc
        raise DownloadError(
            f"抖音详情接口返回 HTTP {exc.response.status_code}，请稍后重试"
        ) from exc
    except httpx.TimeoutException as exc:
        raise DownloadError("抖音详情请求超时，请检查网络或代理后重试") from exc
    except httpx.RequestError as exc:
        raise DownloadError("无法连接抖音详情接口，请检查网络或代理") from exc
    except (ValueError, AttributeError) as exc:
        raise DownloadError(
            "抖音详情接口返回空白或非 JSON 内容，可能需要浏览器验证。"
            "请确认作品能播放后重新导出抖音 Cookie。"
        ) from exc
    if not isinstance(detail, dict) or str(detail.get("aweme_id")) != video_id:
        raise DownloadError("抖音未返回当前作品的详情")
    return detail


def _resolve_douyin_video_media(url: str, job_dir: Path, timeout_seconds: int) -> str:
    return _resolve_douyin_video_media_candidates(url, job_dir, timeout_seconds)[0]


def _resolve_douyin_video_media_candidates(url: str, job_dir: Path, timeout_seconds: float) -> list[str]:
    detail = _resolve_douyin_detail(url, job_dir, timeout_seconds)
    if detail.get("images"):
        raise _DouyinAlbum(detail)
    video = detail.get("video")
    if not isinstance(video, dict):
        raise NoVideoError("该抖音作品中没有视频")
    # 优先 H.264，继续交给 yt-dlp 下载和封装，保持 QQ 播放兼容性。
    addresses = [video.get("play_addr_h264"), video.get("play_addr")]
    candidates: list[str] = []
    for address in addresses:
        if not isinstance(address, dict) or not isinstance(address.get("url_list"), list):
            continue
        for media_url in address["url_list"]:
            if not isinstance(media_url, str):
                continue
            try:
                trusted = _trusted_douyin_media_url(media_url)
            except (DownloadError, ValueError):
                continue
            if trusted not in candidates:
                candidates.append(trusted)
    if candidates:
        return candidates[:3]
    raise DownloadError("抖音未返回可信的视频下载地址")


def _douyin_media_arguments(arguments: list[str], media_url: str) -> list[str]:
    """Download the resolved public media without site session credentials."""
    clean_arguments: list[str] = []
    options = iter(arguments[:-1])
    for option in options:
        if option in {"--cookies", "--cookies-from-browser"}:
            next(options, None)
        else:
            clean_arguments.append(option)
    # Also prevent an external yt-dlp config from reintroducing credentials.
    return [
        *clean_arguments, "--ignore-config", "--output", "douyin.%(ext)s",
        "--socket-timeout", "10", "--retries", "1", "--extractor-retries", "1",
        "--no-continue", "--no-part", "--force-overwrites", media_url,
    ]


def _xiaohongshu_cookie_arguments(job_dir: Path | None = None) -> list[str]:
    cookie_file = os.getenv("XIAOHONGSHU_COOKIES_FILE", "").strip()
    if cookie_file:
        path = Path(cookie_file).expanduser()
        if not path.is_file():
            raise DownloadError(f"XIAOHONGSHU_COOKIES_FILE 不存在：{path}")
        if job_dir is not None:
            path = _copy_cookie_to_job(path, job_dir, "xiaohongshu-cookies.txt")
        return ["--cookies", str(path)]
    browser = os.getenv("XIAOHONGSHU_COOKIES_FROM_BROWSER", "").strip()
    if browser:
        return ["--cookies-from-browser", browser]
    return []


def _douyin_cookie_file() -> Path:
    path = _find_douyin_cookie_file(required=True)
    assert path is not None
    return path


def _douyin_f2_python() -> Path:
    configured = os.getenv("DOUYIN_F2_PYTHON", "").strip()
    if configured:
        candidate = Path(configured).expanduser()
    else:
        project_root = Path(__file__).resolve().parent.parent
        computer_name = os.getenv("COMPUTERNAME", "").strip()
        candidate = (
            project_root
            / ".venvs"
            / f"f2-{computer_name}"
            / "Scripts"
            / "python.exe"
        )
    if not candidate.is_file():
        raise DownloadError("抖音图文组件尚未安装，请在机器人管理器中更新 Python 依赖")
    return candidate


def _trusted_douyin_media_url(url: str) -> str:
    value, parsed, host = _parsed_http_url(url)
    if parsed.scheme != "https" or not any(
        host == suffix or host.endswith(f".{suffix}")
        for suffix in DOUYIN_MEDIA_HOST_SUFFIXES
    ):
        raise DownloadError("抖音返回了不受信任的素材地址")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return value
    raise DownloadError("抖音返回了不受信任的素材地址")


def _download_douyin_asset(url: str, output: Path, timeout_seconds: float) -> None:
    trusted_url = _trusted_douyin_media_url(url)
    deadline = time.monotonic() + timeout_seconds
    with httpx.stream(
        "GET",
        trusted_url,
        headers={
            "Referer": "https://www.douyin.com/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        },
        follow_redirects=True,
        timeout=max(0.1, min(timeout_seconds, 60)),
    ) as response:
        response.raise_for_status()
        _trusted_douyin_media_url(str(response.url))
        for redirect in response.history:
            _trusted_douyin_media_url(str(redirect.url))
        with output.open("wb") as file:
            for chunk in response.iter_bytes():
                if time.monotonic() >= deadline:
                    raise DownloadError("抖音素材下载超时")
                file.write(chunk)


def _prepare_douyin_image(source: Path) -> Path:
    with Image.open(source) as image:
        image.load()
        suffix = {"JPEG": ".jpg", "PNG": ".png", "GIF": ".gif"}.get(image.format)
        output = source.with_suffix(suffix or ".jpg")
        if suffix is None:
            ImageOps.exif_transpose(image).convert("RGB").save(output, "JPEG", quality=95)
    if suffix is not None:
        source.replace(output)
    else:
        source.unlink()
    return output


def _trusted_xiaohongshu_asset_url(url: str) -> str:
    if url.startswith("//"):
        url = "https:" + url
    value, parsed, host = _parsed_http_url(url)
    if host != "xhscdn.com" and not host.endswith(".xhscdn.com"):
        raise DownloadError("小红书返回了不受信任的素材地址")
    if parsed.scheme == "http":
        value = parsed._replace(scheme="https").geturl()
    elif parsed.scheme != "https":
        raise DownloadError("小红书素材地址必须使用 HTTPS")
    return value


def _download_xiaohongshu_asset(url: str, output: Path, timeout_seconds: float) -> None:
    url = _trusted_xiaohongshu_asset_url(url)
    deadline = time.monotonic() + timeout_seconds
    # Validate every redirect before connecting; site credentials never enter
    # these public CDN requests.
    for _ in range(6):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DownloadError("小红书素材下载超时")
        with httpx.stream(
            "GET", url, headers={"Referer": "https://www.xiaohongshu.com/", "User-Agent": "Mozilla/5.0"},
            follow_redirects=False, timeout=max(0.1, min(remaining, 60)),
        ) as response:
            if response.is_redirect:
                url = _trusted_xiaohongshu_asset_url(urljoin(url, response.headers.get("location", "")))
                continue
            response.raise_for_status()
            with output.open("wb") as file:
                for chunk in response.iter_bytes():
                    if time.monotonic() >= deadline:
                        raise DownloadError("小红书素材下载超时")
                    file.write(chunk)
            return
    raise DownloadError("小红书素材重定向次数过多")


def _album_result(
    items: list[AlbumItem], platform: str, job_dir: Path, timeout_seconds: int
) -> tuple[Path | list[Path], str]:
    asset = _download_douyin_asset if platform == "douyin" else _download_xiaohongshu_asset
    if any(item.is_live for item in items):
        if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
            raise DownloadError("实况转换需要 ffmpeg 和 ffprobe，请检查 PATH")
        try:
            live_asset = _download_douyin_live_asset if platform == "douyin" else asset
            result = render_live_album(items, job_dir, timeout_seconds, live_asset, run=_run_subprocess)
        except LivePhotoError as exc:
            raise DownloadError(str(exc)) from exc
        return result, f"已将 {len(items)} 张图文素材按顺序合成为实况 MP4"
    urls = [item.image_urls[0] for item in items if item.image_urls]
    return _download_douyin_images(
        urls, job_dir, timeout_seconds, download_asset=asset,
        label="抖音" if platform == "douyin" else "小红书",
    )


def _download_douyin_live_asset(url: str, output: Path, timeout_seconds: float) -> None:
    if output.suffix != ".media":
        return _download_douyin_asset(url, output, timeout_seconds)
    # Douyin playback gateways may redirect to other CDN domains. Use the
    # ordinary public-video downloader, with a trusted starting URL and no
    # site credentials or external yt-dlp configuration.
    trusted = _trusted_douyin_media_url(url)
    process = _run_subprocess(
        [*get_ytdlp_command(), "--ignore-config", "--no-playlist", "--no-progress",
         "--no-color", "--no-part", "--retries", "3", "--socket-timeout",
         str(max(1, min(timeout_seconds, 30))), "--referer", "https://www.douyin.com/",
         "--format", "best", "--output", output.name, trusted],
        cwd=output.parent, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout_seconds, check=False,
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
    )
    if process.returncode:
        raise DownloadError("抖音实况视频下载失败")


def _resolve_xiaohongshu_note(url: str, job_dir: Path, timeout_seconds: int) -> dict:
    from yt_dlp import YoutubeDL, parse_options
    from yt_dlp.extractor.xiaohongshu import XiaoHongShuIE
    from yt_dlp.utils import js_to_json

    normalized = normalize_xiaohongshu_url(url)
    note_id = urlparse(normalized).path.rstrip("/").split("/")[-1]
    if not re.fullmatch(r"[0-9a-fA-F]{16,32}", note_id):
        raise DownloadError("小红书图文解析未能取得笔记编号")

    class QuietLogger:
        def debug(self, message):
            pass
        warning = error = debug

    options = parse_options(["--ignore-config", *_xiaohongshu_cookie_arguments(job_dir)]).ydl_opts
    options.update({"logger": QuietLogger(), "quiet": True, "no_warnings": True,
                    "socket_timeout": max(1, min(timeout_seconds, 30))})
    desktop = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36"
    mobile = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1"
    agents = (mobile, desktop) if parse_qs(urlparse(normalized).query).get("app_platform") else (desktop, mobile)
    last_error = None
    for agent in agents:
        try:
            with YoutubeDL({**options, "http_headers": {"User-Agent": agent, "Referer": "https://www.xiaohongshu.com/"}}) as ydl:
                extractor = XiaoHongShuIE(ydl)
                webpage = extractor._download_webpage(normalized, note_id)
                state = extractor._search_json(
                    r"window\.__INITIAL_STATE__\s*=", webpage, "initial state", note_id,
                    transform_source=js_to_json,
                )
            return xiaohongshu_note(state, note_id)
        except Exception as exc:
            last_error = exc
    if isinstance(last_error, LivePhotoError):
        raise DownloadError(str(last_error)) from last_error
    raise DownloadError("小红书图文详情获取失败，请检查完整分享链接、Cookie 或平台访问状态") from last_error


def _download_xiaohongshu_album(url: str, job_dir: Path, timeout_seconds: int):
    note = _resolve_xiaohongshu_note(url, job_dir, timeout_seconds)
    if note.get("type") == "video":
        urls = xiaohongshu_video_urls(note)
        if not urls:
            raise DownloadError("小红书视频笔记未返回可用播放地址，请检查作品访问状态")
        result, _ = _album_result([AlbumItem(video_urls=urls, is_live=True)], "xiaohongshu", job_dir, timeout_seconds)
        return result, "已通过备用页面恢复小红书视频并转换为 MP4"
    return _album_result(xiaohongshu_album(note), "xiaohongshu", job_dir, timeout_seconds)


def _download_douyin_note_images(
    url: str, job_dir: Path, timeout_seconds: int
) -> tuple[Path | list[Path], str]:
    """Send static images or preserve live motion in a standard MP4."""
    cookie_path = _copy_cookie_to_job(
        _douyin_cookie_file(), job_dir, "douyin-cookies.txt"
    )
    try:
        return _download_douyin_note_images_impl(
            url, job_dir, timeout_seconds, cookie_path
        )
    finally:
        _cleanup_task_cookies(job_dir)


def _download_douyin_note_images_impl(
    url: str, job_dir: Path, timeout_seconds: int, cookie_path: Path
) -> tuple[Path | list[Path], str]:
    try:
        detail = _resolve_douyin_detail(url, job_dir, timeout_seconds)
    except DownloadError as exc:
        direct_error = str(exc)
    else:
        # Share the compatible detail request already used for videos.
        return _album_result(douyin_album(detail), "douyin", job_dir, timeout_seconds)

    metadata_path = job_dir / "note.json"
    worker = Path(__file__).resolve().parent.parent / "tools" / "douyin_note_metadata.py"
    process = _run_subprocess(
        [str(_douyin_f2_python()), str(worker), "--url", url,
         "--cookies", str(cookie_path), "--output", str(metadata_path)],
        cwd=job_dir, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout_seconds, check=False,
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
    )
    logs = (process.stdout + "\n" + process.stderr).strip()
    if process.returncode != 0 or not metadata_path.is_file():
        # f2 may print signed URLs even without a traceback. Do not expose
        # those URLs and session parameters in the API error or bot log.
        if "403" in logs:
            raise DownloadError("抖音图文详情接口拒绝访问（HTTP 403），请确认作品可访问及 Cookie 有效")
        raise DownloadError(f"抖音图文信息获取失败：{direct_error}")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        image_urls = metadata.get("images", [])
    except (OSError, ValueError, AttributeError) as exc:
        raise DownloadError("抖音图文信息格式不正确") from exc
    if "image_details" in metadata:
        return _album_result(douyin_album({"images": metadata["image_details"]}), "douyin", job_dir, timeout_seconds)
    return _download_douyin_images(image_urls, job_dir, timeout_seconds)


def _retryable_image_error(error: Exception) -> bool:
    """Retry interrupted GETs, never permissions, validation or local IO errors."""
    cause = error
    for _ in range(4):
        if isinstance(cause, httpx.HTTPStatusError):
            return cause.response.status_code in {408, 429, 500, 502, 503, 504}
        if isinstance(cause, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)):
            return True
        cause = cause.__cause__
        if cause is None:
            break
    return False


def _image_failure_summary(error: Exception) -> str:
    # Do not expose full exceptions: CDN URLs can contain signed credentials.
    cause = error
    kinds = []
    for _ in range(4):
        kinds.append(type(cause).__name__)
        if isinstance(cause, httpx.HTTPStatusError):
            kinds.append(f"HTTP {cause.response.status_code}")
        cause = cause.__cause__
        if cause is None:
            break
    return ",".join(kinds)


def _download_douyin_images(
    image_urls: list[str], job_dir: Path, timeout_seconds: int, *, download_asset=None, label="抖音"
) -> tuple[list[Path], str]:
    if not isinstance(image_urls, list) or not image_urls:
        raise NoVideoError(f"该{label}作品中没有可发送的图片")
    images: list[Path] = []
    download_asset = download_asset or _download_douyin_asset
    asset_logs: list[str] = []
    deadline = time.monotonic() + timeout_seconds
    for index, image_url in enumerate(image_urls):
        if not isinstance(image_url, str):
            continue
        source = job_dir / f"image-{index:03d}.img"
        for attempt in range(3):
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DownloadError("图文下载总时限已到")
                download_asset(image_url, source, remaining)
                images.append(_prepare_douyin_image(source))
                break
            except (DownloadError, httpx.HTTPError, OSError, ValueError) as exc:
                delay = 0.5 * (2 ** attempt)
                if attempt < 2 and _retryable_image_error(exc) and deadline - time.monotonic() > delay:
                    # Restart the image from byte zero after a partial stream.
                    source.unlink(missing_ok=True)
                    time.sleep(delay)
                    continue
                summary = _image_failure_summary(exc)
                asset_logs.append(f"第 {index + 1} 张图片失败（{summary}）")
                logger.warning("Album image failed: platform=%s index=%d attempts=%d host=%s error=%s",
                               label, index + 1, attempt + 1, urlparse(image_url).hostname or "unknown", summary)
                break
    if not images:
        raise DownloadError(f"{label}图文图片均下载失败：{'；'.join(asset_logs[:3])}")
    return images, "\n".join([f"已下载 {len(images)} 张{label}图片", *asset_logs])[-4000:]


def download_to_mp4(url: str, job_dir: Path, timeout_seconds: int) -> tuple[Path | list[Path], str]:
    url = normalize_video_url(url)
    platform = video_platform(url)
    source_url = url
    if platform == "bilibili":
        deadline = time.monotonic() + timeout_seconds
        try:
            source_url = bilibili_images.resolve_share_url(url, timeout_seconds)
            if bilibili_images.post_id(source_url):
                post = bilibili_images.fetch_post(source_url, max(0.1, deadline - time.monotonic()))
                if post.video_url:
                    source_url = post.video_url
                else:
                    if not post.images:
                        raise NoVideoError("该 B 站图文中没有可发送的图片")
                    job_dir.mkdir(parents=True, exist_ok=False)
                    def asset(image_url, output, _timeout):
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise DownloadError("B 站图文下载超时")
                        try:
                            bilibili_images.download_image(image_url, output, remaining)
                        except bilibili_images.BilibiliImageError as exc:
                            raise DownloadError(str(exc)) from exc
                    images, logs = _download_douyin_images(list(post.images), job_dir, timeout_seconds, download_asset=asset, label="B 站")
                    return DownloadedImages(images, post.title), logs
        except bilibili_images.BilibiliImageError as exc:
            raise DownloadError(str(exc)) from exc
    if platform == "xiaoheihe":
        deadline = time.monotonic() + timeout_seconds
        try:
            source_url = resolve_xiaoheihe_video_url(url, timeout_seconds)
        except _XiaoheiheAlbum as album:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DownloadError("小黑盒图文下载超时")
            job_dir.mkdir(parents=True, exist_ok=False)
            images, logs = _download_xiaoheihe_images(album.images, job_dir, remaining)
            return DownloadedImages(images, album.title), logs
    if platform == "douyin":
        source_url = resolve_douyin_share_url(url, timeout_seconds)
    if platform == "xiaohongshu":
        source_url = resolve_xiaohongshu_share_url(url, timeout_seconds)
    job_dir.mkdir(parents=True, exist_ok=False)

    if platform == "douyin" and _is_douyin_note_url(source_url):
        return _download_douyin_note_images(source_url, job_dir, timeout_seconds)

    command = ensure_runtime(platform)

    if platform == "bilibili":
        bilibili_proxy = os.getenv("BILIBILI_PROXY", "").strip()
        arguments = [
            *command,
            "download",
            "--output-format",
            "mp4",
            "--no-progress",
            "--no-color",
            *(["--proxy", bilibili_proxy] if bilibili_proxy else []),
            source_url,
        ]
    elif platform == "x":
        base_arguments = [
            *command,
            "--no-playlist",
            "--no-progress",
            "--no-color",
            "--retries",
            "3",
            "--extractor-retries",
            "3",
            "--socket-timeout",
            "30",
            "--format",
            "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
            "--merge-output-format",
            "mp4",
            "--remux-video",
            "mp4",
            "--output",
            "%(id)s.%(ext)s",
            *_x_cookie_arguments(job_dir),
            url,
        ]
        # X 的 syndication 接口偶尔缺少媒体数据。先使用 yt-dlp 的默认
        # 提取链，只有失败时才把 syndication 当作兼容性兜底。
        fallback_arguments = [
            *base_arguments[:-1],
            "--extractor-args",
            "twitter:api=syndication",
            base_arguments[-1],
        ]
        attempts = [base_arguments, fallback_arguments]

    elif platform == "douyin":
        arguments = [
            *command,
            "--no-playlist",
            "--no-progress",
            "--no-color",
            "--retries",
            "3",
            "--extractor-retries",
            "3",
            "--socket-timeout",
            "30",
            "--referer",
            "https://www.douyin.com/",
            "--format",
            "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
            "--merge-output-format",
            "mp4",
            "--remux-video",
            "mp4",
            "--output",
            "%(id)s.%(ext)s",
            *_douyin_cookie_arguments(job_dir),
            source_url,
        ]
        attempts = [arguments]

    elif platform == "xiaohongshu":
        arguments = [
            *command,
            "--no-playlist",
            "--no-progress",
            "--no-color",
            "--retries",
            "3",
            "--extractor-retries",
            "3",
            "--socket-timeout",
            "30",
            "--referer",
            "https://www.xiaohongshu.com/",
            "--format",
            "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
            "--merge-output-format",
            "mp4",
            "--remux-video",
            "mp4",
            "--output",
            "%(id)s.%(ext)s",
            *_xiaohongshu_cookie_arguments(job_dir),
            source_url,
        ]
        attempts = [arguments]

    else:
        arguments = [
            *command,
            "--no-playlist",
            "--no-progress",
            "--no-color",
            "--retries",
            "3",
            "--socket-timeout",
            "30",
            "--referer",
            "https://www.xiaoheihe.cn/",
            "--merge-output-format",
            "mp4",
            "--remux-video",
            "mp4",
            "--output",
            "%(id)s.%(ext)s",
            source_url,
        ]
        attempts = [arguments]

    if platform == "bilibili":
        attempts = [arguments]

    try:
        attempt_logs: list[str] = []
        process = None
        douyin_fallback_attempted = False
        douyin_fallback_error: str | None = None
        bilibili_fallback_attempted = False
        download_deadline = time.monotonic() + timeout_seconds
        for arguments in attempts:
            attempt_timeout: float = timeout_seconds
            if platform in {"bilibili", "douyin"}:
                # Both routes share the job's original timeout budget.
                attempt_timeout = download_deadline - time.monotonic()
                if attempt_timeout <= 0:
                    label = "B 站" if platform == "bilibili" else "抖音"
                    raise DownloadError(f"{label}下载超时，请检查网络或代理后重试")
            # 不启用 shell，URL 永远只作为一个独立参数传给下载器。
            process = _run_subprocess(
                arguments,
                cwd=job_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env={
                    **os.environ,
                    # Windows 子进程默认可能使用 GBK。B 站标题含 emoji 时，
                    # yutto 会在输出日志阶段触发 UnicodeEncodeError。
                    "PYTHONIOENCODING": "utf-8",
                    "PYTHONUTF8": "1",
                },
                timeout=attempt_timeout,
                check=False,
            )
            logs = (process.stdout + "\n" + process.stderr).strip()
            attempt_logs.append(logs)
            if process.returncode == 0:
                break
            if platform == "douyin":
                stage = "media_download" if douyin_fallback_attempted else "extractor"
                _log_douyin_failure(stage, arguments[-1], logs, job_dir.name)
            if (
                platform == "bilibili"
                and not bilibili_fallback_attempted
                and bilibili_proxy in {"", "auto"}
                and _is_bilibili_transport_failure(logs)
            ):
                bilibili_fallback_attempted = True
                attempt_logs.append("B 站媒体连接失败，正在禁用代理并直连重试一次")
                # CLI options override yutto's config and environment proxy.
                attempts.append([*arguments[:-1], "--proxy", "no", source_url])
            if (
                platform == "douyin"
                and not douyin_fallback_attempted
                and (any(marker in logs.casefold() for marker in ("fresh cookies", "http error 403"))
                     or _is_douyin_transport_failure(logs))
            ):
                douyin_fallback_attempted = True
                try:
                    remaining = download_deadline - time.monotonic()
                    if remaining <= 0:
                        raise DownloadError("抖音兼容解析超时")
                    media_urls = _resolve_douyin_video_media_candidates(source_url, job_dir, remaining)
                    # CDN URLs can make the generic extractor use a very long
                    # query string as the ID, exceeding Windows filename limits.
                    attempts.extend(_douyin_media_arguments(arguments, media_url) for media_url in media_urls)
                except _DouyinAlbum as exc:
                    remaining = download_deadline - time.monotonic()
                    if remaining <= 0:
                        raise DownloadError("抖音实况转换超时")
                    return _album_result(douyin_album(exc.detail), "douyin", job_dir, remaining)
                except DownloadError as exc:
                    douyin_fallback_error = str(exc)
                    _log_douyin_failure("compatible_detail", source_url, exc, job_dir.name)
                    attempt_logs.append(str(exc))

        if process is None or process.returncode != 0:
            downloader = "yutto" if platform == "bilibili" else "yt-dlp"
            combined_logs = "\n\n".join(log for log in attempt_logs if log)
            if platform == "douyin":
                note_match = re.search(
                    r"https?://(?:www\.)?douyin\.com/note/\d+[^\s]*",
                    combined_logs,
                )
                if note_match:
                    return _download_douyin_note_images(
                        note_match.group(0), job_dir, timeout_seconds
                    )
            if platform == "xiaohongshu" and any(
                marker in combined_logs.casefold()
                for marker in (
                    "no video formats found",
                    "there are no formats",
                    "does not contain a video",
                )
            ):
                return _download_xiaohongshu_album(source_url, job_dir, timeout_seconds)
            if platform == "douyin" and _is_douyin_transport_failure(combined_logs):
                if douyin_fallback_error:
                    raise DownloadError(f"抖音原下载连接中断；兼容详情解析失败：{douyin_fallback_error}")
                raise DownloadError("抖音连接中断（SSL、连接重置或超时），兼容重试未成功，请稍后再试或检查网络")
            if platform == "bilibili" and _is_bilibili_transport_failure(logs):
                retry_note = "；自动直连重试也未成功" if bilibili_fallback_attempted else ""
                raise DownloadError(
                    f"B 站媒体连接失败{retry_note}，请检查网络、代理或稍后重试。"
                    "日志中的未登录提示只说明高清和字幕受限。\n\n"
                    + combined_logs[-3000:]
                )
            raise DownloadError(combined_logs[-4000:] or f"{downloader} 下载失败")

        candidates = sorted(job_dir.rglob("*.mp4"), key=lambda path: path.stat().st_size, reverse=True)
        if not candidates:
            raise DownloadError("下载器执行成功，但未找到 MP4 文件")
        return candidates[0], logs[-4000:]
    finally:
        _cleanup_task_cookies(job_dir)
