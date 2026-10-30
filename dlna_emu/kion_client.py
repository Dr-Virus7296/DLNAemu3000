"""КИОН Музыка (music.mts.ru) — поиск и стрим по OAuth-токену (API совместим с Яндекс.Музыкой)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import requests
from urllib.parse import quote

log = logging.getLogger("dlna_emu.kion")

API_CANDIDATES = [
    "https://api.music.yandex.net",       # основной API (браузер КИОН ходит сюда)
    "https://music.mts.ru/ya_api",
    "https://music.mts.ru/ya_proxy_api",
]
API = API_CANDIDATES[0]
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
CLIENT_HEADER = "MTSMusicWebPremium/2.22.0"
# Для /get-file-info (FLAC) нужна подпись десктоп-клиента
DESKTOP_CLIENT = "YandexMusicDesktopAppWindows/5.95.0"
DESKTOP_SIGN_KEY = "kzqU4XhfCaY6B6JTHODeq5"
FILE_INFO_CODECS = ["flac", "flac-mp4", "aac-mp4", "he-aac-mp4", "aac", "he-aac", "mp3"]
SIGN_SALT = "XGRlBW9FXlekgbPrRHuSiA"


@dataclass
class KionTrack:
    id: str
    title: str
    artist: str
    duration: int = 0
    album: str = ""
    album_id: str = ""
    cover_uri: str = ""


class KionMusicClient:
    def __init__(
        self,
        token: str = "",
        quality: str = "mp3_320",
        cache_sec: int = 300,
        stream_mode: str = "auto",
        buffer_timeout: float = 6.0,
    ):
        self.token = (token or "").strip().strip('"').strip("'")
        # иногда копируют с префиксом "OAuth "
        if self.token.lower().startswith("oauth "):
            self.token = self.token[6:].strip()
        self.quality = (quality or "mp3_320").lower()
        self._has_plus = False
        self.cache_sec = cache_sec
        # auto = буфер с таймаутом, fallback на chunked (Yamaha RX-V777 и т.п.)
        # buffered = всегда буферизовать
        # chunked = сразу стримить
        self.stream_mode = (stream_mode or "auto").lower().strip()
        self.buffer_timeout = float(buffer_timeout or 6.0)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": UA,
            "Accept": "application/json",
            "X-Yandex-Music-Client": CLIENT_HEADER,
            "X-Music-Client": "web",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "Referer": "https://music.mts.ru/",
        })
        if self.token:
            self.session.headers["Authorization"] = f"OAuth {self.token}"
        self.api_base = API
        self._account_ok: Optional[bool] = None
        self._uid: Optional[str] = None
        self._user_name: Optional[str] = None
        self._tracks_cache: Dict[str, Tuple[float, List[KionTrack]]] = {}
        self._artist_cache: Dict[str, Tuple[float, List[KionTrack]]] = {}
        self._album_cache: Dict[str, Tuple[float, List[KionTrack]]] = {}
        self._metatags_cache: Tuple[float, list] = (0.0, [])
        self._metatag_pl_cache: Dict[str, Tuple[float, list]] = {}
        self._cover_map: Dict[str, str] = {}
        self._playlist_cache: Dict[str, Tuple[float, List[KionTrack]]] = {}
        # Моя волна (rotor)
        self._wave_tracks: List[KionTrack] = []
        self._wave_batch_id: Optional[str] = None
        self._wave_last_track_id: Optional[str] = None
        self._wave_ts: float = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def _ensure_uid(self) -> Optional[str]:
        """UID: /account/status у КИОН часто 404, берём из /account/settings."""
        if self._uid:
            return self._uid
        # 1) settings (работает на api.music.yandex.net с токеном КИОН)
        res = self._get("/account/settings")
        if isinstance(res, dict) and res.get("uid") is not None:
            self._uid = str(res["uid"])
            return self._uid
        # 2) status (классический Яндекс)
        res = self._get("/account/status") or {}
        account = res.get("account") or {}
        uid = account.get("uid") or account.get("id")
        if uid is not None:
            self._uid = str(uid)
            return self._uid
        return None

    def _get(self, path: str, params: Optional[dict] = None) -> Optional[dict]:
        if not self.token:
            return None
        try:
            r = self.session.get(f"{self.api_base}{path}", params=params or {}, timeout=25)
            if r.status_code in (401, 403):
                log.warning("Kion Music: токен недействителен или нет доступа (%s)", r.status_code)
                self._account_ok = False
                return None
            if r.status_code >= 400:
                log.warning("Kion GET %s -> HTTP %s: %s", path, r.status_code, r.text[:200])
                return None
            data = r.json()
            if data.get("error"):
                log.warning("Kion API %s error: %s", path, data.get("error"))
                return None
            return data.get("result", data)
        except Exception as e:
            log.warning("Kion GET %s: %s", path, e)
            return None

    def _post(self, path: str, data: Optional[dict] = None) -> Optional[dict]:
        if not self.token:
            return None
        try:
            r = self.session.post(f"{self.api_base}{path}", data=data or {}, timeout=25)
            if r.status_code >= 400:
                log.warning("Kion POST %s -> HTTP %s: %s", path, r.status_code, r.text[:200])
                return None
            body = r.json()
            if body.get("error"):
                log.warning("Kion API %s error: %s", path, body.get("error"))
                return None
            return body.get("result", body)
        except Exception as e:
            log.warning("Kion POST %s: %s", path, e)
            return None

    def _tracks_by_ids(self, ids: List[str]) -> Dict[str, KionTrack]:
        """Полные данные треков по id (для плейлистов, где пришли только короткие записи)."""
        out: Dict[str, KionTrack] = {}
        ids = [str(i) for i in ids if i]
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            res = self._post("/tracks", {"track-ids": ",".join(chunk), "with-positions": "False"})
            if not isinstance(res, list):
                continue
            for t in res:
                tr = self._track_from(t if isinstance(t, dict) else {})
                if tr:
                    out[tr.id] = tr
        return out

    def _tracks_from_entries(self, entries: list) -> List[KionTrack]:
        """Элементы плейлиста → треки. Если у элемента нет вложенного track
        (короткая запись {id, albumId}) — догружаем пачкой через /tracks."""
        slots: List[Optional[KionTrack]] = []
        missing: List[str] = []
        for t in entries or []:
            if not isinstance(t, dict):
                slots.append(None)
                continue
            obj = t.get("track") if isinstance(t.get("track"), dict) else None
            if obj is None and t.get("title"):
                obj = t                       # уже полный объект трека
            tr = self._track_from(obj) if obj else None
            if tr is None:
                tid = t.get("id") or t.get("realId")
                if tid:
                    aid = t.get("albumId")
                    missing.append(f"{tid}:{aid}" if aid else str(tid))
                slots.append(None)
                continue
            slots.append(tr)
        if missing:
            fetched = self._tracks_by_ids(missing)
            # порядок: заново проходим по entries и берём найденные
            result: List[KionTrack] = []
            for t, tr in zip(entries, slots):
                if tr is not None:
                    result.append(tr)
                    continue
                if isinstance(t, dict):
                    tid = str(t.get("id") or t.get("realId") or "")
                    if tid in fetched:
                        result.append(fetched[tid])
            return result
        return [tr for tr in slots if tr is not None]

    def get_cover_url(self, track_id: str, size: str = "300x300") -> Optional[str]:
        """URL обложки трека (https). Берём из кэша, иначе спрашиваем API."""
        tid = str(track_id or "").strip()
        if not tid or not self.enabled:
            return None
        uri = self._cover_map.get(tid)
        if not uri:
            res = self._post("/tracks", {"track-ids": tid, "with-positions": "False"})
            if isinstance(res, list) and res and isinstance(res[0], dict):
                uri = res[0].get("coverUri") or res[0].get("ogImage") or ""
                if uri:
                    self._remember_cover(tid, uri)
        if not uri:
            return None
        return "https://" + uri.replace("%%", size).lstrip("/")

    def _remember_cover(self, tid: str, uri: str) -> None:
        if len(self._cover_map) > 4000:
            self._cover_map.clear()
        self._cover_map[tid] = uri

    def login(self) -> bool:
        """Проверка токена КИОН. /account/status часто 404 — используем /account/settings."""
        if not self.enabled:
            return False
        last_err = None
        for base in API_CANDIDATES:
            self.api_base = base
            try:
                # settings надёжнее status для токенов КИОН
                r = self.session.get(f"{base}/account/settings", timeout=25)
                if r.status_code in (401, 403):
                    log.warning("Kion Music: токен отклонён (%s) на %s", r.status_code, base)
                    self._account_ok = False
                    return False
                if r.status_code == 404:
                    last_err = "HTTP 404 settings"
                    # попробуем status
                    r2 = self.session.get(f"{base}/account/status", timeout=25)
                    if r2.status_code in (401, 403):
                        log.warning("Kion Music: токен отклонён (%s) на %s", r2.status_code, base)
                        self._account_ok = False
                        return False
                    if r2.status_code >= 400:
                        last_err = f"HTTP {r2.status_code}"
                        continue
                    try:
                        data = r2.json()
                        res = data.get("result", data) if isinstance(data, dict) else {}
                        account = (res.get("account") or {}) if isinstance(res, dict) else {}
                        uid = account.get("uid")
                        name = account.get("displayName") or account.get("login") or uid or "?"
                        self._uid = str(uid) if uid else None
                        self._user_name = str(name) if name and name != "?" else None
                        plus = (res.get("plus") or {}) if isinstance(res, dict) else {}
                        has_plus = bool(plus.get("hasPlus") or plus.get("has_plus"))
                        self._has_plus = has_plus
                        self._account_ok = True
                        log.info("Kion Music: OK (%s) base=%s Plus=%s", name, base, has_plus)
                        return True
                    except Exception as e:
                        last_err = e
                        continue
                if r.status_code >= 400:
                    last_err = f"HTTP {r.status_code}"
                    continue
                data = r.json()
                res = data.get("result", data) if isinstance(data, dict) else {}
                if not isinstance(res, dict) or res.get("uid") is None:
                    last_err = "no uid in settings"
                    continue
                self._uid = str(res["uid"])
                name = res.get("displayName") or res.get("login") or self._uid
                self._has_plus = False  # settings не отдаёт plus
                # optional: try status for plus flag (ignore errors)
                try:
                    st = self._get("/account/status") or {}
                    plus = st.get("plus") or {}
                    self._has_plus = bool(plus.get("hasPlus") or plus.get("has_plus"))
                    acc = st.get("account") or {}
                    if acc.get("displayName") or acc.get("login"):
                        name = acc.get("displayName") or acc.get("login")
                except Exception:
                    pass
                self._account_ok = True
                self._user_name = str(name) if name else None
                log.info(
                    "Kion Music: OK (uid=%s name=%s) base=%s",
                    self._uid, name, base,
                )
                return True
            except Exception as e:
                last_err = e
                log.debug("Kion base %s: %s", base, e)
                continue
        self.api_base = API_CANDIDATES[0]
        log.warning("Kion login failed (last: %s). base=%s", last_err, self.api_base)
        self._account_ok = False
        return False


    def _artists_str(self, artists) -> str:
        if not artists:
            return ""
        names = []
        for a in artists:
            if isinstance(a, dict) and a.get("name"):
                names.append(a["name"])
        return ", ".join(names)

    def _track_from(self, t: dict) -> Optional[KionTrack]:
        if not t or not t.get("id"):
            return None
        title = t.get("title") or ""
        if t.get("version"):
            title = f"{title} ({t['version']})"
        artists = self._artists_str(t.get("artists") or [])
        duration_ms = t.get("durationMs") or 0
        albums = t.get("albums") or []
        album = ""
        album_id = ""
        if albums and isinstance(albums[0], dict):
            album = albums[0].get("title") or ""
            album_id = str(albums[0].get("id") or "")
        cover = t.get("coverUri") or t.get("ogImage") or ""
        if not cover and albums and isinstance(albums[0], dict):
            cover = albums[0].get("coverUri") or ""
        if cover:
            self._remember_cover(str(t["id"]), cover)
        return KionTrack(
            id=str(t["id"]),
            title=title,
            artist=artists,
            duration=int(duration_ms / 1000) if duration_ms else 0,
            album=album,
            album_id=album_id,
            cover_uri=cover,
        )

    def search_tracks(self, query: str, limit: int = 40) -> List[KionTrack]:
        q = (query or "").strip()
        if not q or not self.enabled:
            return []
        res = self._get("/search", {
            "text": q,
            "type": "track",
            "page": 0,
            "nocorrect": "false",
        })
        if not res:
            return []
        tracks_block = res.get("tracks") or {}
        results = tracks_block.get("results") or []
        out: List[KionTrack] = []
        for t in results[:limit]:
            tr = self._track_from(t)
            if tr:
                out.append(tr)
        return out

    def search_artists(self, query: str, limit: int = 20) -> List[dict]:
        q = (query or "").strip()
        if not q or not self.enabled:
            return []
        res = self._get("/search", {
            "text": q,
            "type": "artist",
            "page": 0,
            "nocorrect": "false",
        })
        if not res:
            return []
        block = res.get("artists") or {}
        results = block.get("results") or []
        out = []
        for a in results[:limit]:
            aid = str(a.get("id") or "")
            name = a.get("name") or ""
            if not aid or not name:
                continue
            counts = a.get("counts") or {}
            out.append({
                "id": aid,
                "name": name,
                "nb_album": counts.get("directAlbums") or 0,
                "nb_tracks": counts.get("tracks") or 0,
            })
        return out

    def search_albums(self, query: str, limit: int = 20) -> List[dict]:
        q = (query or "").strip()
        if not q or not self.enabled:
            return []
        res = self._get("/search", {
            "text": q,
            "type": "album",
            "page": 0,
            "nocorrect": "false",
        })
        if not res:
            return []
        block = res.get("albums") or {}
        results = block.get("results") or []
        out = []
        for al in results[:limit]:
            aid = str(al.get("id") or "")
            title = al.get("title") or ""
            if not aid or not title:
                continue
            artists = self._artists_str(al.get("artists") or [])
            out.append({
                "id": aid,
                "title": title,
                "artist": artists,
                "nb_tracks": al.get("trackCount") or 0,
                "year": al.get("year") or "",
            })
        return out

    def get_artist_tracks(self, artist_id: str, limit: int = 100) -> List[KionTrack]:
        aid = str(artist_id or "").strip()
        if not aid or not self.enabled:
            return []
        now = time.time()
        cached = self._artist_cache.get(aid)
        if cached and (now - cached[0]) < self.cache_sec:
            return cached[1][:limit]

        res = self._get(f"/artists/{aid}/tracks", {"page": 0, "page-size": min(limit, 100)})
        out: List[KionTrack] = []
        if res:
            tracks = res.get("tracks") or res if isinstance(res, list) else []
            if isinstance(res, dict) and "tracks" in res:
                tracks = res["tracks"]
            for t in tracks:
                tr = self._track_from(t if isinstance(t, dict) and "title" in t else (t.get("track") if isinstance(t, dict) else t))
                if tr:
                    out.append(tr)
        if out:
            self._artist_cache[aid] = (now, out)
        return out[:limit]

    def get_album_tracks(self, album_id: str) -> List[KionTrack]:
        aid = str(album_id or "").strip()
        if not aid or not self.enabled:
            return []
        now = time.time()
        cached = self._album_cache.get(aid)
        if cached and (now - cached[0]) < self.cache_sec:
            return cached[1]

        res = self._get(f"/albums/{aid}/with-tracks")
        out: List[KionTrack] = []
        if res:
            volumes = res.get("volumes") or []
            for vol in volumes:
                if not isinstance(vol, list):
                    continue
                for t in vol:
                    tr = self._track_from(t)
                    if tr:
                        out.append(tr)
        if out:
            self._album_cache[aid] = (now, out)
        return out

    def _pick_download_info(self, infos: list) -> Optional[dict]:
        if not infos:
            return None
        # prefer non-preview, highest bitrate matching quality preference
        prefer_flac = self.quality in ("flac", "lossless")
        candidates = [i for i in infos if not i.get("preview")]
        if not candidates:
            candidates = list(infos)

        def score(i: dict) -> tuple:
            codec = (i.get("codec") or "").lower()
            br = i.get("bitrateInKbps") or 0
            is_flac = codec in ("flac", "alac")
            if prefer_flac:
                return (1 if is_flac else 0, br)
            # mp3 preference
            return (1 if codec == "mp3" else 0, br)

        candidates.sort(key=score, reverse=True)
        return candidates[0]

    def _file_info_sign(self, track_id: str, quality: str, codecs: list, transport: str):
        """HMAC-подпись для /get-file-info (десктоп-ключ)."""
        ts = int(time.time())
        msg = f"{ts}{track_id}{quality}{''.join(codecs)}{transport}"
        digest = hmac.new(
            DESKTOP_SIGN_KEY.encode("utf-8"),
            msg.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        sign = base64.b64encode(digest).decode("utf-8")[:-1]
        return ts, sign

    def _get_file_info_url(
        self,
        track_id: str,
        quality: str = "lossless",
        codecs: Optional[list] = None,
        require_flac: bool = False,
    ) -> Optional[tuple]:
        """Прямой URL через /get-file-info. Возвращает (url, mime) или None.
        Нужна подписка Plus для lossless.
        """
        transport = "raw"
        codecs = list(codecs) if codecs else list(FILE_INFO_CODECS)
        ts, sign = self._file_info_sign(track_id, quality, codecs, transport)
        params = {
            "ts": ts,
            "trackId": track_id,
            "quality": quality,
            "codecs": ",".join(codecs),
            "transports": transport,
            "sign": sign,
        }
        try:
            headers = {
                "Authorization": f"OAuth {self.token}",
                "X-Yandex-Music-Client": DESKTOP_CLIENT,
                "User-Agent": UA,
                "Accept": "application/json",
            }
            r = self.session.get(
                f"{self.api_base}/get-file-info",
                params=params,
                headers=headers,
                timeout=20,
            )
            if r.status_code == 401:
                log.warning("Kion get-file-info 401 — нет Plus или токен невалиден")
                return None
            if r.status_code >= 400:
                log.warning("Kion get-file-info HTTP %s: %s", r.status_code, r.text[:200])
                return None
            data = r.json()
            info = (data.get("result") or {}).get("downloadInfo") or data.get("downloadInfo") or data.get("result")
            if not info or not isinstance(info, dict):
                return None
            url = info.get("url") or ""
            if not url:
                urls = info.get("urls") or []
                if urls:
                    url = urls[0]
            if not url:
                return None
            codec = (info.get("codec") or "").lower()
            if require_flac and "flac" not in codec:
                log.warning(
                    "Kion track %s: asked FLAC, got codec=%s quality=%s — reject",
                    track_id, codec, info.get("quality"),
                )
                return None
            if "flac" in codec:
                mime = "audio/flac"
            elif "aac" in codec or "mp4" in codec:
                mime = "audio/mp4"
            else:
                mime = "audio/mpeg"
            log.info(
                "Kion track %s: quality=%s codec=%s bitrate=%s",
                track_id, info.get("quality"), codec, info.get("bitrate"),
            )
            return url, mime
        except Exception as e:
            log.warning("Kion get-file-info %s: %s", track_id, e)
            return None

    def get_stream_url(self, track_id: str) -> Optional[tuple]:
        """Возвращает (url, mime) или None.
        При quality=flac/lossless — сначала /get-file-info, иначе download-info (mp3).
        """
        tid = str(track_id or "").strip()
        if not tid or not self.enabled:
            return None

        prefer_flac = self.quality in ("flac", "lossless")
        if prefer_flac:
            # Только flac-кодеки — иначе API спокойно отдаёт AAC
            flac_codecs = ["flac", "flac-mp4"]
            result = self._get_file_info_url(
                tid, quality="lossless", codecs=flac_codecs, require_flac=True
            )
            if result:
                return result
            # повтор с полным списком, но всё равно требуем flac в ответе
            result = self._get_file_info_url(
                tid, quality="lossless", codecs=list(FILE_INFO_CODECS), require_flac=True
            )
            if result:
                return result
            log.warning(
                "Kion: FLAC недоступен для %s (нет Plus или трек без lossless) — mp3",
                tid,
            )

        # Классический download-info (обычно mp3 192/320)
        infos = self._get(f"/tracks/{tid}/download-info")
        if not infos or not isinstance(infos, list):
            log.warning("Kion: no download-info for %s", tid)
            return None
        pick = self._pick_download_info(infos)
        if not pick:
            return None
        info_url = pick.get("downloadInfoUrl") or pick.get("download_info_url")
        if not info_url:
            return None
        try:
            sep = "&" if "?" in info_url else "?"
            r = self.session.get(info_url + sep + "format=json", timeout=20)
            r.raise_for_status()
            try:
                data = r.json()
            except Exception:
                text = r.text
                import re
                host = re.search(r"<host>([^<]+)</host>", text)
                path = re.search(r"<path>([^<]+)</path>", text)
                ts = re.search(r"<ts>([^<]+)</ts>", text)
                s = re.search(r"<s>([^<]+)</s>", text)
                if not all([host, path, ts, s]):
                    log.warning("Kion: cannot parse download info for %s", tid)
                    return None
                data = {
                    "host": host.group(1),
                    "path": path.group(1),
                    "ts": ts.group(1),
                    "s": s.group(1),
                }
            host = data.get("host") or ""
            path = data.get("path") or ""
            ts = data.get("ts") or ""
            s = data.get("s") or ""
            if not (host and path and ts and s):
                return None
            sign_src = SIGN_SALT + path[1:] + s
            sign = hashlib.md5(sign_src.encode("utf-8")).hexdigest()
            url = f"https://{host}/get-mp3/{sign}/{ts}{path}"
            codec = (pick.get("codec") or "mp3").lower()
            mime = "audio/flac" if "flac" in codec else "audio/mpeg"
            return url, mime
        except Exception as e:
            log.warning("Kion get_stream_url %s: %s", tid, e)
            return None

    def open_stream(self, track_id: str):
        """Возвращает (response, content-type, content-length) или None."""
        result = self.get_stream_url(track_id)
        if not result:
            return None
        if isinstance(result, tuple):
            url, mime = result
        else:
            url, mime = result, "audio/mpeg"
        try:
            r = self.session.get(url, stream=True, timeout=30)
            if r.status_code >= 400:
                log.warning("Kion stream HTTP %s for %s", r.status_code, track_id)
                r.close()
                return None
            ctype = r.headers.get("Content-Type") or mime or "audio/mpeg"
            # CDN иногда отдаёт application/octet-stream — подставляем наш mime
            if "octet-stream" in ctype or ctype == "application/octet-stream":
                ctype = mime or "audio/mpeg"
            clen = r.headers.get("Content-Length")
            return r, ctype, clen
        except Exception as e:
            log.warning("Kion open_stream %s: %s", track_id, e)
            return None

    # ---------- Каталог (метатеги) ----------

    def list_metatag_sections(self) -> List[dict]:
        """Разделы каталога: Настроения, Жанры, Занятия, Эпохи.
        [{'title': str, 'id': str}, ...]
        """
        now = time.time()
        if self._metatags_cache[1] and (now - self._metatags_cache[0]) < self.cache_sec:
            return [{"title": s["title"], "id": s["id"]} for s in self._metatags_cache[1]]

        res = self._get("/landing3/metatags")
        if not res:
            res = self._get("/tags")  # fallback
        sections = []
        if res:
            trees = res.get("trees") or res.get("tags") or []
            if isinstance(res, list):
                trees = res
            for i, tree in enumerate(trees):
                if not isinstance(tree, dict):
                    continue
                title = tree.get("title") or tree.get("name") or f"Раздел {i+1}"
                leaves = tree.get("leaves") or tree.get("tags") or tree.get("items") or []
                sections.append({
                    "title": title,
                    "id": str(i),
                    "leaves": leaves,
                })
        if not sections:
            log.warning("Kion metatags: пустой ответ (проверьте токен/API)")
        self._metatags_cache = (now, sections)
        return [{"title": s["title"], "id": s["id"]} for s in sections]

    def list_metatags_in_section(self, section_id: str) -> List[dict]:
        """Метатеги внутри раздела: [{'title', 'tag'}, ...]."""
        now = time.time()
        if not self._metatags_cache[1] or (now - self._metatags_cache[0]) >= self.cache_sec:
            self.list_metatag_sections()  # refresh cache

        sections = self._metatags_cache[1]
        try:
            idx = int(section_id)
        except (TypeError, ValueError):
            idx = -1
        if idx < 0 or idx >= len(sections):
            # try by title
            for s in sections:
                if s.get("title") == section_id:
                    leaves = s.get("leaves") or []
                    break
            else:
                return []
        else:
            leaves = sections[idx].get("leaves") or []

        result: List[dict] = []

        def walk(nodes):
            for leaf in nodes or []:
                tag = leaf.get("tag")
                title = leaf.get("title") or tag or "Unknown"
                if tag:
                    result.append({"title": title, "tag": tag})
                sub = leaf.get("leaves") or []
                if sub:
                    walk(sub)

        walk(leaves)
        return result

    def get_metatag_playlists(self, tag: str, limit: int = 40) -> List[dict]:
        """Плейлисты метатега: [{'title','owner','kind','owner_id','track_count','uuid'}, ...]."""
        tag = (tag or "").strip()
        if not tag or not self.enabled:
            return []
        now = time.time()
        cached = self._metatag_pl_cache.get(tag)
        if cached and (now - cached[0]) < self.cache_sec:
            return cached[1][:limit]

        res = self._get(f"/metatags/{tag}", {"playlistsCount": limit})
        out: List[dict] = self._parse_metatag_playlists(res)
        if not out:
            # запасные варианты запроса: другое имя параметра / постраничный endpoint
            for path, params in (
                (f"/metatags/{tag}", {"playlists-count": limit}),
                (f"/metatags/{tag}/playlists", {"offset": 0, "limit": limit}),
            ):
                out = self._parse_metatag_playlists(self._get(path, params))
                if out:
                    log.info("Kion tag %s: плейлисты получены через %s", tag, path)
                    break
        if out:
            # пустой ответ не кэшируем: иначе разовый сбой «залипает» на cache_sec
            self._metatag_pl_cache[tag] = (now, out)
        else:
            log.warning("Kion tag %s: плейлистов не найдено", tag)
        return out[:limit]

    @staticmethod
    def _parse_metatag_playlists(res) -> List[dict]:
        out: List[dict] = []
        if not res:
            return out
        if isinstance(res, list):
            playlists = res
        else:
            playlists = res.get("playlists") or []
        for item in playlists:
            pl = item.get("playlist") if isinstance(item, dict) and isinstance(item.get("playlist"), dict) else item
            if not isinstance(pl, dict):
                continue
            owner = pl.get("owner") or {}
            owner_id = pl.get("uid") or owner.get("uid") or owner.get("login")
            uuid = pl.get("playlistUuid") or pl.get("uuid")
            if not uuid and (owner_id is None or pl.get("kind") is None):
                continue
            out.append({
                "title": pl.get("title") or "Unknown",
                "owner": owner.get("name") or "",
                "kind": pl.get("kind"),
                "owner_id": owner_id,
                "track_count": pl.get("trackCount") or 0,
                "uuid": uuid,
            })
        # ответ вида {"ids": [{"uid":..,"kind":..}]} без развёрнутых плейлистов
        if not out and isinstance(res, dict):
            for it in res.get("ids") or []:
                if isinstance(it, dict) and it.get("uid") is not None and it.get("kind") is not None:
                    out.append({"title": f"Плейлист {it['kind']}", "owner": "", "kind": it["kind"],
                                "owner_id": it["uid"], "track_count": 0, "uuid": None})
        return out

    def get_liked_tracks(self) -> List[KionTrack]:
        """Треки из «Мне нравится» текущего аккаунта."""
        if not self.enabled:
            return []
        uid = self._ensure_uid()
        if not uid:
            log.warning("Kion: не удалось определить uid для коллекции")
            return []
        res = self._get(f"/users/{uid}/likes/tracks")
        if not isinstance(res, dict):
            return []
        # реальный ответ: {"library": {"tracks": [{"id", "albumId", "timestamp"}, ...]}}
        lib = res.get("library") if isinstance(res.get("library"), dict) else res
        entries = (lib.get("tracks") or [])[:500]
        return self._tracks_from_entries(entries)

    def get_user_playlists(self) -> List[dict]:
        """Список пользовательских плейлистов для раздела «Коллекция»."""
        if not self.enabled:
            return []
        uid = self._ensure_uid()
        if not uid:
            log.warning("Kion: не удалось определить uid для плейлистов")
            return []
        res = self._get(f"/users/{uid}/playlists/list")
        if not isinstance(res, list):
            # Совместимость с вариантами API, возвращающими объект.
            res = (res or {}).get("playlists", []) if isinstance(res, dict) else []
        out = []
        for pl in res:
            if not isinstance(pl, dict):
                continue
            kind = pl.get("kind")
            owner = pl.get("uid") or (pl.get("owner") or {}).get("uid") or uid
            if kind is None:
                continue
            out.append({
                "title": pl.get("title") or f"Плейлист {kind}",
                "kind": kind,
                "owner_id": owner,
                "track_count": pl.get("trackCount") or 0,
            })
        return out

    def get_liked_playlists(self) -> List[dict]:
        """Плейлисты, которым пользователь поставил лайк."""
        if not self.enabled:
            return []
        uid = self._ensure_uid()
        if not uid:
            log.warning("Kion: не удалось определить uid для понравившихся плейлистов")
            return []
        res = self._get(f"/users/{uid}/likes/playlists")
        if isinstance(res, list):
            items = res
        elif isinstance(res, dict):
            items = res.get("playlists") or res.get("items") or []
        else:
            return []
        out = []
        for item in items:
            pl = item.get("playlist") if isinstance(item, dict) and isinstance(item.get("playlist"), dict) else item
            if not isinstance(pl, dict):
                continue
            owner = pl.get("owner") or {}
            owner_id = pl.get("uid") or owner.get("uid")
            kind = pl.get("kind")
            if owner_id is None or kind is None:
                continue
            out.append({
                "title": pl.get("title") or "Плейлист",
                "kind": kind,
                "owner_id": owner_id,
                "track_count": pl.get("trackCount") or 0,
            })
        return out

    def get_playlist_tracks(self, owner_id: str = "", kind=None, uuid: str = "") -> List[KionTrack]:
        """Треки плейлиста по uuid или owner_id:kind."""
        cache_key = uuid or f"{owner_id}:{kind}"
        if not cache_key or cache_key == ":":
            return []
        now = time.time()
        cached = self._playlist_cache.get(cache_key)
        if cached and (now - cached[0]) < self.cache_sec:
            return cached[1]

        res = None
        if uuid:
            res = self._get(f"/playlist/{uuid}")
            if not res:
                res = self._get(f"/playlists/{uuid}")
        if not res and owner_id is not None and kind is not None:
            # rich-tracks=true — иначе API отдаёт только {id, albumId} без названий,
            # и плейлист выглядит пустым
            res = self._get(f"/users/{owner_id}/playlists/{kind}", {"rich-tracks": "true"})
            if not res:
                res = self._get(f"/users/{owner_id}/playlists", {"kinds": kind, "rich-tracks": "true"})
                if isinstance(res, list):
                    res = res[0] if res else None

        out: List[KionTrack] = []
        if res:
            # иногда плейлист вложен в playlist
            pl = res.get("playlist") if isinstance(res, dict) and "playlist" in res else res
            out = self._tracks_from_entries((pl or {}).get("tracks") or [])
        if out:
            self._playlist_cache[cache_key] = (now, out)
        else:
            log.warning("Kion playlist %s: треков не получено", cache_key)
        return out

    # ---------- Моя волна (rotor) ----------

    WAVE_STATION = "user:onyourwave"

    def _station_path(self, station: str) -> str:
        """station id в path. Двоеточие оставляем — так же, как официальный клиент."""
        # safe=':' — иначе %3A ломает /rotor/station/user:onyourwave/...
        return quote(str(station), safe=":")
    WAVE_MAX = 80
    WAVE_TTL = 1800
    WAVE_STALE_SEC = 300  # 5 мин: как Deezer Flow — свежая сессия после паузы; DLNA Browse не сбрасывает раньше

    # Пресеты настроек (как в приложении: характер / настроение / язык)
    WAVE_PRESETS = [
        {"id": "default", "title": "▶ Слушать Мою волну", "settings": {}},
        {"id": "shake", "title": "🔄 Встряхнуть Мою волну", "settings": {"_shake": True}},
        {"id": "discover", "title": "Незнакомое", "settings": {"diversity": "discover"}},
        {"id": "favorite", "title": "Любимое", "settings": {"diversity": "favorite"}},
        {"id": "popular", "title": "Популярное", "settings": {"diversity": "popular"}},
        {"id": "calm", "title": "Спокойное", "settings": {"moodEnergy": "calm"}},
        {"id": "active", "title": "Бодрое", "settings": {"moodEnergy": "active"}},
        {"id": "fun", "title": "Весёлое", "settings": {"moodEnergy": "fun"}},
        {"id": "sad", "title": "Грустное", "settings": {"moodEnergy": "sad"}},
        {"id": "russian", "title": "На русском", "settings": {"language": "russian"}},
        {"id": "not_russian", "title": "Иностранные", "settings": {"language": "not-russian"}},
        {"id": "without_words", "title": "Без слов", "settings": {"language": "without-words"}},
    ]


    # ---------- разделы сайта: чарт / лента / подборки / подкасты ----------

    def get_chart_tracks(self, limit: int = 100) -> List[KionTrack]:
        """Официальный чарт треков (/chart/tracks)."""
        if not self.enabled:
            return []
        res = self._get("/chart/tracks")
        if not isinstance(res, dict):
            return []
        positions = res.get("chartPositions") or []
        out: List[KionTrack] = []
        for pos in positions[:limit]:
            if not isinstance(pos, dict):
                continue
            tr = pos.get("track") or pos
            track = self._track_from(tr) if isinstance(tr, dict) else None
            if track:
                out.append(track)
        return out

    def get_real_chart_tracks(self) -> List[KionTrack]:
        """Плейлист «Настоящий чарт» (редакция)."""
        # известный редакторский плейлист на ЯМ/КИОН
        return self.get_playlist_tracks(owner_id="324504548", kind=1480)

    def get_collection_playlists(self) -> List[dict]:
        """Подборки: соседние чартовые плейлисты + несколько редакционных."""
        if not self.enabled:
            return []
        out: List[dict] = []
        seen = set()

        def add_pl(pl: dict):
            if not isinstance(pl, dict):
                return
            owner = pl.get("owner") or {}
            owner_id = pl.get("uid") or owner.get("uid")
            kind = pl.get("kind")
            title = pl.get("title") or "Подборка"
            if owner_id is None or kind is None:
                return
            key = f"{owner_id}:{kind}"
            if key in seen:
                return
            seen.add(key)
            out.append({
                "title": title,
                "kind": kind,
                "owner_id": owner_id,
                "track_count": pl.get("trackCount") or 0,
                "uuid": pl.get("playlistUuid") or "",
            })

        # 1) otherChartPlaylists из чарта
        res = self._get("/chart/tracks")
        if isinstance(res, dict):
            for pl in (res.get("otherChartPlaylists") or []):
                add_pl(pl if isinstance(pl, dict) else {})

        # 2) фиксированные редакционные
        editorial = [
            ("324504548", 1480, "Настоящий чарт"),
            ("103372440", 1102, "100 хитов русской поп-музыки"),
            ("139954184", 1029, "Хиты FM"),
            ("103372440", 1829, "Топ распознаваний"),
            ("1987736780", 1598, "Чарт КИОН Музыки"),
        ]
        for oid, kind, title in editorial:
            key = f"{oid}:{kind}"
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "title": title,
                "kind": kind,
                "owner_id": oid,
                "track_count": 0,
                "uuid": "",
            })
        return out

    def get_feed_actual(self) -> dict:
        """Актуально сейчас: tracksToPlay + события ленты.

        Возвращает {
          'tracks': [KionTrack...],
          'events': [{'id','title','tracks':[KionTrack...]}, ...]
        }
        """
        empty = {"tracks": [], "events": []}
        if not self.enabled:
            return empty
        res = self._get("/feed")
        if not isinstance(res, dict):
            return empty
        days = res.get("days") or []
        if not days or not isinstance(days[0], dict):
            return empty
        day = days[0]
        tracks = self._tracks_from_entries(day.get("tracksToPlay") or [])
        events = []
        for i, ev in enumerate(day.get("events") or []):
            if not isinstance(ev, dict):
                continue
            etracks = self._tracks_from_entries(ev.get("tracks") or [])
            if not etracks:
                continue
            # title из блоков type/text
            title_parts = []
            raw_title = ev.get("title")
            if isinstance(raw_title, list):
                for part in raw_title:
                    if isinstance(part, dict):
                        title_parts.append(str(part.get("text") or part.get("name") or ""))
                    else:
                        title_parts.append(str(part))
            elif isinstance(raw_title, str):
                title_parts.append(raw_title)
            title = "".join(title_parts).strip() or (ev.get("type") or f"Подборка {i+1}")
            # укоротить
            if len(title) > 80:
                title = title[:77] + "…"
            events.append({
                "id": str(ev.get("id") or i),
                "title": title,
                "tracks": etracks,
            })
        return {"tracks": tracks, "events": events}

    def get_podcast_chart(self, limit: int = 50) -> List[dict]:
        """Чарт подкастов: список альбомов-подкастов [{id,title,artist,track_count}]."""
        if not self.enabled:
            return []
        res = self._get("/chart/podcasts")
        if not isinstance(res, dict):
            return []
        out = []
        for pos in (res.get("chartPositions") or [])[:limit]:
            if not isinstance(pos, dict):
                continue
            alb = pos.get("album") or pos
            if not isinstance(alb, dict) or not alb.get("id"):
                continue
            artists = alb.get("artists") or []
            anames = []
            for a in artists:
                if isinstance(a, dict) and a.get("name"):
                    anames.append(a["name"])
            out.append({
                "id": str(alb["id"]),
                "title": alb.get("title") or f"Подкаст {alb['id']}",
                "artist": ", ".join(anames),
                "track_count": alb.get("trackCount") or 0,
            })
        return out

    def list_wave_presets(self) -> list:
        """Базовые пресеты + станции ротора / landing (best-effort)."""
        out = list(self.WAVE_PRESETS)
        seen = {self.WAVE_STATION}

        # Dashboard персональных станций
        try:
            dash = self._get("/rotor/stations/dashboard")
            stations = (dash or {}).get("stations") or []
            for item in stations:
                st = (item.get("station") or item) if isinstance(item, dict) else None
                if not isinstance(st, dict):
                    continue
                sid_obj = st.get("id") or {}
                if isinstance(sid_obj, dict):
                    sid = f"{sid_obj.get('type', '')}:{sid_obj.get('tag', '')}"
                else:
                    sid = str(sid_obj or "")
                if not sid or sid in seen or sid.startswith("user:"):
                    continue
                title = (
                    st.get("name")
                    or item.get("rupTitle")
                    or item.get("rup_title")
                    or sid
                )
                seen.add(sid)
                out.append({"id": f"st:{sid}", "title": title, "settings": {}, "station": sid})
        except Exception as e:
            log.debug("rotor dashboard: %s", e)

        # Список станций (жанры и т.п.)
        try:
            lst = self._get("/rotor/stations/list", {"language": "ru"})
            if isinstance(lst, list):
                for item in lst[:40]:
                    st = (item.get("station") or item) if isinstance(item, dict) else None
                    if not isinstance(st, dict):
                        continue
                    sid_obj = st.get("id") or {}
                    if isinstance(sid_obj, dict):
                        sid = f"{sid_obj.get('type', '')}:{sid_obj.get('tag', '')}"
                    else:
                        sid = str(sid_obj or "")
                    if not sid or sid in seen or sid.startswith("user:"):
                        continue
                    title = st.get("name") or sid
                    seen.add(sid)
                    out.append({"id": f"st:{sid}", "title": title, "settings": {}, "station": sid})
        except Exception as e:
            log.debug("rotor list: %s", e)

        # Landing scenarios
        for src, prefix in (("waves", "sc"), ("mixes-waves", "mix")):
            try:
                scenarios = self._get_landing_wave_scenarios(src)
            except Exception as e:
                log.debug("landing %s: %s", src, e)
                scenarios = []
            for sc in scenarios:
                title = sc.get("title") or sc.get("name") or ""
                station = sc.get("station_id") or sc.get("stationId") or ""
                seeds = sc.get("seeds") or []
                sid = station
                if not sid and seeds:
                    sid = seeds[0] if isinstance(seeds[0], str) else str(seeds[0])
                if not sid or sid in seen:
                    continue
                seen.add(sid)
                out.append({
                    "id": f"{prefix}:{sid}",
                    "title": title or sid,
                    "settings": {},
                    "station": sid,
                })
        return out

    def _get_landing_wave_scenarios(self, block: str) -> list:
        """Сценарии из /landing-blocks/{waves|mixes-waves}."""
        res = self._get(f"/landing-blocks/{block}")
        if not res:
            return []
        waves = res.get("waves") or res.get("items") or []
        out = []
        if isinstance(waves, list):
            for cat in waves:
                if not isinstance(cat, dict):
                    continue
                # category may contain items/stations
                items = cat.get("items") or cat.get("stations") or cat.get("waves") or []
                if items:
                    for it in items:
                        if not isinstance(it, dict):
                            continue
                        st = it.get("stationId") or it.get("station_id") or it.get("id")
                        if isinstance(st, dict):
                            st = f"{st.get('type', '')}:{st.get('tag', '')}"
                        title = it.get("title") or it.get("name") or it.get("header") or ""
                        if st and title:
                            out.append({"title": title, "station_id": str(st), "seeds": it.get("seeds") or []})
                else:
                    # category itself is a scenario
                    st = cat.get("stationId") or cat.get("station_id") or cat.get("id")
                    if isinstance(st, dict):
                        st = f"{st.get('type', '')}:{st.get('tag', '')}"
                    title = cat.get("title") or cat.get("name") or ""
                    if st and title:
                        out.append({"title": title, "station_id": str(st), "seeds": cat.get("seeds") or []})
        return out

    def _post_json(self, path: str, payload: Optional[dict] = None) -> Optional[dict]:
        """POST JSON body (для сессий радио)."""
        if not self.token:
            return None
        try:
            r = self.session.post(
                f"{self.api_base}{path}",
                json=payload or {},
                timeout=25,
                headers={"Content-Type": "application/json"},
            )
            if r.status_code >= 400:
                log.warning("Kion POST JSON %s -> HTTP %s: %s", path, r.status_code, r.text[:300])
                return None
            body = r.json()
            if body.get("error"):
                log.warning("Kion API %s error: %s", path, body.get("error"))
                return None
            return body.get("result", body)
        except Exception as e:
            log.warning("Kion POST JSON %s: %s", path, e)
            return None

    def _seeds_for_preset(self, preset_id: str, station: str, settings: dict) -> list:
        """Сиды для /rotor/session/new — как в официальном клиенте и MarshalX."""
        seeds = [station or self.WAVE_STATION]
        # setting* сиды (diversity / language / mood)
        div = settings.get("diversity")
        if div and div not in ("default", "all"):
            seeds.append(f"settingDiversity:{div}")
        lang = settings.get("language")
        if lang:
            seeds.append(f"settingLanguage:{lang}")
        mood = settings.get("moodEnergy")
        if mood and mood not in ("all",):
            seeds.append(f"settingMoodEnergy:{mood}")
        return seeds

    def _resolve_wave_preset(self, preset_id: str):
        """preset_id → (preset_id, station, settings, seeds)."""
        preset_id = preset_id or "default"
        station = self.WAVE_STATION
        settings: dict = {}
        if preset_id == "shake":
            return "default", station, {"_shake": True}, [station]
        for pr in self.WAVE_PRESETS:
            if pr["id"] == preset_id:
                settings = dict(pr.get("settings") or {})
                if pr.get("station"):
                    station = pr["station"]
                break
        else:
            if preset_id.startswith(("sc:", "mix:", "st:")):
                station = preset_id.split(":", 1)[1]
            elif ":" in preset_id and preset_id not in ("default",):
                station = preset_id
        seeds = self._seeds_for_preset(preset_id, station, settings)
        return preset_id, station, settings, seeds

    def ensure_wave_ahead(self, track_id: str, ahead: int = 15) -> int:
        """Если track_id в конце какой-то волны — догрузить треки. Возвращает сколько добавлено."""
        if not hasattr(self, "_wave_cache") or not self._wave_cache:
            return 0
        tid = str(track_id)
        total_added = 0
        for preset_id, cache in list(self._wave_cache.items()):
            tracks = cache.get("tracks") or []
            ids = [str(t.id) for t in tracks]
            if tid not in ids:
                continue
            pos = ids.index(tid)
            remaining = len(ids) - pos - 1
            target = max(ahead, 12)
            if remaining >= target:
                continue
            before = len(tracks)
            for _ in range(8):
                if len(cache.get("tracks") or []) >= self.WAVE_MAX:
                    break
                self.get_wave_tracks(preset_id=preset_id, append=True)
                after = len(cache.get("tracks") or [])
                if after == before:
                    break
                before = after
                # достаточно запаса — выходим раньше
                if after - pos - 1 >= target:
                    break
            added = len(cache.get("tracks") or []) - len(tracks)
            if added > 0:
                total_added += added
                log.info(
                    "Kion wave ensure_ahead preset=%s track=%s remaining_was=%s added=%s",
                    preset_id, tid, remaining, added,
                )
        return total_added

    def get_wave_tracks(self, preset_id: str = "default", append: bool = False) -> List[KionTrack]:
        """Треки «Моей волны» через сессии радио (rotor/session) — как в приложении и Deezer Flow.

        Сессия держит контекст: лайки/скипы/дослушивания влияют на следующие партии.
        Старый /rotor/station/.../tracks оставляем только как fallback.
        """
        preset_id, station, settings, seeds = self._resolve_wave_preset(preset_id)
        now = time.time()

        if not hasattr(self, "_wave_cache"):
            self._wave_cache = {}

        # «Встряхнуть» — сбросить сессию и начать заново
        if settings.get("_shake"):
            self._wave_cache.pop("default", None)
            preset_id = "default"
            station = self.WAVE_STATION
            settings = {}
            seeds = [station]

        cache = self._wave_cache.get(preset_id)
        if cache is None:
            cache = {
                "tracks": [],
                "batch_id": None,
                "session_id": None,
                "last_id": None,
                "played_ids": [],
                "issued_ids": [],
                "ts": 0.0,
                "station": station,
                "seeds": seeds,
                "last_access": 0.0,
                "last_fetch": 0.0,
                "exhausted_until": 0.0,
            }
            self._wave_cache[preset_id] = cache
        cache["station"] = station
        cache["seeds"] = seeds
        cache.setdefault("issued_ids", [])
        cache.setdefault("played_ids", [])

        # Idle-сброс (как Deezer Flow): долгая пауза → новая сессия
        last_access = float(cache.get("last_access") or 0.0)
        if cache.get("tracks") and last_access and (now - last_access) > self.WAVE_STALE_SEC:
            log.info(
                "Kion wave preset=%s idle %.0fs > %ds — new session",
                preset_id, now - last_access, self.WAVE_STALE_SEC,
            )
            cache["tracks"] = []
            cache["batch_id"] = None
            cache["session_id"] = None
            cache["last_id"] = None
            cache["played_ids"] = []
            cache["issued_ids"] = []
            cache["ts"] = 0.0

        cache["last_access"] = now

        # Есть кэш и не просили догрузку — отдать как есть
        if cache["tracks"] and not append:
            if (now - float(cache.get("ts") or 0)) < self.WAVE_TTL:
                return list(cache["tracks"])

        if len(cache["tracks"]) >= self.WAVE_MAX:
            return list(cache["tracks"])

        # Не спамим API: короткая пауза после пустого ответа
        if append and float(cache.get("exhausted_until") or 0) > now:
            # если уже есть хотя бы 12 треков — можно подождать; иначе догружаем
            if len(cache.get("tracks") or []) >= 12:
                return list(cache["tracks"])
        last_fetch = float(cache.get("last_fetch") or 0)
        if append and last_fetch and (now - last_fetch) < 0.8:
            return list(cache["tracks"])

        cache["last_fetch"] = now

        # --- Сессия радио (предпочтительный путь) ---
        session_id = cache.get("session_id")
        added = 0

        if not session_id or not cache["tracks"]:
            # Новая сессия
            payload = {
                "seeds": seeds,
                "includeTracksInResponse": True,
                "includeWaveModel": True,
                "interactive": True,
            }
            log.info("Kion wave NEW session seeds=%s preset=%s", seeds, preset_id)
            res = self._post_json("/rotor/session/new", payload)
            if res:
                session_id = (
                    res.get("radioSessionId")
                    or res.get("radio_session_id")
                    or res.get("sessionId")
                    or res.get("id")
                )
                batch_id = res.get("batchId") or res.get("batch_id")
                if session_id:
                    cache["session_id"] = str(session_id)
                if batch_id:
                    cache["batch_id"] = str(batch_id)
                sequence = res.get("sequence") or []
                added = self._ingest_sequence(cache, sequence)
                # radioStarted
                if session_id:
                    self._session_feedback(
                        str(session_id),
                        "radioStarted",
                        batch_id=cache.get("batch_id"),
                    )
            else:
                log.warning("Kion wave session/new failed — fallback to station API")
                return self._wave_tracks_legacy(preset_id, station, settings, cache, append)

        elif append and session_id:
            # Следующая партия. Важно: queue = ВСЕ уже выданные треки
            # (как в MarshalX wave_radio: played_ids.append при взятии из sequence),
            # а не только реально прослушанные. Иначе API часто возвращает 0.
            queue = self._wave_queue_ids(cache)
            payload = {"queue": queue[-40:]}
            log.info(
                "Kion wave session tracks session=%s queue_len=%s preset=%s",
                session_id, len(payload["queue"]), preset_id,
            )
            res = self._post_json(f"/rotor/session/{session_id}/tracks", payload)
            if not res:
                # сессия могла протухнуть — пересоздаём
                log.warning("Kion wave session tracks empty — recreating session")
                cache["session_id"] = None
                cache["batch_id"] = None
                return self.get_wave_tracks(preset_id=preset_id, append=False)
            batch_id = res.get("batchId") or res.get("batch_id")
            if batch_id:
                cache["batch_id"] = str(batch_id)
            sequence = res.get("sequence") or []
            added = self._ingest_sequence(cache, sequence)
            # если session API вернул 0 — один раз пробуем legacy с queue
            if added == 0:
                log.info("Kion wave session +0 — try legacy station tracks")
                legacy_before = len(cache["tracks"])
                self._wave_tracks_legacy(preset_id, station, settings, cache, append=True)
                added = len(cache["tracks"]) - legacy_before

        if added > 0:
            cache["exhausted_until"] = 0.0
        elif append:
            cache["exhausted_until"] = now + 8.0
            log.info(
                "Kion wave +0 (session=%s) — pause 8s, have=%s",
                cache.get("session_id"), len(cache["tracks"]),
            )

        cache["ts"] = now
        log.info(
            "Kion wave station=%s session=%s: +%s tracks, total=%s batch=%s",
            station, cache.get("session_id"), added, len(cache["tracks"]), cache.get("batch_id"),
        )
        return list(cache["tracks"])

    def _wave_queue_ids(self, cache: dict) -> List[str]:
        """ID для параметра queue у /rotor/session/.../tracks.

        API ожидает уже «взятые» из последовательности треки (как в официальном
        клиенте и MarshalX), иначе следующая партия часто пустая.
        Формат: trackId или trackId:albumId — последний надёжнее.
        """
        seen = set()
        out: List[str] = []
        # Сначала реально прослушанные (feedback)
        for tid in cache.get("played_ids") or []:
            s = str(tid)
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        # Затем все уже выданные в кэш (Browse / preload)
        for tr in cache.get("tracks") or []:
            if not tr or not tr.id:
                continue
            # предпочитаем id:albumId если есть album_id
            if tr.album_id:
                key = f"{tr.id}:{tr.album_id}"
            else:
                key = str(tr.id)
            # не дублируем, если уже есть «голый» id или составной
            if str(tr.id) in seen or key in seen:
                continue
            seen.add(str(tr.id))
            seen.add(key)
            out.append(key)
        if not out and cache.get("last_id"):
            out.append(str(cache["last_id"]))
        return out

    def _ingest_sequence(self, cache: dict, sequence: list) -> int:
        """Добавить треки из sequence в cache, без дублей. Возвращает число новых."""
        added = 0
        for item in sequence or []:
            track_obj = None
            if isinstance(item, dict):
                track_obj = item.get("track") or item.get("trackData") or item
            if not isinstance(track_obj, dict):
                continue
            if not (track_obj.get("id") or track_obj.get("realId")):
                continue
            tr = self._track_from(track_obj)
            if not tr:
                continue
            if any(x.id == tr.id for x in cache["tracks"]):
                continue
            cache["tracks"].append(tr)
            cache["last_id"] = tr.id
            # issued_ids — для queue при следующем запросе (ещё до реального play)
            issued = cache.setdefault("issued_ids", [])
            qid = f"{tr.id}:{tr.album_id}" if tr.album_id else str(tr.id)
            if qid not in issued:
                issued.append(qid)
            added += 1
            if len(cache["tracks"]) >= self.WAVE_MAX:
                break
        return added

    def _wave_tracks_legacy(self, preset_id, station, settings, cache, append) -> List[KionTrack]:
        """Fallback на старый /rotor/station/.../tracks."""
        now = time.time()
        if not append and not cache["tracks"] and settings:
            self._apply_wave_settings_legacy(station, settings)
        # queue: полный список уже выданных id (не только last_id)
        qids = self._wave_queue_ids(cache)
        params: dict = {"settings2": "true"}
        if qids:
            # старый API принимает один id или comma-separated
            params["queue"] = qids[-1]
        path = f"/rotor/station/{self._station_path(station)}/tracks"
        res = self._get(path, params)
        if not res and qids:
            res = self._get(path, {"queue": str(qids[-1])})
        if not res:
            return list(cache["tracks"])
        batch_id = res.get("batchId") or res.get("batch_id")
        if batch_id:
            cache["batch_id"] = batch_id
            try:
                self._post_feedback_legacy("radioStarted", station=station, batch_id=batch_id)
            except Exception:
                pass
        # session id может прийти и в station API
        sid = res.get("radioSessionId") or res.get("radio_session_id")
        if sid and not cache.get("session_id"):
            cache["session_id"] = str(sid)
        added = self._ingest_sequence(cache, res.get("sequence") or [])
        if added == 0 and append:
            cache["exhausted_until"] = now + 8.0
        cache["ts"] = now
        return list(cache["tracks"])

    def _apply_wave_settings_legacy(self, station: str, settings: dict) -> None:
        if not settings or settings.get("_shake"):
            return
        data = {"type": "rotor"}
        data["moodEnergy"] = settings.get("moodEnergy") or "all"
        data["diversity"] = settings.get("diversity") or "default"
        if settings.get("language"):
            data["language"] = settings["language"]
        try:
            url = f"{self.api_base}/rotor/station/{self._station_path(station)}/settings3"
            r = self.session.post(url, json=data, timeout=15)
            if r.status_code >= 400:
                url2 = f"{self.api_base}/rotor/station/{self._station_path(station)}/settings2"
                self.session.post(url2, json=data, timeout=15)
        except Exception as e:
            log.debug("wave settings: %s", e)

    def _session_feedback(
        self,
        session_id: str,
        event_type: str,
        track_id: str = "",
        total_played: float = 0.0,
        batch_id: Optional[str] = None,
    ) -> None:
        """Обратная связь в сессию радио — двигает алгоритм как в приложении."""
        if not self.token or not session_id:
            return
        import datetime
        event: dict = {
            "type": event_type,
            "timestamp": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        }
        if track_id:
            event["trackId"] = str(track_id)
        if total_played and event_type in ("trackFinished", "skip", "dislike"):
            event["totalPlayedSeconds"] = float(total_played)
        body: dict = {"event": event}
        if batch_id:
            body["batchId"] = str(batch_id)
        try:
            path = f"/rotor/session/{session_id}/feedback"
            r = self.session.post(
                f"{self.api_base}{path}",
                json=body,
                timeout=10,
                headers={"Content-Type": "application/json"},
            )
            if r.status_code >= 400:
                log.debug("session feedback %s -> %s %s", event_type, r.status_code, r.text[:150])
        except Exception as e:
            log.debug("session feedback %s: %s", event_type, e)

    def _post_feedback_legacy(self, type_: str, track_id: str = "", total_played: float = 0,
                       station: str = "", batch_id: str = "") -> None:
        if not self.token:
            return
        import datetime
        st = station or self.WAVE_STATION
        body = {
            "type": type_,
            "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
            "from": "dlna-emu",
        }
        bid = batch_id
        if not bid and hasattr(self, "_wave_cache"):
            for c in self._wave_cache.values():
                if c.get("batch_id"):
                    bid = c["batch_id"]
                    break
        if bid:
            body["batch-id"] = bid
        if track_id:
            body["trackId"] = track_id
        if total_played:
            body["totalPlayedSeconds"] = total_played
        try:
            url = f"{self.api_base}/rotor/station/{self._station_path(st)}/feedback"
            self.session.post(url, json=body, timeout=10)
        except Exception as e:
            log.debug("wave feedback %s: %s", type_, e)

    def wave_track_started(self, track_id: str) -> None:
        """Сообщить, что трек из волны начал играть (сдвигает алгоритм)."""
        tid = str(track_id or "").strip()
        if not tid:
            return
        session_id, batch_id, preset_id = self._wave_context_for_track(tid)
        if session_id:
            self._session_feedback(session_id, "trackStarted", track_id=tid, batch_id=batch_id)
        else:
            station, bid = self._wave_station_for_track(tid)
            self._post_feedback_legacy("trackStarted", track_id=tid, station=station, batch_id=bid)
        # last_access + played_ids
        if hasattr(self, "_wave_cache"):
            for c in self._wave_cache.values():
                ids = [str(x.id) for x in (c.get("tracks") or [])]
                if tid in ids:
                    c["last_access"] = time.time()
                    played = c.setdefault("played_ids", [])
                    if tid not in played:
                        played.append(tid)
                    break

    def wave_track_finished(self, track_id: str, played_seconds: float = 30.0) -> None:
        """Трек дослушали — Яндекс подмешает другие."""
        tid = str(track_id or "").strip()
        if not tid:
            return
        session_id, batch_id, _ = self._wave_context_for_track(tid)
        if session_id:
            self._session_feedback(
                session_id,
                "trackFinished",
                track_id=tid,
                total_played=float(played_seconds or 30.0),
                batch_id=batch_id,
            )
        else:
            station, bid = self._wave_station_for_track(tid)
            self._post_feedback_legacy(
                "trackFinished",
                track_id=tid,
                total_played=float(played_seconds or 30.0),
                station=station,
                batch_id=bid,
            )

    def wave_track_skipped(self, track_id: str, played_seconds: float = 5.0) -> None:
        """Скип — сильный сигнал для алгоритма (как в Deezer)."""
        tid = str(track_id or "").strip()
        if not tid:
            return
        session_id, batch_id, _ = self._wave_context_for_track(tid)
        if session_id:
            self._session_feedback(
                session_id,
                "skip",
                track_id=tid,
                total_played=float(played_seconds or 5.0),
                batch_id=batch_id,
            )
        else:
            station, bid = self._wave_station_for_track(tid)
            self._post_feedback_legacy(
                "skip",
                track_id=tid,
                total_played=float(played_seconds or 5.0),
                station=station,
                batch_id=bid,
            )

    def _wave_context_for_track(self, track_id: str):
        """→ (session_id, batch_id, preset_id)."""
        if not hasattr(self, "_wave_cache"):
            return None, None, None
        tid = str(track_id)
        for pid, c in self._wave_cache.items():
            ids = [str(x.id) for x in (c.get("tracks") or [])]
            if tid in ids:
                return c.get("session_id"), c.get("batch_id"), pid
        return None, None, None

    def _wave_station_for_track(self, track_id: str):
        station = self.WAVE_STATION
        batch_id = ""
        if not hasattr(self, "_wave_cache"):
            return station, batch_id
        tid = str(track_id)
        for c in self._wave_cache.values():
            ids = [str(x.id) for x in (c.get("tracks") or [])]
            if tid in ids:
                station = c.get("station") or station
                batch_id = c.get("batch_id") or ""
                break
        return station, batch_id

    def reset_wave(self, preset_id: Optional[str] = None) -> None:
        if not hasattr(self, "_wave_cache"):
            self._wave_cache = {}
        if preset_id:
            self._wave_cache.pop(preset_id, None)
        else:
            self._wave_cache.clear()
        self._wave_tracks = []
        self._wave_batch_id = None
        self._wave_last_track_id = None
        self._wave_ts = 0.0
