"""
utils/filtering.py
──────────────────
Filter pre-structured streams from DB.
No PTT, no fuzzy match — data is already structured and keyed by TMDB ID.

Rules (in order):
  1. Language     – at least one user language must be in stream langs
  2. Episode      – series: stream must cover requested season/episode
  3. DDL validity – discard dead links (valid == 0 or -1)
  4. Size         – exclude if file > max_size_gb (torrents only, 0 = disabled)
  5. Quality      – exclude if quality code < min_quality (0 = disabled)
  6. Seeders      – exclude if seeders < min_seeders (torrents only, 0 = disabled)
  7. CAM/TS       – exclude codes 1–6 if exclude_cam is True
"""

import logging

logger = logging.getLogger(__name__)

_FR_CODES:  frozenset[str] = frozenset({"fr", "vff", "vfq", "vf", "french", "truefrench"})
_CAM_CODES: frozenset[int] = frozenset({1, 2, 3, 4, 5, 6})  # CAM, TeleSync, TeleCine, SCR, R5, VHS


def _lang_matches(user_langs: list[str], stream_langs: list[str]) -> bool:
    if not user_langs:
        return True
    lang_set = {lg.lower() for lg in stream_langs}
    for ul in user_langs:
        ul = ul.lower()
        if ul == "fr":
            if lang_set & _FR_CODES or "multi" in lang_set:
                return True
        elif ul == "multi":
            if "multi" in lang_set:
                return True
        elif ul in lang_set:
            return True
    return False


def is_valid(
    stream:      dict,
    user_langs:  list[str],
    season:      int | None = None,
    episode:     int | None = None,
    max_size_gb: int  = 0,
    min_quality: int  = 0,
    min_seeders: int  = 0,
    exclude_cam: bool = False,
) -> bool:
    # ── 1. Language ───────────────────────────────────────────────────────────
    if user_langs and not _lang_matches(user_langs, stream.get("langs") or []):
        return False

    # ── 2. Series episode / season ────────────────────────────────────────────
    if season is not None:
        stream_seasons = stream.get("season") or []
        if stream.get("complete"):
            if stream_seasons and season not in stream_seasons:
                return False
        else:
            if not stream_seasons or season not in stream_seasons:
                return False
            if episode is not None:
                stream_eps = stream.get("episode") or []
                if stream_eps and episode not in stream_eps:
                    return False

    # ── 3. DDL validity ───────────────────────────────────────────────────────
    if stream.get("stream_type") == "ddl":
        valid = stream.get("valid")
        if valid is not None and valid <= 0:
            return False

    # ── 4. Size (torrents only) ───────────────────────────────────────────────
    if max_size_gb and stream.get("stream_type") == "torrent":
        if (stream.get("size") or 0) / (1 << 30) > max_size_gb:
            return False

    # ── 5. Min quality ────────────────────────────────────────────────────────
    if min_quality and (stream.get("quality") or 0) < min_quality:
        return False

    # ── 6. Min seeders (torrents only) ────────────────────────────────────────
    if min_seeders and stream.get("stream_type") == "torrent":
        if (stream.get("seeders") or 0) < min_seeders:
            return False

    # ── 7. Exclude CAM / TS / TC / SCR / R5 / VHS ────────────────────────────
    if exclude_cam and (stream.get("quality") or 0) in _CAM_CODES:
        return False

    return True


def filter_streams(
    streams:     list[dict],
    user_langs:  list[str],
    season:      int | None = None,
    episode:     int | None = None,
    max_size_gb: int  = 0,
    min_quality: int  = 0,
    min_seeders: int  = 0,
    exclude_cam: bool = False,
) -> list[dict]:
    valid   = []
    dropped: dict[str, int] = {}

    for s in streams:
        if is_valid(s, user_langs, season, episode, max_size_gb, min_quality, min_seeders, exclude_cam):
            valid.append(s)
        else:
            r = _reason(s, user_langs, season, episode, max_size_gb, min_quality, min_seeders, exclude_cam)
            dropped[r] = dropped.get(r, 0) + 1

    if dropped:
        logger.info("dropped  %s  →  kept %d/%d",
                    "  ".join(f"{k}×{v}" for k, v in dropped.items()), len(valid), len(streams))
    return valid


def _reason(
    stream:      dict,
    user_langs:  list[str],
    season,
    episode,
    max_size_gb: int  = 0,
    min_quality: int  = 0,
    min_seeders: int  = 0,
    exclude_cam: bool = False,
) -> str:
    if user_langs and not _lang_matches(user_langs, stream.get("langs") or []):
        return "Language"
    if season is not None:
        stream_seasons = stream.get("season") or []
        if stream.get("complete"):
            if stream_seasons and season not in stream_seasons:
                return "Season"
        else:
            if not stream_seasons or season not in stream_seasons:
                return "Season"
            if episode is not None:
                eps = stream.get("episode") or []
                if eps and episode not in eps:
                    return "Episode"
    if stream.get("stream_type") == "ddl":
        v = stream.get("valid")
        if v is not None and v <= 0:
            return "DeadLink"
    if max_size_gb and stream.get("stream_type") == "torrent":
        if (stream.get("size") or 0) / (1 << 30) > max_size_gb:
            return f"Size>{max_size_gb}GB"
    if min_quality and (stream.get("quality") or 0) < min_quality:
        return "Quality"
    if min_seeders and stream.get("stream_type") == "torrent":
        if (stream.get("seeders") or 0) < min_seeders:
            return "Seeders"
    if exclude_cam and (stream.get("quality") or 0) in _CAM_CODES:
        return "CAM"
    return "Unknown"
