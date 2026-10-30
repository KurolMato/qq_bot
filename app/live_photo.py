from __future__ import annotations

import json
import math
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class LivePhotoError(RuntimeError):
    pass


@dataclass(frozen=True)
class AlbumItem:
    image_urls: tuple[str, ...] = ()
    video_urls: tuple[str, ...] = ()
    is_live: bool = False


def _urls(values) -> tuple[str, ...]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (tuple, list)):
        return ()
    return tuple(dict.fromkeys(url for url in values if isinstance(url, str) and url))


def _play_urls(video: dict) -> tuple[str, ...]:
    urls = []
    for field in ("play_addr_h264", "play_addr", "play_addr_bytevc1"):
        address = video.get(field)
        if isinstance(address, dict):
            urls.extend(_urls(address.get("url_list")))
    return _urls(urls)


def douyin_album(detail: dict) -> list[AlbumItem]:
    result = []
    for image in detail.get("images") or []:
        if not isinstance(image, dict):
            continue
        video = image.get("video") or {}
        video = video if isinstance(video, dict) else {}
        video_urls = _play_urls(video)
        result.append(AlbumItem(
            _urls(image.get("url_list")), video_urls,
            bool(video or image.get("is_live_photo") or image.get("live_photo_type")),
        ))
    return result


def _stream_urls(stream) -> tuple[str, ...]:
    if not isinstance(stream, dict):
        return ()
    urls = []
    for codec in ("h264", "h265", "av1"):
        entries = stream.get(codec) or []
        if isinstance(entries, dict):
            entries = [entries]
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict):
                urls.extend(_urls(entry.get("masterUrl") or entry.get("master_url")))
                urls.extend(_urls(entry.get("backupUrls") or entry.get("backup_urls")))
    return _urls(urls)


def xiaohongshu_album(note: dict) -> list[AlbumItem]:
    result = []
    for image in note.get("imageList") or note.get("images_list") or []:
        if not isinstance(image, dict):
            continue
        urls = [image.get("urlDefault"), image.get("urlPre"), image.get("url")]
        for info in image.get("infoList") or []:
            if isinstance(info, dict):
                urls.append(info.get("url"))
        live = image.get("livePhoto") or image.get("live_photo")
        video_urls = list(_stream_urls(image.get("stream")))
        if isinstance(live, dict):
            media = live.get("media") or {}
            video_urls.extend(_stream_urls(media.get("stream") if isinstance(media, dict) else None))
            video_urls.extend(_stream_urls(live.get("stream")))
        video = image.get("video") or {}
        if isinstance(video, dict):
            video_urls.extend(_play_urls(video))
            media = video.get("media") or {}
            video_urls.extend(_stream_urls(media.get("stream") if isinstance(media, dict) else None))
        result.append(AlbumItem(_urls(urls), _urls(video_urls), bool(live or video_urls)))
    return result


def xiaohongshu_video_urls(note: dict) -> tuple[str, ...]:
    video = note.get("video") or {}
    if not isinstance(video, dict):
        return ()
    media = video.get("media") or {}
    urls = list(_stream_urls(media.get("stream") if isinstance(media, dict) else None))
    consumer = video.get("consumer") or {}
    key = consumer.get("originVideoKey") if isinstance(consumer, dict) else None
    if isinstance(key, str) and key and not key.startswith(("http:", "https:", "//")):
        urls.append("https://sns-video-bd.xhscdn.com/" + key.lstrip("/"))
    return _urls(urls)


def xiaohongshu_note(state: dict, note_id: str) -> dict:
    section = state.get("note") or {}
    mapping = (section.get("noteDetailMap") or {}) if isinstance(section, dict) else {}
    entry = (mapping.get(note_id) or {}) if isinstance(mapping, dict) else {}
    note = entry.get("note") if isinstance(entry, dict) else None
    if not isinstance(note, dict) or not note:
        mobile = state.get("noteData") or {}
        data = (mobile.get("data") or {}) if isinstance(mobile, dict) else {}
        note = data.get("noteData") if isinstance(data, dict) else None
    if not isinstance(note, dict) or not note:
        raise LivePhotoError("小红书页面未返回当前笔记详情，请检查完整分享链接或 Cookie")
    returned_id = note.get("noteId") or note.get("note_id")
    if returned_id and str(returned_id).casefold() != note_id.casefold():
        raise LivePhotoError("小红书返回了其他笔记，已停止处理")
    return note


def render_live_album(
    items: list[AlbumItem], job_dir: Path, timeout_seconds: float,
    download_asset: Callable[[str, Path, float], None],
    run: Callable = subprocess.run,
) -> Path:
    """Preserve live motion, order and available audio in one standard MP4."""
    deadline = time.monotonic() + timeout_seconds

    def remaining() -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            raise LivePhotoError("实况图文处理超时")
        return value

    def command(args: list[str], message: str):
        try:
            process = run(args, cwd=job_dir, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=False, timeout=remaining())
        except subprocess.TimeoutExpired as exc:
            raise LivePhotoError("实况图文处理超时") from exc
        if process.returncode:
            raise LivePhotoError(message)
        return process

    if not items or not any(item.is_live for item in items):
        raise LivePhotoError("作品没有实况视频")
    assets = []
    for index, item in enumerate(items):
        candidates = item.video_urls if item.is_live else item.image_urls
        if not candidates:
            raise LivePhotoError(f"第 {index + 1} 张实况未提供视频地址" if item.is_live else f"第 {index + 1} 张图片未提供地址")
        source = job_dir / f"live-source-{index:03d}.{'media' if item.is_live else 'img'}"
        for url in candidates:
            budget = remaining()
            try:
                download_asset(url, source, budget)
                if not source.is_file() or source.stat().st_size == 0:
                    raise LivePhotoError("素材为空")
                probe = command(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", source.name], f"第 {index + 1} 张素材无法读取")
                break
            except LivePhotoError as exc:
                source.unlink(missing_ok=True)
                if str(exc) == "实况图文处理超时":
                    raise
            except Exception:
                source.unlink(missing_ok=True)
        else:
            raise LivePhotoError(f"第 {index + 1} 张{'实况视频' if item.is_live else '图片'}下载失败")
        try:
            data = json.loads(probe.stdout)
            streams = data["streams"]
            video = next(stream for stream in streams if stream.get("codec_type") == "video")
            duration = float(video.get("duration") or data.get("format", {}).get("duration") or 0) if item.is_live else 3.0
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError("invalid duration")
            width, height = int(video["width"]), int(video["height"])
            rotation = next((side.get("rotation", 0) for side in video.get("side_data_list", []) if "rotation" in side), video.get("tags", {}).get("rotate", 0))
            if int(rotation) % 180:
                width, height = height, width
            audio = any(stream.get("codec_type") == "audio" for stream in streams)
        except (ValueError, KeyError, TypeError, StopIteration) as exc:
            raise LivePhotoError(f"第 {index + 1} 张素材信息不完整") from exc
        assets.append((source, item.is_live, duration, width, height, audio))

    first = assets[0]
    width, height = (720, 1280) if first[4] > first[3] else (1280, 720)
    segments = []
    for index, (source, is_live, duration, _, _, audio) in enumerate(assets):
        segment = job_dir / f"live-segment-{index:03d}.mp4"
        args = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
        if not is_live:
            args.extend(["-loop", "1", "-framerate", "30"])
        args.extend(["-i", source.name])
        if not audio:
            args.extend(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"])
        args.extend([
            "-map", "0:v:0", "-map", "0:a:0" if audio else "1:a:0",
            "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p",
            "-af", "aresample=48000:async=1:first_pts=0,apad",
            "-t", str(duration), "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "128k",
            "-video_track_timescale", "90000", "-movflags", "+faststart", segment.name,
        ])
        command(args, f"第 {index + 1} 张实况素材转码失败")
        segments.append(segment)
    manifest = job_dir / "live-segments.txt"
    manifest.write_text("".join(f"file '{segment.name}'\n" for segment in segments), encoding="utf-8")
    output = job_dir / "live-photo.mp4"
    command(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", manifest.name, "-c", "copy", "-movflags", "+faststart", output.name], "实况视频合并失败")
    if not output.is_file() or not output.stat().st_size:
        raise LivePhotoError("实况转换未生成 MP4")
    return output
