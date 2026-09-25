import json
from pathlib import Path

from qq_bot.napcat_watchdog import (
    bot_restart_recent,
    heartbeat_state,
    listener_pid,
    onebot_state,
    runtime_healthy,
)


def test_onebot_state_detects_listener_and_connection() -> None:
    output = """
      TCP    127.0.0.1:8081    0.0.0.0:0          LISTENING       100
      TCP    127.0.0.1:8081    127.0.0.1:45084    ESTABLISHED     100
    """
    assert onebot_state(output) == (True, True)


def test_onebot_state_detects_disconnected_server() -> None:
    output = "TCP    127.0.0.1:8081    0.0.0.0:0    LISTENING    100"
    assert onebot_state(output) == (True, False)


def test_stale_heartbeat_fails_even_while_tcp_is_connected() -> None:
    output = """
      TCP    127.0.0.1:8081    0.0.0.0:0          LISTENING       100
      TCP    127.0.0.1:8081    127.0.0.1:45084    ESTABLISHED     100
    """
    assert runtime_healthy(False, False, output) is False
    assert runtime_healthy(False, True, output) is False
    assert runtime_healthy(True, False, output) is False
    assert runtime_healthy(True, True, output) is True
    assert runtime_healthy(False, False, "") is False


def test_listener_pid_reads_napcat_webui_owner() -> None:
    output = "TCP    0.0.0.0:6099    0.0.0.0:0    LISTENING    27892"
    assert listener_pid(output) == 27892


def test_fresh_application_heartbeat_reports_real_onebot_state(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    path.write_text(
        json.dumps(
            {"updated_at": 1000.0, "pid": 123, "onebot_connected": True}
        ),
        encoding="utf-8",
    )
    assert heartbeat_state(path, now=1010.0, max_age=20.0) == (True, True, 123)


def test_stale_or_malformed_heartbeat_is_not_healthy(tmp_path: Path) -> None:
    stale = tmp_path / "stale.json"
    stale.write_text(
        json.dumps(
            {"updated_at": 1000.0, "pid": 123, "onebot_connected": True}
        ),
        encoding="utf-8",
    )
    assert heartbeat_state(stale, now=1030.0, max_age=20.0) == (False, True, 123)
    assert heartbeat_state(tmp_path / "missing.json", now=1030.0) == (False, False, None)


def test_recent_restart_suppresses_duplicate_watchdog_restart(tmp_path: Path) -> None:
    state = tmp_path / "restart.json"
    state.write_text('{"restarted_at": 1000.0}', encoding="utf-8")

    assert bot_restart_recent(state, now=1100.0, cooldown=180.0) is True
    assert bot_restart_recent(state, now=1200.0, cooldown=180.0) is False


def test_napcat_identity_requires_installation_evidence(tmp_path):
    from qq_bot.napcat_watchdog import napcat_identity
    launcher = tmp_path / "NapCat" / "launcher.bat"
    assert napcat_identity({"Name": "NapCatWinBootMain.exe", "ExecutablePath": str(launcher.parent / "NapCatWinBootMain.exe")}, launcher)
    assert napcat_identity({"Name": "QQ.exe", "CommandLine": f'qq.exe "{launcher.parent / "napcat.js"}"'}, launcher)
    assert not napcat_identity({"Name": "QQ.exe", "ExecutablePath": "C:/QQ/QQ.exe"}, launcher)
    assert not napcat_identity({"Name": "node.exe", "CommandLine": "node unrelated.js"}, launcher)
    assert napcat_identity(
        {"Name": "QQ.exe", "ExecutablePath": "C:/QQ/QQ.exe"},
        launcher,
        owns_webui_port=True,
    )


def test_bot_identity_accepts_venv_command_when_windows_reports_base_python(tmp_path):
    from qq_bot.napcat_watchdog import bot_process_identity
    root = tmp_path / "video_analysis"
    venv_python = root / ".venvs" / "LAPTOP" / "Scripts" / "python.exe"
    info = {
        "Name": "python.exe",
        "ExecutablePath": r"C:\Program Files\Python312\python.exe",
        "CommandLine": f'"{venv_python}" -m qq_bot.run',
    }
    assert bot_process_identity(info, root)


def test_bot_identity_rejects_other_checkout_and_unrelated_python(tmp_path):
    from qq_bot.napcat_watchdog import bot_process_identity
    root = tmp_path / "current"
    other = tmp_path / "old" / ".venv" / "Scripts" / "python.exe"
    assert not bot_process_identity({
        "Name": "python.exe", "ExecutablePath": str(other),
        "CommandLine": f'"{other}" -m qq_bot.run',
    }, root)
    assert not bot_process_identity({
        "Name": "python.exe", "ExecutablePath": str(root / ".venv" / "Scripts" / "python.exe"),
        "CommandLine": 'python -m uvicorn app.main:app',
    }, root)


def test_bot_identity_accepts_matching_project_heartbeat_pid(monkeypatch):
    from qq_bot import napcat_watchdog as w

    monkeypatch.setattr(w, "_process_info", lambda process_id: {
        "Name": "python.exe",
        "ExecutablePath": r"C:\Program Files\Python312\python.exe",
        "CommandLine": 'python.exe -m qq_bot.run',
    })

    assert w._is_python_process(12020, 12020) is True
    assert w._is_python_process(12020, 99999) is False


def test_recovery_permission_failure_does_not_kill(monkeypatch, tmp_path):
    import logging
    from qq_bot import napcat_watchdog as w
    monkeypatch.setattr(w, "is_admin", lambda: False)
    monkeypatch.setattr(w, "_netstat", lambda: (_ for _ in ()).throw(AssertionError("must not inspect/kill")))
    assert w.restart_napcat(tmp_path / "launcher.bat", "", logging.getLogger()) is False


def test_watchdog_stale_connected_retries_bot_without_restarting_napcat(monkeypatch, tmp_path):
    import logging
    import pytest
    from qq_bot import napcat_watchdog as w
    clock = [0.0]
    attempts = []
    class EndSimulation(Exception):
        pass
    def sleep(seconds):
        clock[0] += seconds
        if clock[0] > 700:
            raise EndSimulation
    def bot(*args, **kwargs):
        attempts.append(("bot", clock[0]))
        raise PermissionError("simulated")
    def napcat(*args):
        attempts.append(("napcat", clock[0]))
        return False
    launcher = tmp_path / "launcher.bat"
    launcher.touch()
    monkeypatch.setattr(w, "_logger", lambda: logging.getLogger("test"))
    monkeypatch.setattr(w, "_single_instance", lambda: True)
    monkeypatch.setattr(w, "heartbeat_state", lambda **kw: (False, True, 1))
    monkeypatch.setattr(w, "bot_restart_recent", lambda: False)
    monkeypatch.setattr(w, "restart_bot", bot)
    monkeypatch.setattr(w, "restart_napcat", napcat)
    monkeypatch.setattr(w.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(w.time, "sleep", sleep)
    with pytest.raises(EndSimulation):
        w.run(launcher, "")
    assert attempts[:2] == [("bot", 90), ("bot", 390)]
    assert all(action == "bot" for action, _ in attempts)
    assert len(attempts) <= 4


def test_watchdog_disconnected_escalates_from_bot_to_napcat(monkeypatch, tmp_path):
    import logging
    import pytest
    from qq_bot import napcat_watchdog as w

    clock = [0.0]
    attempts = []

    class EndSimulation(Exception):
        pass

    def sleep(seconds):
        clock[0] += seconds
        if clock[0] > 200:
            raise EndSimulation

    def bot(*args, **kwargs):
        attempts.append(("bot", clock[0]))
        return False

    def napcat(*args, **kwargs):
        attempts.append(("napcat", clock[0]))
        return False

    launcher = tmp_path / "launcher.bat"
    launcher.touch()
    monkeypatch.setattr(w, "_logger", lambda: logging.getLogger("test-disconnected"))
    monkeypatch.setattr(w, "_single_instance", lambda: True)
    monkeypatch.setattr(w, "heartbeat_state", lambda **kw: (False, False, 1))
    monkeypatch.setattr(w, "bot_restart_recent", lambda: False)
    monkeypatch.setattr(w, "restart_bot", bot)
    monkeypatch.setattr(w, "restart_napcat", napcat)
    monkeypatch.setattr(w.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(w.time, "sleep", sleep)

    with pytest.raises(EndSimulation):
        w.run(launcher, "")

    assert attempts[:2] == [("bot", 90), ("napcat", 180)]
