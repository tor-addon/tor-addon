"""
router.py
─────────
All Stremio addon HTTP routes.

  GET  /                            → redirect to /configure
  GET  /configure                   → config page HTML
  GET  /{config}/manifest.json      → addon manifest
  GET  /{config}/stream/{type}/{id} → stream list
  GET  /{config}/playback/{token}   → 307 redirect to resolved CDN URL

Stream cache (TTL=60s): Stremio fires the same request 2-3× in quick succession.
Resolved URL cache (TTL=300s): ExoPlayer on Android TV hits the same token 3-5×.
"""

import asyncio
import logging
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from config import (
    FORMAT_COMPACT,
    FORMAT_EPURE,
    UserConfig,
    decode_playback_token,
    encode_playback_token,
)
from services.postgresql import PostgreSQLClient
from settings import (
    ADDON_DESCRIPTION, ADDON_ID, ADDON_LOGO, ADDON_NAME, ADDON_VERSION,
    ADMIN_KEY, QUAL_NAMES, RES_NAMES,
    STREAM_CACHE_TTL, RESOLVED_CACHE_TTL,
)
from stream_manager import StreamManager
from utils.admin_stats import stats

logger = logging.getLogger(__name__)
router = APIRouter()

_CORS = {
    "Access-Control-Allow-Origin":  "*",
    "Access-Control-Allow-Headers": "*",
}

# ── Static pages ──────────────────────────────────────────────────────────────
_CONFIGURE_HTML: str = (Path(__file__).parent / "static" / "configure.html").read_text(encoding="utf-8")

# ── StreamManager pool (one per unique config) ────────────────────────────────
_managers: dict[str, StreamManager] = {}

def _get_manager(config: UserConfig) -> StreamManager:
    key = config.encode()
    if key not in _managers:
        _managers[key] = StreamManager(config)
    return _managers[key]

# ── Stream cache ──────────────────────────────────────────────────────────────
_stream_cache: dict[tuple, tuple[float, list]] = {}

def _sc_get(key: tuple) -> list | None:
    e = _stream_cache.get(key)
    if e and (time.monotonic() - e[0]) < STREAM_CACHE_TTL:
        return e[1]
    _stream_cache.pop(key, None)
    return None

def _sc_set(key: tuple, streams: list) -> None:
    now = time.monotonic()
    for k in [k for k, (ts, _) in _stream_cache.items() if now - ts >= STREAM_CACHE_TTL]:
        del _stream_cache[k]
    _stream_cache[key] = (now, streams)

# ── Resolved URL cache ────────────────────────────────────────────────────────
_resolved_cache: dict[str, tuple[float, str]] = {}

def _rc_get(key: str) -> str | None:
    e = _resolved_cache.get(key)
    if e and (time.monotonic() - e[0]) < RESOLVED_CACHE_TTL:
        return e[1]
    _resolved_cache.pop(key, None)
    return None

def _rc_set(key: str, url: str) -> None:
    now = time.monotonic()
    for k in [k for k, (ts, _) in _resolved_cache.items() if now - ts >= RESOLVED_CACHE_TTL]:
        del _resolved_cache[k]
    _resolved_cache[key] = (now, url)

# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/")
async def root():
    return RedirectResponse("/configure")


@router.get("/configure", response_class=HTMLResponse)
async def configure_page():
    return _CONFIGURE_HTML


@router.get("/{b64}/configure", response_class=HTMLResponse)
async def configure_preload(b64: str):
    return _CONFIGURE_HTML.replace(
        "// PRELOAD_CONFIG_PLACEHOLDER",
        f"const PRELOAD_CONFIG = {repr(b64)};",
    )


@router.get("/manifest.json")
async def manifest_root():
    return RedirectResponse("/configure")


@router.get("/{b64}/manifest.json")
async def manifest(request: Request, b64: str):
    stats.record_install(b64)
    base_url = str(request.base_url).rstrip("/")
    return JSONResponse(_build_manifest(base_url, b64), headers=_CORS)


@router.get("/{b64}/stream/{media_type}/{stremio_id:path}")
async def stream(request: Request, b64: str, media_type: str, stremio_id: str):
    config = UserConfig.decode(b64)
    if not config.is_valid():
        return JSONResponse({"streams": []}, headers=_CORS)

    stremio_id = stremio_id.removesuffix(".json")

    imdb_id = stremio_id
    season = episode = None
    if ":" in stremio_id:
        parts = stremio_id.split(":")
        imdb_id = parts[0]
        try:
            season  = int(parts[1])
            episode = int(parts[2])
        except (IndexError, ValueError):
            pass

    logger.info("STREAM  type=%s  id=%s  s=%s e=%s", media_type, imdb_id, season, episode)

    cache_key = (b64, imdb_id, season, episode)
    cached = _sc_get(cache_key)
    if cached is not None:
        stats.record_search(imdb_id)
        logger.debug("STREAM cache HIT %s", imdb_id)
        base_url  = str(request.base_url).rstrip("/")
        formatted = [_fmt(s, b64, base_url, season, episode, config) for s in cached]
        if config.max_results:
            formatted = formatted[:config.max_results]
        return JSONResponse({"streams": formatted}, headers=_CORS)

    t0      = time.monotonic()
    manager = _get_manager(config)
    try:
        streams = await manager.get_streams(imdb_id, season=season, episode=episode)
    except Exception as exc:
        logger.error("STREAM pipeline error: %s", exc, exc_info=True)
        stats.record_search(imdb_id)
        return JSONResponse({"streams": []}, headers=_CORS)

    stats.record_search(imdb_id, title=manager.last_title,
                        latency_ms=(time.monotonic() - t0) * 1000)

    if not streams:
        return JSONResponse({"streams": []}, headers=_CORS)

    _sc_set(cache_key, streams)
    base_url  = str(request.base_url).rstrip("/")
    formatted = [_fmt(s, b64, base_url, season, episode, config) for s in streams]
    if config.max_results:
        formatted = formatted[:config.max_results]
    return JSONResponse({"streams": formatted}, headers=_CORS)


@router.get("/{b64}/playback/{token}")
async def playback(b64: str, token: str, request: Request):
    config = UserConfig.decode(b64)
    if not config.is_valid():
        raise HTTPException(status_code=400, detail="Invalid config")

    try:
        info = decode_playback_token(token)
    except Exception as exc:
        logger.error("PLAYBACK: invalid token: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid token")

    cache_key = f"{b64}:{token}"
    cached_url = _rc_get(cache_key)
    if cached_url:
        logger.debug("PLAYBACK cache HIT → %.60s…", cached_url)
        return RedirectResponse(cached_url, status_code=307, headers={**_CORS, "Accept-Ranges": "bytes", "Cache-Control": "no-store"})

    stream_type = info.get("t", "torrent")
    infohash    = info.get("h", "")
    season      = info.get("s")
    episode     = info.get("e")
    year        = info.get("y")
    source      = info.get("sc", "")

    # Multi-link (merged Wawacity) token
    ddl_links  = info.get("dls") or []
    ddl_hosts  = info.get("dhs") or []
    ddl_db_ids = info.get("dis") or []

    # Single-link token — normalise to lists for uniform handling
    if not ddl_links:
        dl = info.get("dl", "")
        if dl:
            ddl_links  = [dl]
            ddl_hosts  = [info.get("dh", "")]
            ddl_db_ids = [info.get("di", "")]

    logger.info(
        "PLAYBACK  type=%s  hash=%.12s…  s=%s e=%s  ddl_hosts=%s",
        stream_type, infohash, season, episode, ddl_hosts,
    )

    stream_dict: dict = {
        "stream_type": stream_type,
        "infohash":    infohash,
        "ddl_links":   ddl_links,
        "ddl_hosts":   ddl_hosts,
        "ddl_db_ids":  ddl_db_ids,
        # Keep single-link fields for backward compat with resolve_stream
        "ddl_link":    ddl_links[0]  if ddl_links  else "",
        "ddl_host":    ddl_hosts[0]  if ddl_hosts  else "",
        "db_id":       ddl_db_ids[0] if ddl_db_ids else None,
    }

    manager = _get_manager(config)
    try:
        url = await manager.resolve_stream(stream_dict, season=season, episode=episode, year=year)
    except Exception as exc:
        logger.error("PLAYBACK resolve error: %s", exc, exc_info=True)
        stats.record_playback(success=False)
        raise HTTPException(status_code=500, detail="Resolution failed")

    if not url:
        raise HTTPException(status_code=404, detail="Could not resolve stream")

    _rc_set(cache_key, url)
    stats.record_playback(success=True, source=source)
    logger.info("PLAYBACK → %.60s…", url)
    return RedirectResponse(url, status_code=307, headers={**_CORS, "Accept-Ranges": "bytes", "Cache-Control": "no-store"})


# ── Manifest ──────────────────────────────────────────────────────────────────

def _build_manifest(base_url: str = "", b64: str = "") -> dict:
    m = {
        "id":          ADDON_ID,
        "version":     ADDON_VERSION,
        "name":        ADDON_NAME,
        "description": ADDON_DESCRIPTION,
        "logo":        ADDON_LOGO,
        "resources":   ["stream"],
        "types":       ["movie", "series"],
        "idPrefixes":  ["tt"],
        "catalogs":    [],
        "behaviorHints": {
            "configurable":          True,
            "configurationRequired": not bool(b64),
        },
    }
    if base_url and b64:
        m["behaviorHints"]["configureUrl"] = f"{base_url}/{b64}/configure"
    return m


# ── Stream formatter ──────────────────────────────────────────────────────────

def _fmt(
    stream:         dict,
    b64:            str,
    base_url:       str,
    season:         int | None,
    episode:        int | None,
    cfg:            "UserConfig | None" = None,
) -> dict:
    from config import UserConfig as _UC
    if cfg is None:
        cfg = _UC()
    display_format = cfg.display_format
    stream_type = stream.get("stream_type", "torrent")
    infohash    = stream.get("infohash", "")
    year        = stream.get("year")

    # ── Playback token ────────────────────────────────────────────────────────
    _src = stream.get("source") or ""
    if stream_type == "ddl":
        ddl_links  = stream.get("ddl_links")  or []
        ddl_hosts  = stream.get("ddl_hosts")  or []
        ddl_db_ids = stream.get("ddl_db_ids") or []
        is_merged  = bool(ddl_links)
        token = encode_playback_token(
            stream_type = stream_type,
            source      = _src,
            season      = season,
            episode     = episode,
            year        = year,
            ddl_links   = ddl_links  if is_merged else [],
            ddl_hosts   = ddl_hosts  if is_merged else [],
            ddl_db_ids  = ddl_db_ids if is_merged else [],
            ddl_link    = stream.get("ddl_link", ""),
            ddl_host    = stream.get("ddl_host", ""),
            ddl_db_id   = str(stream.get("db_id", "")),
        )
    else:
        token = encode_playback_token(
            stream_type = stream_type,
            infohash    = infohash,
            source      = _src,
            season      = season,
            episode     = episode,
            year        = year,
        )

    # ── Metadata ──────────────────────────────────────────────────────────────
    res_code  = stream.get("resolution") or 0
    qual_code = stream.get("quality")    or 0
    res_str   = RES_NAMES.get(res_code, "")
    qual_str  = QUAL_NAMES.get(qual_code, "")
    source    = stream.get("source") or "?"
    langs     = stream.get("langs") or []
    lang_str  = " ".join(l.upper() for l in langs)
    size      = stream.get("size") or 0
    size_fmt  = _fmt_size(size)
    title     = stream.get("torrent_name") or stream.get("title") or ""
    hdr_tags  = stream.get("hdr")       or []
    audio     = stream.get("audio")     or []
    channels  = stream.get("channels")  or []
    codec     = (stream.get("codec")    or "").upper()
    bit_depth = stream.get("bit_depth") or ""
    group     = stream.get("group")     or ""
    ddl_valid = stream.get("valid") if stream_type == "ddl" else None

    # 🟢 = verified DDL (valid=1), 🟣 = unverified DDL, 🔵 = torrent
    dot = ("🟢" if ddl_valid == 1 else "🟣") if stream_type == "ddl" else "🔵"

    if display_format == FORMAT_EPURE:
        name = f"Tor – {source}"

        # Line 1 — resolution • quality • HDR tags (torrents) or hosts (DDL)
        res_qual = " • ".join(filter(None, [
            f"{dot} {res_str}" if res_str else dot,
            qual_str,
        ]))
        if stream_type == "ddl":
            # DDL: replace HDR tags with host(s)
            host_list = stream.get("ddl_hosts") or [stream.get("ddl_host", "")]
            hosts_str = " • ".join(h.title() for h in host_list if h)
            line1 = " • ".join(filter(None, [res_qual, hosts_str]))
        else:
            # Torrent: append HDR tags
            line1 = " • ".join(filter(None, [res_qual, *hdr_tags]))

        # Line 2 — langs • size
        line2 = " • ".join(filter(None, [
            f"🌐 {lang_str}" if lang_str else "",
            size_fmt,
        ]))

        # Line 3 — codec • audio • group (+ bit_depth • channels si debug)
        audio_str = " • ".join(audio)
        line3_parts = [codec]
        if cfg.debug:
            ch_str = " • ".join(channels)
            line3_parts += [bit_depth, ch_str]
        line3_parts += [audio_str, group]
        line3 = " • ".join(filter(None, line3_parts))
        if line3:
            line3 = f"📦 {line3}"

        # Line 4 — filename
        tn    = (title[:57] + "…") if len(title) > 60 else title
        line4 = f"🗂️ {tn}" if tn else ""

        description = "\n".join(p for p in [line1, line2, line3, line4] if p)

    else:  # FORMAT_COMPACT
        name        = qual_str or res_str or source
        description = " • ".join(filter(None, [res_str, size_fmt]))

    hints: dict = {"notWebReady": True, "bingeGroup": f"tor-{infohash or source}"}
    if size > 0:
        hints["videoSize"] = size
    if title:
        hints["filename"] = title if title.lower().endswith(".mkv") else title + ".mkv"

    return {
        "name":          name,
        "description":   description,
        "url":           f"{base_url}/{b64}/playback/{token}",
        "behaviorHints": hints,
    }


def _fmt_size(size: int) -> str:
    if not size:
        return ""
    gb = size / (1 << 30)
    return f"{gb:.2f} GB" if gb >= 1 else f"{size >> 20} MB"


# ── Admin ─────────────────────────────────────────────────────────────────────

_ADMIN_HTML: str = ""

def _load_admin_html() -> str:
    global _ADMIN_HTML
    if not _ADMIN_HTML:
        p = Path(__file__).parent / "static" / "admin.html"
        _ADMIN_HTML = p.read_text(encoding="utf-8") if p.exists() else "<h1>admin.html introuvable</h1>"
    return _ADMIN_HTML


def _check_admin_key(key: str) -> bool:
    return bool(ADMIN_KEY) and key == ADMIN_KEY


@router.get("/admin/{key}", response_class=HTMLResponse)
async def admin_page(key: str):
    if not _check_admin_key(key):
        raise HTTPException(status_code=404)
    return _load_admin_html()


@router.get("/admin/{key}/data")
async def admin_data(key: str):
    if not _check_admin_key(key):
        raise HTTPException(status_code=404)

    db = PostgreSQLClient()
    results = await asyncio.gather(
        db.count_torrents(), db.count_ddl(), db.count_cached_torrents(),
        return_exceptions=True,
    )
    tor_count    = results[0] if not isinstance(results[0], Exception) else -1
    ddl_count    = results[1] if not isinstance(results[1], Exception) else -1
    cached_count = results[2] if not isinstance(results[2], Exception) else -1

    return JSONResponse({
        "uptime":           stats.uptime_str,
        "installs":         stats.installs,
        "searches":         stats.searches,
        "playbacks":        stats.playbacks,
        "playbacks_failed": stats.playbacks_failed,
        "avg_latency_ms":   stats.avg_latency_ms,
        "tor_count":        tor_count,
        "ddl_count":        ddl_count,
        "cached_count":     cached_count,
        "top_searches":     stats.top_searches(8),
        "top_sources":      stats.top_sources(5),
    })
