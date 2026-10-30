"""PromoDJ (promodj.com) client — Featured, Музыка, TOP100, поиск, стримы."""

from __future__ import annotations

import html as html_lib
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote, urljoin, urlparse

import requests

log = logging.getLogger("dlna_emu.promodj")

BASE = "https://promodj.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Основные разделы с музыкой (без Tools/Shop/Video)
MUSIC_TYPES: List[Tuple[str, str, str]] = [
    ("all", "Вся музыка", "/music"),
    ("tracks", "Треки", "/tracks"),
    ("remixes", "Ремиксы", "/remixes"),
    ("mixes", "Миксы", "/mixes"),
    ("promos", "Промо", "/promos"),
    ("lives", "Лайвы", "/lives"),
    ("podcasts", "Подкасты", "/podcasts"),
    ("radioshows", "Радиошоу", "/radioshows"),
]

FEATURED_SECTIONS: List[Tuple[str, str, str]] = [
    ("all", "Featured", "/featured"),
    ("mixes", "Club Mixes", "/featured/mixes"),
    ("tracks_and_remixes", "Tracks & Remixes", "/featured/tracks_and_remixes"),
    ("pop", "Pop", "/featured/pop"),
    ("rap", "Rap", "/featured/rap"),
]

TOP100_SECTIONS: List[Tuple[str, str, str]] = [
    ("tracks", "Треки", "/top100/tracks/1y"),
    ("remixes", "Ремиксы", "/top100/remixes/1y"),
    ("mixes", "Миксы", "/top100/mixes/1y"),
    ("radioshows", "Радиошоу и подкасты", "/top100/radioshows/1y"),
]

# Популярные жанры (полный список очень длинный — берём топ + остальные подтянем с /music)
POPULAR_GENRES: List[Tuple[str, str]] = [
    ("deep_house", "Deep House"),
    ("house", "House"),
    ("tech_house", "Tech House"),
    ("progressive_house", "Progressive House"),
    ("melodic_house", "Melodic House"),
    ("bass_house", "Bass House"),
    ("bassline", "Bassline"),
    ("techno", "Techno"),
    ("minimal", "Minimal"),
    ("dnb", "Drum & Bass"),
    ("liquid_funk", "Liquid Funk"),
    ("neurofunk", "Neurofunk"),
    ("dubstep", "Dubstep"),
    ("trap", "Trap"),
    ("trance", "Trance"),
    ("uplifting_trance", "Uplifting Trance"),
    ("psy_trance", "Psy Trance"),
    ("hardstyle", "Hardstyle"),
    ("hardcore", "Hardcore"),
    ("break_beat", "Breakbeat"),
    ("garage", "Garage"),
    ("uk_garage", "UK Garage"),
    ("afro_house", "Afro House"),
    ("organic_house", "Organic House"),
    ("chillout", "Chillout"),
    ("ambient", "Ambient"),
    ("downtempo", "Downtempo"),
    ("lounge", "Lounge"),
    ("disco", "Disco"),
    ("funk", "Funk"),
    ("hip-hop", "Hip-Hop"),
    ("rap", "Rap"),
    ("pop", "Pop"),
    ("dance_pop", "Dance Pop"),
    ("russian_pop", "Russian Pop"),
    ("edm", "EDM"),
    ("electro", "Electro"),
    ("synthwave", "Synthwave"),
    ("future_bass", "Future Bass"),
    ("jersey_club", "Jersey Club"),
    ("bailefunk", "Baile Funk"),
    ("reggaeton", "Reggaeton"),
    ("dancehall", "Dancehall"),
    ("jungle", "Jungle"),
    ("footwork", "Footwork"),
    ("idm", "IDM"),
    ("experimental", "Experimental"),
]

MUSIC_TYPE_SLUGS = {"tracks", "remixes", "mixes", "promos", "lives", "podcasts", "radioshows"}
NON_GENRE_SLUGS = {"rss.xml", "analytics", "interview", "news", "review", "talk"}
TOP100_SKIP_TYPES = {"djs", "musicians"}
TOP100_TYPE_TITLES = {
    "tracks": "Треки", "remixes": "Ремиксы", "mixes": "Миксы",
    "radioshows": "Радиошоу и подкасты",
}

_RE_SELECTOR = re.compile(
    r'<a\b[^>]*href="(?:https?://promodj\.com)?(/[^"]*)"[^>]*class="selector[^"]*"[^>]*>([^<]*)</a>', re.I)
_RE_TOP_LINK = re.compile(
    r'<a\b[^>]*href="(?:https?://promodj\.com)?/top100/([a-z0-9_]+)/([0-9]+[a-z])"[^>]*>(.*?)</a>', re.I | re.S)

_RE_FILE = re.compile(r'amba="file:(\d+)"', re.I)
_RE_TITLE = re.compile(
    r'amba="file:(\d+)"[^>]*href="([^"]+)"[^>]*>([^<]+)', re.I
)
_RE_TITLE2 = re.compile(
    r'<a[^>]*amba="file:(\d+)"[^>]*href="([^"]+)"[^>]*class="[^"]*invert[^"]*"[^>]*>([^<]+)',
    re.I,
)
_RE_DOWNLOAD = re.compile(
    r'href="(https://promodj\.com/download/(\d+)/[^"]+)"', re.I
)
_RE_PAGE = re.compile(r'[?&]page=(\d+)', re.I)
_RE_NAV_CURRENT = re.compile(
    r'class="NavigatorCurrentPage"[^>]*page="(\d+)"', re.I
)
_RE_GENRE_LINK = re.compile(
    r'href="(?:https://promodj\.com)?/music/([a-z0-9_\-]+)"[^>]*>([^<]+)</a>',
    re.I,
)
_RE_STYLES = re.compile(
    r'class="player_standard_tool player_standard_tool__styles"[^>]*>([^<]+)',
    re.I,
)


@dataclass
class PdUser:
    id: str
    slug: str
    name: str
    styles: str = ""
    country: str = ""


@dataclass
class PdTrack:
    id: str
    title: str
    url: str = ""  # page url
    download_url: str = ""
    artist: str = ""
    styles: str = ""
    duration: int = 0


class PromoDjClient:
    def __init__(
        self,
        enabled: bool = True,
        cache_sec: int = 300,
        tracks_per_chunk: int = 500,
        stream_mode: str = "auto",
        buffer_timeout: float = 6.0,
        buffer_limit_mb: float = 64.0,
    ):
        self.enabled = bool(enabled)
        self.cache_sec = max(60, int(cache_sec or 300))
        # Сколько треков в одной DLNA-«Странице» (как akniga section_books_per_chunk)
        self.tracks_per_chunk = max(50, min(1000, int(tracks_per_chunk or 500)))
        self.stream_mode = (stream_mode or "auto").lower().strip()
        self.buffer_timeout = float(buffer_timeout or 6.0)
        self.buffer_limit_mb = float(buffer_limit_mb or 64.0)
        self.buffer_limit_bytes = int(max(1.0, self.buffer_limit_mb) * 1024 * 1024)
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            }
        )
        self._lock = threading.RLock()
        self._cache: Dict[str, Tuple[float, object]] = {}
        self._genres_cache: Optional[List[Tuple[str, str]]] = None
        # path -> (total_site_pages, per_page)
        self._layout_cache: Dict[str, Tuple[int, int]] = {}

    # ---------- cache ----------

    def _get_cache(self, key: str):
        with self._lock:
            item = self._cache.get(key)
            if not item:
                return None
            ts, val = item
            if time.time() - ts > self.cache_sec:
                del self._cache[key]
                return None
            return val

    def _set_cache(self, key: str, val) -> None:
        with self._lock:
            self._cache[key] = (time.time(), val)

    # ---------- HTTP ----------


    def _get(self, path: str, params: Optional[dict] = None, ajax: bool = False) -> str:
        if path.startswith("http"):
            url = path
        else:
            url = urljoin(BASE + "/", path.lstrip("/"))
        headers = {}
        if ajax:
            headers["X-Requested-With"] = "XMLHttpRequest"
            if "?" in url:
                url += "&ajax=1"
            else:
                url += "?ajax=1"
        try:
            r = self._session.get(url, params=params, headers=headers, timeout=25)
            if r.status_code == 404:
                return ""
            if r.status_code >= 400:
                log.warning("PromoDJ GET %s: HTTP %s", url, r.status_code)
                return ""
            r.encoding = r.apparent_encoding or "utf-8"
            return r.text
        except Exception as e:
            log.warning("PromoDJ GET %s: %s", url, e)
            return ""

    # ---------- parse ----------

    def _parse_tracks(self, html: str) -> List[PdTrack]:
        if not html:
            return []
        # Только основной список. Sticky-карточки в сайдбаре/плеере
        # иначе повторяются на каждой странице и портят подсчёт.
        blocks = re.findall(
            r'class="tracks_dump[^"]*"[\s\S]*?(?=<div class="tracks_dump|class="Navigator|</body>|$)',
            html,
            re.I,
        )
        scope = "\n".join(blocks) if blocks else html

        tracks: List[PdTrack] = []
        seen = set()
        for rx in (_RE_TITLE2, _RE_TITLE):
            for m in rx.finditer(scope):
                tid = m.group(1)
                if tid in seen:
                    continue
                seen.add(tid)
                href = m.group(2)
                title = html_lib.unescape(re.sub(r"\s+", " ", (m.group(3) or "").strip()))
                if not title:
                    title = f"Track {tid}"
                page_url = href if href.startswith("http") else urljoin(BASE, href)
                tracks.append(PdTrack(
                    id=tid, title=title, url=page_url,
                    download_url=f"{BASE}/download/{tid}/track.mp3",
                ))
        if not tracks:
            for m in _RE_FILE.finditer(scope):
                tid = m.group(1)
                if tid in seen:
                    continue
                seen.add(tid)
                tracks.append(PdTrack(
                    id=tid, title=f"Track {tid}",
                    download_url=f"{BASE}/download/{tid}/track.mp3",
                ))
        dl_map = {m.group(2): m.group(1) for m in _RE_DOWNLOAD.finditer(html)}
        for t in tracks:
            if t.id in dl_map:
                t.download_url = dl_map[t.id]
            if " - " in t.title:
                parts = t.title.split(" - ", 1)
                if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                    t.artist = parts[0].strip()
        return tracks

    def _detect_total_pages(self, html: str) -> int:
        pages = [int(p) for p in _RE_PAGE.findall(html or "")]
        cur = _RE_NAV_CURRENT.search(html or "")
        if cur:
            pages.append(int(cur.group(1)))
        if not pages:
            if _RE_FILE.search(html or ""):
                return 1
            return 0
        return max(pages)

    # ---------- public lists ----------

    # ---------- каталоги (динамически с сайта) ----------
    #
    # Типы музыки, Featured, TOP 100 и жанры читаются со страниц сайта
    # (кэш 1 час), встроенные списки — запасной вариант, если сайт не ответил.

    CATALOG_TTL = 3600

    def _cat_get(self, key: str):
        with self._lock:
            item = self._cache.get("cat:" + key)
            if item and time.time() - item[0] < self.CATALOG_TTL:
                return item[1]
        return None

    def _cat_set(self, key: str, val) -> None:
        with self._lock:
            self._cache["cat:" + key] = (time.time(), val)

    @staticmethod
    def _txt(t: str) -> str:
        import html as _h
        return re.sub(r"\s+", " ", _h.unescape(re.sub(r"<[^>]+>", "", t or ""))).strip()

    def _selectors(self, body: str) -> List[Tuple[str, str]]:
        """Переключатели страницы (class="selector"): [(путь, название)]."""
        out: List[Tuple[str, str]] = []
        for m in _RE_SELECTOR.finditer(body or ""):
            path = m.group(1).split("?")[0].rstrip("/") or "/"
            title = self._txt(m.group(2))
            if title and not any(p == path for p, _ in out):
                out.append((path, title))
        return out

    def list_music_types(self) -> List[Tuple[str, str, str]]:
        """Разделы музыки (Вся музыка, Треки, Ремиксы, …) — с навигации /music."""
        hit = self._cat_get("types")
        if hit:
            return list(hit)
        out: List[Tuple[str, str, str]] = []
        body = self._get("/music")
        for path, title in self._selectors(body):
            name = path.strip("/")
            if name == "music":
                out.append(("all", title, path))
            elif name in MUSIC_TYPE_SLUGS:
                out.append((name, title, path))
        if len(out) >= 2:
            # типы, которых не оказалось на странице, но есть в запасном списке, не добавляем
            self._cat_set("types", out)
            return list(out)
        return list(MUSIC_TYPES)

    def list_featured_sections(self) -> List[Tuple[str, str, str]]:
        hit = self._cat_get("featured")
        if hit:
            return list(hit)
        out: List[Tuple[str, str, str]] = []
        body = self._get("/featured")
        for path, title in self._selectors(body):
            if not (path == "/featured" or path.startswith("/featured/")):
                continue
            slug = "all" if path == "/featured" else path.rsplit("/", 1)[-1]
            if slug != "all" and title.lower().startswith("featured "):
                title = title[9:].strip()
            out.append((slug, title, path))
        if len(out) >= 1:
            self._cat_set("featured", out)
            return list(out)
        return list(FEATURED_SECTIONS)

    # --- TOP 100: тип → период → стиль ---

    def top100_info(self, type_slug: str, period: str = "1y") -> dict:
        """{'types': [(slug, title)], 'periods': [(period, title)], 'styles': [(slug, title)]}."""
        type_slug = (type_slug or "tracks").strip("/") or "tracks"
        period = (period or "1y").strip("/") or "1y"
        key = f"top:{type_slug}:{period}"
        hit = self._cat_get(key)
        if hit:
            return hit
        body = self._get(f"/top100/{type_slug}/{period}")
        info = {"types": [], "periods": [], "styles": []}
        if body:
            pm = re.search(r'top100_periods">(.*?)</h1>', body, re.S)
            rest = body
            if pm:
                for m in _RE_TOP_LINK.finditer(pm.group(1)):
                    info["periods"].append((m.group(2), self._txt(m.group(3))))
                rest = body[pm.end():]
                tm = re.search(r"<h1>(.*?)</h1>", rest, re.S)
                if tm:
                    for m in _RE_TOP_LINK.finditer(tm.group(1)):
                        t = m.group(1)
                        if t not in TOP100_SKIP_TYPES and not any(x == t for x, _ in info["types"]):
                            info["types"].append((t, self._txt(m.group(3))))
            cm = re.search(r'styles_tagcloud">(.*?)</div>', rest, re.S)
            if cm:
                for m in re.finditer(
                    r'href="(?:https?://promodj\.com)?/top100/[a-z0-9_]+/([a-z0-9_\-]+)/[0-9]+[a-z]"[^>]*>([^<]+)</a>',
                    cm.group(1), re.I,
                ):
                    slug, title = m.group(1), self._txt(m.group(2))
                    if title and not any(x == slug for x, _ in info["styles"]):
                        info["styles"].append((slug, title))
            self._cat_set(key, info)
        return info

    def list_top100_types(self) -> List[Tuple[str, str]]:
        """Типы TOP 100 (треки, ремиксы, миксы, подкасты). DJ/Музыканты — списки людей, пропускаем."""
        hit = self._cat_get("top_types")
        if hit:
            return list(hit)
        info = self.top100_info("tracks", "1y")
        found = {s: t for s, t in info["types"]}
        found.setdefault("tracks", TOP100_TYPE_TITLES["tracks"])
        out: List[Tuple[str, str]] = []
        for slug in sorted(found, key=lambda x: (list(TOP100_TYPE_TITLES).index(x)
                                                  if x in TOP100_TYPE_TITLES else 99, x)):
            out.append((slug, TOP100_TYPE_TITLES.get(slug) or found[slug]))
        if info["types"]:
            self._cat_set("top_types", out)
            return out
        return [(s, TOP100_TYPE_TITLES[s]) for s in TOP100_TYPE_TITLES]

    def list_top100_periods(self, type_slug: str) -> List[Tuple[str, str]]:
        info = self.top100_info(type_slug, "1y")
        per = list(info["periods"])
        if not per:
            per = [("1y", "Год")] + ([("1m", "Месяц")] if type_slug != "radioshows" else [])
        return per

    def list_top100_styles(self, type_slug: str, period: str = "1y") -> List[Tuple[str, str]]:
        return list(self.top100_info(type_slug, period)["styles"])

    def list_top100_sections(self) -> List[Tuple[str, str, str]]:
        return [(s, t, f"/top100/{s}/1y") for s, t in self.list_top100_types()]

    def list_genres(self, section: str = "music") -> List[Tuple[str, str]]:
        """Жанры раздела (music / tracks / remixes / mixes …): [(slug, название)] в порядке сайта."""
        section = (section or "music").strip("/") or "music"
        hit = self._cat_get(f"genres:{section}")
        if hit:
            return list(hit)
        body = self._get(f"/{section}")
        found: List[Tuple[str, str]] = []
        seen = set()
        rx = re.compile(
            r'href="(?:https://promodj\.com)?/' + re.escape(section) + r'/([a-z0-9_\-]+)"[^>]*>([^<]+)</a>',
            re.I,
        )
        for m in rx.finditer(body or ""):
            slug = m.group(1).lower().strip()
            title = self._txt(m.group(2))
            if (not slug or slug in seen or slug in NON_GENRE_SLUGS or re.match(r"^page\d+$", slug)
                    or not title or len(title) > 60):
                continue
            seen.add(slug)
            found.append((slug, title))
        if found:
            self._cat_set(f"genres:{section}", found)
            return found
        return list(POPULAR_GENRES)

    # ---------- tracks fetch ----------

    MAX_SITE_PAGES = 120

    def get_tracks(self, path: str, page: int = 1) -> Tuple[List[PdTrack], int]:
        """Одна страница сайта. HTML-пагинатору (1..6) доверять нельзя."""
        page = max(1, int(page or 1))
        cache_key = f"tracks:{path}:p{page}"
        cached = self._get_cache(cache_key)
        if cached is not None:
            return cached  # type: ignore
        params = {"page": page} if page > 1 else None
        html = self._get(path, params=params, ajax=False)
        tracks = self._parse_tracks(html)
        hint = self._detect_total_pages(html) if page == 1 else 0
        result = (tracks, hint)
        self._set_cache(cache_key, result)
        return result

    def _discover_total_pages(self, path: str) -> Tuple[int, int]:
        """Реальное число страниц: бинарный поиск до пустой/404.

        Если page=2 отдаёт те же id, что и page=1 (карточка артиста /music
        без пагинации, или редирект на профиль), считаем одну страницу.
        """
        cached = self._get_cache(f"discovered:{path}")
        if cached is not None:
            return cached  # type: ignore
        page1, _ = self.get_tracks(path, page=1)
        if not page1:
            result = (0, 30)
            self._set_cache(f"discovered:{path}", result)
            return result
        per_page = len(page1) or 30
        ids1 = {t.id for t in page1}
        page2, _ = self.get_tracks(path, page=2)
        if not page2:
            result = (1, per_page)
            self._set_cache(f"discovered:{path}", result)
            return result
        ids2 = {t.id for t in page2}
        # Нет реальной пагинации: почти все id совпадают (сайдбар/«сейчас
        # слушают» может чуть отличаться). Типично для /{slug}/music.
        new_ids = ids2 - ids1
        overlap = len(ids1 & ids2)
        if overlap >= max(1, int(0.85 * min(len(ids1), len(ids2)))) and len(new_ids) < max(10, per_page // 5):
            result = (1, per_page)
            self._set_cache(f"discovered:{path}", result)
            log.info(
                "PromoDJ %s: single page (~%d tracks, no pagination; overlap=%d new=%d)",
                path, per_page, overlap, len(new_ids),
            )
            return result
        lo, hi = 2, self.MAX_SITE_PAGES
        last_ok = 2
        while lo <= hi:
            mid = (lo + hi) // 2
            tracks, _ = self.get_tracks(path, page=mid)
            if tracks:
                last_ok = mid
                lo = mid + 1
            else:
                hi = mid - 1
        result = (last_ok, per_page)
        self._set_cache(f"discovered:{path}", result)
        log.info("PromoDJ %s: real pages=%d, ~%d tracks/page", path, last_ok, per_page)
        return result

    def _load_all_unique(self, path: str) -> List[PdTrack]:
        """Все уникальные треки раздела по всем страницам сайта."""
        cache_key = f"allunique:{path}"
        cached = self._get_cache(cache_key)
        if cached is not None:
            return cached  # type: ignore
        total_pages, _per = self._discover_total_pages(path)
        if total_pages <= 0:
            self._set_cache(cache_key, [])
            return []
        all_tracks: List[PdTrack] = []
        seen: set = set()
        for p in range(1, total_pages + 1):
            page_tracks, _ = self.get_tracks(path, page=p)
            if not page_tracks:
                break
            added = 0
            for t in page_tracks:
                if t.id in seen:
                    continue
                seen.add(t.id)
                all_tracks.append(t)
                added += 1
            # Дальше страницы только дублируют уже собранное
            if added == 0:
                break
        self._set_cache(cache_key, all_tracks)
        log.info("PromoDJ %s: %d unique tracks from %d pages", path, len(all_tracks), total_pages)
        return all_tracks

    def section_layout(self, path: str) -> Tuple[int, int, int]:
        """(total_site_pages, per_page, num_dlna_chunks). Чанк = tracks_per_chunk."""
        cached = self._get_cache(f"layout:{path}")
        if cached is not None:
            return cached  # type: ignore
        total_pages, per_page = self._discover_total_pages(path)
        if total_pages <= 0:
            result = (0, per_page or 30, 0)
            self._set_cache(f"layout:{path}", result)
            return result
        all_tracks = self._load_all_unique(path)
        n = len(all_tracks)
        if n <= 0:
            chunks = 0
        elif n <= self.tracks_per_chunk:
            chunks = 1
        else:
            chunks = (n + self.tracks_per_chunk - 1) // self.tracks_per_chunk
        result = (total_pages, per_page, chunks)
        self._set_cache(f"layout:{path}", result)
        self._layout_cache[path] = (total_pages, per_page)
        log.info("PromoDJ layout %s: pages=%d unique=%d chunks=%d", path, total_pages, n, chunks)
        return result

    def get_chunk(self, path: str, chunk_index: int) -> List[PdTrack]:
        """Срез уникальных треков для DLNA-страницы (0-based)."""
        chunk_index = max(0, int(chunk_index or 0))
        all_tracks = self._load_all_unique(path)
        start = chunk_index * self.tracks_per_chunk
        if start >= len(all_tracks):
            return []
        return all_tracks[start : start + self.tracks_per_chunk]


    def _search_json(self, query: str, mode: str = "audio", page: int = 1) -> dict:
        """Официальный JSON API: GET /search?searchfor=&mode=&page=

        mode: audio | user | all | video | ...
        """
        q = (query or "").strip()
        if not q:
            return {"results": [], "total": 0, "perpage": 10}
        page = max(1, int(page or 1))
        cache_key = f"sjson:{mode}:{q}:p{page}"
        cached = self._get_cache(cache_key)
        if cached is not None:
            return cached  # type: ignore
        try:
            r = self._session.get(
                BASE + "/search",
                params={
                    "searchfor": q,
                    "mode": mode,
                    "sortby": "relevance",
                    "period": "all",
                    "page": page,
                    "results": 1,
                },
                headers={
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": f"{BASE}/search?searchfor={quote(q)}&mode={mode}",
                },
                timeout=25,
            )
            if r.status_code >= 400:
                log.warning("PromoDJ search JSON HTTP %s", r.status_code)
                return {"results": [], "total": 0, "perpage": 10}
            data = r.json()
            if not isinstance(data, dict):
                data = {"results": [], "total": 0, "perpage": 10}
            self._set_cache(cache_key, data)
            return data
        except Exception as e:
            log.warning("PromoDJ search JSON: %s", e)
            return {"results": [], "total": 0, "perpage": 10}

    def search_users(self, query: str, page: int = 1) -> Tuple[List[PdUser], int]:
        """Поиск людей (DJ / артистов), mode=user."""
        data = self._search_json(query, mode="user", page=page)
        users: List[PdUser] = []
        seen = set()
        for item in data.get("results") or []:
            if not isinstance(item, dict):
                continue
            if item.get("kind") != "user":
                continue
            html = item.get("html") or ""
            uid = str(item.get("kindID") or "")
            # slug + name из карточки
            m = re.search(
                r'href="https://promodj\.com/([a-zA-Z0-9._\-]+)"[^>]*class="[^"]*user[^"]*"[^>]*>(.*?)</a>',
                html, re.I | re.S,
            )
            if not m:
                m = re.search(
                    r'class="avatar"[^>]*href="https://promodj\.com/([a-zA-Z0-9._\-]+)"',
                    html, re.I,
                )
                slug = m.group(1) if m else ""
                mname = re.search(r'class="user[^"]*"[^>]*>(.*?)</a>', html, re.I | re.S)
                name = re.sub(r"<[^>]+>", "", mname.group(1)).strip() if mname else slug
            else:
                slug = m.group(1)
                name = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            if not slug or slug in seen:
                continue
            seen.add(slug)
            styles_m = re.search(r'class="small"[^>]*>([^<]+)', html)
            country_m = re.search(r'class="desc"[^>]*>([^<]+)', html)
            users.append(PdUser(
                id=uid or slug,
                slug=slug,
                name=name or slug,
                styles=(styles_m.group(1).strip() if styles_m else ""),
                country=re.sub(r"&nbsp;", " ", country_m.group(1)).strip() if country_m else "",
            ))
        total = int(data.get("total") or 0)
        return users, total

    def search(self, query: str, page: int = 1) -> Tuple[List[PdTrack], int]:
        """Поиск музыки (mode=audio) через JSON API. perpage обычно 10."""
        data = self._search_json(query, mode="audio", page=page)
        tracks: List[PdTrack] = []
        seen = set()
        for item in data.get("results") or []:
            if not isinstance(item, dict):
                continue
            if item.get("kind") not in ("file", "audio", None):
                # file = трек/микс
                if item.get("kind") != "file":
                    continue
            html = item.get("html") or ""
            tid = str(item.get("kindID") or "")
            parsed = self._parse_tracks(html)
            if parsed:
                for t in parsed:
                    if t.id not in seen:
                        seen.add(t.id)
                        tracks.append(t)
            elif tid and tid not in seen:
                seen.add(tid)
                # title from html
                tm = re.search(r'amba="file:\d+"[^>]*>([^<]+)', html)
                title = (tm.group(1).strip() if tm else f"Track {tid}")
                title = html_lib.unescape(re.sub(r"<[^>]+>", "", title))
                tracks.append(PdTrack(
                    id=tid, title=title,
                    download_url=f"{BASE}/download/{tid}/track.mp3",
                ))
        total = int(data.get("total") or 0)
        perpage = int(data.get("perpage") or 10) or 10
        # возвращаем total_pages ≈ ceil(total/perpage)
        total_pages = max(1, (total + perpage - 1) // perpage) if total else (1 if tracks else 0)
        return tracks, total_pages

    def _discover_search_pages(self, query: str) -> Tuple[int, int]:
        tracks, total_pages = self.search(query, page=1)
        per_page = len(tracks) or 10
        return total_pages, per_page

    def _load_all_search(self, query: str) -> List[PdTrack]:
        q = (query or "").strip()
        if not q:
            return []
        cache_key = f"sall:{q}"
        cached = self._get_cache(cache_key)
        if cached is not None:
            return cached  # type: ignore
        total_pages, _per = self._discover_search_pages(q)
        # API часто отдаёт total=500 как потолок
        total_pages = min(total_pages, self.MAX_SITE_PAGES)
        all_tracks: List[PdTrack] = []
        seen: set = set()
        for p in range(1, max(1, total_pages) + 1):
            page_tracks, _ = self.search(q, page=p)
            if not page_tracks:
                break
            for t in page_tracks:
                if t.id in seen:
                    continue
                seen.add(t.id)
                all_tracks.append(t)
        self._set_cache(cache_key, all_tracks)
        log.info("PromoDJ search %r: %d unique tracks", q, len(all_tracks))
        return all_tracks

    def search_layout(self, query: str) -> Tuple[int, int, int]:
        q = (query or "").strip()
        if not q:
            return 0, 10, 0
        total_pages, per_page = self._discover_search_pages(q)
        n = len(self._load_all_search(q))
        if n <= 0:
            chunks = 0
        elif n <= self.tracks_per_chunk:
            chunks = 1
        else:
            chunks = (n + self.tracks_per_chunk - 1) // self.tracks_per_chunk
        return total_pages, per_page, chunks

    def search_chunk(self, query: str, chunk_index: int) -> List[PdTrack]:
        q = (query or "").strip()
        if not q:
            return []
        chunk_index = max(0, int(chunk_index or 0))
        all_tracks = self._load_all_search(q)
        start = chunk_index * self.tracks_per_chunk
        if start >= len(all_tracks):
            return []
        return all_tracks[start : start + self.tracks_per_chunk]

    def get_user_tracks(self, slug: str) -> List[PdTrack]:
        """Все треки/миксы артиста с карточки: /{slug}/music.

        Раздел «Музыка» в профиле DJ — единый список (tracks + remixes +
        mixes + …). Путь /{slug}/tracks редиректит на главную профиля и
        не подходит для пагинации.
        """
        slug = (slug or "").strip().strip("/")
        if not slug:
            return []
        return self._load_all_unique(f"/{slug}/music")

    def resolve_download_url(self, track_id: str) -> str:
        """Прямая ссылка на mp3 (через 302 CDN)."""
        tid = str(track_id).strip()
        if not tid:
            return ""
        return f"{BASE}/download/{tid}/track.mp3"

    def open_stream(self, track_id: str):
        """Открыть поток для сервера. Возвращает (iterator_or_resp, mime, content_length|None, is_resp)."""
        url = self.resolve_download_url(track_id)
        if not url:
            return None
        try:
            r = self._session.get(
                url,
                stream=True,
                timeout=30,
                allow_redirects=True,
                headers={"User-Agent": UA},
            )
            if r.status_code >= 400:
                log.warning("PromoDJ stream %s: HTTP %s", track_id, r.status_code)
                r.close()
                return None
            mime = r.headers.get("Content-Type") or "audio/mpeg"
            if "html" in mime.lower():
                r.close()
                return None
            cl = r.headers.get("Content-Length")
            content_length = int(cl) if cl and cl.isdigit() else None
            return r, mime, content_length, True
        except Exception as e:
            log.warning("PromoDJ open_stream %s: %s", track_id, e)
            return None
