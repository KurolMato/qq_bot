"""Manual smoke test for the QQ ⇄ DSH bridge, without touching QQ.

Usage:
    tools\\select_python.cmd tools\\dsh_smoke.py
    tools\\select_python.cmd tools\\dsh_smoke.py "看一下 data/qq-bot.log 最近有没有 ERROR"

It starts the bot's dsh profile with an explicit read-only override, asks one question and
prints the answer plus a one-line summary (elapsed, tools, session id).
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

from qq_bot.dsh_client import DshClient, DshError, DshSettings  # noqa: E402


def main() -> int:
    question = " ".join(sys.argv[1:]).strip() or "只回复三个字：在线中"
    settings = replace(DshSettings.from_env(PROJECT_ROOT), permission_mode="read-only")
    print(f"profile={settings.profile} home={settings.home} workdir={settings.workdir}")
    client = DshClient(settings)
    try:
        session_id = client.new_session()
        print(f"session={session_id}")
        answer = client.prompt(session_id, question)
        print(f"elapsed={answer.elapsed:.1f}s stop={answer.stop_reason} tools={answer.tools}")
        print("---")
        print(answer.text or "(empty)")
        return 0
    except DshError as exc:
        print(f"FAILED: {exc}")
        print(client.diagnostics())
        return 1
    finally:
        client.stop()


if __name__ == "__main__":
    raise SystemExit(main())
