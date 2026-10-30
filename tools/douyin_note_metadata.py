from __future__ import annotations

import argparse
import asyncio
import json
import http.cookiejar
from pathlib import Path

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)


def read_netscape_cookie_file(path: Path) -> str:
    jar = http.cookiejar.MozillaCookieJar()
    jar.load(str(path), ignore_discard=True)
    cookies = {
        cookie.name: cookie.value for cookie in jar
        if cookie.domain.lstrip(".").casefold() == "douyin.com"
        or cookie.domain.lstrip(".").casefold().endswith(".douyin.com")
    }
    if not cookies:
        raise ValueError("Cookie 文件中没有找到 douyin.com 的 Cookie")
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


async def fetch_metadata(url: str, cookie_file: Path) -> dict[str, object]:
    from f2.apps.douyin.handler import DouyinHandler
    from f2.apps.douyin.utils import AwemeIdFetcher

    cookie = read_netscape_cookie_file(cookie_file)
    kwargs = {
        "headers": {
            "User-Agent": USER_AGENT,
            "Referer": "https://www.douyin.com/",
            "x-tt-argus": "1",
        },
        "cookie": cookie,
        "proxies": {"http://": None, "https://": None},
    }
    aweme_id = await AwemeIdFetcher.get_aweme_id(url)
    item = await DouyinHandler(kwargs).fetch_one_video(aweme_id=aweme_id)
    raw = item._to_raw() if hasattr(item, "_to_raw") else {}
    detail = raw.get("aweme_detail") or {}
    return {
        "aweme_id": str(item.aweme_id or aweme_id),
        "aweme_type": item.aweme_type,
        "images": [value for value in (item.images or []) if isinstance(value, str)],
        "music_url": item.music_play_url if isinstance(item.music_play_url, str) else None,
        **({"image_details": detail["images"]} if "images" in detail else {}),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--cookies", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()

    payload = asyncio.run(fetch_metadata(arguments.url, arguments.cookies))
    arguments.output.write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
