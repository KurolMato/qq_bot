from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

from .config import BotConfig


class JobFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class CompletedVideo:
    job_id: str
    url: str
    filename: str
    size_bytes: int


_TRANSIENT_POLL_STATUS_CODES = {408, 425, 429}


def _is_transient_poll_error(error: httpx.HTTPError) -> bool:
    """Return whether a job-status request is safe to retry.

    The conversion job is already created when polling starts, so a failed
    status request must not be treated as a failed conversion.  Network
    interruptions and 5xx/408/425/429 responses are temporary in practice;
    other HTTP errors (for example a 404 job id) should still surface.
    """
    if isinstance(error, httpx.HTTPStatusError):
        status_code = error.response.status_code
        return status_code >= 500 or status_code in _TRANSIENT_POLL_STATUS_CODES
    return isinstance(error, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError))


class VideoServiceClient:
    def __init__(self, config: BotConfig) -> None:
        self.config = config

    async def convert(self, source_url: str) -> CompletedVideo:
        timeout = httpx.Timeout(30.0, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(f"{self.config.api_url}/api/jobs", json={"url": source_url})
            response.raise_for_status()
            job = response.json()
            job_id = job["id"]
            deadline = time.monotonic() + self.config.job_timeout
            base_poll_interval = max(self.config.poll_interval, 0.5)
            poll_interval = base_poll_interval

            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                await asyncio.sleep(min(poll_interval, max(remaining, 0.0)))
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    # HTTPX timeouts apply separately to network phases and
                    # reads; they do not bound the whole status request.
                    response = await asyncio.wait_for(
                        client.get(f"{self.config.api_url}/api/jobs/{job_id}"),
                        timeout=remaining,
                    )
                    response.raise_for_status()
                    job = response.json()
                except asyncio.TimeoutError:
                    break
                except httpx.HTTPError as exc:
                    if not _is_transient_poll_error(exc):
                        raise
                    # Keep the retry inside the original job deadline.  A
                    # short exponential backoff prevents a temporary API
                    # outage from becoming a tight request loop.
                    poll_interval = min(max(poll_interval * 2, base_poll_interval), 30.0)
                    continue

                poll_interval = base_poll_interval
                if job["status"] == "failed":
                    raise JobFailed(job.get("error") or "转换失败")
                if job["status"] == "completed":
                    size = int(job.get("size_bytes") or 0)
                    if size > self.config.max_video_bytes:
                        limit_mb = self.config.max_video_bytes // (1024 * 1024)
                        actual_mb = size / (1024 * 1024)
                        raise JobFailed(f"视频约 {actual_mb:.1f} MB，超过群发送上限 {limit_mb} MB")
                    path = str(job["download_url"])
                    return CompletedVideo(
                        job_id=job_id,
                        url=urljoin(f"{self.config.public_api_url}/", path.lstrip("/")),
                        filename=str(job.get("filename") or "video.mp4"),
                        size_bytes=size,
                    )
        raise JobFailed("转换超时")

    async def delete(self, job_id: str) -> None:
        """Delete a completed job and its local media after QQ accepted it."""
        timeout = httpx.Timeout(15.0, connect=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.delete(f"{self.config.api_url}/api/jobs/{job_id}")
            response.raise_for_status()
