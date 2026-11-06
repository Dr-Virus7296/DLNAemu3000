"""Проксирование интернет-радио через эмулятор (роутер).

Вместо редиректа 302 (ресивер сам идёт на сервер станции) эмулятор открывает
поток сам и пересылает его ресиверу. Плюсы: ресиверу не нужен прямой доступ к
станции, не мешают https/редиректы/«ICY 200 OK», странные заголовки и таймауты.

open_station() → (iterator байт, content-type, close()) либо None,
если проксировать нельзя (HLS .m3u8, ошибка источника) — тогда вызывающий код
делает обычный редирект.
"""

from __future__ import annotations

import logging
import re
import socket
import ssl
import threading
import time
from typing import Callable, Dict, Iterator, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests

log = logging.getLogger("dlna_emu.radio_proxy")

UA = "Mozilla/5.0 (compatible; DLNA-emu radio proxy)"
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 20
CHUNK = 8192
MAX_REDIRECTS = 5
RETRY_DELAY = 1.0          # пауза перед повтором (растёт: 1, 2, 3 с, не больше 3)
INITIAL_BUDGET = 30.0      # сек: сколько всего пробуем открыть станцию при старте
RETRY_RESET_BYTES = 32768  # после стольких байт на новом соединении счётчик попыток обнуляется

_ICY_MAX_METAINT = 262144
_TITLE_RE = re.compile(rb"StreamTitle='(.*?)';|StreamTitle='(.*)$", re.S)
_JUNK = re.compile(r"^(?:\s*|-+|n/?a|unknown|null|http\S*|advert\w*|реклам\w*)$", re.I)

# Что сейчас играет на станции — берём из ICY-метаданных проксируемого потока,
# чтобы НЕ открывать вторым подключением ту же станцию (у части станций лимит
# «1 слушатель с IP», а за роутером все подключения идут с одного IP).
_LIVE: Dict[str, dict] = {}
_LIVE_LOCK = threading.Lock()
LIVE_TITLE_KEEP = 120   # сек: сколько помнить название после закрытия потока

_PLAYLIST_CT = ("mpegurl", "x-scpls", "pls+xml")
_HLS_CT = ("vnd.apple.mpegurl", "application/x-mpegurl")


def _decode(b: bytes) -> str:
    for enc in ("utf-8", "cp1251", "latin-1"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", errors="replace")


def live_title(key: str) -> Optional[str]:
    """Название трека из проксируемого потока станции (None — неизвестно)."""
    now = time.time()
    with _LIVE_LOCK:
        e = _LIVE.get(key)
        if not e:
            return None
        if e["active"] <= 0 and now - e["ts"] > LIVE_TITLE_KEEP:
            del _LIVE[key]
            return None
        return e["title"] or None


def _live_reset(key: str) -> None:
    """Начало новой попытки открыть станцию: прежнее название/флаги больше не верны."""
    with _LIVE_LOCK:
        e = _LIVE.setdefault(key, {"title": "", "ts": time.time(), "active": 0})
        if e["active"] <= 0:
            e["title"] = ""
        e["nometa"] = False
        e["failed"] = False
        e["ts"] = time.time()


def _live_flag(key: str, **kw) -> None:
    with _LIVE_LOCK:
        e = _LIVE.setdefault(key, {"title": "", "ts": time.time(), "active": 0})
        e.update(kw)


def wait_title(key: str, timeout: float) -> Optional[str]:
    """Подождать, пока из проксируемого потока придёт название трека.

    Нужно для первого запуска станции: обложку ресивер просит сразу, а ICY-блок
    с названием приходит лишь после первых килобайт потока. Ждём, пока станция
    не окажется без метаданных / не открылась (тогда ждать нечего) или не истечёт timeout.
    """
    deadline = time.time() + max(0.0, timeout)
    while True:
        t = live_title(key)
        if t:
            return t
        with _LIVE_LOCK:
            e = _LIVE.get(key) or {}
            if e.get("nometa") or e.get("failed"):
                return None
        if time.time() >= deadline:
            return None
        time.sleep(0.15)


def _live_open(key: str) -> None:
    with _LIVE_LOCK:
        e = _LIVE.setdefault(key, {"title": "", "ts": time.time(), "active": 0})
        e["active"] += 1


def _live_close(key: str) -> None:
    with _LIVE_LOCK:
        e = _LIVE.get(key)
        if e:
            e["active"] = max(0, e["active"] - 1)
            e["ts"] = time.time()


# Хук для истории эфира: ON_TITLE(url_станции, название) при смене трека
ON_TITLE: Optional[Callable[[str, str], None]] = None


def _live_set(key: str, meta: bytes) -> None:
    m = _TITLE_RE.search(meta)
    if not m:
        return
    text = _decode(m.group(1) if m.group(1) is not None else m.group(2)).strip("\x00 ")
    title = "" if _JUNK.match(text) else text
    changed = False
    with _LIVE_LOCK:
        e = _LIVE.get(key)
        if e is not None:
            changed = bool(title) and e.get("title") != title
            e["title"] = title
            e["ts"] = time.time()
    if changed and ON_TITLE is not None:
        try:
            ON_TITLE(key, title)
        except Exception:  # noqa: BLE001
            pass


class _IcyParser:
    """Разбирает поток «аудио metaint байт + блок метаданных»; feed() → чистое аудио."""

    def __init__(self, metaint: int, on_meta: Callable[[bytes], None]):
        self.metaint = metaint
        self.on_meta = on_meta
        self.audio_left = metaint
        self.need_len = False
        self.meta_left = 0
        self.meta = bytearray()

    def feed(self, chunk: bytes) -> bytes:
        out = bytearray()
        i, n = 0, len(chunk)
        while i < n:
            if self.need_len:
                self.meta_left = chunk[i] * 16
                i += 1
                self.need_len = False
                self.meta = bytearray()
                if self.meta_left == 0:
                    self.audio_left = self.metaint
                continue
            if self.meta_left > 0:
                take = min(self.meta_left, n - i)
                self.meta += chunk[i:i + take]
                i += take
                self.meta_left -= take
                if self.meta_left == 0:
                    try:
                        self.on_meta(bytes(self.meta))
                    except Exception:
                        pass
                    self.audio_left = self.metaint
                continue
            take = min(self.audio_left, n - i)
            out += chunk[i:i + take]
            i += take
            self.audio_left -= take
            if self.audio_left == 0:
                self.need_len = True
        return bytes(out)


class _IcyMux:
    """Вставляет блоки ICY-метаданных в чистое аудио через каждые metaint байт.

    Нужен после переподключения: ресивер считает байты с начала ИСХОДНОГО соединения,
    а новое соединение со станцией начинает свой отсчёт заново — границы блоков
    пришлось бы иначе перепутать, и звук «посыпался» бы.
    """

    def __init__(self, metaint: int, left: int, title_fn: Callable[[], Optional[str]]):
        self.metaint = metaint
        self.left = left if 0 < left <= metaint else metaint
        self.title_fn = title_fn
        self.sent: Optional[str] = None

    def _block(self) -> bytes:
        t = (self.title_fn() or "")
        if t == self.sent:
            return b"\x00"
        self.sent = t
        payload = ("StreamTitle='%s';" % t.replace("'", "’")).encode("utf-8")[:4080]
        payload += b"\x00" * ((-len(payload)) % 16)
        return bytes([len(payload) // 16]) + payload

    def feed(self, audio: bytes) -> bytes:
        out = bytearray()
        i, n = 0, len(audio)
        while i < n:
            take = min(self.left, n - i)
            out += audio[i:i + take]
            i += take
            self.left -= take
            if self.left == 0:
                out += self._block()
                self.left = self.metaint
        return bytes(out)


class _Stream:
    """Итерируемый поток; metaint > 0 — ресиверу отдаём метаданные ICY как есть."""

    def __init__(self, gen: Iterator[bytes], metaint: int = 0):
        self._gen = gen
        self.metaint = metaint

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        return next(self._gen)


def _parse_metaint(v) -> int:
    try:
        n = int(v or 0)
    except (TypeError, ValueError):
        return 0
    return n if 0 < n <= _ICY_MAX_METAINT else 0


def _audio_ct(ct: str) -> str:
    ct = (ct or "").split(";")[0].strip().lower()
    if ct.startswith("audio/") or ct in ("application/ogg", "video/mp2t"):
        return ct
    return "audio/mpeg"


def _candidates(url: str):
    yield url
    # «?.mp3» добавляется для ресивера (авто-суффикс) — источнику он не нужен
    if url.endswith("?.mp3"):
        yield url[: -len("?.mp3")]


def _first_playlist_url(text: str, base: str) -> str:
    """Первая ссылка из .pls (File1=...) или .m3u (строка с адресом)."""
    is_pls = "[playlist]" in text.lower()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        m = re.match(r"(?i)^file\d*\s*=\s*(\S+)", line)
        if m:
            return urljoin(base, m.group(1))
        if not is_pls and re.match(r"(?i)^(https?://|/|\w[\w./-]*$)", line):
            return urljoin(base, line)
    return ""


def _open_requests(url: str):
    r = requests.get(
        url, stream=True, allow_redirects=True,
        timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        headers={"User-Agent": UA, "Icy-MetaData": "1", "Accept": "*/*"},
    )
    return r


def _open_raw_icy(url: str):
    """Shoutcast v1 отвечает «ICY 200 OK» — requests/http.client это не понимают."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return None
    port = u.port or (443 if u.scheme == "https" else 80)
    sock = socket.create_connection((u.hostname, port), timeout=CONNECT_TIMEOUT)
    if u.scheme == "https":
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=u.hostname)
    sock.settimeout(READ_TIMEOUT)
    path = (u.path or "/") + (("?" + u.query) if u.query else "")
    req = (
        f"GET {path} HTTP/1.0\r\nHost: {u.netloc}\r\nUser-Agent: {UA}\r\n"
        "Icy-MetaData: 1\r\nAccept: */*\r\nConnection: close\r\n\r\n"
    )
    sock.sendall(req.encode("latin-1", "replace"))
    buf = b""
    while b"\r\n\r\n" not in buf and len(buf) < 16384:
        part = sock.recv(4096)
        if not part:
            break
        buf += part
    head, _, rest = buf.partition(b"\r\n\r\n")
    lines = head.decode("latin-1", "replace").split("\r\n")
    m = re.match(r"^(?:ICY|HTTP/\d\.\d)\s+(\d{3})", lines[0] if lines else "")
    if not m or int(m.group(1)) >= 400:
        sock.close()
        return None
    hdrs = {}
    for ln in lines[1:]:
        k, _, v = ln.partition(":")
        hdrs[k.strip().lower()] = v.strip()

    def gen() -> Iterator[bytes]:
        if rest:
            yield rest
        while True:
            d = sock.recv(CHUNK)
            if not d:
                return
            yield d

    return gen(), hdrs.get("content-type", ""), sock.close, _parse_metaint(hdrs.get("icy-metaint"))


def _open_any(url: str):
    """Один проход по вариантам адреса. → (результат | None, была_ли_сетевая_ошибка)."""
    net_err = False
    for cand in _candidates(url):
        try:
            res = _open_one(cand, depth=0)
        except Exception as e:
            log.info("radio proxy %s: %s", cand, e)
            net_err = True
            res = None
        if res:
            return res, net_err
    return None, net_err


def open_station(url: str, client_meta: bool = False, key: Optional[str] = None,
                 retries: int = 0):
    """Открыть поток станции. None — проксировать не получилось/нельзя.

    Возвращает (поток, content-type, close). Метаданные ICY всегда разбираются
    (название трека → live_title(key)); ресиверу они отдаются только если он сам
    просил (client_meta=True) — тогда у потока .metaint > 0 и нужен заголовок
    icy-metaint, иначе вырезаются, чтобы не портить звук.

    retries — сколько раз переподключаться к станции при таймауте/обрыве (и при
    открытии, и посреди воспроизведения; ресивер обрыва не замечает). Все попытки
    провалены → поток завершается, ресивер сам переходит к следующему.
    """
    key = key or url
    retries = max(0, int(retries or 0))
    _live_reset(key)
    deadline = time.time() + INITIAL_BUDGET
    res = None
    for attempt in range(retries + 1):
        res, net_err = _open_any(url)
        if res or not net_err:      # HLS / HTTP-ошибка — повтор не поможет
            break
        if attempt < retries and time.time() < deadline:
            log.info("radio proxy %s: не открылась, повтор %d/%d", url, attempt + 1, retries)
            time.sleep(min(RETRY_DELAY * (attempt + 1), 3.0))
        else:
            break
    if not res:
        _live_flag(key, failed=True)
        return None
    return _wrap(res, client_meta, key, url, retries)


def _wrap(res, client_meta: bool, key: str, url: str = "", retries: int = 0):
    it, ct, close, metaint = res
    if not metaint:
        log.info("radio proxy %s: станция не отдала icy-metaint — названия трека не будет", key)
        _live_flag(key, nometa=True)
    out_metaint = metaint if (client_meta and metaint) else 0   # что обещаем ресиверу
    state = {"closed": False, "close": close}

    def on_meta(m: bytes) -> None:
        _live_set(key, m)
        t = live_title(key)
        if t and t != state.get("last"):
            state["last"] = t
            log.info("radio proxy %s: сейчас играет «%s»", key, t)

    _live_open(key)

    def close_all() -> None:
        if not state["closed"]:
            state["closed"] = True
            _live_close(key)
        try:
            state["close"]()
        except Exception:
            pass

    def gen() -> Iterator[bytes]:
        cur_it = it
        parser = _IcyParser(metaint, on_meta) if metaint else None
        mux: Optional[_IcyMux] = None     # появляется после переподключения
        attempts = 0
        conn_bytes = 0
        while True:
            err = "станция закрыла поток"
            try:
                for chunk in cur_it:
                    if mux is not None:
                        audio = parser.feed(chunk) if parser else chunk
                        out = mux.feed(audio) if audio else b""
                    elif out_metaint:
                        parser.feed(chunk)            # только следим за позицией
                        out = chunk                   # ресиверу — байты как есть
                    else:
                        out = parser.feed(chunk) if parser else chunk
                    if not out:
                        continue
                    conn_bytes += len(chunk)
                    if attempts and conn_bytes >= RETRY_RESET_BYTES:
                        attempts = 0
                    yield out
            except Exception as e:          # таймаут чтения / обрыв источника
                err = str(e) or e.__class__.__name__
            if state["closed"]:
                return
            try:
                state["close"]()
            except Exception:
                pass
            if retries <= 0:
                log.info("radio proxy %s: %s", key, err)
                return
            # ---- переподключение
            res2 = None
            while True:
                attempts += 1
                if attempts > retries:
                    log.info("radio proxy %s: %d попыток подряд не удались (%s) — "
                             "завершаю поток, ресивер перейдёт к следующему", key, retries, err)
                    return
                log.info("radio proxy %s: обрыв (%s) — переподключение %d/%d",
                         key, err, attempts, retries)
                time.sleep(min(RETRY_DELAY * attempts, 3.0))
                if state["closed"]:
                    return
                res2, _ = _open_any(url)
                if res2:
                    break
            cur_it, _ct2, state["close"], metaint2 = res2
            old = parser
            parser = _IcyParser(metaint2, on_meta) if metaint2 else None
            conn_bytes = 0
            if out_metaint and mux is None:
                # доводим блок метаданных, на котором оборвалось, до конца и
                # дальше сами расставляем границы ICY для ресивера
                filler = b""
                left = out_metaint
                if old is not None:
                    if old.meta_left > 0:
                        filler = b"\x00" * old.meta_left
                    elif old.need_len:
                        filler = b"\x00"
                    else:
                        left = old.audio_left
                mux = _IcyMux(out_metaint, left, lambda: live_title(key))
                if filler:
                    yield filler

    return _Stream(gen(), out_metaint), ct, close_all


def _open_one(url: str, depth: int):
    if depth > 2 or urlparse(url).path.lower().endswith(".m3u8"):
        return None   # HLS потоком не проксируем — отдаём редиректом
    try:
        r = _open_requests(url)
    except (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError) as e:
        # «ICY 200 OK» и подобное
        raw = _open_raw_icy(url)
        if raw is None:
            raise e
        it, ct, close, metaint = raw
        return it, _audio_ct(ct), close, metaint
    if r.status_code >= 400:
        r.close()
        return None
    ct = (r.headers.get("Content-Type") or "").lower()
    if any(x in ct for x in _HLS_CT) or urlparse(r.url).path.lower().endswith(".m3u8"):
        # m3u8: либо HLS, либо обычный плейлист — различим по содержимому
        text = r.raw.read(65536, decode_content=True).decode("utf-8", "replace")
        r.close()
        if "#EXT-X-" in text:
            return None
        nxt = _first_playlist_url(text, r.url)
        return _open_one(nxt, depth + 1) if nxt else None
    if any(x in ct for x in _PLAYLIST_CT) or urlparse(r.url).path.lower().endswith((".pls", ".m3u")):
        text = r.raw.read(65536, decode_content=True).decode("utf-8", "replace")
        r.close()
        nxt = _first_playlist_url(text, r.url)
        return _open_one(nxt, depth + 1) if nxt else None

    def gen() -> Iterator[bytes]:
        # Нельзя iter_content(chunk_size=None): для потока без Content-Length и без
        # chunked (Icecast, HTTP/1.0) он читает до конца потока — у радио конца нет,
        # и ресивер не получает ни байта. Читаем маленькими порциями по мере прихода.
        raw = r.raw
        read1 = getattr(raw, "read1", None)
        if read1 is not None:        # urllib3 ≥ 2: отдаёт сразу то, что уже пришло
            while True:
                d = read1(CHUNK, decode_content=True)
                if not d:
                    return
                yield d
        else:                        # старый urllib3: блокируется лишь до CHUNK байт
            for chunk in r.iter_content(chunk_size=2048):
                if chunk:
                    yield chunk

    return gen(), _audio_ct(ct), r.close, _parse_metaint(r.headers.get("icy-metaint"))
