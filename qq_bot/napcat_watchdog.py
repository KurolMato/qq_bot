from __future__ import annotations

import argparse
import ctypes
import json
import locale
import logging
import os
import subprocess
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from .atomic_json import write_json


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = PROJECT_ROOT / "data" / "napcat-watchdog.log"
HEARTBEAT_PATH = PROJECT_ROOT / "data" / "qq-runtime-heartbeat.json"
BOT_RESTART_STATE_PATH = PROJECT_ROOT / "data" / "qq-bot-last-restart.json"
MUTEX_NAME = "Local\\VideoAnalysisNapCatWatchdog"
ERROR_ALREADY_EXISTS = 183
CREATE_NO_WINDOW = 0x08000000
_mutex_handle = None


def _logger() -> logging.Logger:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("napcat-watchdog")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(
            LOG_PATH, maxBytes=512 * 1024, backupCount=2, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


def _single_instance(name: str = MUTEX_NAME) -> bool:
    global _mutex_handle
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        return False
    _mutex_handle = handle
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def _netstat() -> str:
    try:
        return subprocess.check_output(
            ["netstat", "-ano"],
            timeout=10,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return ""


def onebot_state(output: str, port: int = 8081) -> tuple[bool, bool]:
    """Return (server_listening, napcat_connected) from netstat output."""
    listening = False
    connected = False
    suffix = f":{port}"
    for line in output.splitlines():
        columns = line.split()
        if len(columns) < 4 or not columns[1].endswith(suffix):
            continue
        state = columns[3].upper()
        listening = listening or state == "LISTENING"
        connected = connected or state == "ESTABLISHED"
    return listening, connected


def runtime_healthy(fresh: bool, connected: bool, network_output: str) -> bool:
    # TCP can remain established indefinitely while the event loop is frozen.
    return fresh and connected


def listener_pid(output: str, port: int = 6099) -> int | None:
    suffix = f":{port}"
    for line in output.splitlines():
        columns = line.split()
        if len(columns) >= 5 and columns[1].endswith(suffix) and columns[3].upper() == "LISTENING":
            try:
                return int(columns[4])
            except ValueError:
                return None
    return None


def heartbeat_state(
    path: Path = HEARTBEAT_PATH,
    *,
    now: float | None = None,
    max_age: float = 20.0,
) -> tuple[bool, bool, int | None]:
    """Return (fresh, OneBot connected, bot PID) from the application heartbeat."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return False, False, None
        updated_at = float(payload["updated_at"])
        process_id = int(payload["pid"])
        connected = payload.get("onebot_connected") is True
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return False, False, None
    current = time.time() if now is None else now
    fresh = 0 <= current - updated_at <= max_age
    return fresh, connected, process_id


def bot_restart_recent(
    path: Path = BOT_RESTART_STATE_PATH,
    *,
    now: float | None = None,
    cooldown: float = 180.0,
) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return False
        restarted_at = float(payload["restarted_at"])
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return False
    current = time.time() if now is None else now
    return 0 <= current - restarted_at < cooldown


def _record_bot_restart(path: Path = BOT_RESTART_STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, {"restarted_at": time.time(), "pid": os.getpid()})


def is_admin() -> bool:
    return os.name == "nt" and bool(ctypes.windll.shell32.IsUserAnAdmin())


def _process_info(process_id: int) -> dict:
    try:
        output = subprocess.check_output(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             f"[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; Get-CimInstance Win32_Process -Filter 'ProcessId={int(process_id)}' | Select-Object Name,ExecutablePath,CommandLine | ConvertTo-Json -Compress"],
            text=True, encoding="utf-8", errors="replace", timeout=10,
            creationflags=CREATE_NO_WINDOW,
        )
        value = json.loads(output.lstrip('\ufeff'))
        return value if isinstance(value, dict) else {}
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}


def napcat_identity(info: dict, launcher: Path, *, owns_webui_port: bool = False) -> bool:
    executable = str(info.get("ExecutablePath") or "")
    try:
        within = Path(executable).resolve().is_relative_to(launcher.resolve().parent)
    except (OSError, ValueError):
        within = False
    name = str(info.get("Name") or "").casefold()
    command = str(info.get("CommandLine") or "").casefold()
    command_in_install = str(launcher.resolve().parent).casefold() + os.sep in command
    return (owns_webui_port and name in {"qq.exe", "napcatwinbootmain.exe"} or
            within and name == "napcatwinbootmain.exe" or
            name in {"qq.exe", "node.exe"} and command_in_install and "napcat" in command)


def _is_napcat_process(process_id: int, launcher: Path) -> bool:
    # The injected NapCat WebUI normally listens from QQ.exe itself, whose
    # command line does not contain the NapCat installation directory.
    return napcat_identity(_process_info(process_id), launcher, owns_webui_port=True)


def bot_process_identity(
    info: dict,
    project_root: Path = PROJECT_ROOT,
    *,
    trusted_heartbeat: bool = False,
) -> bool:
    """Recognise the bot even when a Windows venv reports its base interpreter.

    Win32_Process.ExecutablePath may point at the system Python although argv[0]
    is the venv interpreter.  Requiring the module and project path in the
    command line keeps the kill check scoped to this checkout.
    """
    command = str(info.get("CommandLine") or "").casefold()
    if (str(info.get("Name") or "").casefold() not in {"python.exe", "pythonw.exe"}
            or "-m qq_bot.run" not in command):
        return False
    root = project_root.resolve()
    try:
        executable_in_project = Path(str(info.get("ExecutablePath") or "")).resolve().is_relative_to(root)
    except (OSError, ValueError):
        executable_in_project = False
    normalized_command = command.replace('/', os.sep)
    command_in_project = str(root).casefold().replace('/', os.sep) + os.sep in normalized_command
    return executable_in_project or command_in_project or trusted_heartbeat


def _is_python_process(process_id: int, expected_heartbeat_pid: int | None = None) -> bool:
    return bot_process_identity(
        _process_info(process_id),
        trusted_heartbeat=process_id == expected_heartbeat_pid,
    )


def restart_bot(
    logger: logging.Logger,
    *,
    reason: str = "watchdog",
    expected_heartbeat_pid: int | None = None,
) -> bool:
    output = _netstat()
    process_id = listener_pid(output, 8081)
    if process_id is not None:
        if expected_heartbeat_pid is None:
            _, _, expected_heartbeat_pid = heartbeat_state(max_age=float("inf"))
        if not _is_python_process(process_id, expected_heartbeat_pid):
            logger.error("Port 8081 PID %s is not a verified project QQ Bot; refusing restart", process_id)
            return False
        result = subprocess.run(
            ["taskkill", "/PID", str(process_id), "/F"],
            timeout=15,
            capture_output=True,
            text=True,
            encoding=locale.getpreferredencoding(False),
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        if result.returncode != 0:
            logger.error("Unable to stop QQ Bot PID %s: %s", process_id, result.stderr.strip())
            return False
        time.sleep(2)

    start_script = PROJECT_ROOT / "scripts" / "windows" / "start-qq-bot.cmd"
    if not start_script.is_file():
        logger.error("QQ Bot start script not found: %s", start_script)
        return False
    subprocess.Popen(
        [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(start_script)],
        cwd=str(PROJECT_ROOT),
        creationflags=CREATE_NO_WINDOW,
        close_fds=True,
    )
    _record_bot_restart()
    if reason == "watchdog":
        logger.warning("QQ Bot restart launched after stale or disconnected application heartbeat")
    else:
        logger.info("QQ Bot restart launched by management action")
    return True


def restart_napcat(launcher: Path, qq: str, logger: logging.Logger) -> bool:
    if not is_admin():
        logger.error("NapCat recovery requires administrator rights; restart the full bot suite as administrator")
        return False
    output = _netstat()
    process_id = listener_pid(output)
    if process_id is not None:
        if not _is_napcat_process(process_id, launcher):
            logger.error("Port 6099 belongs to PID %s, not NapCat; refusing restart", process_id)
            return False
        result = subprocess.run(
            ["taskkill", "/PID", str(process_id), "/F"],
            timeout=15,
            capture_output=True,
            text=True,
            encoding=locale.getpreferredencoding(False),
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        if result.returncode != 0:
            logger.error("Unable to stop NapCat PID %s: %s", process_id, result.stderr.strip())
            return False
        time.sleep(3)

    start_script = PROJECT_ROOT / "scripts" / "windows" / "start-napcat.cmd"
    command = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(start_script), str(launcher)]
    if qq:
        command.append(qq)
    with (PROJECT_ROOT / "data" / "napcat-launch.log").open("a", encoding="utf-8") as output:
        output.write(f"\n{time.strftime('%Y-%m-%d %H:%M:%S')} watchdog launching NapCat\n")
        output.flush()
        subprocess.Popen(command, cwd=str(PROJECT_ROOT), creationflags=CREATE_NO_WINDOW,
                         close_fds=True, stdout=output, stderr=subprocess.STDOUT)
    logger.warning("NapCat restart launched after persistent OneBot disconnect")
    return True


def run(launcher: Path, qq: str, *, poll_seconds: float = 10, grace_seconds: float = 90) -> None:
    logger = _logger()
    if not _single_instance():
        logger.info("Another NapCat watchdog is already running")
        return
    if not launcher.is_file():
        logger.error("NapCat launcher not found: %s", launcher)
        return

    unhealthy_at: float | None = None
    recovery_stage = 0
    healthy_samples = 0
    last_bot_restart = float("-inf")
    last_napcat_restart = float("-inf")
    last_report = float("-inf")
    logger.info("NapCat watchdog started")
    while True:
        now = time.monotonic()
        fresh, connected, _bot_pid = heartbeat_state(max_age=max(poll_seconds * 2.5, 20.0))
        healthy = runtime_healthy(fresh, connected, "")
        if now - last_report >= 60:
            logger.info("Watchdog alive: heartbeat_fresh=%s connected=%s bot_pid=%s recovery_stage=%s", fresh, connected, _bot_pid, recovery_stage)
            last_report = now
        if healthy:
            healthy_samples += 1
            # Require several consecutive application-level samples so a
            # five-second reconnect attempt cannot masquerade as recovery.
            if healthy_samples >= 3:
                if unhealthy_at is not None:
                    logger.info("OneBot application heartbeat recovered and remained stable")
                unhealthy_at = None
                recovery_stage = 0
        else:
            healthy_samples = 0
            if unhealthy_at is None:
                logger.warning("Application heartbeat unhealthy: fresh=%s connected=%s; grace=%ss", fresh, connected, grace_seconds)
                unhealthy_at = now
            unhealthy_for = now - unhealthy_at
            if unhealthy_for >= grace_seconds and recovery_stage == 0:
                if bot_restart_recent():
                    # An admin restart can happen while this watchdog already
                    # has an outage timer. Give the new process a fresh grace
                    # period instead of immediately restarting it a second time.
                    unhealthy_at = now
                elif now - last_bot_restart >= 300:
                    last_bot_restart = now
                    safe_recovery(
                        logger,
                        restart_bot,
                        logger,
                        expected_heartbeat_pid=_bot_pid,
                    )
                    recovery_stage = 1
                    unhealthy_at = now
            elif unhealthy_for >= grace_seconds and recovery_stage == 1:
                if not connected and now - last_napcat_restart >= 300:
                    last_napcat_restart = now
                    safe_recovery(logger, restart_napcat, launcher, qq, logger)
                    recovery_stage = 2
                    unhealthy_at = now
                elif connected and now - last_bot_restart >= 300:
                    # A live OneBot socket with a stale heartbeat means the bot
                    # is stuck. NapCat is healthy, so retry only the bot.
                    last_bot_restart = now
                    safe_recovery(
                        logger,
                        restart_bot,
                        logger,
                        expected_heartbeat_pid=_bot_pid,
                    )
                    unhealthy_at = now
            elif unhealthy_for >= 300 and recovery_stage == 2:
                # Start a new recovery cycle if both previous actions failed.
                recovery_stage = 0
                unhealthy_at = now
        time.sleep(max(poll_seconds, 5))


def safe_recovery(logger, action, *args, **kwargs):
    try:
        success = action(*args, **kwargs)
        if not success:
            logger.error("Recovery action %s failed; retry is rate limited", action.__name__)
        return success
    except Exception:
        logger.exception("Recovery action %s raised; watchdog will continue", action.__name__)
        return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--launcher", type=Path, required=True)
    parser.add_argument("--qq", default="")
    args = parser.parse_args()
    run(args.launcher.resolve(), args.qq.strip())


if __name__ == "__main__":
    main()
