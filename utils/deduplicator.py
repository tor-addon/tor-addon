"""
utils/deduplicator.py
─────────────────────
Remove duplicate streams:
  - Same infohash  → exact same torrent
  - Same size      → same content encoded differently (user spec)

Call is_new() on each stream in order (best quality first after sorting).
First occurrence is kept; subsequent duplicates are dropped.
"""


class Deduplicator:
    __slots__ = ("_hashes", "_sizes", "_links")

    def __init__(self) -> None:
        self._hashes: set[str] = set()
        self._sizes:  set[int] = set()
        self._links:  set[str] = set()

    def is_new(self, stream: dict) -> bool:
        if stream.get("stream_type") == "ddl":
            # DDL: dedup only on exact link URL.
            # Ne pas dédupliquer par taille — même taille = même contenu
            # sur des hôtes différents → ils doivent être mergés ensuite.
            link = (stream.get("ddl_link") or "").strip()
            if link and link in self._links:
                return False
            if link:
                self._links.add(link)
            return True

        # Torrents: dedup par infohash et par taille
        h    = (stream.get("infohash") or "").strip().lower()
        size = stream.get("size") or 0

        if h and h in self._hashes:
            return False
        if size > 0 and size in self._sizes:
            return False

        if h:
            self._hashes.add(h)
        if size > 0:
            self._sizes.add(size)
        return True
