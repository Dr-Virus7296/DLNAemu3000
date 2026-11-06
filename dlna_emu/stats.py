"""Статистика прослушивания, история эфира радио и автоплейлист «по статистике»
(из самых часто прослушиваемых треков).

Что считаем
-----------
* Каждый запуск трека/потока от ресивера — это «сессия» (устройство, площадка, трек).
  Когда сессия закончилась, в ``stats.jsonl`` дописывается одна строка-событие
  (старт, секунды, площадка, исполнитель, трек, станция для радио).
* Секунды — оценка:
  - радио: пока ресивер держит соединение с потоком (прокси эмулятора) — это точно;
  - треки: если ресивер качал файл «в темпе воспроизведения» (USB-файлы целиком,
    chunked-стримы) — длина соединения; если файл ушёл пачкой в буфер (buffered) —
    время до запуска следующего трека, но не больше длительности трека
    (известна из DIDL / заголовка файла; иначе потолок ``DEFAULT_CAP``).
* Прослушивания короче ``min_sec`` секунд (по умолчанию 30, настройка статистики) (пропуск, проба) не учитываются.

История эфира радио хранится отдельно (``radio_history.json``): названия треков,
которые станция передавала в ICY-метаданных, пока вы её слушали.
"""

from __future__ import annotations

import glob
import gzip
import json
import logging
import os
import re
import shutil
import struct
import threading
import time
from collections import OrderedDict
from typing import Callable, Dict, List, Optional, Set, Tuple

from .radio_meta import split_title
from .shuffle import (
    _artist_from_folder,
    artist_from_name,
    read_tags,
    smart_shuffle,
    split_artists,
)

log = logging.getLogger("dlna_emu.stats")

MIN_SEC = 30            # по умолчанию: короче — не считаем (пропуск/проба); меняется в настройках (min_sec)
DEFAULT_CAP = 8 * 60    # потолок «одного трека» без известной длительности
SESSION_SWEEP = 30      # как часто закрывать «забытые» сессии, сек

DEFAULTS: Dict[str, object] = {
    "enabled": True,          # собирать статистику прослушивания
    "exclude_dirs": [],       # каталоги USB, которые не учитывать
    "min_sec": 30,            # засчитывать прослушивание, если играло не меньше N секунд (1..3600)
    "max_mb": 20,             # если статистика занимает больше N МБ — удалять 3 самых старых месяца (0 — не чистить)
    "top_artists": 20,        # 1..100
    "top_tracks": 50,         # 1..500
    "auto_playlist": False,
    "auto_title": "★ По статистике",
    "auto_max": 200,          # 10..1000
    "auto_hours": 24,         # 1..720
    "radio_stations": 5,      # 1..10
    "radio_tracks": 15,       # 1..30
}
LIMITS = {
    "top_artists": (1, 100), "top_tracks": (1, 500), "auto_max": (10, 1000),
    "auto_hours": (1, 720), "radio_stations": (1, 10), "radio_tracks": (1, 30),
    "min_sec": (1, 3600), "max_mb": (0, 100000),
}

SERVICE_LABELS = {
    "usb": "USB / файлы", "file": "USB / файлы", "local": "Локальные файлы",
    "net": "Сетевое хранилище", "yandex": "Яндекс.Музыка", "kion": "КИОН Музыка",
    "zvuk": "Звук", "deezer": "Deezer", "radio": "Радио", "drivemusic": "DriveMusic",
    "promodj": "PromoDJ", "zaycev": "Zaycev.net", "akniga": "akniga.org",
}
_USB_LIKE = ("usb", "file", "local")
_GENERIC_DIRS = {
    "music", "музыка", "mp3", "flac", "audio", "rock", "рок", "pop", "поп", "rap", "рэп", "рэп",
    "edm", "classic", "классика", "retro", "ретро", "games", "игры", "разное", "various", "other",
    "playlists", "downloads", "новая папка", "mnt", "tmp", "jazz", "джаз", "metal", "dance",
    "hip-hop", "hiphop", "electronic", "soundtrack", "ost", "collection", "сборники", "сборник",
}
_YEAR_RE = re.compile(r"^\s*[\[(]?(19|20)\d{2}\b")


def clean_settings(raw: Optional[dict]) -> dict:
    """Настройки с границами (значения из config.yaml/формы → допустимые)."""
    out = dict(DEFAULTS)
    raw = raw or {}
    for k, default in DEFAULTS.items():
        if k not in raw:
            continue
        v = raw[k]
        if isinstance(default, list):
            if isinstance(v, str):
                v = [x for x in v.replace("\r", "").split("\n")]
            if isinstance(v, (list, tuple)):
                seen, lst = set(), []
                for x in v:
                    x = str(x).strip().rstrip("/\\") or ""
                    if x and x not in seen:
                        seen.add(x)
                        lst.append(x)
                out[k] = lst[:500]
            continue
        if isinstance(default, bool):
            out[k] = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
        elif isinstance(default, int):
            try:
                n = int(v)
            except (TypeError, ValueError):
                continue
            lo, hi = LIMITS.get(k, (0, 10 ** 9))
            out[k] = max(lo, min(hi, n))
        else:
            s = str(v).strip()
            if s:
                out[k] = s[:80]
    return out


# ------------------------------------------------------------ длительность файлов

def local_duration(path: str) -> int:
    """Длительность локального файла в секундах (0 — неизвестно). FLAC/MP3/M4A."""
    try:
        ext = os.path.splitext(path)[1].lower()
        if ext == ".flac":
            return _dur_flac(path)
        if ext in (".m4a", ".mp4", ".m4b", ".aac"):
            return _dur_mp4(path)
        if ext == ".mp3":
            return _dur_mp3(path)
    except Exception as e:  # noqa: BLE001
        log.debug("duration %s: %s", path, e)
    return 0


def _dur_flac(path: str) -> int:
    with open(path, "rb") as f:
        head = f.read(10)
        skip = 0
        if head[:3] == b"ID3" and len(head) == 10:
            skip = 10 + ((head[6] << 21) | (head[7] << 14) | (head[8] << 7) | head[9])
        f.seek(skip)
        if f.read(4) != b"fLaC":
            return 0
        h = f.read(4)
        if len(h) < 4 or (h[0] & 0x7F) != 0:
            return 0
        si = f.read(34)
        if len(si) < 34:
            return 0
        v = int.from_bytes(si[10:18], "big")
        rate = (v >> 44) & 0xFFFFF
        total = v & 0xFFFFFFFFF
        return int(total / rate) if rate else 0


def _dur_mp4(path: str) -> int:
    with open(path, "rb") as f:
        f.seek(0, 2)
        total = f.tell()
        pos = 0
        while pos + 8 <= total:
            f.seek(pos)
            h = f.read(16)
            if len(h) < 8:
                return 0
            size, name = struct.unpack(">I4s", h[:8])
            hdr = 8
            if size == 1 and len(h) == 16:
                size = struct.unpack(">Q", h[8:16])[0]
                hdr = 16
            if size == 0:
                size = total - pos
            if size < hdr:
                return 0
            if name == b"moov":
                f.seek(pos + hdr)
                buf = f.read(min(size - hdr, 4 * 1024 * 1024))
                i = buf.find(b"mvhd")
                if i < 0:
                    return 0
                ver = buf[i + 4]
                if ver == 1:
                    ts, dur = struct.unpack(">IQ", buf[i + 24:i + 36])
                else:
                    ts, dur = struct.unpack(">II", buf[i + 16:i + 24])
                return int(dur / ts) if ts else 0
            pos += size
    return 0


_MP3_BR = {
    (1, 1): [0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448],
    (1, 2): [0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384],
    (1, 3): [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
    (2, 1): [0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256],
    (2, 2): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
    (2, 3): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
}
_MP3_SR = {1: [44100, 48000, 32000], 2: [22050, 24000, 16000], 25: [11025, 12000, 8000]}


def _dur_mp3(path: str) -> int:
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(10)
        pos = 0
        if head[:3] == b"ID3" and len(head) == 10:
            pos = 10 + ((head[6] << 21) | (head[7] << 14) | (head[8] << 7) | head[9])
        f.seek(pos)
        buf = f.read(8192)
    i = 0
    while i + 4 <= len(buf):
        if buf[i] == 0xFF and (buf[i + 1] & 0xE0) == 0xE0:
            b1, b2 = buf[i + 1], buf[i + 2]
            ver_bits = (b1 >> 3) & 3
            layer = 4 - ((b1 >> 1) & 3)
            br_idx, sr_idx = (b2 >> 4) & 15, (b2 >> 2) & 3
            if ver_bits != 1 and layer in (1, 2, 3) and 0 < br_idx < 15 and sr_idx < 3:
                mpeg = 1 if ver_bits == 3 else (2 if ver_bits == 2 else 25)
                table = _MP3_BR.get((1 if mpeg == 1 else 2, layer))
                if not table:
                    return 0
                kbps = table[br_idx]
                rate = _MP3_SR[mpeg][sr_idx]
                # Xing/Info: точное число кадров для VBR
                x = buf.find(b"Xing", i, i + 60)
                if x < 0:
                    x = buf.find(b"Info", i, i + 60)
                if x >= 0 and buf[x + 7] & 1:
                    frames = struct.unpack(">I", buf[x + 8:x + 12])[0]
                    spf = 1152 if layer != 1 and not (layer == 3 and mpeg != 1) else (384 if layer == 1 else 576)
                    return int(frames * spf / rate)
                return int((size - pos) * 8 / (kbps * 1000)) if kbps else 0
        i += 1
    return 0


# ------------------------------------------------------------ исполнитель

def display_artist(name: str, path: str = "") -> str:
    """Читаемое имя исполнителя: теги файла → «Artist - Title» в имени → папка исполнителя."""
    a = read_tags(path)[0] if path else ""
    if not a:
        a = artist_from_name(name)
    if not a and path:
        a = artist_from_name(os.path.splitext(os.path.basename(path))[0])
    if not a and path:
        g = _artist_from_folder(path)
        if g and g.casefold() not in _GENERIC_DIRS and not _YEAR_RE.match(g):
            a = g
    return a.strip()


_USB_META_CACHE: Dict[str, Tuple[float, str, str]] = {}


def usb_meta(path: str) -> Tuple[str, str]:
    """(исполнитель, название) локального файла; название — из тега, иначе имя без расширения.
    Пустая строка вместо исполнителя — неизвестен. Кэш по mtime."""
    try:
        mt = os.stat(path).st_mtime
    except OSError:
        return "", ""
    hit = _USB_META_CACHE.get(path)
    if hit and hit[0] == mt:
        return hit[1], hit[2]
    stem = os.path.splitext(os.path.basename(path))[0]
    artist = display_artist(stem, path)
    title = read_tags(path)[1] or stem
    if len(_USB_META_CACHE) > 20000:
        _USB_META_CACHE.clear()
    _USB_META_CACHE[path] = (mt, artist, title)
    return artist, title


def _norm(s: str) -> str:
    return re.sub(r"[\W_]+", "", (s or "").casefold())


_COLLAB_X_RE = re.compile(r"\s+[xXхХ×]\s+")
_PROTECTED_ARTISTS = {"ac/dc", "simon & garfunkel", "earth, wind & fire", "hall & oates",
                      "florence + the machine", "tyler, the creator", "crosby, stills & nash",
                      "crosby, stills, nash & young", "emerson, lake & palmer"}


def collab_parts(artist: str) -> List[str]:
    """«A & B», «A x B», «A feat. B», «A, B» → ['A', 'B'] (в порядке записи).
    Если это не связка (одно имя, «AC/DC», «Simon & Garfunkel») — список из одного имени."""
    raw = re.sub(r"\s+", " ", artist or "").strip()
    if not raw:
        return []
    if raw.casefold() in _PROTECTED_ARTISTS:
        return [raw]
    txt = _COLLAB_X_RE.sub(" , ", raw)
    # «/» делит только когда вокруг пробелы («A / B»): «AC/DC» остаётся целым
    txt = re.sub(r"\s+/\s+", " , ", txt)
    txt = re.sub(r"\s*(?:\bfeat\.?|\bft\.?|\bfeaturing\b|\bvs\.?|\bпри уч\.?|&|;|\bи\b)\s*", " , ", txt, flags=re.I)
    parts = [re.sub(r"\s+", " ", x).strip(" .\t") for x in txt.split(",")]
    parts = [x for x in parts if x]
    seen, out = set(), []
    for x in parts:
        k = _norm(x)
        if k and k not in seen:
            seen.add(k)
            out.append(x)
    return out or [raw]


def _primary(artist: str) -> str:
    """Для топа берём первого из «A feat. B»: у связки одного артиста один ключ."""
    s = sorted(split_artists(artist))
    # сохраняем порядок появления в исходной строке
    low = (artist or "").casefold()
    best = min(s, key=lambda x: low.find(x) if low.find(x) >= 0 else 10 ** 6) if s else ""
    return best


# ------------------------------------------------------------ учёт сессий

class _Sess:
    __slots__ = ("ip", "svc", "tid", "title", "artist", "path", "station", "url", "t0",
                 "dur", "open", "conn_end", "key", "sent", "size")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


class Stats:
    def __init__(self, directory: str):
        self.dir = directory or "."
        self.legacy_path = os.path.join(self.dir, "stats.jsonl")     # старый единый файл
        self.data_dir = os.path.join(self.dir, "stats_data")          # YYYY-MM.jsonl / .jsonl.gz
        self.path = self.data_dir
        self.settings_fn: Optional[Callable[[], dict]] = None
        self._set_cache: Tuple[float, dict] = (0.0, {})
        self._cur_month = ""
        self._cur_events: List[dict] = []
        self._old_cache: Optional[Tuple[float, List[dict]]] = None
        self._lock = threading.RLock()
        self._sessions: Dict[str, _Sess] = {}
        self._meta: "OrderedDict[Tuple[str, str], Tuple[str, str, int]]" = OrderedDict()
        self._loaded = False
        self._last_archive = 0.0
        self._last_prune = 0.0
        self._score_cache = None
        self._warned = False
        self._sweeper: Optional[threading.Thread] = None
        self._lib_index: Optional[Tuple[float, list]] = None
        self.auto_state_path = os.path.join(self.dir, "stats_auto.json")
        self.auto_status = ""
        self._auto_running = False

    # ---------------------------------------------------------- метаданные

    def meta(self, service: str, tid: str, artist: str = "", title: str = "", duration: int = 0) -> None:
        """Запомнить исполнителя/длительность трека (вызывается при показе списка)."""
        if not service or not tid:
            return
        key = (service, str(tid))
        with self._lock:
            self._meta[key] = (artist or "", title or "", int(duration or 0))
            self._meta.move_to_end(key)
            while len(self._meta) > 20000:
                self._meta.popitem(last=False)

    # -------------------------------------------------------------- события

    # ------------------------------------------------------ хранение (по месяцам)
    #
    # data_dir/YYYY-MM.jsonl      — текущий месяц (в памяти + дозапись строкой на событие)
    # data_dir/YYYY-MM.jsonl.gz   — прошлые месяцы, сжатые gzip (стандартный модуль Python).
    # Прошлые месяцы читаются «на лету» в память (распаковка), на диске остаются сжатыми.

    @staticmethod
    def _month_of(ts: float) -> str:
        t = time.localtime(ts)
        return f"{t.tm_year}-{t.tm_mon:02d}"

    def _mpath(self, month: str, gz: bool = False) -> str:
        return os.path.join(self.data_dir, f"{month}.jsonl" + (".gz" if gz else ""))

    @staticmethod
    def _read_jsonl(path: str) -> List[dict]:
        out: List[dict] = []
        opener = gzip.open if path.endswith(".gz") else open
        try:
            with opener(path, "rt", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(e, dict) and e.get("t") and e.get("s") is not None:
                        out.append(e)
        except FileNotFoundError:
            pass
        except (OSError, EOFError) as e:
            log.warning("stats: не удалось прочитать %s: %s", path, e)
        return out

    def _months_on_disk(self) -> List[str]:
        names = set()
        for f in glob.glob(os.path.join(self.data_dir, "*.jsonl")) + glob.glob(os.path.join(self.data_dir, "*.jsonl.gz")):
            m = re.match(r"(\d{4}-\d{2})\.jsonl(\.gz)?$", os.path.basename(f))
            if m:
                names.add(m.group(1))
        return sorted(names)

    def _load(self) -> None:
        """Старт: перенос старого stats.jsonl по месяцам, загрузка текущего месяца, архивация прошлых."""
        if self._loaded:
            return
        self._loaded = True
        try:
            os.makedirs(self.data_dir, exist_ok=True)
        except OSError as e:
            log.warning("stats: не удалось создать %s: %s", self.data_dir, e)
        # миграция единого файла
        if os.path.isfile(self.legacy_path):
            try:
                by_month: Dict[str, List[dict]] = {}
                for e in self._read_jsonl(self.legacy_path):
                    by_month.setdefault(self._month_of(int(e["t"])), []).append(e)
                for m, lst in by_month.items():
                    with open(self._mpath(m), "a", encoding="utf-8") as f:
                        for e in lst:
                            f.write(json.dumps(e, ensure_ascii=False) + "\n")
                os.replace(self.legacy_path, self.legacy_path + ".migrated")
                log.info("stats: старый stats.jsonl разложен по месяцам (%d мес.)", len(by_month))
            except OSError as e:
                log.warning("stats: миграция stats.jsonl не удалась: %s", e)
        self._cur_month = self._month_of(time.time())
        self._cur_events = self._read_jsonl(self._mpath(self._cur_month))
        self.archive_old()
        try:
            self.prune_if_needed()
        except Exception as e:  # noqa: BLE001
            log.debug("stats prune: %s", e)
        self.prune_if_needed()

    def archive_old(self) -> int:
        """Сжать gzip все прошлые месяцы (текущий остаётся обычным файлом). Возвращает число сжатых."""
        n = 0
        cur = self._month_of(time.time())
        for m in self._months_on_disk():
            plain = self._mpath(m)
            if m >= cur or not os.path.isfile(plain):
                continue
            gz = self._mpath(m, gz=True)
            try:
                if os.path.isfile(gz):
                    # дописываем новый gzip-«член» (multi-member gzip читается как один поток)
                    with open(plain, "rb") as src, gzip.open(gz, "ab", compresslevel=6) as dst:
                        shutil.copyfileobj(src, dst)
                else:
                    tmp = gz + ".tmp"
                    with open(plain, "rb") as src, gzip.open(tmp, "wb", compresslevel=6) as dst:
                        shutil.copyfileobj(src, dst)
                    os.replace(tmp, gz)
                os.remove(plain)
                n += 1
            except OSError as e:
                log.warning("stats: не удалось сжать %s: %s", plain, e)
        if n:
            self._old_cache = None
            log.info("stats: сжато прошлых месяцев: %d", n)
        self._last_archive = time.time()
        return n

    def _roll_if_needed(self) -> None:
        cur = self._month_of(time.time())
        if cur != self._cur_month:
            self._cur_month = cur
            self._cur_events = self._read_jsonl(self._mpath(cur))
            self.archive_old()

    def _append(self, ev: dict) -> None:
        self._load()
        self._roll_if_needed()
        self._cur_events.append(ev)
        try:
            with open(self._mpath(self._cur_month), "a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        except OSError as e:
            if not self._warned:
                self._warned = True
                log.warning("stats: нет записи в %s (%s) — статистика не переживёт перезапуск", self.data_dir, e)

    def clear(self) -> None:
        with self._lock:
            self._load()
            self._cur_events.clear()
            self._old_cache = None
            for f in glob.glob(os.path.join(self.data_dir, "*.jsonl")) + glob.glob(os.path.join(self.data_dir, "*.jsonl.gz")):
                try:
                    os.remove(f)
                except OSError:
                    pass
            self._lib_index = None

    def _old_events(self) -> List[dict]:
        """События прошлых месяцев (распаковка в память; держим 2 минуты, затем забываем)."""
        now = time.time()
        if self._old_cache and now - self._old_cache[0] < 120:
            return self._old_cache[1]
        cur = self._cur_month
        out: List[dict] = []
        for m in self._months_on_disk():
            if m >= cur:
                continue
            for path in (self._mpath(m, gz=True), self._mpath(m)):
                if os.path.isfile(path):
                    out.extend(self._read_jsonl(path))
        self._old_cache = (now, out)
        return out

    def drop_cache(self) -> None:
        """Освободить память: распакованные прошлые месяцы больше не нужны."""
        with self._lock:
            if self._old_cache and time.time() - self._old_cache[0] >= 120:
                self._old_cache = None

    def events(self) -> List[dict]:
        with self._lock:
            self._load()
            self._roll_if_needed()
            return self._old_events() + list(self._cur_events)

    def events_month(self, month: str) -> List[dict]:
        """События одного месяца 'YYYY-MM' (с распаковкой из архива; в кэше не остаются)."""
        with self._lock:
            self._load()
            self._roll_if_needed()
            y, m = int(month[:4]), int(month[5:7])
            nxt = f"{y + (m // 12)}-{(m % 12) + 1:02d}"
            out: List[dict] = []
            for mm in (month, nxt):          # события, начатые в конце месяца, могли записаться в следующий
                if mm == self._cur_month:
                    out.extend(self._cur_events)
                    continue
                for path in (self._mpath(mm, gz=True), self._mpath(mm)):
                    if os.path.isfile(path):
                        out.extend(self._read_jsonl(path))
            return [e for e in out if self._month_of(int(e["t"])) == month]

    def available_months(self) -> List[str]:
        with self._lock:
            self._load()
            ms = set(self._months_on_disk())
            ms.add(self._cur_month)
            return sorted(ms, reverse=True)

    def storage_info(self) -> dict:
        """Размер хранилища: {'plain': байт, 'gz': байт, 'months_gz': N}."""
        plain = gz = n = 0
        for f in glob.glob(os.path.join(self.data_dir, "*.jsonl")):
            plain += os.path.getsize(f)
        for f in glob.glob(os.path.join(self.data_dir, "*.jsonl.gz")):
            gz += os.path.getsize(f)
            n += 1
        return {"plain": plain, "gz": gz, "months_gz": n}

    # ------------------------------------------------- настройки и исключения

    def _settings(self) -> dict:
        now = time.time()
        if now - self._set_cache[0] < 5:
            return self._set_cache[1]
        try:
            st = clean_settings(self.settings_fn() if self.settings_fn else None)
        except Exception:  # noqa: BLE001
            st = clean_settings(None)
        self._set_cache = (now, st)
        return st

    @staticmethod
    def is_excluded(path: str, dirs: List[str]) -> bool:
        """Файл лежит внутри одной из исключённых папок (сравниваем и как есть, и по realpath)."""
        if not path or not dirs:
            return False
        cands = {os.path.normpath(path)}
        try:
            cands.add(os.path.realpath(path))
        except OSError:
            pass
        for d in dirs:
            variants = {os.path.normpath(d)}
            try:
                variants.add(os.path.realpath(d))
            except OSError:
                pass
            for dv in variants:
                pre = dv.rstrip(os.sep) + os.sep
                for p in cands:
                    if p == dv or p.startswith(pre):
                        return True
        return False

    # -------------------------------------------------------------- сессии

    def start(self, ip: str, service: str, title: str, track_id: str = "", path: str = "",
              station_url: str = "") -> None:
        if not ip or not service:
            return
        now = time.time()
        svc = service
        key = f"{svc}|{path or track_id}|{station_url}"
        cfg = self._settings()
        skip = (not cfg["enabled"]) or (svc in _USB_LIKE and self.is_excluded(path, cfg["exclude_dirs"]))
        with self._lock:
            cur = self._sessions.get(ip)
            if skip:
                # новый трек запущен — предыдущий закончился, но сам этот не учитываем
                if cur is not None:
                    self._close(cur, next_start=now)
                return
            if cur is not None:
                same = cur.key == key
                if same and (cur.conn_end is None or now - cur.conn_end < 45):
                    cur.open += 1                   # тот же трек, повторный запрос (Range/проба)
                    cur.conn_end = None
                    return
                self._close(cur, next_start=now)
            tid = str(track_id or "")
            artist, mtitle, dur = ("", "", 0)
            m = self._meta.get((svc, tid)) or self._meta.get((svc if svc != "usb" else "file", tid))
            if m:
                artist, mtitle, dur = m
            if svc in _USB_LIKE and path:
                dur = dur or local_duration(path)
                artist, mtitle = usb_meta(path)
            elif svc == "radio":
                artist = ""
            else:
                if not artist and title:
                    a, _t = split_title(title)
                    artist = a
            self._sessions[ip] = _Sess(
                ip=ip, svc=svc, tid=tid, title=mtitle or title or tid, artist=artist,
                path=path, station=title if svc == "radio" else "", url=station_url,
                t0=now, dur=int(dur or 0), open=1, conn_end=None, key=key, sent=0, size=0,
            )
        self._ensure_sweeper()

    def progress(self, ip: str, sent: int, size: int) -> None:
        """Сколько байт файла реально отдано ресиверу (для оценки паузы/остановки)."""
        with self._lock:
            s = self._sessions.get(ip)
            if s is not None and size > 0:
                s.sent = max(s.sent or 0, int(sent))
                s.size = int(size)

    def conn_end(self, ip: str) -> None:
        """Соединение с ресивером закрылось (конец отдачи потока)."""
        if not ip:
            return
        now = time.time()
        with self._lock:
            s = self._sessions.get(ip)
            if s is None:
                return
            s.open = max(0, s.open - 1)
            if s.open > 0:
                return
            s.conn_end = now
            conn = now - s.t0
            if s.svc == "radio":
                self._finalize(s, conn)
                self._sessions.pop(ip, None)
                return
            if s.sent and s.size and s.dur:
                # Файл отдавался последовательно: ресивер забирает данные по мере воспроизведения,
                # на паузе он перестаёт читать (отдача блокируется) — по байтам видно, докуда дослушал.
                frac = min(1.0, s.sent / float(s.size))
                est = s.dur * frac
                if frac < 0.97 or conn >= max(60.0, 0.5 * s.dur):
                    self._finalize(s, min(conn, est))
                    self._sessions.pop(ip, None)
                    return
            real = max(60.0, 0.5 * s.dur) if s.dur else 90.0
            if conn >= real:                      # качал «в темпе воспроизведения»
                self._finalize(s, min(conn, s.dur) if s.dur else conn)
                self._sessions.pop(ip, None)

    def _cap(self, s: _Sess) -> float:
        return float(s.dur) if s.dur else float(DEFAULT_CAP)

    def _close(self, s: _Sess, next_start: Optional[float] = None) -> None:
        now = next_start or time.time()
        elapsed = now - s.t0
        if s.svc == "radio":
            seconds = elapsed if s.conn_end is None else s.conn_end - s.t0
        else:
            seconds = min(elapsed, self._cap(s))
        self._finalize(s, seconds)
        self._sessions.pop(s.ip, None)

    def _finalize(self, s: _Sess, seconds: float) -> None:
        seconds = int(seconds)
        if seconds < int(self._settings().get("min_sec", MIN_SEC)):
            return
        ev = {"t": int(s.t0), "s": seconds, "svc": s.svc, "id": s.tid,
              "title": s.title, "artist": s.artist or ""}
        if s.path:
            ev["path"] = s.path
        if s.svc == "radio":
            ev["st"] = s.station
            if s.url:
                ev["url"] = s.url
        self._append(ev)

    def sweep(self) -> None:
        now = time.time()
        with self._lock:
            for ip, s in list(self._sessions.items()):
                if s.svc == "radio":
                    continue
                if s.conn_end is not None and now - s.t0 >= self._cap(s) + 15:
                    self._finalize(s, self._cap(s))
                    self._sessions.pop(ip, None)
                elif now - s.t0 > 6 * 3600:        # зависшие
                    self._sessions.pop(ip, None)

    def _ensure_sweeper(self) -> None:
        if self._sweeper is not None:
            return

        def loop():
            while True:
                time.sleep(SESSION_SWEEP)
                try:
                    self.sweep()
                    self.drop_cache()
                    if time.time() - self._last_archive > 86400:
                        self.archive_old()
                    if time.time() - self._last_prune > 3600:
                        self._last_prune = time.time()
                        self.prune_if_needed()
                except Exception:  # noqa: BLE001
                    pass

        self._sweeper = threading.Thread(target=loop, name="stats-sweeper", daemon=True)
        self._sweeper.start()

    def flush_all(self) -> None:
        """На остановку: закрыть открытые сессии."""
        with self._lock:
            for s in list(self._sessions.values()):
                self._close(s)

    # ------------------------------------------- совместные исполнители

    @staticmethod
    def _with_usb_meta(e: dict) -> dict:
        if e.get("path") and str(e.get("svc")) in _USB_LIKE:
            # исполнитель/название берём из тегов файла (так исправляются и старые записи)
            a2, t2 = usb_meta(e["path"])
            if a2 or t2:
                return dict(e, artist=a2 or e.get("artist", ""), title=t2 or e.get("title", ""))
        return e

    def _artist_scores(self) -> Tuple[Dict[str, int], Dict[str, int], Dict[str, str]]:
        """По ВСЕЙ базе: solo[имя] — прослушивания, где исполнитель указан один;
        seen[имя] — во скольких прослушиваниях он вообще встречается (в т.ч. в связках);
        disp[имя] — как его писать. Кэш на минуту."""
        now = time.time()
        c = getattr(self, "_score_cache", None)
        if c and now - c[0] < 60:
            return c[1]
        solo: Dict[str, int] = {}
        seen: Dict[str, int] = {}
        disp: Dict[str, str] = {}
        for e in self.events():
            if self._category(str(e.get("svc") or "")) == "radio":
                continue
            e = self._with_usb_meta(e)
            art = (e.get("artist") or "").strip()
            if not art:
                continue
            parts = collab_parts(art)
            if len(parts) == 1:
                k = _norm(parts[0])
                if k:
                    solo[k] = solo.get(k, 0) + 1
                    seen[k] = seen.get(k, 0) + 1
                    disp.setdefault(k, parts[0])
            else:
                for x in parts:
                    k = _norm(x)
                    if k:
                        seen[k] = seen.get(k, 0) + 1
                        disp.setdefault(k, x)
        res = (solo, seen, disp)
        self._score_cache = (now, res)
        return res

    def _artist_resolver(self) -> Callable[[str], Tuple[str, str]]:
        """artist-строка → (ключ, имя для показа). Для связки выбирается тот из участников,
        которого в базе больше (сначала по сольным прослушиваниям, затем по общему числу
        появлений, затем первый в строке). Решение пересчитывается по всей базе, поэтому
        когда один из исполнителей становится «главным», его получают и старые связки."""
        solo, seen, disp = self._artist_scores()
        cache: Dict[str, Tuple[str, str]] = {}

        def resolve(art: str) -> Tuple[str, str]:
            hit = cache.get(art)
            if hit:
                return hit
            parts = collab_parts(art)
            if len(parts) <= 1:
                nm = parts[0] if parts else art
                k = _norm(nm) or nm.casefold()
                res = (k, disp.get(k, nm))
            else:
                best, best_key = None, None
                for i, x in enumerate(parts):
                    k = _norm(x)
                    if not k:
                        continue
                    score = (solo.get(k, 0), seen.get(k, 0), -i)
                    if best_key is None or score > best_key:
                        best, best_key = (k, disp.get(k, x)), score
                res = best or (_norm(art) or art.casefold(), art)
            cache[art] = res
            return res

        return resolve

    # ------------------------------------------- размер хранилища

    def prune_if_needed(self) -> int:
        """Если статистика занимает больше max_mb МБ — удалить 3 самых старых месяца
        (текущий не трогаем). Возвращает число удалённых месяцев."""
        limit = int(self._settings().get("max_mb", 0) or 0)
        if limit <= 0:
            return 0
        with self._lock:
            self._load()
            si = self.storage_info()
            if si["plain"] + si["gz"] <= limit * 1024 * 1024:
                return 0
            cur = self._cur_month
            old = [m for m in self._months_on_disk() if m < cur][:3]
            for m in old:
                for path in (self._mpath(m, gz=True), self._mpath(m)):
                    try:
                        if os.path.isfile(path):
                            os.remove(path)
                    except OSError as e:
                        log.warning("stats: не удалось удалить %s: %s", path, e)
            if old:
                self._old_cache = None
                self._score_cache = None
                self._lib_index = None
                log.info("stats: данные превысили %d МБ — удалены самые старые месяцы: %s", limit, ", ".join(old))
            return len(old)

    # --------------------------------------------------------------- отчёты

    @staticmethod
    def _category(svc: str) -> str:
        if svc == "radio":
            return "radio"
        if svc == "akniga":
            return "books"
        return "music"

    def summary(self, top_artists: int = 20, top_tracks: int = 50, year: Optional[int] = None,
                month: str = "") -> dict:
        ev = self.events_month(month) if month else self.events()
        excl = self._settings()["exclude_dirs"]
        if excl:
            ev = [e for e in ev if not (e.get("path") and self.is_excluded(e["path"], excl))]
        # исполнители из тегов USB — до подсчёта, чтобы связки разбирались по реальным именам
        ev = [self._with_usb_meta(e) for e in ev]
        canon = self._artist_resolver()
        lt = time.localtime
        total = music = radio = books = 0
        by_svc: Dict[str, dict] = {}
        artists: Dict[str, dict] = {}
        tracks: Dict[str, dict] = {}
        months: Dict[str, dict] = {}
        stations: Dict[str, dict] = {}
        years: Set[int] = set()
        cur_year = lt().tm_year
        year = year or cur_year
        ysum = {"sec": 0, "plays": 0, "svc": {}, "artists": {}, "tracks": {}}
        ymonths = {m: {"sec": 0, "plays": 0} for m in range(1, 13)}

        for e in ev:
            sec = int(e.get("s") or 0)
            svc = str(e.get("svc") or "")
            cat = self._category(svc)
            tm = lt(int(e["t"]))
            years.add(tm.tm_year)
            total += sec
            if cat == "radio":
                radio += sec
            elif cat == "books":
                books += sec
            else:
                music += sec
            b = by_svc.setdefault(svc, {"svc": svc, "sec": 0, "plays": 0})
            b["sec"] += sec
            b["plays"] += 1
            mk = f"{tm.tm_year}-{tm.tm_mon:02d}"
            m = months.setdefault(mk, {"month": mk, "sec": 0, "plays": 0, "svc": {}})
            m["sec"] += sec
            m["plays"] += 1
            m["svc"][svc] = m["svc"].get(svc, 0) + sec
            in_year = tm.tm_year == year
            if in_year:
                ysum["sec"] += sec
                ysum["plays"] += 1
                ysum["svc"][svc] = ysum["svc"].get(svc, 0) + sec
                ymonths[tm.tm_mon]["sec"] += sec
                ymonths[tm.tm_mon]["plays"] += 1
            if cat == "radio":
                name = e.get("st") or e.get("title") or "?"
                st = stations.setdefault(name, {"name": name, "sec": 0, "sessions": 0, "last": 0, "url": e.get("url", "")})
                st["sec"] += sec
                st["sessions"] += 1
                st["last"] = max(st["last"], int(e["t"]))
                if e.get("url"):
                    st["url"] = e["url"]
                continue
            art = (e.get("artist") or "").strip()
            if art:
                k, art_name = canon(art)
                a = artists.setdefault(k, {"name": art_name, "plays": 0, "sec": 0})
                a["plays"] += 1
                a["sec"] += sec
                if in_year:
                    ya = ysum["artists"].setdefault(k, {"name": art_name, "plays": 0, "sec": 0})
                    ya["plays"] += 1
                    ya["sec"] += sec
            tk = e.get("path") or f"{svc}:{e.get('id')}"
            t = tracks.setdefault(tk, {"title": e.get("title") or "?", "artist": art, "svc": svc,
                                       "path": e.get("path", ""), "plays": 0, "sec": 0})
            t["plays"] += 1
            t["sec"] += sec
            if in_year:
                yt = ysum["tracks"].setdefault(tk, {"title": t["title"], "artist": art, "plays": 0, "sec": 0})
                yt["plays"] += 1
                yt["sec"] += sec

        def top_of(d: dict, n: int):
            return sorted(d.values(), key=lambda x: (-x["plays"], -x["sec"]))[:n]

        svc_rows = sorted(by_svc.values(), key=lambda x: (-x["sec"], -x["plays"]))
        for m in months.values():
            m["top_svc"] = max(m["svc"], key=m["svc"].get) if m["svc"] else ""
        month_rows = sorted(months.values(), key=lambda x: x["month"], reverse=True)
        st_rows = sorted(stations.values(), key=lambda x: (-x["sec"], -x["sessions"]))
        ytop_a = top_of(ysum["artists"], 1)
        ytop_t = top_of(ysum["tracks"], 1)
        return {
            "total": total, "music": music, "radio": radio, "books": books, "plays": len(ev),
            # «любимая площадка» — среди музыкальных; радио/книги — только если больше ничего нет
            "top_service": next((r for r in svc_rows if self._category(r["svc"]) == "music"),
                                svc_rows[0] if svc_rows else None),
            "services": svc_rows,
            "artists": top_of(artists, top_artists),
            "tracks": top_of(tracks, top_tracks),
            "months": month_rows[:24],
            "stations": st_rows,
            "year": year, "years": sorted(years | {cur_year}, reverse=True),
            "year_sum": {
                "sec": ysum["sec"], "plays": ysum["plays"],
                "top_service": max(ysum["svc"], key=ysum["svc"].get) if ysum["svc"] else "",
                "top_artist": ytop_a[0] if ytop_a else None,
                "top_track": ytop_t[0] if ytop_t else None,
                "months": [ymonths[i] for i in range(1, 13)],
            },
        }

    # ------------------------------------------------------- автоплейлист

    def _library_index(self, files, force: bool = False) -> list:
        """[(путь, исполнитель(норм.), название(норм.))] по USB. Кэш на 6 часов."""
        now = time.time()
        if not force and self._lib_index and now - self._lib_index[0] < 6 * 3600:
            return self._lib_index[1]
        from .files_lib import AUDIO_EXT
        out: list = []
        cap = 30000
        for root in list(getattr(files, "roots", []) or []):
            for dirpath, dirnames, filenames in os.walk(str(root)):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for fn in filenames:
                    if fn.startswith(".") or os.path.splitext(fn)[1].lower() not in AUDIO_EXT:
                        continue
                    p = os.path.join(dirpath, fn)
                    stem = os.path.splitext(fn)[0]
                    art = display_artist(stem, p)
                    title = re.sub(r"^\s*\d{1,3}\s*[.)\-_]\s*", "", stem)
                    if art and title.casefold().startswith(art.casefold()):
                        title = re.sub(r"^\s*[-–—]\s*", "", title[len(art):])
                    out.append((p, _primary(art) if art else "", _norm(title)))
                    if len(out) >= cap:
                        break
                if len(out) >= cap:
                    break
        self._lib_index = (now, out)
        return out

    def build_auto_playlist(self, files, settings: dict) -> Tuple[int, str]:
        """Собрать/обновить USB-плейлист из самых часто прослушиваемых треков статистики.

        Берутся именно треки (а не исполнители): прослушивания одного и того же трека на
        разных площадках и на USB складываются, треки ранжируются по числу прослушиваний
        (при равенстве — по времени) и берутся сверху вниз, пока есть файл на USB,
        до «Макс. треков». Треки исполнителей, которые вы не слушали, не добавляются.
        """
        st = clean_settings(settings)
        # весь рейтинг треков, а не только «Топ треков» для отчёта: часть треков со
        # стриминга может не оказаться на флешке, и добирать приходится глубже по списку
        summ = self.summary(top_artists=1, top_tracks=1_000_000)
        if not summ["tracks"]:
            return 0, "В статистике пока нет прослушиваний — плейлист не создан"
        index = self._library_index(files)
        if not index:
            return 0, "На USB не найдено аудиофайлов (проверьте корни USB в настройках)"
        by_path = {p for p, _a, _t in index}
        by_title: Dict[Tuple[str, str], List[str]] = {}
        for p, a, t in index:
            if a:
                by_title.setdefault((a, t), []).append(p)

        # трек статистики → файл на USB; прослушивания одного файла суммируем
        score: Dict[str, List[int]] = {}
        first_rank: Dict[str, int] = {}
        for rank, t in enumerate(summ["tracks"]):
            path = t.get("path") or ""
            if path and (path in by_path or os.path.isfile(path)):
                target = path                           # играли прямо с USB
            else:                                       # другая площадка — по «исполнитель + название»
                a = _primary(t.get("artist") or "")
                cand = sorted(by_title.get((a, _norm(t.get("title") or "")), []))
                target = cand[0] if cand else ""        # один файл на трек, без копий-дублей
            if not target:
                continue
            sc = score.setdefault(target, [0, 0])
            sc[0] += int(t.get("plays") or 0)
            sc[1] += int(t.get("sec") or 0)
            first_rank.setdefault(target, rank)
        if not score:
            return 0, "Самые прослушиваемые треки не найдены среди файлов на USB"
        ranked = sorted(score, key=lambda p: (-score[p][0], -score[p][1], first_rank[p]))
        chosen: List[str] = ranked[: st["auto_max"]]
        # порядок: исполнители не идут подряд
        arts = [split_artists(display_artist(os.path.basename(p), p)) for p in chosen]
        order = smart_shuffle(arts)
        ordered = [chosen[k] for k in order]
        title = st["auto_title"]
        pid = ""
        try:
            for pl in files.discover_playlists(force=True):
                if pl.title == title:
                    pid = pl.id
                    break
        except Exception:  # noqa: BLE001
            pass
        pl, err = files.save_playlist(title, ["f:" + p for p in ordered], pid=pid)
        if err:
            return 0, f"Не удалось сохранить плейлист: {err}"
        n = len(pl.tracks)
        msg = f"Плейлист «{title}» обновлён: {n} треков"
        self.auto_status = f"{time.strftime('%d.%m %H:%M')} — {msg}"
        self._save_auto_state()
        return n, msg

    def _save_auto_state(self) -> None:
        try:
            with open(self.auto_state_path, "w", encoding="utf-8") as f:
                json.dump({"last_run": time.time(), "status": self.auto_status}, f, ensure_ascii=False)
        except OSError:
            pass

    def _load_auto_state(self) -> dict:
        try:
            with open(self.auto_state_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            return {}

    def auto_last_status(self) -> str:
        if self.auto_status:
            return self.auto_status
        return self._load_auto_state().get("status", "")

    def run_auto_async(self, files, settings: dict) -> bool:
        """Собрать плейлист в фоне (обход USB может занять время). False — уже идёт."""
        with self._lock:
            if self._auto_running:
                return False
            self._auto_running = True

        def work():
            try:
                self.auto_status = "идёт сборка…"
                n, msg = self.build_auto_playlist(files, settings)
                self.auto_status = f"{time.strftime('%d.%m %H:%M')} — {msg}"
                if n:
                    self._save_auto_state()
            except Exception as e:  # noqa: BLE001
                log.warning("auto playlist: %s", e)
                self.auto_status = f"ошибка: {e}"
            finally:
                self._auto_running = False

        threading.Thread(target=work, name="stats-autoplaylist", daemon=True).start()
        return True

    def start_auto_scheduler(self, files_getter: Callable[[], object],
                             settings_getter: Callable[[], dict]) -> None:
        """Фоновая проверка: пора ли обновить плейлист (раз в 5 минут)."""
        def loop():
            time.sleep(60)
            while True:
                try:
                    st = clean_settings(settings_getter())
                    if st["auto_playlist"]:
                        last = float(self._load_auto_state().get("last_run") or 0)
                        files = files_getter()
                        if files is not None and time.time() - last >= st["auto_hours"] * 3600:
                            self.run_auto_async(files, st)
                except Exception as e:  # noqa: BLE001
                    log.debug("auto scheduler: %s", e)
                time.sleep(300)

        threading.Thread(target=loop, name="stats-auto-scheduler", daemon=True).start()


# ====================================================================
#  История эфира радио
# ====================================================================

_JUNK_RE = re.compile(r"^(?:\s*|-+|n/?a|unknown|null|http\S*|advert\w*|реклам\w*)$", re.I)


class RadioHistory:
    PER_STATION = 120
    MAX_STATIONS = 60

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._st: Dict[str, dict] = {}      # url -> {name, last, tracks:[{t, raw, artist, title}]}
        self._last_save = 0.0
        self._dirty = False
        self._warned = False
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            st = data.get("stations") if isinstance(data, dict) else None
            if isinstance(st, dict):
                self._st = {u: e for u, e in st.items() if isinstance(e, dict)}
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001
            log.warning("radio_history: не удалось прочитать %s: %s", self.path, e)

    def _save(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_save < 10:
            self._dirty = True
            return
        self._last_save = now
        self._dirty = False
        payload = json.dumps({"version": 1, "stations": self._st}, ensure_ascii=False)
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, self.path)
        except OSError as e:
            if not self._warned:
                self._warned = True
                log.warning("radio_history: нет записи в %s (%s)", self.path, e)

    def flush(self) -> None:
        with self._lock:
            if self._dirty:
                self._save(force=True)

    def touch(self, url: str, name: str) -> None:
        """Станцию включили (для порядка «последние потоки»)."""
        if not url:
            return
        with self._lock:
            e = self._st.setdefault(url, {"name": name or url, "last": 0, "tracks": []})
            if name:
                e["name"] = name
            e["last"] = time.time()
            if len(self._st) > self.MAX_STATIONS:
                old = min(self._st, key=lambda u: self._st[u].get("last", 0))
                self._st.pop(old, None)
            self._save()

    def title(self, url: str, raw: str) -> None:
        """Станция передала название трека (ICY)."""
        raw = (raw or "").strip()
        if not url or not raw or _JUNK_RE.match(raw):
            return
        with self._lock:
            e = self._st.setdefault(url, {"name": url, "last": 0, "tracks": []})
            tr = e["tracks"]
            if tr and tr[-1].get("raw") == raw:
                e["last"] = time.time()
                return
            artist, title = split_title(raw)
            now = time.time()
            tr.append({"t": int(now), "raw": raw, "artist": artist, "title": title})
            if len(tr) > self.PER_STATION:
                del tr[: len(tr) - self.PER_STATION]
            e["last"] = now
            self._save()

    def latest(self, n_stations: int, n_tracks: int, names: Optional[Dict[str, str]] = None) -> List[dict]:
        names = names or {}
        with self._lock:
            rows = sorted(self._st.items(), key=lambda kv: -float(kv[1].get("last") or 0))
            out = []
            for url, e in rows:
                if not e.get("tracks"):
                    continue
                out.append({
                    "url": url,
                    "name": names.get(url) or e.get("name") or url,
                    "last": float(e.get("last") or 0),
                    "tracks": list(reversed(e["tracks"][-n_tracks:])),
                })
                if len(out) >= n_stations:
                    break
            return out

    def clear(self) -> None:
        with self._lock:
            self._st.clear()
            self._save(force=True)


# ====================================================================
#  Синглтоны
# ====================================================================

STATS: Optional[Stats] = None
RADIO_HISTORY: Optional[RadioHistory] = None


def init(directory: str) -> None:
    global STATS, RADIO_HISTORY
    STATS = Stats(directory)
    try:
        STATS._load()                # миграция старого файла и сжатие прошлых месяцев — сразу при старте
    except Exception as e:  # noqa: BLE001
        log.warning("stats: загрузка: %s", e)
    RADIO_HISTORY = RadioHistory(os.path.join(directory or ".", "radio_history.json"))


def get_stats() -> Optional[Stats]:
    return STATS


def get_radio_history() -> Optional[RadioHistory]:
    return RADIO_HISTORY
