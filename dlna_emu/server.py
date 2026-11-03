"""HTTP + UPnP SOAP MediaServer."""

from __future__ import annotations

import logging
import os
import re
import socket
import time
import traceback
import uuid as uuid_lib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import unquote, urlparse
from xml.sax.saxutils import escape

from .content import ContentTree
from .deezer_client import DeezerClient
from .drivemusic_client import DriveMusicClient
from .zaycev_client import ZaycevClient
from . import radio_proxy
from .akniga_client import AknigaClient
from .promodj_client import PromoDjClient
from .radio_m3u import RadioLibrary
from .ssdp import SSDPServer
from .yandex_client import YandexMusicClient
from .zvuk_client import ZvukClient
from .kion_client import KionMusicClient

log = logging.getLogger("dlna_emu.server")
trace_log = logging.getLogger("dlna_emu.trace")

# Флаги трассировки DLNA-трафика от ресивера (переопределяются из config.yaml
# при старте и на лету из webui при сохранении настроек).
_TRACE_CFG = {"enabled": True, "covers": False}

# IP устройства -> {service, track, ts, title, cover, streaming}
ACTIVE_CLIENTS: dict = {}
_ACTIVE_LOCK = __import__("threading").Lock()
_ACTIVE_TTL = 300  # сек без активного потока
_KEEPALIVE_INTERVAL = 30
_KEEPALIVE_STARTED = False


# Какую радиостанцию слушает каждый ресивер: ip -> (pid, index, ts).
# Живёт, пока ресивер не запросит другой поток (радио играет часами).
RADIO_PLAYING: dict = {}
# Когда ресивер последний раз открывал список радио: ip -> ts.
# Обложки, пришедшие сразу после этого, — для списка станций (ждать название трека не нужно).
RADIO_LIST_BROWSED: dict = {}
_RADIO_PLAYING_TTL = 12 * 3600


def _cover_for(service: str, track_id: str) -> str:
    """Относительный /cover/... путь на DLNA-порту."""
    if not service or not track_id:
        return ""
    svc = service.lower()
    tid = str(track_id)
    if svc == "radio":
        parts = tid.split(":")
        if len(parts) >= 3:
            return f"/cover/radio/{parts[1]}/{parts[2]}"
        return ""
    if svc in ("yandex", "kion", "zvuk", "deezer", "drivemusic", "promodj", "zaycev"):
        return f"/cover/{svc}/{tid}"
    if svc == "akniga":
        return f"/cover/akniga/{tid.split(':')[0]}"
    if svc in ("file", "local", "usb"):
        # track_id = относительный/полный путь — обложка через /cover/file|local/
        kind = "file" if svc in ("file", "usb") else "local"
        return f"/cover/{kind}/{tid}"
    if svc == "net":
        return ""  # net covers через /cover/file не всегда есть
    return ""


def radio_is_playing(pid: str, index: int) -> bool:
    now = time.time()
    with _ACTIVE_LOCK:
        for ip, (p, i, ts) in list(RADIO_PLAYING.items()):
            if now - ts > _RADIO_PLAYING_TTL:
                del RADIO_PLAYING[ip]
            elif p == pid and i == index:
                return True
    return False


def update_active_client(ip: str, service: str, title: str, track_id: str = "",
                         cover: str = "", streaming: bool = True) -> None:
    if not ip:
        return
    if service != "radio":
        with _ACTIVE_LOCK:
            RADIO_PLAYING.pop(ip, None)
    cov = cover or _cover_for(service, track_id)
    with _ACTIVE_LOCK:
        prev = ACTIVE_CLIENTS.get(ip) or {}
        ACTIVE_CLIENTS[ip] = {
            "service": service,
            "title": title or track_id or "?",
            "track_id": track_id,
            "cover": cov or prev.get("cover") or "",
            "streaming": bool(streaming),
            "ts": time.time(),
        }
    _ensure_keepalive()


def end_active_stream(ip: str) -> None:
    if not ip:
        return
    with _ACTIVE_LOCK:
        info = ACTIVE_CLIENTS.get(ip)
        if info:
            info["streaming"] = False
            info["ts"] = time.time()


def _keepalive_loop() -> None:
    while True:
        try:
            time.sleep(_KEEPALIVE_INTERVAL)
            now = time.time()
            with _ACTIVE_LOCK:
                for ip, info in list(ACTIVE_CLIENTS.items()):
                    if info.get("streaming"):
                        info["ts"] = now
                    elif now - info.get("ts", 0) > _ACTIVE_TTL:
                        del ACTIVE_CLIENTS[ip]
        except Exception:
            pass


def _ensure_keepalive() -> None:
    global _KEEPALIVE_STARTED
    if _KEEPALIVE_STARTED:
        return
    _KEEPALIVE_STARTED = True
    th = __import__("threading").Thread(
        target=_keepalive_loop, name="client-keepalive", daemon=True
    )
    th.start()


def get_active_clients() -> list:
    now = time.time()
    with _ACTIVE_LOCK:
        dead = [
            k for k, v in ACTIVE_CLIENTS.items()
            if (not v.get("streaming")) and (now - v.get("ts", 0) > _ACTIVE_TTL)
        ]
        for k in dead:
            del ACTIVE_CLIENTS[k]
        return [
            {"ip": ip, **info}
            for ip, info in sorted(ACTIVE_CLIENTS.items(), key=lambda x: -x[1].get("ts", 0))
        ]


def detect_lan_ip(prefer: Optional[str] = None) -> str:
    if prefer:
        return prefer
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _xml_arg(body: str, name: str) -> Optional[str]:
    m = re.search(rf"<{name}[^>]*>([^<]*)</{name}>", body)
    return m.group(1) if m else None


def _extract_search_query(criteria: str) -> str:
    """Вытащить строку поиска из UPnP SearchCriteria."""
    if not criteria:
        return ""
    c = (
        criteria.replace("&quot;", '"')
        .replace("&apos;", "'")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
    ).strip()
    if c in ("*", ""):
        return ""
    matches = re.findall(r'contains\s+"([^"]*)"', c, re.IGNORECASE)
    for m in reversed(matches):
        if m.strip():
            return m.strip()
    matches = re.findall(
        r'(?:dc:title|upnp:artist|upnp:album)\s*=\s*"([^"]*)"',
        c, re.IGNORECASE,
    )
    for m in reversed(matches):
        if m.strip():
            return m.strip()
    fallback = re.findall(r'"([^"]*)"', c)
    for m in fallback:
        m = m.strip()
        if m and not m.startswith("object."):
            return m
    return ""


class DLNAHandler(BaseHTTPRequestHandler):
    server_version = "DLNA-emu/0.1"
    protocol_version = "HTTP/1.1"

    # Обрыв соединения клиентом. Windows даёт ConnectionAbortedError
    # (WinError 10053/10054), Linux — BrokenPipeError/ConnectionResetError.
    # Ловим всё, чтобы не валить в лог трейсбек на каждый "next".
    _CONN_ERRORS = (
        BrokenPipeError,
        ConnectionResetError,
        ConnectionAbortedError,
        TimeoutError,
        OSError,
    )

    def log_message(self, fmt, *args):
        log.debug("%s - " + fmt, self.address_string(), *args)

    def _trace(self, method: str, extra: str = "", cover: bool = False) -> None:
        """Логировать запрос от DLNA-ресивера в dlna_emu.trace.

        cover=True (или /cover/*) — пишем на DEBUG, чтобы не забивать
        INFO-вид картинками обложек. Включить в INFO можно флагом
        trace.covers: true в конфиге или в web-панели.
        """
        if not _TRACE_CFG.get("enabled"):
            return
        ip = self.client_address[0] if self.client_address else "?"
        if cover and not _TRACE_CFG.get("covers"):
            trace_log.debug("[%s] %s %s", ip, method, self.path)
            return

        soap = (
            self.headers.get("SOAPACTION")
            or self.headers.get("SoapAction")
            or ""
        ).strip('"')
        rng = self.headers.get("Range") or ""
        ua = self.headers.get("User-Agent") or ""
        bits = []
        if soap:
            bits.append(f"SOAP={soap}")
        if rng:
            bits.append(f"Range={rng}")
        if ua:
            bits.append(f"UA={ua[:60]}")
        tail = (" | " + " ".join(bits)) if bits else ""
        if extra:
            trace_log.info("[%s] %s %s %s%s", ip, method, self.path, extra, tail)
        else:
            trace_log.info("[%s] %s %s%s", ip, method, self.path, tail)

    def _send(self, code: int, body: bytes, content_type: str = "text/xml; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.send_header("EXT", "")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # ---------- Range helpers -----------------------------------------

    def _parse_range(self, total: int):
        """Разобрать Range: bytes=X-Y. → (start, end, partial) или None (416)."""
        h = self.headers.get("Range") or ""
        if not h.startswith("bytes="):
            return 0, total - 1, False
        m = re.match(r"bytes=(\d*)-(\d*)", h)
        if not m:
            return 0, total - 1, False
        s, e = m.group(1), m.group(2)
        if not s and not e:
            return 0, total - 1, False
        if s and not e:
            start = int(s)
            end = total - 1
        elif not s and e:
            n = int(e)
            start = max(0, total - n)
            end = total - 1
        else:
            start = int(s)
            end = min(int(e), total - 1)
        if start > end or start >= total:
            return None
        return start, end, True

    def _serve_bytes_with_range(self, body: bytes, mime: str) -> None:
        """Отдать байты с Content-Length и поддержкой Range (перемотка)."""
        total = len(body)
        rng = self._parse_range(total) if total else (0, -1, False)
        if rng is None:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{total}")
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            return
        start, end, partial = rng
        chunk = body[start : end + 1] if total else b""
        try:
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", mime or "audio/mpeg")
            self.send_header("Content-Length", str(len(chunk)))
            self.send_header("Accept-Ranges", "bytes")
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(chunk)
        except self._CONN_ERRORS:
            log.debug("range send: client aborted")

    # ---------- HTTP verb handling ------------------------------------

    def do_HEAD(self):
        self.do_GET(head_only=True)

    def do_GET(self, head_only: bool = False):
        path = urlparse(self.path).path
        client_ip = self.client_address[0] if self.client_address else ""
        is_stream = (not head_only) and path.startswith("/stream/")
        try:
            return self._do_GET_impl(head_only=head_only)
        finally:
            if is_stream and client_ip:
                try:
                    end_active_stream(client_ip)
                except Exception:
                    pass

    def _do_GET_impl(self, head_only: bool = False):
        path = urlparse(self.path).path
        self._trace("HEAD" if head_only else "GET",
                    cover=path.startswith("/cover/"))
        if path in ("/", "/description.xml", "/DeviceDescription.xml"):
            body = self.server.description_xml.encode("utf-8")  # type: ignore
            self._send(200, body)
            return
        if path == "/ContentDirectory.xml":
            self._send(200, CONTENT_DIRECTORY_SCPD.encode("utf-8"))
            return
        if path == "/ConnectionManager.xml":
            self._send(200, CONNECTION_MANAGER_SCPD.encode("utf-8"))
            return

        m = re.match(r"^/cover/radio/([^/]+)/(\d+)(?:\.(?:jpe?g|png))?$", path)
        if m:
            ip = self.client_address[0] if self.client_address else ""
            listing = time.time() - RADIO_LIST_BROWSED.get(ip, 0.0) < 3.0
            self._serve_cover("radio", f"{m.group(1)}/{m.group(2)}", dynamic=True,
                              radio_wait=0.0 if listing else 5.0)
            return

        m = re.match(r"^/cover/(deezer|drivemusic|yandex|akniga|zvuk|kion|zaycev)/(?:a(\d+)/)?(\d+)(?:\.(?:jpe?g|png))?$", path)
        if m:
            self._serve_cover(m.group(1), m.group(3), acc=int(m.group(2) or 0))
            return

        m = re.match(r"^/cover/(file|local)/(.+)$", path)
        if m:
            ident = m.group(2)
            # albumArtURI часто с суффиксом .jpg
            if ident.lower().endswith((".jpg", ".jpeg", ".png")):
                base = ident.rsplit(".", 1)[0]
                # если это audio.ext.jpg — убрать только .jpg
                if base.lower().endswith((
                    ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".wma",
                )):
                    ident = base
            self._serve_cover(m.group(1), ident)
            return

        # Радио, файлы, flow/wave — не папки «Текущего плейлиста»: если ресивер начал
        # играть их, прежняя играющая папка больше не играет (список при возврате подрежется).
        # HEAD/пробы не считаем — только реальный GET.
        if not head_only and re.match(
                r"^/stream/(?:radio|deezer_flow|yandex-wave|net|file|local)/", path):
            try:
                tree = getattr(self.server, "tree", None)
                if tree and hasattr(tree, "release_playing"):
                    tree.release_playing(self.client_address[0] if self.client_address else "")
            except Exception:
                pass

        m = re.match(r"^/stream/radio/([^/]+)/(\d+)$", path)
        if m:
            self._stream_radio(unquote(m.group(1)), int(m.group(2)))
            return

        m = re.match(r"^/stream/deezer/(?:a(\d+)/)?(\d+)$", path)
        if m:
            with self._account("deezer", m.group(1)):
                self._stream_deezer(m.group(2))
            return

        # Flow-слот: /stream/deezer_flow/<config>/<index> — догрузка по 3 трека
        m = re.match(r"^/stream/deezer_flow/([^/]+)/(\d+)$", path)
        if m:
            self._stream_deezer_flow(unquote(m.group(1)), int(m.group(2)))
            return

        m = re.match(r"^/stream/drivemusic/(\d+)$", path)
        if m:
            self._stream_drivemusic(m.group(1))
            return

        m = re.match(r"^/stream/akniga/(\d+)/(\d+)$", path)
        if m:
            self._stream_akniga(m.group(1), int(m.group(2)))
            return

        m = re.match(r"^/stream/akniga/(\d+)$", path)
        if m:
            self._stream_akniga_full(m.group(1))
            return

        m = re.match(r"^/stream/yandex/(?:a(\d+)/)?(\d+)$", path)
        if m:
            with self._account("yandex", m.group(1)):
                self._stream_yandex(m.group(2))
            return

        m = re.match(r"^/stream/yandex-wave/(.+)$", path)
        if m:
            self._stream_yandex_wave(unquote(m.group(1)))
            return

        m = re.match(r"^/stream/zvuk/(?:a(\d+)/)?(\d+)$", path)
        if m:
            with self._account("zvuk", m.group(1)):
                self._stream_zvuk(m.group(2))
            return

        m = re.match(r"^/stream/kion/(?:a(\d+)/)?(\d+)$", path)
        if m:
            with self._account("kion", m.group(1)):
                self._stream_kion(m.group(2))
            return

        m = re.match(r"^/stream/promodj/(\d+)$", path)
        if m:
            self._stream_promodj(m.group(1))
            return

        m = re.match(r"^/stream/zaycev/(\d+)$", path)
        if m:
            self._stream_zaycev(m.group(1))
            return

        m = re.match(r"^/stream/net/([0-9a-f]{8})/(.+)$", path)
        if m:
            self._stream_net(m.group(1), m.group(2))
            return

        m = re.match(r"^/stream/file/(.+)$", path)
        if m:
            self._stream_local_path(m.group(1))
            return

        m = re.match(r"^/stream/local/(.+)$", path)
        if m:
            self._stream_local_path(m.group(1))
            return

        self._send(404, b"Not found", "text/plain")

    @staticmethod
    def _reconcile(tree, didl: str) -> str:
        """Заявлять в списке формат, который реально отдастся (FLAC или MP3)."""
        try:
            if hasattr(tree, "reconcile_formats"):
                return tree.reconcile_formats(didl)
        except Exception as e:
            log.debug("reconcile_formats: %s", e)
        return didl

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        text = body.decode("utf-8", errors="replace")
        soap_action = (
            self.headers.get("SOAPACTION")
            or self.headers.get("SoapAction")
            or ""
        ).strip('"')

        # Разбираем самые интересные поля, чтобы в логе было видно,
        # что именно запросил ресивер.
        action_name = (
            soap_action.split("#")[-1] if "#" in soap_action else soap_action
        ).strip()
        extra = action_name
        if action_name == "Browse":
            oid = _xml_arg(text, "ObjectID") or "?"
            flag = _xml_arg(text, "BrowseFlag") or "?"
            idx = _xml_arg(text, "StartingIndex") or "0"
            cnt = _xml_arg(text, "RequestedCount") or "0"
            extra = f"Browse oid={oid!r} flag={flag} start={idx} count={cnt}"
        elif action_name == "Search":
            oid = _xml_arg(text, "ContainerID") or "?"
            crit = _xml_arg(text, "SearchCriteria") or ""
            extra = f"Search cid={oid!r} criteria={crit[:150]!r}"
        self._trace("POST", extra)

        if "ContentDirectory" in path or "ContentDirectory" in soap_action:
            self._content_directory(soap_action, text)
            return
        if "ConnectionManager" in path or "ConnectionManager" in soap_action:
            self._connection_manager(soap_action)
            return
        self._send(404, b"Not found", "text/plain")

    # ---------- GENA (event subscription) ----------

    def do_SUBSCRIBE(self):
        sid = f"uuid:{self.server.device_uuid}:{int(time.time())}"  # type: ignore
        self.send_response(200)
        self.send_header("SID", sid)
        self.send_header("TIMEOUT", "Second-1800")
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def do_UNSUBSCRIBE(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def do_NOTIFY(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    # ---------- Streaming ----------------------------------------------

    def _serve_cover(self, kind: str, ident: str, acc: int = 0, dynamic: bool = False,
                     radio_wait: float = 0.0):
        """Обложка трека: локальный файл (тег/folder.jpg) или стриминг."""
        svc = getattr(self.server, "covers", None)
        pool = getattr(self.server, kind, None) if acc else None
        if pool is not None and hasattr(pool, "use"):
            with pool.use(acc):
                res = svc.get(kind, ident) if svc else None
        elif kind == "radio":
            res = svc.get(kind, ident, radio_wait=radio_wait) if svc else None
        else:
            res = svc.get(kind, ident) if svc else None
        if not res:
            self._send(404, b"no cover", "text/plain")
            return
        data, mime = res
        try:
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store" if dynamic else "public, max-age=86400")
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
        except self._CONN_ERRORS:
            log.debug("cover: client aborted")

    def _account(self, service: str, idx):
        """Контекст нужного аккаунта (из /stream/<svc>/aN/<id>) на время обработки запроса."""
        import contextlib
        if idx is None:
            return contextlib.nullcontext()
        tree = getattr(self.server, "tree", None)
        if tree is not None and hasattr(tree, "account_scope"):
            return tree.account_scope(service, int(idx))
        return contextlib.nullcontext()

    def _stream_radio(self, pid: str, index: int):
        radio: RadioLibrary = self.server.radio  # type: ignore
        st = radio.get_station(pid, index)
        if not st:
            self._send(404, b"station not found", "text/plain")
            return
        try:
            client_ip = self.client_address[0] if self.client_address else ""
            update_active_client(client_ip, "radio", st.name, f"radio:{pid}:{index}")
            with _ACTIVE_LOCK:
                RADIO_PLAYING[client_ip] = (pid, index, time.time())
        except Exception:
            pass
        if getattr(radio, "proxy", True) and self._proxy_radio(st):
            return
        self.send_response(302)
        self.send_header("Location", st.url)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def _proxy_radio(self, st) -> bool:
        """Открыть поток станции самим эмулятором и переслать ресиверу.
        False — не вышло (HLS, ошибка источника): вызывающий делает редирект."""
        if self.command == "HEAD":
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Accept-Ranges", "none")
            self.send_header("Connection", "close")
            self.end_headers()
            return True
        client_meta = (self.headers.get("Icy-MetaData") or "").strip() == "1"
        radio_lib = self.server.radio  # type: ignore
        retries = int(getattr(radio_lib, "proxy_retries", 5) or 0)
        res = radio_proxy.open_station(st.url, client_meta=client_meta, key=st.url, retries=retries)
        if not res:
            log.info("radio proxy: %s недоступна для проксирования → редирект", st.name)
            return False
        it, ctype, close = res
        popular = radio_lib.popular if getattr(radio_lib, "popular_active", lambda: False)() else None
        session = popular.begin(st.url, st.name) if popular is not None else None
        try:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("Accept-Ranges", "none")
            self.send_header("icy-name", st.name.encode("latin-1", "replace").decode("latin-1"))
            metaint = int(getattr(it, "metaint", 0) or 0)
            if metaint:
                self.send_header("icy-metaint", str(metaint))
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            for chunk in it:
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except self._CONN_ERRORS:
                    break
                if session is not None:
                    session.tick()      # «слушаете подряд N минут» → станция в популярном
        except self._CONN_ERRORS:
            log.debug("radio proxy: client aborted %s", st.name)
        except Exception as e:   # обрыв источника/таймаут чтения
            log.info("radio proxy %s: %s", st.name, e)
        finally:
            if session is not None:
                session.end()
            try:
                close()
            except Exception:
                pass
        return True

    def _stream_deezer_flow(self, cfg_key: str, index: int):
        """Слот виртуального Flow-плейлиста: догружаем треки по 3, затем стримим."""
        deezer: DeezerClient = self.server.deezer  # type: ignore
        config_id = "" if (not cfg_key or cfg_key == "default") else cfg_key
        # Сброс другого Flow — только когда реально включили этот плейлист
        try:
            deezer.activate_flow_playback(config_id)
        except Exception:
            pass
        try:
            tr = deezer.ensure_flow_index(config_id, index)
        except Exception as e:
            log.warning("deezer_flow %s[%s]: %s", cfg_key, index, e)
            tr = None
        if tr is None:
            self._send(503, b"flow slot unavailable", "text/plain")
            return
        log.info("deezer_flow %s[%d] → %s — %s", cfg_key, index, tr.artist, tr.title)
        # Заранее дотянуть слоты впереди (партиями по 3), пока идёт текущий трек
        try:
            deezer.ensure_flow_index(config_id, index + 3)
        except Exception:
            pass
        self._stream_deezer(tr.id)

    def _stream_deezer(self, track_id: str):
        deezer: DeezerClient = self.server.deezer  # type: ignore
        result = deezer.open_stream(track_id)
        if not result or result[0] is None:
            self._send(503, b"stream unavailable", "text/plain")
            return
        try:
            client_ip = self.client_address[0] if self.client_address else ""
            oid = f"deezer:tr:{track_id}"
            tree = getattr(self.server, "tree", None)
            title = ""
            if tree and hasattr(tree, "resolve_track_title"):
                title = tree.resolve_track_title("deezer", oid) or ""
            if not title:
                title = f"Deezer {track_id}"
            update_active_client(client_ip, "deezer", title, track_id)
            if tree and hasattr(tree, "advance_current"):
                tree.advance_current("deezer", oid, client_ip=client_ip)
        except Exception:
            pass
        if len(result) == 3:
            data, mime, is_resp = result
            content_length = None
        else:
            data, mime, content_length, is_resp = result

        # Deezer отдаётся потоком (Blowfish/CBC). Content-Length известен,
        # но перемотку поддержать нельзя без полной буферизации в RAM.
        # Даём явный Accept-Ranges: none — часть клиентов тогда не пытается.
        try:
            self.send_response(200)
            self.send_header("Content-Type", mime or "audio/mpeg")
            self.send_header("Connection", "close")
            self.send_header("Accept-Ranges", "none")
            if content_length:
                self.send_header("Content-Length", str(content_length))
            else:
                self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            if is_resp:
                iterator = data.iter_content(chunk_size=16 * 1024)
            else:
                iterator = data

            if content_length:
                for chunk in iterator:
                    if not chunk:
                        continue
                    try:
                        self.wfile.write(chunk)
                    except self._CONN_ERRORS:
                        break
            else:
                for chunk in iterator:
                    if not chunk:
                        continue
                    try:
                        self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                        self.wfile.write(chunk)
                        self.wfile.write(b"\r\n")
                    except self._CONN_ERRORS:
                        break
                try:
                    self.wfile.write(b"0\r\n\r\n")
                except self._CONN_ERRORS:
                    pass
        except self._CONN_ERRORS:
            log.debug("deezer stream: client aborted %s", track_id)
        finally:
            if is_resp:
                try:
                    data.close()
                except Exception:
                    pass

    def _stream_drivemusic(self, track_id: str):
        dm: DriveMusicClient = self.server.drivemusic  # type: ignore
        # Страница трека берётся из сохранённого «Текущего плейлиста» (переживает перезапуск);
        # если её нет — клиент найдёт трек поиском по названию.
        page_url, title_hint = "", ""
        try:
            _tree = getattr(self.server, "tree", None)
            _oid = f"drivemusic:tr:{track_id}"
            if _tree and hasattr(_tree, "resolve_track_page_url"):
                page_url = _tree.resolve_track_page_url("drivemusic", _oid) or ""
            if _tree and hasattr(_tree, "resolve_track_title"):
                title_hint = _tree.resolve_track_title("drivemusic", _oid) or ""
        except Exception:
            pass
        result = dm.open_stream(track_id, page_url, title_hint=title_hint)
        if not result or result[0] is None:
            self._send(503, b"stream unavailable", "text/plain")
            return
        try:
            client_ip = self.client_address[0] if self.client_address else ""
            oid = f"drivemusic:tr:{track_id}"
            tree = getattr(self.server, "tree", None)
            title = ""
            if tree and hasattr(tree, "resolve_track_title"):
                title = tree.resolve_track_title("drivemusic", oid) or ""
            if not title:
                title = f"DriveMusic {track_id}"
            update_active_client(client_ip, "drivemusic", title, track_id)
            if tree and hasattr(tree, "advance_current"):
                tree.advance_current("drivemusic", oid, client_ip=client_ip)
        except Exception:
            pass
        data, mime, content_length, is_resp = result

        # Если размер известен и файл разумный — качаем целиком и отдаём
        # с Content-Length + Range. Тогда работает перемотка.
        # Если размер неизвестен/слишком большой — стримим как раньше.
        if content_length and content_length <= 64 * 1024 * 1024:
            try:
                body = b"".join(data.iter_content(chunk_size=64 * 1024))
                self._serve_bytes_with_range(body, mime or "audio/mpeg")
                return
            except self._CONN_ERRORS:
                log.debug("drivemusic buffer: client aborted %s", track_id)
                return
            except Exception as e:
                log.warning("drivemusic buffer %s: %s", track_id, e)
                # падаем на стриминг ниже
            finally:
                try:
                    data.close()
                except Exception:
                    pass

        # Стриминг (без Range)
        try:
            self.send_response(200)
            self.send_header("Content-Type", mime or "audio/mpeg")
            self.send_header("Connection", "close")
            self.send_header("Accept-Ranges", "none")
            if content_length:
                self.send_header("Content-Length", str(content_length))
            else:
                self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            iterator = data.iter_content(chunk_size=16 * 1024)
            if content_length:
                for chunk in iterator:
                    if not chunk:
                        continue
                    try:
                        self.wfile.write(chunk)
                    except self._CONN_ERRORS:
                        break
            else:
                for chunk in iterator:
                    if not chunk:
                        continue
                    try:
                        self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                        self.wfile.write(chunk)
                        self.wfile.write(b"\r\n")
                    except self._CONN_ERRORS:
                        break
                try:
                    self.wfile.write(b"0\r\n\r\n")
                except self._CONN_ERRORS:
                    pass
        except self._CONN_ERRORS:
            log.debug("drivemusic stream: client aborted %s", track_id)
        finally:
            try:
                data.close()
            except Exception:
                pass

    def _stream_akniga_full(self, bid: str):
        """Целиком mp3 книги, с пробросом Range (файлы бывают на сотни МБ)."""
        ak = getattr(self.server, "akniga", None)
        rng = self.headers.get("Range") or ""
        resp = ak.open_mp3(bid, rng) if ak else None
        if resp is None:
            self._send(503, b"akniga stream unavailable", "text/plain")
            return
        try:
            client_ip = self.client_address[0] if self.client_address else ""
            update_active_client(client_ip, "akniga", f"book {bid}", bid)
            tree = getattr(self.server, "tree", None)
            if tree and hasattr(tree, "advance_current"):
                tree.advance_current("akniga", f"akniga:ch:{bid}:all", client_ip=client_ip)
        except Exception:
            pass
        try:
            self.send_response(resp.status_code)
            ctype = resp.headers.get("Content-Type") or "audio/mpeg"
            self.send_header("Content-Type", ctype.split(";")[0])
            self.send_header("Accept-Ranges", "bytes")
            for h in ("Content-Length", "Content-Range"):
                if resp.headers.get(h):
                    self.send_header(h, resp.headers[h])
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command == "HEAD":
                return
            for chunk in resp.iter_content(64 * 1024):
                if not chunk:
                    continue
                try:
                    self.wfile.write(chunk)
                except self._CONN_ERRORS:
                    break
        except self._CONN_ERRORS:
            log.debug("akniga full: client aborted %s", bid)
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def _stream_akniga(self, bid: str, index: int):
        """Глава книги: ffmpeg → CBR MP3 с точным размером.

        Размер известен заранее (CBR 48 кГц), поэтому отдаём Content-Length
        и Range → плеер видит длину, работает перемотка, а обрыв источника
        не выглядит как «трек закончился» (ffmpeg перезапускается с места
        обрыва; если не вышло — соединение рвём, а не закрываем «чисто»).
        """
        ak = getattr(self.server, "akniga", None)
        if not ak:
            self._send(503, b"akniga stream unavailable", "text/plain")
            return
        book, ch = ak.find_chapter(bid, index)
        try:
            client_ip = self.client_address[0] if self.client_address else ""
            ch_title = (getattr(ch, "title", None) if ch else None) or f"Глава {index + 1}"
            update_active_client(client_ip, "akniga", ch_title, f"{bid}:{index}")
            tree = getattr(self.server, "tree", None)
            if tree and hasattr(tree, "advance_current"):
                tree.advance_current("akniga", f"akniga:ch:{bid}:{index}", client_ip=client_ip)
        except Exception:
            pass
        total = ak.chapter_total_bytes(ch)
        if total <= 0:
            return self._stream_akniga_unknown_len(bid, index)

        rng = self._parse_range(total)
        if rng is None:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{total}")
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            return
        start, end, partial = rng
        length = end - start + 1
        try:
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command == "HEAD":
                return
        except self._CONN_ERRORS:
            return

        fb = ak.chapter_frame_bytes
        sent = 0                     # байт реально отправлено клиенту
        retries = 0
        while sent < length:
            pos = start + sent       # абсолютная позиция в главе
            proc = ak.open_chapter_mp3(bid, index, pos // fb)
            if not proc or not proc.stdout:
                break
            skip = pos % fb          # докрутить до нужного байта внутри кадра
            got = 0
            try:
                while sent < length:
                    chunk = proc.stdout.read(32 * 1024)
                    if not chunk:
                        break
                    got += len(chunk)
                    if skip:
                        cut = min(skip, len(chunk))
                        chunk = chunk[cut:]
                        skip -= cut
                        if not chunk:
                            continue
                    chunk = chunk[: length - sent]
                    try:
                        self.wfile.write(chunk)
                    except self._CONN_ERRORS:
                        log.debug("akniga stream: client aborted %s/%s", bid, index)
                        return
                    sent += len(chunk)
            finally:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=2)
                except Exception:
                    pass
            if sent >= length:
                break
            # ffmpeg завершился раньше времени — пробуем продолжить с этого места
            retries = retries + 1 if got < 64 * 1024 else 0
            if retries > 3:
                break
            log.warning("akniga %s/%s: ffmpeg оборвался на %d/%d, перезапуск",
                        bid, index, sent, length)
        if sent < length:
            # не доставили обещанное — рвём соединение, чтобы плеер не принял
            # это за нормальный конец трека
            log.error("akniga %s/%s: недодано %d из %d байт", bid, index, sent, length)
            self.close_connection = True
            try:
                self.connection.shutdown(2)
            except Exception:
                pass

    def _stream_akniga_unknown_len(self, bid: str, index: int):
        """Запасной вариант: длина главы неизвестна → chunked без Range."""
        ak = getattr(self.server, "akniga", None)
        if self.command == "HEAD":
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Accept-Ranges", "none")
            self.send_header("Connection", "close")
            self.end_headers()
            return
        proc = ak.open_chapter_mp3(bid, index) if ak else None
        if not proc or not proc.stdout:
            self._send(503, b"akniga stream unavailable (ffmpeg?)", "text/plain")
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Connection", "close")
            self.send_header("Accept-Ranges", "none")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            while True:
                chunk = proc.stdout.read(32 * 1024)
                if not chunk:
                    break
                try:
                    self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                    self.wfile.write(chunk)
                    self.wfile.write(b"\r\n")
                except self._CONN_ERRORS:
                    break
            try:
                self.wfile.write(b"0\r\n\r\n")
            except self._CONN_ERRORS:
                pass
        except self._CONN_ERRORS:
            log.debug("akniga stream: client aborted %s/%s", bid, index)
        finally:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=2)
            except Exception:
                pass


    def _stream_yandex_wave(self, preset_id: str):
        """Непрерывный поток волны отключён.

        Склейка треков в одном HTTP-ответе (как m3u) ломается при смене
        кодека FLAC↔MP3 mid-stream — Content-Type один, декодер ресивера
        не переключается. Слушайте треки по одному из списка «Моя волна».
        """
        msg = (
            b"yandex-wave continuous stream disabled: "
            b"FLAC/MP3 mix mid-stream breaks receivers. "
            b"Play individual tracks from My Wave list."
        )
        self._send(410, msg, "text/plain")

    def _stream_yandex(self, track_id: str):
        """Стрим Яндекс.Музыки с режимами chunked | buffered | auto.

        auto (по умолчанию): пытаемся скачать трек целиком за buffer_timeout сек
        и отдать с Content-Length + Range — так работают старые ресиверы
        (Yamaha RX-V777 и т.п.). Если не успели — fallback на chunked.
        buffered: всегда буферизуем (с таймаутом).
        chunked: сразу стримим без буфера.
        """
        try:
            client_ip = self.client_address[0] if self.client_address else ""
            oid = f"yandex:tr:{track_id}"
            tree = getattr(self.server, "tree", None)
            title = ""
            if tree and hasattr(tree, "resolve_track_title"):
                title = tree.resolve_track_title("yandex", oid) or ""
            if not title:
                title = f"Яндекс {track_id}"
            update_active_client(client_ip, "yandex", title, track_id)
            if tree and hasattr(tree, "advance_current"):
                tree.advance_current("yandex", oid, client_ip=client_ip)
        except Exception:
            tree = getattr(self.server, "tree", None)
        # Если трек из «Моей волны» и близко к концу очереди — догрузить следующие
        try:
            if tree is not None and hasattr(tree, "on_yandex_track_playing"):
                tree.on_yandex_track_playing(track_id)
        except Exception as e:
            log.debug("wave ensure on stream: %s", e)

        ya: YandexMusicClient = self.server.yandex  # type: ignore
        result = ya.open_stream(track_id)
        if not result:
            self._send(503, b"stream unavailable", "text/plain")
            return
        resp, mime, content_length = result
        try:
            clen = int(content_length) if content_length else None
        except (TypeError, ValueError):
            clen = None

        mode = getattr(ya, "stream_mode", "auto") or "auto"
        mode = str(mode).lower().strip()
        timeout = float(getattr(ya, "buffer_timeout", 6.0) or 6.0)
        max_buf = 64 * 1024 * 1024  # 64 MB

        want_buffer = mode in ("auto", "buffered")
        if mode == "auto" and clen and clen > max_buf:
            want_buffer = False

        if want_buffer:
            try:
                body = self._read_with_timeout(resp, timeout=timeout, max_bytes=max_buf)
                if body is not None and len(body) > 0:
                    self._serve_bytes_with_range(body, mime or "audio/mpeg")
                    return
                # body is None → timeout/fail, fall through to chunked (if auto)
                if mode == "buffered":
                    self._send(503, b"buffer timeout", "text/plain")
                    return
            except self._CONN_ERRORS:
                log.debug("yandex buffer: client aborted %s", track_id)
                return
            except Exception as e:
                log.warning("yandex buffer %s: %s", track_id, e)
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
            # auto fallback: reopen stream for chunked delivery
            result = ya.open_stream(track_id)
            if not result:
                self._send(503, b"stream unavailable", "text/plain")
                return
            resp, mime, content_length = result
            try:
                clen = int(content_length) if content_length else None
            except (TypeError, ValueError):
                clen = None

        try:
            self.send_response(200)
            self.send_header("Content-Type", mime or "audio/mpeg")
            self.send_header("Connection", "close")
            self.send_header("Accept-Ranges", "none")
            if clen:
                self.send_header("Content-Length", str(clen))
            else:
                self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            iterator = resp.iter_content(chunk_size=16 * 1024)
            if clen:
                for chunk in iterator:
                    if not chunk:
                        continue
                    try:
                        self.wfile.write(chunk)
                    except self._CONN_ERRORS:
                        break
            else:
                for chunk in iterator:
                    if not chunk:
                        continue
                    try:
                        self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                        self.wfile.write(chunk)
                        self.wfile.write(b"\r\n")
                    except self._CONN_ERRORS:
                        break
                try:
                    self.wfile.write(b"0\r\n\r\n")
                except self._CONN_ERRORS:
                    pass
        except self._CONN_ERRORS:
            log.debug("yandex stream: client aborted %s", track_id)
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def _stream_kion(self, track_id: str):
        """Стрим КИОН.Музыки с режимами chunked | buffered | auto.

        auto (по умолчанию): пытаемся скачать трек целиком за buffer_timeout сек
        и отдать с Content-Length + Range — так работают старые ресиверы
        (Yamaha RX-V777 и т.п.). Если не успели — fallback на chunked.
        buffered: всегда буферизуем (с таймаутом).
        chunked: сразу стримим без буфера.
        """
        try:
            client_ip = self.client_address[0] if self.client_address else ""
            oid = f"kion:tr:{track_id}"
            tree = getattr(self.server, "tree", None)
            title = ""
            if tree and hasattr(tree, "resolve_track_title"):
                title = tree.resolve_track_title("kion", oid) or ""
            if not title:
                title = f"КИОН {track_id}"
            update_active_client(client_ip, "kion", title, track_id)
            if tree and hasattr(tree, "advance_current"):
                tree.advance_current("kion", oid, client_ip=client_ip)
        except Exception:
            tree = getattr(self.server, "tree", None)
        # Если трек из «Моей волны» и близко к концу очереди — догрузить следующие
        try:
            if tree is not None and hasattr(tree, "on_kion_track_playing"):
                tree.on_kion_track_playing(track_id)
        except Exception as e:
            log.debug("wave ensure on stream: %s", e)

        ki: KionMusicClient = self.server.kion  # type: ignore
        result = ki.open_stream(track_id)
        if not result:
            self._send(503, b"stream unavailable", "text/plain")
            return
        resp, mime, content_length = result
        try:
            clen = int(content_length) if content_length else None
        except (TypeError, ValueError):
            clen = None

        mode = getattr(ki, "stream_mode", "auto") or "auto"
        mode = str(mode).lower().strip()
        timeout = float(getattr(ki, "buffer_timeout", 6.0) or 6.0)
        max_buf = 64 * 1024 * 1024  # 64 MB

        want_buffer = mode in ("auto", "buffered")
        if mode == "auto" and clen and clen > max_buf:
            want_buffer = False

        if want_buffer:
            try:
                body = self._read_with_timeout(resp, timeout=timeout, max_bytes=max_buf)
                if body is not None and len(body) > 0:
                    self._serve_bytes_with_range(body, mime or "audio/mpeg")
                    return
                # body is None → timeout/fail, fall through to chunked (if auto)
                if mode == "buffered":
                    self._send(503, b"buffer timeout", "text/plain")
                    return
            except self._CONN_ERRORS:
                log.debug("kion buffer: client aborted %s", track_id)
                return
            except Exception as e:
                log.warning("kion buffer %s: %s", track_id, e)
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
            # auto fallback: reopen stream for chunked delivery
            result = ki.open_stream(track_id)
            if not result:
                self._send(503, b"stream unavailable", "text/plain")
                return
            resp, mime, content_length = result
            try:
                clen = int(content_length) if content_length else None
            except (TypeError, ValueError):
                clen = None

        try:
            self.send_response(200)
            self.send_header("Content-Type", mime or "audio/mpeg")
            self.send_header("Connection", "close")
            self.send_header("Accept-Ranges", "none")
            if clen:
                self.send_header("Content-Length", str(clen))
            else:
                self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            iterator = resp.iter_content(chunk_size=16 * 1024)
            if clen:
                for chunk in iterator:
                    if not chunk:
                        continue
                    try:
                        self.wfile.write(chunk)
                    except self._CONN_ERRORS:
                        break
            else:
                for chunk in iterator:
                    if not chunk:
                        continue
                    try:
                        self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                        self.wfile.write(chunk)
                        self.wfile.write(b"\r\n")
                    except self._CONN_ERRORS:
                        break
                try:
                    self.wfile.write(b"0\r\n\r\n")
                except self._CONN_ERRORS:
                    pass
        except self._CONN_ERRORS:
            log.debug("kion stream: client aborted %s", track_id)
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def _stream_zvuk(self, track_id: str):
        """Стрим Звук с режимами chunked | buffered | auto (как Яндекс)."""
        try:
            client_ip = self.client_address[0] if self.client_address else ""
            oid = f"zvuk:tr:{track_id}"
            tree = getattr(self.server, "tree", None)
            title = ""
            if tree and hasattr(tree, "resolve_track_title"):
                title = tree.resolve_track_title("zvuk", oid) or ""
            if not title:
                title = f"Звук {track_id}"
            update_active_client(client_ip, "zvuk", title, track_id)
            if tree and hasattr(tree, "advance_current"):
                tree.advance_current("zvuk", oid, client_ip=client_ip)
        except Exception:
            pass

        zv: ZvukClient = getattr(self.server, "zvuk", None)  # type: ignore
        if not zv:
            self._send(503, b"zvuk disabled", "text/plain")
            return
        result = zv.open_stream(track_id)
        if not result:
            self._send(503, b"stream unavailable", "text/plain")
            return
        resp, mime, content_length = result
        try:
            clen = int(content_length) if content_length else None
        except (TypeError, ValueError):
            clen = None

        mode = getattr(zv, "stream_mode", "auto") or "auto"
        mode = str(mode).lower().strip()
        timeout = float(getattr(zv, "buffer_timeout", 6.0) or 6.0)
        max_buf = 64 * 1024 * 1024

        want_buffer = mode in ("auto", "buffered")
        if mode == "auto" and clen and clen > max_buf:
            want_buffer = False

        if want_buffer:
            try:
                body = self._read_with_timeout(resp, timeout=timeout, max_bytes=max_buf)
                if body is not None and len(body) > 0:
                    self._serve_bytes_with_range(body, mime or "audio/mpeg")
                    return
                if mode == "buffered":
                    self._send(503, b"buffer timeout", "text/plain")
                    return
            except self._CONN_ERRORS:
                log.debug("zvuk buffer: client aborted %s", track_id)
                return
            except Exception as e:
                log.warning("zvuk buffer %s: %s", track_id, e)
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
            result = zv.open_stream(track_id)
            if not result:
                self._send(503, b"stream unavailable", "text/plain")
                return
            resp, mime, content_length = result
            try:
                clen = int(content_length) if content_length else None
            except (TypeError, ValueError):
                clen = None

        try:
            self.send_response(200)
            self.send_header("Content-Type", mime or "audio/mpeg")
            self.send_header("Connection", "close")
            self.send_header("Accept-Ranges", "none")
            if clen:
                self.send_header("Content-Length", str(clen))
            else:
                self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            iterator = resp.iter_content(chunk_size=16 * 1024)
            if clen:
                for chunk in iterator:
                    if not chunk:
                        continue
                    try:
                        self.wfile.write(chunk)
                    except self._CONN_ERRORS:
                        break
            else:
                for chunk in iterator:
                    if not chunk:
                        continue
                    try:
                        self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                        self.wfile.write(chunk)
                        self.wfile.write(b"\r\n")
                    except self._CONN_ERRORS:
                        break
                try:
                    self.wfile.write(b"0\r\n\r\n")
                except self._CONN_ERRORS:
                    pass
        except self._CONN_ERRORS:
            log.debug("zvuk stream: client aborted %s", track_id)
        finally:
            try:
                resp.close()
            except Exception:
                pass


    def _read_with_timeout(self, resp, timeout: float = 6.0, max_bytes: int = 64 * 1024 * 1024):
        """Скачать ответ целиком за timeout секунд. None при таймауте/ошибке."""
        import time as _time
        deadline = _time.monotonic() + max(0.5, timeout)
        chunks: list = []
        total = 0
        try:
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                chunks.append(chunk)
                total += len(chunk)
                if total > max_bytes:
                    log.warning("buffer: exceeded max_bytes (%d)", max_bytes)
                    return None
                if _time.monotonic() > deadline:
                    log.debug("buffer: timeout after %.1fs (%d bytes)", timeout, total)
                    return None
            return b"".join(chunks) if chunks else b""
        except Exception as e:
            log.warning("buffer read: %s", e)
            return None

    def _stream_promodj(self, track_id: str):
        pdj: PromoDjClient = getattr(self.server, "promodj", None)  # type: ignore
        if not pdj or not pdj.enabled:
            self._send(503, b"promodj disabled", "text/plain")
            return
        result = pdj.open_stream(track_id)
        if not result:
            self._send(503, b"stream unavailable", "text/plain")
            return
        try:
            client_ip = self.client_address[0] if self.client_address else ""
            oid = f"promodj:tr:{track_id}"
            tree = getattr(self.server, "tree", None)
            title = ""
            if tree and hasattr(tree, "resolve_track_title"):
                title = tree.resolve_track_title("promodj", oid) or ""
            if not title:
                title = f"PromoDJ {track_id}"
            update_active_client(client_ip, "promodj", title, track_id)
            if tree and hasattr(tree, "advance_current"):
                tree.advance_current("promodj", oid, client_ip=client_ip)
        except Exception:
            pass
        data, mime, content_length, is_resp = result

        # Буферизация с таймаутом (как Yandex/Zvuk): иначе на медленном CDN
        # ресивер «висит» на старте; длинные миксы > limit уходят в chunked.
        max_buf = int(getattr(pdj, "buffer_limit_bytes", 64 * 1024 * 1024) or (64 * 1024 * 1024))
        timeout = float(getattr(pdj, "buffer_timeout", 6.0) or 6.0)
        mode = (getattr(pdj, "stream_mode", "auto") or "auto").lower()

        do_buffer = (
            is_resp
            and mode != "chunked"
            and content_length
            and content_length <= max_buf
        )
        if do_buffer or (is_resp and mode == "buffered" and content_length and content_length <= max_buf):
            try:
                body = self._read_with_timeout(data, timeout=timeout, max_bytes=max_buf)
                if body:
                    self._serve_bytes_with_range(body, mime or "audio/mpeg")
                    return
                # таймаут / слишком большой — fallback на chunked ниже
                log.debug("promodj buffer miss %s, fallback chunked", track_id)
            except Exception as e:
                log.warning("promodj buffer %s: %s", track_id, e)
            # не return — продолжаем chunked; resp ещё жив если timeout вернул None
            # но после частичного чтения resp может быть в плохом состоянии — переоткроем
            try:
                data.close()
            except Exception:
                pass
            result2 = pdj.open_stream(track_id)
            if not result2:
                self._send(503, b"stream unavailable", "text/plain")
                return
            data, mime, content_length, is_resp = result2

        try:
            self.send_response(200)
            self.send_header("Content-Type", mime or "audio/mpeg")
            self.send_header("Connection", "close")
            self.send_header("Accept-Ranges", "none")
            if content_length:
                self.send_header("Content-Length", str(content_length))
            else:
                self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            if is_resp:
                iterator = data.iter_content(chunk_size=16 * 1024)
            else:
                iterator = data
            if content_length:
                for chunk in iterator:
                    if not chunk:
                        continue
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        break
            else:
                for chunk in iterator:
                    if not chunk:
                        continue
                    try:
                        self.wfile.write(b"%x\r\n" % len(chunk))
                        self.wfile.write(chunk)
                        self.wfile.write(b"\r\n")
                    except (BrokenPipeError, ConnectionResetError):
                        break
                try:
                    self.wfile.write(b"0\r\n\r\n")
                except Exception:
                    pass
        except Exception as e:
            log.warning("promodj stream write: %s", e)
        finally:
            try:
                if is_resp:
                    data.close()
            except Exception:
                pass

    def _stream_zaycev(self, track_id: str):
        """Zaycev.net: ссылка на mp3 берётся через track/play/{hash} при каждом play."""
        zy: ZaycevClient = getattr(self.server, "zaycev", None)  # type: ignore
        if not zy or not zy.enabled:
            self._send(503, b"zaycev disabled", "text/plain")
            return
        result = zy.open_stream(track_id)
        if not result:
            self._send(503, b"stream unavailable", "text/plain")
            return
        try:
            client_ip = self.client_address[0] if self.client_address else ""
            oid = f"zaycev:tr:{track_id}"
            tree = getattr(self.server, "tree", None)
            title = ""
            if tree and hasattr(tree, "resolve_track_title"):
                title = tree.resolve_track_title("zaycev", oid) or ""
            if not title:
                title = f"Zaycev {track_id}"
            update_active_client(client_ip, "zaycev", title, track_id)
            # возобновляемый «Текущий плейлист»: запоминаем подборку и позицию
            if tree and hasattr(tree, "advance_current"):
                tree.advance_current("zaycev", oid, client_ip=client_ip)
        except Exception:
            pass
        data, mime, content_length, is_resp = result

        # Буфер с таймаутом (как Yandex/Zvuk/PromoDJ): с Content-Length и Range
        # работает перемотка, а медленный CDN не «вешает» ресивер на старте.
        max_buf = int(getattr(zy, "buffer_limit_bytes", 64 * 1024 * 1024) or (64 * 1024 * 1024))
        timeout = float(getattr(zy, "buffer_timeout", 6.0) or 6.0)
        mode = (getattr(zy, "stream_mode", "auto") or "auto").lower()

        do_buffer = (
            is_resp
            and mode != "chunked"
            and content_length
            and content_length <= max_buf
        )
        if do_buffer:
            try:
                body = self._read_with_timeout(data, timeout=timeout, max_bytes=max_buf)
                if body:
                    self._serve_bytes_with_range(body, mime or "audio/mpeg")
                    return
                log.debug("zaycev buffer miss %s, fallback chunked", track_id)
            except self._CONN_ERRORS:
                log.debug("zaycev buffer: client aborted %s", track_id)
                return
            except Exception as e:
                log.warning("zaycev buffer %s: %s", track_id, e)
            # после частичного чтения соединение «грязное» — открываем заново
            try:
                data.close()
            except Exception:
                pass
            result2 = zy.open_stream(track_id)
            if not result2:
                self._send(503, b"stream unavailable", "text/plain")
                return
            data, mime, content_length, is_resp = result2

        try:
            self.send_response(200)
            self.send_header("Content-Type", mime or "audio/mpeg")
            self.send_header("Connection", "close")
            self.send_header("Accept-Ranges", "none")
            if content_length:
                self.send_header("Content-Length", str(content_length))
            else:
                self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            iterator = data.iter_content(chunk_size=16 * 1024)
            if content_length:
                for chunk in iterator:
                    if not chunk:
                        continue
                    try:
                        self.wfile.write(chunk)
                    except self._CONN_ERRORS:
                        break
            else:
                for chunk in iterator:
                    if not chunk:
                        continue
                    try:
                        self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                        self.wfile.write(chunk)
                        self.wfile.write(b"\r\n")
                    except self._CONN_ERRORS:
                        break
                try:
                    self.wfile.write(b"0\r\n\r\n")
                except self._CONN_ERRORS:
                    pass
        except self._CONN_ERRORS:
            log.debug("zaycev stream: client aborted %s", track_id)
        except Exception as e:
            log.warning("zaycev stream write: %s", e)
        finally:
            try:
                data.close()
            except Exception:
                pass

    def _stream_net(self, sid: str, enc_path: str):
        """Стрим аудио с сетевого хранилища (WebDAV/FTP) без буферизации на диск/в RAM.

        Без Range → 200 + Content-Length (как для USB — совместимо со старыми Yamaha),
        с Range → 206 (перемотка), Range пробрасывается в WebDAV / REST в FTP.
        """
        import posixpath
        from .files_lib import AUDIO_EXT, mime_for
        from .netstorage import NetStorageError, norm_rel

        mgr = getattr(self.server, "netstorage", None)
        st = mgr.get(sid) if mgr else None
        if st is None:
            self._send(404, b"storage not found", "text/plain")
            return
        try:
            rel = norm_rel(unquote(enc_path))
        except NetStorageError:
            self._send(400, b"bad path", "text/plain")
            return
        # отдаём только аудио — как и в листинге (наружу не торчит остальное содержимое NAS)
        if not rel or posixpath.splitext(rel)[1].lower() not in AUDIO_EXT:
            self._send(403, b"forbidden", "text/plain")
            return

        mime = mime_for(rel)
        try:
            client_ip = self.client_address[0] if self.client_address else ""
            title = rel.rsplit("/", 1)[-1] if rel else "?"
            update_active_client(
                client_ip, "net", f"{st.name}: {title}", track_id=f"{sid}/{rel}",
            )
        except Exception:
            pass
        try:
            size = st.size_of(rel)
        except Exception as e:
            log.warning("net stream %s/%s: size: %s", st.name, rel, e)
            size = None

        start, end, partial = 0, None, False
        if size is not None and size > 0:
            rng = self._parse_range(size) if st.honor_range else (0, size - 1, False)
            if rng is None:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                return
            start, end_i, partial = rng
            end = end_i if partial else None
            length = end_i - start + 1
        else:
            length = size  # 0 или None (длина неизвестна → без Content-Length)

        if self.command == "HEAD":
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", mime)
            if length is not None:
                self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes" if (size and st.honor_range) else "none")
            self.send_header("Connection", "close")
            self.end_headers()
            return

        try:
            reader = st.open_read(rel, start, end)
        except NetStorageError as e:
            log.warning("net stream %s/%s: %s", st.name, rel, e)
            self._send(502, str(e).encode("utf-8", "replace"), "text/plain; charset=utf-8")
            return
        except Exception as e:
            log.warning("net stream %s/%s: %s", st.name, rel, e)
            self._send(502, b"storage error", "text/plain")
            return

        log.info("net stream «%s»: %s%s", st.name, rel, f" (from {start})" if start else "")
        try:
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", mime)
            if length is not None:
                self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes" if (size and st.honor_range) else "none")
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Connection", "close")
            self.end_headers()
            while True:
                chunk = reader.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except self._CONN_ERRORS:
            log.debug("net stream: client aborted")
        except Exception as e:
            log.warning("net stream %s/%s: %s", st.name, rel, e)
        finally:
            reader.close()

    def _stream_local_path(self, encoded: str):
        from urllib.parse import unquote as _uq
        from .files_lib import mime_for
        files = getattr(self.server, "files", None)
        raw = _uq(encoded)
        path = None
        if files:
            path = files.resolve_file(encoded) or files.resolve_file(raw)
        if path is None:
            from pathlib import Path
            p = Path(raw)
            if p.is_file() and (str(p).startswith("/tmp/mnt/") or str(p).startswith("/opt/")):
                path = p
        if not path or not path.is_file():
            self._send(404, b"file not found", "text/plain")
            return
        try:
            client_ip = self.client_address[0] if self.client_address else ""
            title = path.name
            # USB vs прочий локальный файл
            svc = "usb"
            try:
                if files and files._allowed_path(path):
                    svc = "usb"
                else:
                    svc = "local"
            except Exception:
                svc = "local"
            # обложка через реестр covers (короткий id)
            cover = ""
            try:
                cov_svc = getattr(self.server, "covers", None)
                if cov_svc is not None and hasattr(cov_svc, "register_file_path"):
                    fid = cov_svc.register_file_path(str(path))
                    if fid:
                        cover = f"/cover/file/{fid}"
            except Exception:
                cover = ""
            update_active_client(
                client_ip, svc, title, track_id=str(path.name),
                cover=cover,
            )
            size = path.stat().st_size
            mime = mime_for(str(path))

            # USB/local library: preserve the older Yamaha-compatible streaming
            # behavior (full sequential response; do not honor Range requests).
            # Keep Range support for other local streams such as downloaded/API media.
            is_usb = False
            if files:
                try:
                    is_usb = files._allowed_path(path)
                except Exception:
                    roots = getattr(files, "roots", [])
                    try:
                        resolved = path.resolve()
                        is_usb = any(resolved == root.resolve() or root.resolve() in resolved.parents
                                     for root in roots)
                    except Exception:
                        is_usb = False
            if is_usb:
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(size))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Connection", "close")
                self.end_headers()
                if self.command == "HEAD":
                    return
                with open(path, "rb") as f:
                    while True:
                        chunk = f.read(64 * 1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                return

            rng = self._parse_range(size)
            if rng is None:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                return
            start, end, partial = rng
            length = end - start + 1
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command == "HEAD":
                return
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except self._CONN_ERRORS:
            log.debug("local stream: client aborted")
        except Exception as e:
            log.warning("local stream: %s", e)

    # ---------- SOAP ---------------------------------------------------

    def _soap_response(self, action: str, body_inner: str, service: str = "ContentDirectory"):
        xml = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            "<s:Body>"
            f'<u:{action}Response xmlns:u="urn:schemas-upnp-org:service:{service}:1">'
            f"{body_inner}"
            f"</u:{action}Response>"
            "</s:Body></s:Envelope>"
        )
        data = xml.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", 'text/xml; charset="utf-8"')
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.send_header("EXT", "")
        self.end_headers()
        self.wfile.write(data)

    def _content_directory(self, soap_action: str, body: str):
        action = soap_action.split("#")[-1] if "#" in soap_action else soap_action
        tree: ContentTree = self.server.tree  # type: ignore

        try:
            if action == "Browse":
                object_id = _xml_arg(body, "ObjectID") or "0"
                flag = _xml_arg(body, "BrowseFlag") or "BrowseDirectChildren"
                start = int(_xml_arg(body, "StartingIndex") or "0")
                count = int(_xml_arg(body, "RequestedCount") or "50")
                client_ip = self.client_address[0] if self.client_address else ""
                if hasattr(tree, "set_request_client"):
                    tree.set_request_client(client_ip)
                if client_ip and (object_id == "radio" or object_id.startswith("radio:")):
                    RADIO_LIST_BROWSED[client_ip] = time.time()
                if flag == "BrowseMetadata":
                    # Слот Flow — item с реальным названием (не container «Flow»)
                    if object_id.startswith("deezer:flowslot:"):
                        # deezer:flowslot:<cfg>:<index>
                        parts = object_id.split(":")
                        cfg_key = parts[2] if len(parts) > 2 else "default"
                        try:
                            idx = int(parts[3]) if len(parts) > 3 else 0
                        except ValueError:
                            idx = 0
                        config_id = "" if cfg_key == "default" else cfg_key
                        tr = None
                        try:
                            dz = getattr(tree, "deezer", None)
                            if dz and hasattr(dz, "ensure_flow_index"):
                                tr = dz.ensure_flow_index(config_id, idx)
                        except Exception as e:
                            log.warning("flowslot metadata %s: %s", object_id, e)
                        if tr is not None:
                            artist = (tr.artist or "").strip()
                            name = (tr.title or "").strip()
                            if artist and name:
                                title = f"{artist} — {name}"
                            else:
                                title = name or artist or f"Flow · {idx + 1}"
                            album = (tr.album or "").strip()
                            duration = int(tr.duration or 0)
                        else:
                            title = f"Flow · {idx + 1}"
                            artist, album, duration = "", "", 0
                        host = self.headers.get("Host") or "127.0.0.1"
                        url = f"http://{host}/stream/deezer_flow/{cfg_key}/{idx}"
                        art = ""
                        if tr is not None and getattr(tr, "id", None):
                            art = f"http://{host}/cover/deezer/{tr.id}.jpg"
                        h, rem = divmod(max(duration, 0), 3600)
                        mm, ss = divmod(rem, 60)
                        dur_s = f"{h}:{mm:02d}:{ss:02d}"
                        art_xml = (
                            f"<upnp:albumArtURI>{escape(art)}</upnp:albumArtURI>"
                            if art else ""
                        )
                        didl = (
                            '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
                            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
                            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
                            'xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">'
                            f'<item id="{escape(object_id)}" parentID="deezer:flowcfg:{escape(cfg_key)}" restricted="1">'
                            f"<dc:title>{escape(title)}</dc:title>"
                            f"<upnp:artist>{escape(artist)}</upnp:artist>"
                            f"<upnp:album>{escape(album)}</upnp:album>"
                            f"<dc:creator>{escape(artist)}</dc:creator>"
                            f"<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
                            f"{art_xml}"
                            f'<res protocolInfo="http-get:*:audio/flac:*" duration="{dur_s}">'
                            f"{escape(url)}</res>"
                            "</item></DIDL-Lite>"
                        )
                        n, total = 1, 1
                    else:
                        # Нормальный container + childCount (Onkyo/dLeyna смотрят на это)
                        title = object_id
                        child_count = 0
                        try:
                            if object_id.startswith("yandex:wave:"):
                                title = "Моя волна"
                                ya = getattr(tree, "yandex", None)
                                if ya and hasattr(ya, "_wave_cache"):
                                    pid = object_id[len("yandex:wave:"):] or "default"
                                    ch = (ya._wave_cache.get(pid) or {}).get("tracks") or []
                                    child_count = len(ch) or 1
                            elif object_id.startswith("deezer:flowcfg:"):
                                tail = object_id.split(":")[-1] or "Flow"
                                title = "Flow" if tail in ("default", "flowcfg") else tail
                                child_count = 100
                            else:
                                kids = tree._children(object_id) if hasattr(tree, "_children") else []
                                child_count = len(kids)
                                if ":" in object_id:
                                    title = object_id.rsplit(":", 1)[-1][:40]
                        except Exception:
                            child_count = 1
                        didl = (
                            '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
                            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
                            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
                            f'<container id="{escape(object_id)}" parentID="0" restricted="1" '
                            f'childCount="{int(child_count)}">'
                            f"<dc:title>{escape(title)}</dc:title>"
                            "<upnp:class>object.container.album.musicAlbum</upnp:class>"
                            "</container></DIDL-Lite>"
                        )
                        n, total = 1, 1
                else:
                    didl, n, total = tree.browse(object_id, start, count)
                    didl = self._reconcile(tree, didl)
                try:
                    upd = int(getattr(tree, "system_update_id", 1) or 1)
                except Exception:
                    upd = 1
                trace_log.debug(
                    "[%s] <- Browse oid=%s flag=%s n=%d total=%d",
                    self.client_address[0] if self.client_address else "?",
                    object_id, flag, n, total,
                )
                inner = (
                    f"<Result>{escape(didl)}</Result>"
                    f"<NumberReturned>{n}</NumberReturned>"
                    f"<TotalMatches>{total}</TotalMatches>"
                    f"<UpdateID>{upd}</UpdateID>"
                )
                self._soap_response("Browse", inner)
                return

            if action == "Search":
                container_id = _xml_arg(body, "ContainerID") or "0"
                criteria = _xml_arg(body, "SearchCriteria") or ""
                start = int(_xml_arg(body, "StartingIndex") or "0")
                count = int(_xml_arg(body, "RequestedCount") or "50")
                client_ip = self.client_address[0] if self.client_address else ""
                if hasattr(tree, "set_request_client"):
                    tree.set_request_client(client_ip)

                query = _extract_search_query(criteria)
                log.info(
                    "UPnP Search: container=%r criteria=%r query=%r",
                    container_id, criteria[:200], query,
                )

                if not query:
                    empty = (
                        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
                        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
                        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"/>'
                    )
                    inner = (
                        f"<Result>{escape(empty)}</Result>"
                        "<NumberReturned>0</NumberReturned>"
                        "<TotalMatches>0</TotalMatches>"
                        "<UpdateID>1</UpdateID>"
                    )
                    self._soap_response("Search", inner)
                    return

                didl, n, total = tree.search(container_id, query, start, count)
                didl = self._reconcile(tree, didl)
                trace_log.debug(
                    "[%s] <- Search cid=%s q=%r n=%d total=%d",
                    self.client_address[0] if self.client_address else "?",
                    container_id, query, n, total,
                )
                inner = (
                    f"<Result>{escape(didl)}</Result>"
                    f"<NumberReturned>{n}</NumberReturned>"
                    f"<TotalMatches>{total}</TotalMatches>"
                    "<UpdateID>1</UpdateID>"
                )
                self._soap_response("Search", inner)
                return

            if action == "GetSearchCapabilities":
                caps = "dc:title,upnp:artist,upnp:album,upnp:class"
                self._soap_response(
                    "GetSearchCapabilities",
                    f"<SearchCaps>{escape(caps)}</SearchCaps>",
                )
                return
            if action == "GetSortCapabilities":
                self._soap_response("GetSortCapabilities", "<SortCaps></SortCaps>")
                return
            if action == "GetSortExtensionCapabilities":
                self._soap_response(
                    "GetSortExtensionCapabilities",
                    "<SortExtensionCaps></SortExtensionCaps>",
                )
                return
            if action == "GetFeatureList":
                self._soap_response("GetFeatureList", "<FeatureList></FeatureList>")
                return
            if action == "GetSystemUpdateID":
                try:
                    upd = int(getattr(tree, "system_update_id", 1) or 1)
                except Exception:
                    upd = 1
                self._soap_response("GetSystemUpdateID", f"<Id>{upd}</Id>")
                return

            log.warning("unsupported CD action: %s", action)
            self._soap_response(action or "Browse", "")
        except Exception:
            log.error("ContentDirectory error:\n%s", traceback.format_exc())
            self._send(500, b"error", "text/plain")

    def _connection_manager(self, soap_action: str):
        action = soap_action.split("#")[-1] if "#" in soap_action else soap_action
        if action == "GetProtocolInfo":
            inner = (
                "<Source>http-get:*:audio/mpeg:*,http-get:*:audio/flac:*,"
                "http-get:*:audio/x-mpegurl:*</Source>"
                "<Sink></Sink>"
            )
            self._soap_response("GetProtocolInfo", inner, service="ConnectionManager")
            return
        if action == "GetCurrentConnectionIDs":
            self._soap_response(
                "GetCurrentConnectionIDs",
                "<ConnectionIDs>0</ConnectionIDs>",
                service="ConnectionManager",
            )
            return
        if action == "GetCurrentConnectionInfo":
            inner = (
                "<RcsID>-1</RcsID><AVTransportID>-1</AVTransportID>"
                "<ProtocolInfo></ProtocolInfo><PeerConnectionManager></PeerConnectionManager>"
                "<PeerConnectionID>-1</PeerConnectionID><Direction>Output</Direction>"
                "<Status>OK</Status>"
            )
            self._soap_response(
                "GetCurrentConnectionInfo", inner, service="ConnectionManager"
            )
            return
        self._soap_response(action or "GetProtocolInfo", "", service="ConnectionManager")


def build_description_xml(
    friendly_name: str,
    uuid: str,
    base_url: str,
) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0"
      xmlns:dlna="urn:schemas-dlna-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>
    <friendlyName>{escape(friendly_name)}</friendlyName>
    <manufacturer>DLNA-emu</manufacturer>
    <manufacturerURL>https://github.com/local/dlna-emu</manufacturerURL>
    <modelDescription>Radio + Deezer virtual media server</modelDescription>
    <modelName>DLNA-emu</modelName>
    <modelNumber>1</modelNumber>
    <serialNumber>1</serialNumber>
    <UDN>uuid:{uuid}</UDN>
    <dlna:X_DLNADOC xmlns:dlna="urn:schemas-dlna-org:device-1-0">DMS-1.50</dlna:X_DLNADOC>
    <serviceList>
      <service>
        <serviceType>urn:schemas-upnp-org:service:ContentDirectory:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:ContentDirectory</serviceId>
        <SCPDURL>/ContentDirectory.xml</SCPDURL>
        <controlURL>/upnp/control/ContentDirectory</controlURL>
        <eventSubURL>/upnp/event/ContentDirectory</eventSubURL>
      </service>
      <service>
        <serviceType>urn:schemas-upnp-org:service:ConnectionManager:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:ConnectionManager</serviceId>
        <SCPDURL>/ConnectionManager.xml</SCPDURL>
        <controlURL>/upnp/control/ConnectionManager</controlURL>
        <eventSubURL>/upnp/event/ConnectionManager</eventSubURL>
      </service>
    </serviceList>
  </device>
</root>
"""


CONTENT_DIRECTORY_SCPD = """<?xml version="1.0" encoding="utf-8"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <actionList>
    <action><name>Browse</name>
      <argumentList>
        <argument><name>ObjectID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_ObjectID</relatedStateVariable></argument>
        <argument><name>BrowseFlag</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_BrowseFlag</relatedStateVariable></argument>
        <argument><name>Filter</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Filter</relatedStateVariable></argument>
        <argument><name>StartingIndex</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Index</relatedStateVariable></argument>
        <argument><name>RequestedCount</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
        <argument><name>SortCriteria</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_SortCriteria</relatedStateVariable></argument>
        <argument><name>Result</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Result</relatedStateVariable></argument>
        <argument><name>NumberReturned</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
        <argument><name>TotalMatches</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
        <argument><name>UpdateID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_UpdateID</relatedStateVariable></argument>
      </argumentList>
    </action>
    <action><name>Search</name>
      <argumentList>
        <argument><name>ContainerID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_ObjectID</relatedStateVariable></argument>
        <argument><name>SearchCriteria</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_SearchCriteria</relatedStateVariable></argument>
        <argument><name>Filter</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Filter</relatedStateVariable></argument>
        <argument><name>StartingIndex</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Index</relatedStateVariable></argument>
        <argument><name>RequestedCount</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
        <argument><name>SortCriteria</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_SortCriteria</relatedStateVariable></argument>
        <argument><name>Result</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Result</relatedStateVariable></argument>
        <argument><name>NumberReturned</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
        <argument><name>TotalMatches</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
        <argument><name>UpdateID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_UpdateID</relatedStateVariable></argument>
      </argumentList>
    </action>
    <action><name>GetSearchCapabilities</name>
      <argumentList>
        <argument><name>SearchCaps</name><direction>out</direction><relatedStateVariable>SearchCapabilities</relatedStateVariable></argument>
      </argumentList>
    </action>
    <action><name>GetSortCapabilities</name>
      <argumentList>
        <argument><name>SortCaps</name><direction>out</direction><relatedStateVariable>SortCapabilities</relatedStateVariable></argument>
      </argumentList>
    </action>
    <action><name>GetSystemUpdateID</name>
      <argumentList>
        <argument><name>Id</name><direction>out</direction><relatedStateVariable>SystemUpdateID</relatedStateVariable></argument>
      </argumentList>
    </action>
  </actionList>
  <serviceStateTable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_ObjectID</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Result</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_SearchCriteria</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_BrowseFlag</name><dataType>string</dataType><allowedValueList><allowedValue>BrowseMetadata</allowedValue><allowedValue>BrowseDirectChildren</allowedValue></allowedValueList></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Filter</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_SortCriteria</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Index</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Count</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_UpdateID</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>SearchCapabilities</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>SortCapabilities</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="yes"><name>SystemUpdateID</name><dataType>ui4</dataType></stateVariable>
  </serviceStateTable>
</scpd>
"""

CONNECTION_MANAGER_SCPD = """<?xml version="1.0" encoding="utf-8"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <actionList>
    <action><name>GetProtocolInfo</name>
      <argumentList>
        <argument><name>Source</name><direction>out</direction><relatedStateVariable>SourceProtocolInfo</relatedStateVariable></argument>
        <argument><name>Sink</name><direction>out</direction><relatedStateVariable>SinkProtocolInfo</relatedStateVariable></argument>
      </argumentList>
    </action>
    <action><name>GetCurrentConnectionIDs</name>
      <argumentList>
        <argument><name>ConnectionIDs</name><direction>out</direction><relatedStateVariable>CurrentConnectionIDs</relatedStateVariable></argument>
      </argumentList>
    </action>
    <action><name>GetCurrentConnectionInfo</name>
      <argumentList>
        <argument><name>ConnectionID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_ConnectionID</relatedStateVariable></argument>
        <argument><name>RcsID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_RcsID</relatedStateVariable></argument>
        <argument><name>AVTransportID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_AVTransportID</relatedStateVariable></argument>
        <argument><name>ProtocolInfo</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_ProtocolInfo</relatedStateVariable></argument>
        <argument><name>PeerConnectionManager</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_ConnectionManager</relatedStateVariable></argument>
        <argument><name>PeerConnectionID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_ConnectionID</relatedStateVariable></argument>
        <argument><name>Direction</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Direction</relatedStateVariable></argument>
        <argument><name>Status</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_ConnectionStatus</relatedStateVariable></argument>
      </argumentList>
    </action>
  </actionList>
  <serviceStateTable>
    <stateVariable sendEvents="no"><name>SourceProtocolInfo</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>SinkProtocolInfo</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>CurrentConnectionIDs</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_ConnectionStatus</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_ConnectionManager</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Direction</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_ProtocolInfo</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_ConnectionID</name><dataType>i4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_AVTransportID</name><dataType>i4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_RcsID</name><dataType>i4</dataType></stateVariable>
  </serviceStateTable>
</scpd>
"""


def run_server(cfg: dict, config_path: str = "config.yaml") -> None:
    server_cfg = cfg.get("server") or {}
    radio_cfg = cfg.get("radio") or {}
    deezer_cfg = cfg.get("deezer") or {}
    dm_cfg = cfg.get("drivemusic") or {}
    ak_cfg = cfg.get("akniga") or {}
    ya_cfg = cfg.get("yandex") or {}
    pdj_cfg = cfg.get("promodj") or {}
    zy_cfg = cfg.get("zaycev") or {}
    web_cfg = cfg.get("web") or {}
    files_cfg = cfg.get("files") or {}
    trace_cfg = cfg.get("trace") or {}

    # trace-флаги применяем к модульному словарю, чтобы изменения из webui
    # работали на лету.
    _t_on = trace_cfg.get("enabled", True)
    if isinstance(_t_on, str):
        _t_on = _t_on.lower() in ("1", "true", "yes", "on")
    _t_cov = trace_cfg.get("covers", False)
    if isinstance(_t_cov, str):
        _t_cov = _t_cov.lower() in ("1", "true", "yes", "on")
    _TRACE_CFG["enabled"] = bool(_t_on)
    _TRACE_CFG["covers"] = bool(_t_cov)

    name = server_cfg.get("name") or "DLNA-emu"
    host = server_cfg.get("host") or "0.0.0.0"
    port = int(server_cfg.get("port") or 8200)
    web_port = int(web_cfg.get("port") or 8088)
    web_enabled = web_cfg.get("enabled", True)
    if isinstance(web_enabled, str):
        web_enabled = web_enabled.lower() in ("1", "true", "yes", "on")

    announce_ip = detect_lan_ip(server_cfg.get("announce_ip"))
    base_url = f"http://{announce_ip}:{port}"

    device_uuid = str(uuid_lib.uuid5(uuid_lib.NAMESPACE_DNS, f"dlna-emu-{name}-{announce_ip}"))

    radio = RadioLibrary(
        radio_cfg.get("folder") or "",
        reload_on_browse=bool(radio_cfg.get("reload_on_browse", True)),
        auto_mp3_suffix=bool(radio_cfg.get("auto_mp3_suffix", True)),
        proxy=bool(radio_cfg.get("proxy", True)),
        proxy_retries=radio_cfg.get("proxy_retries", 5),
    )
    # «Ваше популярное радио»: статистика прослушивания лежит рядом с config.yaml
    try:
        from .radio_popular import RadioPopular
        radio.popular = RadioPopular(
            os.path.join(os.path.dirname(os.path.abspath(config_path)) or ".", "radio_popular.json"),
            min_minutes=radio_cfg.get("popular_min_minutes", 15),
            keep_days=radio_cfg.get("popular_keep_days", 30),
        )
        _pe = radio_cfg.get("popular_enabled")
        if isinstance(_pe, str):
            _pe = _pe.lower() in ("1", "true", "yes", "on")
        # галка из config.yaml; фактически работает только вместе с проксированием радио
        radio.popular_enabled = True if _pe is None else bool(_pe)
    except Exception as e:
        log.warning("radio popular: %s", e)
    from .accounts import AccountPool
    deezer = AccountPool("deezer", lambda tok: DeezerClient(
        arl=tok,
        quality=deezer_cfg.get("quality") or "mp3_128",
        cache_sec=int(deezer_cfg.get("cache_sec") or 300),
    ), deezer_cfg.get("arl"))
    deezer.login_all()

    dm_enabled = dm_cfg.get("enabled", True)
    if isinstance(dm_enabled, str):
        dm_enabled = dm_enabled.lower() in ("1", "true", "yes", "on")
    drivemusic = DriveMusicClient(
        cache_sec=int(dm_cfg.get("cache_sec") or 300),
        base_url=dm_cfg.get("base_url") or "https://ru.drivemusic.me",
        enabled=bool(dm_enabled),
    )

    ak_enabled = ak_cfg.get("enabled", True)
    if isinstance(ak_enabled, str):
        ak_enabled = ak_enabled.lower() in ("1", "true", "yes", "on")
    akniga = AknigaClient(
        cache_sec=int(ak_cfg.get("cache_sec") or 600),
        enabled=bool(ak_enabled),
        ffmpeg=str(ak_cfg.get("ffmpeg") or ""),
        chapter_kbps=int(ak_cfg.get("chapter_kbps") or 128),
        section_books_per_chunk=int(ak_cfg.get("section_books_per_chunk") or 500),
    )

    yandex = AccountPool("yandex", lambda tok: YandexMusicClient(
        token=tok,
        quality=ya_cfg.get("quality") or "mp3_320",
        cache_sec=int(ya_cfg.get("cache_sec") or 300),
        stream_mode=ya_cfg.get("stream_mode") or "auto",
        buffer_timeout=float(ya_cfg.get("buffer_timeout") or 6.0),
    ), ya_cfg.get("token"))
    yandex.login_all()

    zv_cfg = cfg.get("zvuk") or {}
    zvuk = AccountPool("zvuk", lambda tok: ZvukClient(
        token=tok,
        quality=zv_cfg.get("quality") or "high",
        cache_sec=int(zv_cfg.get("cache_sec") or 300),
        stream_mode=zv_cfg.get("stream_mode") or "auto",
        buffer_timeout=float(zv_cfg.get("buffer_timeout") or 6.0),
    ), zv_cfg.get("token"))
    zvuk.login_all()
    zvuk.set_keepalive(str(zv_cfg.get("keepalive", False)).lower() in ("1", "true", "yes", "on"))

    kion_cfg = cfg.get("kion") or {}
    kion = AccountPool("kion", lambda tok: KionMusicClient(
        token=tok,
        quality=kion_cfg.get("quality") or "mp3_320",
        cache_sec=int(kion_cfg.get("cache_sec") or 300),
        stream_mode=kion_cfg.get("stream_mode") or "auto",
        buffer_timeout=float(kion_cfg.get("buffer_timeout") or 6.0),
    ), kion_cfg.get("token"))
    kion.login_all()

    pdj_enabled = pdj_cfg.get("enabled", True)
    if isinstance(pdj_enabled, str):
        pdj_enabled = pdj_enabled.lower() in ("1", "true", "yes", "on")
    try:
        promodj = PromoDjClient(
            enabled=bool(pdj_enabled),
            cache_sec=int(pdj_cfg.get("cache_sec") or 300),
            tracks_per_chunk=int(pdj_cfg.get("tracks_per_chunk") or 500),
            stream_mode=str(pdj_cfg.get("stream_mode") or "auto"),
            buffer_timeout=float(pdj_cfg.get("buffer_timeout") or 6.0),
            buffer_limit_mb=float(pdj_cfg.get("buffer_limit_mb") or 64),
        )
    except TypeError as e:
        # совместимость со старым promodj_client без stream_mode
        log.warning("PromoDJ client init fallback: %s", e)
        promodj = PromoDjClient(
            enabled=bool(pdj_enabled),
            cache_sec=int(pdj_cfg.get("cache_sec") or 300),
            tracks_per_chunk=int(pdj_cfg.get("tracks_per_chunk") or 500),
        )
        promodj.stream_mode = str(pdj_cfg.get("stream_mode") or "auto")
        promodj.buffer_timeout = float(pdj_cfg.get("buffer_timeout") or 6.0)
        promodj.buffer_limit_mb = float(pdj_cfg.get("buffer_limit_mb") or 64)
        promodj.buffer_limit_bytes = int(promodj.buffer_limit_mb * 1024 * 1024)

    zy_enabled = zy_cfg.get("enabled", True)
    if isinstance(zy_enabled, str):
        zy_enabled = zy_enabled.lower() in ("1", "true", "yes", "on")
    zaycev = ZaycevClient(
        enabled=bool(zy_enabled),
        cache_sec=int(zy_cfg.get("cache_sec") or 300),
        tracks_per_chunk=int(zy_cfg.get("tracks_per_chunk") or 100),
        stream_mode=str(zy_cfg.get("stream_mode") or "auto"),
        buffer_timeout=float(zy_cfg.get("buffer_timeout") or 6.0),
        buffer_limit_mb=float(zy_cfg.get("buffer_limit_mb") or 64),
        base_url=str(zy_cfg.get("base_url") or "https://zaycev.net"),
    )

    roots = files_cfg.get("roots") or files_cfg.get("root") or []
    if isinstance(roots, str):
        roots = [roots]
    roots = [r for r in roots if r]
    from .files_lib import FilesLibrary
    pl_folders = files_cfg.get("playlists_folder") or files_cfg.get("playlists_folders") or []
    if isinstance(pl_folders, str):
        pl_folders = [ln.strip() for ln in pl_folders.replace(",", "\n").splitlines()]
    pl_folders = [f for f in pl_folders if f]
    # плейлисты нужны, даже если корни не заданы, а папка с .m3u указана
    files = FilesLibrary(roots, playlist_folders=pl_folders) if (roots or pl_folders) else None

    from .netstorage import NetStorageManager
    netstorage = NetStorageManager(cfg.get("network_storages") or [])

    covers_cfg = cfg.get("covers") or {}
    covers_on = covers_cfg.get("enabled", True)
    if isinstance(covers_on, str):
        covers_on = covers_on.lower() in ("1", "true", "yes", "on")
    covers_profile = covers_cfg.get("dlna_profile", False)
    if isinstance(covers_profile, str):
        covers_profile = covers_profile.lower() in ("1", "true", "yes", "on")

    # Отдельный конфиг для избранного аудиокниг (рядом с основным config.yaml)
    fav_path = os.path.join(os.path.dirname(os.path.abspath(config_path)) or ".", "akniga_favorites.json")

    tree = ContentTree(
        radio=radio,
        deezer=deezer,
        base_url=base_url,
        files=files,
        drivemusic=drivemusic,
        yandex=yandex,
        akniga=akniga,
        promodj=promodj,
        zvuk=zvuk,
        kion=kion,
        zaycev=zaycev,
        netstorage=netstorage,
        covers_enabled=bool(covers_on),
        covers_dlna_profile=bool(covers_profile),
        favorites_path=fav_path,
    )
    # Настройки «Текущего плейлиста» (веб-панель → «Текущие плейлисты»)
    try:
        _cp = cfg.get("current_playlists") or {}
        def _cp_bool(v, d):
            if v is None:
                return d
            return v.lower() in ("1", "true", "yes", "on") if isinstance(v, str) else bool(v)
        tree.configure_current(
            max_folders=_cp.get("max_folders", 100),
            skip_artist_repeats=_cp.get("skip_artist_repeats", 6),
            skip_same_album=_cp_bool(_cp.get("skip_same_album"), True),
        )
    except Exception as e:
        log.warning("current_playlists settings: %s", e)
    # Автоочистка «Текущий плейлист» по сроку из config.yaml
    try:
        _cp = cfg.get("current_playlists") or {}
        _days = float(_cp.get("max_idle_days") or 0)
        if _days > 0:
            n = tree.prune_current_playlists(_days)
            if n:
                log.info("startup: pruned %d current playlists (>%s days idle)", n, _days)
    except Exception as e:
        log.debug("startup prune current: %s", e)
    from .covers import CoverService
    from .radio_meta import RadioNowPlaying
    radio_on = lambda v, d=True: (v.lower() in ("1", "true", "yes", "on") if isinstance(v, str) else bool(d if v is None else v))
    tree.radio_covers = radio_on(covers_cfg.get("radio"))
    radio_meta = RadioNowPlaying()
    covers = CoverService(
        radio=radio,
        radio_meta=radio_meta,
        radio_active=radio_is_playing,
        radio_nowplaying=radio_on(covers_cfg.get("radio_nowplaying")),
        yandex=yandex,
        deezer=deezer,
        drivemusic=drivemusic,
        akniga=akniga,
        zvuk=zvuk,
        kion=kion,
        zaycev=zaycev,
        files=files,
        max_bytes=int(covers_cfg.get("max_kb") or 2048) * 1024,
        cache_items=int(covers_cfg.get("cache_items") or 48),
        max_px=int(covers_cfg.get("max_px", 600) or 0),
    )
    description = build_description_xml(name, device_uuid, base_url)

    httpd = ThreadingHTTPServer((host, port), DLNAHandler)
    try:
        tree.set_format_probe(
            float(4.0 if server_cfg.get("format_probe_timeout") in (None, "") else server_cfg.get("format_probe_timeout")),
            float(server_cfg.get("format_cache_sec") or 1800),
        )
    except (TypeError, ValueError):
        pass
    httpd.tree = tree  # type: ignore
    httpd.radio = radio  # type: ignore
    httpd.deezer = deezer  # type: ignore
    httpd.drivemusic = drivemusic  # type: ignore
    httpd.akniga = akniga  # type: ignore
    httpd.yandex = yandex  # type: ignore
    httpd.zvuk = zvuk  # type: ignore
    httpd.kion = kion  # type: ignore
    httpd.promodj = promodj  # type: ignore
    httpd.zaycev = zaycev  # type: ignore
    httpd.files = files  # type: ignore
    httpd.netstorage = netstorage  # type: ignore
    httpd.covers = covers  # type: ignore
    tree.covers_svc = covers  # type: ignore
    httpd.friendly_name = name  # type: ignore
    httpd.device_uuid = device_uuid  # type: ignore
    httpd.description_xml = description  # type: ignore

    ssdp = SSDPServer(
        uuid=device_uuid,
        location=f"{base_url}/description.xml",
        server_name=name,
        interval=15,
        announce_ip=announce_ip,
    )
    ssdp.start()

    web_httpd = None
    if web_enabled and web_port and web_port != port:
        from . import webui
        webui.set_state(
            cfg=cfg,
            config_path=config_path,
            radio=radio,
            deezer=deezer,
            drivemusic=drivemusic,
            akniga=akniga,
            yandex=yandex,
            zvuk=zvuk,
            kion=kion,
            promodj=promodj,
            zaycev=zaycev,
            tree=tree,
            announce_ip=announce_ip,
            ssdp=ssdp,
        )
        try:
            web_httpd = webui.start_webui(host, web_port)
            log.info("Web UI: http://%s:%s", announce_ip, web_port)
        except OSError as e:
            log.error("Web UI port %s failed: %s", web_port, e)
    elif web_port == port:
        log.warning("web.port == server.port — веб-панель отключена (нужен отдельный порт)")

    log.info("DLNA MediaServer «%s» at %s", name, base_url)
    log.info("Radio folder: %s (%d playlists)", radio.folder, len(radio.list_playlists()))
    log.info("Files roots: %s", roots or "(none)")
    log.info("Network storages: %d", len(netstorage.storages))
    log.info("Deezer: %s", "enabled" if deezer.enabled else "disabled (no ARL)")
    log.info("DriveMusic: %s", "enabled" if drivemusic.enabled else "disabled")
    log.info("Zaycev.net: %s", "enabled" if zaycev.enabled else "disabled")
    log.info(
        "Akniga: %s%s (книг на страницу раздела: %s)",
        "enabled" if akniga.enabled else "disabled",
        "" if akniga.ffmpeg_ok else " (нет ffmpeg — главы не заиграют)",
        akniga.section_books_per_chunk,
    )
    log.info("Yandex Music: %s", "enabled" if yandex.enabled else "disabled (no token)")
    log.info("Zvuk: %s", "enabled" if zvuk.enabled else "disabled (no token)")
    log.info("Kion Music: %s", "enabled" if kion.enabled else "disabled (no token)")
    log.info("Covers: %s", "enabled" if covers_on else "disabled")
    log.info(
        "Trace: %s%s",
        "enabled" if _TRACE_CFG["enabled"] else "disabled",
        " (+covers)" if _TRACE_CFG["covers"] else "",
    )
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        log.info("stopping…")
    finally:
        ssdp.stop()
        if web_httpd:
            try:
                web_httpd.shutdown()
            except Exception:
                pass
        httpd.server_close()