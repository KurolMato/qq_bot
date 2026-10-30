"""Minimal ACP (Agent Client Protocol) client for a local ``dsh`` profile.

The QQ bot drives a long-lived ``dsh`` child process over newline-delimited
JSON-RPC on stdio. Each QQ conversation owns one ACP session, so follow-up
questions keep their context while the process itself stays warm.

Only the subset the bot needs is implemented: ``initialize``, ``session/new``,
``session/prompt``, ``session/cancel`` and ``session/close``. Permission
requests for additional privileges are rejected. The launch overlay explicitly
selects read-only or workspace-write independently of the user's profile.
"""

from __future__ import annotations

import json
import base64
import logging
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .atomic_json import write_json
from .browser_tools import browser_mcp_config

logger = logging.getLogger(__name__)

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class DshError(RuntimeError):
    """The dsh process could not start or the protocol failed."""


@dataclass
class DshSettings:
    command: list[str]
    profile: str
    home: Path
    workdir: Path
    timeout: float = 600.0
    idle_seconds: float = 0.0
    permission_mode: str = "read-only"
    browser_mcp: dict | None = None

    def __post_init__(self) -> None:
        if self.permission_mode not in {"read-only", "workspace-write"}:
            raise ValueError("QQ_DSH_PERMISSION_MODE 只支持 read-only 或 workspace-write")

    @classmethod
    def from_env(cls, project_root: Path) -> "DshSettings":
        # Deliberately not DSH_*: those variables describe the harness that may
        # be running this bot, and must not leak into the bridge configuration.
        home = Path(os.path.expanduser(os.getenv("QQ_DSH_HOME", str(Path.home() / ".dsh-qq"))))
        default_bin = home / "cli" / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js"
        cli = os.getenv("QQ_DSH_CLI", "").strip()
        command = [os.getenv("QQ_DSH_NODE", "node"), cli] if cli else [os.getenv("QQ_DSH_NODE", "node"), str(default_bin)]
        workdir = os.getenv("QQ_DSH_WORKDIR", "").strip()
        return cls(
            command=command,
            profile=os.getenv("QQ_DSH_PROFILE", "qq").strip() or "qq",
            home=home,
            workdir=Path(workdir) if workdir else project_root,
            timeout=max(float(os.getenv("QQ_DSH_TIMEOUT", "600")), 30.0),
            idle_seconds=max(float(os.getenv("QQ_DSH_IDLE_SECONDS", "0")), 0.0),
            permission_mode=os.getenv("QQ_DSH_PERMISSION_MODE", "workspace-write").strip(),
            browser_mcp=browser_mcp_config(project_root, command[0]),
        )


@dataclass
class DshAnswer:
    text: str
    session_id: str
    elapsed: float
    tools: list[str] = field(default_factory=list)
    cancelled: bool = False
    stop_reason: str = ""


@dataclass
class ImagePart:
    """One image attachment forwarded to the agent as an ACP image block."""

    data: bytes
    mime: str = "image/png"

    def block(self) -> dict:
        return {
            "type": "image",
            "mimeType": self.mime,
            "data": base64.b64encode(self.data).decode("ascii"),
        }


@dataclass
class _Prompt:
    text: list[str] = field(default_factory=list)
    tools: dict[str, dict] = field(default_factory=dict)
    cancelled: bool = False
    stop_reason: str = ""
    done: threading.Event = field(default_factory=threading.Event)


def clean_answer(text: str) -> str:
    """Drop terminal escapes and markdown emphasis QQ shows literally."""
    text = _ANSI.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    text = re.sub(r"^\s*```.*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    return text.strip()


def split_message(text: str, limit: int = 1200) -> list[str]:
    """Split on paragraph boundaries so each QQ message stays readable."""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    parts: list[str] = []
    remaining = text
    while remaining and len(parts) < 3:
        if len(remaining) <= limit:
            parts.append(remaining)
            remaining = ""
            break
        cut = remaining.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        parts[-1] = parts[-1] + "\n…（回答过长，已截断）"
    return parts


_CLAUSE_SEP = re.compile(r"[，；、。]")


def split_bubbles(text: str, max_parts: int = 4, max_len: int = 300, comma_split_max_len: int = 30, hard_cap: int = 30) -> list[str]:
    """Split a reply into chat bubbles.

    Newlines and Chinese commas/semicolons/full stops all end a bubble, because
    the model tends to write one comma-joined line instead of several messages.
    """
    clauses: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Short comma chains are the model's way of writing several messages in
        # one line; a long sentence keeps its commas and stays a single bubble.
        if len(line) <= comma_split_max_len and _CLAUSE_SEP.search(line):
            clauses.extend(part.strip() for part in _CLAUSE_SEP.split(line) if part.strip())
        else:
            clauses.append(line)
    sized: list[str] = []
    for clause in clauses:
        while len(clause) > max_len:
            cut = max(clause.rfind(mark, 0, max_len) for mark in "。！？；;")
            if cut < max_len // 3:
                cut = max_len
            sized.append(clause[:cut].strip())
            clause = clause[cut:].strip()
        if clause:
            sized.append(clause)
    if not sized:
        return []
    if max_parts > 0 and len(sized) > max_parts:
        # Optional chat-sized cap: fold the overflow into one final bubble
        # instead of truncating it. max_parts=0 means "no limit".
        sized = sized[: max_parts - 1] + ["，".join(sized[max_parts - 1 :])]
    elif len(sized) > hard_cap:
        # Even with no configured limit, stop a runaway comma chain from
        # flooding the chat with hundreds of messages.
        sized = sized[: hard_cap - 1] + ["，".join(sized[hard_cap - 1 :])]
    return sized


def trim_answer(text: str, limit: int = 700) -> str:
    """Keep replies chat-sized: cut at a sentence end instead of mid-thought."""
    text = text.strip()
    if limit <= 0 or len(text) <= limit:
        return text
    window = text[:limit]
    cut = max(window.rfind(mark) for mark in "。！？!?\n")
    if cut >= limit // 2:
        return window[: cut + 1].rstrip() + "…"
    return window.rstrip() + "…"


class SessionStore:
    """Maps a QQ account to its dsh session id, persisted as JSON."""

    def __init__(self, path: Path):
        self.path = path
        self._data: dict[str, dict] = {}
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._data = {}
            return
        users = raw.get("users") if isinstance(raw, dict) else None
        self._data = users if isinstance(users, dict) else {}

    def get(self, user_id: str) -> str:
        return str((self._data.get(user_id) or {}).get("session_id") or "")

    def info(self, user_id: str) -> dict:
        return dict(self._data.get(user_id) or {})

    def bind(self, user_id: str, session_id: str) -> None:
        """Remember a fresh session without counting it as a completed turn."""
        item = self._data.get(user_id) or {}
        item.setdefault("turns", 0)
        item.update(
            {
                "session_id": session_id,
                "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        )
        self._data[user_id] = item
        self.save()

    def record(self, user_id: str, session_id: str, elapsed: float) -> None:
        item = self._data.get(user_id) or {}
        item.update(
            {
                "session_id": session_id,
                "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "turns": int(item.get("turns") or 0) + 1,
                "last_elapsed": round(elapsed, 1),
            }
        )
        self._data[user_id] = item
        self.save()

    def forget(self, user_id: str) -> None:
        if self._data.pop(user_id, None) is not None:
            self.save()

    def save(self) -> None:
        try:
            write_json(self.path, {"version": 1, "users": self._data})
        except OSError:
            logger.exception("保存 dsh 会话映射失败")


class DshClient:
    """Owns the dsh child process and its ACP sessions (thread safe)."""

    def __init__(self, settings: DshSettings):
        self.settings = settings
        self._proc: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._pending: dict[int, queue.Queue] = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._prompts: dict[str, _Prompt] = {}
        self._next_id = 0
        self._stderr_tail: list[str] = []
        self._last_used = 0.0
        self._initialized = False
        self._live: set[str] = set()
        self._policy_dir: tempfile.TemporaryDirectory | None = None

    # -- process -----------------------------------------------------------
    def start(self) -> None:
        if self._proc is not None and self._proc.poll() is None and self._initialized:
            return
        self.stop()
        env = {key: value for key, value in os.environ.items() if not key.startswith("DSH_")}
        env.update(
            {
                "DSH_HOME": str(self.settings.home),
                "DSH_PERMISSION_MODE": self.settings.permission_mode,
                "NO_COLOR": "1",
                "PYTHONIOENCODING": "utf-8",
            }
        )
        try:
            # DSH's sandbox-policy config is authoritative; the environment
            # variable alone does not override a profile's mode.
            self._policy_dir = tempfile.TemporaryDirectory(prefix="qq-dsh-policy-")
            policy_file = Path(self._policy_dir.name) / "policy.yml"
            patches = [
                {"id": "sandbox-policy", "config": {"mode": self.settings.permission_mode}},
                {"id": "permission", "config": {"defaultPreset": self.settings.permission_mode}},
            ]
            if self.settings.permission_mode == "workspace-write" and self.settings.browser_mcp:
                # One MCP worker per owner DSH process, shared by its sessions.
                # Group/background clients must never mount desktop capabilities.
                patches.append({"insert": [{
                    "id": "qq-browser-mcp", "name": "@deepseek-ai/dsh-mcp-client",
                    "config": self.settings.browser_mcp,
                }]})
            policy_file.write_text(json.dumps(patches), encoding="utf-8")
            self._proc = subprocess.Popen(
                [*self.settings.command, "--profile", self.settings.profile, "--patch", str(policy_file)],
                cwd=str(self.settings.workdir),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            self.stop()
            raise DshError(f"无法启动 dsh：{exc}") from exc
        self._stderr_tail = []
        self._reader = threading.Thread(target=self._read_stdout, name="dsh-stdout", daemon=True)
        self._stderr_reader = threading.Thread(target=self._read_stderr, name="dsh-stderr", daemon=True)
        self._reader.start()
        self._stderr_reader.start()
        try:
            result = self._request(
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}},
                },
                timeout=120.0,
            )
        except DshError as exc:
            # Startup failures (bad profile YAML, missing CLI, wrong node) end in a
            # dead process; the reason is on stderr, so surface its last line.
            time.sleep(0.3)
            hint = self._startup_hint()
            self.stop()
            detail = f"（{hint}）" if hint else ""
            raise DshError(f"dsh 启动失败：{exc}{detail}") from exc
        if not result.get("protocolVersion"):
            raise DshError("dsh 的 ACP 握手没有返回协议版本")
        self._initialized = True
        logger.info("dsh ACP 已就绪（%s，%s）", self.settings.profile, self.settings.permission_mode)

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        self._initialized = False
        self._live.clear()
        try:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=10)
        except Exception:  # noqa: BLE001 - best effort shutdown
            try:
                if proc is not None:
                    proc.kill()
            except Exception:  # noqa: BLE001
                pass
        finally:
            policy_dir, self._policy_dir = self._policy_dir, None
            if policy_dir is not None:
                policy_dir.cleanup()

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None and self._initialized

    def maybe_stop_idle(self) -> bool:
        if self._prompts or self._pending:
            return False
        if not self.alive():
            return False
        if self.settings.idle_seconds <= 0:
            return False
        if time.monotonic() - self._last_used < self.settings.idle_seconds:
            return False
        logger.info("dsh 空闲超过 %.0f 秒，关闭子进程", self.settings.idle_seconds)
        self.stop()
        return True

    # -- protocol ----------------------------------------------------------
    def _send(self, payload: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise DshError("dsh 进程未运行")
        line = json.dumps(payload, ensure_ascii=False)
        with self._write_lock:
            try:
                proc.stdin.write(line + "\n")
                proc.stdin.flush()
            except OSError as exc:
                raise DshError(f"写入 dsh 失败：{exc}") from exc

    def _request(self, method: str, params: dict, timeout: float) -> dict:
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            waiter: queue.Queue = queue.Queue()
            self._pending[request_id] = waiter
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        try:
            message = waiter.get(timeout=timeout)
        except queue.Empty as exc:
            if method == "session/prompt":
                # Keep the waiter registered until cancellation is acknowledged.
                # Otherwise late chunks can become the next turn's answer.
                self.cancel(params["sessionId"])
                try:
                    waiter.get(timeout=1.0)
                except queue.Empty:
                    self.stop()
            with self._lock:
                self._pending.pop(request_id, None)
            raise TimeoutError(f"{method} 超时") from exc
        if "error" in message:
            raise DshError(f"{method} 失败：{message['error']}")
        return message.get("result") or {}

    def _read_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                logger.debug("dsh 非协议输出：%s", line[:200])
                continue
            if "id" in message and ("result" in message or "error" in message):
                with self._lock:
                    waiter = self._pending.pop(int(message["id"]), None)
                if waiter is not None:
                    waiter.put(message)
            elif "id" in message and "method" in message:
                self._answer_request(message)
            else:
                self._handle_notification(message)
        self._fail_pending("dsh 进程已退出")

    def _read_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for line in proc.stderr:
            line = _ANSI.sub("", line.rstrip())
            if line:
                self._stderr_tail.append(line)
                del self._stderr_tail[:-20]

    def _fail_pending(self, reason: str) -> None:
        with self._lock:
            pending, self._pending = self._pending, {}
        self._live.clear()
        for waiter in pending.values():
            waiter.put({"error": {"code": -32000, "message": reason}})
        for prompt in list(self._prompts.values()):
            prompt.stop_reason = "error"
            prompt.done.set()
        self._initialized = False

    def _answer_request(self, message: dict) -> None:
        method = message.get("method", "")
        if method == "session/request_permission":
            params = message.get("params") or {}
            options = params.get("options") or []
            reject = next(
                (option for option in options if str(option.get("kind", "")).startswith("reject")),
                None,
            )
            title = (params.get("toolCall") or {}).get("title") or "tool"
            logger.warning("dsh 请求权限，已拒绝：%s", title)
            result = (
                {"outcome": {"outcome": "selected", "optionId": reject["optionId"]}}
                if reject
                else {"outcome": {"outcome": "cancelled"}}
            )
            self._send({"jsonrpc": "2.0", "id": message["id"], "result": result})
            return
        self._send(
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32601, "message": f"unsupported {method}"},
            }
        )

    def _handle_notification(self, message: dict) -> None:
        if message.get("method") != "session/update":
            return
        params = message.get("params") or {}
        session_id = params.get("sessionId") or ""
        prompt = self._prompts.get(session_id)
        if prompt is None:
            return
        update = params.get("update") or {}
        kind = update.get("sessionUpdate")
        content = update.get("content") or {}
        if kind == "agent_message_chunk" and content.get("type") == "text":
            prompt.text.append(content.get("text", ""))
        elif kind in {"tool_call", "tool_call_update"}:
            tool_id = update.get("toolCallId") or str(len(prompt.tools))
            entry = prompt.tools.setdefault(tool_id, {})
            if update.get("title"):
                entry["title"] = update["title"]
            if update.get("kind"):
                entry["kind"] = update["kind"]
            if update.get("status"):
                entry["status"] = update["status"]

    # -- sessions ----------------------------------------------------------
    def new_session(self) -> str:
        self.start()
        result = self._request(
            "session/new",
            {"cwd": str(self.settings.workdir), "mcpServers": []},
            timeout=120.0,
        )
        session_id = result.get("sessionId")
        if not session_id:
            raise DshError("dsh 没有返回会话 id")
        session_id = str(session_id)
        self._live.add(session_id)
        self._last_used = time.monotonic()
        return session_id

    def resume_session(self, session_id: str) -> bool:
        """Adopt a persisted session after the dsh process was restarted."""
        if not session_id or not self.alive():
            return False
        if session_id in self._live:
            return True
        try:
            self._request(
                "session/resume",
                {
                    "sessionId": session_id,
                    "cwd": str(self.settings.workdir),
                    "mcpServers": [],
                },
                timeout=90.0,
            )
        except (DshError, TimeoutError):
            logger.info("恢复 dsh 会话失败，改用新会话：%s", session_id)
            return False
        self._live.add(session_id)
        self._last_used = time.monotonic()
        logger.info("已恢复 dsh 会话：%s", session_id)
        return True

    def ensure_session(self, session_id: str) -> str:
        """Return a session this process can prompt: resume the stored one or start new."""
        self.start()
        if session_id and session_id in self._live:
            return session_id
        if session_id and self.resume_session(session_id):
            return session_id
        return self.new_session()

    def close_session(self, session_id: str) -> None:
        self._live.discard(session_id)
        if not self.alive():
            return
        try:
            self._request("session/close", {"sessionId": session_id}, timeout=15.0)
        except Exception:  # noqa: BLE001 - closing is best effort
            logger.debug("关闭 dsh 会话失败：%s", session_id)

    def prompt(self, session_id: str, text: str, images: list[ImagePart] | None = None) -> DshAnswer:
        self.start()
        session_id = self.ensure_session(session_id)
        blocks: list[dict] = [{"type": "text", "text": text}]
        blocks.extend(image.block() for image in (images or [])[:4])
        prompt = _Prompt()
        self._prompts[session_id] = prompt
        started = time.monotonic()
        try:
            try:
                result = self._request(
                    "session/prompt",
                    {"sessionId": session_id, "prompt": blocks},
                    timeout=self.settings.timeout,
                )
                prompt.stop_reason = str(result.get("stopReason") or "")
            except TimeoutError:
                logger.warning("dsh 回答超时，保留会话及已生成文字：%s", session_id)
                prompt.cancelled = True
                prompt.stop_reason = "timeout"
            except DshError as exc:
                if "unknown session" in str(exc):
                    # The process restarted between resume and prompt; retry once on a live session.
                    logger.warning("dsh 会话已失效，重试一次：%s", session_id)
                    self._live.discard(session_id)
                    self._prompts.pop(session_id, None)
                    session_id = self.new_session()
                    self._prompts[session_id] = prompt
                    result = self._request(
                        "session/prompt",
                        {"sessionId": session_id, "prompt": blocks},
                        timeout=self.settings.timeout,
                    )
                    prompt.stop_reason = str(result.get("stopReason") or "")
                else:
                    prompt.stop_reason = "error"
                    raise
            answer = clean_answer("".join(prompt.text))
            tools = [str(tool.get("title") or tool.get("kind") or "tool") for tool in prompt.tools.values()]
            return DshAnswer(
                text=answer,
                session_id=session_id,
                elapsed=time.monotonic() - started,
                tools=tools,
                cancelled=prompt.cancelled or "cancel" in prompt.stop_reason,
                stop_reason=prompt.stop_reason,
            )
        finally:
            self._prompts.pop(session_id, None)
            self._last_used = time.monotonic()

    def cancel(self, session_id: str) -> None:
        if not self.alive():
            return
        try:
            self._send({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": session_id}})
        except DshError:
            return
        prompt = self._prompts.get(session_id)
        if prompt is not None:
            prompt.cancelled = True

    def diagnostics(self) -> str:
        return "\n".join(self._stderr_tail[-6:])

    def _startup_hint(self) -> str:
        """Pick the one stderr line that explains why the process died."""
        markers = ("Error:", "YAMLException", "failed to parse", "Cannot find", "ERR_", "SyntaxError")
        for line in reversed(self._stderr_tail):
            if any(marker in line for marker in markers):
                return line.strip()[:200]
        return self._stderr_tail[-1].strip()[:200] if self._stderr_tail else ""
