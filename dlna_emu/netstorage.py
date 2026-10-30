"""Сетевые хранилища: WebDAV и FTP/FTPS.

Папки листаются «на лету» (с коротким кэшем), аудио стримится прямо с хранилища
в ресивер — на диск роутера ничего не скачивается и в RAM целиком не грузится.

Зависимости: только stdlib (ftplib, ssl, xml) и requests (уже есть в проекте).
"""
from __future__ import annotations

import ftplib
import hashlib
import logging
import posixpath
import re
import socket
import ssl
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote, urlparse

from .files_lib import AUDIO_EXT

log = logging.getLogger("dlna_emu.netstorage")

# Служебные папки NAS/Windows, которые нет смысла показывать на ресивере
_SKIP_NAMES = {"#recycle", "$recycle.bin", "system volume information", "lost+found"}


class NetStorageError(Exception):
    """Понятная пользователю ошибка хранилища (показывается в логе и в папке на ресивере)."""


@dataclass
class NetEntry:
    name: str
    is_dir: bool
    size: int = -1


def _bool(v, default: bool) -> bool:
    if v is None:
        return default
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on", "да")
    return bool(v)


def norm_rel(rel: str) -> str:
    """Нормализовать относительный путь внутри хранилища: без '..', без лишних '/'."""
    parts = [p for p in (rel or "").replace("\\", "/").split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        raise NetStorageError("недопустимый путь")
    return "/".join(parts)


def _nat_key(s: str):
    # «2 - x» раньше «10 - y»; split с группой даёт чередование str,int,str… — сравнимо
    return [int(t) if t.isdigit() else t.casefold() for t in re.split(r"(\d+)", s)]


class NetReader:
    """Поток байт удалённого файла (уже спозиционированный на нужный offset)."""

    def __init__(self, read_fn: Callable[[int], bytes], close_fn: Callable[[], None],
                 length: Optional[int] = None):
        self._read = read_fn
        self._close = close_fn
        self.remaining = length

    def read(self, n: int = 65536) -> bytes:
        if self.remaining is not None:
            if self.remaining <= 0:
                return b""
            n = min(n, self.remaining)
        data = self._read(n)
        if self.remaining is not None:
            self.remaining -= len(data)
        return data

    def close(self) -> None:
        try:
            self._close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _discard(read_fn: Callable[[int], bytes], n: int) -> None:
    """Пропустить n байт (если сервер не умеет Range/REST)."""
    while n > 0:
        chunk = read_fn(min(n, 65536))
        if not chunk:
            break
        n -= len(chunk)


# ============================================================
#  Базовый класс
# ============================================================

class BaseStorage:
    kind = ""
    # False → всегда отдавать файл целиком (200), игнорируя Range: как USB-файлы для Yamaha
    honor_range = True
    MAX_CACHE_DIRS = 64
    MAX_SIZES = 5000

    def __init__(self, name: str, cache_sec: float = 60.0):
        self.name = name
        # стабильный короткий id (не зависит от порядка в конфиге; меняется только с именем)
        self.sid = hashlib.md5(name.encode("utf-8")).hexdigest()[:8]
        self.cache_sec = float(cache_sec)
        self._cache: Dict[str, Tuple[float, List[NetEntry]]] = {}
        self._sizes: Dict[str, int] = {}
        self._lock = threading.Lock()

    # ----- реализуют наследники -----
    def _list_raw(self, rel: str) -> List[NetEntry]:
        raise NotImplementedError

    def _stat_size(self, rel: str) -> Optional[int]:
        return None

    def open_read(self, rel: str, start: int = 0, end: Optional[int] = None) -> NetReader:
        raise NotImplementedError

    def describe(self) -> str:
        return self.name

    # ----- общее -----
    def list_dir(self, rel: str = "") -> List[NetEntry]:
        rel = norm_rel(rel)
        now = time.time()
        with self._lock:
            hit = self._cache.get(rel)
            if hit and now - hit[0] < self.cache_sec:
                return hit[1]
        raw = self._list_raw(rel)
        entries: List[NetEntry] = []
        for e in raw:
            n = e.name
            if not n or n.startswith(".") or n.startswith("@") or n.casefold() in _SKIP_NAMES:
                continue
            if e.is_dir or posixpath.splitext(n)[1].lower() in AUDIO_EXT:
                entries.append(e)
        entries.sort(key=lambda e: (not e.is_dir, _nat_key(e.name)))
        with self._lock:
            if len(self._cache) >= self.MAX_CACHE_DIRS:
                oldest = min(self._cache, key=lambda k: self._cache[k][0])
                self._cache.pop(oldest, None)
            self._cache[rel] = (now, entries)
            if len(self._sizes) > self.MAX_SIZES:
                self._sizes.clear()
            for e in entries:
                if not e.is_dir and e.size >= 0:
                    self._sizes[f"{rel}/{e.name}" if rel else e.name] = e.size
        return entries

    def size_of(self, rel: str) -> Optional[int]:
        """Размер файла в байтах (нужен для Content-Length/Range) или None."""
        rel = norm_rel(rel)
        with self._lock:
            s = self._sizes.get(rel)
        if s is not None:
            return s
        try:
            s = self._stat_size(rel)
        except Exception as e:
            log.debug("%s: stat %s: %s", self.name, rel, e)
            s = None
        if s is not None and s >= 0:
            return s
        parent = rel.rpartition("/")[0]
        try:
            self.list_dir(parent)
        except Exception:
            return None
        with self._lock:
            return self._sizes.get(rel)

    def test(self) -> str:
        """Проверка соединения: вернуть краткое описание или бросить NetStorageError."""
        self._cache.clear()
        entries = self.list_dir("")
        d = sum(1 for e in entries if e.is_dir)
        return f"OK: папок {d}, файлов {len(entries) - d}"


# ============================================================
#  WebDAV
# ============================================================

_PROPFIND_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:"><d:prop>'
    "<d:resourcetype/><d:getcontentlength/>"
    "</d:prop></d:propfind>"
)
_DAV = "{DAV:}"


class WebDavStorage(BaseStorage):
    kind = "webdav"

    def __init__(self, name: str, url: str, user: str = "", password: str = "",
                 auth: str = "basic", verify_ssl: bool = True, timeout: float = 20.0,
                 cache_sec: float = 60.0):
        super().__init__(name, cache_sec)
        import requests

        u = url.strip()
        low = u.lower()
        if low.startswith("davs://"):
            u = "https://" + u[7:]
        elif low.startswith("dav://"):
            u = "http://" + u[6:]
        if not re.match(r"^https?://", u, re.I):
            raise NetStorageError(f"WebDAV: url должен начинаться с http:// или https:// ({url!r})")
        self.base = u.rstrip("/")
        self.timeout = float(timeout)
        self.verify_ssl = bool(verify_ssl)

        s = requests.Session()
        if user:
            if str(auth).lower() == "digest":
                from requests.auth import HTTPDigestAuth
                s.auth = HTTPDigestAuth(user, password)
            else:
                s.auth = (user, password)
        s.verify = self.verify_ssl
        s.headers["User-Agent"] = "DLNA-emu"
        if not self.verify_ssl:
            try:
                import urllib3
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            except Exception:
                pass
        self.s = s

    def describe(self) -> str:
        return f"WebDAV {self.base}"

    def _url(self, rel: str, is_dir: bool = False) -> str:
        u = self.base + ("/" + quote(rel, safe="/") if rel else "")
        if is_dir:
            u += "/"
        return u

    def _check(self, r, what: str = "") -> None:
        c = r.status_code
        if c in (200, 206, 207):
            return
        if c == 401:
            raise NetStorageError("WebDAV: 401 — неверный логин/пароль (или нужен auth: digest)")
        if c == 403:
            raise NetStorageError("WebDAV: 403 — доступ запрещён")
        if c == 404:
            raise NetStorageError(f"WebDAV: 404 — не найдено {what}".strip())
        raise NetStorageError(f"WebDAV: HTTP {c} {what}".strip())

    def _req(self, method: str, url: str, **kw):
        import requests
        try:
            return self.s.request(method, url, **kw)
        except requests.exceptions.SSLError as e:
            raise NetStorageError(
                "WebDAV: ошибка SSL-сертификата (для самоподписанного — verify_ssl: false): "
                + str(e)[:120]
            )
        except requests.exceptions.RequestException as e:
            raise NetStorageError(f"WebDAV: нет связи с хранилищем: {str(e)[:120]}")

    def _propfind(self, url: str, depth: str):
        r = self._req(
            "PROPFIND", url, data=_PROPFIND_BODY.encode("utf-8"),
            headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
            timeout=self.timeout,
        )
        self._check(r, url)
        try:
            return ET.fromstring(r.content)
        except ET.ParseError as e:
            raise NetStorageError(f"WebDAV: некорректный ответ PROPFIND: {e}")

    @staticmethod
    def _props(resp) -> Tuple[bool, int]:
        is_dir, size = False, -1
        for ps in resp.findall(_DAV + "propstat"):
            status = ps.findtext(_DAV + "status") or ""
            if status and " 200" not in status:
                continue
            prop = ps.find(_DAV + "prop")
            if prop is None:
                continue
            rt = prop.find(_DAV + "resourcetype")
            if rt is not None and rt.find(_DAV + "collection") is not None:
                is_dir = True
            cl = (prop.findtext(_DAV + "getcontentlength") or "").strip()
            if cl.isdigit():
                size = int(cl)
        return is_dir, size

    def _list_raw(self, rel: str) -> List[NetEntry]:
        url = self._url(rel, True)
        root = self._propfind(url, "1")
        req_norm = unquote(urlparse(url).path).rstrip("/")
        out: List[NetEntry] = []
        for i, resp in enumerate(root.iter(_DAV + "response")):
            href = resp.findtext(_DAV + "href") or ""
            pn = unquote(urlparse(href).path).rstrip("/")
            name = posixpath.basename(pn)
            is_dir, size = self._props(resp)
            if pn == req_norm:
                continue  # сама папка
            if i == 0 and is_dir and name == posixpath.basename(req_norm):
                continue  # то же, если сервер за прокси с другим префиксом
            if not name:
                continue
            out.append(NetEntry(name, is_dir, size))
        return out

    def _stat_size(self, rel: str) -> Optional[int]:
        url = self._url(rel)
        r = self._req("HEAD", url, timeout=self.timeout, allow_redirects=True)
        if r.status_code == 200:
            cl = r.headers.get("Content-Length", "")
            if cl.isdigit():
                return int(cl)
        root = self._propfind(url, "0")
        for resp in root.iter(_DAV + "response"):
            _, size = self._props(resp)
            if size >= 0:
                return size
        return None

    def open_read(self, rel: str, start: int = 0, end: Optional[int] = None) -> NetReader:
        url = self._url(rel)
        headers = {}
        if start > 0 or end is not None:
            headers["Range"] = f"bytes={start}-" + ("" if end is None else str(end))
        r = self._req("GET", url, headers=headers, stream=True, timeout=(self.timeout, 60))
        try:
            self._check(r, rel)
        except Exception:
            r.close()
            raise
        raw = r.raw
        if start > 0 and r.status_code == 200:
            # сервер проигнорировал Range — доматываем вручную
            _discard(lambda n: raw.read(n), start)
        length = None if end is None else end - start + 1
        return NetReader(lambda n: raw.read(n), r.close, length)


# ============================================================
#  FTP / FTPS
# ============================================================

_UNIX_RE = re.compile(
    r"^([\-dlbcps])[rwxsStTl\-+@.]{8,10}\s+\d+\s+\S+\s+(?:\S+\s+)?(\d+)\s+"
    r"(?:\w{3}\s+\d{1,2}\s+(?:\d{4}|\d{1,2}:\d{2})|\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2})\s+(.+)$"
)
_DOS_RE = re.compile(
    r"^(\d{2}-\d{2}-\d{2,4})\s+(\d{1,2}:\d{2}\s*[AaPp]?[Mm]?)\s+(<DIR>|\d+)\s+(.+)$"
)


class FtpStorage(BaseStorage):
    kind = "ftp"

    def __init__(self, name: str, host: str, port: int = 21, path: str = "",
                 user: str = "", password: str = "", passive: bool = True,
                 tls: bool = False, verify_ssl: bool = True, encoding: str = "utf-8",
                 timeout: float = 20.0, cache_sec: float = 60.0):
        super().__init__(name, cache_sec)
        if not host:
            raise NetStorageError("FTP: не задан host")
        self.host = host
        # port уже разрешён в build_storage (явный / из url / из host:PORT / 21)
        self.port = int(port) if port else 21
        p = (path or "").strip().strip("/")
        self.base_path = ("/" + p) if p else ""
        self.user = user or ""
        self.password = password or ""
        self.passive = bool(passive)
        self.tls = bool(tls)
        self.encoding = encoding or "utf-8"
        self.timeout = float(timeout)
        self._ctx = None
        if self.tls:
            ctx = ssl.create_default_context()
            if not verify_ssl:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            self._ctx = ctx
        self.kind = "ftps" if self.tls else "ftp"

    def describe(self) -> str:
        return f"{self.kind.upper()} {self.host}:{self.port}{self.base_path}"

    def _full(self, rel: str) -> str:
        parts = [p for p in (self.base_path.strip("/"), rel) if p]
        return "/" + "/".join(parts)

    def _connect(self, encoding: Optional[str] = None) -> ftplib.FTP:
        try:
            if self.tls:
                ftp: ftplib.FTP = ftplib.FTP_TLS(context=self._ctx, timeout=self.timeout)
            else:
                ftp = ftplib.FTP(timeout=self.timeout)
            ftp.encoding = encoding or self.encoding
            ftp.connect(self.host, self.port)
            if self.user:
                ftp.login(self.user, self.password)
            else:
                ftp.login("anonymous", "anonymous@")
            if self.tls:
                ftp.prot_p()  # type: ignore[attr-defined]
            ftp.set_pasv(self.passive)
            if (encoding or self.encoding).lower().replace("-", "") == "utf8":
                try:
                    ftp.sendcmd("OPTS UTF8 ON")
                except ftplib.all_errors:
                    pass
            return ftp
        except ftplib.error_perm as e:
            raise NetStorageError(f"FTP: отказ сервера: {str(e)[:100]}")
        except ssl.SSLCertVerificationError as e:
            raise NetStorageError(
                "FTPS: ошибка сертификата (для самоподписанного — verify_ssl: false): "
                + str(e)[:100]
            )
        except (OSError, EOFError, ftplib.Error) as e:
            raise NetStorageError(f"FTP: нет связи с {self.host}:{self.port}: {str(e)[:100]}")

    @staticmethod
    def _quit(ftp: Optional[ftplib.FTP]) -> None:
        if ftp is None:
            return
        try:
            ftp.quit()
        except Exception:
            try:
                ftp.close()
            except Exception:
                pass

    def _fetch_lines(self, ftp: ftplib.FTP, cmd: str) -> List[str]:
        """Выполнить MLSD/LIST и вернуть строки ответа.

        Читаем data-канал сами, а не через ftplib.retrlines: тот в конце делает
        TLS-unwrap() и падает на FTPS-серверах, закрывающих канал без close_notify.
        """
        conn = ftp.transfercmd(cmd)
        buf = bytearray()
        try:
            conn.settimeout(self.timeout + 10)
            while True:
                try:
                    chunk = conn.recv(65536)
                except ssl.SSLError as e:
                    if "EOF" in str(e).upper():
                        break
                    raise
                if not chunk:
                    break
                buf += chunk
        finally:
            try:
                conn.close()
            except Exception:
                pass
        try:
            ftp.voidresp()  # 226 Transfer complete
        except ftplib.error_temp:
            pass
        # strict: на кривой кодировке бросит UnicodeDecodeError → _list_raw попробует другую
        return bytes(buf).decode(ftp.encoding).splitlines()

    def _list_cwd(self, ftp: ftplib.FTP) -> List[NetEntry]:
        out: List[NetEntry] = []
        try:
            try:
                ftp.sendcmd("OPTS MLST type;size;")
            except ftplib.error_perm:
                pass
            mlsd_lines = self._fetch_lines(ftp, "MLSD")
        except (ftplib.error_perm, ftplib.error_reply):
            mlsd_lines = None  # MLSD не поддерживается → LIST
        if mlsd_lines is not None:
            for ln in mlsd_lines:
                facts_s, _, name = ln.partition(" ")
                if not name:
                    continue
                facts = {}
                for f in facts_s.split(";"):
                    k, _, v = f.partition("=")
                    if k:
                        facts[k.lower()] = v
                t = facts.get("type", "").lower()
                if t in ("cdir", "pdir") or name in (".", ".."):
                    continue
                if t == "dir":
                    out.append(NetEntry(name, True))
                elif t == "file":
                    sz = facts.get("size", "")
                    out.append(NetEntry(name, False, int(sz) if sz.isdigit() else -1))
                elif t.startswith("os.unix=slink") or t.startswith("os.unix=symlink"):
                    ext = posixpath.splitext(name)[1].lower()
                    out.append(NetEntry(name, ext not in AUDIO_EXT))
            return out

        try:
            lines = self._fetch_lines(ftp, "LIST")
        except ftplib.error_perm:
            return []  # «550 No files found» на пустой папке
        for ln in lines:
            m = _UNIX_RE.match(ln)
            if m:
                kind, size, name = m.group(1), m.group(2), m.group(3)
                if kind == "l":
                    name = name.split(" -> ")[0]
                    ext = posixpath.splitext(name)[1].lower()
                    out.append(NetEntry(name, ext not in AUDIO_EXT))
                elif kind == "d":
                    out.append(NetEntry(name, True))
                elif kind == "-":
                    out.append(NetEntry(name, False, int(size)))
                continue
            m = _DOS_RE.match(ln)
            if m:
                size, name = m.group(3), m.group(4)
                if size == "<DIR>":
                    out.append(NetEntry(name, True))
                else:
                    out.append(NetEntry(name, False, int(size)))
        return out

    def _list_raw(self, rel: str) -> List[NetEntry]:
        encs = [self.encoding] + [e for e in ("utf-8", "cp1251", "latin-1") if e != self.encoding]
        for enc in encs:
            ftp = self._connect(enc)
            try:
                try:
                    ftp.cwd(self._full(rel))
                except ftplib.error_perm as e:
                    raise NetStorageError(f"FTP: папка недоступна ({str(e)[:80]})")
                entries = self._list_cwd(ftp)
                if enc != self.encoding:
                    log.info("FTP %s: кодировка имён определена как %s", self.name, enc)
                    self.encoding = enc
                return entries
            except UnicodeError:
                continue  # имена не в этой кодировке — пробуем следующую
            except (OSError, EOFError, ftplib.Error) as e:
                raise NetStorageError(f"FTP: ошибка листинга: {str(e)[:100]}")
            finally:
                self._quit(ftp)
        raise NetStorageError("FTP: не удалось определить кодировку имён (задайте encoding)")

    def _stat_size(self, rel: str) -> Optional[int]:
        ftp = self._connect()
        try:
            ftp.voidcmd("TYPE I")
            return ftp.size(self._full(rel))
        except ftplib.all_errors:
            return None
        finally:
            self._quit(ftp)

    def open_read(self, rel: str, start: int = 0, end: Optional[int] = None) -> NetReader:
        full = self._full(rel)
        ftp = self._connect()
        conn = None
        skip = 0
        try:
            ftp.voidcmd("TYPE I")
            attempts = [start, 0] if start > 0 else [0]
            last_err: Optional[Exception] = None
            for rest in attempts:
                try:
                    conn = ftp.transfercmd("RETR " + full, rest=rest or None)
                    skip = start - rest
                    break
                except ftplib.error_perm as e:
                    last_err = e
                    continue
            if conn is None:
                raise NetStorageError(f"FTP: не удалось открыть файл ({str(last_err)[:80]})")
        except NetStorageError:
            self._quit(ftp)
            raise
        except (OSError, EOFError, ftplib.Error) as e:
            self._quit(ftp)
            raise NetStorageError(f"FTP: ошибка открытия файла: {str(e)[:100]}")

        conn.settimeout(self.timeout + 10)

        def _read(n: int) -> bytes:
            try:
                return conn.recv(n)
            except ssl.SSLError as e:
                # некоторые FTPS-серверы закрывают data-канал без close_notify
                if "EOF" in str(e).upper():
                    return b""
                raise

        def _close() -> None:
            try:
                conn.close()
            finally:
                try:
                    ftp.close()
                except Exception:
                    pass

        try:
            if skip > 0:
                _discard(_read, skip)
        except Exception:
            _close()
            raise
        length = None if end is None else end - start + 1
        return NetReader(_read, _close, length)


# ============================================================
#  Менеджер
# ============================================================

def build_storage(cfg: dict) -> BaseStorage:
    """Создать хранилище из одной записи network_storages."""
    if not isinstance(cfg, dict):
        raise NetStorageError("запись хранилища должна быть словарём")
    url = str(cfg.get("url") or "").strip()
    kind = str(cfg.get("type") or "").strip().lower()
    if not kind and url:
        scheme = urlparse(url).scheme.lower()
        kind = {"http": "webdav", "https": "webdav", "dav": "webdav", "davs": "webdav",
                "ftp": "ftp", "ftps": "ftps"}.get(scheme, "")
    if kind == "dav":
        kind = "webdav"
    if kind not in ("webdav", "ftp", "ftps"):
        raise NetStorageError(f"неизвестный type {kind!r} (нужен webdav | ftp | ftps)")

    cache_sec = float(cfg.get("cache_sec") or 60)
    timeout = float(cfg.get("timeout") or 20)
    user = str(cfg.get("user") or cfg.get("username") or "")
    password = str(cfg.get("password") or "")
    verify = _bool(cfg.get("verify_ssl"), True)

    if kind == "webdav":
        if not url:
            raise NetStorageError("WebDAV: не задан url")
        name = str(cfg.get("name") or "").strip() or (urlparse(url).hostname or "WebDAV")
        pu = urlparse(url)
        if pu.username and not user:
            user = unquote(pu.username)
            password = unquote(pu.password or "")
            url = url.replace(pu.netloc, pu.netloc.rsplit("@", 1)[-1], 1)
        st: BaseStorage = WebDavStorage(
            name, url, user, password, auth=str(cfg.get("auth") or "basic"),
            verify_ssl=verify, timeout=timeout, cache_sec=cache_sec,
        )
        st.honor_range = _bool(cfg.get("honor_range"), True)
        return st

    # ftp / ftps
    # Порт НЕ дописывается в host и не вшивается в конфиг автоматически.
    # Источники (по приоритету): явный cfg["port"] → порт из url → суффикс host:PORT → 21.
    host = str(cfg.get("host") or "").strip()
    port = cfg.get("port")
    path = str(cfg.get("path") or "")
    if url:
        pu = urlparse(url if "://" in url else "ftp://" + url)
        host = host or (pu.hostname or "")
        if port is None or port == "":
            port = pu.port
        path = path or unquote(pu.path or "")
        if pu.username and not user:
            user = unquote(pu.username)
            password = unquote(pu.password or "")
        if pu.scheme.lower() == "ftps":
            kind = "ftps"
    # host мог быть задан как "192.168.1.50:2121" — вытаскиваем порт, host оставляем чистым
    if host and ":" in host and not host.startswith("["):
        h, _, p = host.rpartition(":")
        if h and p.isdigit():
            host = h
            if port is None or port == "":
                port = int(p)
    if port is None or port == "":
        port = 21
    else:
        try:
            port = int(port)
        except (TypeError, ValueError):
            raise NetStorageError(f"FTP: некорректный port {port!r}")
    name = str(cfg.get("name") or "").strip() or host or "FTP"
    st = FtpStorage(
        name, host, port, path, user, password,
        passive=_bool(cfg.get("passive"), True),
        tls=(kind == "ftps"),
        verify_ssl=verify,
        encoding=str(cfg.get("encoding") or "utf-8"),
        timeout=timeout, cache_sec=cache_sec,
    )
    st.honor_range = _bool(cfg.get("honor_range"), True)
    return st


class NetStorageManager:
    """Набор хранилищ из config.yaml → network_storages (можно перечитать на лету)."""

    def __init__(self, items: Optional[list] = None):
        self.storages: Dict[str, BaseStorage] = {}
        self.errors: List[str] = []
        self.reload(items or [])

    def reload(self, items) -> List[str]:
        if isinstance(items, dict):
            items = [items]
        new: Dict[str, BaseStorage] = {}
        errors: List[str] = []
        for i, cfg in enumerate(items or [], 1):
            if isinstance(cfg, dict) and not _bool(cfg.get("enabled"), True):
                continue
            try:
                st = build_storage(cfg)
            except NetStorageError as e:
                errors.append(f"#{i}: {e}")
                continue
            except Exception as e:  # кривой конфиг не должен ронять сервер
                errors.append(f"#{i}: {e}")
                continue
            if st.sid in new:
                errors.append(f"#{i}: имя {st.name!r} повторяется — переименуйте")
                continue
            new[st.sid] = st
        self.storages = new
        self.errors = errors
        for msg in errors:
            log.warning("network_storages %s", msg)
        for st in new.values():
            log.info("Сетевое хранилище «%s»: %s", st.name, st.describe())
        return errors

    def get(self, sid: str) -> Optional[BaseStorage]:
        return self.storages.get(sid)

    def all(self) -> List[BaseStorage]:
        return list(self.storages.values())
