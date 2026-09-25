from __future__ import annotations

import getpass
import os
import re
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def credential_path() -> Path:
    configured = os.getenv("PSN_NPSSO_FILE", "").strip()
    if not configured:
        return PROJECT_ROOT / "secrets" / "psn-npsso.txt"
    path = Path(configured).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def main() -> None:
    try:
        from psnawp_api import PSNAWP
    except ImportError:
        print("PSNAWP is not installed. Run: .venv\\Scripts\\python.exe -m pip install -r requirements-bot.txt")
        raise SystemExit(1) from None

    print("Sign in to playstation.com using the dedicated observer account.")
    print("Then open https://ca.account.sony.com/api/v1/ssocookie in the same browser.")
    print("NPSSO is equivalent to the account password. Never send it to a QQ group.")
    npsso = getpass.getpass("Paste the 64-character npsso value: ").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{64}", npsso):
        print("Login failed: NPSSO must be exactly 64 characters.")
        raise SystemExit(1)

    try:
        client = PSNAWP(npsso).me()
        online_id = str(client.online_id)
    except Exception:
        print("Login failed: PlayStation rejected this NPSSO. Generate a new value and try again.")
        raise SystemExit(1) from None

    path = credential_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(npsso, encoding="utf-8")
    temporary.replace(path)
    print(f"Login successful: {online_id}")
    print("Restart the bot from 机器人管理.cmd, then use /psn status in the QQ group.")


if __name__ == "__main__":
    main()
