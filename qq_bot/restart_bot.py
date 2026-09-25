from __future__ import annotations

from .napcat_watchdog import _logger, restart_bot


def main() -> int:
    print("Restarting QQ Bot...")
    if not restart_bot(_logger(), reason="manual"):
        print("Restart failed. Check data\\napcat-watchdog.log.")
        return 1
    print("Restart command sent. NapCat will reconnect automatically.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
