import asyncio
import subprocess
import sys
from pathlib import Path

from qq_bot.interprocess_lock import (
    AsyncInterProcessFileLock,
    InterProcessFileLock,
    atomic_write_text,
    lock_path_for,
)


def test_atomic_write_text_replaces_file_and_uses_distinct_lock_path(tmp_path: Path) -> None:
    target = tmp_path / "tokens.json"

    atomic_write_text(target, '{"version": 1}')
    atomic_write_text(target, '{"version": 2}')

    assert target.read_text(encoding="utf-8") == '{"version": 2}'
    assert lock_path_for(target) == tmp_path / "tokens.json.lock"


def test_lock_blocks_a_second_process_until_released(tmp_path: Path) -> None:
    lock_path = tmp_path / "tokens.json.lock"
    lock = InterProcessFileLock(lock_path, timeout=1)
    lock.acquire()
    try:
        probe = (
            "from pathlib import Path; "
            "from qq_bot.interprocess_lock import InterProcessFileLock; "
            f"lock=InterProcessFileLock(Path({str(lock_path)!r}), timeout=0.2); "
            "lock.acquire()"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode != 0
        assert "Timed out waiting for lock" in result.stderr
    finally:
        lock.release()


def test_async_lock_releases_after_exception(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "tokens.json.lock"
        try:
            async with AsyncInterProcessFileLock(path, timeout=1):
                raise RuntimeError("probe")
        except RuntimeError:
            pass
        async with AsyncInterProcessFileLock(path, timeout=1):
            return

    asyncio.run(scenario())
