"""Звук (zvuk.com) — поиск, коллекция, плейлисты и стрим по X-Auth-Token.

Неофициальный API: Tiny REST + GraphQL.
Токен: войти на zvuk.com → https://zvuk.com/api/tiny/profile → поле token.
Требуется подписка для HIGH/FLAC; анонимный токен даёт только mid (128).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import requests

log = logging.getLogger("dlna_emu.zvuk")

TINY = "https://zvuk.com/api/tiny"
GQL = "https://zvuk.com/api/v1/graphql"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# quality для /tiny/track/stream
QUALITY_MAP = {
    "mid": "mid",
    "mp3_128": "mid",
    "128": "mid",
    "high": "high",
    "mp3_320": "high",
    "320": "high",
    "flac": "flac",
    "lossless": "flac",
}

# Жанры как на https://zvuk.com/genres — query для search playlists
ZVUK_GENRES = [
    {"id": "pop", "title": "Поп музыка", "query": "поп",
     "tabs": ["Хиты", "Новинки", "Русский поп", "Зарубежный поп", "Поп 2000-х", "Поп 2010-х"]},
    {"id": "rusrap", "title": "Русский рэп", "query": "русский рэп",
     "tabs": ["Хиты", "Новинки", "Old school", "New school", "Лирика"]},
    {"id": "rock", "title": "Рок", "query": "рок",
     "tabs": ["Хиты", "Новинки", "Русский рок", "Зарубежный рок", "Классика рока"]},
    {"id": "shanson", "title": "Русский шансон", "query": "шансон",
     "tabs": ["Хиты", "Новинки", "Блатной", "Лирический"]},
    {"id": "electronic", "title": "Электронная музыка", "query": "электроника",
     "tabs": ["Хиты", "Новинки", "House", "Techno", "Trance", "Drum & Bass"]},
    {"id": "classical", "title": "Классическая музыка", "query": "классика",
     "tabs": ["Хиты", "Опера", "Симфонии", "Фортепиано"]},
    {"id": "folk", "title": "Русский фолк", "query": "фолк",
     "tabs": ["Хиты", "Новинки", "Народная", "Этно"]},
    {"id": "dance", "title": "Танцевальная музыка", "query": "танцевальная",
     "tabs": ["Хиты", "Новинки", "Клубная", "EDM"]},
    {"id": "jazz", "title": "Джаз", "query": "джаз",
     "tabs": ["Хиты", "Новинки", "Smooth jazz", "Vocal jazz"]},
    {"id": "rnb", "title": "R&B", "query": "r&b",
     "tabs": ["Хиты", "Новинки", "Soul", "Neo soul"]},
    {"id": "metal", "title": "Метал", "query": "метал",
     "tabs": ["Хиты", "Новинки", "Heavy", "Death", "Black"]},
    {"id": "hiphop", "title": "Хип-хоп", "query": "хип-хоп",
     "tabs": ["Хиты", "Новинки", "Зарубежный", "Trap"]},
    {"id": "alt", "title": "Альтернативная музыка", "query": "альтернатива",
     "tabs": ["Хиты", "Новинки", "Indie rock", "Post-punk"]},
    {"id": "blues", "title": "Блюз", "query": "блюз",
     "tabs": ["Хиты", "Новинки", "Classic blues"]},
    {"id": "phonk", "title": "Фонк", "query": "фонк",
     "tabs": ["Хиты", "Новинки", "Drift phonk", "Brazilian phonk"]},
    {"id": "indie", "title": "Инди", "query": "инди",
     "tabs": ["Хиты", "Новинки", "Indie pop", "Indie rock"]},
    {"id": "live", "title": "Лайв концерты", "query": "концерт live",
     "tabs": ["Хиты", "Рок live", "Поп live"]},
    {"id": "world", "title": "Музыка мира", "query": "world music",
     "tabs": ["Хиты", "Новинки", "Latin", "African", "Asian"]},
]




# Подборки с главной / discovery (name для /api/tiny/grid/content или query для search)
ZVUK_CURATED = [
    {"id": "popular_playlists", "title": "Популярное", "source": "grid", "grid": "popular_playlists"},
    {"id": "editorial", "title": "Редакция", "source": "editorial"},
    {"id": "soundtracks", "title": "Саундтреки", "source": "search", "query": "музыка из кино саундтрек"},
    {"id": "best_albums", "title": "Лучшие альбомы XXI века", "source": "search", "query": "лучшие альбомы"},
    {"id": "editors", "title": "Выбор редакции", "source": "search", "query": "выбор редакции"},
    {"id": "new_faces", "title": "Новые лица", "source": "search", "query": "новые лица новинки"},
    {"id": "karaoke", "title": "Караоке", "source": "search", "query": "караоке"},
    {"id": "motivation", "title": "Мотивация", "source": "search", "query": "мотивация"},
    {"id": "epochs", "title": "Эпохи", "source": "search", "query": "хиты по годам эпохи"},
    {"id": "car", "title": "В машину", "source": "search", "query": "музыка в машину за рулем"},
    {"id": "training", "title": "Тренировка", "source": "search", "query": "тренировка"},
    {"id": "active", "title": "Активность", "source": "search", "query": "активность спорт"},
    {"id": "calm", "title": "Спокойствие", "source": "search", "query": "спокойствие релакс"},
    {"id": "romantic", "title": "Романтика", "source": "search", "query": "романтика"},
    {"id": "sleep", "title": "Сон", "source": "search", "query": "сон колыбельная"},
    {"id": "work_study", "title": "Работа и учёба", "source": "search", "query": "работа учёба фокус"},
    {"id": "nature", "title": "Природотерапия", "source": "search", "query": "природа звуки природы"},
    {"id": "party", "title": "Вечеринка", "source": "search", "query": "вечеринка"},
    {"id": "melancholy", "title": "Меланхолия", "source": "search", "query": "грустно меланхолия"},
    {"id": "relax", "title": "Релакс и чилл", "source": "search", "query": "релакс чилл"},
    {"id": "weekend", "title": "Выходные", "source": "search", "query": "выходные weekend"},
    {"id": "food", "title": "Еда и готовка", "source": "search", "query": "готовка кухня"},
    {"id": "wellness", "title": "Wellness", "source": "search", "query": "wellness медитация"},
    {"id": "charts", "title": "Чарты", "source": "search", "query": "топ 100 чарт"},
]


# Редакционные профили (zvuk.com/profile/…)
ZVUK_EDITORIAL_PROFILES = [
    {"id": "438615003", "title": "ГЛАВНЫЕ НОВИНКИ"},
    {"id": "21322059", "title": "Звук"},
    {"id": "386097314", "title": "HiFi"},
]

@dataclass
class ZvTrack:
    id: str
    title: str
    artist: str = ""
    duration: int = 0
    album: str = ""
    album_id: str = ""
    cover_uri: str = ""
    release_id: str = ""


@dataclass
class ZvPlaylist:
    id: str
    title: str
    track_count: int = 0
    is_public: bool = False
    tracks: List[ZvTrack] = field(default_factory=list)


class ZvukClient:
    def __init__(
        self,
        token: str = "",
        quality: str = "high",
        cache_sec: int = 300,
        stream_mode: str = "auto",
        buffer_timeout: float = 6.0,
    ):
        self.token = (token or "").strip()
        self.quality = (quality or "high").lower()
        self.cache_sec = cache_sec
        self.stream_mode = (stream_mode or "auto").lower().strip()
        self.buffer_timeout = float(buffer_timeout or 6.0)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": "https://zvuk.com/",
            "Origin": "https://zvuk.com",
        })
        if self.token:
            self.session.headers["X-Auth-Token"] = self.token
            # cookie тоже помогает некоторым эндпоинтам
            self.session.cookies.set("auth", self.token, domain=".zvuk.com")
            self.session.cookies.set("sauth", self.token, domain=".zvuk.com")
        self._account_ok: Optional[bool] = None
        self._user_name: Optional[str] = None
        self._is_anonymous: bool = True
        self._can_flac: bool = False
        self._tracks_cache: Dict[str, Tuple[float, List[ZvTrack]]] = {}
        self._playlist_cache: Dict[str, Tuple[float, List[ZvTrack]]] = {}
        self._playlists_cache: Tuple[float, List[ZvPlaylist]] = (0.0, [])
        self._liked_cache: Tuple[float, List[ZvTrack]] = (0.0, [])
        self._cover_map: Dict[str, str] = {}
        self._search_cache: Dict[str, Tuple[float, list]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def _tiny_get(self, path: str, params: Optional[dict] = None) -> Optional[dict]:
        if not self.enabled:
            return None
        try:
            r = self.session.get(f"{TINY}/{path.lstrip('/')}", params=params or {}, timeout=20)
            if r.status_code in (401, 403):
                log.warning("Zvuk auth error %s on %s", r.status_code, path)
                self._account_ok = False
                return None
            if r.status_code == 418 or (r.status_code >= 400 and b"<html" in r.content[:200].lower()):
                log.warning("Zvuk anti-bot on %s (HTTP %s)", path, r.status_code)
                return None
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict) and "result" in data:
                return data["result"]
            return data
        except Exception as e:
            log.warning("Zvuk tiny GET %s: %s", path, e)
            return None

    def _gql(self, query: str, variables: Optional[dict] = None, op: str = "") -> Optional[dict]:
        if not self.enabled:
            return None
        payload: dict = {"query": query}
        if op:
            payload["operationName"] = op
        if variables:
            payload["variables"] = variables
        try:
            r = self.session.post(GQL, json=payload, timeout=25)
            if r.status_code in (401, 403):
                log.warning("Zvuk GQL auth error %s", r.status_code)
                self._account_ok = False
                return None
            if r.status_code == 418 or (b"<html" in r.content[:200].lower()):
                log.warning("Zvuk anti-bot on GraphQL")
                return None
            r.raise_for_status()
            data = r.json()
            if data.get("errors"):
                log.warning("Zvuk GQL errors: %s", data["errors"][:2])
            return data.get("data") or {}
        except Exception as e:
            log.warning("Zvuk GQL: %s", e)
            return None

    def login(self) -> bool:
        if not self.token:
            log.info("Zvuk token not set — folder disabled")
            return False
        try:
            res = self._tiny_get("profile")
            if not res:
                # fallback: проверить токен через GraphQL
                data = self._gql("{ getCollection { tracks { id } } }", op="")
                if data is not None:
                    self._account_ok = True
                    self._is_anonymous = False
                    log.info("Zvuk: token OK (via GraphQL, profile blocked)")
                    return True
                log.warning("Zvuk: invalid token or blocked")
                self._account_ok = False
                return False
            profile = res.get("profile") or res
            self._user_name = (
                profile.get("name")
                or profile.get("login")
                or profile.get("username")
                or "?"
            )
            self._is_anonymous = bool(res.get("is_anonymous") or profile.get("is_anonymous"))
            sub = res.get("subscription") or profile.get("subscription") or {}
            features = res.get("features") or profile.get("features") or {}
            self._can_flac = bool(features.get("can_flac") or sub.get("status") == "confirmed")
            if self._is_anonymous:
                log.info("Zvuk: anonymous token (mid quality only)")
            else:
                log.info(
                    "Zvuk: logged in as %s (flac=%s)",
                    self._user_name,
                    self._can_flac,
                )
            self._account_ok = True
            return True
        except Exception as e:
            log.warning("Zvuk login: %s", e)
            self._account_ok = False
            return False

    def _track_from(self, t: dict) -> Optional[ZvTrack]:
        if not isinstance(t, dict):
            return None
        tid = t.get("id") or t.get("track_id")
        if tid is None:
            return None
        title = (t.get("title") or t.get("name") or "").strip() or f"Track {tid}"
        artists = t.get("artists") or t.get("artist") or []
        if isinstance(artists, list):
            names = []
            for a in artists:
                if isinstance(a, dict):
                    names.append(a.get("title") or a.get("name") or "")
                elif isinstance(a, str):
                    names.append(a)
            artist = ", ".join(n for n in names if n)
        elif isinstance(artists, dict):
            artist = artists.get("title") or artists.get("name") or ""
        else:
            artist = str(artists or "")
        duration = int(t.get("duration") or t.get("duration_ms") or 0)
        if duration > 10000:  # ms
            duration = duration // 1000
        release = t.get("release") or t.get("album") or {}
        if isinstance(release, dict):
            album = release.get("title") or release.get("name") or ""
            album_id = str(release.get("id") or "")
            release_id = album_id
        else:
            album = str(release or "")
            album_id = ""
            release_id = str(t.get("release_id") or t.get("album_id") or "")
        # CDN type=track часто 422; type=release работает — предпочитаем обложку релиза
        cover = ""
        if isinstance(release, dict) and release.get("image"):
            cover = release.get("image")
        if not cover:
            cover = t.get("image") or t.get("cover") or t.get("image_url") or ""
        if isinstance(cover, dict):
            cover = cover.get("src") or cover.get("url") or ""
        return ZvTrack(
            id=str(tid),
            title=title,
            artist=artist,
            duration=duration,
            album=album,
            album_id=album_id,
            cover_uri=str(cover or ""),
            release_id=release_id,
        )

    def _remember_cover(self, tid: str, uri: str) -> None:
        if uri and tid:
            if len(self._cover_map) > 4000:
                self._cover_map.clear()
            self._cover_map[tid] = uri

    def get_cover_url(self, track_id: str, size: str = "300x300") -> Optional[str]:
        """URL обложки. type=release на CDN работает; type=track — нет."""
        tid = str(track_id or "").strip()
        if not tid or not self.enabled:
            return None
        uri = self._cover_map.get(tid)
        if not uri:
            # Явно тянем release.image
            gql = """
            query getTracks($ids: [ID!]!) {
              getTracks(ids: $ids) {
                id
                image { src }
                release { id image { src } }
              }
            }
            """
            data = self._gql(gql, {"ids": [tid]}, "getTracks")
            if data:
                items = data.get("getTracks") or data.get("get_tracks") or []
                if items and isinstance(items[0], dict):
                    rel = items[0].get("release") or {}
                    img = (rel.get("image") if isinstance(rel, dict) else None) or items[0].get("image")
                    if isinstance(img, dict):
                        uri = img.get("src") or ""
                    elif isinstance(img, str):
                        uri = img
                    if uri:
                        self._remember_cover(tid, uri)
        if not uri:
            tracks = self.get_tracks_by_ids([tid])
            if tracks and tracks[0].cover_uri:
                uri = tracks[0].cover_uri
                self._remember_cover(tid, uri)
        if not uri:
            return None
        # type=track на CDN отклоняется — подменить на release если возможно
        if "type=track" in uri:
            # без release id не поможет; оставляем, download может упасть
            pass
        sz = size or "400x400"
        if "{size}" in uri:
            uri = uri.replace("{size}", sz)
        if uri.startswith("http"):
            return uri
        if uri.startswith("//"):
            return "https:" + uri
        return "https://" + uri.lstrip("/")

    def get_tracks_by_ids(self, ids: List[str]) -> List[ZvTrack]:
        if not ids or not self.enabled:
            return []
        # Tiny API
        res = self._tiny_get("tracks", {"ids": ",".join(str(i) for i in ids)})
        out: List[ZvTrack] = []
        if isinstance(res, dict):
            items = res.get("tracks") or res.get("items") or list(res.values())
        elif isinstance(res, list):
            items = res
        else:
            items = []
        for t in items:
            tr = self._track_from(t) if isinstance(t, dict) else None
            if tr:
                if tr.cover_uri:
                    self._remember_cover(tr.id, tr.cover_uri)
                out.append(tr)
        if out:
            return out
        # GraphQL fallback
        gql = """
        query getTracks($ids: [ID!]!) {
          getTracks(ids: $ids) {
            id title duration
            artists { id title }
            release { id title image { src } }
            image { src }
          }
        }
        """
        data = self._gql(gql, {"ids": [str(i) for i in ids]}, "getTracks")
        if not data:
            return []
        for t in data.get("getTracks") or data.get("get_tracks") or []:
            tr = self._track_from(t)
            if tr:
                if tr.cover_uri:
                    self._remember_cover(tr.id, tr.cover_uri)
                out.append(tr)
        return out

    # ---------- Search ----------

    def search_tracks(self, query: str, limit: int = 40) -> List[ZvTrack]:
        """Поиск треков: GraphQL search (как zvuk.com/search), fallback quickSearch."""
        q = (query or "").strip()
        if not q or not self.enabled:
            return []
        cache_key = f"tr:{q}:{limit}"
        ts, cached = self._search_cache.get(cache_key, (0.0, []))
        if time.time() - ts < self.cache_sec and cached:
            return list(cached)

        tracks: List[ZvTrack] = []
        lim = min(max(limit, 1), 50)

        # Основной поиск сайта
        gql = """
        query search($query: String!) {
          search(query: $query) {
            tracks(limit: %d) {
              items {
                id title duration
                artists { id title }
                release { id title image { src } }
                image { src }
              }
              page { total cursor }
            }
          }
        }
        """ % lim
        data = self._gql(gql, {"query": q}, "search")
        if data:
            items = (
                ((data.get("search") or {}).get("tracks") or {}).get("items") or []
            )
            for t in items:
                tr = self._track_from(t)
                if tr:
                    tracks.append(tr)

        # quickSearch — если search пустой
        if not tracks:
            gql_q = """
            query quickSearch($query: String!, $limit: Int) {
              quickSearch(query: $query, limit: $limit) {
                content {
                  ... on Track {
                    id title duration
                    artists { id title }
                    release { id title image { src } }
                    image { src }
                  }
                }
              }
            }
            """
            data = self._gql(gql_q, {"query": q, "limit": lim}, "quickSearch")
            if data:
                content = (
                    (data.get("quickSearch") or data.get("quick_search") or {}).get("content")
                    or []
                )
                for item in content:
                    if isinstance(item, dict) and item.get("id") and item.get("duration") is not None:
                        tr = self._track_from(item)
                        if tr:
                            tracks.append(tr)

        for tr in tracks:
            if tr.cover_uri:
                self._remember_cover(tr.id, tr.cover_uri)
        self._search_cache[cache_key] = (time.time(), tracks)
        return tracks

    def search_artists(self, query: str, limit: int = 20) -> List[dict]:
        """Поиск артистов. name+title для webui."""
        q = (query or "").strip()
        if not q or not self.enabled:
            return []
        out: List[dict] = []
        gql = """
        query search($query: String!) {
          search(query: $query) {
            artists(limit: %d) {
              items { id title image { src } }
            }
          }
        }
        """ % min(limit, 30)
        data = self._gql(gql, {"query": q}, "search")
        if data:
            items = ((data.get("search") or {}).get("artists") or {}).get("items") or []
            for a in items:
                if isinstance(a, dict) and a.get("id"):
                    name = (a.get("title") or a.get("name") or "").strip()
                    img = a.get("image") or {}
                    src = img.get("src") if isinstance(img, dict) else ""
                    out.append({
                        "id": str(a["id"]),
                        "name": name,
                        "title": name,
                        "cover": src or "",
                    })
        # quickSearch fallback
        if not out:
            gql_q = """
            query quickSearch($query: String!, $limit: Int) {
              quickSearch(query: $query, limit: $limit) {
                content { ... on Artist { id title image { src } } }
              }
            }
            """
            data = self._gql(gql_q, {"query": q, "limit": limit}, "quickSearch")
            if data:
                content = (
                    (data.get("quickSearch") or data.get("quick_search") or {}).get("content")
                    or []
                )
                for item in content:
                    if not isinstance(item, dict) or not item.get("id"):
                        continue
                    if item.get("duration") is not None:
                        continue
                    name = (item.get("title") or "").strip()
                    if not name:
                        continue
                    img = item.get("image") or {}
                    src = img.get("src") if isinstance(img, dict) else ""
                    out.append({
                        "id": str(item["id"]),
                        "name": name,
                        "title": name,
                        "cover": src or "",
                    })
        return out[:limit]

    def search_albums(self, query: str, limit: int = 20) -> List[dict]:
        q = (query or "").strip()
        if not q or not self.enabled:
            return []
        out: List[dict] = []
        gql = """
        query search($query: String!) {
          search(query: $query) {
            releases(limit: %d) {
              items {
                id title date
                artists { id title }
                image { src }
              }
            }
          }
        }
        """ % min(limit, 30)
        data = self._gql(gql, {"query": q}, "search")
        if data:
            items = ((data.get("search") or {}).get("releases") or {}).get("items") or []
            for r in items:
                if not isinstance(r, dict) or not r.get("id"):
                    continue
                anames = []
                for a in (r.get("artists") or []):
                    if isinstance(a, dict):
                        anames.append(a.get("title") or "")
                img = r.get("image") or {}
                src = img.get("src") if isinstance(img, dict) else ""
                out.append({
                    "id": str(r["id"]),
                    "title": r.get("title") or "",
                    "artist": ", ".join(n for n in anames if n),
                    "year": str(r.get("date") or "")[:4],
                    "track_count": 0,
                    "nb_tracks": 0,
                    "cover": src or "",
                })
        return out[:limit]

    def search_playlists(self, query: str, limit: int = 30) -> List[dict]:
        """Поиск плейлистов (жанры / подборки)."""
        q = (query or "").strip()
        if not q or not self.enabled:
            return []
        cache_key = f"pl:{q}:{limit}"
        ts, cached = self._search_cache.get(cache_key, (0.0, []))
        if time.time() - ts < self.cache_sec and cached:
            return list(cached)

        out: List[dict] = []
        gql = """
        query search($query: String!) {
          search(query: $query) {
            playlists(limit: %d) {
              items { id title description }
            }
          }
        }
        """ % min(limit, 40)
        data = self._gql(gql, {"query": q}, "search")
        if data:
            items = ((data.get("search") or {}).get("playlists") or {}).get("items") or []
            for pl in items:
                if not isinstance(pl, dict) or not pl.get("id"):
                    continue
                out.append({
                    "id": str(pl["id"]),
                    "title": (pl.get("title") or f"Playlist {pl['id']}").strip(),
                    "description": (pl.get("description") or "").strip(),
                    "track_count": int(pl.get("track_count") or 0),
                })
        self._search_cache[cache_key] = (time.time(), out)
        return out

    def list_genres(self) -> List[dict]:
        """Каталог жанров (zvuk.com/genres)."""
        return list(ZVUK_GENRES)

    # Ключевые слова для фильтрации выдачи search по жанру
    _GENRE_KEYWORDS: Dict[str, List[str]] = {
        "pop": ["поп", "pop", "тин-поп", "teen"],
        "rusrap": ["рэп", "rap", "хип-хоп", "hip-hop", "hiphop", "trap"],
        "rock": ["рок", "rock"],
        "shanson": ["шансон", "блатн"],
        "electronic": ["электрон", "electro", "techno", "house", "trance", "edm", "drum", "фонк", "phonk", "ambient"],
        "classical": ["классик", "classic", "opera", "опер", "симфон", "фортепиан"],
        "folk": ["фолк", "folk", "народн", "этно"],
        "dance": ["танцев", "dance", "клубн", "edm"],
        "jazz": ["джаз", "jazz"],
        "rnb": ["r&b", "rnb", "soul", "neo soul"],
        "metal": ["метал", "metal", "death", "black metal", "heavy", "ню-метал", "nu-metal", "nu metal"],
        "hiphop": ["хип-хоп", "hip-hop", "hiphop", "рэп", "rap", "trap"],
        "alternative": ["альтернатив", "alternative", "indie rock", "post-punk"],
        "blues": ["блюз", "blues"],
        "phonk": ["phonk", "фонк", "drift"],
        "indie": ["инди", "indie"],
        "live": ["live", "концерт", "live"],
        "world": ["world", "latin", "african", "asian", "ethnic", "этнич"],
    }

    def _playlist_matches_genre(self, title: str, genre_id: str, tab: str = "") -> bool:
        """Отсечь плейлисты, явно не относящиеся к жанру."""
        t = (title or "").lower()
        keys = self._GENRE_KEYWORDS.get(genre_id) or []
        tab_l = (tab or "").lower().strip()

        # вкладка «Новинки» / «Хиты» — название должно содержать жанр ИЛИ явный паттерн «Отборные новинки: X»
        if keys:
            has_genre = any(k in t for k in keys)
        else:
            # fallback: слова из title жанра
            genre = next((g for g in ZVUK_GENRES if g["id"] == genre_id), None)
            base = (genre.get("query") or genre.get("title") or "").lower() if genre else ""
            has_genre = bool(base) and (base in t or any(w in t for w in base.split() if len(w) > 2))

        if tab_l in ("новинки", "хиты"):
            # общие «Актуальные хиты» / «Топ 100» без жанра — выкидываем
            generic = (
                t in ("топ 100", "актуальные хиты", "главное")
                or t.startswith("хиты: русская")
                or t.startswith("майский отдых")
                or t.startswith("весенние новинки")
                or t.startswith("зимние новинки")
                or "русские хиты" in t and not has_genre
            )
            if generic and not has_genre:
                return False
            return has_genre

        # остальные вкладки: жанр или текст вкладки в названии
        if has_genre:
            return True
        if tab_l and len(tab_l) > 2 and tab_l in t:
            return True
        return False

    def get_genre_playlists(self, genre_id: str, tab: Optional[str] = None, limit: int = 30) -> List[dict]:
        """Плейлисты жанра/вкладки: несколько точных запросов + фильтр по названию."""
        genre = next((g for g in ZVUK_GENRES if g["id"] == genre_id), None)
        if not genre:
            return []
        base = (genre.get("query") or genre.get("title") or "").strip()
        title = (genre.get("title") or base).strip()
        tab = (tab or "").strip()
        tab_l = tab.lower()

        # Набор запросов от более точных к общим
        queries: List[str] = []
        if tab_l == "новинки":
            queries = [
                f"Отборные новинки: {title}",
                f"{title} | Новинки",
                f"{base} | Новинки",
                f"Новинки: {base}",
                f"{base} новинки",
            ]
            # англ. варианты для жанров с латиницей в каталоге
            if genre_id in ("metal", "jazz", "blues", "indie", "phonk", "rnb"):
                eng = {
                    "metal": "metal", "jazz": "jazz", "blues": "blues",
                    "indie": "indie", "phonk": "phonk", "rnb": "r&b",
                }.get(genre_id, base)
                queries.insert(1, f"{eng} | Новинки")
                queries.insert(2, f"Отборные новинки: {eng}")
        elif tab_l == "хиты":
            queries = [
                f"ГЛАВНОЕ: {title}",
                f"Топ-100: {base}",
                f"ХИТЫ: {base}",
                f"{base} хиты",
                title,
            ]
        elif tab:
            queries = [
                f"{base} {tab}",
                f"{title} {tab}",
                f"{tab} {base}",
                tab if len(tab) > 3 else f"{base} {tab}",
            ]
        else:
            queries = [base, title]

        seen: set = set()
        matched: List[dict] = []
        weak: List[dict] = []

        for q in queries:
            q = " ".join(q.split())
            if not q:
                continue
            try:
                items = self.search_playlists(q, limit=max(limit, 20))
            except Exception:
                items = []
            for pl in items:
                pid = str(pl.get("id") or "")
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                if self._playlist_matches_genre(pl.get("title") or "", genre_id, tab):
                    matched.append(pl)
                else:
                    weak.append(pl)
            if len(matched) >= limit:
                break

        out = matched[:limit]
        # Если «Новинки» пустые (нет редакционного плейлиста) — жанровые плейлисты без общего мусора
        if not out and tab_l in ("новинки", "хиты"):
            try:
                fallback = self.search_playlists(base or title, limit=max(limit, 20))
            except Exception:
                fallback = []
            for pl in fallback:
                pid = str(pl.get("id") or "")
                if not pid or pid in seen:
                    continue
                if self._playlist_matches_genre(pl.get("title") or "", genre_id, tab=""):
                    # для новичков/хитов без слова «новинки» в title — всё же жанр
                    out.append(pl)
                    seen.add(pid)
                if len(out) >= limit:
                    break
        elif tab_l not in ("новинки", "хиты") and len(out) < 3:
            for pl in weak:
                if pl not in out:
                    out.append(pl)
                if len(out) >= min(limit, 8):
                    break
        return out[:limit]

    # ---------- Collection / Playlists ----------

    def get_liked_tracks(self, limit: int = 500) -> List[ZvTrack]:
        if not self.enabled:
            return []
        ts, cached = self._liked_cache
        if time.time() - ts < self.cache_sec and cached:
            return list(cached)

        # Актуальная схема: collection.tracks — сразу список Track (не page/items)
        gql = """
        query {
          collection {
            tracks {
              id title duration
              artists { id title }
              release { id title image { src } }
              image { src }
            }
          }
        }
        """
        data = self._gql(gql)
        tracks: List[ZvTrack] = []
        if data:
            coll = data.get("collection") or {}
            items = coll.get("tracks") or []
            if isinstance(items, dict):
                items = items.get("items") or []
            for t in items[:limit]:
                tr = self._track_from(t)
                if tr:
                    if tr.cover_uri:
                        self._remember_cover(tr.id, tr.cover_uri)
                    tracks.append(tr)

        self._liked_cache = (time.time(), tracks)
        return tracks

    def get_user_playlists(self) -> List[ZvPlaylist]:
        if not self.enabled:
            return []
        ts, cached = self._playlists_cache
        if time.time() - ts < self.cache_sec and cached:
            return list(cached)

        gql = """
        query {
          collection {
            playlists {
              id title
            }
          }
        }
        """
        data = self._gql(gql)
        out: List[ZvPlaylist] = []
        if data:
            coll = data.get("collection") or {}
            items = coll.get("playlists") or []
            if isinstance(items, dict):
                items = items.get("items") or []
            for pl in items:
                if not isinstance(pl, dict) or not pl.get("id"):
                    continue
                out.append(ZvPlaylist(
                    id=str(pl["id"]),
                    title=(pl.get("title") or f"Playlist {pl['id']}").strip(),
                    track_count=int(pl.get("track_count") or 0),
                ))
        self._playlists_cache = (time.time(), out)
        return out

    def get_playlist_tracks(self, playlist_id: str) -> List[ZvTrack]:
        """Все треки плейлиста (пагинация playlistTracks, если их сотни)."""
        pid = str(playlist_id or "").strip()
        if not pid or not self.enabled:
            return []
        cached = self._playlist_cache.get(pid)
        if cached and time.time() - cached[0] < self.cache_sec:
            return list(cached[1])

        tracks: List[ZvTrack] = []
        seen: set = set()

        # 1) getPlaylists — часто отдаёт полный список (до ~100+)
        gql = """
        query getPlaylist($ids: [ID!]!) {
          getPlaylists(ids: $ids) {
            id title trackCount
            tracks {
              id title duration
              artists { id title }
              release { id title image { src } }
              image { src }
            }
          }
        }
        """
        data = self._gql(gql, {"ids": [pid]}, "getPlaylist")
        expected = 0
        if data:
            pls = data.get("getPlaylists") or data.get("get_playlists") or []
            if pls and isinstance(pls[0], dict):
                expected = int(pls[0].get("trackCount") or 0)
                for t in pls[0].get("tracks") or []:
                    tr = self._track_from(t)
                    if tr and tr.id not in seen:
                        seen.add(tr.id)
                        if tr.cover_uri:
                            self._remember_cover(tr.id, tr.cover_uri)
                        tracks.append(tr)

        # 2) Если trackCount больше или список подозрительно короткий — догружаем страницами
        need_more = (expected and len(tracks) < expected) or (not tracks)
        if need_more or len(tracks) >= 50:
            # даже если getPlaylists вернул 50, пробуем добрать хвост
            gql_page = """
            query playlistTracks($id: ID!, $limit: Int, $offset: Int) {
              playlistTracks(id: $id, limit: $limit, offset: $offset) {
                id title duration
                artists { id title }
                release { id title image { src } }
                image { src }
              }
            }
            """
            offset = 0
            page_size = 50
            max_tracks = 2000  # защита от бесконечного цикла
            while offset < max_tracks:
                data = self._gql(
                    gql_page,
                    {"id": pid, "limit": page_size, "offset": offset},
                    "playlistTracks",
                )
                items = (data or {}).get("playlistTracks") or (data or {}).get("playlist_tracks") or []
                if not items:
                    break
                for t in items:
                    tr = self._track_from(t)
                    if tr and tr.id not in seen:
                        seen.add(tr.id)
                        if tr.cover_uri:
                            self._remember_cover(tr.id, tr.cover_uri)
                        tracks.append(tr)
                if len(items) < page_size:
                    break
                offset += page_size
                # если уже набрали expected — можно остановиться
                if expected and len(tracks) >= expected:
                    break

        self._playlist_cache[pid] = (time.time(), tracks)
        return tracks

    def get_artist_tracks(self, artist_id: str, limit: int = 100) -> List[ZvTrack]:
        aid = str(artist_id or "").strip()
        if not aid or not self.enabled:
            return []
        cache_key = f"artist:{aid}"
        cached = self._tracks_cache.get(cache_key)
        if cached and time.time() - cached[0] < self.cache_sec:
            return list(cached[1])

        gql = """
        query getArtist($id: ID!) {
          getArtists(ids: [$id]) {
            id title
            popularTracks {
              id title duration
              artists { id title }
              release { id title image { src } }
              image { src }
            }
          }
        }
        """
        data = self._gql(gql, {"id": aid}, "getArtist")
        tracks: List[ZvTrack] = []
        if data:
            artists = data.get("getArtists") or data.get("get_artists") or []
            if artists and isinstance(artists[0], dict):
                for t in (artists[0].get("popularTracks") or artists[0].get("popular_tracks") or [])[:limit]:
                    tr = self._track_from(t)
                    if tr:
                        tracks.append(tr)

        self._tracks_cache[cache_key] = (time.time(), tracks)
        return tracks

    def get_release_tracks(self, release_id: str) -> List[ZvTrack]:
        rid = str(release_id or "").strip()
        if not rid or not self.enabled:
            return []
        cache_key = f"release:{rid}"
        cached = self._tracks_cache.get(cache_key)
        if cached and time.time() - cached[0] < self.cache_sec:
            return list(cached[1])

        gql = """
        query getRelease($id: ID!) {
          getReleases(ids: [$id]) {
            id title
            tracks {
              id title duration position
              artists { id title }
              release { id title image { src } }
              image { src }
            }
          }
        }
        """
        data = self._gql(gql, {"id": rid}, "getRelease")
        tracks: List[ZvTrack] = []
        if data:
            releases = data.get("getReleases") or data.get("get_releases") or []
            if releases and isinstance(releases[0], dict):
                for t in releases[0].get("tracks") or []:
                    tr = self._track_from(t)
                    if tr:
                        tracks.append(tr)

        self._tracks_cache[cache_key] = (time.time(), tracks)
        return tracks

    # ---------- Streaming ----------

    def _resolve_quality(self) -> str:
        q = QUALITY_MAP.get(self.quality, "high")
        if q == "flac" and not self._can_flac and not self._is_anonymous:
            # попробуем flac, API сам откажет если нет подписки
            return "flac"
        if self._is_anonymous:
            return "mid"
        return q



    def list_editorial_profiles(self) -> List[dict]:
        """Редакционные профили (подборки редакции)."""
        return list(ZVUK_EDITORIAL_PROFILES)

    def get_profile_playlists(self, profile_id: str, limit: int = 100) -> List[dict]:
        """Плейлисты публичного профиля (getProfiles.playlists + titles)."""
        pid = str(profile_id or "").strip()
        if not pid or not self.enabled:
            return []
        cache_key = f"prof:{pid}"
        ts, cached = self._search_cache.get(cache_key, (0.0, []))
        if time.time() - ts < self.cache_sec and cached:
            return list(cached)[:limit]

        gql = """
        query getProfiles($ids: [ID!]!) {
          getProfiles(ids: $ids) {
            id name
            playlists { id }
          }
        }
        """
        data = self._gql(gql, {"ids": [pid]}, "getProfiles")
        ids: List[str] = []
        if data:
            profiles = data.get("getProfiles") or data.get("get_profiles") or []
            if profiles and isinstance(profiles[0], dict):
                for pl in profiles[0].get("playlists") or []:
                    if isinstance(pl, dict) and pl.get("id"):
                        ids.append(str(pl["id"]))

        out: List[dict] = []
        if ids:
            title_map: Dict[str, str] = {}
            gql_pl = """
            query getPlaylists($ids: [ID!]!) {
              getPlaylists(ids: $ids) { id title }
            }
            """
            for i in range(0, len(ids), 25):
                batch = ids[i:i + 25]
                data_pl = self._gql(gql_pl, {"ids": batch}, "getPlaylists")
                if data_pl:
                    for pl in data_pl.get("getPlaylists") or data_pl.get("get_playlists") or []:
                        if isinstance(pl, dict) and pl.get("id"):
                            title_map[str(pl["id"])] = (pl.get("title") or pl["id"]).strip()
            for iid in ids:
                out.append({
                    "id": iid,
                    "title": title_map.get(iid, f"Playlist {iid}"),
                    "track_count": 0,
                })

        self._search_cache[cache_key] = (time.time(), out)
        return out[:limit]

    def list_curated_sections(self) -> List[dict]:
        """Разделы подборок (популярное + discovery)."""
        return list(ZVUK_CURATED)

    def get_grid_playlists(self, grid_name: str, limit: int = 60) -> List[dict]:
        """Плейлисты из /api/tiny/grid/content?name=..."""
        name = (grid_name or "").strip()
        if not name or not self.enabled:
            return []
        cache_key = f"grid:{name}"
        ts, cached = self._search_cache.get(cache_key, (0.0, []))
        if time.time() - ts < self.cache_sec and cached:
            return list(cached)

        res = self._tiny_get("grid/content", {"name": name, "ranker_enabled": "true"})
        data = []
        if isinstance(res, dict):
            page = res.get("page") or res
            data = page.get("data") or res.get("data") or []
        ids = []
        for it in data:
            if isinstance(it, dict) and (it.get("type") == "playlist" or not it.get("type")):
                iid = it.get("id")
                if iid:
                    ids.append(str(iid))
        out: List[dict] = []
        if ids:
            # titles via GraphQL
            gql = """
            query getPlaylists($ids: [ID!]!) {
              getPlaylists(ids: $ids) { id title }
            }
            """
            # batch 25
            title_map = {}
            for i in range(0, len(ids), 25):
                batch = ids[i:i+25]
                data_g = self._gql(gql, {"ids": batch}, "getPlaylists")
                if data_g:
                    for pl in data_g.get("getPlaylists") or data_g.get("get_playlists") or []:
                        if isinstance(pl, dict) and pl.get("id"):
                            title_map[str(pl["id"])] = (pl.get("title") or pl["id"]).strip()
            for pid in ids[:limit]:
                out.append({
                    "id": pid,
                    "title": title_map.get(pid, f"Playlist {pid}"),
                    "track_count": 0,
                })
        self._search_cache[cache_key] = (time.time(), out)
        return out

    def get_curated_playlists(self, section_id: str, limit: int = 40) -> List[dict]:
        """Плейлисты раздела подборок: grid / editorial profile / search."""
        # Прямой profile_id из ZVUK_EDITORIAL_PROFILES
        if section_id.startswith("profile:"):
            return self.get_profile_playlists(section_id.split(":", 1)[1], limit=limit)
        for ep in ZVUK_EDITORIAL_PROFILES:
            if ep["id"] == section_id:
                return self.get_profile_playlists(ep["id"], limit=limit)

        sec = next((s for s in ZVUK_CURATED if s["id"] == section_id), None)
        if not sec:
            return []
        if sec.get("source") == "editorial":
            # Список профилей как «псевдо-плейлисты» не подходит —
            # контент-дерево раскрывает их отдельно.
            return []
        if sec.get("source") == "grid":
            items = self.get_grid_playlists(sec.get("grid") or section_id, limit=limit)
            if items:
                return items
            return self.search_playlists(sec.get("title") or section_id, limit=limit)
        q = sec.get("query") or sec.get("title") or section_id
        return self.search_playlists(q, limit=limit)

    def get_stream_url(self, track_id: str) -> Optional[Tuple[str, str]]:
        """Вернуть (url, mime) или None."""
        tid = str(track_id or "").strip()
        if not tid or not self.enabled:
            return None
        quality = self._resolve_quality()

        # GraphQL getTracks.stream — работает без anti-bot 418
        gql = """
        query getTracks($ids: [ID!]!) {
          getTracks(ids: $ids) {
            id
            stream { mid high flac }
          }
        }
        """
        data = self._gql(gql, {"ids": [tid]}, "getTracks")
        if data:
            tracks = data.get("getTracks") or data.get("get_tracks") or []
            if tracks and isinstance(tracks[0], dict):
                stream = tracks[0].get("stream") or {}
                order = []
                if quality == "flac":
                    order = ["flac", "high", "mid"]
                elif quality == "mid":
                    order = ["mid", "high"]
                else:
                    order = ["high", "mid", "flac"]
                for key in order:
                    url = stream.get(key)
                    if url:
                        mime = "audio/flac" if key == "flac" else "audio/mpeg"
                        return str(url), mime

        # Tiny direct stream (может отдать 418 с некоторых IP)
        for q in (quality, "high", "mid"):
            res = self._tiny_get("track/stream", {"id": tid, "quality": q})
            if not res:
                continue
            url = res.get("stream") or res.get("url") or res.get("src")
            if url:
                mime = "audio/flac" if q == "flac" else "audio/mpeg"
                return str(url), mime
        return None

    def open_stream(self, track_id: str):
        """Открыть HTTP-поток: (response, mime, content_length) или None."""
        result = self.get_stream_url(track_id)
        if not result:
            return None
        url, mime = result
        try:
            r = self.session.get(url, stream=True, timeout=30)
            if r.status_code >= 400:
                log.warning("Zvuk stream HTTP %s for %s", r.status_code, track_id)
                r.close()
                return None
            ctype = (r.headers.get("Content-Type") or mime or "audio/mpeg").split(";")[0].strip()
            if "octet-stream" in ctype:
                ctype = mime or "audio/mpeg"
            clen = r.headers.get("Content-Length")
            return r, ctype, clen
        except Exception as e:
            log.warning("Zvuk open_stream %s: %s", track_id, e)
            return None
