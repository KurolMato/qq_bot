"""A tiny stand-in for ``dsh --profile ...`` used by the ACP client tests.

Speaks newline-delimited JSON-RPC on stdin/stdout and emulates just enough of
ACP: initialize, session/new, session/resume, session/prompt, session/close,
plus one permission request. Prompt text selects the behaviour so tests stay
deterministic.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time

DEFAULT_SESSION = "fake-session"
RESUMABLE_SESSION = "persisted-session"


def send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def result(request_id: int, payload: dict) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "result": payload})


def error(request_id: int, code: int, message: str) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def update(session_id: str, body: dict) -> None:
    send(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": session_id, "update": body},
        }
    )


def text_update(session_id: str, text: str) -> None:
    update(session_id, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}})


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}

        if method == "initialize":
            result(request_id, {"protocolVersion": 1, "agentCapabilities": {}})
        elif method == "session/new":
            result(request_id, {"sessionId": DEFAULT_SESSION, "configOptions": []})
        elif method == "session/resume":
            resumed = params.get("sessionId")
            if resumed == RESUMABLE_SESSION:
                result(request_id, {"configOptions": []})
            else:
                error(request_id, -32602, f"session is not resumable: {resumed}")
        elif method == "session/close":
            result(request_id, {})
        elif method == "session/cancel":
            continue
        elif method == "session/prompt":
            sid = params.get("sessionId") or DEFAULT_SESSION
            text = ""
            image_count = 0
            for block in params.get("prompt") or []:
                if block.get("type") == "image":
                    image_count += 1
                text += block.get("text", "")
            update(
                sid,
                {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {"type": "text", "text": "THOUGHT: internal reasoning"},
                },
            )
            if text == "browser-policy":
                patch = Path(sys.argv[sys.argv.index("--patch") + 1])
                rows = json.loads(patch.read_text(encoding="utf-8"))
                mounts = [item for row in rows for item in row.get("insert", [])
                          if item.get("id") == "qq-browser-mcp"]
                text_update(sid, f"BROWSER:{len(mounts)}")
            elif text == "policy":
                patch = Path(sys.argv[sys.argv.index("--patch") + 1])
                rows = json.loads(patch.read_text(encoding="utf-8"))
                mode = next(row["config"]["mode"] for row in rows if row["id"] == "sandbox-policy")
                preset = next(row["config"]["defaultPreset"] for row in rows if row["id"] == "permission")
                text_update(sid, f"POLICY:{mode}:{preset}:{os.getenv('DSH_PERMISSION_MODE')}")
            elif text == "write":
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": 9001,
                        "method": "session/request_permission",
                        "params": {
                            "sessionId": sid,
                            "toolCall": {"title": "write dsh-test.txt"},
                            "options": [
                                {"optionId": "allow", "kind": "allow_once", "name": "Allow"},
                                {"optionId": "deny", "kind": "reject_once", "name": "Reject"},
                            ],
                        },
                    }
                )
                reply = json.loads(sys.stdin.readline() or "{}")
                outcome = ((reply.get("result") or {}).get("outcome") or {})
                chosen = outcome.get("optionId")
                text_update(sid, "DENIED" if chosen == "deny" else f"ALLOWED:{chosen}")
            elif text == "slow":
                text_update(sid, "PARTIAL")
                time.sleep(3)
                text_update(sid, "SLOW-DONE")
            elif text == "tool":
                update(
                    sid,
                    {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "t1",
                        "title": "read data/qq-bot.log",
                        "kind": "read",
                        "status": "completed",
                    },
                )
                text_update(sid, "READ-OK")
            elif image_count:
                text_update(sid, f"IMAGES:{image_count}")
            else:
                text_update(sid, f"FAKE:{text}")
            result(request_id, {"stopReason": "end_turn"})
        elif request_id is not None:
            error(request_id, -32601, "unsupported")


if __name__ == "__main__":
    main()
