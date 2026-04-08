"""
services/alldebrid.py
─────────────────────
AllDebrid API client. Async (httpx).

  check_cache(torrents)      – batch instant-availability check (modifies cached in-place)
  resolve_stream(infohash)   – upload magnet → file tree → pick file → CDN URL → delete
  unlock_link(url)           – direct link → (cdn_url | None, error_code | None)
  redirector_link(url)       – follow AllDebrid redirector → real link

unlock_link returns a 2-tuple so callers can detect permanent failures (LINK_DOWN)
and update the database accordingly without re-trying.

Retry policy: ConnectError / TimeoutException retried up to 3×, 0.5 s delay.
Logic errors (bad key, link down…) are NOT retried.

Permanent link-down error codes (safe to mark valid=0 in DB):
  LINK_DOWN, LINK_BLOCKED, LINK_NOT_SUPPORTED, LINK_PASS_NEEDED
"""

import asyncio
import logging

import httpx

from settings import ALLDEBRID_BASE_URL, ALLDEBRID_AGENT, ALLDEBRID_BATCH_SIZE, ALLDEBRID_PROXY
from utils.episode_selector import find_best_file

logger = logging.getLogger(__name__)

_RETRY_ATTEMPTS = 3
_RETRY_DELAY    = 0.5

# AllDebrid error codes that mean the link is permanently dead
_DEAD_LINK_CODES: frozenset[str] = frozenset({
    "LINK_DOWN",
    "LINK_BLOCKED",
    "LINK_NOT_SUPPORTED",
    "LINK_PASS_NEEDED",
})


async def _retry(coro_fn):
    last_exc = None
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            return await coro_fn()
        except (httpx.ConnectError, httpx.TimeoutException, httpx.ProxyError, OSError, UnboundLocalError) as exc:
            last_exc = exc
            if attempt < _RETRY_ATTEMPTS:
                logger.warning("AllDebrid │ network error (attempt %d/%d): %s: %s",
                               attempt, _RETRY_ATTEMPTS, type(exc).__name__, exc)
                await asyncio.sleep(_RETRY_DELAY)
    raise last_exc


class AllDebridClient:
    __slots__ = ("api_key", "client")

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.client  = httpx.AsyncClient(
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
                keepalive_expiry=60.0,
            ),
            timeout=httpx.Timeout(connect=5.0, read=12.0, write=5.0, pool=5.0),
            follow_redirects=True,
            proxy=ALLDEBRID_PROXY or None,
        )

    # ── Cache check ──────────────────────────────────────────────────────────

    async def check_cache(self, torrents: list[dict]) -> list[dict]:
        """
        Sets cached=True/False in-place on each dict.
        Only processes streams where cached is None (unknown).
        """
        to_check = [t for t in torrents if t.get("cached") is None]
        if not to_check:
            return torrents

        hash_map: dict[str, list[dict]] = {}
        for t in to_check:
            h = (t.get("infohash") or "").strip().lower()
            if h:
                hash_map.setdefault(h, []).append(t)

        unique  = list(hash_map)
        batches = [unique[i : i + ALLDEBRID_BATCH_SIZE] for i in range(0, len(unique), ALLDEBRID_BATCH_SIZE)]

        async def _safe_batch(batch: list[str], idx: int) -> None:
            try:
                await self._check_batch(batch, hash_map)
            except Exception as exc:
                logger.error("AllDebrid │ batch %d failed: %s: %s", idx, type(exc).__name__, exc)
                _mark(batch, hash_map, False)

        await asyncio.gather(*(_safe_batch(b, i) for i, b in enumerate(batches)))

        cached_n = sum(1 for t in torrents if t.get("cached") is True)
        logger.info("AllDebrid │ cache  %d hashes → %d/%d cached", len(unique), cached_n, len(torrents))
        return torrents

    # ── Torrent resolution ────────────────────────────────────────────────────

    async def resolve_stream(
        self,
        infohash: str,
        season:   int | None = None,
        episode:  int | None = None,
        year:     int | None = None,
    ) -> str | None:
        logger.info("AllDebrid │ resolve  hash=%.12s…  s=%s e=%s", infohash, season, episode)
        magnet_id = await self._upload_magnet(infohash)
        if magnet_id is None:
            return None
        raw_files = await self._fetch_files(magnet_id)
        asyncio.create_task(self._delete_magnet(magnet_id))   # fire-and-forget, ne bloque pas
        if raw_files is None:
            return None
        flat = _flatten(raw_files)
        best = find_best_file(flat, season=season, episode=episode, year=year)
        if best is None:
            logger.warning("AllDebrid │ no matching file found")
            return None
        logger.info("AllDebrid │ selected → %s (%.2f GB)", best["n"], best.get("s", 0) / 1e9)
        url, _ = await self._unlock(best["l"])
        return url

    # ── DDL resolution ────────────────────────────────────────────────────────

    async def unlock_link(self, url: str) -> tuple[str | None, str | None]:
        """
        Unlock a direct link via AllDebrid.
        Returns (cdn_url, error_code):
          - (url, None)        on success
          - (None, error_code) on failure — error_code in _DEAD_LINK_CODES means permanent
          - (None, None)       on unclassified failure
        """
        return await self._unlock(url)

    async def redirector_link(self, url: str) -> tuple[str | None, bool]:
        """
        Follow AllDebrid redirector → real host link.
        Returns (resolved_url | None, is_transient).
        is_transient=True  → REDIRECTOR_ERROR, lien potentiellement valide, ne pas marquer dead.
        is_transient=False → erreur permanente, lien mort.
        """
        for attempt in range(1, 5):
            body = await _retry(lambda: self._redirector_call(url))
            if body.get("status") != "success":
                err      = body.get("error") or {}
                err_code = err.get("code", "") if isinstance(err, dict) else str(err)
                err_msg  = err.get("message", "") if isinstance(err, dict) else str(err)
                is_redir_err = "REDIRECTOR_ERROR" in str(err_code) or "Could not extract" in str(err_msg)
                if is_redir_err and attempt < 4:
                    logger.warning("AllDebrid │ redirector attempt %d/4 – retrying in 1s", attempt)
                    await asyncio.sleep(1.0)
                    continue
                logger.error("AllDebrid │ redirector failed: %s", err)
                return None, is_redir_err
            links = body.get("data", {}).get("links") or []
            if not links:
                logger.error("AllDebrid │ redirector: no links for %.60s…", url)
                return None, False
            resolved = links[0].get("link") if isinstance(links[0], dict) else str(links[0])
            logger.info("AllDebrid │ redirector  %.40s…  →  %.40s…", url, resolved)
            return resolved, False
        return None, True

    async def _redirector_call(self, url: str) -> dict:
        r = await self.client.get(
            f"{ALLDEBRID_BASE_URL}/link/redirector",
            params={"link": url, "apikey": self.api_key, "agent": ALLDEBRID_AGENT},
        )
        r.raise_for_status()
        return r.json()

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _upload_magnet(self, infohash: str) -> int | None:
        async def _call():
            r = await self.client.post(
                f"{ALLDEBRID_BASE_URL}/magnet/upload",
                data={"magnets[]": infohash, "apikey": self.api_key, "agent": ALLDEBRID_AGENT},
            )
            r.raise_for_status()
            return r.json()

        body = await _retry(_call)
        if body.get("status") != "success":
            logger.error("AllDebrid │ upload failed: %s", body.get("error"))
            return None
        magnets = body.get("data", {}).get("magnets") or []
        if not magnets:
            return None
        mid = magnets[0].get("id")
        logger.debug("AllDebrid │ uploaded magnet id=%s", mid)
        return mid

    async def _fetch_files(self, magnet_id: int) -> list | None:
        async def _call():
            r = await self.client.post(
                f"{ALLDEBRID_BASE_URL}/magnet/files",
                data={"id[]": [magnet_id], "apikey": self.api_key, "agent": ALLDEBRID_AGENT},
            )
            r.raise_for_status()
            return r.json()

        body = await _retry(_call)
        if body.get("status") != "success":
            logger.error("AllDebrid │ files fetch failed: %s", body.get("error"))
            return None
        magnets = body.get("data", {}).get("magnets") or []
        if not magnets:
            return None
        return magnets[0].get("files") or []

    async def _unlock(self, url: str) -> tuple[str | None, str | None]:
        """Internal unlock. Returns (cdn_url | None, error_code | None)."""
        async def _call():
            r = await self.client.get(
                f"{ALLDEBRID_BASE_URL}/link/unlock",
                params={"link": url, "apikey": self.api_key, "agent": ALLDEBRID_AGENT},
            )
            r.raise_for_status()
            return r.json()

        body = await _retry(_call)
        if body.get("status") != "success":
            err      = body.get("error") or {}
            err_code = err.get("code", "") if isinstance(err, dict) else ""
            logger.error("AllDebrid │ unlock failed: %s", err)
            return None, err_code or None
        cdn_url = body["data"]["link"]
        logger.info("AllDebrid │ unlock  %.40s…  →  %.40s…", url, cdn_url)
        return cdn_url, None

    async def _delete_magnet(self, magnet_id: int) -> None:
        await self._delete_magnets([magnet_id])

    async def _delete_magnets(self, ids: list[int]) -> None:
        try:
            await self.client.post(
                f"{ALLDEBRID_BASE_URL}/magnet/delete",
                data={"ids[]": ids, "apikey": self.api_key, "agent": ALLDEBRID_AGENT},
                timeout=10,
            )
        except Exception as exc:
            logger.warning("AllDebrid │ delete failed ids=%s: %s", ids, exc)

    async def _check_batch(self, batch: list[str], hash_map: dict[str, list[dict]]) -> None:
        payload = {"agent": ALLDEBRID_AGENT, "apikey": self.api_key, "magnets[]": batch}

        async def _call():
            try:
                r = await self.client.post(f"{ALLDEBRID_BASE_URL}/magnet/upload", data=payload)
            except Exception as e:
                print(e)
            #r.raise_for_status()
            return r.json()

        body = await _retry(_call)
        if body.get("status") != "success":
            logger.warning("AllDebrid │ cache batch error: %s", body.get("error", {}))
            _mark(batch, hash_map, False)
            return

        ids_to_delete: list[int] = []
        for m in body.get("data", {}).get("magnets", []):
            ad_hash  = str(m.get("hash") or m.get("magnet", "")).strip().lower()
            is_ready = bool(m.get("ready", False))
            if "id" in m:
                ids_to_delete.append(m["id"])
            if ad_hash in hash_map:
                _mark_list(hash_map[ad_hash], is_ready)
            else:
                for local_hash, objs in hash_map.items():
                    if local_hash in ad_hash:
                        _mark_list(objs, is_ready)
                        break

        if ids_to_delete:
            asyncio.create_task(self._delete_magnets(ids_to_delete))  # fire-and-forget

    def is_dead_link_error(self, error_code: str | None) -> bool:
        return bool(error_code and error_code in _DEAD_LINK_CODES)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _flatten(entries: list, path: str = "") -> list[dict]:
    files = []
    for item in entries:
        name = item.get("n", "")
        if "l" in item:
            files.append({"n": name, "l": item["l"], "s": item.get("s", 0), "path": path})
        if "e" in item:
            files.extend(_flatten(item["e"], f"{path}/{name}".strip("/")))
    return files


def _mark(batch: list[str], hash_map: dict[str, list[dict]], value: bool) -> None:
    for h in batch:
        _mark_list(hash_map.get(h, []), value)


def _mark_list(objs: list[dict], value: bool) -> None:
    for obj in objs:
        obj["cached"] = value
