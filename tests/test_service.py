import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from qq_bot.config import BotConfig
from qq_bot.service import CompletedVideo, JobFailed, VideoServiceClient


@dataclass
class _Response:
    payload: dict[str, object]
    status_code: int = 200

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "http://test/api/jobs/id")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("temporary", request=request, response=response)

    def json(self) -> dict[str, object]:
        return self.payload


class _Client:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.get_calls = 0

    async def __aenter__(self) -> "_Client":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def post(self, *_args: object, **_kwargs: object) -> _Response:
        return _Response({"id": "job-1"})

    async def get(self, *_args: object, **_kwargs: object) -> _Response:
        response = self.responses[self.get_calls]
        self.get_calls += 1
        if isinstance(response, BaseException):
            raise response
        return response  # type: ignore[return-value]


def _config(**overrides: object) -> BotConfig:
    values: dict[str, object] = {
        "api_url": "http://127.0.0.1:8000",
        "public_api_url": "http://127.0.0.1:8000",
        "allowed_groups": frozenset(),
        "poll_interval": 0.01,
        "job_timeout": 2.0,
        "max_video_bytes": 100 * 1024 * 1024,
        "announce_start": False,
    }
    values.update(overrides)
    return BotConfig(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("error", [httpx.ReadError("connection reset"), httpx.ReadTimeout("read timed out")])
def test_poll_retries_transient_network_error_before_completion(error: httpx.HTTPError) -> None:
    client = _Client(
        [
            error,
            _Response(
                {
                    "status": "completed",
                    "download_url": "/api/jobs/job-1/file",
                    "filename": "video.mp4",
                    "size_bytes": 123,
                }
            ),
        ]
    )
    service = VideoServiceClient(_config())

    async def run() -> CompletedVideo:
        with patch("qq_bot.service.httpx.AsyncClient", return_value=client):
            with patch("qq_bot.service.asyncio.sleep", new=AsyncMock()):
                return await service.convert("https://x.com/a/status/1")

    video = asyncio.run(run())

    assert video.job_id == "job-1"
    assert video.size_bytes == 123
    assert client.get_calls == 2


@pytest.mark.parametrize("status_code", [429, 502])
def test_poll_retries_server_error_within_deadline(status_code: int) -> None:
    client = _Client(
        [
            _Response({}, status_code=status_code),
            _Response(
                {
                    "status": "completed",
                    "download_url": "/api/jobs/job-1/file",
                    "filename": "video.mp4",
                    "size_bytes": 456,
                }
            ),
        ]
    )
    service = VideoServiceClient(_config())

    async def run() -> CompletedVideo:
        with patch("qq_bot.service.httpx.AsyncClient", return_value=client):
            with patch("qq_bot.service.asyncio.sleep", new=AsyncMock()):
                return await service.convert("https://x.com/a/status/1")

    video = asyncio.run(run())

    assert video.filename == "video.mp4"
    assert client.get_calls == 2


def test_poll_does_not_retry_permanent_http_error() -> None:
    client = _Client([_Response({}, status_code=404)])
    service = VideoServiceClient(_config())

    async def run() -> None:
        with patch("qq_bot.service.httpx.AsyncClient", return_value=client):
            with patch("qq_bot.service.asyncio.sleep", new=AsyncMock()):
                await service.convert("https://x.com/a/status/1")

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(run())
    assert client.get_calls == 1


class _SlowClient(_Client):
    def __init__(self) -> None:
        super().__init__([])
        self.started = asyncio.Event()
        self.cancelled = False

    async def get(self, *_args: object, **_kwargs: object) -> _Response:
        self.get_calls += 1
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("The simulated stalled request must be cancelled")


def test_poll_cancels_stalled_request_at_job_deadline() -> None:
    async def run() -> None:
        client = _SlowClient()
        service = VideoServiceClient(_config(job_timeout=0.05))
        with patch("qq_bot.service.httpx.AsyncClient", return_value=client):
            with patch("qq_bot.service.asyncio.sleep", new=AsyncMock()):
                # The outer timeout is a test guard, not the job timeout.
                with pytest.raises(JobFailed, match="转换超时"):
                    await asyncio.wait_for(service.convert("https://x.com/a/status/1"), 0.5)
        assert client.get_calls == 1
        assert client.cancelled

    asyncio.run(run())


def test_poll_preserves_external_cancellation() -> None:
    async def run() -> None:
        client = _SlowClient()
        service = VideoServiceClient(_config())
        with patch("qq_bot.service.httpx.AsyncClient", return_value=client):
            with patch("qq_bot.service.asyncio.sleep", new=AsyncMock()):
                task = asyncio.create_task(service.convert("https://x.com/a/status/1"))
                await asyncio.wait_for(client.started.wait(), 0.5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        assert client.get_calls == 1
        assert client.cancelled

    asyncio.run(run())
