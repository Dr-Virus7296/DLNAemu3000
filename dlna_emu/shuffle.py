"""«Умное» перемешивание плейлиста: один исполнитель как можно реже подряд
и через один трек.

Исполнитель берётся из названия («Artist - Title» из m3u / имени файла),
а если там его нет — из тегов файла (ID3v2 TPE1 у MP3, ARTIST у FLAC).
"""

from __future__ import annotations

import logging
import math
import os
import random
import re
import struct
from typing import Dict, List, Optional, Sequence, Set, Tuple

log = logging.getLogger("dlna_emu.shuffle")

_SEP_RE = re.compile(r"\s+[-–—]\s+")
_NUM_PREFIX_RE = re.compile(r"^\s*\d{1,3}\s*[.)\-_]\s*")
# несколько исполнителей в одном поле
_MULTI_RE = re.compile(
    r"\s*(?:,|;|/|&|\bfeat\.?\b|\bft\.?\b|\bfeaturing\b|\bvs\.?\b|\bи\b|\bпри уч\.?\b)\s*",
    re.IGNORECASE,
)

KMAX = 5  # к какому расстоянию между одинаковыми исполнителями стремимся


# ------------------------------------------------------------------ артисты

def split_artists(raw: str) -> Set[str]:
    """'A feat. B, C' → {'a', 'b', 'c'} (нормализовано)."""
    out: Set[str] = set()
    for part in _MULTI_RE.split(raw or ""):
        p = re.sub(r"\s+", " ", part).strip(" .\t").casefold()
        if p:
            out.add(p)
    return out


def artist_from_name(name: str) -> str:
    """'03. Linkin Park - Numb' → 'Linkin Park'; без разделителя → ''."""
    n = _NUM_PREFIX_RE.sub("", name or "").strip()
    parts = _SEP_RE.split(n, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        a = parts[0].strip()
        if re.fullmatch(r"[\d\s.\-_#№]+", a):      # «2019 - Название», «05 - Название»
            return ""
        return a
    return ""


def _decode_text(enc: int, data: bytes) -> str:
    try:
        if enc == 1:
            return data.decode("utf-16", errors="replace")
        if enc == 2:
            return data.decode("utf-16-be", errors="replace")
        if enc == 3:
            return data.decode("utf-8", errors="replace")
        # enc 0 — «latin-1», но в русских библиотеках там обычно cp1251
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("cp1251", errors="replace")
    except Exception:
        return ""


def _synchsafe(b: bytes) -> int:
    return (b[0] << 21) | (b[1] << 14) | (b[2] << 7) | b[3]


def _tags_mp3(path: str) -> Tuple[str, str]:
    """(исполнитель, название) из ID3v2."""
    with open(path, "rb") as f:
        head = f.read(10)
        if len(head) < 10 or head[:3] != b"ID3":
            return "", ""
        ver = head[3]
        total = _synchsafe(head[6:10])
        v2 = ver == 2
        hlen = 6 if v2 else 10
        pos = 0
        found: Dict[bytes, str] = {}
        want = (b"TPE1", b"TP1", b"TPE2", b"TP2", b"TIT2", b"TT2")
        while pos + hlen <= total:
            f.seek(10 + pos)
            h = f.read(hlen)
            if len(h) < hlen or h[0:1] == b"\x00":
                break
            if v2:
                fid = h[:3]
                size = (h[3] << 16) | (h[4] << 8) | h[5]
            else:
                fid = h[:4]
                size = _synchsafe(h[4:8]) if ver == 4 else struct.unpack(">I", h[4:8])[0]
            if size <= 0 or pos + hlen + size > total:
                break
            if fid in want and size < 4096:
                body = f.read(size)
                if body:
                    found[fid] = _decode_text(body[0], body[1:]).strip("\x00 ")
            pos += hlen + size
        artist = ""
        for key in (b"TPE1", b"TP1", b"TPE2", b"TP2"):
            if found.get(key):
                artist = found[key].split("\x00")[0].strip()
                break
        title = (found.get(b"TIT2") or found.get(b"TT2") or "").split("\x00")[0].strip()
        return artist, title


def _tags_flac(path: str) -> Tuple[str, str]:
    with open(path, "rb") as f:
        head = f.read(10)
        skip = 0
        if head[:3] == b"ID3" and len(head) == 10:
            skip = 10 + _synchsafe(head[6:10])
        f.seek(skip)
        if f.read(4) != b"fLaC":
            return "", ""
        while True:
            h = f.read(4)
            if len(h) < 4:
                return "", ""
            last = bool(h[0] & 0x80)
            btype = h[0] & 0x7F
            blen = (h[1] << 16) | (h[2] << 8) | h[3]
            if btype == 4 and blen < 1024 * 1024:
                blk = f.read(blen)
                p = 4 + struct.unpack("<I", blk[:4])[0]
                n = struct.unpack("<I", blk[p:p + 4])[0]
                p += 4
                tags: Dict[str, str] = {}
                for _ in range(n):
                    ln = struct.unpack("<I", blk[p:p + 4])[0]
                    p += 4
                    item = blk[p:p + ln].decode("utf-8", errors="replace")
                    p += ln
                    if "=" in item:
                        k, v = item.split("=", 1)
                        tags.setdefault(k.upper(), v)
                return ((tags.get("ARTIST") or tags.get("ALBUMARTIST") or "").strip(),
                        (tags.get("TITLE") or "").strip())
            f.seek(blen, 1)
            if last:
                return "", ""


_TAG_CACHE: Dict[Tuple[str, float], Tuple[str, str]] = {}


def read_tags(path: str) -> Tuple[str, str]:
    """(исполнитель, название) из тегов файла (MP3/FLAC); ('', '') — тегов нет."""
    try:
        key = (path, os.stat(path).st_mtime)
    except OSError:
        return "", ""
    hit = _TAG_CACHE.get(key)
    if hit is not None:
        return hit
    ext = os.path.splitext(path)[1].lower()
    val: Tuple[str, str] = ("", "")
    try:
        if ext == ".mp3":
            val = _tags_mp3(path)
        elif ext == ".flac":
            val = _tags_flac(path)
    except Exception as e:
        log.debug("tags %s: %s", path, e)
    if len(_TAG_CACHE) > 20000:
        _TAG_CACHE.clear()
    _TAG_CACHE[key] = val
    return val


def artist_from_tags(path: str) -> str:
    return read_tags(path)[0]


def artists_for(name: str, path: str) -> Set[str]:
    """Множество исполнителей трека: имя → теги. Пусто — исполнитель неизвестен."""
    raw = artist_from_tags(path) or artist_from_name(name) \
        or artist_from_name(os.path.splitext(os.path.basename(path))[0])
    return split_artists(raw)


_YEAR_DIR_RE = re.compile(r"^\s*[\[(]?(19|20)\d{2}\b")


def _artist_from_folder(path: str) -> str:
    """Запасной вариант: …/Исполнитель/[2005 - Альбом]/трек → имя папки исполнителя."""
    parts = [x for x in re.split(r"[\\/]+", path) if x][:-1]
    if not parts:
        return ""
    cand = parts[-1]
    if _YEAR_DIR_RE.match(cand) and len(parts) >= 2:
        cand = parts[-2]
    return cand


def artists_for_tracks(tracks: Sequence[Tuple[str, str]]) -> List[Set[str]]:
    """[(название, путь)] → множества исполнителей.

    Порядок источников: «Artist - Title» в названии/имени файла → теги файла →
    имя папки исполнителя. Папочная догадка отбрасывается, если она «склеивает»
    больше половины плейлиста (значит это жанровая папка вроде Rock/, а не артист).
    """
    base: List[Set[str]] = []
    folder_guess: List[str] = []
    for name, path in tracks:
        a = artists_for(name, path)
        base.append(a)
        folder_guess.append("" if a else _artist_from_folder(path))
    n = len(tracks)
    counts: Dict[str, int] = {}
    for g in folder_guess:
        if g:
            counts[g.casefold()] = counts.get(g.casefold(), 0) + 1
    out: List[Set[str]] = []
    for a, g in zip(base, folder_guess):
        if a:
            out.append(a)
        elif g and not (counts[g.casefold()] > 5 and counts[g.casefold()] > n / 2):
            out.append(split_artists(g))
        else:
            out.append(set())
    return out


# ----------------------------------------------------------------- алгоритм

def smart_shuffle(artists: Sequence[Set[str]], rng: Optional[random.Random] = None) -> List[int]:
    """Вернуть порядок индексов 0..n-1.

    artists[i] — множество исполнителей i-го трека (пустое = неизвестен,
    не конфликтует ни с чем). Жадный алгоритм «k-distance rearrange»:
    на каждом шаге берём трек, у которого исполнитель не звучал последние
    k-1 позиций; среди подходящих — случайный, но «срочным» (чьи треки
    иначе уже не удастся развести) отдаём приоритет. Если разнести нельзя,
    берётся вариант с наибольшим расстоянием до прошлого повтора.
    """
    rng = rng or random.Random()
    n = len(artists)
    if n <= 1:
        return list(range(n))

    # группируем по основному исполнителю (лексикографически первому — стабильно)
    groups: Dict[str, List[int]] = {}
    loners: List[int] = []
    for i, s in enumerate(artists):
        if not s:
            loners.append(i)
        else:
            groups.setdefault(min(s), []).append(i)
    for g in groups.values():
        rng.shuffle(g)
    for i in loners:                       # безымянные — каждый сам себе группа
        groups[f"\x00{i}"] = [i]

    # счётчик по каждому исполнителю (включая «feat.»-участников) для оценки срочности
    max_cnt = max(len(g) for g in groups.values())
    k = max(2, min(KMAX, n // max_cnt)) if max_cnt > 1 else 2

    last_pos: Dict[str, int] = {}
    virt_last: Dict[str, float] = {}   # последняя позиция группы (с учётом случайной фазы)
    order: List[int] = []
    remaining = n

    def gap(idx: int, pos: int) -> float:
        g = math.inf
        for a in artists[idx]:
            lp = last_pos.get(a)
            if lp is not None:
                g = min(g, pos - lp)
        return g

    while remaining:
        pos = len(order)
        # кандидаты: по одному (случайному) треку от каждой группы
        cands: List[Tuple[str, int, float]] = []
        for key, g in groups.items():
            if not g:
                continue
            # внутри группы выбираем трек с наибольшим gap (важно для feat.)
            best_i, best_gap = g[-1], gap(g[-1], pos)
            if len(g) > 1 and best_gap < k:
                for idx in g[-4:]:
                    gp = gap(idx, pos)
                    if gp > best_gap:
                        best_i, best_gap = idx, gp
            cands.append((key, best_i, best_gap))

        eligible = [c for c in cands if c[2] >= k]
        if eligible:
            # 1) срочные: иначе их треки уже не удастся развести на остатке списка
            urgent = [
                c for c in eligible
                if len(groups[c[0]]) > 1 and (len(groups[c[0]]) - 1) * k + 1 >= remaining - 1
            ]
            if urgent:
                top = max(len(groups[c[0]]) for c in urgent)
                key, idx, _ = rng.choice([c for c in urgent if len(groups[c[0]]) == top])
            else:
                # 2) «шаговое» планирование: у исполнителя с m треками на остаток
                # идеальный шаг = remaining / m. Берём того, кто больше всех «просрочил»
                # свой шаг. У исполнителей с одним треком шага нет — они заполняют
                # промежутки. Случайная начальная фаза даёт случайность порядка.
                best = None
                for c in eligible:
                    m = len(groups[c[0]])
                    if m == 1:
                        score = 0.45 + rng.random() * 0.1
                    else:
                        stride = remaining / m
                        lp = virt_last.get(c[0])
                        if lp is None:
                            lp = -rng.uniform(0, stride)
                            virt_last[c[0]] = lp
                        score = (pos - lp) / stride + rng.random() * 0.6
                    if best is None or score > best[0]:
                        best = (score, c)
                key, idx, _ = best[1]
        else:
            # разнести нельзя: максимальный gap, затем «у кого больше осталось»
            top_gap = max(c[2] for c in cands)
            pool = [c for c in cands if c[2] == top_gap]
            top = max(len(groups[c[0]]) for c in pool)
            pool = [c for c in pool if len(groups[c[0]]) == top]
            key, idx, _ = rng.choice(pool)

        groups[key].remove(idx)
        virt_last[key] = pos
        order.append(idx)
        for a in artists[idx]:
            last_pos[a] = pos
        remaining -= 1
    return order


def repeat_stats(artists: Sequence[Set[str]], order: Sequence[int]) -> Tuple[int, int]:
    """(повторов подряд, повторов через один трек) — для тестов и логов."""
    adj = skip = 0
    for p in range(1, len(order)):
        a, b = artists[order[p - 1]], artists[order[p]]
        if a and b and a & b:
            adj += 1
        if p >= 2:
            c = artists[order[p - 2]]
            if c and b and c & b:
                skip += 1
    return adj, skip
