"""
utils/admin_stats.py
────────────────────
Compteurs in-memory ultra-légers pour la page /admin.
Aucun verrou — incréments entiers atomiques sous le GIL CPython.
"""
import time
from collections import Counter


class _Stats:
    __slots__ = (
        "start_time", "_installs",
        "searches", "playbacks", "playbacks_failed",
        "_top", "_titles", "_sources",
        "_lat_total", "_lat_count",
    )

    def __init__(self) -> None:
        self.start_time              = time.time()
        self._installs: set[str]    = set()
        self.searches:  int         = 0
        self.playbacks: int         = 0
        self.playbacks_failed: int  = 0
        self._top:     Counter      = Counter()
        self._titles:  dict[str,str]= {}
        self._sources: Counter      = Counter()
        self._lat_total: float      = 0.0
        self._lat_count: int        = 0

    def record_install(self, b64: str) -> None:
        self._installs.add(b64)

    def record_search(self, imdb_id: str = "", title: str = "",
                      latency_ms: float = 0.0) -> None:
        self.searches += 1
        if imdb_id:
            self._top[imdb_id] += 1
            if title and imdb_id not in self._titles:
                self._titles[imdb_id] = title
        if latency_ms > 0:
            self._lat_total += latency_ms
            self._lat_count += 1

    def record_playback(self, success: bool = True, source: str = "") -> None:
        if success:
            self.playbacks += 1
            if source:
                self._sources[source] += 1
        else:
            self.playbacks_failed += 1

    @property
    def installs(self) -> int:
        return len(self._installs)

    @property
    def uptime_str(self) -> str:
        s = int(time.time() - self.start_time)
        h, r = divmod(s, 3600)
        m, s = divmod(r, 60)
        return f"{h}h {m:02d}m {s:02d}s"

    @property
    def avg_latency_ms(self) -> int:
        return round(self._lat_total / self._lat_count) if self._lat_count else 0

    def top_searches(self, n: int = 8) -> list[dict]:
        return [
            {"id": k, "count": v, "title": self._titles.get(k, "")}
            for k, v in self._top.most_common(n)
        ]

    def top_sources(self, n: int = 5) -> list[dict]:
        return [{"source": k, "count": v} for k, v in self._sources.most_common(n)]


stats = _Stats()
