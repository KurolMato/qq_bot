import json
from pathlib import Path

from qq_bot import watchdog
from qq_bot.health import mark_failure, mark_success


def test_runtime_heartbeat_contains_bot_status(tmp_path: Path, monkeypatch) -> None:
    heartbeat = tmp_path / "heartbeat.json"
    monkeypatch.setattr(watchdog, "HEARTBEAT_PATH", heartbeat)
    mark_success("qq", "已连接")
    mark_failure("steam", "服务超时")

    watchdog._write_heartbeat(connected=True)

    payload = json.loads(heartbeat.read_text(encoding="utf-8"))
    assert payload["onebot_connected"] is True
    assert payload["components"]["qq"]["detail"] == "已连接"
    assert payload["components"]["steam"]["state"] == "error"
    assert isinstance(payload["uptime"], str)


def test_atomic_heartbeat_retries_permission_and_cleans_temp(tmp_path, monkeypatch):
    from qq_bot import atomic_json
    path = tmp_path / "heartbeat.json"
    original = Path.replace
    attempts = []
    def replace(self, target):
        attempts.append(self)
        if len(attempts) < 3:
            raise PermissionError("locked")
        return original(self, target)
    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr(atomic_json.time, "sleep", lambda _: None)
    atomic_json.write_json(path, {"ok": True})
    assert len(attempts) == 3
    assert json.loads(path.read_text()) == {"ok": True}
    assert not list(tmp_path.glob("*.tmp"))


def test_permanent_write_failure_preserves_old_and_task_survives(tmp_path, monkeypatch):
    import asyncio
    from qq_bot import atomic_json
    path = tmp_path / "heartbeat.json"
    path.write_text('{"old": true}')
    monkeypatch.setattr(watchdog, "HEARTBEAT_PATH", path)
    def fail(*args):
        raise PermissionError("locked")
    monkeypatch.setattr(Path, "replace", fail)
    monkeypatch.setattr(atomic_json.time, "sleep", lambda _: None)
    runtime = watchdog.RuntimeWatchdog()
    asyncio.run(runtime.write_heartbeat(True))
    assert json.loads(path.read_text()) == {"old": True}
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_concurrent_writers(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from qq_bot.atomic_json import write_json
    path = tmp_path / "heartbeat.json"
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda n: write_json(path, {"n": n}), range(12)))
    assert json.loads(path.read_text())["n"] in range(12)
    assert not list(tmp_path.glob("*.tmp"))
