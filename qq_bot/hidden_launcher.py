from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CREATE_NO_WINDOW = 0x08000000


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        return 2
    log_path = args.log if args.log.is_absolute() else PROJECT_ROOT / args.log
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as output:
        subprocess.Popen(
            command,
            cwd=str(PROJECT_ROOT),
            creationflags=CREATE_NO_WINDOW,
            close_fds=True,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
