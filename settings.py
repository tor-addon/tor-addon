"""
settings.py
───────────
Single source of truth for credentials, URLs and pipeline defaults.
"""

import os
from pathlib import Path

# ── Load .env (no external dependency) ───────────────────────────────────────
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# ── PostgreSQL (DigitalOcean) ─────────────────────────────────────────────────
TOR_PG_DSN = os.environ.get("TOR_PG_DSN", "")
DDL_PG_DSN = os.environ.get("DDL_PG_DSN", "")

# ── AllDebrid ─────────────────────────────────────────────────────────────────
ALLDEBRID_BASE_URL   = "https://api.alldebrid.com/v4"
ALLDEBRID_AGENT      = "Tor"
ALLDEBRID_BATCH_SIZE = 80

# ── TMDB ──────────────────────────────────────────────────────────────────────
TMDB_BASE_URL    = "https://api.themoviedb.org/3"
TMDB_DEFAULT_KEY = (
    "eyJhbGciOiJIUzI1NiJ9"
    ".eyJhdWQiOiJlNTkxMmVmOWFhM2IxNzg2Zjk3ZTE1NWY1YmQ3ZjY1MSIsInN1YiI6IjY1M2NjNWUyZTg5NGE2MDBmZjE2N2FmYyIsInNjb3BlcyI6WyJhcGlfcmVhZCJdLCJ2ZXJzaW9uIjoxfQ"
    ".xrIXsMFJpI1o1j5g2QpQcFP1X3AfRjFA5FlBFO5Naw8"
)

# ── Quality / Resolution label maps ──────────────────────────────────────────
# Numeric codes stored in DB → human-readable labels for display
QUAL_NAMES: dict[int, str] = {
    1:  "CAM",
    2:  "TeleSync",
    3:  "TeleCine",
    4:  "SCR",
    5:  "R5",
    6:  "VHS",
    7:  "VHSRip",
    8:  "PDTV",
    9:  "TVRip",
    10: "SATRip",
    11: "DVD",
    12: "DVDRip",
    13: "HDTV",
    14: "HDTVRip",
    15: "WEBRip",
    16: "WEB-DLRip",
    17: "HDRip",
    18: "BRRip",
    19: "BDRip",
    20: "WEB-DL",
    21: "WEBMux",
    22: "UHDRip",
    23: "BluRay",
    24: "REMUX",
    25: "BluRay REMUX",
}

RES_NAMES: dict[int, str] = {
    1: "480p",
    2: "720p",
    3: "1080p",
    4: "1440p",
    5: "2160p",
}

# ── Darki DDL decode ──────────────────────────────────────────────────────────
# Darki rows in the DDL DB store a numeric code as `link`.
# Decode it to a real 1fichier URL via this API before calling AllDebrid.
DARKI_DECODE_BASE_URL = "https://api.movix.blog/api/darkiworld/decode"
DARKI_DECODE_ORIGIN   = "https://movix.rodeo"

# ── DDL redirector detection ─────────────────────────────────────────────────
# Some stored links point to intermediary services (link protectors, aggregators)
# that AllDebrid cannot unlock directly. They must go through /link/redirector
# first to extract the real file host URL.
#
# Two detection strategies used together:
#   1. ddl_host field matches a known host that always uses intermediaries
#   2. The link's own domain is a known link-protection service
DDL_REDIRECTOR_HOSTS: frozenset[str] = frozenset({
    "wawacity",
})
DDL_REDIRECTOR_DOMAINS: frozenset[str] = frozenset({
    "dl-protect.link",
    "liensdl.fr",
    "www.wawacity.vip",
    "wawacity.vip",
})

# ── Pipeline defaults ─────────────────────────────────────────────────────────
DEFAULT_LANGUAGES    = ["fr"]
STREAM_CACHE_TTL     = 60.0   # seconds — dedup repeated Stremio requests
RESOLVED_CACHE_TTL   = 300.0  # seconds — avoid re-resolving same playback token

# ── Addon metadata ────────────────────────────────────────────────────────────
ADDON_ID          = "community.stremio-tor-addon"
ADDON_NAME        = "Tor"
ADDON_VERSION     = "3.0.0"
ADDON_DESCRIPTION = "Stremio Torrent + DDL │ PostgreSQL │ AllDebrid │ By Adam"
ADDON_LOGO        = "https://images.icon-icons.com/2552/PNG/512/tor_alpha_browser_logo_icon_152957.png"
LOG_LEVEL         = os.environ.get("LOG_LEVEL", "INFO")

# ── Admin page ────────────────────────────────────────────────────────────────
# Définir ADMIN_KEY dans les variables d'environnement Render pour activer /admin
# Ex: ADMIN_KEY=monmotdepasse → accessible sur /admin/monmotdepasse
ADMIN_KEY         = os.environ.get("ADMIN_KEY", "")

# ── Public URL ────────────────────────────────────────────────────────────────
# Forcer l'URL publique pour éviter que le reverse proxy génère du http://
# Ex: BASE_URL=https://tor-sodn5.ondigitalocean.app
BASE_URL          = os.environ.get("BASE_URL", "").rstrip("/")

# ── Proxy AllDebrid ───────────────────────────────────────────────────────────
# Ex: socks5://user:pass@host:port  ou  http://user:pass@host:port
# Laisser vide pour connexion directe
def _parse_proxy(raw: str) -> str:
    """Accepte IP:PORT:USER:PASS ou http://user:pass@ip:port"""
    if not raw:
        return ""
    if raw.startswith(("http://", "https://", "socks5://", "socks4://")):
        return raw
    parts = raw.split(":")
    if len(parts) == 4:
        ip, port, user, password = parts
        return f"http://{user}:{password}@{ip}:{port}"
    return raw

ALLDEBRID_PROXY   = _parse_proxy(os.environ.get("ALLDEBRID_PROXY", ""))
