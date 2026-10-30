"""Compare avatar download routes and certificate stores without running the bot."""
from __future__ import annotations

import argparse
import asyncio
from io import BytesIO
import os
from pathlib import Path
import re
import ssl
import sys
from urllib.request import getproxies

import httpx
from PIL import Image
import truststore
from dotenv import load_dotenv


DEFAULT_URL = (
    "https://avatars.steamstatic.com/"
    "88ca271e657a0882145b8764db8c4fdb7ea4c128_full.jpg"
)


def error_chain(error: Exception) -> str:
    details = []
    seen = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        message = re.sub(r"(https?://)[^/@\s]+@", r"\1***@", str(current))
        details.append(f"{type(current).__name__}: {message}")
        current = current.__cause__ or current.__context__
    return " <- ".join(details)


async def probe(label: str, url: str, *, trust_env: bool, system_cert: bool = False,
                proxy: str | None = None) -> tuple[str, bool, str]:
    try:
        verify = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT) if system_cert else True
        options = {"proxy": proxy} if proxy else {}
        async with asyncio.timeout(6):
            async with httpx.AsyncClient(
                trust_env=trust_env, verify=verify, follow_redirects=True,
                timeout=5, headers={"User-Agent": "Mozilla/5.0", "Accept": "image/*"},
                **options,
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
            with Image.open(BytesIO(response.content)) as image:
                image.verify()
        return label, True, f"HTTP {response.status_code}; valid image; {len(response.content)} bytes"
    except Exception as exc:
        return label, False, error_chain(exc)


async def diagnose(url: str) -> list[tuple[str, bool, str]]:
    tests = [
        probe("Direct / Python certificates", url, trust_env=False),
        probe("Automatic proxy / Python certificates", url, trust_env=True),
        probe("Direct / Windows system certificates", url, trust_env=False, system_cert=True),
        probe("Automatic proxy / Windows system certificates", url, trust_env=True, system_cert=True),
    ]
    proxy = os.getenv("STEAM_HTTP_PROXY", "").strip()
    if proxy:
        tests.extend([
            probe("STEAM_HTTP_PROXY / Python certificates", url, trust_env=False, proxy=proxy),
            probe("STEAM_HTTP_PROXY / Windows system certificates", url,
                  trust_env=False, system_cert=True, proxy=proxy),
        ])
    return list(await asyncio.gather(*tests))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL, help="Full Steam avatar image URL")
    args = parser.parse_args()
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    proxies = getproxies()
    print(f"Python: {sys.executable}")
    print("Automatic proxy detected:", any(proxies.get(key) for key in ("https", "http", "all")))
    print("STEAM_HTTP_PROXY configured:", bool(os.getenv("STEAM_HTTP_PROXY", "").strip()))
    print("Testing avatar downloads (6-second limit per route)...")
    results = asyncio.run(diagnose(args.url))
    for label, ok, detail in results:
        print(f"\n[{'PASS' if ok else 'FAIL'}] {label}\n{detail}")
    print("\nNo bot messages sent; configuration, databases and image caches were not modified.")
    return 0 if any(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
