"""Deezer через ARL: плейлисты аккаунта + URL потока для прокси."""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import requests

log = logging.getLogger("dlna_emu.deezer")

GW = "https://www.deezer.com/ajax/gw-light.php"
API = "https://api.deezer.com"
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

QUALITY_MAP = {
    "mp3_128": "MP3_128",
    "mp3_320": "MP3_320",
    "flac": "FLAC",
}

# ----- Flow (moods / genres) -----
# Партия к Deezer: 3 трека. В каталоге ресивера — 100 слотов (см. content_ids.FLOW_MAX_CACHE).
FLOW_BATCH = 3
FLOW_MAX_CACHE = 100       # совпадает с content_ids.FLOW_MAX_CACHE (слоты плейлиста)
FLOW_TREE_MAX_CACHE = 100
FLOW_MIN_AHEAD = 3
FLOW_TTL = 7200            # 2 ч — слоты привязаны к позиции, не сбрасывать часто
FLOW_STALE_SEC = 7200.0    # idle-сброс только после долгого простоя (иначе индексы «плывут»)


@dataclass
class DzTrack:
    id: str
    title: str
    artist: str
    duration: int = 0
    album: str = ""


@dataclass
class DzPlaylist:
    id: str
    title: str
    track_count: int = 0
    tracks: List[DzTrack] = field(default_factory=list)


class DeezerClient:
    def __init__(self, arl: str, quality: str = "mp3_128", cache_sec: int = 300):
        self.arl = (arl or "").strip()
        self.quality = "auto"
        self.cache_sec = cache_sec
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9"})
        if self.arl:
            self.session.cookies.set("arl", self.arl, domain=".deezer.com")
        self._api_token: Optional[str] = None
        self._user_id: Optional[str] = None
        self._user_name: Optional[str] = None
        self._playlists_cache: Tuple[float, List[DzPlaylist]] = (0.0, [])
        self._tracks_cache: Dict[str, Tuple[float, List[DzTrack]]] = {}

        # Flow-кэш (отдельный список на каждый genre/mood)
        self._flow_cache: Dict[str, List] = {}
        self._flow_cache_ts: Dict[str, float] = {}
        self._flow_last_access: Dict[str, float] = {}
        self._flow_playing_key: Optional[str] = None  # что сейчас играет (не browse)
        self.flow_ttl = FLOW_TTL

        # Чёрный список недоступных треков — заполняется при 2002/CDN fail
        self._unavailable: set = set()
        self._cdn_fails: Dict[str, int] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.arl)

    def _gw(self, method: str, payload: Optional[dict] = None, api_token: str = "") -> dict:
        params = {
            "method": method,
            "input": "3",
            "api_version": "1.0",
            "api_token": api_token or self._api_token or "",
        }
        r = self.session.post(GW, params=params, json=payload or {}, timeout=25)
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            log.warning("gw %s error: %s", method, data.get("error"))
        return data

    def login(self) -> bool:
        if not self.arl:
            log.info("Deezer ARL not set — Deezer folder disabled")
            return False
        try:
            data = self._gw("deezer.getUserData", api_token="")
            results = data.get("results") or {}
            self._api_token = results.get("checkForm") or results.get("checkFormLogin") or ""
            user = results.get("USER") or {}
            self._user_id = str(user.get("USER_ID") or results.get("USER_ID") or "")
            self._user_name = (
                user.get("BLOG_NAME")
                or user.get("USER_NAME")
                or (user.get("FIRSTNAME") or "")
                or "Deezer"
            )
            opts = user.get("OPTIONS") or {}
            self._license_token = opts.get("license_token") or ""
            self._license_ts = time.time()
            if self._license_token:
                log.info("license_token OK (len=%d)", len(self._license_token))
            else:
                log.warning("license_token missing in getUserData")
            if not self._api_token or not self._user_id or self._user_id == "0":
                log.error("Deezer login failed — check ARL")
                return False
            log.info("Deezer login OK user=%s id=%s", self._user_name, self._user_id)
            return True
        except Exception as e:
            log.error("Deezer login: %s", e)
            return False

    def ensure_login(self) -> bool:
        if self._api_token and self._user_id:
            return True
        return self.login()

    # ========================================================================
    #  Плейлисты / треки плейлистов
    # ========================================================================

    def get_playlists(self) -> List[DzPlaylist]:
        if not self.ensure_login():
            return []
        now = time.time()
        ts, cached = self._playlists_cache
        if cached and now - ts < self.cache_sec:
            return cached

        playlists: List[DzPlaylist] = []
        try:
            data = self._gw(
                "deezer.pageProfile",
                {"user_id": self._user_id, "tab": "playlists", "nb": 100},
            )
            results = data.get("results") or {}
            tab = results.get("TAB") or results.get("playlists") or {}
            items = tab.get("data") or results.get("data") or []
            if isinstance(tab, dict) and "data" in tab:
                items = tab["data"]
            for it in items:
                pid = str(it.get("PLAYLIST_ID") or it.get("id") or "")
                title = (it.get("TITLE") or it.get("title") or "Playlist").strip()
                nb = int(it.get("NB_SONG") or it.get("nb_tracks") or 0)
                if pid:
                    playlists.append(DzPlaylist(id=pid, title=title, track_count=nb))
        except Exception as e:
            log.warning("pageProfile playlists: %s", e)

        if not playlists and self._user_id:
            try:
                r = self.session.get(
                    f"{API}/user/{self._user_id}/playlists",
                    params={"limit": 100},
                    timeout=20,
                )
                for it in (r.json().get("data") or []):
                    playlists.append(
                        DzPlaylist(
                            id=str(it.get("id")),
                            title=(it.get("title") or "Playlist").strip(),
                            track_count=int(it.get("nb_tracks") or 0),
                        )
                    )
            except Exception as e:
                log.warning("api playlists: %s", e)

        playlists.insert(
            0,
            DzPlaylist(id="0", title="❤ Любимые треки", track_count=0),
        )
        self._playlists_cache = (now, playlists)
        log.info("Deezer playlists: %d", len(playlists))
        return playlists

    def get_tracks(self, playlist_id: str) -> List[DzTrack]:
        if not self.ensure_login():
            return []
        now = time.time()
        cached = self._tracks_cache.get(playlist_id)
        if cached and now - cached[0] < self.cache_sec:
            return cached[1]

        tracks: List[DzTrack] = []
        try:
            if playlist_id == "0":
                data = self._gw(
                    "deezer.pageProfile",
                    {"user_id": self._user_id, "tab": "loved", "nb": 200},
                )
                results = data.get("results") or {}
                tab = results.get("TAB") or {}
                items = tab.get("data") or []
            else:
                data = self._gw(
                    "deezer.pagePlaylist",
                    {"playlist_id": playlist_id, "nb": 500},
                )
                results = data.get("results") or {}
                songs = results.get("SONGS") or results.get("songs") or {}
                items = songs.get("data") or results.get("data") or []

            for it in items:
                tid = str(it.get("SNG_ID") or it.get("id") or "")
                if not tid:
                    continue
                title = (it.get("SNG_TITLE") or it.get("title") or "").strip()
                artist = (
                    it.get("ART_NAME")
                    or (it.get("artist") or {}).get("name")
                    or ""
                )
                if isinstance(artist, dict):
                    artist = artist.get("name") or ""
                artist = str(artist).strip()
                duration = int(it.get("DURATION") or it.get("duration") or 0)
                album = (it.get("ALB_TITLE") or it.get("album", {}).get("title") or "")
                if isinstance(album, dict):
                    album = album.get("title") or ""
                tracks.append(
                    DzTrack(
                        id=tid,
                        title=title or tid,
                        artist=str(artist),
                        duration=duration,
                        album=str(album),
                    )
                )
        except Exception as e:
            log.warning("get_tracks %s: %s", playlist_id, e)

        if not tracks and playlist_id != "0":
            try:
                index = 0
                while True:
                    r = self.session.get(
                        f"{API}/playlist/{playlist_id}/tracks",
                        params={"limit": 100, "index": index},
                        timeout=20,
                    )
                    data = r.json()
                    batch = data.get("data") or []
                    if not batch:
                        break
                    for it in batch:
                        artist = it.get("artist") or {}
                        tracks.append(
                            DzTrack(
                                id=str(it.get("id")),
                                title=(it.get("title") or "").strip(),
                                artist=(artist.get("name") or "").strip(),
                                duration=int(it.get("duration") or 0),
                                album=(it.get("album") or {}).get("title") or "",
                            )
                        )
                    if len(batch) < 100:
                        break
                    index += 100
            except Exception as e:
                log.warning("api tracks: %s", e)

        if self._unavailable:
            before = len(tracks)
            tracks = [t for t in tracks if t.id not in self._unavailable]
            if before != len(tracks):
                log.info(
                    "playlist %s: скрыто недоступных: %d",
                    playlist_id, before - len(tracks),
                )

        self._tracks_cache[playlist_id] = (now, tracks)
        log.info("Deezer playlist %s tracks: %d", playlist_id, len(tracks))
        return tracks

    # ========================================================================
    #  Поиск треков
    # ========================================================================

    def search_tracks(self, query: str, limit: int = 100) -> List[DzTrack]:
        q = (query or "").strip()
        if not q:
            return []
        tracks: List[DzTrack] = []
        seen: set = set()

        if self.ensure_login():
            try:
                data = self._gw("search.music", {
                    "query": q, "type": "track", "nb": limit, "start": 0,
                })
                results = data.get("results") or {}
                items = (
                    results.get("TRACK") or
                    results.get("track") or
                    results.get("data") or
                    []
                )
                if isinstance(items, dict):
                    items = items.get("data") or []
                for it in items[:limit]:
                    tid = str(it.get("SNG_ID") or it.get("id") or "")
                    if not tid or tid in seen:
                        continue
                    seen.add(tid)
                    artist = it.get("ART_NAME") or ""
                    if not artist and isinstance(it.get("artist"), dict):
                        artist = it["artist"].get("name") or ""
                    album = it.get("ALB_TITLE") or ""
                    if not album and isinstance(it.get("album"), dict):
                        album = it["album"].get("title") or ""
                    tracks.append(DzTrack(
                        id=tid,
                        title=(it.get("SNG_TITLE") or it.get("title") or "").strip(),
                        artist=str(artist).strip(),
                        duration=int(it.get("DURATION") or it.get("duration") or 0),
                        album=str(album).strip(),
                    ))
            except Exception as e:
                log.warning("search gw: %s", e)

        if not tracks:
            try:
                r = self.session.get(
                    f"{API}/search",
                    params={"q": q, "limit": min(limit, 100)},
                    timeout=20,
                )
                for it in (r.json().get("data") or []):
                    tid = str(it.get("id") or "")
                    if not tid or tid in seen:
                        continue
                    seen.add(tid)
                    tracks.append(DzTrack(
                        id=tid,
                        title=(it.get("title") or "").strip(),
                        artist=((it.get("artist") or {}).get("name") or "").strip(),
                        duration=int(it.get("duration") or 0),
                        album=((it.get("album") or {}).get("title") or "").strip(),
                    ))
            except Exception as e:
                log.warning("search api: %s", e)

        if self._unavailable:
            tracks = [t for t in tracks if t.id not in self._unavailable]
        log.info("Deezer search %r: %d tracks", q, len(tracks))
        return tracks

    # ========================================================================
    #  Поиск артистов / альбомов
    # ========================================================================

    def search_artists(self, query: str, limit: int = 20) -> List[dict]:
        q = (query or "").strip()
        if not q:
            return []
        out: List[dict] = []
        seen: set = set()

        # 1) gw-light — тот же путь, что и search_tracks; работает с ARL.
        if self.ensure_login():
            try:
                data = self._gw("search.music", {
                    "query": q, "type": "artist", "nb": limit, "start": 0,
                })
                results = data.get("results") or {}
                items = (
                    results.get("ARTIST") or
                    results.get("artist") or
                    results.get("data") or
                    []
                )
                if isinstance(items, dict):
                    items = items.get("data") or []
                for it in items[:limit]:
                    aid = str(it.get("ART_ID") or it.get("id") or "")
                    name = (it.get("ART_NAME") or it.get("name") or "").strip()
                    if not aid or not name or aid in seen:
                        continue
                    seen.add(aid)
                    out.append({
                        "id": aid,
                        "name": name,
                        "nb_album": int(it.get("NB_ALBUM") or it.get("nb_album") or 0),
                        "picture": it.get("ART_PICTURE") or it.get("picture_medium") or "",
                    })
            except Exception as e:
                log.warning("search_artists gw: %s", e)

        # 2) публичный api как fallback / дополнение
        if not out:
            try:
                r = self.session.get(
                    f"{API}/search/artist",
                    params={"q": q, "limit": min(limit, 50)},
                    timeout=20,
                )
                if r.status_code != 200:
                    log.warning(
                        "search_artists api HTTP %s: %s",
                        r.status_code, (r.text or "")[:200],
                    )
                else:
                    for it in (r.json().get("data") or []):
                        aid = str(it.get("id") or "")
                        if not aid or aid in seen:
                            continue
                        seen.add(aid)
                        out.append({
                            "id": aid,
                            "name": (it.get("name") or "").strip(),
                            "nb_album": int(it.get("nb_album") or 0),
                            "picture": it.get("picture_medium") or "",
                        })
            except Exception as e:
                log.warning("search_artists api: %s", e)

        log.info("Deezer search artists %r: %d", q, len(out))
        return out

    def search_albums(self, query: str, limit: int = 20) -> List[dict]:
        q = (query or "").strip()
        if not q:
            return []
        out: List[dict] = []
        seen: set = set()

        # 1) gw-light
        if self.ensure_login():
            try:
                data = self._gw("search.music", {
                    "query": q, "type": "album", "nb": limit, "start": 0,
                })
                results = data.get("results") or {}
                items = (
                    results.get("ALBUM") or
                    results.get("album") or
                    results.get("data") or
                    []
                )
                if isinstance(items, dict):
                    items = items.get("data") or []
                for it in items[:limit]:
                    aid = str(it.get("ALB_ID") or it.get("id") or "")
                    title = (it.get("ALB_TITLE") or it.get("title") or "").strip()
                    if not aid or not title or aid in seen:
                        continue
                    seen.add(aid)
                    artist = (
                        it.get("ART_NAME")
                        or (it.get("artist") or {}).get("name")
                        or ""
                    )
                    if isinstance(artist, dict):
                        artist = artist.get("name") or ""
                    out.append({
                        "id": aid,
                        "title": title,
                        "artist": str(artist).strip(),
                        "nb_tracks": int(it.get("NB_TRACK") or it.get("nb_tracks") or 0),
                        "cover": it.get("ALB_PICTURE") or it.get("cover_medium") or "",
                    })
            except Exception as e:
                log.warning("search_albums gw: %s", e)

        # 2) публичный api
        if not out:
            try:
                r = self.session.get(
                    f"{API}/search/album",
                    params={"q": q, "limit": min(limit, 50)},
                    timeout=20,
                )
                if r.status_code != 200:
                    log.warning(
                        "search_albums api HTTP %s: %s",
                        r.status_code, (r.text or "")[:200],
                    )
                else:
                    for it in (r.json().get("data") or []):
                        aid = str(it.get("id") or "")
                        if not aid or aid in seen:
                            continue
                        seen.add(aid)
                        out.append({
                            "id": aid,
                            "title": (it.get("title") or "").strip(),
                            "artist": ((it.get("artist") or {}).get("name") or "").strip(),
                            "nb_tracks": int(it.get("nb_tracks") or 0),
                            "cover": it.get("cover_medium") or "",
                        })
            except Exception as e:
                log.warning("search_albums api: %s", e)

        log.info("Deezer search albums %r: %d", q, len(out))
        return out

    # ========================================================================
    #  Артист / альбом
    # ========================================================================

    def get_artist_albums(self, artist_id: str, limit: int = 50) -> List[dict]:
        aid = str(artist_id or "").strip()
        if not aid:
            return []
        out: List[dict] = []
        seen: set = set()
        index = 0
        while len(out) < limit:
            try:
                r = self.session.get(
                    f"{API}/artist/{aid}/albums",
                    params={"limit": min(100, limit - len(out)), "index": index},
                    timeout=20,
                )
                batch = (r.json().get("data") or [])
            except Exception as e:
                log.warning("get_artist_albums %s: %s", aid, e)
                break
            if not batch:
                break
            for it in batch:
                alb_id = str(it.get("id") or "")
                if not alb_id or alb_id in seen:
                    continue
                seen.add(alb_id)
                out.append({
                    "id": alb_id,
                    "title": (it.get("title") or "").strip(),
                    "release_date": it.get("release_date") or "",
                    "nb_tracks": int(it.get("nb_tracks") or 0),
                    "cover": it.get("cover_medium") or "",
                })
            if len(batch) < 100:
                break
            index += len(batch)
        log.info("artist %s albums: %d", aid, len(out))
        return out

    def get_artist_top_tracks(self, artist_id: str, limit: int = 50) -> List[DzTrack]:
        aid = str(artist_id or "").strip()
        if not aid:
            return []
        tracks: List[DzTrack] = []
        try:
            r = self.session.get(
                f"{API}/artist/{aid}/top",
                params={"limit": min(limit, 100)},
                timeout=20,
            )
            for it in (r.json().get("data") or []):
                tid = str(it.get("id") or "")
                if not tid:
                    continue
                tracks.append(DzTrack(
                    id=tid,
                    title=(it.get("title") or "").strip(),
                    artist=((it.get("artist") or {}).get("name") or "").strip(),
                    duration=int(it.get("duration") or 0),
                    album=((it.get("album") or {}).get("title") or "").strip(),
                ))
        except Exception as e:
            log.warning("get_artist_top_tracks %s: %s", aid, e)
        if self._unavailable:
            tracks = [t for t in tracks if t.id not in self._unavailable]
        log.info("artist %s top tracks: %d", aid, len(tracks))
        return tracks

    def get_album_tracks(self, album_id: str) -> List[DzTrack]:
        aid = str(album_id or "").strip()
        if not aid:
            return []
        tracks: List[DzTrack] = []
        try:
            r = self.session.get(f"{API}/album/{aid}", timeout=20)
            data = r.json() or {}
            album_title = (data.get("title") or "").strip()
            for it in (data.get("tracks", {}).get("data") or []):
                tid = str(it.get("id") or "")
                if not tid:
                    continue
                tracks.append(DzTrack(
                    id=tid,
                    title=(it.get("title") or "").strip(),
                    artist=((it.get("artist") or {}).get("name") or "").strip(),
                    duration=int(it.get("duration") or 0),
                    album=album_title,
                ))
        except Exception as e:
            log.warning("get_album_tracks %s: %s", aid, e)
        if self._unavailable:
            tracks = [t for t in tracks if t.id not in self._unavailable]
        log.info("album %s tracks: %d", aid, len(tracks))
        return tracks

    # ========================================================================
    #  Flow (moods / genres)
    # ========================================================================

    def _auth_jwt(self) -> Optional[str]:
        try:
            r = self.session.post(
                "https://auth.deezer.com/login/renew?jo=p&rto=c&i=c",
                timeout=15,
            )
            return (r.json() or {}).get("jwt") or (r.json() or {}).get("access_token")
        except Exception as e:
            log.debug("jwt: %s", e)
            return None

    def _flow_configs(self) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
        """Настроения и жанры Flow. ID — как в web-плеере (с дефисами)."""
        moods: List[Dict[str, str]] = []
        genres: List[Dict[str, str]] = []
        jwt = self._auth_jwt()
        if jwt:
            q = """
            { me { flowConfigs {
              moods { edges { node { id title } } }
              genres { edges { node { id title } } }
            } } }
            """
            try:
                r = self.session.post(
                    "https://pipe.deezer.com/api",
                    headers={
                        "Authorization": f"Bearer {jwt}",
                        "Content-Type": "application/json",
                    },
                    json={"query": q},
                    timeout=20,
                )
                data = (r.json() or {}).get("data") or {}
                fc = (data.get("me") or {}).get("flowConfigs") or {}
                for e in ((fc.get("moods") or {}).get("edges") or []):
                    n = e.get("node") or {}
                    if n.get("id"):
                        moods.append({"id": str(n["id"]), "title": (n.get("title") or n["id"]).strip()})
                for e in ((fc.get("genres") or {}).get("edges") or []):
                    n = e.get("node") or {}
                    if n.get("id"):
                        genres.append({"id": str(n["id"]), "title": (n.get("title") or n["id"]).strip()})
            except Exception as e:
                log.warning("flowConfigs GraphQL: %s", e)

        if not moods:
            moods = [
                {"id": "",                   "title": "Flow"},
                {"id": "mood-happy",         "title": "Радостное"},
                {"id": "mood-motivational",  "title": "Тренировка"},
                {"id": "mood-chill",         "title": "Чиллаут"},
                {"id": "mood-discovery",     "title": "Находка"},
                {"id": "mood-party",         "title": "Вечеринка"},
                {"id": "mood-focus",         "title": "Концентрация"},
                {"id": "mood-melancholy",    "title": "Грустное"},
                {"id": "mood-love",          "title": "Любовь"},
            ]
        else:
            if not any(m.get("id") in ("", "default", None) for m in moods):
                moods.insert(0, {"id": "", "title": "Flow"})

        if not genres:
            genres = [
                {"id": "",                   "title": "Flow"},
                {"id": "discovery",          "title": "Находка"},
                {"id": "genre-danceedm",     "title": "Танцпол/EDM"},
                {"id": "genre-rap",          "title": "Рэп"},
                {"id": "genre-electronic",   "title": "Электронная"},
                {"id": "genre-house",        "title": "Хаус"},
                {"id": "genre-metal",        "title": "Метал"},
                {"id": "genre-rock",         "title": "Рок"},
                {"id": "genre-pop",          "title": "Поп"},
            ]
        return moods, genres

    def get_flow_tracks(
        self,
        config_id: str = "",
        limit: int = FLOW_BATCH,
        *,
        append: bool = True,
        max_cache: int = FLOW_MAX_CACHE,
    ) -> List[DzTrack]:
        """Одна порция flow-треков с автосбросом по idle > 60 сек и TTL 30 мин."""
        if not self.ensure_login() or not self._user_id:
            return []

        key = config_id or "default"
        now = time.time()

        if not hasattr(self, "_flow_cache"):
            self._flow_cache = {}
        if not hasattr(self, "_flow_cache_ts"):
            self._flow_cache_ts = {}
        if not hasattr(self, "_flow_last_access"):
            self._flow_last_access = {}
        # Idle-триггер
        last_access = self._flow_last_access.get(key, 0.0)
        if self._flow_cache.get(key) and last_access:
            idle = now - last_access
            if idle > FLOW_STALE_SEC:
                log.info(
                    "Flow config=%r idle %.0fs > %ds — resetting",
                    key, idle, FLOW_STALE_SEC,
                )
                self._flow_cache.pop(key, None)
                self._flow_cache_ts.pop(key, None)

        # TTL-триггер
        ts = self._flow_cache_ts.get(key, 0.0)
        if self._flow_cache.get(key) and (now - ts) > self.flow_ttl:
            log.info(
                "Flow config=%r TTL expired (%.0fs) — resetting",
                key, now - ts,
            )
            self._flow_cache.pop(key, None)
            self._flow_cache_ts.pop(key, None)

        self._flow_last_access[key] = now

        def _fetch_batch() -> List[DzTrack]:
            batch: List[DzTrack] = []
            payload: dict = {"user_id": self._user_id}
            if config_id:
                payload["config_id"] = config_id
            try:
                data = self._gw("radio.getUserRadio", payload)
                results = data.get("results") or {}
                items = results if isinstance(results, list) else (results.get("data") or [])
                for it in items[:limit]:
                    tid = str(it.get("SNG_ID") or it.get("id") or "")
                    if not tid:
                        continue
                    title = (it.get("SNG_TITLE") or it.get("title") or "").strip()
                    artist = (it.get("ART_NAME") or "").strip()
                    if not artist and isinstance(it.get("artist"), dict):
                        artist = (it["artist"].get("name") or "").strip()
                    duration = int(it.get("DURATION") or it.get("duration") or 0)
                    album = it.get("ALB_TITLE") or ""
                    if isinstance(album, dict):
                        album = album.get("title") or ""
                    batch.append(DzTrack(
                        id=tid, title=title or tid, artist=str(artist),
                        duration=duration, album=str(album),
                    ))
            except Exception as e:
                log.warning("Flow config=%r: %s", config_id, e)

            if not batch and not config_id:
                try:
                    r = self.session.get(
                        f"{API}/user/{self._user_id}/flow",
                        params={"limit": limit}, timeout=20,
                    )
                    for it in (r.json().get("data") or []):
                        artist = it.get("artist") or {}
                        batch.append(DzTrack(
                            id=str(it.get("id")),
                            title=(it.get("title") or "").strip(),
                            artist=(artist.get("name") or "").strip(),
                            duration=int(it.get("duration") or 0),
                            album=(it.get("album") or {}).get("title") or "",
                        ))
                except Exception as e:
                    log.warning("Flow API: %s", e)
            return batch

        existing = list(self._flow_cache.get(key) or [])
        seen = {t.id for t in existing}

        if not existing or append:
            # Не режем начало кэша: индексы слотов (0..99) должны оставаться стабильными
            # для ресиверов, которые один раз прочитали каталог и дальше идут по списку.
            if len(existing) >= max_cache:
                log.info(
                    "Deezer Flow config=%r cache=%d (full, no fetch)",
                    key, len(existing),
                )
            else:
                batch = _fetch_batch()
                added = 0
                for t in batch:
                    if len(existing) >= max_cache:
                        break
                    if t.id not in seen:
                        existing.append(t)
                        seen.add(t.id)
                        added += 1
                self._flow_cache[key] = existing
                self._flow_cache_ts[key] = time.time()
                log.info(
                    "Deezer Flow config=%r cache=%d (+%d new)",
                    key, len(existing), added,
                )
        else:
            log.info("Deezer Flow config=%r cache=%d (no fetch)", key, len(existing))

        return list(self._flow_cache.get(key) or [])

    def ensure_flow_buffer(self, config_id: str = "", min_ahead: int = FLOW_MIN_AHEAD, from_index: int = 0) -> List[DzTrack]:
        tracks = self.get_flow_tracks(config_id, append=False, max_cache=FLOW_TREE_MAX_CACHE)
        if len(tracks) >= FLOW_TREE_MAX_CACHE:
            return tracks
        remaining = len(tracks) - from_index
        if remaining < min_ahead:
            tracks = self.get_flow_tracks(config_id, append=True, max_cache=FLOW_TREE_MAX_CACHE)
        return tracks

    def activate_flow_playback(self, config_id: str) -> None:
        """Вызвать при старте воспроизведения слота Flow.

        Сбрасывает кэш *других* категорий только когда реально включили
        другой Flow-плейлист. Уход в меню, пока metal ещё играет — кэш metal
        не трогаем.
        """
        key = config_id or "default"
        if not hasattr(self, "_flow_playing_key"):
            self._flow_playing_key = None
        if not hasattr(self, "_flow_cache"):
            return
        prev = self._flow_playing_key
        if prev is not None and prev != key:
            for k in list(self._flow_cache.keys()):
                if k == key:
                    continue
                self._flow_cache.pop(k, None)
                self._flow_cache_ts.pop(k, None)
                self._flow_last_access.pop(k, None)
                log.info(
                    "Flow config=%r — сброс (включили другой плейлист → %r)",
                    k, key,
                )
        self._flow_playing_key = key

    def ensure_flow_index(self, config_id: str, index: int) -> Optional[DzTrack]:

        """Гарантировать трек в слоте index: догружать партиями по FLOW_BATCH.

        Индексы стабильны (начало кэша не выкидывается). Нужно для ресиверов,
        которые один раз прочитали 100 слотов и дальше играют по URL слота.
        """
        if index < 0:
            return None
        max_cache = FLOW_TREE_MAX_CACHE
        if index >= max_cache:
            return None
        tracks = self.get_flow_tracks(
            config_id, limit=FLOW_BATCH, append=False, max_cache=max_cache,
        )
        grew = 0
        while len(tracks) <= index and grew < 40:
            before = len(tracks)
            tracks = self.get_flow_tracks(
                config_id, limit=FLOW_BATCH, append=True, max_cache=max_cache,
            )
            grew += 1
            if len(tracks) == before:
                break
        if 0 <= index < len(tracks):
            return tracks[index]
        return None

    def reset_flow_cache(self, config_id: Optional[str] = None) -> None:
        """Сбросить кэш Flow. Без аргумента — весь."""
        if not hasattr(self, "_flow_cache_ts"):
            self._flow_cache_ts = {}
        if not hasattr(self, "_flow_last_access"):
            self._flow_last_access = {}
        if config_id is None:
            self._flow_cache.clear()
            self._flow_cache_ts.clear()
            self._flow_last_access.clear()
            self._flow_playing_key = None
            log.info("Flow cache: reset all")
        else:
            key = config_id or "default"
            self._flow_cache.pop(key, None)
            self._flow_cache_ts.pop(key, None)
            self._flow_last_access.pop(key, None)
            if getattr(self, "_flow_playing_key", None) == key:
                self._flow_playing_key = None
            log.info("Flow cache: reset %r", key)

    # ========================================================================
    #  License / token
    # ========================================================================

    def _get_license_token(self) -> str:
        if getattr(self, "_license_token", None) and getattr(self, "_license_ts", 0):
            if time.time() - self._license_ts < 6 * 3600:
                return self._license_token or ""
        try:
            data = self._gw("deezer.getUserData", api_token="")
            results = data.get("results") or {}
            tok = results.get("checkForm") or results.get("checkFormLogin")
            if tok:
                self._api_token = tok
            lt = (
                (results.get("USER") or {}).get("OPTIONS") or {}
            ).get("license_token") or results.get("license_token") or ""
            self._license_token = lt
            self._license_ts = time.time()
            if not lt:
                log.warning("license_token empty — check ARL / Premium")
            return lt
        except Exception as e:
            log.warning("license_token: %s", e)
            return ""

    # ========================================================================
    #  Explore
    # ========================================================================

    def get_explore(self) -> List[DzPlaylist]:
        if not self.ensure_login():
            return []
        out: List[DzPlaylist] = []
        seen = set()
        try:
            r = self.session.get(
                f"{API}/chart/0/playlists", params={"limit": 50}, timeout=20
            )
            for it in (r.json().get("data") or []):
                pid = str(it.get("id") or "")
                if pid and pid not in seen:
                    seen.add(pid)
                    out.append(DzPlaylist(
                        id=pid,
                        title=(it.get("title") or "Chart").strip(),
                        track_count=int(it.get("nb_tracks") or 0),
                    ))
        except Exception as e:
            log.warning("chart playlists: %s", e)
        for genre_id in (132, 116, 152, 113, 165, 85):
            try:
                r = self.session.get(
                    f"{API}/chart/{genre_id}/playlists",
                    params={"limit": 10},
                    timeout=15,
                )
                for it in (r.json().get("data") or []):
                    pid = str(it.get("id") or "")
                    if pid and pid not in seen:
                        seen.add(pid)
                        out.append(DzPlaylist(
                            id=pid,
                            title=(it.get("title") or "Playlist").strip(),
                            track_count=int(it.get("nb_tracks") or 0),
                        ))
            except Exception:
                continue
        log.info("Deezer explore: %d playlists", len(out))
        return out

    # ========================================================================
    #  Стриминг
    # ========================================================================

    @staticmethod
    def _blowfish_key(track_id: str) -> bytes:
        sec = b"g4el58wc0zvf9na1"
        m = hashlib.md5(str(track_id).encode()).hexdigest().encode()
        return bytes([m[i] ^ m[i + 16] ^ sec[i] for i in range(16)])

    def _track_token(self, track_id: str) -> Optional[str]:
        try:
            data = self._gw("song.getListData", {
                "sng_ids": [int(track_id) if str(track_id).isdigit() else track_id],
            })
            results = data.get("results") or {}
            items = results.get("data") if isinstance(results, dict) else results
            if isinstance(items, dict):
                items = [items]
            if not items:
                items = []
            for it in items:
                sid = str(it.get("SNG_ID") or it.get("id") or "")
                if sid == str(track_id) or not sid:
                    tok = it.get("TRACK_TOKEN") or it.get("track_token")
                    if not tok and it.get("FALLBACK"):
                        tok = it["FALLBACK"].get("TRACK_TOKEN")
                    if tok:
                        return tok
            if items:
                it = items[0]
                return (
                    it.get("TRACK_TOKEN")
                    or (it.get("FALLBACK") or {}).get("TRACK_TOKEN")
                )
        except Exception as e:
            log.debug("track_token: %s", e)
        try:
            data = self._gw("song.getData", {"sng_id": str(track_id)})
            results = data.get("results") or {}
            return (
                results.get("TRACK_TOKEN")
                or (results.get("FALLBACK") or {}).get("TRACK_TOKEN")
            )
        except Exception as e:
            log.debug("song.getData: %s", e)
        return None

    def _add_unavailable(self, track_id: str) -> None:
        tid = str(track_id)
        if tid in self._unavailable:
            return
        self._unavailable.add(tid)
        try:
            with open("/opt/dlna-emu/unavailable.txt", "a", encoding="utf-8") as f:
                f.write(tid + "\n")
        except Exception:
            pass

    def _media_formats(self) -> List[Tuple[str, str]]:
        """Порядок форматов: preferred quality, затем запасные."""
        q = (getattr(self, "quality", None) or "auto").lower()
        all_fmts = [
            ("FLAC", "audio/flac"),
            ("MP3_320", "audio/mpeg"),
            ("MP3_128", "audio/mpeg"),
        ]
        if q in ("mp3_128", "mp3_320", "flac"):
            want = QUALITY_MAP.get(q, "FLAC")
            preferred = [(f, m) for f, m in all_fmts if f == want]
            rest = [(f, m) for f, m in all_fmts if f != want]
            return preferred + rest
        return all_fmts

    def get_media_url(
        self,
        track_id: str,
        *,
        formats: Optional[List[Tuple[str, str]]] = None,
        blacklist_on_fail: bool = True,
    ) -> Optional[Tuple[str, str, bool, str]]:
        if not self.ensure_login():
            return None
        if str(track_id) in self._unavailable:
            log.info("track %s — в чёрном списке, пропускаем", track_id)
            return None

        lic = self._get_license_token()
        if not lic:
            log.warning("no license_token")
            return None
        token = self._track_token(track_id)
        if not token:
            log.warning("no TRACK_TOKEN for %s", track_id)
            return None

        fmt_list = formats or self._media_formats()
        last_errs: list = []
        for fmt, mime in fmt_list:
            try:
                mr = self.session.post(
                    "https://media.deezer.com/v1/get_url",
                    json={
                        "license_token": lic,
                        "media": [{
                            "type": "FULL",
                            "formats": [{"cipher": "BF_CBC_STRIPE", "format": fmt}],
                        }],
                        "track_tokens": [token],
                    },
                    timeout=20,
                )
                if mr.status_code != 200:
                    log.debug("get_url HTTP %s fmt=%s", mr.status_code, fmt)
                    continue
                j = mr.json()
                block = (j.get("data") or [{}])[0]
                media = block.get("media") or []
                errs = block.get("errors") or []
                if errs:
                    last_errs = errs
                if not media:
                    log.debug("get_url no media fmt=%s err=%s", fmt, errs)
                    continue
                sources = media[0].get("sources") or []
                if sources and sources[0].get("url"):
                    log.info("track %s -> %s", track_id, fmt)
                    return sources[0]["url"], mime, True, str(track_id)
            except Exception as e:
                log.debug("get_url %s: %s", fmt, e)

        no_rights = any(
            isinstance(e, dict) and e.get("code") == 2002 for e in last_errs
        )
        if blacklist_on_fail:
            if no_rights:
                log.warning(
                    "track %s: no sufficient rights — в чёрный список", track_id,
                )
            else:
                log.warning(
                    "track %s: недоступен для стрима (geo/licensing) — в чёрный список",
                    track_id,
                )
            self._add_unavailable(track_id)
        return None

    def _cdn_open(self, url: str, attempts: int = 4):
        """Открыть CDN-URL с ретраями. Таймаут: connect 25с, read 180с."""
        r = None
        last_err: Optional[BaseException] = None
        for attempt in range(attempts):
            try:
                r = self.session.get(
                    url,
                    stream=True,
                    timeout=(25, 180),
                    headers={
                        "User-Agent": UA,
                        "Accept": "*/*",
                        "Connection": "close",
                    },
                )
                if r.status_code == 200:
                    return r
                code = r.status_code
                log.warning("cdn HTTP %s (попытка %d/%d)", code, attempt + 1, attempts)
                try:
                    r.close()
                except Exception:
                    pass
                r = None
                if code in (403, 404, 410):
                    break  # ссылка протухла / нет прав — другой формат
            except Exception as e:
                last_err = e
                log.warning("cdn connect (попытка %d/%d): %s", attempt + 1, attempts, e)
                time.sleep(0.4 * (attempt + 1))
        if last_err and r is None:
            log.debug("cdn give up: %s", last_err)
        return r

    def open_stream(self, track_id: str):
        """Открыть поток. При сбое CDN FLAC пробуем MP3_320 / MP3_128."""
        if str(track_id) in self._unavailable:
            log.info("track %s — в чёрном списке, пропускаем", track_id)
            return None, None, None, False

        formats = self._media_formats()
        last_tid = str(track_id)
        tried_fmt: List[str] = []

        for i, (fmt, mime) in enumerate(formats):
            info = self.get_media_url(
                track_id,
                formats=[(fmt, mime)],
                blacklist_on_fail=(i == len(formats) - 1),
            )
            if not info:
                continue
            url, mime, needs_decrypt, tid = info
            last_tid = tid
            tried_fmt.append(fmt)

            r = self._cdn_open(url, attempts=4)
            if r is None:
                log.warning(
                    "track %s: CDN fail fmt=%s — пробуем другой формат", tid, fmt
                )
                continue

            try:
                cl_hdr = r.headers.get("Content-Length")
                content_length = int(cl_hdr) if cl_hdr and cl_hdr.isdigit() else None

                if not needs_decrypt:
                    self._cdn_fails.pop(str(tid), None)
                    return r, mime, content_length, True

                key = self._blowfish_key(tid)
                iv = bytes.fromhex("0001020304050607")
                chunk_size = 2048

                def gen(_r=r, _key=key, _iv=iv, _cs=chunk_size):
                    try:
                        from Crypto.Cipher import Blowfish
                    except ImportError:
                        from Cryptodome.Cipher import Blowfish
                    buf = b""
                    block_index = 0
                    try:
                        for raw in _r.iter_content(chunk_size=8192):
                            if not raw:
                                continue
                            buf += raw
                            while len(buf) >= _cs:
                                chunk = buf[:_cs]
                                buf = buf[_cs:]
                                if block_index % 3 == 0:
                                    chunk = Blowfish.new(
                                        _key, Blowfish.MODE_CBC, _iv
                                    ).decrypt(chunk)
                                block_index += 1
                                yield chunk
                        if buf:
                            if len(buf) == _cs and block_index % 3 == 0:
                                buf = Blowfish.new(
                                    _key, Blowfish.MODE_CBC, _iv
                                ).decrypt(buf)
                            yield buf
                    finally:
                        try:
                            _r.close()
                        except Exception:
                            pass

                self._cdn_fails.pop(str(tid), None)
                return gen(), mime, content_length, False
            except Exception as e:
                log.warning("open_stream fmt=%s: %s", fmt, e)
                try:
                    r.close()
                except Exception:
                    pass

        try:
            r = self.session.get(f"{API}/track/{track_id}", timeout=15)
            preview = (r.json() or {}).get("preview")
            if preview:
                log.warning(
                    "track %s: only preview (cdn failed: %s)", track_id, tried_fmt
                )
                pr = self.session.get(preview, stream=True, timeout=30)
                if pr.status_code == 200:
                    cl = pr.headers.get("Content-Length")
                    return pr, "audio/mpeg", int(cl) if cl else None, True
        except Exception:
            pass

        fails = self._cdn_fails.get(str(last_tid), 0) + 1
        self._cdn_fails[str(last_tid)] = fails
        if fails >= 4:
            log.warning(
                "track %s: CDN не отдаёт поток (%d раз) — в чёрный список",
                last_tid, fails,
            )
            self._add_unavailable(last_tid)
        return None, None, None, False
