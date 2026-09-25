from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Iterable, Literal


JobStatus = Literal["queued", "downloading", "completed", "failed"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Job:
    id: str
    source_url: str
    status: JobStatus = "queued"
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    file_path: Path | None = None
    error: str | None = None
    log_tail: str = ""

    def public(self) -> dict[str, object]:
        return {
            "id": self.id,
            "source_url": self.source_url,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "filename": self.file_path.name if self.file_path else None,
            "size_bytes": self.file_path.stat().st_size if self.file_path and self.file_path.is_file() else None,
            "error": self.error,
            "download_url": f"/api/jobs/{self.id}/file" if self.file_path else None,
        }


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = Lock()

    def add(self, job: Job, *, max_pending: int | None = None) -> bool:
        """Add a job, optionally enforcing a pending-job cap atomically.

        ``ThreadPoolExecutor`` has an unbounded internal work queue.  The
        store is therefore the single place where admission is controlled so
        concurrent HTTP requests cannot both pass a separate count check and
        overfill the queue.
        """
        with self._lock:
            if max_pending is not None:
                pending = sum(
                    1 for existing in self._jobs.values()
                    if existing.status in {"queued", "downloading"}
                )
                if pending >= max_pending:
                    return False
            self._jobs[job.id] = job
            return True

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def update(self, job_id: str, **changes: object) -> Job:
        with self._lock:
            job = self._jobs[job_id]
            for key, value in changes.items():
                setattr(job, key, value)
            job.updated_at = utc_now()
            return job

    def remove(self, job_id: str) -> Job | None:
        """Remove and return a job, if it exists."""
        with self._lock:
            return self._jobs.pop(job_id, None)

    def remove_expired(
        self,
        ttl_seconds: float,
        *,
        now: datetime | None = None,
        exclude: Iterable[str] = (),
    ) -> list[Job]:
        """Remove terminal jobs older than ``ttl_seconds``.

        Active jobs are never eligible.  ``exclude`` lets the application
        protect a job whose executor Future is still in its completion
        callback, avoiding a cleanup/delete race.
        """
        if ttl_seconds <= 0:
            return []
        cutoff = (now or datetime.now(timezone.utc)).timestamp() - ttl_seconds
        excluded = set(exclude)
        removed: list[Job] = []
        with self._lock:
            for job_id, job in list(self._jobs.items()):
                if job_id in excluded or job.status not in {"completed", "failed"}:
                    continue
                try:
                    updated_at = datetime.fromisoformat(job.updated_at)
                except (TypeError, ValueError):
                    continue
                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=timezone.utc)
                if updated_at.timestamp() <= cutoff:
                    removed.append(self._jobs.pop(job_id))
        return removed
