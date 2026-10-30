import logging
import subprocess
from types import SimpleNamespace

import pytest

from qq_bot import restart_bot as helper
from qq_bot import napcat_watchdog as watchdog


@pytest.fixture(autouse=True)
def prevent_real_process_operations(monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("unmocked process launch"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("unmocked process termination"))
    monkeypatch.setattr(helper, "_logger", lambda: logging.getLogger("test-restart-helper"))
    monkeypatch.setattr(watchdog, "_record_bot_restart", lambda: None)


def test_detached_helper_uses_current_python_and_expected_pid(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: calls.append((a, kw)))
    helper.schedule_restart()
    args, options = calls[0]
    command = args[0]
    assert command[:3] == [helper.sys.executable, "-m", "qq_bot.restart_bot"]
    assert command[command.index("--expected-pid") + 1] == str(helper.os.getpid())
    assert command[command.index("--delay") + 1] == "3"
    assert options["cwd"] == str(helper.PROJECT_ROOT)
    assert options["creationflags"] & subprocess.DETACHED_PROCESS
    assert options["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
    assert options["stdin"] == options["stdout"] == options["stderr"] == subprocess.DEVNULL


def test_helper_delays_then_restarts_only_expected_process(monkeypatch):
    order = []
    monkeypatch.setattr(helper.time, "sleep", lambda seconds: order.append(("delay", seconds)))
    def restart(logger, **kwargs):
        order.append(("restart", kwargs))
        return True
    monkeypatch.setattr(helper, "restart_bot", restart)
    assert helper.main(["--delay", "3", "--expected-pid", "42"]) == 0
    assert order == [("delay", 3), ("restart", {"reason": "manual", "expected_process_id": 42})]


@pytest.mark.parametrize("failure", [False, OSError("launch failed")])
def test_helper_reports_failure(monkeypatch, failure):
    def restart(*a, **kw):
        if isinstance(failure, Exception):
            raise failure
        return failure
    monkeypatch.setattr(helper, "restart_bot", restart)
    assert helper.main([]) == 1


@pytest.mark.parametrize("pid", [None, 99])
def test_requested_restart_does_not_touch_replacement_process(monkeypatch, pid):
    monkeypatch.setattr(watchdog, "_netstat", lambda: "mock")
    monkeypatch.setattr(watchdog, "listener_pid", lambda *args: pid)
    assert watchdog.restart_bot(logging.getLogger("test-restart"), expected_process_id=42) is False


def test_requested_restart_verifies_and_replaces_matching_bot(monkeypatch, tmp_path):
    script = tmp_path / "scripts" / "windows" / "start-qq-bot.cmd"
    script.parent.mkdir(parents=True)
    script.touch()
    monkeypatch.setattr(watchdog, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(watchdog, "_netstat", lambda: "mock")
    monkeypatch.setattr(watchdog, "listener_pid", lambda *a: 42)
    monkeypatch.setattr(watchdog, "heartbeat_state", lambda **kw: (True, True, 42))
    identities = []
    monkeypatch.setattr(watchdog, "_is_python_process", lambda *args: identities.append(args) or True)
    stopped, launched = [], []
    monkeypatch.setattr(subprocess, "run", lambda command, **kw: stopped.append(command) or SimpleNamespace(returncode=0))
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kw: launched.append(command))
    monkeypatch.setattr(watchdog.time, "sleep", lambda _: None)
    assert watchdog.restart_bot(logging.getLogger("test-restart"), expected_process_id=42)
    assert identities == [(42, 42)]
    assert stopped == [["taskkill", "/PID", "42", "/F"]]
    assert launched[0][-1] == str(script)
