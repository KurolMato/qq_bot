from __future__ import annotations

import getpass
import os
import re
import ssl
from pathlib import Path

import httpx
import truststore
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def credential_path() -> Path:
    configured = os.getenv("STEAM_API_KEY_FILE", "").strip()
    if not configured:
        return PROJECT_ROOT / "secrets" / "steam-api-key.txt"
    path = Path(configured).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def main() -> None:
    print("Open https://steamcommunity.com/dev/apikey and sign in to Steam.")
    print("Create a Web API Key. Keep it private and never send it to a QQ group.")
    key = getpass.getpass("Paste the 32-character Steam Web API Key: ").strip()
    if not re.fullmatch(r"[A-Fa-f0-9]{32}", key):
        print("Setup failed: the API Key must be exactly 32 hexadecimal characters.")
        raise SystemExit(1)
    try:
        context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        with httpx.Client(verify=context, timeout=20, trust_env=False) as client:
            response = client.get(
                "https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/",
                params={"key": key, "vanityurl": "gaben"},
            )
        response.raise_for_status()
        if not isinstance(response.json().get("response"), dict):
            raise ValueError("invalid response")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {401, 403}:
            print("Setup failed: Steam rejected this API Key (HTTP 401/403).")
        else:
            print(f"Setup failed: Steam returned HTTP {exc.response.status_code}.")
        raise SystemExit(1) from None
    except httpx.TimeoutException:
        print("Setup failed: Steam API connection timed out. Check the network and retry.")
        raise SystemExit(1) from None
    except httpx.ConnectError as exc:
        reason = "TLS certificate validation failed" if "certificate" in str(exc).casefold() else "connection failed"
        print(f"Setup failed: Steam API {reason}. Check the network or proxy and retry.")
        raise SystemExit(1) from None
    except Exception:
        print("Setup failed: Steam returned an invalid response. Please retry later.")
        raise SystemExit(1) from None

    path = credential_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(key, encoding="utf-8")
    temporary.replace(path)
    print("Steam Web API Key saved successfully.")
    print("Restart the bot from 机器人管理.cmd, then use /steam status in the QQ group.")


if __name__ == "__main__":
    main()
