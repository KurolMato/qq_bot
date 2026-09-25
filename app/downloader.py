from __future__ import annotations

import hashlib
import http.cookiejar
import ipaddress
import json
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
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from PIL import Image, ImageFilter, ImageOps


BILIBILI_HOSTS = {
    "bilibili.com",
    "www.bilibili.com",
    "m.bilibili.com",
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


class DownloadError(RuntimeError):
    pass


class NoVideoError(DownloadError):
    """A supported social post that contains no downloadable video."""


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


def resolve_xiaohongshu_share_url(url: str, timeout_seconds: int = 20) -> str:
    normalized = normalize_xiaohongshu_url(url)
    parsed = urlparse(normalized)
    if (parsed.hostname or "").casefold() not in XIAOHONGSHU_SHORT_HOSTS:
        return normalized
    try:
        response = httpx.get(
            normalized,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": "https://www.xiaohongshu.com/",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
            },
            follow_redirects=True,
            timeout=max(5, min(timeout_seconds, 30)),
        )
        response.raise_for_status()
        final_url = str(response.url)
    except httpx.HTTPError:
        # yt-dlp 的通用提取器仍可能自行完成短链跳转。
        return normalized

    try:
        return normalize_xiaohongshu_url(final_url)
    except ValueError as exc:
        final = urlparse(final_url)
        final_host = (final.hostname or "").casefold().rstrip(".")
        if final_host not in {"xiaohongshu.com", "www.xiaohongshu.com"}:
            raise DownloadError("小红书短链接跳转到了不受信任的地址") from exc
        query = parse_qs(final.query)
        note_id = next(
            (
                query.get(key, [""])[0]
                for key in ("noteId", "note_id", "noteid")
                if query.get(key, [""])[0]
            ),
            "",
        )
        if not re.fullmatch(r"[0-9a-fA-F]{16,32}", note_id):
            raise DownloadError("小红书分享链接未能解析到具体笔记") from exc
        preserved = {
            key: query[key][0]
            for key in ("xsec_token", "xsec_source", "type")
            if query.get(key, [""])[0]
        }
        suffix = f"?{urlencode(preserved)}" if preserved else ""
        return f"https://www.xiaohongshu.com/explore/{note_id}{suffix}"


def _is_douyin_note_url(url: str) -> bool:
    parts = [part.casefold() for part in urlparse(url).path.split("/") if part]
    return any(part == "note" for part in parts)


def resolve_douyin_share_url(url: str, timeout_seconds: int = 20) -> str:
    normalized = normalize_douyin_url(url)
    parsed = urlparse(normalized)
    if (parsed.hostname or "").casefold() != "v.douyin.com":
        return normalized
    try:
        response = httpx.get(
            normalized,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            follow_redirects=True,
            timeout=max(5, min(timeout_seconds, 30)),
        )
        response.raise_for_status()
        final_url = str(response.url)
        # 抖音短链只应落到抖音自己的域名；拒绝把外部重定向交给下载器。
        return normalize_douyin_url(final_url)
    except (httpx.HTTPError, ValueError):
        # 短链解析偶尔会被风控；普通视频仍可交由 yt-dlp 自己处理。
        return normalized


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
            follow_redirects=False,
            timeout=max(5, min(timeout_seconds, 30)),
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise DownloadError("小黑盒服务暂时无法解析该视频，请稍后再试") from exc

    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise DownloadError("小黑盒服务暂时无法解析该帖子，请稍后再试")

    link = payload.get("result", {}).get("link", {})
    media_url = link.get("video_url") if isinstance(link, dict) else None
    if not isinstance(media_url, str) or not media_url:
        raise NoVideoError("该小黑盒帖子中没有视频")

    _value, media_parsed, media_host = _parsed_http_url(media_url)
    if media_parsed.scheme != "https" or not (
        media_host == "max-c.com" or media_host.endswith(".max-c.com")
    ):
        raise DownloadError("小黑盒返回了不受信任的视频地址")
    return media_url


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
            f"抖音图文需要 Cookie，请将导出文件保存为 {Path.home() / 'www' / 'douyin.com_cookies.txt'}"
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


def _resolve_douyin_video_media(url: str, job_dir: Path, timeout_seconds: int) -> str:
    """Read Douyin detail with the lowercase header its Argus gateway requires.

    yt-dlp/urllib title-cases custom headers, which still produces HTTP 403.
    Keep cookies scoped to the detail request; only the media URL goes to yt-dlp.
    """
    normalized = resolve_douyin_share_url(url, timeout_seconds)
    parsed = urlparse(normalized)
    match = re.search(r"/(?:share/)?video/(\d+)(?:/|$)", parsed.path)
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
        response = httpx.get(
            "https://www.douyin.com/aweme/v1/web/aweme/detail/",
            params={"aweme_id": video_id},
            cookies=cookies,
            headers={
                "x-tt-argus": "1",
                "Referer": "https://www.douyin.com/",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/130.0.0.0 Safari/537.36"
                ),
            },
            timeout=max(5, min(timeout_seconds, 30)),
        )
        response.raise_for_status()
        detail = response.json().get("aweme_detail")
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        raise DownloadError("抖音兼容详情请求失败，请检查平台访问状态") from exc
    if not isinstance(detail, dict) or str(detail.get("aweme_id")) != video_id:
        raise DownloadError("抖音未返回当前作品的详情")
    video = detail.get("video")
    if not isinstance(video, dict):
        raise NoVideoError("该抖音作品中没有视频")
    # 优先 H.264，继续交给 yt-dlp 下载和封装，保持 QQ 播放兼容性。
    addresses = [video.get("play_addr_h264"), video.get("play_addr")]
    for address in addresses:
        if not isinstance(address, dict) or not isinstance(address.get("url_list"), list):
            continue
        for media_url in address["url_list"]:
            if not isinstance(media_url, str):
                continue
            try:
                return _trusted_douyin_media_url(media_url)
            except (DownloadError, ValueError):
                continue
    raise DownloadError("抖音未返回可信的视频下载地址")


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


def _download_douyin_asset(url: str, output: Path, timeout_seconds: int) -> None:
    trusted_url = _trusted_douyin_media_url(url)
    with httpx.stream(
        "GET",
        trusted_url,
        headers={
            "Referer": "https://www.douyin.com/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        },
        follow_redirects=True,
        timeout=max(10, min(timeout_seconds, 60)),
    ) as response:
        response.raise_for_status()
        _trusted_douyin_media_url(str(response.url))
        for redirect in response.history:
            _trusted_douyin_media_url(str(redirect.url))
        with output.open("wb") as file:
            for chunk in response.iter_bytes():
                file.write(chunk)


def _render_douyin_slide(source: Path, output: Path) -> None:
    canvas_size = (720, 1280)
    with Image.open(source) as original:
        image = ImageOps.exif_transpose(original).convert("RGB")
        background = ImageOps.fit(image, canvas_size, method=Image.Resampling.LANCZOS)
        background = background.filter(ImageFilter.GaussianBlur(28))
        background = Image.blend(background, Image.new("RGB", canvas_size, "black"), 0.18)
        foreground = ImageOps.contain(image, (680, 1240), method=Image.Resampling.LANCZOS)
        x = (canvas_size[0] - foreground.width) // 2
        y = (canvas_size[1] - foreground.height) // 2
        background.paste(foreground, (x, y))
        background.save(output, "JPEG", quality=92, optimize=True)


def _download_douyin_note_to_mp4(
    url: str, job_dir: Path, timeout_seconds: int
) -> tuple[Path, str]:
    """Convert a Douyin image note and remove its task Cookie copy."""
    cookie_path = _copy_cookie_to_job(
        _douyin_cookie_file(), job_dir, "douyin-cookies.txt"
    )
    try:
        return _download_douyin_note_to_mp4_impl(
            url, job_dir, timeout_seconds, cookie_path
        )
    finally:
        _cleanup_task_cookies(job_dir)


def _download_douyin_note_to_mp4_impl(
    url: str, job_dir: Path, timeout_seconds: int, cookie_path: Path
) -> tuple[Path, str]:
    metadata_path = job_dir / "note.json"
    worker = Path(__file__).resolve().parent.parent / "tools" / "douyin_note_metadata.py"
    process = _run_subprocess(
        [
            str(_douyin_f2_python()),
            str(worker),
            "--url",
            url,
            "--cookies",
            str(cookie_path),
            "--output",
            str(metadata_path),
        ],
        cwd=job_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        check=False,
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
    )
    logs = (process.stdout + "\n" + process.stderr).strip()
    if process.returncode != 0 or not metadata_path.is_file():
        raise DownloadError(logs[-4000:] or "抖音图文信息获取失败")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        image_urls = metadata.get("images", [])
    except (OSError, ValueError, AttributeError) as exc:
        raise DownloadError("抖音图文信息格式不正确") from exc
    if not isinstance(image_urls, list) or not image_urls:
        raise NoVideoError("该抖音作品中没有可转换的图片")

    slides: list[Path] = []
    asset_logs: list[str] = []
    for index, image_url in enumerate(image_urls[:40]):
        if not isinstance(image_url, str):
            continue
        source = job_dir / f"source-{index:03d}.img"
        slide = job_dir / f"slide-{index:03d}.jpg"
        try:
            _download_douyin_asset(image_url, source, timeout_seconds)
            _render_douyin_slide(source, slide)
            slides.append(slide)
        except (DownloadError, httpx.HTTPError, OSError, ValueError) as exc:
            asset_logs.append(f"第 {index + 1} 张图片失败：{exc}")
    if not slides:
        raise DownloadError("抖音图文图片均下载失败")

    seconds_per_slide = 3
    concat_file = job_dir / "slides.txt"
    concat_lines: list[str] = []
    for slide in slides:
        concat_lines.extend([f"file '{slide.name}'", f"duration {seconds_per_slide}"])
    concat_lines.append(f"file '{slides[-1].name}'")
    concat_file.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")

    music_path: Path | None = None
    music_url = metadata.get("music_url")
    if isinstance(music_url, str) and music_url:
        candidate = job_dir / "music.mp3"
        try:
            _download_douyin_asset(music_url, candidate, timeout_seconds)
            music_path = candidate
        except (DownloadError, httpx.HTTPError, OSError) as exc:
            asset_logs.append(f"原声下载失败：{exc}")

    output = job_dir / f"{metadata.get('aweme_id') or 'douyin-note'}.mp4"
    ffmpeg_arguments = [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        concat_file.name,
    ]
    if music_path is not None:
        ffmpeg_arguments.extend(["-stream_loop", "-1", "-i", music_path.name])
    ffmpeg_arguments.extend(
        [
            "-t",
            str(len(slides) * seconds_per_slide),
            "-r",
            "30",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
        ]
    )
    if music_path is not None:
        ffmpeg_arguments.extend(["-c:a", "aac", "-b:a", "128k"])
    ffmpeg_arguments.append(output.name)
    ffmpeg = _run_subprocess(
        ffmpeg_arguments,
        cwd=job_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        check=False,
    )
    ffmpeg_logs = (ffmpeg.stdout + "\n" + ffmpeg.stderr).strip()
    if ffmpeg.returncode != 0 or not output.is_file():
        raise DownloadError(ffmpeg_logs[-4000:] or "抖音图文 MP4 合成失败")
    summary = [logs[-2000:], *asset_logs, ffmpeg_logs[-2000:]]
    return output, "\n".join(part for part in summary if part)[-4000:]


def download_to_mp4(url: str, job_dir: Path, timeout_seconds: int) -> tuple[Path, str]:
    url = normalize_video_url(url)
    platform = video_platform(url)
    command = ensure_runtime(platform)
    source_url = (
        resolve_xiaoheihe_video_url(url, timeout_seconds)
        if platform == "xiaoheihe"
        else url
    )
    if platform == "douyin":
        source_url = resolve_douyin_share_url(url, timeout_seconds)
    if platform == "xiaohongshu":
        source_url = resolve_xiaohongshu_share_url(url, timeout_seconds)
    job_dir.mkdir(parents=True, exist_ok=False)

    if platform == "douyin" and _is_douyin_note_url(source_url):
        return _download_douyin_note_to_mp4(source_url, job_dir, timeout_seconds)

    if platform == "bilibili":
        arguments = [
            *command,
            "download",
            "--output-format",
            "mp4",
            "--no-progress",
            "--no-color",
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
        for arguments in attempts:
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
                timeout=timeout_seconds,
                check=False,
            )
            logs = (process.stdout + "\n" + process.stderr).strip()
            attempt_logs.append(logs)
            if process.returncode == 0:
                break
            if (
                platform == "douyin"
                and not douyin_fallback_attempted
                and any(marker in logs.casefold() for marker in ("fresh cookies", "http error 403"))
            ):
                douyin_fallback_attempted = True
                try:
                    media_url = _resolve_douyin_video_media(source_url, job_dir, timeout_seconds)
                    # CDN URLs can make the generic extractor use a very long
                    # query string as the ID, exceeding Windows filename limits.
                    attempts.append([
                        *arguments[:-1], "--output", "douyin.%(ext)s", media_url,
                    ])
                except DownloadError as exc:
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
                    return _download_douyin_note_to_mp4(
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
                raise NoVideoError("该小红书笔记中没有视频")
            raise DownloadError(combined_logs[-4000:] or f"{downloader} 下载失败")

        candidates = sorted(job_dir.rglob("*.mp4"), key=lambda path: path.stat().st_size, reverse=True)
        if not candidates:
            raise DownloadError("下载器执行成功，但未找到 MP4 文件")
        return candidates[0], logs[-4000:]
    finally:
        _cleanup_task_cookies(job_dir)
