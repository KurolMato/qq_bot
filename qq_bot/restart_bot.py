from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

from .napcat_watchdog import PROJECT_ROOT, _logger, restart_bot


def schedule_restart() -> None:
    """Start a helper that survives the bot process it is going to replace."""
    if os.name != "nt":
        raise OSError("QQ Bot 自重启目前只支持 Windows")
    subprocess.Popen(
        [sys.executable, "-m", "qq_bot.restart_bot", "--delay", "3",
         "--expected-pid", str(os.getpid())],
        cwd=str(PROJECT_ROOT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Restart this project's QQ Bot")
    parser.add_argument("--delay", type=float, default=0)
    parser.add_argument("--expected-pid", type=int)
    args = parser.parse_args(argv)
    if not 0 <= args.delay <= 30:
        parser.error("--delay must be between 0 and 30 seconds")
    if args.expected_pid is not None and args.expected_pid <= 0:
        parser.error("--expected-pid must be positive")
    if args.delay:
        time.sleep(args.delay)
    print("Restarting QQ Bot...")
    logger = _logger()
    try:
        ok = restart_bot(logger, reason="manual", expected_process_id=args.expected_pid)
    except (OSError, subprocess.SubprocessError):
        logger.exception("QQ Bot restart helper failed")
        ok = False
    if not ok:
        print("Restart failed. Check data\\napcat-watchdog.log.")
        return 1
    print("Restart command sent. NapCat will reconnect automatically.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
