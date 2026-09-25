from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import ipaddress
import logging
import os
import shutil
import subprocess
import uuid
from contextlib import asynccontextmanager, suppress
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import RLock

from fastapi import FastAPI, HTTPException, status
from fastapi import Depends, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

from .admin import (
    ConfigUpdate,
    GameCalendarUpdate,
    SecretUpdate,
    admin_game_calendar,
    admin_page,
    admin_logs,
    admin_status,
    delete_game_calendar_entry,
    game_calendar_image,
    launch_action,
    require_admin,
    save_game_calendar_entry,
    save_secret,
    update_config,
)
from .downloader import DownloadError, NoVideoError, download_to_mp4, normalize_video_url
from .models import Job, JobStore


logger = logging.getLogger(__name__)
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", BASE_DIR / "downloads")).resolve()
MAX_WORKERS = max(1, min(int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2")), 8))
TIMEOUT_SECONDS = int(os.getenv("DOWNLOAD_TIMEOUT_SECONDS", "3600"))
MAX_QUEUE_SIZE = max(1, min(int(os.getenv("MAX_QUEUE_SIZE", "16")), 1000))
JOB_TTL_SECONDS = max(int(os.getenv("JOB_TTL_SECONDS", str(6 * 60 * 60))), 60)
JOB_CLEANUP_INTERVAL_SECONDS = max(
    int(os.getenv("JOB_CLEANUP_INTERVAL_SECONDS", "300")), 60
)
VIDEO_API_TOKEN = os.getenv("VIDEO_API_TOKEN", "").strip()

store = JobStore()
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="video-download")
_job_futures: dict[str, Future[None]] = {}
_job_futures_lock = RLock()


def _cleanup_job_dir(job_id: str) -> None:
    shutil.rmtree(DOWNLOAD_DIR / job_id, ignore_errors=True)


def _active_future_ids() -> set[str]:
    with _job_futures_lock:
        return set(_job_futures)


def _cleanup_expired_jobs() -> int:
    removed = store.remove_expired(
        JOB_TTL_SECONDS,
        exclude=_active_future_ids(),
    )
    for job in removed:
        _cleanup_job_dir(job.id)
    if removed:
        logger.info("Removed %d expired video job(s)", len(removed))
    return len(removed)


async def _job_cleanup_loop() -> None:
    while True:
        await asyncio.sleep(JOB_CLEANUP_INTERVAL_SECONDS)
        try:
            # Large media directories can take time to remove; keep API requests
            # responsive and await completion before scheduling the next cleanup.
            await asyncio.to_thread(_cleanup_expired_jobs)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Video job cleanup failed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    # 服务上次异常退出时可能留下任务目录。启动时没有运行中的任务，
    # 因此可以安全清空这些临时文件。
    for child in DOWNLOAD_DIR.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
    cleanup_task = asyncio.create_task(_job_cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task
        executor.shutdown(wait=False, cancel_futures=False)


app = FastAPI(title="Bilibili / X / 抖音 / 小红书 / 小黑盒 MP4 下载服务", version="0.5.0", lifespan=lifespan)


class CreateJobRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


def run_job(job_id: str, url: str) -> None:
    job_dir = DOWNLOAD_DIR / job_id
    try:
        store.update(job_id, status="downloading")
        file_path, logs = download_to_mp4(url, job_dir, TIMEOUT_SECONDS)
        store.update(job_id, status="completed", file_path=file_path, log_tail=logs)
    except subprocess.TimeoutExpired:  # type: ignore[name-defined]
        _mark_job_failed(job_id, "下载超时")
    except NoVideoError:
        _mark_job_failed(job_id, "NO_VIDEO")
    except (DownloadError, OSError) as exc:
        _mark_job_failed(job_id, str(exc) or "下载失败")
    except Exception:
        # A decoder/Pillow or dependency error must not escape the worker:
        # otherwise the job remains in ``downloading`` forever and the API
        # never gives the QQ bot a terminal state.
        logger.exception("Unhandled video job failure: %s", job_id)
        _mark_job_failed(job_id, "转换失败")
    finally:
        job = store.get(job_id)
        if job is None or job.status != "completed":
            _cleanup_job_dir(job_id)


def _mark_job_failed(job_id: str, error: str) -> None:
    try:
        store.update(job_id, status="failed", error=error)
    except KeyError:
        # The only normal reason for this is a queued Future being cancelled
        # at the same time it was about to start.
        logger.debug("Skipped failure update for removed video job %s", job_id)


def _forget_future(job_id: str, future: Future[None]) -> None:
    with _job_futures_lock:
        if _job_futures.get(job_id) is future:
            _job_futures.pop(job_id, None)


def _request_is_loopback(request: Request) -> bool:
    host = request.client.host if request.client is not None else ""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _request_video_api_token(request: Request) -> str:
    direct = request.headers.get("x-video-api-token", "").strip()
    if direct:
        return direct

    authorization = request.headers.get("authorization", "")
    scheme, _, value = authorization.partition(" ")
    if scheme.casefold() == "bearer":
        return value.strip()
    if scheme.casefold() != "basic" or not value:
        return ""
    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return ""
    username, separator, password = decoded.partition(":")
    if not separator:
        return username
    # ``httpx`` automatically creates Basic Auth when the API URL contains
    # userinfo.  Accepting the password makes a remote BotConfig usable
    # without changing qq_bot/service.py, while the configured token remains
    # required for every non-loopback request.
    return password or username


def require_video_api_access(request: Request) -> None:
    """Allow local Bot/WebUI calls, or a remote caller with a shared token."""
    if _request_is_loopback(request):
        return
    if VIDEO_API_TOKEN:
        provided = _request_video_api_token(request)
        if provided and hmac.compare_digest(provided, VIDEO_API_TOKEN):
            return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="视频 API 仅允许本机访问或提供有效 API Token",
    )


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((BASE_DIR / "static" / "index.html").read_text(encoding="utf-8"))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/admin", response_class=HTMLResponse)
def get_admin(request: Request, token: str | None = None) -> Response:
    return admin_page(request, token)


app.add_api_route("/api/admin/status", admin_status, methods=["GET"])
app.add_api_route("/api/admin/logs", admin_logs, methods=["GET"])
app.add_api_route("/api/admin/game-calendar", admin_game_calendar, methods=["GET"])
app.add_api_route(
    "/api/admin/game-calendar", save_game_calendar_entry, methods=["POST"]
)
app.add_api_route(
    "/api/admin/game-calendar/{game_id}", delete_game_calendar_entry, methods=["DELETE"]
)
app.add_api_route(
    "/api/admin/game-calendar/{game_id}/image", game_calendar_image, methods=["GET"]
)
app.add_api_route("/api/admin/config", update_config, methods=["POST"])
app.add_api_route("/api/admin/secrets/{kind}", save_secret, methods=["POST"])
app.add_api_route("/api/admin/actions/{action}", launch_action, methods=["POST"])


@app.post(
    "/api/jobs",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_video_api_access)],
)
def create_job(request: CreateJobRequest) -> dict[str, object]:
    try:
        url = normalize_video_url(request.url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    job = Job(id=uuid.uuid4().hex, source_url=url)
    if not store.add(job, max_pending=MAX_QUEUE_SIZE):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="下载队列已满，请稍后再试",
            headers={"Retry-After": "30"},
        )
    try:
        with _job_futures_lock:
            future = executor.submit(run_job, job.id, url)
            _job_futures[job.id] = future
    except RuntimeError as exc:
        store.remove(job.id)
        raise HTTPException(status_code=503, detail="视频服务正在关闭，请稍后再试") from exc
    future.add_done_callback(lambda completed, job_id=job.id: _forget_future(job_id, completed))
    return job.public()


@app.get(
    "/api/jobs/{job_id}",
    dependencies=[Depends(require_video_api_access)],
)
def get_job(job_id: str) -> dict[str, object]:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job.public()


@app.get(
    "/api/jobs/{job_id}/file",
    dependencies=[Depends(require_video_api_access)],
)
def get_file(job_id: str) -> FileResponse:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job.status != "completed" or job.file_path is None or not job.file_path.is_file():
        raise HTTPException(status_code=409, detail="文件尚未就绪")
    return FileResponse(job.file_path, filename=job.file_path.name, media_type="video/mp4")


@app.delete(
    "/api/jobs/{job_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_video_api_access)],
)
def delete_job(job_id: str) -> None:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    if job.status == "queued":
        with _job_futures_lock:
            future = _job_futures.get(job_id)
            cancelled = future is not None and future.cancel()
        if not cancelled:
            current = store.get(job_id)
            if current is None:
                raise HTTPException(status_code=404, detail="任务不存在")
            raise HTTPException(status_code=409, detail="任务已开始运行，暂时不能删除")

    current = store.get(job_id)
    if current is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if current.status == "downloading":
        raise HTTPException(status_code=409, detail="运行中的任务不能删除")
    if current.status == "queued" and not cancelled:
        # This is only reachable if the executor raced the cancellation
        # attempt and changed state between the two reads.
        raise HTTPException(status_code=409, detail="任务已开始运行，暂时不能删除")

    removed = store.remove(job_id)
    if removed is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    _cleanup_job_dir(job_id)
