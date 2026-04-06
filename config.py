"""
config.py
─────────
UserConfig encoded as base64url-JSON in the Stremio install URL.
Playback token: compact base64url-JSON passed in the playback URL.
"""

import base64
import json
import logging
from dataclasses import dataclass, field

from settings import DEFAULT_LANGUAGES

logger = logging.getLogger(__name__)

FORMAT_EPURE   = "epure"
FORMAT_COMPACT = "compact"


@dataclass
class UserConfig:
    alldebrid_key:  str       = ""
    languages:      list[str] = field(default_factory=lambda: list(DEFAULT_LANGUAGES))
    display_format: str       = FORMAT_EPURE
    # ── Filters ───────────────────────────────────────────────────────────────
    max_size_gb:    int       = 0      # sz: taille max en Go (0 = illimité)
    min_seeders:    int       = 0      # ms: seeders minimum torrents (0 = tout)
    exclude_cam:    bool      = False  # xc: exclure CAM / TS / TC / SCR
    max_results:    int       = 0      # nr: résultats max affichés (0 = illimité)
    # ── Ranking ───────────────────────────────────────────────────────────────
    hdr_boost:      bool      = False  # hb: bonus de classement pour HDR/DV
    # ── Debug ─────────────────────────────────────────────────────────────────
    debug:          bool      = False  # dg: afficher infos techniques (bit_depth, channels)

    def encode(self) -> str:
        payload: dict = {
            "ak": self.alldebrid_key,
            "lg": self.languages,
            "df": self.display_format,
        }
        if self.max_size_gb:  payload["sz"] = self.max_size_gb
        if self.min_seeders:  payload["ms"] = self.min_seeders
        if self.exclude_cam:  payload["xc"] = True
        if self.max_results:  payload["nr"] = self.max_results
        if self.hdr_boost:    payload["hb"] = True
        if self.debug:        payload["dg"] = True
        raw = json.dumps(payload, separators=(",", ":"))
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, b64: str) -> "UserConfig":
        try:
            padding = "=" * (-len(b64) % 4)
            data    = json.loads(base64.urlsafe_b64decode(b64 + padding))
            return cls(
                alldebrid_key  = data.get("ak", ""),
                languages      = data.get("lg", list(DEFAULT_LANGUAGES)),
                display_format = data.get("df", FORMAT_EPURE),
                max_size_gb    = int(data.get("sz", 0)),
                min_seeders    = int(data.get("ms", 0)),
                exclude_cam    = bool(data.get("xc", False)),
                max_results    = int(data.get("nr", 0)),
                hdr_boost      = bool(data.get("hb", False)),
                debug          = bool(data.get("dg", False)),
            )
        except Exception as exc:
            logger.warning("Config decode error: %s – using defaults", exc)
            return cls()

    def is_valid(self) -> bool:
        return bool(self.alldebrid_key)


# ── Playback token ─────────────────────────────────────────────────────────────

def encode_playback_token(
    stream_type: str,
    infohash:    str             = "",
    season:      int | None      = None,
    episode:     int | None      = None,
    year:        int | None      = None,
    source:      str             = "",
    ddl_link:    str             = "",
    ddl_host:    str             = "",
    ddl_db_id:   str             = "",
    ddl_links:   list[str]       = (),
    ddl_hosts:   list[str]       = (),
    ddl_db_ids:  list[str]       = (),
) -> str:
    payload: dict = {"t": stream_type}
    if infohash:            payload["h"]   = infohash
    if season  is not None: payload["s"]   = season
    if episode is not None: payload["e"]   = episode
    if year    is not None: payload["y"]   = year
    if source:              payload["sc"]  = source
    if ddl_links:           payload["dls"] = list(ddl_links)
    if ddl_hosts:           payload["dhs"] = list(ddl_hosts)
    if ddl_db_ids:          payload["dis"] = list(ddl_db_ids)
    if ddl_link and not ddl_links:   payload["dl"] = ddl_link
    if ddl_host and not ddl_hosts:   payload["dh"] = ddl_host
    if ddl_db_id and not ddl_db_ids: payload["di"] = ddl_db_id
    raw = json.dumps(payload, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_playback_token(token: str) -> dict:
    padding = "=" * (-len(token) % 4)
    return json.loads(base64.urlsafe_b64decode(token + padding))
