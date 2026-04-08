"""
services/postgresql.py
──────────────────────
Async PostgreSQL client (asyncpg) — remplace AppwriteClient.

Deux pools de connexions partagés (class-level) :
  _tor_pool → base `torrent`   (table: torrents)
  _ddl_pool → base `ddl`       (table: ddl_streams)

Initialisés au démarrage de l'app via PostgreSQLClient.init_pools().

Torrent stream dict produit :
  stream_type, db_id, infohash, torrent_name, season, episode, complete,
  quality(int), resolution(int), langs, size, seeders, source,
  cached(True|False|None), cache_checked(int unix ts)
  + HDR tags: hdr, audio, channels, codec, bit_depth, group

DDL stream dict produit :
  stream_type, db_id, site_id, ddl_link, ddl_host, title, season, episode,
  quality(int), resolution(int), langs, size, valid(int|None), source
"""

import asyncio
import json
import logging
import time
from time import perf_counter

import asyncpg

from settings import TOR_PG_DSN, DDL_PG_DSN

logger = logging.getLogger(__name__)


# ── Field helpers ─────────────────────────────────────────────────────────────

def _int(v, default: int = 0) -> int:
    try:
        return int(v) if v not in (None, "") else default
    except (ValueError, TypeError):
        return default


def _langs(s) -> list[str]:
    return [lg for lg in (s or "").split(",") if lg]


def _int_list(s) -> list[int] | None:
    parts = [x for x in (s or "").split(",") if x not in ("", "0")]
    return [int(x) for x in parts] or None


def _cached_field(v) -> bool | None:
    if v == 1 or v is True:  return True
    if v == 0 or v is False: return False
    return None


def _parse_tags(r: dict) -> dict:
    raw = r.get("tags")
    if not raw:
        return {"hdr": [], "audio": [], "channels": [], "codec": "", "bit_depth": "", "group": ""}
    try:
        t = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return {"hdr": [], "audio": [], "channels": [], "codec": "", "bit_depth": "", "group": ""}
    return {
        "hdr":       t.get("hdr")       or [],
        "audio":     t.get("audio")     or [],
        "channels":  t.get("channels")  or [],
        "codec":     t.get("codec")     or "",
        "bit_depth": t.get("bit_depth") or "",
        "group":     t.get("group")     or "",
    }


# ── Transform ─────────────────────────────────────────────────────────────────

def _transform_torrent(r: dict) -> dict:
    tags = _parse_tags(r)
    return {
        "stream_type":   "torrent",
        "db_id":         r.get("id"),
        "infohash":      (r.get("info_hash") or "").lower(),
        "torrent_name":  r.get("torrent_name") or "",
        "season":        _int_list(r.get("season")),
        "episode":       _int_list(r.get("episode")),
        "complete":      _int(r.get("complete")) == 1,
        "quality":       _int(r.get("quality")) or 20,
        "resolution":    _int(r.get("resolution")),
        "langs":         _langs(r.get("langs")),
        "hdr":           tags["hdr"],
        "audio":         tags["audio"],
        "channels":      tags["channels"],
        "codec":         tags["codec"],
        "bit_depth":     tags["bit_depth"],
        "group":         tags["group"],
        "size":          _int(r.get("size")),
        "seeders":       _int(r.get("seeders")),
        "source":        r.get("source") or "",
        "cached":        _cached_field(r.get("cached")),
        "cache_checked": _int(r.get("cache_checked")),
    }


def _transform_ddl(r: dict) -> dict:
    v = r.get("valid")
    return {
        "stream_type": "ddl",
        "db_id":       r.get("id"),
        "site_id":     str(r.get("site_id") or ""),
        "ddl_link":    r.get("link")    or "",
        "ddl_host":    r.get("host")    or "",
        "title":       r.get("title")   or "",
        "season":      _int_list(r.get("season")),
        "episode":     _int_list(r.get("episode")),
        "quality":     _int(r.get("quality")) or 20,
        "resolution":  _int(r.get("resolution")),
        "langs":       _langs(r.get("langs")),
        "hdr":         [],
        "audio":       [],
        "channels":    [],
        "codec":       "",
        "bit_depth":   "",
        "group":       "",
        "size":        _int(r.get("size")),
        "valid":       int(v) if v is not None else None,
        "source":      r.get("source") or "",
    }


# ── Client ────────────────────────────────────────────────────────────────────

class PostgreSQLClient:
    _tor_pool: asyncpg.Pool | None = None
    _ddl_pool: asyncpg.Pool | None = None

    @classmethod
    async def init_pools(cls) -> None:
        if not TOR_PG_DSN or not DDL_PG_DSN:
            logger.error("PostgreSQL │ TOR_PG_DSN ou DDL_PG_DSN non défini — pools non initialisés")
            return
        try:
            cls._tor_pool = await asyncpg.create_pool(TOR_PG_DSN, min_size=1, max_size=5, timeout=10)
            cls._ddl_pool = await asyncpg.create_pool(DDL_PG_DSN, min_size=1, max_size=5, timeout=10)
            logger.info("PostgreSQL │ pools initialisés (tor=%s ddl=%s)",
                        TOR_PG_DSN.split("/")[-1].split("?")[0],
                        DDL_PG_DSN.split("/")[-1].split("?")[0])
        except Exception as exc:
            logger.error("PostgreSQL │ échec init pools: %s — l'app démarre sans DB", exc)
            cls._tor_pool = None
            cls._ddl_pool = None

    @classmethod
    async def close_pools(cls) -> None:
        if cls._tor_pool: await cls._tor_pool.close()
        if cls._ddl_pool: await cls._ddl_pool.close()
        logger.info("PostgreSQL │ pools fermés")

    # ── Read ──────────────────────────────────────────────────────────────────

    async def search(self, tmdb_id: str) -> dict[str, list[dict]]:
        """Interroge les deux bases en parallèle — une seule ligne de log."""
        t0 = perf_counter()

        async def _tor():
            t    = perf_counter()
            rows = await self._tor_pool.fetch(
                "SELECT * FROM torrents WHERE tmdb_id = $1 ORDER BY seeders DESC", tmdb_id
            )
            return [_transform_torrent(dict(r)) for r in rows], (perf_counter() - t) * 1000

        async def _ddl():
            t    = perf_counter()
            rows = await self._ddl_pool.fetch(
                "SELECT * FROM ddl_streams WHERE tmdb_id = $1 ORDER BY quality DESC", tmdb_id
            )
            return [_transform_ddl(dict(r)) for r in rows], (perf_counter() - t) * 1000

        tor_res, ddl_res = await asyncio.gather(_tor(), _ddl(), return_exceptions=True)

        if isinstance(tor_res, Exception):
            logger.error("PostgreSQL │ torrents: %s", tor_res)
            torrents, tor_ms = [], 0.0
        else:
            torrents, tor_ms = tor_res

        if isinstance(ddl_res, Exception):
            logger.error("PostgreSQL │ DDL: %s", ddl_res)
            ddl, ddl_ms = [], 0.0
        else:
            ddl, ddl_ms = ddl_res

        logger.info("tmdb=%-9s  tor(%dms/%d)  ddl(%dms/%d)  Δ%dms",
                    tmdb_id, round(tor_ms), len(torrents), round(ddl_ms), len(ddl),
                    round((perf_counter() - t0) * 1000))
        return {"torrents": torrents, "ddl": ddl}

    # ── Write ─────────────────────────────────────────────────────────────────

    async def update_torrents_cache(self, updates: list[tuple[str, bool]]) -> None:
        """Batch-update cached + cache_checked. Un seul aller-retour DB via executemany."""
        valid = [(db_id, cached) for db_id, cached in updates if db_id is not None]
        if not valid:
            return
        now = int(time.time())
        await self._tor_pool.executemany(
            "UPDATE torrents SET cached=$1, cache_checked=$2, updated_at=$3 WHERE id=$4",
            [(1 if cached else 0, now, now, int(db_id)) for db_id, cached in valid],
        )
        logger.info("PostgreSQL │ %d torrent(s) cache updated", len(valid))

    async def update_ddl_valid(self, db_id, valid: int) -> None:
        if db_id is None:
            return
        now = int(time.time())
        await self._ddl_pool.execute(
            "UPDATE ddl_streams SET valid=$1, updated_at=$2 WHERE id=$3",
            valid, now, int(db_id),
        )
        logger.info("PostgreSQL │ DDL id=%s → valid=%d", db_id, valid)

    async def count_torrents(self) -> int:
        row = await self._tor_pool.fetchrow("SELECT COUNT(*) FROM torrents")
        return int(row[0]) if row else 0

    async def count_ddl(self) -> int:
        row = await self._ddl_pool.fetchrow("SELECT COUNT(*) FROM ddl_streams")
        return int(row[0]) if row else 0

    async def count_cached_torrents(self) -> int:
        row = await self._tor_pool.fetchrow("SELECT COUNT(*) FROM torrents WHERE cached = 1")
        return int(row[0]) if row else 0

    async def update_ddl_link_and_valid(self, db_id, link: str, valid: int) -> None:
        if db_id is None:
            return
        now = int(time.time())
        await self._ddl_pool.execute(
            "UPDATE ddl_streams SET link=$1, valid=$2, updated_at=$3 WHERE id=$4",
            link, valid, now, int(db_id),
        )
        logger.info("PostgreSQL │ DDL id=%s → link updated, valid=%d", db_id, valid)
