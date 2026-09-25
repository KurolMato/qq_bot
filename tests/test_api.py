import asyncio
from concurrent.futures import Future
from contextlib import suppress
from threading import Event
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app, store
from app.models import Job


client = TestClient(app, client=("127.0.0.1", 50000))


def _finished_future(*_args, **_kwargs) -> Future[None]:
    future: Future[None] = Future()
    future.set_result(None)
    return future


def test_periodic_cleanup_keeps_event_loop_responsive(monkeypatch) -> None:
    import app.main as main

    release = Event()
    finished = Event()

    async def exercise() -> None:
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        calls = []

        def slow_cleanup() -> int:
            calls.append(True)
            loop.call_soon_threadsafe(started.set)
            try:
                release.wait(timeout=2)
            finally:
                finished.set()
            return 0

        monkeypatch.setattr(main, "JOB_CLEANUP_INTERVAL_SECONDS", 0)
        monkeypatch.setattr(main, "_cleanup_expired_jobs", slow_cleanup)
        task = asyncio.create_task(main._job_cleanup_loop())
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            # A blocked event loop can only get here after slow_cleanup returns.
            assert not finished.is_set()
            for _ in range(5):
                await asyncio.sleep(0)
            assert len(calls) == 1
        finally:
            release.set()
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    asyncio.run(exercise())


def test_periodic_cleanup_recovers_after_failure_and_can_be_cancelled(monkeypatch) -> None:
    import app.main as main

    async def exercise() -> None:
        loop = asyncio.get_running_loop()
        recovered = asyncio.Event()
        calls = []

        def cleanup() -> int:
            calls.append(True)
            if len(calls) == 1:
                raise OSError("temporary cleanup failure")
            loop.call_soon_threadsafe(recovered.set)
            return 0

        monkeypatch.setattr(main, "JOB_CLEANUP_INTERVAL_SECONDS", 0)
        monkeypatch.setattr(main, "_cleanup_expired_jobs", cleanup)
        task = asyncio.create_task(main._job_cleanup_loop())
        try:
            await asyncio.wait_for(recovered.wait(), timeout=5)
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        assert task.cancelled()
        assert len(calls) >= 2

    asyncio.run(exercise())


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_homepage() -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "B站 / X / 抖音 / 小红书 / 小黑盒转 MP4" in response.text


def test_rejects_unsupported_job() -> None:
    response = client.post("/api/jobs", json={"url": "https://example.com/video.mp4"})
    assert response.status_code == 422
    assert "仅支持" in response.json()["detail"]


def test_accepts_x_job() -> None:
    with patch("app.main.executor.submit", side_effect=_finished_future):
        response = client.post("/api/jobs", json={"url": "https://x.com/example/status/1234567890"})
    assert response.status_code == 202
    store.remove(response.json()["id"])


def test_accepts_xiaoheihe_job() -> None:
    with patch("app.main.executor.submit", side_effect=_finished_future):
        response = client.post(
            "/api/jobs",
            json={
                "url": "https://api.xiaoheihe.cn/v3/bbs/app/api/web/share?link_id=5a28725c3264"
            },
        )
        assert response.status_code == 202
        store.remove(response.json()["id"])


def test_accepts_xiaohongshu_job() -> None:
    with patch("app.main.executor.submit", side_effect=_finished_future):
        response = client.post(
            "/api/jobs",
            json={"url": "https://xhslink.com/a/qsoHVeD0Liw1"},
        )
    assert response.status_code == 202
    store.remove(response.json()["id"])


def test_accepts_xiaohongshu_cn_job() -> None:
    with patch("app.main.executor.submit", side_effect=_finished_future):
        response = client.post(
            "/api/jobs",
            json={"url": "https://xhslink.cn/o/2wom7IQq1d2"},
        )
    assert response.status_code == 202
    store.remove(response.json()["id"])


def test_unknown_job() -> None:
    assert client.get("/api/jobs/not-found").status_code == 404


def test_delete_completed_job_removes_local_file(
    tmp_path, monkeypatch
) -> None:
    import app.main as main

    job_id = "delete-after-delivery-test"
    job_dir = tmp_path / job_id
    job_dir.mkdir()
    video = job_dir / "video.mp4"
    video.write_bytes(b"video")
    store.add(Job(id=job_id, source_url="https://x.com/a/status/1", status="completed", file_path=video))
    monkeypatch.setattr(main, "DOWNLOAD_DIR", tmp_path)

    response = client.delete(f"/api/jobs/{job_id}")

    assert response.status_code == 204
    assert not job_dir.exists()
    assert store.get(job_id) is None


def test_video_api_rejects_non_loopback_without_shared_token(monkeypatch) -> None:
    import app.main as main

    monkeypatch.setattr(main, "VIDEO_API_TOKEN", "shared-token")
    remote_client = TestClient(app, client=("192.0.2.10", 50000))

    denied = remote_client.get("/api/jobs/not-found")
    assert denied.status_code == 403
    allowed = remote_client.get(
        "/api/jobs/not-found",
        headers={"X-Video-API-Token": "shared-token"},
    )
    assert allowed.status_code == 404


def test_video_api_accepts_basic_auth_for_remote_bot_url(monkeypatch) -> None:
    import app.main as main
    import base64

    monkeypatch.setattr(main, "VIDEO_API_TOKEN", "shared-token")
    remote_client = TestClient(app, client=("192.0.2.11", 50000))
    encoded = base64.b64encode(b":shared-token").decode("ascii")

    response = remote_client.get(
        "/api/jobs/not-found",
        headers={"Authorization": f"Basic {encoded}"},
    )
    assert response.status_code == 404


def test_queue_limit_rejects_new_jobs_and_queued_delete_cancels_future(
    tmp_path, monkeypatch
) -> None:
    import app.main as main

    monkeypatch.setattr(main, "MAX_QUEUE_SIZE", 1)
    monkeypatch.setattr(main, "DOWNLOAD_DIR", tmp_path)
    pending_future: Future[None] = Future()
    with patch("app.main.executor.submit", return_value=pending_future):
        first = client.post("/api/jobs", json={"url": "https://x.com/a/status/1"})
        second = client.post("/api/jobs", json={"url": "https://x.com/a/status/2"})
        assert first.status_code == 202
        assert second.status_code == 429

        deleted = client.delete(f"/api/jobs/{first.json()['id']}")

    assert deleted.status_code == 204
    assert pending_future.cancelled()
    assert client.get(f"/api/jobs/{first.json()['id']}").status_code == 404


def test_run_job_converts_unexpected_worker_exception_to_failed_job(tmp_path, monkeypatch) -> None:
    import app.main as main

    job_id = "unexpected-worker-error"
    store.add(Job(id=job_id, source_url="https://x.com/a/status/1"))
    monkeypatch.setattr(main, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(main, "download_to_mp4", lambda *args: (_ for _ in ()).throw(RuntimeError("boom")))

    main.run_job(job_id, "https://x.com/a/status/1")

    job = store.get(job_id)
    assert job is not None
    assert job.status == "failed"
    assert job.error == "转换失败"
    store.remove(job_id)
