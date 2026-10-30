"""Что играет на интернет-радио сейчас и где взять обложку этого трека.

1. ICY: открываем поток с заголовком `Icy-MetaData: 1`, читаем до первого блока
   метаданных и берём StreamTitle («Исполнитель - Название»). Читаем десятки КБ
   и сразу закрываем соединение — на роутер это почти не влияет.
2. Обложка: публичный поиск Deezer, запасной — iTunes Search. Ключи не нужны.

Всё best-effort: любая ошибка → None, и обложка станции подменяется логотипом
из m3u (tvg-logo) или сгенерированной заглушкой.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Dict, Optional, Tuple

import requests

log = logging.getLogger("dlna_emu.radio_meta")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36"

_TITLE_RE = re.compile(rb"StreamTitle='(.*?)';|StreamTitle='(.*)$", re.S)
_JUNK = re.compile(r"^(?:\s*|-+|n/?a|unknown|null|http\S*|advert\w*|реклам\w*)$", re.I)


def _read_exact(raw, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = raw.read(n - len(buf))
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


def _decode(b: bytes) -> str:
    for enc in ("utf-8", "cp1251", "latin-1"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", errors="replace")


def split_title(raw: str) -> Tuple[str, str]:
    """'Artist - Title' → (artist, title). Без разделителя → ('', raw)."""
    raw = (raw or "").strip()
    for sep in (" - ", " – ", " — ", " | "):
        if sep in raw:
            a, t = raw.split(sep, 1)
            return a.strip(), t.strip()
    return "", raw


class RadioNowPlaying:
    def __init__(self, ttl: int = 25, timeout: float = 4.0, max_parallel: int = 3):
        self.ttl = ttl
        self.timeout = timeout
        self._sem = threading.BoundedSemaphore(max_parallel)
        self._cache: Dict[str, Tuple[float, Optional[str]]] = {}
        self._cover_url: Dict[str, str] = {}
        self._lock = threading.Lock()
        self._http = requests.Session()
        self._http.headers["User-Agent"] = UA

    # ---------------------------------------------------------------- ICY

    def stream_title(self, url: str, wait: float = 0.0) -> Optional[str]:
        """Текущий StreamTitle станции или None (кэш на ttl секунд, в т.ч. отрицательный)."""
        # Станцию уже проксирует эмулятор — название берём из того же потока,
        # без второго подключения (у станций бывает лимит «1 слушатель с IP»).
        try:
            from . import radio_proxy
            live = radio_proxy.live_title(url)
            if not live and wait > 0:
                # станцию только что запустили — ждём первый ICY-блок из потока
                live = radio_proxy.wait_title(url, wait)
            if live:
                return live
        except Exception:  # noqa: BLE001
            pass
        now = time.time()
        with self._lock:
            hit = self._cache.get(url)
            if hit and now - hit[0] < (self.ttl if hit[1] else max(self.ttl, 120)):
                return hit[1]
        title: Optional[str] = None
        if not self._sem.acquire(timeout=self.timeout):
            return None
        try:
            title = self._fetch_icy(url)
            if title is None and url.endswith("?.mp3"):
                # «?.mp3» добавлен для ресивера — станции он может мешать
                title = self._fetch_icy(url[: -len("?.mp3")])
        except Exception as e:  # noqa: BLE001
            log.debug("icy %s: %s", url, e)
        finally:
            self._sem.release()
        with self._lock:
            if len(self._cache) > 500:
                self._cache.clear()
            self._cache[url] = (time.time(), title)
        return title

    def _fetch_icy(self, url: str) -> Optional[str]:
        if ".m3u8" in url.lower().split("?", 1)[0]:
            return None  # HLS — метаданных ICY нет
        r = self._http.get(
            url, headers={"Icy-MetaData": "1"}, stream=True,
            timeout=(self.timeout, self.timeout), allow_redirects=True,
        )
        try:
            if r.status_code >= 400:
                return None
            try:
                metaint = int(r.headers.get("icy-metaint") or 0)
            except ValueError:
                metaint = 0
            if metaint <= 0 or metaint > 262144:
                return None
            raw = r.raw
            deadline = time.time() + self.timeout * 2
            # иногда первый блок пустой — даём пару попыток
            for _ in range(3):
                if time.time() > deadline:
                    return None
                if len(_read_exact(raw, metaint)) < metaint:
                    return None
                ln = _read_exact(raw, 1)
                if not ln:
                    return None
                size = ln[0] * 16
                if not size:
                    continue
                meta = _read_exact(raw, size)
                m = _TITLE_RE.search(meta)
                if not m:
                    continue
                text = _decode(m.group(1) if m.group(1) is not None else m.group(2)).strip("\x00 ")
                return None if _JUNK.match(text) else text
            return None
        finally:
            r.close()

    # ------------------------------------------------------------- обложка

    def cover_url(self, artist: str, title: str) -> Optional[str]:
        """URL обложки трека из публичных каталогов (кэшируется)."""
        key = f"{artist}|{title}".lower()
        with self._lock:
            if key in self._cover_url:
                return self._cover_url[key] or None
        url = ""
        try:
            url = self._deezer(artist, title) or self._itunes(artist, title) or ""
        except Exception as e:  # noqa: BLE001
            log.debug("radio cover lookup %s: %s", key, e)
        with self._lock:
            if len(self._cover_url) > 2000:
                self._cover_url.clear()
            self._cover_url[key] = url
        return url or None

    def _deezer(self, artist: str, title: str) -> Optional[str]:
        queries = []
        if artist:
            queries.append(f'artist:"{artist}" track:"{title}"')
        queries.append(f"{artist} {title}".strip())
        for q in queries:
            r = self._http.get(
                "https://api.deezer.com/search",
                params={"q": q, "limit": 1}, timeout=8,
            )
            if r.status_code >= 400:
                continue
            for it in (r.json() or {}).get("data") or []:
                alb = it.get("album") or {}
                u = alb.get("cover_big") or alb.get("cover_medium") or alb.get("cover")
                if u:
                    return u
        return None

    def _itunes(self, artist: str, title: str) -> Optional[str]:
        r = self._http.get(
            "https://itunes.apple.com/search",
            params={"term": f"{artist} {title}".strip(), "entity": "song", "limit": 1},
            timeout=8,
        )
        if r.status_code >= 400:
            return None
        for it in (r.json() or {}).get("results") or []:
            u = it.get("artworkUrl100") or ""
            if u:
                return re.sub(r"/\d+x\d+(bb)?\.", "/600x600bb.", u)
        return None
