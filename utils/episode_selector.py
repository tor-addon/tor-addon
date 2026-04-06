"""
utils/episode_selector.py
─────────────────────────
Finds the best matching video file inside a flattened AllDebrid torrent file tree.

Selection strategies (tried in order):
  1. SINGLE  – only one video file → return directly.
  2. EPISODE – match by season + episode number.
  3. YEAR    – for movie packs: match by release year.
  4. LARGEST – fallback to the biggest video file.
"""

import logging
import re

from PTT import parse_title

logger = logging.getLogger(__name__)

_VIDEO_EXT: frozenset[str] = frozenset({
    ".mkv", ".mp4", ".avi", ".mov", ".ts", ".m2ts",
    ".wmv", ".flv", ".webm", ".mpg", ".mpeg", ".m4v",
})


def find_best_file(
    files:   list[dict],
    season:  int | None = None,
    episode: int | None = None,
    year:    int | None = None,
) -> dict | None:
    videos = [
        f for f in files
        if (dot := f.get("n", "").rfind(".")) != -1
        and f["n"][dot:].lower() in _VIDEO_EXT
        and "sample" not in f.get("n", "").lower()
    ]
    if not videos:
        logger.warning("EpisodeSelector: no video files in torrent")
        return None

    if len(videos) == 1:
        return videos[0]

    parsed = [(f, parse_title(f["n"])) for f in videos]

    if season is not None and episode is not None:
        match = _match_episode(parsed, season, episode)
        if match:
            logger.info("EpisodeSelector: S%02dE%02d → %s", season, episode, match["n"])
            return match

    if year is not None:
        for f, p in parsed:
            if p.get("year") == year:
                logger.info("EpisodeSelector: year=%d → %s", year, f["n"])
                return f

    best = max(videos, key=lambda f: f.get("s", 0))
    logger.warning("EpisodeSelector: fallback to largest → %s", best["n"])
    return best


def _match_episode(parsed: list[tuple], target_s: int, target_e: int) -> dict | None:
    season_candidates = []
    for f, p in parsed:
        file_s = p.get("seasons")  or []
        file_e = p.get("episodes") or []
        if target_s in file_s and target_e in file_e:
            return f
        if target_s in file_s and not file_e:
            season_candidates.append(f)

    if season_candidates:
        season_candidates.sort(key=lambda f: [
            int(x) if x.isdigit() else x.lower()
            for x in re.split(r"(\d+)", f.get("n", ""))
        ])
        idx = target_e - 1
        if 0 <= idx < len(season_candidates):
            return season_candidates[idx]
    return None
