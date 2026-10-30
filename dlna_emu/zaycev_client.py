"""Zaycev.net — каталог, поиск и стрим без авторизации.

Сайт — Next.js-приложение, которое само ходит в собственный JSON-API
``https://zaycev.net/api/external/…``. Эмулятор использует тот же API
(так же динамически, как каталоги Яндекс.Музыки): разделы, жанры, подборки
подтягиваются с сайта по требованию, статические списки — лишь запасной вариант.

Используемые эндпоинты (взяты из клиентского JS сайта):

  pages/new                     новинки              (page, limit)
  pages/index/top               топ                  (page, limit, period=day|week|month, entity=track)
  pages/genres                  список жанров
  pages/genres/{slug}/subgenres поджанры
  pages/genres/{slug}/tracks    треки жанра          (page, limit, sort=popularity|new)
  pages/musicset/categories/readAll   категории подборок
  pages/musicset, pages/musicset/list подборки категории (type, limit, page, query)
  pages/musicset/tracks         треки подборки       (url)
  pages/artist/{id}/tracks      треки артиста        (page, limit, sort)
  pages/search/tracks|artists   поиск                (q, page, limit)
  track/play/{streaming}        → {url, duration}    ссылка на mp3
  track/tracks/info, pages/track  данные трека по id

Формат ответов списков: ``{trackIds, tracksInfo, page, pagesCount}`` либо
``{list, info, page, pagesCount}``. Разбор сделан «по признакам» (см.
``_extract_tracks``), поэтому небольшие изменения обёрток API его не ломают.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests

log = logging.getLogger("dlna_emu.zaycev")

BASE = "https://zaycev.net"
API_PATH = "/api/external"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) "
    "Gecko/20100101 Firefox/128.0"
)

# Размер страницы API (на сайте новинки/топ тоже грузятся по 50)
PAGE_LIMIT = 50
# Сколько страниц сайта максимум забираем под один список (защита от гигантов)
MAX_SITE_PAGES = 400
# Лимит запоминаемых треков (id → streaming-hash), RAM роутера не резиновая
MAX_TRACK_MEMORY = 6000

TOP_PERIODS: List[Tuple[str, str]] = [
    ("day", "За день"),
    ("week", "За неделю"),
    ("month", "За месяц"),
]

# Запасной список жанров (с главной сайта) — если pages/genres недоступен
STATIC_GENRES: List[Tuple[str, str]] = [
    ("electronic", "Электроника"), ("rap", "Рэп"), ("pop", "Поп"),
    ("rock", "Рок"), ("shanson", "Шансон"), ("metal", "Метал"),
    ("easy", "Легкая"), ("classical", "Классика"),
    ("world", "Музыка народов мира"), ("dance", "Танцевальная"),
    ("jazz", "Джаз"), ("folk", "Фолк"), ("rnb", "R’n’B"),
    ("indie", "Инди-музыка"), ("instrumental", "Инструментальная"),
    ("reggae", "Регги"), ("blues", "Блюз"), ("children", "Детская"),
    ("rbt", "Рингтоны"), ("soundtrack", "Саундтреки"), ("other", "Другое"),
]

# Запасные поджанры (со страницы /genres/index.html) — slug вида «родитель/поджанр»
STATIC_SUBGENRES: Dict[str, List[Tuple[str, str]]] = {
    "other": [
        ("other/playing", "Для занятий"), ("other/meditatio", "Медитация"),
        ("other/audiobook", "Аудиокнига"),
        ("other/christian", "Современная христианская музыка"),
        ("other/gospel", "Госпел"), ("other/prayer", "Молитва"),
        ("other/meme", "Прикол"), ("other/ballad", "Баллады"),
        ("other/game", "Музыка из игр"), ("other/holiday", "Праздничная музыка"),
        ("other/christmas", "Рождественские"), ("other/fitness", "Фитнес"),
    ],
}

# Категория подборок «Сборники по жанрам» (slug на сайте: /musicset/zhanry/)
GENRE_SETS_CATEGORY = "zhanry"

# Запасной список категорий подборок (typeId, название) — если API не ответил
STATIC_MUSICSET_CATEGORIES: List[Tuple[str, str]] = [
    ("all", "Все подборки"), ("news", "Новинки"), ("top100", "Топ Zaycev.net"),
    ("zhanry", "По жанрам"), ("mood", "По настроению"), ("epochs", "По эпохам"),
    ("starsets", "Звёздная подборка"), ("auto", "Музыка в машину"),
    ("holiday", "К празднику"), ("sport", "Музыка для тренировок"),
    ("dance", "Для танцев"), ("national", "Народная музыка"),
    ("children", "Детская музыка"), ("soundtrack", "Саундтреки"),
    ("stayhome", "Для отдыха дома"), ("calm", "Успокаивающая музыка"),
    ("bezslov", "Музыка без слов"), ("massage", "Музыка для массажа"),
    ("newyear", "Новогодние"), ("background", "Фоновая музыка"),
    ("other", "Прочее"),
]


# ------------------------------------------------------------------ модели

@dataclass
class ZyTrack:
    id: str
    title: str
    artist: str = ""
    artist_id: str = ""
    duration: int = 0
    streaming: str = ""      # хеш для track/play/{hash}
    image: str = ""
    album: str = ""


@dataclass
class ZyMusicset:
    url: str                 # slug подборки (pages/musicset/tracks?url=)
    title: str
    category: str = ""
    track_count: int = 0


# ------------------------------------------------------------------ утилиты

def _parse_duration(v: Any) -> int:
    """'02:48' | '1:02:48' | 168 | '168' → секунды."""
    if v is None or v == "":
        return 0
    if isinstance(v, bool):
        return 0
    if isinstance(v, (int, float)):
        return max(0, int(v))
    s = str(v).strip()
    if s.isdigit():
        return int(s)
    parts = s.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    sec = 0
    for n in nums:
        sec = sec * 60 + n
    return sec


def _is_id(v: Any) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return True
    return isinstance(v, str) and v.strip().isdigit()


def _is_track_dict(d: Any) -> bool:
    if not isinstance(d, dict):
        return False
    if not _is_id(d.get("id")):
        return False
    if not (isinstance(d.get("track"), str) or "streaming" in d):
        return False
    return "artistName" in d or "artistId" in d or "streaming" in d


def _walk(obj: Any, fn: Callable[[dict, int], None], depth: int = 0) -> None:
    if depth > 9:
        return
    if isinstance(obj, dict):
        fn(obj, depth)
        for v in obj.values():
            if isinstance(v, (dict, list)):
                _walk(v, fn, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            if isinstance(v, (dict, list)):
                _walk(v, fn, depth + 1)


def _find_id_order(obj: Any, keys=("trackIds", "list", "ids")) -> Optional[list]:
    """Первый список скалярных id под ключами trackIds/list/ids (в глубину)."""
    found: List[list] = []

    def fn(d: dict, _depth: int) -> None:
        if found:
            return
        for k in keys:
            v = d.get(k)
            if isinstance(v, list) and v and all(
                _is_id(x) or (isinstance(x, str) and x) for x in v
            ) and not any(isinstance(x, (dict, list)) for x in v):
                found.append(v)
                return

    _walk(obj, fn)
    return found[0] if found else None


def _norm_image(u: Any) -> str:
    if not u or not isinstance(u, str):
        return ""
    u = u.strip()
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("/"):
        return "https://img.zaycev.net" + u
    return u


def _to_track(d: dict) -> ZyTrack:
    tid = str(d.get("id")).strip()
    title = str(d.get("track") or d.get("title") or "").strip() or f"Track {tid}"
    artist = str(d.get("artistName") or d.get("artist") or "").strip()
    dur = _parse_duration(d.get("durationTime")) or _parse_duration(d.get("duration"))
    img = _norm_image(d.get("imageJpg") or d.get("imageWebp") or d.get("image") or "")
    album = ""
    alb = d.get("album")
    if isinstance(alb, dict):
        album = str(alb.get("title") or alb.get("name") or "")
    elif isinstance(alb, str):
        album = alb
    return ZyTrack(
        id=tid,
        title=title,
        artist=artist,
        artist_id=str(d.get("artistId") or "").strip(),
        duration=dur,
        streaming=str(d.get("streaming") or "").strip(),
        image=img,
        album=album,
    )


def _playable(d: dict) -> bool:
    if d.get("notAvailable") is True:
        return False
    if d.get("playbackEnabled") is False:
        return False
    return True


# ------------------------------------------------------------------ разбор HTML сайта

import html as _html

_TRACK_HREF = re.compile(r'href="(?:https?://zaycev\.net)?/pages/\d+/(\d+)\.shtml"')
_ARTIST_HREF = re.compile(r'href="(?:https?://zaycev\.net)?/artist/(\d+)\.shtml[^"]*"')
_SPAN = re.compile(r"<span[^>]*>([^<]*)</span>")


def _clean(t: str) -> str:
    return _html.unescape(re.sub(r"\s+", " ", t or "")).strip()


def parse_tracks_html(body: str) -> List[ZyTrack]:
    """Треки со страницы сайта: блоки data-qa="track" (id из /pages/N/ID.shtml)."""
    out: List[ZyTrack] = []
    seen = set()
    parts = (body or "").split('data-qa="track"')[1:]
    for part in parts:
        part = part[:6000]
        m = _TRACK_HREF.search(part)
        if not m:
            continue
        tid = m.group(1)
        if tid in seen:
            continue
        seen.add(tid)
        title = ""
        a = re.search(r'data-qa="track-link"[^>]*>(.*?)</a>', part, re.S)
        if not a:
            a = re.search(r'<a[^>]*data-qa="track-link"[^>]*>(.*?)</a>', part, re.S)
        if a:
            sp = _SPAN.search(a.group(1))
            title = _clean(sp.group(1) if sp else re.sub(r"<[^>]+>", "", a.group(1)))
        artist, aid = "", ""
        am = re.search(r'<a[^>]*href="[^"]*/artist/(\d+)\.shtml[^"]*"[^>]*data-qa="artist-link"[^>]*>(.*?)</a>', part, re.S)
        if am:
            aid = am.group(1)
            sp = _SPAN.search(am.group(2))
            artist = _clean(sp.group(1) if sp else re.sub(r"<[^>]+>", "", am.group(2)))
        dm = re.search(r'data-qa="track-duration"[^>]*>([^<]+)<', part)
        dur = _parse_duration(dm.group(1)) if dm else 0
        im2 = re.search(r'<meta content="([^"]+)" itemprop="thumbnailUrl"', part)
        img = _norm_image(im2.group(1)) if im2 else ""
        if img.endswith(".webp"):
            img = img[:-5] + ".jpg"
        out.append(ZyTrack(id=tid, title=title or f"Track {tid}", artist=artist,
                           artist_id=aid, duration=dur, image=img))
    return out


def parse_artists_html(body: str) -> List[dict]:
    """Исполнители со страницы поиска: data-qa="artist-name-link"."""
    out: List[dict] = []
    seen = set()
    chunks = re.split(r'(?=<li type="list")', body or "")
    for ch in chunks:
        if 'data-qa="artist-name-link"' not in ch:
            continue
        ch = ch[:5000]
        m = re.search(r'href="[^"]*/artist/(\d+)\.shtml"[^>]*title="([^"]*)"', ch)
        if not m:
            continue
        aid, name = m.group(1), _clean(m.group(2))
        if aid in seen or not name:
            continue
        seen.add(aid)
        n = re.search(r"<span>(\d+)\s*трек", ch)
        out.append({"id": aid, "name": name, "nb_fan": 0,
                    "nb_tracks": int(n.group(1)) if n else 0})
    return out


def parse_musicsets_html(body: str) -> List[ZyMusicset]:
    out: List[ZyMusicset] = []
    seen = set()
    for m in re.finditer(
        r'href="(?:https?://zaycev\.net)?/musicset/([^/"]+)/([^/".]+)\.shtml"[^>]*title="([^"]*)"[^>]*data-qa="musicset-item"',
        body or "",
    ):
        cat, url, title = m.group(1), m.group(2), _clean(m.group(3))
        if url in seen:
            continue
        seen.add(url)
        title = re.sub(r"^Плейлист\s+", "", title)
        out.append(ZyMusicset(url=url, title=title or url, category=cat))
    return out


_GENRE_A = re.compile(
    r'<a\b[^>]*?href="(?:https?://zaycev\.net)?/genres/([A-Za-z0-9_\-]+(?:/[A-Za-z0-9_\-]+)?)/index\.html[^"]*"[^>]*>(.*?)</a>',
    re.S,
)


def parse_genres_html(body: str) -> List[Tuple[str, str]]:
    """Жанры и поджанры со страницы /genres/index.html: [(slug|parent/sub, название)] в порядке сайта."""
    out: List[Tuple[str, str]] = []
    seen = set()
    for m in _GENRE_A.finditer(body or ""):
        slug = m.group(1).strip("/")
        if slug in ("index",) or slug in seen:
            continue
        tm = re.search(r'title="([^"]+)"', m.group(0)[: m.group(0).find(">") + 1])
        title = _clean(tm.group(1)) if tm else _clean(re.sub(r"<[^>]+>", "", m.group(2)))
        if not title:
            continue
        seen.add(slug)
        out.append((slug, title))
    return out


# ------------------------------------------------------------------ клиент

class ZaycevClient:
    def __init__(
        self,
        enabled: bool = True,
        cache_sec: int = 300,
        tracks_per_chunk: int = 100,
        stream_mode: str = "auto",
        buffer_timeout: float = 6.0,
        buffer_limit_mb: float = 64.0,
        base_url: str = BASE,
    ):
        self.enabled = bool(enabled)
        self.cache_sec = max(30, int(cache_sec or 300))
        # элементов в одной DLNA-«Странице» (как у PromoDJ)
        self.tracks_per_chunk = max(20, min(1000, int(tracks_per_chunk or 100)))
        self.stream_mode = (stream_mode or "auto").lower().strip()
        self.buffer_timeout = float(buffer_timeout or 6.0)
        self.buffer_limit_mb = float(buffer_limit_mb or 64.0)
        self.buffer_limit_bytes = int(max(1.0, self.buffer_limit_mb) * 1024 * 1024)
        self.base = (base_url or BASE).rstrip("/")

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": UA,
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        })
        self._lock = threading.RLock()
        self._cache: Dict[str, Tuple[float, Any]] = {}
        self._tracks: "OrderedDict[str, ZyTrack]" = OrderedDict()
        self._stream_cache: Dict[str, Tuple[float, str]] = {}
        self._unavailable: Dict[str, float] = {}
        self._warmed = False
        self._mset_cat: Dict[str, str] = {}   # url подборки → категория (для HTML-запаса)

    # ---------- кэш ----------

    def _get_cache(self, key: str, ttl: Optional[int] = None):
        with self._lock:
            item = self._cache.get(key)
            if not item:
                return None
            ts, val = item
            if time.time() - ts > (ttl or self.cache_sec):
                self._cache.pop(key, None)
                return None
            return val

    def _set_cache(self, key: str, val: Any) -> None:
        with self._lock:
            if len(self._cache) > 600:
                # грубая чистка: выкидываем самую старую половину
                for k, _ in sorted(self._cache.items(), key=lambda kv: kv[1][0])[:300]:
                    self._cache.pop(k, None)
            self._cache[key] = (time.time(), val)

    def _remember(self, tracks: List[ZyTrack]) -> None:
        with self._lock:
            for t in tracks:
                self._tracks[t.id] = t
                self._tracks.move_to_end(t.id)
            while len(self._tracks) > MAX_TRACK_MEMORY:
                self._tracks.popitem(last=False)

    def get_cached_track(self, track_id: str) -> Optional[ZyTrack]:
        with self._lock:
            return self._tracks.get(str(track_id))

    # ---------- HTTP ----------

    def _warmup(self) -> None:
        """Получить cookie сайта (user_id и т.п.) — API иногда требует их."""
        try:
            self.session.get(self.base + "/", timeout=15,
                             headers={"Accept": "text/html,*/*;q=0.8"})
            self.session.get(f"{self.base}{API_PATH}/auth/refresh", timeout=10,
                             headers={"Accept": "application/json"})
        except Exception as e:
            log.debug("Zaycev warmup: %s", e)
        self._warmed = True

    def _api(
        self,
        path: str,
        params: Optional[dict] = None,
        method: str = "GET",
        body: Optional[dict] = None,
    ) -> Optional[Any]:
        url = f"{self.base}{API_PATH}/{path.lstrip('/')}"
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": self.base + "/",
            "Origin": self.base,
        }
        for attempt in (1, 2):
            try:
                r = self.session.request(
                    method, url, params=params, json=body, headers=headers, timeout=20,
                )
            except Exception as e:
                log.warning("Zaycev API %s: %s", path, e)
                return None
            if r.status_code in (401, 403, 429) and attempt == 1:
                self._warmup()
                continue
            if r.status_code != 200:
                log.warning("Zaycev API %s → HTTP %s", path, r.status_code)
                return None
            try:
                return r.json()
            except Exception:
                log.warning("Zaycev API %s: ответ не JSON", path)
                return None
        return None

    def _get_html(self, url: str) -> str:
        try:
            r = self.session.get(
                url, timeout=20,
                headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                         "Referer": self.base + "/"},
            )
            if r.status_code != 200:
                return ""
            r.encoding = r.encoding or "utf-8"
            return r.text
        except Exception as e:
            log.debug("Zaycev html %s: %s", url[:80], e)
            return ""

    # ---------- разбор ответов ----------

    def _extract_tracks(self, data: Any) -> List[ZyTrack]:
        """Достать треки из любого ответа API в порядке показа на сайте."""
        if data is None:
            return []
        infos: Dict[str, dict] = {}
        appear: List[str] = []

        def add(tid: str, d: dict) -> None:
            if tid not in infos:
                infos[tid] = d
                appear.append(tid)

        def fn(d: dict, _depth: int) -> None:
            if _is_track_dict(d):
                add(str(d.get("id")).strip(), d)
                return
            # tracksInfo / info: {"25077263": {track, artistName, streaming…}} —
            # id только ключом, внутри словаря его нет
            for k, v in d.items():
                if _is_id(k) and isinstance(v, dict) and "id" not in v and (
                    "streaming" in v or isinstance(v.get("track"), str)
                ) and ("artistName" in v or "streaming" in v):
                    v2 = dict(v)
                    v2["id"] = str(k).strip()
                    add(str(k).strip(), v2)

        _walk(data, fn)
        if not infos:
            return []
        order = _find_id_order(data)
        ids: List[str]
        if order:
            ids = [str(i).strip() for i in order if str(i).strip() in infos]
            if not ids:
                ids = appear
        else:
            ids = appear
        out = [_to_track(infos[i]) for i in ids if _playable(infos[i])]
        self._remember(out)
        return out

    @staticmethod
    def _pages_count(data: Any) -> Optional[int]:
        if isinstance(data, dict):
            for k in ("pagesCount", "pageCount", "totalPages"):
                v = data.get(k)
                if isinstance(v, int) and not isinstance(v, bool):
                    return max(1, v)
                if isinstance(v, str) and v.isdigit():
                    return max(1, int(v))
            for v in data.values():
                if isinstance(v, dict):
                    r = ZaycevClient._pages_count(v)
                    if r:
                        return r
        return None

    @staticmethod
    def _find_titled(data: Any, need: Tuple[str, ...]) -> List[dict]:
        """Все словари, у которых есть ВСЕ ключи из need и title/name — строка."""
        out: List[dict] = []

        def fn(d: dict, _depth: int) -> None:
            if all(k in d for k in need):
                out.append(d)

        _walk(data, fn)
        return out

    # ---------- списки треков (с пагинацией) ----------

    def _list_request(self, kind: str, key: str, page: int, limit: int) -> Optional[Any]:
        if kind == "new":
            return self._api("pages/new", {"page": page, "limit": limit, "limitRecommended": 0})
        if kind == "top":
            return self._api("pages/index/top", {
                "page": page, "limit": limit,
                "period": key if key in ("day", "week", "month") else "day",
                "entity": "track",
            })
        if kind in ("genre", "genrenew"):
            return self._api(f"pages/genres/{key}/tracks", {
                "page": page, "limit": limit,
                "sort": "new" if kind == "genrenew" else "popularity",
            })
        if kind == "artist":
            return self._api(f"pages/artist/{key}/tracks", {
                "page": page, "limit": limit, "sort": "popularity",
            })
        if kind == "mset":
            if page > 1:
                return None   # подборка отдаётся целиком одним ответом
            data = self._api("pages/musicset/tracks", {"url": key})
            if data is not None and self._extract_tracks(data):
                return data
            return self._api("pages/musicset/item", {"url": key, "limitMusicsets": 1})
        if kind == "srch":
            return self._api("pages/search/tracks", {"q": key, "page": page, "limit": limit})
        return None

    def _html_list_url(self, kind: str, key: str, page: int) -> str:
        from urllib.parse import quote
        b = self.base
        pg = f"page={page}"
        if kind == "new":
            return f"{b}/new/index.html?{pg}"
        if kind == "top":
            sort = f"&popular-sort={key}" if key in ("week", "month") else ""
            return f"{b}/popular/index.html?{pg}{sort}"
        if kind == "genre":
            return f"{b}/genres/{key}/index.html?{pg}"
        if kind == "genrenew":
            return f"{b}/genres/{key}/index.html?{pg}&genres-sort=new"
        if kind == "artist":
            return f"{b}/artist/{key}.shtml?tracks-page={page}"
        if kind == "mset":
            if page > 1:
                return ""
            cat = self._mset_cat.get(key) or "news"
            return f"{b}/musicset/{cat}/{key}.shtml"
        if kind == "srch":
            return f"{b}/search?query_search={quote(key)}&type=tracks&{pg}"
        return ""

    @staticmethod
    def _html_pages_count(body: str) -> int:
        nums = [int(n) for n in re.findall(r"[?&;](?:amp;)?(?:page|tracks-page)=(\d+)", body or "")]
        return max(nums) if nums else 1

    def _html_tracks(self, kind: str, key: str, page: int) -> Tuple[List[ZyTrack], int]:
        url = self._html_list_url(kind, key, page)
        if not url:
            return [], 1
        body = self._get_html(url)
        tracks = parse_tracks_html(body)
        if tracks:
            self._remember(tracks)
            log.info("Zaycev: %s/%s стр.%s взято с HTML (%d треков)", kind, key, page, len(tracks))
        return tracks, self._html_pages_count(body)

    def fetch_page(
        self, kind: str, key: str, page: int = 1, limit: int = PAGE_LIMIT
    ) -> Tuple[List[ZyTrack], int]:
        """Одна страница списка → (треки, всего страниц). Сначала JSON-API, затем HTML сайта."""
        ck = f"pg:{kind}:{key}:{page}:{limit}"
        hit = self._get_cache(ck)
        if hit is not None:
            return hit
        data = self._list_request(kind, key, page, limit)
        tracks = self._extract_tracks(data)
        pc = self._pages_count(data) or 1
        if not tracks:
            tracks, pc = self._html_tracks(kind, key, page)
        if tracks:
            self._set_cache(ck, (tracks, pc))
        return tracks, pc

    def list_layout(self, kind: str, key: str) -> Tuple[int, int, int]:
        """(страниц сайта, треков на странице, DLNA-«Страниц»)."""
        tracks, pc = self.fetch_page(kind, key, 1)
        if not tracks:
            return 0, 0, 0
        per_page = max(1, len(tracks))
        pc = min(pc, MAX_SITE_PAGES)
        pages_per_chunk = max(1, self.tracks_per_chunk // per_page)
        chunks = (pc + pages_per_chunk - 1) // pages_per_chunk
        return pc, per_page, max(1, chunks)

    def get_chunk(self, kind: str, key: str, chunk: int) -> List[ZyTrack]:
        pc, per_page, chunks = self.list_layout(kind, key)
        if chunks <= 0 or chunk < 0 or chunk >= chunks:
            return []
        pages_per_chunk = max(1, self.tracks_per_chunk // per_page)
        first = chunk * pages_per_chunk + 1
        last = min(pc, first + pages_per_chunk - 1)
        out: List[ZyTrack] = []
        seen = set()
        for p in range(first, last + 1):
            tracks, _ = self.fetch_page(kind, key, p)
            if not tracks:
                break
            for t in tracks:
                if t.id not in seen:
                    seen.add(t.id)
                    out.append(t)
        return out

    # ---------- каталоги (динамически) ----------

    def _genres_tree(self) -> Tuple[List[Tuple[str, str]], Dict[str, List[Tuple[str, str]]]]:
        """Полное дерево жанров: (верхний уровень, {родитель: [поджанры]}).

        Источники объединяются, чтобы не терять жанры: страница сайта /genres/index.html
        (там все жанры и поджанры), JSON-API pages/genres (+ /subgenres), запасной список.
        Кэш — 1 час, затем снова подтягивается с сайта (новые жанры появляются сами)."""
        ck = "genres_tree"
        hit = self._get_cache(ck, ttl=3600)
        if hit:
            return hit
        top: List[Tuple[str, str]] = []
        subs: Dict[str, List[Tuple[str, str]]] = {}

        def add_top(slug: str, title: str) -> None:
            if slug and title and "/" not in slug and not any(s == slug for s, _ in top):
                top.append((slug, title))

        def add_sub(parent: str, slug: str, title: str) -> None:
            if not (slug and title) or slug == parent:
                return
            if "/" not in slug:
                slug = f"{parent}/{slug}"
            lst = subs.setdefault(parent, [])
            if not any(s == slug for s, _ in lst):
                lst.append((slug, title))

        got_site = False
        # 1) страница сайта — главный источник (порядок и состав как на сайте)
        body = self._get_html(f"{self.base}/genres/index.html")
        for slug, title in parse_genres_html(body):
            got_site = True
            if "/" in slug:
                add_sub(slug.split("/", 1)[0], slug, title)
            else:
                add_top(slug, title)
        # 2) JSON-API — добавляет то, чего нет в HTML
        data = self._api("pages/genres")
        if data is not None:
            cands = self._find_titled(data, ("url", "title"))
            main = [d for d in cands if "subGenreCount" in d or "image200" in d]
            for d in (main or cands):
                slug = str(d.get("url") or "").strip().strip("/")
                title = str(d.get("title") or "").strip()
                if "/" in slug:
                    add_sub(slug.split("/", 1)[0], slug, title)
                else:
                    add_top(slug, title)
        # 3) запасной список — только если чего-то не хватает
        for slug, title in STATIC_GENRES:
            add_top(slug, title)
        for parent, lst in STATIC_SUBGENRES.items():
            for slug, title in lst:
                add_sub(parent, slug, title)
        # 4) поджанры верхних жанров из API (у «Другое» и др. их может быть больше)
        for slug, _t in list(top):
            if slug in subs and got_site:
                continue
            d2 = self._api(f"pages/genres/{slug}/subgenres")
            if d2 is not None:
                for d in self._find_titled(d2, ("url", "title")):
                    add_sub(slug, str(d.get("url") or "").strip().strip("/"),
                            str(d.get("title") or "").strip())
        res = (top, subs)
        self._set_cache(ck, res)
        return res

    def list_genres(self) -> List[Tuple[str, str]]:
        """Жанры верхнего уровня: [(slug, название)]."""
        return list(self._genres_tree()[0])

    def list_subgenres(self, slug: str) -> List[Tuple[str, str]]:
        """Поджанры жанра: [(parent/sub, название)]."""
        return list(self._genres_tree()[1].get(slug, []))

    def genre_title(self, slug: str) -> str:
        top, subs = self._genres_tree()
        for s_, t_ in top:
            if s_ == slug:
                return t_
        for lst in subs.values():
            for s_, t_ in lst:
                if s_ == slug:
                    return t_
        return slug

    def genre_sets_category(self) -> str:
        """typeId категории «Сборники по жанрам» (ищем по названию, запасной — zhanry)."""
        for tid, title in self.list_musicset_categories():
            if "жанр" in title.lower():
                return tid
        return GENRE_SETS_CATEGORY

    def list_musicset_categories(self) -> List[Tuple[str, str]]:
        ck = "mset_cats"
        hit = self._get_cache(ck, ttl=3600)
        if hit:
            return list(hit)
        data = self._api("pages/musicset/categories/readAll")
        out: List[Tuple[str, str]] = []
        if data is not None:
            for d in self._find_titled(data, ("typeId", "title")):
                if d.get("visible") is False:
                    continue
                tid = str(d.get("typeId") or "").strip()
                title = str(d.get("title") or "").strip()
                if not tid or not title:
                    continue
                if not any(x == tid for x, _ in out):
                    out.append((tid, title[:1].upper() + title[1:]))
        if out:
            self._set_cache(ck, out)
            return list(out)
        return list(STATIC_MUSICSET_CATEGORIES)

    @staticmethod
    def _mset_from(d: dict) -> Optional[ZyMusicset]:
        url = d.get("url")
        title = d.get("title")
        if not (isinstance(url, str) and url.strip() and isinstance(title, str) and title.strip()):
            return None
        if not ("trackCount" in d or "category" in d or "square200" in d or "presentationDate" in d):
            return None
        try:
            n = int(d.get("trackCount") or 0)
        except (TypeError, ValueError):
            n = 0
        return ZyMusicset(
            url=url.strip().strip("/"),
            title=title.strip(),
            category=str(d.get("category") or ""),
            track_count=n,
        )

    def _musicsets_request(self, category: str, page: int, limit: int) -> Optional[Any]:
        if category.startswith("genre:"):
            return self._api(f"pages/genres/{category[6:]}/musicsets", {"limit": limit, "page": page})
        if category in ("", "all"):
            data = self._api("pages/musicset/list", {"limit": limit, "page": page, "query": ""})
        else:
            data = self._api("pages/musicset", {
                "type": "category", "limit": limit, "page": page, "query": category,
            })
        return data

    def fetch_musicsets_page(
        self, category: str, page: int = 1, limit: int = 30
    ) -> Tuple[List[ZyMusicset], int]:
        ck = f"msp:{category}:{page}:{limit}"
        hit = self._get_cache(ck)
        if hit is not None:
            return hit
        data = self._musicsets_request(category, page, limit)
        found: Dict[str, ZyMusicset] = {}
        order_hint: List[str] = []

        def fn(d: dict, _depth: int) -> None:
            ms = self._mset_from(d)
            if ms and ms.url not in found:
                found[ms.url] = ms
                order_hint.append(ms.url)

        if data is not None:
            _walk(data, fn)
        ids = _find_id_order(data) if data is not None else None
        if ids and all(isinstance(i, str) for i in ids):
            ordered = [found[i.strip("/")] for i in ids if i.strip("/") in found]
            items = ordered or [found[u] for u in order_hint]
        else:
            items = [found[u] for u in order_hint]
        pc = self._pages_count(data) or 1
        if not items:
            if category.startswith("genre:"):
                path = f"/genres/{category[6:]}/index.html"
            elif category in ("", "all"):
                path = "/musicset/index.html"
            else:
                path = f"/musicset/{category}/index.html"
            body = self._get_html(f"{self.base}{path}?page={page}")
            items = parse_musicsets_html(body)
            pc = self._html_pages_count(body)
        for m in items:
            if m.category:
                self._mset_cat[m.url] = m.category
        if items:
            self._set_cache(ck, (items, pc))
        return items, pc

    def musicsets_layout(self, category: str) -> Tuple[int, int, int]:
        items, pc = self.fetch_musicsets_page(category, 1)
        if not items:
            return 0, 0, 0
        per_page = max(1, len(items))
        pc = min(pc, MAX_SITE_PAGES)
        ppc = max(1, self.tracks_per_chunk // per_page)
        return pc, per_page, max(1, (pc + ppc - 1) // ppc)

    def get_musicsets_chunk(self, category: str, chunk: int) -> List[ZyMusicset]:
        pc, per_page, chunks = self.musicsets_layout(category)
        if chunks <= 0 or chunk < 0 or chunk >= chunks:
            return []
        ppc = max(1, self.tracks_per_chunk // per_page)
        first = chunk * ppc + 1
        last = min(pc, first + ppc - 1)
        out: List[ZyMusicset] = []
        seen = set()
        for p in range(first, last + 1):
            items, _ = self.fetch_musicsets_page(category, p)
            if not items:
                break
            for m in items:
                if m.url not in seen:
                    seen.add(m.url)
                    out.append(m)
        return out

    # ---------- поиск ----------

    def search_tracks_page(
        self, query: str, page: int = 1, limit: int = 20
    ) -> Tuple[List[ZyTrack], int]:
        q = (query or "").strip()
        if not q:
            return [], 0
        return self.fetch_page("srch", q, page, limit)

    def search_tracks(self, query: str, limit: int = 40) -> List[ZyTrack]:
        """Треки для DLNA Search(): несколько страниц до limit."""
        q = (query or "").strip()
        if not q:
            return []
        out: List[ZyTrack] = []
        seen = set()
        page = 1
        while len(out) < limit and page <= 5:
            tracks, pc = self.fetch_page("srch", q, page, PAGE_LIMIT)
            if not tracks:
                break
            for t in tracks:
                if t.id not in seen:
                    seen.add(t.id)
                    out.append(t)
            if page >= pc:
                break
            page += 1
        log.info("Zaycev search %r → %d tracks", q, len(out))
        return out[:limit]

    def search_artists(self, query: str, limit: int = 20) -> List[dict]:
        """Исполнители (через веб-поиск): [{id, name, nb_fan}].

        Сначала pages/search/artists; если API ответил иначе, берём
        исполнителей из найденных треков (artistId/artistName)."""
        q = (query or "").strip()
        if not q:
            return []
        ck = f"sart:{q.lower()}"
        hit = self._get_cache(ck)
        if hit is not None:
            return list(hit)[:limit]

        found: "OrderedDict[str, dict]" = OrderedDict()
        from urllib.parse import quote
        # 1) страница поиска сайта (как её видит пользователь): блок «Исполнители»
        for typ in ("artists", "all"):
            body = self._get_html(f"{self.base}/search?query_search={quote(q)}&type={typ}")
            for a in parse_artists_html(body):
                found.setdefault(a["id"], a)
            if found:
                break
        data = None if found else self._api(
            "pages/search/artists", {"q": q, "page": 1, "limit": max(limit, 15)})
        if data is not None:
            ids_order = _find_id_order(data)
            cand: Dict[str, dict] = {}
            appear: List[str] = []

            def fn(d: dict, _depth: int) -> None:
                if _is_track_dict(d) or not _is_id(d.get("id")):
                    return
                name = ""
                for k in ("name", "artistName", "title"):
                    if isinstance(d.get(k), str) and d[k].strip():
                        name = d[k].strip()
                        break
                if not name:
                    return
                aid = str(d["id"]).strip()
                if aid not in cand:
                    cand[aid] = {"id": aid, "name": name, "nb_fan": 0}
                    appear.append(aid)

            _walk(data, fn)
            order = [str(i).strip() for i in (ids_order or []) if str(i).strip() in cand] or appear
            for aid in order:
                found[aid] = cand[aid]

        if not found:
            tracks, _ = self.fetch_page("srch", q, 1, PAGE_LIMIT)
            ql = q.lower()
            scored: List[Tuple[int, str, str]] = []
            for t in tracks:
                if t.artist_id and t.artist and t.artist_id not in {s[1] for s in scored}:
                    # артисты, чьё имя содержит запрос, выше остальных
                    scored.append((0 if ql in t.artist.lower() else 1, t.artist_id, t.artist))
            scored.sort(key=lambda s: s[0])
            for _, aid, name in scored:
                found[aid] = {"id": aid, "name": name, "nb_fan": 0}

        res = list(found.values())
        if res:
            self._set_cache(ck, res)
        return res[:limit]

    # ---------- данные трека / стрим ----------

    def _filezmeta(self, tid: str) -> str:
        """Хеш streaming трека: POST track/filezmeta → {tracks:[{id, streaming, download}]}.

        Так делает плеер сайта: в списках треков streaming пустой, настоящий хеш
        приходит отдельным запросом «проверки» треков."""
        try:
            num: Any = int(tid)
        except ValueError:
            num = tid
        for subscription in (None, False):
            data = self._api("track/filezmeta", method="POST",
                             body={"trackIds": [num], "subscription": subscription})
            if data is None:
                continue
            found: List[str] = []

            def fn(d: dict, _depth: int) -> None:
                if str(d.get("id", "")).strip() == tid and isinstance(d.get("streaming"), str):
                    found.append(d["streaming"].strip())

            _walk(data, fn)
            for h in found:
                if h:
                    return h
            # в ответе может быть словарь {id: {streaming…}}
            if isinstance(data, dict):
                for container in (data, data.get("tracks"), data.get("tracksInfo")):
                    if isinstance(container, dict):
                        v = container.get(tid)
                        if isinstance(v, dict) and isinstance(v.get("streaming"), str) and v["streaming"].strip():
                            return v["streaming"].strip()
            log.info("Zaycev filezmeta %s (subscription=%s) без хеша: %s",
                     tid, subscription, json.dumps(data, ensure_ascii=False)[:400])
            if found:      # ответ есть, но хеш пуст: трек заблокирован
                return ""
        return ""

    def _fetch_track(self, tid: str) -> Optional[ZyTrack]:
        """Найти данные трека (нужен хеш streaming), если его нет в памяти."""
        def pick(data: Any) -> Optional[ZyTrack]:
            for t in self._extract_tracks(data):
                if t.id == tid and t.streaming:
                    return t
            return None

        data = self._api("track/tracks/info", method="POST",
                         body={"tracksList": [int(tid)], "source": "track"})
        t = pick(data)
        if t:
            return t
        data = self._api("pages/track", {
            "id": tid, "othersPage": 1, "popularPage": 1,
            "othersLimit": 1, "limitPopular": 1, "limitMusicset": 1,
        })
        t = pick(data)
        if t:
            return t
        # HTML страницы трека: /pages/{id//100}/{id}.shtml → __NEXT_DATA__
        try:
            page = f"{self.base}/pages/{int(tid) // 100}/{tid}.shtml"
        except ValueError:
            return None
        body = self._get_html(page)
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', body or "", re.S)
        if m:
            try:
                t = pick(json.loads(m.group(1)))
                if t:
                    return t
            except Exception:
                pass
        m2 = re.search(r'"streaming"\s*:\s*"([^"]+)"', body or "")
        if m2:
            return ZyTrack(id=tid, title=f"Track {tid}", streaming=m2.group(1))
        return None

    def resolve_stream_url(self, track_id: str, force: bool = False) -> Optional[str]:
        tid = str(track_id)
        now = time.time()
        bad = self._unavailable.get(tid)
        if bad and now - bad < 60 and not force:
            return None
        if not force:
            hit = self._stream_cache.get(tid)
            if hit and now - hit[0] < min(self.cache_sec, 120):
                return hit[1]
        tr = self.get_cached_track(tid)
        if not tr or not tr.streaming:
            h = self._filezmeta(tid)
            if h:
                if tr is None:
                    tr = ZyTrack(id=tid, title=f"Track {tid}")
                tr.streaming = h
                self._remember([tr])
        if not tr or not tr.streaming:
            tr = self._fetch_track(tid) or tr
            if tr:
                self._remember([tr])
        if not tr or not tr.streaming:
            log.warning("Zaycev: нет streaming-хеша для трека %s", tid)
            self._unavailable[tid] = now
            return None
        data = self._api(f"track/play/{tr.streaming}")
        if not (isinstance(data, dict) and data.get("url")):
            log.info("Zaycev track/play/%s → %s", tr.streaming[:16],
                     json.dumps(data, ensure_ascii=False)[:400] if data is not None else "нет ответа")
        url = ""
        if isinstance(data, dict):
            url = str(data.get("url") or "").strip()
            if not url:
                for v in data.values():
                    if isinstance(v, dict) and isinstance(v.get("url"), str):
                        url = v["url"].strip()
                        break
        if not url:
            log.warning("Zaycev: track/play/%s без url", tr.streaming[:12])
            # хеш мог протухнуть — забываем, в следующий раз найдём заново
            with self._lock:
                t0 = self._tracks.get(tid)
                if t0:
                    t0.streaming = ""
            self._unavailable[tid] = now
            return None
        url = urljoin(self.base + "/", url)
        log.info("Zaycev: трек %s → %s", tid, url[:90])
        self._stream_cache[tid] = (now, url)
        return url

    def get_cover_url(self, track_id: str) -> str:
        t = self.get_cached_track(str(track_id))
        return t.image if t else ""

    def open_stream(self, track_id: str):
        """(response, mime, content_length|None, True) или None.

        Цепочка как в браузере: dl.zaycev.net → 302 → zerocdn → 302 → pulse (206).
        Попытка 1 — обычный GET; попытка 2 — свежая ссылка и Range: bytes=0-."""
        tid = str(track_id)
        for attempt in (1, 2):
            url = self.resolve_stream_url(tid, force=(attempt == 2))
            if not url:
                return None
            headers = {
                "User-Agent": UA,
                "Accept": "*/*",
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                "Referer": self.base + "/",
                "Origin": self.base,
                "Sec-Fetch-Dest": "audio",
                "Sec-Fetch-Mode": "no-cors",
                "Sec-Fetch-Site": "cross-site",
            }
            if attempt == 2:
                headers["Range"] = "bytes=0-"
            try:
                r = self.session.get(url, stream=True, timeout=30,
                                     allow_redirects=True, headers=headers)
            except Exception as e:
                log.warning("Zaycev open_stream %s (попытка %d): %s", tid, attempt, e)
                self._stream_cache.pop(tid, None)
                continue
            mime = r.headers.get("Content-Type") or "audio/mpeg"
            hops = " → ".join(
                [f"{h.status_code} {urlparse(h.url).netloc}" for h in r.history]
                + [f"{r.status_code} {urlparse(r.url).netloc}"]
            )
            if r.status_code >= 400 or "html" in mime.lower() or "json" in mime.lower():
                log.warning("Zaycev stream %s (попытка %d): %s (%s)", tid, attempt, hops, mime)
                r.close()
                self._stream_cache.pop(tid, None)
                continue
            cl = r.headers.get("Content-Length")
            clen = int(cl) if cl and cl.isdigit() else None
            if "octet" in mime.lower():
                mime = "audio/mpeg"
            log.info("Zaycev stream %s ok: %s · %s · %s байт", tid, hops, mime, clen)
            return r, mime, clen, True
        self._unavailable[tid] = time.time()
        return None
