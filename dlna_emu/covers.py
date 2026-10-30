"""Обложки для DLNA: локальные файлы (встроенные + folder.jpg) и стриминги.

Ресиверу отдаётся ссылка вида  http://<ip>:<port>/cover/<сервис>/<id>
(тег upnp:albumArtURI). Картинку мы сами достаём и отдаём по HTTP, чтобы
ресиверу не нужен был HTTPS и доступ к CDN.

Без внешних зависимостей: теги читаются встроенными мини-парсерами
(ID3v2 для MP3, PICTURE-блок для FLAC, covr для M4A/MP4).
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import struct
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import unquote, urljoin

import requests

try:                      # необязательно: без Pillow обложки отдаются как есть
    from PIL import Image  # type: ignore
except Exception:         # pragma: no cover
    Image = None          # type: ignore

log = logging.getLogger("dlna_emu.covers")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

FOLDER_NAMES = ("cover", "folder", "front", "albumart", "album", "artwork", "thumb")
IMAGE_EXT = (".jpg", ".jpeg", ".png")

Cover = Tuple[bytes, str]  # (данные, mime)


# ---------------------------------------------------------------- helpers

def sniff_mime(data: bytes) -> Optional[str]:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _synchsafe(b: bytes) -> int:
    return (b[0] << 21) | (b[1] << 14) | (b[2] << 7) | b[3]


def _skip_id3(f) -> int:
    """Если файл начинается с ID3-тега — вернуть размер тега (включая заголовок)."""
    f.seek(0)
    head = f.read(10)
    if len(head) == 10 and head[:3] == b"ID3":
        return 10 + _synchsafe(head[6:10])
    return 0


def _carve_image(buf: bytes, start: int) -> Optional[bytes]:
    """Вырезать JPEG/PNG, начинающийся в buf[start:] (запасной разбор битых тегов)."""
    if buf[start:start + 3] == b"\xff\xd8\xff":
        end = buf.rfind(b"\xff\xd9", start)
        return buf[start:end + 2] if end > start else None
    if buf[start:start + 8] == b"\x89PNG\r\n\x1a\n":
        pos = start + 8
        while pos + 12 <= len(buf):
            ln = struct.unpack(">I", buf[pos:pos + 4])[0]
            name = buf[pos + 4:pos + 8]
            pos += 12 + ln
            if name == b"IEND":
                return buf[start:pos]
            if ln > len(buf):
                break
    return None


def _id3_scan_fallback(tag: bytes) -> Optional[bytes]:
    """Если кадры не разобрались (нестандартные размеры у некоторых тегировщиков) —
    ищем картинку по сигнатуре сразу после метки APIC/PIC."""
    for marker in (b"APIC", b"PIC"):
        pos = 0
        while True:
            i = tag.find(marker, pos)
            if i < 0:
                break
            pos = i + len(marker)
            window = tag[i:i + 600]
            for sig in (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n"):
                j = window.find(sig)
                if j >= 0:
                    img = _carve_image(tag, i + j)
                    if img and sniff_mime(img):
                        return img
    return None


# ------------------------------------------------------------ ID3v2 (MP3)

def _id3_picture(data: bytes, v2: bool) -> Optional[Tuple[int, bytes]]:
    """Разобрать содержимое кадра APIC/PIC → (тип картинки, байты)."""
    if len(data) < 6:
        return None
    enc = data[0]
    pos = 1
    if v2:                      # PIC: 3 байта формата
        pos += 3
    else:                       # APIC: mime до нуля
        end = data.find(b"\x00", pos)
        if end < 0:
            return None
        pos = end + 1
    if pos >= len(data):
        return None
    ptype = data[pos]
    pos += 1
    # описание: для UTF-16 заканчивается двойным нулём, для остальных — одним
    if enc in (1, 2):
        while pos + 1 < len(data):
            if data[pos] == 0 and data[pos + 1] == 0:
                pos += 2
                break
            pos += 2
    else:
        end = data.find(b"\x00", pos)
        if end < 0:
            return None
        pos = end + 1
    img = data[pos:]
    if sniff_mime(img) is None:
        # иногда перед картинкой остаётся мусор — ищем сигнатуру рядом
        for sig in (b"\xff\xd8\xff", b"\x89PNG"):
            i = img.find(sig, 0, 64)
            if i >= 0:
                img = img[i:]
                break
    return ptype, img


def cover_from_mp3(path: str, max_tag: int = 16 * 1024 * 1024) -> Optional[bytes]:
    with open(path, "rb") as f:
        head = f.read(10)
        if len(head) < 10 or head[:3] != b"ID3":
            return None
        ver = head[3]
        flags = head[5]
        size = _synchsafe(head[6:10])
        if size <= 0 or size > max_tag:
            return None
        tag = f.read(size)
    if ver < 4 and flags & 0x80:          # unsynchronisation на весь тег
        tag = tag.replace(b"\xff\x00", b"\xff")
    pos = 0
    if flags & 0x40 and len(tag) >= 4:    # расширенный заголовок
        if ver == 4:
            pos = _synchsafe(tag[:4])
        else:
            pos = struct.unpack(">I", tag[:4])[0] + 4

    best: Optional[Tuple[int, bytes]] = None
    v2 = ver == 2
    hlen = 6 if v2 else 10
    while pos + hlen <= len(tag):
        fid = tag[pos:pos + (3 if v2 else 4)]
        if fid[0:1] == b"\x00":
            break
        if v2:
            fsize = (tag[pos + 3] << 16) | (tag[pos + 4] << 8) | tag[pos + 5]
        elif ver == 4:
            fsize = _synchsafe(tag[pos + 4:pos + 8])
        else:
            fsize = struct.unpack(">I", tag[pos + 4:pos + 8])[0]
        body = tag[pos + hlen:pos + hlen + fsize]
        pos += hlen + fsize
        if fsize <= 0:
            continue
        if fid in (b"APIC", b"PIC"):
            if ver == 4 and not v2:
                fflags = tag[pos - fsize - hlen + 8:pos - fsize - hlen + 10]
                if len(fflags) == 2 and fflags[1] & 0x02:   # unsync на кадр
                    body = body.replace(b"\xff\x00", b"\xff")
                if len(fflags) == 2 and fflags[1] & 0x01 and len(body) > 4:  # data length indicator
                    body = body[4:]
            pic = _id3_picture(body, v2)
            if pic and sniff_mime(pic[1]):
                if pic[0] == 3:       # front cover — лучше всего
                    return pic[1]
                if best is None:
                    best = pic
    if best:
        return best[1]
    return _id3_scan_fallback(tag)


# ------------------------------------------------------------------ FLAC

def cover_from_flac(path: str) -> Optional[bytes]:
    with open(path, "rb") as f:
        skip = _skip_id3(f)
        f.seek(skip)
        if f.read(4) != b"fLaC":
            return None
        best: Optional[Tuple[int, bytes]] = None
        while True:
            h = f.read(4)
            if len(h) < 4:
                break
            last = bool(h[0] & 0x80)
            btype = h[0] & 0x7F
            blen = (h[1] << 16) | (h[2] << 8) | h[3]
            if btype == 6 and blen < 16 * 1024 * 1024:
                blk = f.read(blen)
                try:
                    ptype, mlen = struct.unpack(">II", blk[:8])
                    p = 8 + mlen
                    dlen = struct.unpack(">I", blk[p:p + 4])[0]
                    p += 4 + dlen + 16          # описание + width/height/depth/colors
                    ilen = struct.unpack(">I", blk[p:p + 4])[0]
                    img = blk[p + 4:p + 4 + ilen]
                except struct.error:
                    img = b""
                    ptype = -1
                if sniff_mime(img):
                    if ptype == 3:
                        return img
                    if best is None:
                        best = (ptype, img)
            else:
                f.seek(blen, 1)
            if last:
                break
    return best[1] if best else None


# ------------------------------------------------------------- MP4 / M4A

_MP4_CONTAINERS = {b"moov", b"udta", b"ilst", b"covr"}


def _mp4_find_covr(buf: bytes, start: int, end: int) -> Optional[bytes]:
    pos = start
    while pos + 8 <= end:
        size, name = struct.unpack(">I4s", buf[pos:pos + 8])
        hdr = 8
        if size == 1 and pos + 16 <= end:
            size = struct.unpack(">Q", buf[pos + 8:pos + 16])[0]
            hdr = 16
        if size == 0:
            size = end - pos
        if size < hdr or pos + size > end:
            break
        body_s, body_e = pos + hdr, pos + size
        if name == b"meta":
            r = _mp4_find_covr(buf, body_s + 4, body_e)   # 4 байта version/flags
            if r:
                return r
        elif name in _MP4_CONTAINERS:
            if name == b"covr":
                # внутри covr — один или несколько атомов data
                p = body_s
                while p + 16 <= body_e:
                    dsize, dname = struct.unpack(">I4s", buf[p:p + 8])
                    if dname == b"data" and dsize >= 16:
                        img = buf[p + 16:p + dsize]
                        if sniff_mime(img):
                            return img
                    if dsize < 8:
                        break
                    p += dsize
            else:
                r = _mp4_find_covr(buf, body_s, body_e)
                if r:
                    return r
        pos += size
    return None


def cover_from_mp4(path: str, max_moov: int = 32 * 1024 * 1024) -> Optional[bytes]:
    with open(path, "rb") as f:
        f.seek(0, 2)
        total = f.tell()
        pos = 0
        while pos + 8 <= total:
            f.seek(pos)
            h = f.read(16)
            if len(h) < 8:
                break
            size, name = struct.unpack(">I4s", h[:8])
            hdr = 8
            if size == 1 and len(h) == 16:
                size = struct.unpack(">Q", h[8:16])[0]
                hdr = 16
            if size == 0:
                size = total - pos
            if size < hdr:
                break
            if name == b"moov":
                if size > max_moov:
                    return None
                f.seek(pos)
                buf = f.read(size)
                return _mp4_find_covr(buf, 0, len(buf))
            pos += size
    return None


# --------------------------------------------------------- локальные файлы

def embedded_cover(path: str) -> Optional[bytes]:
    ext = Path(path).suffix.lower()
    try:
        if ext == ".mp3":
            return cover_from_mp3(path)
        if ext == ".flac":
            return cover_from_flac(path)
        if ext in (".m4a", ".mp4", ".aac", ".m4b"):
            return cover_from_mp4(path)
        # прочее (wav/aiff с ID3, mp3 с неверным расширением)
        with open(path, "rb") as f:
            head = f.read(4)
        if head[:3] == b"ID3":
            return cover_from_mp3(path)
        if head == b"fLaC":
            return cover_from_flac(path)
    except Exception as e:
        log.debug("embedded cover %s: %s", path, e)
    return None


def _pick_image(folder: Path, stem: str = "") -> Optional[Path]:
    found: Dict[str, Path] = {}
    for e in os.scandir(folder):
        if not e.is_file():
            continue
        low = e.name.lower()
        if low.endswith(IMAGE_EXT):
            found[low] = Path(e.path)
    if not found:
        return None
    stem = stem.lower()
    if stem:                                   # «Трек.mp3» ↔ «Трек.jpg»
        for ext in IMAGE_EXT:
            p = found.get(stem + ext)
            if p:
                return p
    for base in FOLDER_NAMES:
        for ext in IMAGE_EXT:
            p = found.get(base + ext)
            if p:
                return p
    for low, p in found.items():               # AlbumArt_{GUID}_Large.jpg, Cover_01.jpg …
        if low.startswith(("albumart", "cover", "folder", "front")):
            return p
    if len(found) == 1:                        # в папке одна картинка — это и есть обложка
        return next(iter(found.values()))
    return None


def folder_cover(path: str) -> Optional[Path]:
    """cover.jpg / folder.jpg / front.png и т.п. рядом с файлом
    (а для CD1/CD2/Disc 1 — ещё и уровнем выше)."""
    try:
        p = Path(path)
        hit = _pick_image(p.parent, p.stem)
        if hit:
            return hit
        if re.match(r"(?i)^(cd|disc|disk|диск)\s*[-_ ]?\d+", p.parent.name):
            return _pick_image(p.parent.parent)
    except Exception as e:
        log.debug("folder cover %s: %s", path, e)
    return None


# ------------------------------------------------------------------ сервис

class CoverService:
    """Отдаёт (bytes, mime) для /cover/<kind>/<id>."""

    def __init__(self, yandex=None, deezer=None, drivemusic=None, akniga=None, zvuk=None, kion=None, files=None,
                 zaycev=None, radio=None, radio_meta=None, radio_active=None,
                 radio_nowplaying: bool = True,
                 max_bytes: int = 2 * 1024 * 1024, cache_items: int = 48,
                 yandex_size: str = "300x300", max_px: int = 600):
        self.yandex = yandex
        self.deezer = deezer
        self.drivemusic = drivemusic
        self.akniga = akniga
        self.zvuk = zvuk
        self.kion = kion
        self.zaycev = zaycev
        self.radio = radio                    # RadioLibrary
        self.radio_meta = radio_meta          # RadioNowPlaying | None
        self.radio_active = radio_active      # callable(pid, idx) -> bool: станцию сейчас слушают
        self.radio_nowplaying = bool(radio_nowplaying)
        self._radio_ph: Dict[str, Cover] = {}
        self.kion_size = "300x300"
        self.files = files
        self.max_bytes = int(max_bytes)
        # исходник тяжелее max_bytes допустим, если его можно ужать через Pillow
        self.max_src_bytes = max(self.max_bytes, 12 * 1024 * 1024) if Image else self.max_bytes
        self.max_px = int(max_px or 0)
        self.yandex_size = yandex_size
        # чтение тегов с USB — не больше 3 одновременно, чтобы не душить музыку на слабом роутере
        self._io_sem = threading.BoundedSemaphore(3)
        self._cache_items = int(cache_items)
        self._bytes: "OrderedDict[str, Cover]" = OrderedDict()
        self._miss: Dict[str, float] = {}
        self._url_cache: Dict[str, str] = {}
        self._lock = threading.Lock()
        self._dz_lock = threading.Lock()
        self._dz_last = 0.0
        self._http = requests.Session()
        self._http.headers["User-Agent"] = UA
        # короткий id → абсолютный путь (обложки USB без %2F в URL)
        self._file_ids: Dict[str, str] = {}

    # --- кэш
    def _cache_get(self, key: str) -> Optional[Cover]:
        with self._lock:
            v = self._bytes.get(key)
            if v is not None:
                self._bytes.move_to_end(key)
            return v

    def _cache_put(self, key: str, val: Cover) -> None:
        if len(val[0]) > 400 * 1024:     # крупное не держим — роутеру важна память
            return
        with self._lock:
            self._bytes[key] = val
            self._bytes.move_to_end(key)
            while len(self._bytes) > self._cache_items:
                self._bytes.popitem(last=False)

    def _is_miss(self, key: str) -> bool:
        with self._lock:
            ts = self._miss.get(key)
            if ts and time.time() - ts < 600:
                return True
            return False

    def _set_miss(self, key: str) -> None:
        with self._lock:
            if len(self._miss) > 5000:
                self._miss.clear()
            self._miss[key] = time.time()


    def register_file_path(self, abs_path: str) -> str:
        """Зарегистрировать путь к файлу, вернуть короткий id для /cover/file/<id>.jpg."""
        p = str(abs_path or "").strip()
        if not p:
            return ""
        key = hashlib.md5(p.encode("utf-8", errors="replace")).hexdigest()[:16]
        self._file_ids[key] = p
        if len(self._file_ids) > 8000:
            # простой сброс половины
            for i, k in enumerate(list(self._file_ids.keys())):
                if i % 2 == 0:
                    self._file_ids.pop(k, None)
        return key

    def resolve_file_id(self, ident: str) -> Optional[str]:
        ident = (ident or "").strip()
        if ident in self._file_ids:
            return self._file_ids[ident]
        # без расширения .jpg
        if ident.endswith(".jpg") and ident[:-4] in self._file_ids:
            return self._file_ids[ident[:-4]]
        return None

    # --- публичный вход
    def get(self, kind: str, ident: str, radio_wait: float = 0.0) -> Optional[Cover]:
        if kind == "radio":
            # динамическая обложка (меняется с треком) — мимо общего кэша и кэша промахов
            try:
                res = self._radio(ident, radio_wait)
            except Exception as e:
                log.debug("cover radio %s: %s", ident, e)
                res = None
            return self._normalize(res) if res else None
        key = f"{kind}:{ident}"
        hit = self._cache_get(key)
        if hit:
            return hit
        if self._is_miss(key):
            return None
        try:
            if kind in ("file", "local"):
                mapped = self.resolve_file_id(ident)
                if mapped:
                    res = self._local(mapped)
                else:
                    res = self._local(ident)
            elif kind == "yandex":
                res = self._yandex(ident)
            elif kind == "kion":
                res = self._kion(ident)
            elif kind == "deezer":
                res = self._deezer(ident)
            elif kind == "drivemusic":
                res = self._drivemusic(ident)
            elif kind == "akniga":
                res = self._akniga(ident)
            elif kind == "zvuk":
                res = self._zvuk(ident)
            elif kind == "zaycev":
                res = self._zaycev(ident)
            else:
                res = None
        except Exception as e:
            log.debug("cover %s: %s", key, e)
            res = None
        if res:
            res = self._normalize(res)
            self._cache_put(key, res)
        else:
            self._set_miss(key)
        return res

    # --- приведение к «ресиверному» виду: baseline-JPEG, не больше max_px
    def _normalize(self, cov: Cover) -> Cover:
        data, mime = cov
        if Image is None or self.max_px <= 0:
            return cov
        try:
            im = Image.open(io.BytesIO(data))
            w, h = im.size
            is_prog = bool(im.info.get("progressive") or im.info.get("progression"))
            if mime == "image/jpeg" and max(w, h) <= self.max_px and not is_prog \
                    and len(data) <= 400 * 1024:
                return cov                      # уже подходит — не трогаем
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                bg = Image.new("RGB", im.size, (255, 255, 255))
                bg.paste(im, mask=im.split()[-1])
                im = bg
            elif im.mode != "RGB":
                im = im.convert("RGB")
            if max(w, h) > self.max_px:
                im.thumbnail((self.max_px, self.max_px))
            out = io.BytesIO()
            im.save(out, "JPEG", quality=85, progressive=False)
            return out.getvalue(), "image/jpeg"
        except Exception as e:
            log.debug("cover normalize: %s", e)
            return cov

    # --- локальные файлы
    def _resolve_local(self, encoded: str) -> Optional[Path]:
        """Путь к аудиофайлу для обложки. Учитываем %2F → / после HTTP-decode."""
        raw = unquote(encoded or "")
        # иногда .jpg/.jpeg прилипает к пути из albumArtURI
        for suf in (".jpg", ".jpeg", ".png", ".JPG", ".JPEG", ".PNG"):
            if raw.endswith(suf) and not Path(raw).is_file():
                cand = raw[: -len(suf)]
                # не срезать, если это часть имени без реального файла-обложки
                if Path(cand).suffix.lower() in {
                    ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".wma",
                }:
                    raw = cand
                    break
        candidates = [raw, encoded]
        if raw and not raw.startswith("/"):
            candidates.append("/" + raw)
        # дубли после unquote
        seen = set()
        uniq = []
        for c in candidates:
            if c and c not in seen:
                seen.add(c)
                uniq.append(c)
        if self.files:
            for c in uniq:
                p = self.files.resolve_file(c)
                if p:
                    return p
        for c in uniq:
            p = Path(c)
            try:
                if p.is_file() and (
                    str(p).startswith("/tmp/mnt/")
                    or str(p).startswith("/opt/")
                    or str(p).startswith("/home/")
                    or (len(str(p)) > 3 and str(p)[1] == ":")  # Windows
                ):
                    return p
            except Exception:
                continue
        return None

    def _local(self, encoded: str) -> Optional[Cover]:
        path = self._resolve_local(encoded)
        if not path:
            return None
        with self._io_sem:
            data = embedded_cover(str(path))
            if data and len(data) <= self.max_src_bytes:
                mime = sniff_mime(data)
                if mime:
                    return data, mime
            fp = folder_cover(str(path))
            if fp:
                try:
                    if fp.stat().st_size <= self.max_src_bytes:
                        data = fp.read_bytes()
                        mime = sniff_mime(data)
                        if mime:
                            return data, mime
                except OSError:
                    pass
        return None

    # --- удалённые
    def _download(self, url: str, referer: Optional[str] = None) -> Optional[Cover]:
        if not url:
            return None
        hit = self._cache_get("url:" + url)
        if hit:
            return hit
        headers = {"Referer": referer} if referer else None
        r = self._http.get(url, timeout=12, stream=True, headers=headers)
        try:
            if r.status_code >= 400:
                return None
            buf = bytearray()
            for chunk in r.iter_content(65536):
                buf.extend(chunk)
                if len(buf) > self.max_bytes:
                    return None
        finally:
            r.close()
        data = bytes(buf)
        mime = sniff_mime(data)
        if not mime:
            return None
        res = (data, mime)
        self._cache_put("url:" + url, res)
        return res

    # --- радио: обложка текущего трека → логотип станции → заглушка
    def _radio(self, ident: str, wait: float = 0.0) -> Optional[Cover]:
        pid, _, idx_s = (ident or "").rpartition("/")
        try:
            idx = int(idx_s)
        except ValueError:
            return None
        st = self.radio.get_station(unquote(pid), idx) if self.radio else None
        if not st:
            return None
        def _is_active() -> bool:
            return bool(self.radio_active and self.radio_active(unquote(pid), idx))

        active = _is_active()
        if not active and wait > 0:
            # запрос обложки мог прийти на миллисекунды раньше запроса потока
            end = time.time() + min(wait, 1.0)
            while time.time() < end and not active:
                time.sleep(0.1)
                active = _is_active()
        # при проксировании название трека придёт из потока — ему нужно время на первом запуске
        title_wait = wait if getattr(self.radio, "proxy", False) else 0.0
        title = None
        why = ""
        # 1) обложка трека, который играет СЕЙЧАС (только для станции, которую слушают:
        #    иначе список из 50 станций открывал бы 50 соединений к радиосерверам)
        if self.radio_nowplaying and self.radio_meta is not None and active:
            try:
                title = self.radio_meta.stream_title(st.url, wait=title_wait)
                if not title:
                    why = "станция не отдаёт название трека (ICY)"
                else:
                    from .radio_meta import split_title
                    artist, name = split_title(title)
                    if name and name.strip().lower() != st.name.strip().lower():
                        url = self.radio_meta.cover_url(artist, name)
                        if url:
                            cov = self._download(url)
                            if cov:
                                log.info("radio cover «%s»: трек «%s» → обложка трека", st.name, title)
                                return cov
                            why = "обложка найдена, но не скачалась"
                        else:
                            why = "обложка трека не найдена в Deezer/iTunes"
                    else:
                        why = "в названии только имя станции"
            except Exception as e:
                why = f"ошибка: {e}"
                log.debug("radio now-playing cover: %s", e)
        elif not active:
            why = "станция сейчас не играет (запрос обложки до старта потока)"
        # 2) логотип станции из m3u
        logo = getattr(st, "logo", "")
        if logo.startswith(("http://", "https://")):
            try:
                cov = self._download(logo)
                if cov:
                    log.info("radio cover «%s»: логотип (%s)", st.name, why or "—")
                    return cov
            except Exception as e:
                log.debug("radio logo %s: %s", logo, e)
        # 3) заглушка с инициалами
        ph = self._radio_placeholder(st.name)
        log.info("radio cover «%s»: %s (%s)", st.name,
                 "заглушка" if ph else "НЕТ ОБЛОЖКИ — не установлен Pillow", why or "—")
        return ph

    def _radio_placeholder(self, name: str) -> Optional[Cover]:
        if Image is None:
            return None
        hit = self._radio_ph.get(name)
        if hit:
            return hit
        try:
            from PIL import ImageDraw, ImageFont  # type: ignore
            h = int(hashlib.md5(name.encode("utf-8", "replace")).hexdigest()[:6], 16)
            bg = (40 + (h >> 16) % 120, 40 + (h >> 8) % 120, 40 + h % 120)
            size = 300
            im = Image.new("RGB", (size, size), bg)
            d = ImageDraw.Draw(im)
            words = [w for w in re.split(r"\W+", name, flags=re.UNICODE) if w and not w.isdigit()]
            text = "".join(w[0] for w in words[:3]).upper() or "FM"
            try:
                font = ImageFont.load_default(size=110)
            except TypeError:
                font = ImageFont.load_default()
            try:
                box = d.textbbox((0, 0), text, font=font)
                tw, th = box[2] - box[0], box[3] - box[1]
                d.text(((size - tw) / 2 - box[0], (size - th) / 2 - box[1]), text,
                       fill=(255, 255, 255), font=font)
            except Exception:
                d.text((size // 3, size // 3), text, fill=(255, 255, 255), font=font)
            out = io.BytesIO()
            im.save(out, "JPEG", quality=85)
            res = (out.getvalue(), "image/jpeg")
            if len(self._radio_ph) > 300:
                self._radio_ph.clear()
            self._radio_ph[name] = res
            return res
        except Exception as e:
            log.debug("radio placeholder: %s", e)
            return None

    def _yandex(self, tid: str) -> Optional[Cover]:
        if not self.yandex or not self.yandex.enabled:
            return None
        url = self.yandex.get_cover_url(tid, self.yandex_size)
        return self._download(url or "")

    def _kion(self, tid: str) -> Optional[Cover]:
        if not self.kion or not self.kion.enabled:
            return None
        url = self.kion.get_cover_url(tid, self.kion_size)
        return self._download(url or "")

    def _deezer(self, tid: str) -> Optional[Cover]:
        ck = "dz:" + tid
        url = self._url_cache.get(ck)
        if not url:
            # публичный API: лимит ~50 запросов / 5 сек — слегка притормаживаем
            with self._dz_lock:
                wait = 0.12 - (time.time() - self._dz_last)
                if wait > 0:
                    time.sleep(wait)
                self._dz_last = time.time()
            r = self._http.get(f"https://api.deezer.com/track/{tid}", timeout=10)
            if r.status_code >= 400:
                return None
            album = (r.json() or {}).get("album") or {}
            url = album.get("cover_medium") or album.get("cover_big") or album.get("cover") or ""
            if url:
                if len(self._url_cache) > 5000:
                    self._url_cache.clear()
                self._url_cache[ck] = url
        return self._download(url)

    _OG_RE = (
        re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', re.I),
        re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', re.I),
        re.compile(r'<link[^>]+rel=["\']image_src["\'][^>]+href=["\']([^"\']+)', re.I),
    )

    def _drivemusic(self, tid: str) -> Optional[Cover]:
        dm = self.drivemusic
        if not dm or not dm.enabled:
            return None
        ck = "dm:" + tid
        url = self._url_cache.get(ck)
        if not url:
            page = dm._page_cache.get(tid)
            if not page:
                return None
            body = dm._get(page, referer=dm.base)
            if not body:
                return None
            for rx in self._OG_RE:
                m = rx.search(body)
                if m:
                    url = urljoin(page, m.group(1).replace("&amp;", "&"))
                    break
            if not url:
                return None
            if len(self._url_cache) > 5000:
                self._url_cache.clear()
            self._url_cache[ck] = url
        return self._download(url, referer=dm.base)

    def _akniga(self, bid: str) -> Optional[Cover]:
        ak = self.akniga
        if not ak or not ak.enabled:
            return None
        url = ak.cover_url(bid)
        if not url:
            return None
        return self._download(url, referer="https://akniga.org/")

    def _zaycev(self, tid: str) -> Optional[Cover]:
        zy = self.zaycev
        if not zy or not getattr(zy, "enabled", False):
            return None
        url = zy.get_cover_url(tid)
        if not url:
            return None
        return self._download(url, referer="https://zaycev.net/")

    def _zvuk(self, tid: str) -> Optional[Cover]:
        zv = self.zvuk
        if not zv or not getattr(zv, "enabled", False):
            return None
        url = zv.get_cover_url(tid, size="400x400")
        if not url:
            return None
        return self._download(url, referer="https://zvuk.com/")

