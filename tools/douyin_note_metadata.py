from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from f2.apps.douyin.handler import DouyinHandler
from f2.apps.douyin.utils import AwemeIdFetcher


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)


def read_netscape_cookie_file(path: Path) -> str:
    cookies: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_") :]
        fields = line.split("\t")
        if len(fields) != 7:
            continue
        domain, _subdomains, _cookie_path, _secure, _expires, name, value = fields
        normalized_domain = domain.lstrip(".").casefold()
        if normalized_domain == "douyin.com" or normalized_domain.endswith(".douyin.com"):
            cookies[name] = value
    if not cookies:
        raise ValueError("Cookie 文件中没有找到 douyin.com 的 Cookie")
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


async def fetch_metadata(url: str, cookie_file: Path) -> dict[str, object]:
    cookie = read_netscape_cookie_file(cookie_file)
    kwargs = {
        "headers": {
            "User-Agent": USER_AGENT,
            "Referer": "https://www.douyin.com/",
        },
        "cookie": cookie,
        "proxies": {"http://": None, "https://": None},
    }
    aweme_id = await AwemeIdFetcher.get_aweme_id(url)
    item = await DouyinHandler(kwargs).fetch_one_video(aweme_id=aweme_id)
    return {
        "aweme_id": str(item.aweme_id or aweme_id),
        "aweme_type": item.aweme_type,
        "images": [value for value in (item.images or []) if isinstance(value, str)],
        "music_url": item.music_play_url if isinstance(item.music_play_url, str) else None,
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
