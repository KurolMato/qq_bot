from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import httpx


PAGE_HOSTS = {"bilibili.com", "www.bilibili.com", "m.bilibili.com", "t.bilibili.com", "b23.tv"}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
}


class BilibiliImageError(RuntimeError):
    pass


@dataclass(frozen=True)
class BilibiliPost:
    images: tuple[str, ...] = ()
    video_url: str | None = None
    title: str = ""


def _page_url(url: str) -> str:
    parsed = urlparse(url)
    if (parsed.scheme not in {"http", "https"} or parsed.hostname not in PAGE_HOSTS
            or parsed.username or parsed.password or parsed.port not in {None, 80, 443}):
        raise BilibiliImageError("B 站分享链接跳转到了不受信任的地址")
    return url


def post_id(url: str) -> tuple[str, str] | None:
    parsed = urlparse(url)
    if parsed.hostname not in PAGE_HOSTS:
        return None
    match = re.fullmatch(r"/(?:opus|dynamic)/(\d+)/?", parsed.path)
    if match:
        return "opus", match[1]
    if parsed.hostname == "t.bilibili.com" and re.fullmatch(r"/\d+/?", parsed.path):
        return "opus", parsed.path.strip("/")
    match = re.fullmatch(r"/read/cv(\d+)/?", parsed.path)
    if match:
        return "article", match[1]
    if parsed.path.rstrip("/") in {"/read/mobile", "/read/native"}:
        value = parse_qs(parsed.query).get("id", [""])[0]
        if value.isdigit():
            return "article", value
    return None


def resolve_share_url(url: str, timeout: float) -> str:
    if urlparse(url).hostname != "b23.tv":
        return url
    deadline = time.monotonic() + min(timeout, 15)
    current = url
    for _ in range(6):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return url
        try:
            response = httpx.get(current, headers=HEADERS, follow_redirects=False, timeout=remaining)
            if not response.is_redirect:
                response.raise_for_status()
        except httpx.HTTPError:
            # Let the established video downloader try its own short-link route.
            return url
        if not response.is_redirect:
            return current
        current = _page_url(urljoin(current, response.headers.get("location", "")))
        if urlparse(current).hostname != "b23.tv":
            return current
    raise BilibiliImageError("B 站短链接重定向次数过多")


def content_images(item: dict, expected_id: str) -> BilibiliPost:
    actual_id = str(item.get("id_str") or item.get("id") or "")
    if actual_id and actual_id != expected_id:
        raise BilibiliImageError("B 站返回的图文编号与请求不一致")
    modules = item.get("modules", [])
    urls: list[str] = []
    album_urls: list[str] = []
    has_album_pictures = False
    title = (item.get("basic") or {}).get("title", "")
    if isinstance(modules, list):
        for module in modules:
            if not isinstance(module, dict):
                continue
            if module.get("module_paywall") or module.get("module_blocked"):
                raise BilibiliImageError("该 B 站图文需要登录或相应访问权限")
            module_title = (module.get("module_title") or {}).get("text")
            if isinstance(module_title, str) and module_title.strip():
                title = module_title
            album = (((module.get("module_top") or {}).get("display") or {}).get("album") or {})
            pictures = album.get("pics") or []
            has_album_pictures = has_album_pictures or bool(pictures)
            for picture in pictures:
                if isinstance(picture, dict) and isinstance(picture.get("url"), str):
                    album_urls.append(picture["url"])
            content = module.get("module_content") or {}
            for paragraph in content.get("paragraphs", []):
                if not isinstance(paragraph, dict) or paragraph.get("para_type") != 2:
                    continue
                pictures = paragraph.get("pic") or paragraph.get("pics") or {}
                for picture in pictures.get("pics", []):
                    if isinstance(picture, dict) and isinstance(picture.get("url"), str):
                        urls.append(picture["url"])
        # Gallery dynamics use the top album instead of picture paragraphs.
        # For long articles, body pictures remain authoritative: the top
        # display may contain only a cover or duplicate preview pictures.
        if not urls:
            urls = album_urls
        if has_album_pictures and not any(urls):
            raise BilibiliImageError("B 站图文包含相册，但未返回可识别的图片地址")
    elif isinstance(modules, dict):
        major = (modules.get("module_dynamic") or {}).get("major") or {}
        title = (major.get("opus") or {}).get("title") or title
        # Draw/opus pictures are the post itself. Never send archive covers,
        # author portraits, emoji or recommendation thumbnails as post images.
        for picture in (major.get("draw") or {}).get("items", []):
            if isinstance(picture, dict) and isinstance(picture.get("src"), str):
                urls.append(picture["src"])
        for picture in (major.get("opus") or {}).get("pics", []):
            if isinstance(picture, dict) and isinstance(picture.get("url"), str):
                urls.append(picture["url"])
        archive = major.get("archive") or {}
        bvid = archive.get("bvid", "")
        if not urls and isinstance(bvid, str) and re.fullmatch(r"BV[A-Za-z0-9]+", bvid):
            return BilibiliPost(video_url=f"https://www.bilibili.com/video/{bvid}")
    return BilibiliPost(tuple(dict.fromkeys(url for url in urls if url)), title=title.strip() if isinstance(title, str) else "")


class _ArticleImages(HTMLParser):
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.urls: list[str] = []
        self.title = ""
        self._in_title = False
        self._title_parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "meta" and values.get("property") == "og:title":
            self.title = values.get("content") or ""
        if tag == "h1" and (not self.depth or {"title", "article-title"}.intersection((values.get("class") or "").split())):
            self._in_title = True
            self._title_parts = []
        if not self.depth and (values.get("id") == "read-article-holder" or "article-holder" in (values.get("class") or "").split()):
            self.depth = 1
        elif self.depth and tag not in {"img", "br", "hr", "input", "source", "meta", "link", "wbr"}:
            self.depth += 1
        if self.depth and tag == "img":
            url = values.get("data-src") or values.get("data-original") or values.get("src")
            if url and not url.startswith("data:"):
                self.urls.append(url)

    def handle_endtag(self, tag):
        if tag == "h1" and self._in_title:
            self.title = "".join(self._title_parts).strip() or self.title
            self._in_title = False
        if self.depth and tag not in {"img", "br", "hr", "input", "source", "meta", "link", "wbr"}:
            self.depth -= 1

    def handle_data(self, data):
        if self._in_title:
            self._title_parts.append(data)


def page_content(text: str, kind: str, expected_id: str) -> BilibiliPost | None:
    marker = re.search(r"(?:window\.)?__INITIAL_STATE__\s*=\s*", text)
    if marker:
        try:
            state, _ = json.JSONDecoder().raw_decode(text[marker.end():])
        except ValueError:
            state = {}
        detail = state.get("detail") if isinstance(state, dict) else None
        if isinstance(detail, dict) and isinstance(detail.get("modules"), (list, dict)):
            return content_images(detail, expected_id)
    if kind == "article":
        parser = _ArticleImages()
        parser.feed(text)
        if parser.urls:
            return BilibiliPost(tuple(dict.fromkeys(parser.urls)), title=parser.title)
    return None


def fetch_post(url: str, timeout: float) -> BilibiliPost:
    identity = post_id(url)
    if identity is None:
        raise BilibiliImageError("B 站图文链接格式不正确")
    kind, identifier = identity
    deadline = time.monotonic() + timeout
    current = f"https://www.bilibili.com/opus/{identifier}" if kind == "opus" else url
    try:
        for _ in range(6):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BilibiliImageError("B 站图文解析超时")
            response = httpx.get(_page_url(current), headers=HEADERS, follow_redirects=False, timeout=min(remaining, 20))
            if response.is_redirect:
                current = _page_url(urljoin(current, response.headers.get("location", "")))
                redirected_id = post_id(current)
                if redirected_id is None:
                    raise BilibiliImageError("B 站图文跳转到登录页或无法识别的页面")
                kind, identifier = redirected_id
                continue
            if response.status_code == 200:
                parsed = page_content(response.text, kind, identifier)
                if parsed is not None:
                    return parsed
            break
        else:
            raise BilibiliImageError("B 站图文重定向次数过多")
        # The dynamic API also recognizes video dynamics; return their BV URL
        # to the original video route rather than sending a cover as a picture.
        if kind == "opus":
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BilibiliImageError("B 站图文解析超时")
            api = httpx.get("https://api.bilibili.com/x/polymer/web-dynamic/v1/detail", params={"id": identifier, "features": "itemOpusStyle"}, headers=HEADERS, follow_redirects=False, timeout=min(remaining, 20))
            api.raise_for_status()
            payload = api.json()
            code = payload.get("code")
            if code in {-352, -412}:
                raise BilibiliImageError("B 站图文触发访问验证，请先在浏览器确认作品可见后重试")
            if code == -101:
                raise BilibiliImageError("该 B 站图文需要登录，请先在浏览器确认访问权限")
            if code == 0:
                item = (payload.get("data") or {}).get("item")
                if isinstance(item, dict):
                    if item.get("type") == "DYNAMIC_TYPE_ARTICLE":
                        raise BilibiliImageError("B 站未返回完整图文正文，请在浏览器确认作品可见后重试")
                    return content_images(item, identifier)
        raise BilibiliImageError("B 站未返回图文正文，作品可能已删除、需要登录或访问受限")
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        raise BilibiliImageError("B 站图文请求失败，请检查网络或稍后重试") from exc


def trusted_image_url(url: str) -> str:
    if url.startswith("//"):
        url = "https:" + url
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
            or parsed.port not in {None, 80, 443}
            or not any(host == suffix or host.endswith("." + suffix) for suffix in ("hdslb.com", "bilibili.com"))):
        raise BilibiliImageError("B 站返回了不受信任的图片地址")
    return parsed._replace(scheme="https", netloc=host).geturl()


def download_image(url: str, output: Path, timeout: float) -> None:
    current = trusted_image_url(url)
    deadline = time.monotonic() + timeout
    for _ in range(6):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BilibiliImageError("B 站图片下载超时")
        with httpx.stream("GET", current, headers=HEADERS, follow_redirects=False, timeout=min(remaining, 30)) as response:
            if response.is_redirect:
                current = trusted_image_url(urljoin(current, response.headers.get("location", "")))
                continue
            response.raise_for_status()
            with output.open("wb") as file:
                for chunk in response.iter_bytes():
                    if time.monotonic() >= deadline:
                        raise BilibiliImageError("B 站图片下载超时")
                    file.write(chunk)
            return
    raise BilibiliImageError("B 站图片重定向次数过多")
