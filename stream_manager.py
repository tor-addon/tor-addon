"""
stream_manager.py
─────────────────
Pipeline orchestrator.

get_streams() flow:
  1. TMDB       → tmdb_id + year (cached in-process)
  2. PostgreSQL → torrents + DDL en parallèle (query by tmdb_id)
  3. Deduplicate (infohash + size)
  4. Filter (language, episode coverage)
  5. Rank (numeric quality/resolution codes)
  6. Smart AllDebrid cache check:
       cached=True  → skip (permanent, AllDebrid keeps files forever)
       cached=False + checked < CACHE_RECHECK_TTL → skip (recently negative)
       cached=None  → check
       cached=False + checked > CACHE_RECHECK_TTL → re-check
  7. Update Appwrite DB with new cache status (batch, fire-and-forget)
  8. Return: cached torrents + valid DDL

resolve_stream() flow:
  torrent → AllDebrid magnet → episode_selector → CDN URL
  ddl     → detect link type:
              numeric code (Darki) → Darki decode → 1fichier URL → AllDebrid unlock
                                     persist real URL + valid in DB
              wawacity host        → AllDebrid redirector → real URL → AllDebrid unlock
              other http URL       → AllDebrid unlock directly
              all DDL paths: update DB valid=1 on success, valid=0 on dead link
"""

import asyncio
import logging
import time
from urllib.parse import urlparse

from config             import UserConfig
from services.alldebrid    import AllDebridClient, _DEAD_LINK_CODES
from services.postgresql   import PostgreSQLClient
from services.darki        import DarkiClient
from services.tmdb       import TMDBClient
from settings           import DDL_REDIRECTOR_HOSTS, DDL_REDIRECTOR_DOMAINS
from utils.deduplicator import Deduplicator
from utils.filtering    import filter_streams
from utils.ranking      import rank_and_sort

logger = logging.getLogger(__name__)

# Torrents that are NOT cached: re-check after this delay.
# AllDebrid caches files permanently once they're in → cached=True never needs re-check.
# cached=False means "not yet in AllDebrid" — recheck weekly in case someone added it.
CACHE_RECHECK_TTL = 7 * 24 * 3600  # 7 days in seconds


def _bg(coro, label: str) -> None:
    """Schedule a coroutine as a fire-and-forget task with error logging."""
    async def _run():
        try:
            await coro
        except Exception as exc:
            logger.error("BG task [%s] failed: %s", label, exc)
    asyncio.create_task(_run())


class StreamManager:
    __slots__ = ("_db", "_ad", "_tmdb", "_darki", "_config", "last_title")

    def __init__(self, config: UserConfig) -> None:
        self._config  = config
        self._db      = PostgreSQLClient()
        self._ad      = AllDebridClient(config.alldebrid_key)
        self._tmdb    = TMDBClient()
        self._darki   = DarkiClient()
        self.last_title: str = ""   # titre du dernier get_streams(), pour admin stats

    # ── Public API ────────────────────────────────────────────────────────────

    async def get_streams(
        self,
        imdb_id: str,
        season:  int | None = None,
        episode: int | None = None,
    ) -> list[dict]:
        t0 = time.perf_counter()
        logger.info("━━ %s  s=%s e=%s  langs=%s", imdb_id, season, episode, self._config.languages)

        # ── 1. TMDB → tmdb_id (cached in-process after first hit) ─────────────
        tmdb_info       = await self._tmdb.fetch(imdb_id)
        tmdb_id         = tmdb_info.get("tmdb_id")
        year            = tmdb_info.get("year")
        self.last_title = tmdb_info.get("title") or ""

        if not tmdb_id:
            logger.error("[%s] TMDB: no tmdb_id found – aborting", imdb_id)
            return []

        # ── 2. Appwrite: both collections in parallel ─────────────────────────
        data     = await self._db.search(tmdb_id)
        torrents = data.get("torrents", [])
        ddl      = data.get("ddl", [])
        all_raw  = torrents + ddl

        if not all_raw:
            logger.info("━━ %s  done %.0fms  0 streams", imdb_id, _ms(t0))
            return []

        # ── 3. Dedup ──────────────────────────────────────────────────────────
        dedup   = Deduplicator()
        deduped = [s for s in all_raw if dedup.is_new(s)]

        # ── 4. Filter ─────────────────────────────────────────────────────────
        cfg = self._config
        filtered = filter_streams(
            deduped, cfg.languages, season, episode,
            max_size_gb = cfg.max_size_gb,
            min_seeders = cfg.min_seeders,
            exclude_cam = cfg.exclude_cam,
        )
        if not filtered:
            logger.info("━━ %s  done %.0fms  0 streams (filtered)", imdb_id, _ms(t0))
            return []

        # ── 4b. Merge same-quality Wawacity DDL links ─────────────────────────
        filtered = _merge_wawacity(filtered)

        # ── 5. Rank ───────────────────────────────────────────────────────────
        rank_and_sort(filtered, hdr_boost=cfg.hdr_boost)

        # Attach year for episode_selector at playback time
        if year:
            for s in filtered:
                s.setdefault("year", year)

        # ── 6. Smart cache check ──────────────────────────────────────────────
        tor_streams = [s for s in filtered if s["stream_type"] == "torrent"]
        ddl_streams = [s for s in filtered if s["stream_type"] == "ddl"]

        if tor_streams:
            db_cached = [t for t in tor_streams if t.get("cached") is True]
            to_check  = _classify_for_check(tor_streams)
            ad_found  = 0
            if to_check:
                for t in to_check:
                    t["cached"] = None
                await self._ad.check_cache(to_check)
                ad_found = sum(1 for t in to_check if t.get("cached") is True)
                updates = [
                    (t["db_id"], t["cached"])
                    for t in to_check
                    if t.get("db_id") is not None and t.get("cached") is not None
                ]
                if updates:
                    _bg(self._db.update_torrents_cache(updates), "cache-update")

        # ── 8. Final assembly ─────────────────────────────────────────────────
        cached_torrents = [t for t in tor_streams if t.get("cached") is True]
        final = cached_torrents + ddl_streams
        rank_and_sort(final, hdr_boost=cfg.hdr_boost)

        cache_info = ""
        if tor_streams:
            cache_info = f"  cache(DB:{len(db_cached)} AD:{ad_found}/{len(to_check)})"
        logger.info("━━ %s  done %.0fms%s  DDL:%d  → %d streams",
                    imdb_id, _ms(t0), cache_info, len(ddl_streams), len(final))
        return final

    # ── Resolve ───────────────────────────────────────────────────────────────

    async def resolve_stream(
        self,
        stream:  dict,
        season:  int | None = None,
        episode: int | None = None,
        year:    int | None = None,
    ) -> str | None:
        if stream.get("stream_type") == "ddl":
            return await self._resolve_ddl(stream)
        return await self._ad.resolve_stream(
            stream.get("infohash", ""),
            season=season,
            episode=episode,
            year=year,
        )

    # ── DDL resolution ────────────────────────────────────────────────────────

    async def _resolve_ddl(self, stream: dict) -> str | None:
        # Support both single-link and merged multi-link (Wawacity) streams.
        links  = stream.get("ddl_links") or ([stream["ddl_link"]] if stream.get("ddl_link") else [])
        hosts  = stream.get("ddl_hosts") or ([stream.get("ddl_host", "")] * len(links))
        db_ids = stream.get("ddl_db_ids") or ([stream.get("db_id")] * len(links))

        if not links:
            logger.error("StreamManager │ DDL: no link in token")
            return None

        if len(links) == 1:
            return await self._resolve_one_ddl(links[0], (hosts[0] or "").lower(), db_ids[0])

        # Multiple links (Wawacity): race all in parallel, return the fastest working one
        tasks = [
            asyncio.create_task(
                self._resolve_one_ddl(link, (host or "").lower(), db_id)
            )
            for link, host, db_id in zip(links, hosts, db_ids)
        ]
        result = None
        pending = set(tasks)
        try:
            while pending and result is None:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if not task.exception():
                        r = task.result()
                        if r and result is None:
                            result = r
        finally:
            for t in pending:
                t.cancel()
        return result

    async def _resolve_one_ddl(self, link: str, host: str, db_id) -> str | None:
        # ── Darki: numeric code → decode → 1fichier URL ───────────────────────
        if not link.startswith("http"):
            return await self._resolve_darki(link, db_id)

        # ── Redirector: wawacity host OR link-protection domain ───────────────
        if _needs_redirector(link, host):
            logger.info("StreamManager │ DDL redirector  host=%s  url=%.60s…", host, link)
            real_url, is_transient = await self._ad.redirector_link(link)
            if not real_url:
                if is_transient:
                    logger.warning("StreamManager │ DDL redirector transitoire (REDIRECTOR_ERROR)  host=%s  id=%s → DB inchangée", host, db_id)
                else:
                    logger.warning("StreamManager │ DDL redirector échec permanent  host=%s  id=%s → valid=0", host, db_id)
                    if db_id is not None:
                        _bg(self._db.update_ddl_valid(db_id, 0), "redirect-fail")
                return None
            link = real_url

        # ── Direct AllDebrid unlock ────────────────────────────────────────────
        logger.info("StreamManager │ DDL unlock  host=%s  url=%.60s…", host, link)
        cdn_url, error_code = await self._ad.unlock_link(link)

        if cdn_url:
            if db_id is not None:
                _bg(self._db.update_ddl_valid(db_id, 1), "ddl-valid-ok")
        elif error_code in _DEAD_LINK_CODES:
            logger.warning("StreamManager │ DDL dead link  code=%s  id=%s", error_code, db_id)
            if db_id is not None:
                _bg(self._db.update_ddl_valid(db_id, 0), "ddl-valid-dead")

        return cdn_url

    async def _resolve_darki(self, code: str, db_id) -> str | None:
        logger.info("StreamManager │ Darki decode  code=%s", code)
        real_url = await self._darki.decode(code)
        if not real_url:
            logger.warning("StreamManager │ Darki decode failed  code=%s", code)
            return None

        cdn_url, error_code = await self._ad.unlock_link(real_url)
        if cdn_url:
            if db_id is not None:
                _bg(self._db.update_ddl_link_and_valid(db_id, real_url, 1), "darki-link-update")
        elif error_code in _DEAD_LINK_CODES:
            logger.warning("StreamManager │ Darki dead link  code=%s  err=%s  id=%s", code, error_code, db_id)
            if db_id is not None:
                _bg(self._db.update_ddl_valid(db_id, 0), "darki-valid-dead")

        return cdn_url


# ── Helpers ───────────────────────────────────────────────────────────────────

def _merge_wawacity(streams: list[dict]) -> list[dict]:
    """
    Group Wawacity DDL streams par site_id (même upload, hôtes différents)
    en un seul stream avec plusieurs liens. Au playback, les liens sont
    essayés dans l'ordre — le premier qui fonctionne est utilisé.

    Clé de groupement :
      - Si site_id présent → grouper par site_id (plus précis)
      - Sinon → fallback sur quality/resolution/langs/season/episode
    """
    result: list[dict] = []
    groups: dict = {}  # key → merged stream (already in result)

    for s in streams:
        if s.get("stream_type") != "ddl" or (s.get("source") or "").lower() != "wawacity":
            result.append(s)
            continue

        site_id = (s.get("site_id") or "").strip()
        if site_id:
            key = ("site_id", site_id)
        else:
            key = (
                "fallback",
                s.get("quality"),
                s.get("resolution"),
                tuple(sorted(s.get("langs") or [])),
                tuple(s.get("season") or []),
                tuple(s.get("episode") or []),
            )

        if key in groups:
            g = groups[key]
            g["ddl_links"].append(s["ddl_link"])
            g["ddl_hosts"].append(s["ddl_host"])
            g["ddl_db_ids"].append(str(s["db_id"]) if s.get("db_id") is not None else "")
        else:
            merged = dict(s)
            merged["ddl_links"]  = [s["ddl_link"]]
            merged["ddl_hosts"]  = [s["ddl_host"]]
            merged["ddl_db_ids"] = [str(s["db_id"]) if s.get("db_id") is not None else ""]
            groups[key] = merged
            result.append(merged)

    merged_count = sum(1 for g in groups.values() if len(g["ddl_links"]) > 1)
    if merged_count:
        logger.info("Wawacity │ %d groupe(s) mergés (multi-liens)", merged_count)
    return result


def _classify_for_check(torrents: list[dict]) -> list[dict]:
    """
    Return only the torrents that actually need an AllDebrid cache check.

    Rules:
      cached=True               → permanent, never re-check
      cached=False, recent      → still not available, skip until TTL expires
      cached=None               → unknown, must check
      cached=False, old         → might be available now, re-check
    """
    now = int(time.time())
    needs_check = []
    skipped = 0

    for t in torrents:
        cached     = t.get("cached")
        checked_at = t.get("cache_checked") or 0

        if cached is True:
            skipped += 1
            continue
        if cached is False and (now - checked_at) < CACHE_RECHECK_TTL:
            skipped += 1
            continue
        needs_check.append(t)

    if skipped:
        logger.debug("Cache check │ skipped %d (already known)", skipped)
    return needs_check


def _needs_redirector(link: str, host: str) -> bool:
    """
    True if this DDL link must go through AllDebrid's /link/redirector first.

    Two cases:
      1. ddl_host is a known aggregator (wawacity etc.)
      2. The stored link itself is a link-protection domain (dl-protect.link etc.)
    """
    if host in DDL_REDIRECTOR_HOSTS:
        return True
    domain = urlparse(link).netloc.lower().removeprefix("www.")
    return domain in DDL_REDIRECTOR_DOMAINS


def _ms(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000
