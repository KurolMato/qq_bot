from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import sqlite3
import unicodedata
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from PIL import Image, ImageOps

from .list_cards import _cache_path, _load_image
from .personal_rank_sources import canonical_cover_title, native_cover_urls, same_title


logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
GAME_CALENDAR_DB_PATH = PROJECT_ROOT / "data" / "game-calendar.db"
GAME_CALENDAR_IMAGE_DIR = PROJECT_ROOT / "data" / "game-calendar-images"
GAME_COVER_CACHE_DIR = PROJECT_ROOT / "data" / "personal_rank_cover_cache"
_url_cache_path = _cache_path

# The shared image loader has its own network timeouts. These limits also bound
# cache reads, batch size fan-out, and the optional second catalog lookup.
MAX_CONCURRENT_IMAGE_LOADS = 8
IMAGE_BATCH_DEADLINE_SECONDS = 20.0
IMAGE_LOAD_DEADLINE_SECONDS = 19.0
MAX_CATALOG_COVERS_PER_GAME = 3

_STEAM_LIBRARY_COVER = re.compile(r"^library_600x900(?:_2x)?\.(?:jpe?g|png|webp)$", re.I)


def _steam_header_urls(value: str) -> tuple[str, ...]:
    """Return ordered Steam landscape header URLs for a portrait library cover."""
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").casefold().rstrip(".")
        if not (host == "steamstatic.com" or host.endswith(".steamstatic.com")):
            return ()
        parts = parsed.path.strip("/").split("/")
        if not parts or not _STEAM_LIBRARY_COVER.fullmatch(parts[-1]):
            return ()
        app_index = next(
            (
                index
                for index in range(len(parts) - 1)
                if parts[index].casefold() == "apps"
                and parts[index + 1].isdigit()
            ),
            None,
        )
        if app_index is None:
            return ()
        app_path = parts[: app_index + 2]
        conventional_path = "/" + "/".join(app_path + ["header.jpg"])
        conventional = urlunsplit(
            (parsed.scheme, parsed.netloc, conventional_path, "", "")
        )
        candidates: list[str] = []
        # Some Steam CDN deployments publish the header beside hashed assets.
        # Try that exact directory first, then the stable app-level URL.
        if len(parts) > app_index + 3:
            same_directory = "/" + "/".join(parts[:-1] + ["header.jpg"])
            candidate = urlunsplit((parsed.scheme, parsed.netloc, same_directory, "", ""))
            if candidate != conventional:
                candidates.append(candidate)
        candidates.append(conventional)
        return tuple(candidates)
    except (TypeError, ValueError):
        return ()


def _image_size(content: bytes) -> tuple[int, int] | None:
    """Decode an image fully and return display dimensions unless fully transparent."""
    try:
        with Image.open(BytesIO(content)) as image:
            image.load()
            oriented = ImageOps.exif_transpose(image)
            try:
                oriented.load()
                rgba = oriented.convert("RGBA")
                try:
                    if rgba.getchannel("A").getbbox() is None:
                        return None
                finally:
                    rgba.close()
                return oriented.size
            finally:
                if oriented is not image:
                    oriented.close()
    except Exception:
        return None


def _valid_asset(content: bytes | None, *, landscape: bool) -> bool:
    if not content:
        return False
    size = _image_size(content)
    if size is None:
        return False
    width, height = size
    return not landscape or (width > height and width >= 240 and height >= 90)


def _game_key(game: Any) -> tuple[str, str, str | None]:
    return (
        str(getattr(game, "platform", "")).casefold(),
        str(getattr(game, "name", "")).casefold(),
        getattr(game, "image_url", None),
    )


def _cover_identity(game: Any) -> tuple[str, str]:
    platform = _platform_family(getattr(game, "platform", ""))
    if platform is None:
        platform = _normalized_catalog_title(getattr(game, "platform", ""))
    title = _normalized_catalog_title(getattr(game, "name", ""))
    return platform, title


def _cover_cache_path(game: Any) -> Path:
    platform, title = _cover_identity(game)
    digest = hashlib.sha256(f"{platform}\0{title}".encode("utf-8")).hexdigest()
    namespace = "switch-v1" if platform == "switch" else "native-v1"
    return GAME_COVER_CACHE_DIR / namespace / f"{digest}.img"


def _cover_cache_key(game: Any) -> str:
    return f"personal-rank-cache://{_cover_cache_path(game).stem}"


def _legacy_cover_cache_path(game: Any) -> Path:
    """Keep old regional-name cache files usable without changing user data."""
    platform, _ = _cover_identity(game)
    title = unicodedata.normalize("NFKC", str(getattr(game, "name", ""))).casefold()
    title = "".join(character for character in title if character.isalnum())
    digest = hashlib.sha256(f"{platform}\0{title}".encode("utf-8")).hexdigest()
    namespace = "switch-v1" if platform == "switch" else "native-v1"
    return GAME_COVER_CACHE_DIR / namespace / f"{digest}.img"


def _primary_game_urls(url: str | None) -> tuple[str, ...]:
    if not url:
        return ()
    return _steam_header_urls(url) or (url,)


def _normalized_catalog_title(value: object) -> str:
    normalized = canonical_cover_title(str(value)).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _platform_family(value: object) -> str | None:
    text = re.sub(r"[^a-z0-9]", "", str(value).casefold())
    if text in {"ps", "ps3", "ps4", "ps5", "psn", "playstation"}:
        return "ps"
    if text.startswith("playstation"):
        return "ps"
    if text in {"switch", "nintendo", "ns", "nsw", "nintendoswitch"}:
        return "switch"
    if text == "steam":
        return "steam"
    if "switch" in text:
        return "switch"
    return None


def _read_catalog_matches(
    games: tuple[Any, ...],
) -> dict[tuple[str, str], tuple[str, ...]]:
    """Read matching calendar art without creating or migrating its DB."""
    path = GAME_CALENDAR_DB_PATH
    if not games or not path.is_file():
        return {}

    try:
        # The connection is explicitly read-only: the calendar registry's normal
        # constructor creates/migrates tables, which a render lookup must avoid.
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.5)
        try:
            columns = {
                str(row[1]) for row in db.execute("PRAGMA table_info(game_releases)")
            }
            if not {"name", "image_url"}.issubset(columns):
                return {}
            platform_expr = "platform" if "platform" in columns else "'Steam'"
            rows = db.execute(
                f"SELECT name, image_url, {platform_expr} FROM game_releases "
                "WHERE image_url IS NOT NULL ORDER BY updated_at DESC"
                if "updated_at" in columns
                else f"SELECT name, image_url, {platform_expr} FROM game_releases "
                "WHERE image_url IS NOT NULL"
            ).fetchall()
        finally:
            db.close()
    except (OSError, sqlite3.Error, ValueError):
        logger.debug("Personal rank catalog lookup unavailable", exc_info=True)
        return {}

    result: dict[tuple[str, str], tuple[str, ...]] = {}
    for game in games:
        family = _platform_family(getattr(game, "platform", ""))
        if family is None:
            continue
        name = str(getattr(game, "name", ""))
        if not name:
            continue
        matches: list[tuple[int, str]] = []
        for catalog_name, image_url, platform in rows:
            candidate_url = str(image_url or "")
            if not candidate_url:
                continue
            if family == "switch" and _platform_family(platform) != "switch":
                continue
            candidate_name = str(catalog_name or "")
            if same_title(name, candidate_name):
                matches.append((0 if _platform_family(platform) == family else 1, candidate_url))
        matches.sort(key=lambda item: item[0])
        urls: list[str] = []
        for _, candidate_url in matches:
            if candidate_url not in urls:
                urls.append(candidate_url)
            if len(urls) >= MAX_CATALOG_COVERS_PER_GAME:
                break
        if urls:
            result[_cover_identity(game)] = tuple(urls)
    return result


def _read_local_catalog_image(url: str) -> bytes | None:
    if not url.startswith("local:"):
        return None
    try:
        root = GAME_CALENDAR_IMAGE_DIR.resolve()
        path = (root / url.removeprefix("local:")).resolve()
        path.relative_to(root)
        return path.read_bytes() if path.is_file() else None
    except (OSError, ValueError):
        return None


def _read_image_file(path: Path) -> bytes | None:
    try:
        return path.read_bytes() if path.is_file() else None
    except OSError:
        return None


async def _read_cover_cache_batch(
    paths: dict[tuple[str, str], Path], deadline: float,
) -> dict[tuple[str, str], bytes]:
    if not paths:
        return {}
    loop = asyncio.get_running_loop()
    slots = asyncio.Semaphore(MAX_CONCURRENT_IMAGE_LOADS)

    async def read_one(key: tuple[str, str], path: Path) -> bytes | None:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return None
        try:
            async with slots:
                content = await asyncio.wait_for(
                    asyncio.to_thread(_read_image_file, path), timeout=remaining
                )
            if content and await asyncio.wait_for(
                asyncio.to_thread(_valid_asset, content, landscape=key[0] != "switch"),
                timeout=max(0.01, min(2.0, deadline - loop.time())),
            ):
                return content
        except asyncio.CancelledError:
            raise
        except Exception:
            return None
        return None

    tasks = {
        asyncio.create_task(read_one(key, path)): key for key, path in paths.items()
    }
    loaded: dict[tuple[str, str], bytes] = {}
    try:
        remaining = max(deadline - loop.time(), 0.0)
        done, pending = await asyncio.wait(tasks, timeout=remaining) if remaining else (set(), set(tasks))
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            if task.cancelled():
                continue
            try:
                content = task.result()
            except Exception:
                content = None
            if content is not None:
                loaded[tasks[task]] = content
    finally:
        unfinished = [task for task in tasks if not task.done()]
        for task in unfinished:
            task.cancel()
        if unfinished:
            await asyncio.gather(*unfinished, return_exceptions=True)
    return loaded


def _read_url_cache_image(url: str) -> bytes | None:
    if url.startswith("local:"):
        return _read_local_catalog_image(url)
    try:
        return _read_image_file(_url_cache_path(url))
    except Exception:
        return None


async def _read_url_cache_batch(
    requirements: dict[str, bool], deadline: float,
) -> dict[str, bytes]:
    if not requirements:
        return {}
    loop = asyncio.get_running_loop()
    slots = asyncio.Semaphore(MAX_CONCURRENT_IMAGE_LOADS)

    async def read_one(url: str, landscape: bool) -> bytes | None:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return None
        try:
            async with slots:
                content = await asyncio.wait_for(
                    asyncio.to_thread(_read_url_cache_image, url), timeout=remaining
                )
            if content and await asyncio.wait_for(
                asyncio.to_thread(_valid_asset, content, landscape=landscape),
                timeout=max(0.01, min(2.0, deadline - loop.time())),
            ):
                return content
        except asyncio.CancelledError:
            raise
        except Exception:
            return None
        return None

    tasks = {
        asyncio.create_task(read_one(url, landscape)): url
        for url, landscape in requirements.items()
    }
    loaded: dict[str, bytes] = {}
    try:
        remaining = max(deadline - loop.time(), 0.0)
        done, pending = await asyncio.wait(tasks, timeout=remaining) if remaining else (set(), set(tasks))
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            if task.cancelled():
                continue
            url = tasks[task]
            try:
                content = task.result()
            except Exception:
                content = None
            if content is not None:
                loaded[url] = content
    finally:
        unfinished = [task for task in tasks if not task.done()]
        for task in unfinished:
            task.cancel()
        if unfinished:
            await asyncio.gather(*unfinished, return_exceptions=True)
    return loaded


def _persist_cover_cache(path: Path, content: bytes, *, landscape: bool = True) -> bytes | None:
    if not _valid_asset(content, landscape=landscape):
        return None
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
        with temporary.open("xb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        staged = temporary.read_bytes()
        if not _valid_asset(staged, landscape=landscape):
            return None
        os.replace(temporary, path)
        stored = path.read_bytes()
        return stored if _valid_asset(stored, landscape=landscape) else None
    except OSError as exc:
        logger.debug("Personal rank cover cache write failed: %s", type(exc).__name__)
        return None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


async def _load_url_batch(
    requirements: dict[str, bool], deadline: float,
) -> dict[str, bytes]:
    """Load and validate URLs with bounded concurrency and a shared deadline.

    The bool value says whether the URL is used as a cover and therefore must
    decode to a landscape image. A URL can also be an avatar; in that case the
    cover requirement takes precedence so portrait bytes cannot reach a cover.
    """
    if not requirements:
        return {}
    loop = asyncio.get_running_loop()
    worker_slots = asyncio.Semaphore(MAX_CONCURRENT_IMAGE_LOADS)
    network_slots = asyncio.Semaphore(MAX_CONCURRENT_IMAGE_LOADS)
    local_slots = asyncio.Semaphore(MAX_CONCURRENT_IMAGE_LOADS)

    async def load_one(url: str, landscape: bool) -> bytes | None:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return None
        timeout = min(IMAGE_LOAD_DEADLINE_SECONDS, remaining)
        try:
            async with worker_slots:
                if url.startswith("local:"):
                    async with local_slots:
                        content = await asyncio.wait_for(
                            asyncio.to_thread(_read_local_catalog_image, url), timeout=timeout
                        )
                else:
                    content = await asyncio.wait_for(
                        _load_image(url, network_slots), timeout=timeout
                    )
                if not content:
                    return None
                if not await asyncio.wait_for(
                    asyncio.to_thread(_valid_asset, content, landscape=landscape),
                    timeout=max(0.01, min(2.0, deadline - loop.time())),
                ):
                    return None
                return content
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("Personal rank image unavailable: %s (%s)", url, type(exc).__name__)
            return None

    tasks = {
        asyncio.create_task(load_one(url, landscape)): url
        for url, landscape in requirements.items()
    }
    loaded: dict[str, bytes] = {}
    try:
        remaining = max(deadline - loop.time(), 0.0)
        if remaining:
            done, pending = await asyncio.wait(tasks, timeout=remaining)
        else:
            done, pending = set(), set(tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        for task in done:
            if task.cancelled():
                continue
            url = tasks[task]
            try:
                content = task.result()
            except Exception:
                content = None
            if content is not None:
                loaded[url] = content
    finally:
        unfinished = [task for task in tasks if not task.done()]
        for task in unfinished:
            task.cancel()
        if unfinished:
            await asyncio.gather(*unfinished, return_exceptions=True)
    return loaded


async def load_personal_rank_assets(data: Any) -> tuple[Any, dict[str, bytes | None]]:
    """Resolve the card's visible art from durable local caches before network.

    Only the renderer's six monthly covers and most-played weekly cover are
    loaded. Other game rows and every period total remain unchanged. A usable
    downloaded or URL-cache cover is atomically copied into the per-game cache;
    only its validated read-back is returned to the renderer.
    """
    from .personal_rank_card import MAX_GAME_TILES

    loop = asyncio.get_running_loop()
    deadline = loop.time() + IMAGE_BATCH_DEADLINE_SECONDS
    write_reserve = min(2.0, IMAGE_BATCH_DEADLINE_SECONDS * 0.1)
    load_deadline = deadline - write_reserve
    games_by_field = {
        field: tuple(getattr(data, field, ()) or ())
        for field in ("games", "recent_games")
    }
    monthly_games = list(games_by_field["games"][:MAX_GAME_TILES])
    weekly_games = games_by_field["recent_games"]
    visible_games = monthly_games[:]
    if weekly_games:
        visible_games.append(max(weekly_games, key=lambda game: game.seconds))

    games_by_identity: dict[tuple[str, str], list[Any]] = {}
    for game in visible_games:
        identity = _cover_identity(game)
        games_by_identity.setdefault(identity, []).append(game)
    representatives = {key: values[0] for key, values in games_by_identity.items()}
    cache_paths = {key: _cover_cache_path(game) for key, game in representatives.items()}

    # First layer: a title-scoped cache survives source URL changes and works
    # without any game store or profile endpoint being reachable.
    durable_covers = await _read_cover_cache_batch(cache_paths, load_deadline)
    legacy_paths = {
        key: _legacy_cover_cache_path(game) for key, game in representatives.items()
        if key not in durable_covers and _legacy_cover_cache_path(game) != cache_paths[key]
    }
    legacy_covers = await _read_cover_cache_batch(legacy_paths, load_deadline)
    missing_identities = [key for key in representatives if key not in durable_covers]

    catalog_by_identity: dict[tuple[str, str], tuple[str, ...]] = {}
    catalog_games = tuple(
        representatives[key]
        for key in missing_identities
        if _platform_family(getattr(representatives[key], "platform", ""))
    )
    if catalog_games and loop.time() < load_deadline:
        try:
            catalog_by_identity = await asyncio.wait_for(
                asyncio.to_thread(_read_catalog_matches, catalog_games),
                timeout=max(load_deadline - loop.time(), 0.001),
            )
        except Exception as exc:
            logger.debug("Personal rank catalog lookup failed (%s)", type(exc).__name__)

    primary_by_identity: dict[tuple[str, str], tuple[str, ...]] = {}
    candidates_by_identity: dict[tuple[str, str], tuple[str, ...]] = {}
    url_cache_requirements: dict[str, bool] = {}
    for identity, games in games_by_identity.items():
        primary_urls: list[str] = []
        catalog_urls = list(catalog_by_identity.get(identity, ()))
        for game in games:
            for url in _primary_game_urls(
                str(game.image_url) if getattr(game, "image_url", None) else None
            ):
                if url not in primary_urls:
                    primary_urls.append(url)
        if identity[0] == "switch":
            primary_urls = list(dict.fromkeys([*catalog_urls, *primary_urls]))
        primary_by_identity[identity] = tuple(primary_urls)
        all_urls = list(primary_urls)
        for url in catalog_urls:
            if url not in all_urls:
                all_urls.append(url)
        candidates_by_identity[identity] = tuple(all_urls)
        if identity not in durable_covers:
            for url in all_urls:
                url_cache_requirements[url] = url_cache_requirements.get(url, False) or identity[0] != "switch"

    avatar_url = str(getattr(data, "avatar_url", "") or "")
    if avatar_url:
        url_cache_requirements.setdefault(avatar_url, False)

    # Second layer: inspect all existing URL-hash and local catalog assets before
    # starting any network request. Portrait/corrupt/transparent entries miss.
    url_cached = await _read_url_cache_batch(url_cache_requirements, load_deadline)
    # Legacy hits are persisted under the canonical key below, after validation.
    resolved_covers: dict[tuple[str, str], bytes] = {**legacy_covers, **durable_covers}
    for identity, candidates in candidates_by_identity.items():
        if identity in resolved_covers:
            continue
        cached_url = next((url for url in candidates if url in url_cached), None)
        if cached_url:
            resolved_covers[identity] = url_cached[cached_url]

    # Third layer: download only unresolved primary sources. Steam's hashed
    # directory header is first; its conventional app header remains a fallback.
    network_primary: dict[str, bool] = {}
    for identity in representatives:
        if identity in resolved_covers:
            continue
        primary = primary_by_identity.get(identity, ())
        if primary and not primary[0].startswith("local:"):
            network_primary[primary[0]] = network_primary.get(primary[0], False) or identity[0] != "switch"
    if avatar_url and avatar_url not in url_cached and not avatar_url.startswith("local:"):
        network_primary[avatar_url] = network_primary.get(avatar_url, False)
    remote_primary = (
        await _load_url_batch(network_primary, load_deadline) if network_primary else {}
    )
    tried_primary_urls = set(network_primary)

    for identity in representatives:
        if identity in resolved_covers:
            continue
        primary = primary_by_identity.get(identity, ())
        downloaded_url = next((url for url in primary if url in remote_primary), None)
        if downloaded_url:
            resolved_covers[identity] = remote_primary[downloaded_url]

    # Last layer: after every cache candidate is checked and primary misses fail,
    # try Steam's secondary header or an exact PS/Switch catalog match.
    fallback_requirements: dict[str, bool] = {}
    for identity in representatives:
        if identity in resolved_covers:
            continue
        primary = primary_by_identity.get(identity, ())
        for url in candidates_by_identity.get(identity, ()):
            if url in tried_primary_urls or url.startswith("local:"):
                continue
            fallback_requirements[url] = fallback_requirements.get(url, False) or identity[0] != "switch"
    remote_fallback = (
        await _load_url_batch(fallback_requirements, load_deadline)
        if fallback_requirements and loop.time() < load_deadline
        else {}
    )
    for identity, candidates in candidates_by_identity.items():
        if identity in resolved_covers:
            continue
        primary = primary_by_identity.get(identity, ())
        fallback_candidates = tuple(url for url in candidates if url not in tried_primary_urls)
        fallback_url = next(
            (url for url in fallback_candidates if url in url_cached),
            None,
        )
        if fallback_url is None:
            fallback_url = next(
                (url for url in fallback_candidates if url in remote_fallback),
                None,
            )
        if fallback_url:
            resolved_covers[identity] = url_cached.get(fallback_url) or remote_fallback[fallback_url]

    # Resolve official store media using the same sources as the calendar.
    # Give discovery its own bounded phase so slow metadata cannot starve downloads.
    unresolved = [key for key in representatives if key not in resolved_covers]
    if unresolved:
        discovered = await asyncio.gather(*(native_cover_urls(representatives[key]) for key in unresolved))
        deadline = loop.time() + IMAGE_BATCH_DEADLINE_SECONDS
        load_deadline = deadline - write_reserve
        urls_by_identity = dict(zip(unresolved, discovered))
        requirements = {}
        for identity, urls in urls_by_identity.items():
            for url in urls:
                requirements[url] = requirements.get(url, False) or identity[0] != "switch"
        native = await _read_url_cache_batch(requirements, load_deadline)
        missing = {url: landscape for url, landscape in requirements.items() if url not in native}
        native.update(await _load_url_batch(missing, load_deadline))
        for identity, urls in urls_by_identity.items():
            source = next((url for url in urls if url in native), None)
            if source:
                resolved_covers[identity] = native[source]

    # Metadata success does not guarantee that CDN downloads succeed. Once
    # official PS candidates fail, try the independent landscape provider.
    missing_ps = [key for key in representatives if key[0] == "ps" and key not in resolved_covers]
    if missing_ps:
        alternatives = await asyncio.gather(*(native_cover_urls(representatives[key], grid_only=True) for key in missing_ps))
        deadline = loop.time() + IMAGE_BATCH_DEADLINE_SECONDS
        load_deadline = deadline - write_reserve
        requirements = {url: True for urls in alternatives for url in urls}
        contents = await _read_url_cache_batch(requirements, load_deadline)
        contents.update(await _load_url_batch({url: True for url in requirements if url not in contents}, load_deadline))
        for identity, urls in zip(missing_ps, alternatives):
            chosen = next((url for url in urls if url in contents), None)
            if chosen:
                resolved_covers[identity] = contents[chosen]

    assets: dict[str, bytes | None] = {}
    if avatar_url:
        avatar = url_cached.get(avatar_url) or remote_primary.get(avatar_url)
        if avatar and _valid_asset(avatar, landscape=False):
            assets[avatar_url] = avatar

    effective_by_identity: dict[tuple[str, str], str] = {}
    for identity, content in resolved_covers.items():
        if identity in durable_covers:
            stored = content
        else:
            remaining = max(deadline - loop.time(), 0.001)
            try:
                stored = await asyncio.wait_for(
                    asyncio.to_thread(_persist_cover_cache, cache_paths[identity], content, landscape=identity[0] != "switch"),
                    timeout=remaining,
                )
            except Exception as exc:
                logger.debug("Personal rank cover cache verification failed (%s)", type(exc).__name__)
                stored = None
        if not stored or not _valid_asset(stored, landscape=identity[0] != "switch"):
            continue
        chosen_url = _cover_cache_key(representatives[identity])
        effective_by_identity[identity] = chosen_url
        assets[chosen_url] = stored

    changes: dict[str, tuple[Any, ...]] = {}
    for field, games in games_by_field.items():
        updated: list[Any] = []
        for game in games:
            effective = effective_by_identity.get(_cover_identity(game))
            if effective is not None and getattr(game, "image_url", None) != effective:
                updated.append(replace(game, image_url=effective))
            else:
                updated.append(game)
        if hasattr(data, field):
            changes[field] = tuple(updated)

    if changes:
        try:
            data = replace(data, **changes)
        except (TypeError, ValueError):
            logger.debug("Personal rank data is not replaceable", exc_info=True)

    return data, assets
