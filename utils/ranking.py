"""
utils/ranking.py
────────────────
Score streams using the numeric quality/resolution codes stored in the DB.

Score formula (strict priority tiers, no overflow between tiers):
  Resolution  ×100 000   (codes 1–5  → 100k–500k)
  Quality     ×1 000     (remapped rank, see _QUAL_RANK)
  HDR boost   +500       (si hdr_boost=True et stream HDR/DV, jamais > quality gap)
  Size        ×50/GiB    (capped at 300 GiB → max 15k — never overrides quality gap)
  Seeders     capped 50  (tiebreaker)
"""

import logging

logger = logging.getLogger(__name__)

_RES_MULT       = 100_000
_QUAL_MULT      = 1_000
_HDR_BOOST_PTS  = 500    # < quality gap (1000) → ne change jamais le tier
_LANG_BOOST_PTS = 200    # < HDR boost → tiebreaker intra-tier seulement
_SIZE_MULT      = 50
_SIZE_CAP       = 300
_SEED_CAP       = 50

# Codes français présents dans la DB
_FR_CODES: frozenset[str] = frozenset({"fr", "vfq"})

_QUAL_RANK: dict[int, int] = {
    **{i: i for i in range(1, 22)},
    22: 19,   # UHDRip → below WEB-DL (20)
    23: 22,   # BluRay
    24: 23,   # REMUX
    25: 24,   # BluRay REMUX
}


def score(stream: dict, hdr_boost: bool = False) -> int:
    res  = stream.get("resolution") or 0
    qual = stream.get("quality")    or 0

    pts  = res * _RES_MULT
    pts += _QUAL_RANK.get(qual, qual) * _QUAL_MULT
    if hdr_boost and stream.get("hdr"):
        pts += _HDR_BOOST_PTS
    pts += min(stream.get("size", 0) >> 30, _SIZE_CAP) * _SIZE_MULT
    pts += min(stream.get("seeders", 0), _SEED_CAP)

    return pts


def rank_and_sort(streams: list[dict], hdr_boost: bool = False) -> list[dict]:
    """Compute rank in-place, sort descending. Returns same list."""
    for s in streams:
        s["rank"] = score(s, hdr_boost)
    streams.sort(key=lambda s: s["rank"], reverse=True)
    return streams
