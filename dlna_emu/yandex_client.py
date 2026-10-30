"""Яндекс.Музыка — поиск и стрим по OAuth-токену (неофициальный API)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import requests
from urllib.parse import quote

log = logging.getLogger("dlna_emu.yandex")

API = "https://api.music.yandex.net"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
CLIENT_HEADER = "YandexMusicAndroid/24023621"
# Для /get-file-info (FLAC) нужна подпись десктоп-клиента
DESKTOP_CLIENT = "YandexMusicDesktopAppWindows/5.95.0"
DESKTOP_SIGN_KEY = "kzqU4XhfCaY6B6JTHODeq5"
FILE_INFO_CODECS = ["flac", "flac-mp4", "aac-mp4", "he-aac-mp4", "aac", "he-aac", "mp3"]
SIGN_SALT = "XGRlBW9FXlekgbPrRHuSiA"


@dataclass
class YaTrack:
    id: str
    title: str
    artist: str
    duration: int = 0
    album: str = ""
    album_id: str = ""
    cover_uri: str = ""


class YandexMusicClient:
    def __init__(
        self,
        token: str = "",
        quality: str = "mp3_320",
        cache_sec: int = 300,
        stream_mode: str = "auto",
        buffer_timeout: float = 6.0,
    ):
        self.token = (token or "").strip()
        self.quality = (quality or "mp3_320").lower()
        self._has_plus = False
        # tid -> (ts, flac_доступен): чтобы в списке заявлять реальный формат
        self._flac_cache: Dict[str, Tuple[float, bool]] = {}
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
            "Accept-Language": "ru-RU,ru;q=0.9",
        })
        if self.token:
            self.session.headers["Authorization"] = f"OAuth {self.token}"
        self._account_ok: Optional[bool] = None
        self._uid: Optional[str] = None
        self._user_name: Optional[str] = None
        self._tracks_cache: Dict[str, Tuple[float, List[YaTrack]]] = {}
        self._artist_cache: Dict[str, Tuple[float, List[YaTrack]]] = {}
        self._album_cache: Dict[str, Tuple[float, List[YaTrack]]] = {}
        self._metatags_cache: Tuple[float, list] = (0.0, [])
        self._metatag_pl_cache: Dict[str, Tuple[float, list]] = {}
        self._cover_map: Dict[str, str] = {}
        self._playlist_cache: Dict[str, Tuple[float, List[YaTrack]]] = {}
        # Моя волна (rotor)
        self._wave_tracks: List[YaTrack] = []
        self._wave_batch_id: Optional[str] = None
        self._wave_last_track_id: Optional[str] = None
        self._wave_ts: float = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def _get(self, path: str, params: Optional[dict] = None) -> Optional[dict]:
        if not self.token:
            return None
        try:
            r = self.session.get(f"{API}{path}", params=params or {}, timeout=25)
            if r.status_code == 401:
                log.warning("Yandex Music: токен недействителен (401)")
                self._account_ok = False
                return None
            if r.status_code >= 400:
                log.warning("Yandex GET %s -> HTTP %s: %s", path, r.status_code, r.text[:200])
                return None
            data = r.json()
            if data.get("error"):
                log.warning("Yandex API %s error: %s", path, data.get("error"))
                return None
            return data.get("result", data)
        except Exception as e:
            log.warning("Yandex GET %s: %s", path, e)
            return None

    def _post(self, path: str, data: Optional[dict] = None) -> Optional[dict]:
        if not self.token:
            return None
        try:
            r = self.session.post(f"{API}{path}", data=data or {}, timeout=25)
            if r.status_code >= 400:
                log.warning("Yandex POST %s -> HTTP %s: %s", path, r.status_code, r.text[:200])
                return None
            body = r.json()
            if body.get("error"):
                log.warning("Yandex API %s error: %s", path, body.get("error"))
                return None
            return body.get("result", body)
        except Exception as e:
            log.warning("Yandex POST %s: %s", path, e)
            return None

    def _tracks_by_ids(self, ids: List[str]) -> Dict[str, YaTrack]:
        """Полные данные треков по id (для плейлистов, где пришли только короткие записи)."""
        out: Dict[str, YaTrack] = {}
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

    def _tracks_from_entries(self, entries: list) -> List[YaTrack]:
        """Элементы плейлиста → треки. Если у элемента нет вложенного track
        (короткая запись {id, albumId}) — догружаем пачкой через /tracks."""
        slots: List[Optional[YaTrack]] = []
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
            result: List[YaTrack] = []
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
        if not self.token:
            log.info("Yandex Music token not set — folder disabled")
            return False
        try:
            res = self._get("/account/status")
            if res and res.get("account"):
                acc = res["account"]
                name = acc.get("displayName") or acc.get("login") or "?"
                self._uid = str(acc.get("uid") or acc.get("id") or "") or None
                self._user_name = name if name and name != "?" else None
                # Plus / subscription
                plus = res.get("plus") or {}
                has_plus = bool(plus.get("hasPlus") or plus.get("has_plus"))
                sub = res.get("subscription") or {}
                auto = sub.get("autoRenewable") or sub.get("nonAutoRenewable") or []
                if isinstance(auto, dict):
                    auto = [auto]
                sub_names = []
                for s in (auto or []):
                    if isinstance(s, dict):
                        sub_names.append(s.get("product", {}).get("title") or s.get("operator") or "sub")
                log.info(
                    "Yandex Music: OK (%s) Plus=%s subs=%s",
                    name, has_plus, ",".join(sub_names) or "-",
                )
                if not has_plus:
                    log.warning(
                        "Yandex: Plus=false — lossless/FLAC API не отдаст. "
                        "Проверьте токен (нужен OAuth от music.yandex.ru, не от других сервисов)."
                    )
                self._account_ok = True
                self._has_plus = has_plus
                return True
            log.warning("Yandex Music: не удалось проверить аккаунт")
            self._account_ok = False
            return False
        except Exception as e:
            log.warning("Yandex login: %s", e)
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

    def _track_from(self, t: dict) -> Optional[YaTrack]:
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
        return YaTrack(
            id=str(t["id"]),
            title=title,
            artist=artists,
            duration=int(duration_ms / 1000) if duration_ms else 0,
            album=album,
            album_id=album_id,
            cover_uri=cover,
        )

    def search_tracks(self, query: str, limit: int = 40) -> List[YaTrack]:
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
        out: List[YaTrack] = []
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

    def get_artist_tracks(self, artist_id: str, limit: int = 100) -> List[YaTrack]:
        aid = str(artist_id or "").strip()
        if not aid or not self.enabled:
            return []
        now = time.time()
        cached = self._artist_cache.get(aid)
        if cached and (now - cached[0]) < self.cache_sec:
            return cached[1][:limit]

        res = self._get(f"/artists/{aid}/tracks", {"page": 0, "page-size": min(limit, 100)})
        out: List[YaTrack] = []
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

    def get_album_tracks(self, album_id: str) -> List[YaTrack]:
        aid = str(album_id or "").strip()
        if not aid or not self.enabled:
            return []
        now = time.time()
        cached = self._album_cache.get(aid)
        if cached and (now - cached[0]) < self.cache_sec:
            return cached[1]

        res = self._get(f"/albums/{aid}/with-tracks")
        out: List[YaTrack] = []
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
        probe: bool = False,
    ) -> Optional[tuple]:
        """Прямой URL через /get-file-info. Возвращает (url, mime) или None.
        Нужна подписка Plus для lossless.

        probe=True (проверка наличия FLAC для списка): тихо, и различает
        «FLAC точно нет» (возвращает False) и «не удалось выяснить» (None).
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
                f"{API}/get-file-info",
                params=params,
                headers=headers,
                timeout=20,
            )
            if r.status_code == 401:
                if probe:
                    return False  # type: ignore[return-value]
                log.warning("Yandex get-file-info 401 — нет Plus или токен невалиден")
                return None
            if r.status_code >= 400:
                if probe:
                    return False if r.status_code in (403, 404) else None  # type: ignore[return-value]
                log.warning("Yandex get-file-info HTTP %s: %s", r.status_code, r.text[:200])
                return None
            data = r.json()
            info = (data.get("result") or {}).get("downloadInfo") or data.get("downloadInfo") or data.get("result")
            if not info or not isinstance(info, dict):
                return False if probe else None  # type: ignore[return-value]
            url = info.get("url") or ""
            if not url:
                urls = info.get("urls") or []
                if urls:
                    url = urls[0]
            if not url:
                return False if probe else None  # type: ignore[return-value]
            codec = (info.get("codec") or "").lower()
            if require_flac and "flac" not in codec:
                if probe:
                    return False  # type: ignore[return-value]
                log.warning(
                    "Yandex track %s: asked FLAC, got codec=%s quality=%s — reject",
                    track_id, codec, info.get("quality"),
                )
                return None
            if "flac" in codec:
                mime = "audio/flac"
            elif "aac" in codec or "mp4" in codec:
                mime = "audio/mp4"
            else:
                mime = "audio/mpeg"
            if not probe:
                log.info(
                    "Yandex track %s: quality=%s codec=%s bitrate=%s",
                    track_id, info.get("quality"), codec, info.get("bitrate"),
                )
            return url, mime
        except Exception as e:
            if not probe:
                log.warning("Yandex get-file-info %s: %s", track_id, e)
            return None

    # ---------- Проверка FLAC (чтобы в списке заявлять реальный формат) ----------

    flac_cache_sec: float = 1800.0   # сколько помнить «есть ли FLAC у трека» (сек)

    def _flac_remember(self, tid: str, ok: bool) -> None:
        if len(self._flac_cache) > 20000:
            self._flac_cache.clear()
        self._flac_cache[str(tid)] = (time.time(), bool(ok))

    def flac_cached(self, tid: str) -> Optional[bool]:
        hit = self._flac_cache.get(str(tid))
        if hit and time.time() - hit[0] < self.flac_cache_sec:
            return hit[1]
        return None

    def probe_flac(self, tid: str) -> Optional[bool]:
        """True/False — FLAC для трека доступен/нет; None — выяснить не удалось."""
        tid = str(tid or "").strip()
        if not tid or not self.enabled:
            return None
        known = self.flac_cached(tid)
        if known is not None:
            return known
        if self._account_ok and not self._has_plus:
            self._flac_remember(tid, False)   # без Plus lossless не отдаётся
            return False
        res = self._get_file_info_url(
            tid, quality="lossless", codecs=["flac", "flac-mp4"],
            require_flac=True, probe=True,
        )
        if res is None:
            return None
        ok = bool(res)
        self._flac_remember(tid, ok)
        return ok

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
                self._flac_remember(tid, True)
                return result
            # повтор с полным списком, но всё равно требуем flac в ответе
            result = self._get_file_info_url(
                tid, quality="lossless", codecs=list(FILE_INFO_CODECS), require_flac=True
            )
            if result:
                self._flac_remember(tid, True)
                return result
            self._flac_remember(tid, False)
            log.warning(
                "Yandex: FLAC недоступен для %s (нет Plus или трек без lossless) — mp3",
                tid,
            )

        # Классический download-info (обычно mp3 192/320)
        infos = self._get(f"/tracks/{tid}/download-info")
        if not infos or not isinstance(infos, list):
            log.warning("Yandex: no download-info for %s", tid)
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
                    log.warning("Yandex: cannot parse download info for %s", tid)
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
            log.warning("Yandex get_stream_url %s: %s", tid, e)
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
                log.warning("Yandex stream HTTP %s for %s", r.status_code, track_id)
                r.close()
                return None
            ctype = r.headers.get("Content-Type") or mime or "audio/mpeg"
            # CDN иногда отдаёт application/octet-stream — подставляем наш mime
            if "octet-stream" in ctype or ctype == "application/octet-stream":
                ctype = mime or "audio/mpeg"
            clen = r.headers.get("Content-Length")
            return r, ctype, clen
        except Exception as e:
            log.warning("Yandex open_stream %s: %s", track_id, e)
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
            log.warning("Yandex metatags: пустой ответ (проверьте токен/API)")
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
                    log.info("Yandex tag %s: плейлисты получены через %s", tag, path)
                    break
        if out:
            # пустой ответ не кэшируем: иначе разовый сбой «залипает» на cache_sec
            self._metatag_pl_cache[tag] = (now, out)
        else:
            log.warning("Yandex tag %s: плейлистов не найдено", tag)
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

    def get_liked_tracks(self) -> List[YaTrack]:
        """Треки из «Мне нравится» текущего аккаунта."""
        if not self.enabled:
            return []
        uid = self._uid
        if not uid:
            status = self._get("/account/status") or {}
            account = status.get("account") or {}
            uid = str(account.get("uid") or account.get("id") or "") or None
            self._uid = uid
        if not uid:
            log.warning("Yandex: не удалось определить uid для коллекции")
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
        uid = self._uid
        if not uid:
            status = self._get("/account/status") or {}
            account = status.get("account") or {}
            uid = str(account.get("uid") or account.get("id") or "") or None
            self._uid = uid
        if not uid:
            log.warning("Yandex: не удалось определить uid для плейлистов")
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
        uid = self._uid
        if not uid:
            status = self._get("/account/status") or {}
            account = status.get("account") or {}
            uid = str(account.get("uid") or account.get("id") or "") or None
            self._uid = uid
        if not uid:
            log.warning("Yandex: не удалось определить uid для понравившихся плейлистов")
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

    def get_playlist_tracks(self, owner_id: str = "", kind=None, uuid: str = "") -> List[YaTrack]:
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

        out: List[YaTrack] = []
        if res:
            # иногда плейлист вложен в playlist
            pl = res.get("playlist") if isinstance(res, dict) and "playlist" in res else res
            out = self._tracks_from_entries((pl or {}).get("tracks") or [])
        if out:
            self._playlist_cache[cache_key] = (now, out)
        else:
            log.warning("Yandex playlist %s: треков не получено", cache_key)
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


    # ---------- Разделы сайта: «Для вас», «Тренды», «Книги и подкасты», «Детям» ----------
    #
    # Сайт music.yandex.ru собирает страницы из «скелета»:
    #   GET /landing/skeleton/{id}                  → список блоков (полок) и вкладок
    #   GET /landing/block/{blockId}/entities       → содержимое полки (альбомы/плейлисты/артисты/…)
    # Состав полок, их названия и порядок приходят из API, поэтому каталог
    # обновляется сам (сезонные подборки, чарты, новинки и т.д.), без хардкода.
    # Если скелет недоступен — используется запасной набор полок (LANDING_SEED).

    # id скелета страницы (пробуем по очереди, берём первый, где есть блоки)
    LANDING_PAGES = {
        "foryou":   {"title": "Для вас",          "skeleton": ["main"], "tab": ("для вас", 0)},
        "trends":   {"title": "Тренды",           "skeleton": ["main"], "tab": ("тренды", 1)},
        "nonmusic": {"title": "Книги и подкасты", "skeleton": ["non-music", "nonmusic", "non_music"]},
        "kids":     {"title": "Детям",            "skeleton": ["kids", "children"]},
    }

    # Блоки, которые не дают воспроизводимого списка в DLNA (волна/коллекция/концерты/баннеры)
    LANDING_SKIP_MARKERS = (
        "WAVE", "CONCERT", "BANNER", "PROMO", "STORIES", "LIKES_AND_HISTORY",
        "CONTINUE", "SHELF", "MENU", "DONATION", "VIDEO", "BOOKMATE", "LUMEN", "FACT",
    )

    # Запасной набор (если /landing/skeleton недоступен): blockId, тип, заголовок.
    # Заголовки заменяются на актуальные из ответа API, как только он получен.
    LANDING_SEED = {
        "foryou": [
            ("NEWRELEASES", "new-releases", "Новые релизы"),
            ("IN_STYLE", "in-style", "В стиле"),
        ],
        "trends": [
            ("RUSSIA_newcomers", "editorial-compilation", "Открытия"),
            ("ALL_isrka", "editorial-artists", "Зажглись от Искры"),
            ("ALL_albums_with_commentary", "editorial-compilation", "Артисты комментируют"),
            ("nitro_playlists", "editorial-compilation", "Они взлетели в Нитро"),
            ("RUSSIA_editorial_compilation", "editorial-compilation", "Выбор редакции"),
        ],
        "nonmusic": [
            ("editorial_audiobooks_CHART", "chart-albums", "Чарт аудиокниг"),
            ("vampires_romantic", "editorial-compilation", "Сезон «хоа-хоа-хоа»"),
            ("audiobooks_autumn_healing", "editorial-compilation", "Руководства по борьбе с осенней хандрой"),
        ],
        "kids": [
            ("children_mixes_grid", "mixes-grid", "Что послушаем?"),
            ("kids_season_new_editorial", "non-music-editorial-compilation", "Новинки сезона"),
            ("kids_book_autumn", "non-music-editorial-compilation", "Греемся этой осенью"),
        ],
    }

    # Разделы «Что послушаем?» (микс-карточки детского лендинга) на случай, если API не отдаст сетку
    LANDING_SEED_SUBLANDINGS = {
        "children_mixes_grid": [
            ("category_kids_fairytales", "Сказки и книжки"),
            ("category_kids_podcasts", "Развивайки"),
            ("category_kids_songs", "Песенки"),
            ("category_kids_sleep", "Засыпайки"),
        ],
    }

    def _lcache_get(self, key: str):
        c = getattr(self, "_landing_cache", None)
        if c is None:
            self._landing_cache = c = {}
        hit = c.get(key)
        if hit and (time.time() - hit[0]) < self.cache_sec:
            return hit[1]
        return None

    def _lcache_put(self, key: str, value) -> None:
        c = getattr(self, "_landing_cache", None)
        if c is None:
            self._landing_cache = c = {}
        if len(c) > 600:
            c.clear()
        c[key] = (time.time(), value)

    def _block_meta_map(self) -> dict:
        m = getattr(self, "_landing_block_meta", None)
        if m is None:
            self._landing_block_meta = m = {}
        return m

    @staticmethod
    def _norm_type(t) -> str:
        return str(t or "").strip().upper().replace("-", "_")

    def _landing_skip(self, btype: str) -> bool:
        nt = self._norm_type(btype)
        return any(mk in nt for mk in self.LANDING_SKIP_MARKERS)

    def _title_of(self, d: dict) -> str:
        t = d.get("title")
        if isinstance(t, dict):
            t = t.get("text") or t.get("title")
        if not t and isinstance(d.get("header"), dict):
            t = d["header"].get("title")
        if isinstance(t, list):
            t = "".join(str(p.get("text") if isinstance(p, dict) else p) for p in t)
        return str(t or "").strip()

    def _block_from_skeleton(self, b: dict) -> Optional[dict]:
        if not isinstance(b, dict):
            return None
        bid = b.get("id") or b.get("blockId") or b.get("block_id")
        btype = b.get("type") or ""
        if not bid:
            return None
        src = b.get("source") if isinstance(b.get("source"), dict) else {}
        meta = {
            "id": str(bid),
            "type": str(btype),
            "title": self._title_of(b),
            "source_uri": src.get("uri") or "",
        }
        self._block_meta_map()[meta["id"]] = meta
        return meta

    def _parse_skeleton(self, res) -> Optional[dict]:
        """→ {'blocks': [...], 'tabs': [{'title','blocks'}]} или None, если блоков нет."""
        if not isinstance(res, dict):
            return None
        raw = res.get("blocks")
        if not isinstance(raw, list):
            return None
        blocks: List[dict] = []
        tabs: List[dict] = []

        def is_tabs(b: dict) -> bool:
            data = b.get("data")
            return isinstance(data, dict) and isinstance(data.get("tabs"), list)

        for b in raw:
            if not isinstance(b, dict):
                continue
            if is_tabs(b):
                for i, tab in enumerate(b["data"]["tabs"]):
                    if not isinstance(tab, dict):
                        continue
                    tb = []
                    for x in (tab.get("blocks") or []):
                        m = self._block_from_skeleton(x)
                        if m and not self._landing_skip(m["type"]):
                            tb.append(m)
                    tabs.append({"id": str(tab.get("id") or i),
                                 "title": self._title_of(tab) or f"Вкладка {i + 1}",
                                 "blocks": tb})
                continue
            m = self._block_from_skeleton(b)
            if m and not self._landing_skip(m["type"]):
                blocks.append(m)
        if not blocks and not tabs:
            return None
        return {"blocks": blocks, "tabs": tabs}

    def get_landing_skeleton(self, skeleton_ids) -> Optional[dict]:
        """Скелет страницы по одному из id. Кэшируется."""
        if isinstance(skeleton_ids, str):
            skeleton_ids = [skeleton_ids]
        for sid in skeleton_ids:
            key = f"sk:{sid}"
            cached = self._lcache_get(key)
            if cached is not None:
                return cached or None
            res = self._get(f"/landing/skeleton/{quote(str(sid), safe='')}")
            parsed = self._parse_skeleton(res)
            if parsed:
                log.info("Yandex landing skeleton %s: blocks=%d tabs=%s", sid,
                         len(parsed["blocks"]),
                         [(t["title"], len(t["blocks"])) for t in parsed["tabs"]])
                self._lcache_put(key, parsed)
                return parsed
        log.warning("Yandex landing skeleton %s: недоступен — запасной набор полок", skeleton_ids)
        return None

    def _seed_blocks(self, page_key: str) -> List[dict]:
        out = []
        for bid, btype, title in self.LANDING_SEED.get(page_key, []):
            meta = self._block_meta_map().get(bid) or {
                "id": bid, "type": btype, "title": title, "source_uri": ""}
            if not meta.get("title"):
                meta["title"] = title
            self._block_meta_map()[bid] = meta
            out.append(meta)
        return out

    def get_landing_page(self, page_key: str) -> dict:
        """Страница верхнего уровня → {'blocks': [...], 'tabs': [{'title','blocks'}]}."""
        spec = self.LANDING_PAGES.get(page_key)
        if not spec or not self.enabled:
            return {"blocks": [], "tabs": []}
        sk = self.get_landing_skeleton(spec["skeleton"])
        if not sk:
            return {"blocks": self._seed_blocks(page_key), "tabs": []}
        want = spec.get("tab")
        if not want:
            return sk
        # «Для вас» / «Тренды» — вкладки одной страницы: отдаём содержимое нужной вкладки
        tabs = sk.get("tabs") or []
        if not tabs:
            if page_key == "foryou":
                return {"blocks": sk["blocks"], "tabs": []}
            return {"blocks": self._seed_blocks(page_key), "tabs": []}
        name, idx = want
        chosen = None
        for t in tabs:
            if name in (t.get("title") or "").lower():
                chosen = t
                break
        if chosen is None and idx < len(tabs) and len(tabs) > 1:
            chosen = tabs[idx]
        if chosen is None:
            return {"blocks": self._seed_blocks(page_key), "tabs": []}
        return {"blocks": list(chosen["blocks"]), "tabs": []}

    def get_landing_tab(self, page_key: str, idx: int) -> List[dict]:
        page = self.get_landing_page(page_key)
        tabs = page.get("tabs") or []
        if 0 <= idx < len(tabs):
            return tabs[idx]["blocks"]
        return []

    def get_sublanding(self, skeleton_id: str) -> dict:
        """Вложенная страница (например category_kids_songs)."""
        sk = self.get_landing_skeleton([skeleton_id])
        if sk:
            blocks = list(sk["blocks"])
            return {"blocks": blocks, "tabs": sk.get("tabs") or []}
        return {"blocks": [], "tabs": []}

    # ----- содержимое полок -----

    @staticmethod
    def _dig(d, *keys):
        """Найти dict по одному из ключей на глубине до 2 уровней."""
        if not isinstance(d, dict):
            return None
        for k in keys:
            v = d.get(k)
            if isinstance(v, dict):
                return v
        for v in d.values():
            if isinstance(v, dict):
                for k in keys:
                    w = v.get(k)
                    if isinstance(w, dict):
                        return w
        return None

    def _parse_entity(self, it) -> Optional[dict]:
        """Элемент полки → {'kind': album|playlist|artist|track|landing, ...}."""
        if not isinstance(it, dict):
            return None
        data = it.get("data") if isinstance(it.get("data"), dict) else it
        pos = None
        chart = data.get("chart")
        if isinstance(chart, dict):
            pos = chart.get("position")

        alb = self._dig(data, "album")
        if alb and alb.get("id") is not None:
            artists = data.get("artists") or alb.get("artists") or []
            names = self._artists_str(artists)
            meta_type = str(alb.get("metaType") or alb.get("type") or "music").lower()
            return {
                "kind": "album", "id": str(alb["id"]),
                "title": alb.get("title") or f"Альбом {alb['id']}",
                "artist": names, "count": int(alb.get("trackCount") or 0),
                "meta": meta_type, "pos": pos,
            }

        pl = self._dig(data, "playlist")
        if pl:
            uuid = pl.get("playlistUuid") or pl.get("uuid") or ""
            owner = pl.get("owner") if isinstance(pl.get("owner"), dict) else {}
            owner_id = pl.get("uid") or owner.get("uid")
            kind = pl.get("kind")
            if uuid:
                pid = f"uuid:{uuid}"
            elif owner_id is not None and kind is not None:
                pid = f"{owner_id}:{kind}"
            else:
                return None
            return {
                "kind": "playlist", "id": pid,
                "title": pl.get("title") or "Плейлист",
                "owner": owner.get("name") or "",
                "count": int(pl.get("trackCount") or data.get("trackCount") or 0),
                "pos": pos,
            }

        art = self._dig(data, "artist")
        if art and art.get("id") is not None and art.get("name"):
            return {"kind": "artist", "id": str(art["id"]), "title": art["name"], "count": 0, "pos": pos}

        tr = self._dig(data, "track")
        if tr:
            track = self._track_from(tr)
            if track:
                return {"kind": "track", "track": track, "title": track.title, "pos": pos}

        # плитка-микс (детский лендинг): {title, action:{weblink}}
        action = data.get("action") if isinstance(data.get("action"), dict) else {}
        link = action.get("weblink") or action.get("url") or data.get("weblink") or ""
        m = re.search(r"/landing/([^/?#]+)", str(link))
        if m:
            return {"kind": "landing", "id": m.group(1), "title": self._title_of(data) or m.group(1), "count": 1}
        return None

    def _albums_by_ids(self, ids: List[str]) -> List[dict]:
        out: List[dict] = []
        ids = [str(i) for i in ids if i]
        for i in range(0, len(ids), 100):
            res = self._post("/albums", {"album-ids": ",".join(ids[i:i + 100])})
            if isinstance(res, list):
                for a in res:
                    if isinstance(a, dict) and a.get("id") is not None:
                        out.append({"data": {"album": a}})
        return out

    def _parse_block_response(self, res) -> List[dict]:
        out: List[dict] = []
        if isinstance(res, list):
            raw = res
        elif isinstance(res, dict):
            # «В стиле»: вкладки со своими альбомами
            tabs = res.get("inStyleTabs")
            if isinstance(tabs, list):
                for i, t in enumerate(tabs):
                    if not isinstance(t, dict):
                        continue
                    kids = [e for e in (self._parse_entity(x) for x in (t.get("items") or [])) if e]
                    if kids:
                        out.append({"kind": "group", "title": t.get("title") or f"Стиль {i + 1}",
                                    "items": kids, "count": len(kids)})
                if out:
                    return out
            raw = (res.get("items") or res.get("entities") or res.get("playlists")
                   or res.get("albums") or res.get("artists") or [])
            if not raw and isinstance(res.get("newReleases"), list):
                nr = res["newReleases"]
                if nr and not isinstance(nr[0], dict):
                    raw = self._albums_by_ids(nr)
                else:
                    raw = nr
        else:
            return out
        for it in raw or []:
            if isinstance(it, dict) and it.get("id") is not None and not it.get("data") \
                    and not any(k in it for k in ("album", "playlist", "artist", "track")):
                # голый объект: определяем по полям
                if it.get("name") and not it.get("title"):
                    it = {"data": {"artist": it}}
                elif it.get("kind") is not None or it.get("playlistUuid"):
                    it = {"data": {"playlist": it}}
                else:
                    it = {"data": {"album": it}}
            e = self._parse_entity(it)
            if e:
                out.append(e)
        return out

    def get_block_items(self, block_id: str) -> List[dict]:
        """Содержимое полки. Кэш cache_sec. Пустой результат не кэшируется."""
        bid = str(block_id or "").strip()
        if not bid or not self.enabled:
            return []
        key = f"bl:{bid}"
        cached = self._lcache_get(key)
        if cached is not None:
            return cached
        meta = self._block_meta_map().get(bid) or {}
        bt = (meta.get("type") or "").lower().replace("_", "-")
        attempts = [(f"/landing/block/{quote(bid, safe='')}/entities", {"page": 0, "pageSize": 100})]
        if meta.get("source_uri"):
            attempts.append((meta["source_uri"], {}))
        attempts.append((f"/landing/block/{quote(bid, safe='')}", {}))
        if bt:
            attempts.append((f"/landing/block/{bt}", {}))
        items: List[dict] = []
        for path, params in attempts:
            res = self._get(path, params)
            if res is not None and isinstance(res, dict) and res.get("title") and not meta.get("title"):
                meta["title"] = str(res["title"])
                self._block_meta_map()[bid] = meta
            items = self._parse_block_response(res)
            if items:
                log.info("Yandex block %s: %d элементов (%s)", bid, len(items), path)
                break
        if not items and bid in self.LANDING_SEED_SUBLANDINGS:
            items = [{"kind": "landing", "id": sid, "title": t, "count": 1}
                     for sid, t in self.LANDING_SEED_SUBLANDINGS[bid]]
        if items:
            self._lcache_put(key, items)
        else:
            log.warning("Yandex block %s: пусто (проверьте токен/API)", bid)
        return items

    def ready_blocks(self, blocks: List[dict]) -> List[dict]:
        """Параллельно подгрузить полки и оставить непустые: [{**block, 'count': N}]."""
        from concurrent.futures import ThreadPoolExecutor
        blocks = [b for b in blocks or [] if b.get("id")]
        if not blocks:
            return []

        def load(b):
            try:
                return self.get_block_items(b["id"])
            except Exception as e:
                log.warning("Yandex block %s: %s", b.get("id"), e)
                return []

        with ThreadPoolExecutor(max_workers=min(6, len(blocks))) as ex:
            results = list(ex.map(load, blocks))
        out = []
        for b, items in zip(blocks, results):
            if not items:
                continue
            nb = dict(b)
            nb["count"] = len(items)
            if not nb.get("title"):
                nb["title"] = (self._block_meta_map().get(b["id"]) or {}).get("title") or b["id"]
            out.append(nb)
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
                f"{API}{path}",
                json=payload or {},
                timeout=25,
                headers={"Content-Type": "application/json"},
            )
            if r.status_code >= 400:
                log.warning("Yandex POST JSON %s -> HTTP %s: %s", path, r.status_code, r.text[:300])
                return None
            body = r.json()
            if body.get("error"):
                log.warning("Yandex API %s error: %s", path, body.get("error"))
                return None
            return body.get("result", body)
        except Exception as e:
            log.warning("Yandex POST JSON %s: %s", path, e)
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
                    "Yandex wave ensure_ahead preset=%s track=%s remaining_was=%s added=%s",
                    preset_id, tid, remaining, added,
                )
        return total_added

    def get_wave_tracks(self, preset_id: str = "default", append: bool = False) -> List[YaTrack]:
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
                "Yandex wave preset=%s idle %.0fs > %ds — new session",
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
            log.info("Yandex wave NEW session seeds=%s preset=%s", seeds, preset_id)
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
                log.warning("Yandex wave session/new failed — fallback to station API")
                return self._wave_tracks_legacy(preset_id, station, settings, cache, append)

        elif append and session_id:
            # Следующая партия. Важно: queue = ВСЕ уже выданные треки
            # (как в MarshalX wave_radio: played_ids.append при взятии из sequence),
            # а не только реально прослушанные. Иначе API часто возвращает 0.
            queue = self._wave_queue_ids(cache)
            payload = {"queue": queue[-40:]}
            log.info(
                "Yandex wave session tracks session=%s queue_len=%s preset=%s",
                session_id, len(payload["queue"]), preset_id,
            )
            res = self._post_json(f"/rotor/session/{session_id}/tracks", payload)
            if not res:
                # сессия могла протухнуть — пересоздаём
                log.warning("Yandex wave session tracks empty — recreating session")
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
                log.info("Yandex wave session +0 — try legacy station tracks")
                legacy_before = len(cache["tracks"])
                self._wave_tracks_legacy(preset_id, station, settings, cache, append=True)
                added = len(cache["tracks"]) - legacy_before

        if added > 0:
            cache["exhausted_until"] = 0.0
        elif append:
            cache["exhausted_until"] = now + 8.0
            log.info(
                "Yandex wave +0 (session=%s) — pause 8s, have=%s",
                cache.get("session_id"), len(cache["tracks"]),
            )

        cache["ts"] = now
        log.info(
            "Yandex wave station=%s session=%s: +%s tracks, total=%s batch=%s",
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

    def _wave_tracks_legacy(self, preset_id, station, settings, cache, append) -> List[YaTrack]:
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
            url = f"{API}/rotor/station/{self._station_path(station)}/settings3"
            r = self.session.post(url, json=data, timeout=15)
            if r.status_code >= 400:
                url2 = f"{API}/rotor/station/{self._station_path(station)}/settings2"
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
                f"{API}{path}",
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
            url = f"{API}/rotor/station/{self._station_path(st)}/feedback"
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
