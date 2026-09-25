from datetime import datetime, timedelta, timezone

from app.models import Job, JobStore


def test_job_store_remove_and_expire_only_terminal_jobs() -> None:
    store = JobStore()
    now = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
    old = (now - timedelta(hours=2)).isoformat()
    recent = (now - timedelta(seconds=30)).isoformat()
    store.add(Job("old", "https://x.com/old", status="failed", updated_at=old))
    store.add(Job("recent", "https://x.com/recent", status="completed", updated_at=recent))
    store.add(Job("active", "https://x.com/active", status="downloading", updated_at=old))

    removed = store.remove_expired(3600, now=now)

    assert [job.id for job in removed] == ["old"]
    assert store.get("old") is None
    assert store.get("recent") is not None
    assert store.get("active") is not None
    assert store.remove("recent") is not None
    assert store.remove("recent") is None
