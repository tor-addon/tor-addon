"""
services/darki.py
─────────────────
Decode Darki numeric link codes to real 1fichier URLs.

Darki rows in the DDL DB store a short numeric code as `link` (e.g. "19056310").
This client calls the Movix decode API to get the real download URL,
which is then passed to AllDebrid for unlocking.

API: GET https://api.movix.blog/api/darkiworld/decode/{id}
Response: {"success": true, "embed_url": {"lien": "https://1fichier.com/..."}, ...}

After a successful decode the caller persists the real URL back to the DB
so future requests skip this step entirely.
"""

import logging

import httpx

from settings import DARKI_DECODE_BASE_URL, DARKI_DECODE_ORIGIN

logger = logging.getLogger(__name__)

_HEADERS = {
    "accept":             "application/json, text/plain, */*",
    "accept-language":    "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "origin":             DARKI_DECODE_ORIGIN,
    "referer":            f"{DARKI_DECODE_ORIGIN}/",
    "sec-ch-ua":          '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
    "sec-ch-ua-mobile":   "?0",
    "sec-ch-ua-platform": '"macOS"',
    "sec-fetch-dest":     "empty",
    "sec-fetch-mode":     "cors",
    "sec-fetch-site":     "cross-site",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/146.0.0.0 Safari/537.36"
    ),
}


class DarkiClient:
    __slots__ = ("_client",)

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            headers=_HEADERS,
            timeout=10,
            follow_redirects=True,
        )

    async def decode(self, code: str) -> str | None:
        """
        Decode a Darki numeric code → real 1fichier URL.
        Returns None on any failure.
        """
        try:
            r = await self._client.get(f"{DARKI_DECODE_BASE_URL}/{code}")
            r.raise_for_status()
            body = r.json()
        except Exception as exc:
            logger.error("Darki │ decode error code=%s: %s", code, exc)
            return None

        if not body.get("success"):
            logger.warning("Darki │ decode failed code=%s: %s", code, body)
            return None

        url = (body.get("embed_url") or {}).get("lien")
        if not url:
            logger.warning("Darki │ no lien in response for code=%s", code)
            return None

        logger.info("Darki │ code=%s → %.60s…", code, url)
        return url
