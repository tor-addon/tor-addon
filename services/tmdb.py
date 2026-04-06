"""
services/tmdb.py
────────────────
TMDB async client.

Returns tmdb_id, year and media type for a given imdb_id.
Results cached in-process up to 500 entries.

This is called BEFORE Turso so we can query by tmdb_id (primary DB key).
"""

import logging

import httpx

from settings import TMDB_BASE_URL, TMDB_DEFAULT_KEY

logger = logging.getLogger(__name__)

_cache: dict[str, dict] = {}  # croît sans limite — vit tant que le serveur tourne


class TMDBClient:
    __slots__ = ("_client",)

    def __init__(self, api_key: str | None = None) -> None:
        self._client = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {api_key or TMDB_DEFAULT_KEY}",
                "Accept": "application/json",
            },
            timeout=8,
        )

    async def fetch(self, imdb_id: str) -> dict:
        """
        Return {"tmdb_id": str, "year": int|None, "type": "movie"|"series"|None, "title": str}.
        Never raises — on error returns all fields None/"".
        """
        if imdb_id in _cache:
            logger.debug("TMDB │ cache HIT %s", imdb_id)
            return _cache[imdb_id]

        try:
            result = await self._fetch_uncached(imdb_id)
        except Exception as exc:
            logger.warning("TMDB │ %s failed: %s", imdb_id, exc)
            result = {"tmdb_id": None, "year": None, "type": None, "title": ""}

        _cache[imdb_id] = result
        return result

    async def _fetch_uncached(self, imdb_id: str) -> dict:
        r = await self._client.get(
            f"{TMDB_BASE_URL}/find/{imdb_id}",
            params={"external_source": "imdb_id"},
        )
        r.raise_for_status()
        data = r.json()

        if data.get("movie_results"):
            item       = data["movie_results"][0]
            media_type = "movie"
            tmdb_id    = str(item["id"])
            date_raw   = item.get("release_date", "")
            title      = item.get("title") or item.get("original_title") or ""
        elif data.get("tv_results"):
            item       = data["tv_results"][0]
            media_type = "series"
            tmdb_id    = str(item["id"])
            date_raw   = item.get("first_air_date", "")
            title      = item.get("name") or item.get("original_name") or ""
        else:
            logger.warning("TMDB │ no result for %s", imdb_id)
            return {"tmdb_id": None, "year": None, "type": None, "title": ""}

        year: int | None = None
        try:
            year = int(date_raw.split("-")[0]) if date_raw else None
        except (ValueError, IndexError):
            pass

        logger.info("TMDB │ %s → tmdb_id=%s  type=%s  year=%s  title=%s",
                    imdb_id, tmdb_id, media_type, year, title)
        return {"tmdb_id": tmdb_id, "year": year, "type": media_type, "title": title}
