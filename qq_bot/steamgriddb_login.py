from __future__ import annotations

import getpass
import ssl
from pathlib import Path

import httpx
import truststore

from .steam_registry import DEFAULT_STEAMGRIDDB_KEY_PATH, STEAMGRIDDB_API_ROOT


def _validate(key: str) -> None:
    context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    with httpx.Client(
        timeout=httpx.Timeout(12.0, connect=5.0),
        follow_redirects=True,
        verify=context,
        trust_env=False,
        headers={"Authorization": f"Bearer {key}"},
    ) as client:
        response = client.get(
            f"{STEAMGRIDDB_API_ROOT}/grids/steam/10",
            params={"dimensions": "600x900", "types": "static", "nsfw": "false"},
        )
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise ValueError("SteamGridDB did not accept this API Key.")


def main() -> int:
    print("SteamGridDB vertical cover setup")
    print("Create a key at: https://www.steamgriddb.com/profile/preferences/api")
    key = getpass.getpass("Paste the SteamGridDB API Key: ").strip()
    if len(key) < 16:
        print("Setup failed: the API Key is empty or too short.")
        return 1
    try:
        _validate(key)
    except Exception as exc:
        print(f"Setup failed: SteamGridDB rejected the API Key or is unavailable: {exc}")
        return 1

    path = Path(DEFAULT_STEAMGRIDDB_KEY_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key + "\n", encoding="utf-8")
    print(f"SteamGridDB API Key saved to {path}")
    print("Restart the QQ bot. New Steam games will prefer 600x900 vertical covers.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
