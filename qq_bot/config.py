from __future__ import annotations

import os
from dataclasses import dataclass


def _csv_set(name: str) -> frozenset[str]:
    return frozenset(item.strip() for item in os.getenv(name, "").split(",") if item.strip())


@dataclass(frozen=True)
class BotConfig:
    api_url: str
    public_api_url: str
    allowed_groups: frozenset[str]
    poll_interval: float
    job_timeout: float
    max_video_bytes: int
    announce_start: bool

    @classmethod
    def from_env(cls) -> "BotConfig":
        api_url = os.getenv("BILI_API_URL", "http://127.0.0.1:8000").rstrip("/")
        public_url = os.getenv("BILI_PUBLIC_API_URL", api_url).rstrip("/")
        return cls(
            api_url=api_url,
            public_api_url=public_url,
            allowed_groups=_csv_set("QQ_ALLOWED_GROUPS"),
            poll_interval=max(float(os.getenv("QQ_POLL_INTERVAL", "2")), 0.5),
            job_timeout=max(float(os.getenv("QQ_JOB_TIMEOUT", "3600")), 10),
            max_video_bytes=max(int(os.getenv("QQ_MAX_VIDEO_MB", "100")), 1) * 1024 * 1024,
            announce_start=os.getenv("QQ_ANNOUNCE_START", "true").lower() in {"1", "true", "yes", "on"},
        )
