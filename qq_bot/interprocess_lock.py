from __future__ import annotations

import asyncio
import os
import tempfile
import time
from pathlib import Path


class InterProcessFileLock:
    """A small cross-platform advisory lock backed by a lock file.

    Windows uses the CRT byte-range lock and Unix-like systems use ``flock``.
    The lock file is deliberately separate from the protected data file so a
    process can atomically replace the data file while the lock is held.
    Operating-system locks are released automatically if a process exits.
    """

    def __init__(
        self,
        path: Path,
        *,
        timeout: float = 45.0,
        poll_interval: float = 0.1,
    ) -> None:
        self.path = Path(path)
        self.timeout = max(float(timeout), 0.0)
        self.poll_interval = max(float(poll_interval), 0.01)
        self._handle = None

    def _try_acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        handle = self.path.open("r+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ImportError):
            handle.close()
            return False
        self._handle = handle
        return True

    def acquire(self) -> None:
        deadline = time.monotonic() + self.timeout
        while True:
            if self._try_acquire():
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for lock: {self.path}")
            time.sleep(min(self.poll_interval, max(deadline - time.monotonic(), 0.0)))

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "InterProcessFileLock":
        self.acquire()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.release()


class AsyncInterProcessFileLock:
    """Non-blocking-to-the-event-loop wrapper around :class:`InterProcessFileLock`."""

    def __init__(
        self,
        path: Path,
        *,
        timeout: float = 45.0,
        poll_interval: float = 0.1,
    ) -> None:
        self._lock = InterProcessFileLock(
            path,
            timeout=timeout,
            poll_interval=poll_interval,
        )

    async def __aenter__(self) -> "AsyncInterProcessFileLock":
        deadline = time.monotonic() + self._lock.timeout
        while True:
            if self._lock._try_acquire():
                return self
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Timed out waiting for lock: {self._lock.path}")
            await asyncio.sleep(min(self._lock.poll_interval, remaining))

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self._lock.release()


def lock_path_for(path: Path) -> Path:
    """Return the shared lock path for a protected file."""

    path = Path(path)
    return path.with_name(path.name + ".lock")


def atomic_write_text(path: Path, value: str) -> None:
    """Write UTF-8 text and atomically replace ``path`` in the same directory."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
