"""Локальные файлы с USB + M3U с абсолютными путями (/tmp/mnt/...)."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote, unquote

log = logging.getLogger("dlna_emu.files")

AUDIO_EXT = {
    ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".wma",
    ".aiff", ".ape", ".wv", ".mpc",
}
PLAYLIST_EXT = {".m3u", ".m3u8"}


def _safe_seg(s: str) -> str:
    return quote(s, safe="")


def _unseg(s: str) -> str:
    return unquote(s)


@dataclass
class LocalStation:
    name: str
    path: str  # absolute path on disk


@dataclass
class LocalPlaylist:
    id: str
    title: str
    path: str
    tracks: List[LocalStation] = field(default_factory=list)


def mime_for(path: str) -> str:
    ext = Path(path).suffix.lower()
    return {
        ".mp3": "audio/mpeg",
        ".flac": "audio/flac",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".ogg": "audio/ogg",
        ".opus": "audio/ogg",
        ".wav": "audio/wav",
        ".wma": "audio/x-ms-wma",
    }.get(ext, "application/octet-stream")


def _read_m3u_text(path: Path) -> str:
    """M3U бывают в UTF-8 и в CP1251 (Windows) — пробуем по очереди."""
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1251", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_local_m3u(path: Path) -> List[LocalStation]:
    tracks: List[LocalStation] = []
    try:
        text = _read_m3u_text(path)
    except Exception as e:
        log.warning("read m3u %s: %s", path, e)
        return tracks
    title: Optional[str] = None
    base = path.parent
    for raw in text.splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith("#EXTM3U"):
            continue
        if line.startswith("#EXTINF:"):
            m = re.match(r"#EXTINF:[^,]*,\s*(.+)$", line)
            title = m.group(1).strip() if m else None
            continue
        if line.startswith("#"):
            continue
        if line.lower().startswith("file://"):
            line = unquote(line[7:])
        # обратные слэши Windows → прямые (кроме дисковых путей вида D:\...)
        win_abs = bool(re.match(r"^[A-Za-z]:[\\/]", line))
        if "\\" in line and not win_abs:
            line = line.replace("\\", "/")
        if line.startswith("/") or win_abs:
            fpath = line
        else:
            fpath = str((base / line).resolve())
        name = title or Path(fpath).stem
        title = None
        tracks.append(LocalStation(name=name, path=fpath))
    return tracks


class FilesLibrary:
    """
    roots: список корней USB, напр. ["/tmp/mnt"] или ["/tmp/mnt/6EE7-47CC"]
    playlist_folder: отдельно папка с m3u (может совпадать с roots)
    """

    PL_CACHE_SEC = 15.0

    def __init__(
        self,
        roots: List[str],
        playlist_globs: Optional[List[str]] = None,
        playlist_folders: Optional[List[str]] = None,
    ):
        self.roots = [Path(r) for r in roots if r]
        self.playlist_globs = playlist_globs or []
        # Явные папки с .m3u (config.yaml → files.playlists_folder). Пусто — искать под roots.
        self.playlist_folders: List[Path] = []
        self._pl_cache: Dict[str, LocalPlaylist] = {}
        self._pl_list: Optional[List[LocalPlaylist]] = None
        self._pl_ts: float = 0.0
        self.set_playlist_folders(playlist_folders or [])

    def set_playlist_folders(self, folders) -> None:
        """Задать папки с плейлистами (можно на лету, из веб-админки)."""
        if isinstance(folders, str):
            folders = [ln for ln in folders.replace(",", "\n").splitlines()]
        self.playlist_folders = [Path(f.strip()).expanduser() for f in folders if f and f.strip()]
        self._pl_list = None
        self._pl_ts = 0.0

    def _allowed_path(self, path: Path) -> bool:
        try:
            real = path.resolve()
        except Exception:
            return False
        for root in self.roots:
            try:
                real.relative_to(root.resolve())
                return True
            except ValueError:
                continue
            except Exception:
                continue
        return False

    def list_dir(self, rel: str = "") -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]]]:
        """
        rel — путь относительно первого подходящего корня, через /.
        Возвращает (dirs, files) как (name, rel_path).
        Если rel пустой — список корней / верхний уровень.
        """
        dirs: List[Tuple[str, str]] = []
        files: List[Tuple[str, str]] = []

        if not rel:
            # показать каждый root как папку, или если один root — его содержимое
            if len(self.roots) == 1 and self.roots[0].is_dir():
                return self._scan(self.roots[0], "")
            for root in self.roots:
                if root.is_dir():
                    name = root.name or str(root)
                    dirs.append((name, _safe_seg(str(root))))
            return dirs, files

        # rel is encoded absolute path segments joined
        target = Path(_unseg(rel))
        if not target.is_absolute():
            # try under roots
            for root in self.roots:
                cand = root / _unseg(rel)
                if cand.exists():
                    target = cand
                    break
        if not self._allowed_path(target) and not any(
            str(target).startswith(str(r)) for r in self.roots
        ):
            # allow if under any root string prefix
            ok = False
            for r in self.roots:
                if str(target.resolve()).startswith(str(r.resolve())):
                    ok = True
                    break
            if not ok:
                return [], []
        return self._scan(target, rel)

    def _scan(self, folder: Path, rel_prefix: str) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]]]:
        dirs: List[Tuple[str, str]] = []
        files: List[Tuple[str, str]] = []
        try:
            entries = sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as e:
            log.warning("list %s: %s", folder, e)
            return dirs, files
        for p in entries:
            if p.name.startswith("."):
                continue
            if p.is_dir():
                child_rel = str(p) if not rel_prefix else str(p)
                dirs.append((p.name, _safe_seg(child_rel)))
            elif p.suffix.lower() in AUDIO_EXT:
                child_rel = str(p)
                files.append((p.name, _safe_seg(child_rel)))
        return dirs, files

    def resolve_file(self, encoded_path: str) -> Optional[Path]:
        path = Path(_unseg(encoded_path))
        if not path.is_file():
            return None
        # security: must be under roots
        try:
            real = path.resolve()
            for r in self.roots:
                try:
                    real.relative_to(r.resolve())
                    return real
                except ValueError:
                    continue
        except Exception:
            pass
        # also allow /tmp/mnt/* common on Keenetic
        s = str(path)
        if s.startswith("/tmp/mnt/") or s.startswith("/opt/"):
            if path.is_file():
                return path
        return None

    def discover_playlists(self, force: bool = False) -> List[LocalPlaylist]:
        """Найти .m3u: в files.playlists_folder (если задана), иначе под roots.

        Результат кэшируется на PL_CACHE_SEC — Browse списка и открытие плейлиста
        не сканируют диск заново (на роутере с USB обход дерева занимает секунды).
        """
        now = time.time()
        if not force and self._pl_list is not None and (now - self._pl_ts) < self.PL_CACHE_SEC:
            return self._pl_list

        found: Dict[str, Path] = {}

        def scan(base: Path, max_depth: int) -> None:
            if not base.is_dir():
                log.warning("playlists folder not found: %s", base)
                return
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
                rel = Path(dirpath).relative_to(base)
                if len(rel.parts) >= max_depth:
                    dirnames.clear()
                for fn in filenames:
                    if fn.startswith("."):
                        continue
                    if Path(fn).suffix.lower() in PLAYLIST_EXT:
                        p = Path(dirpath) / fn
                        found[str(p)] = p
                if len(found) > 500:
                    return

        if self.playlist_folders:
            for folder in self.playlist_folders:
                scan(folder, 3)
        else:
            for root in self.roots:
                scan(root, 4)

        playlists: List[LocalPlaylist] = []
        used: set = set()
        for p in sorted(found.values(), key=lambda x: (x.stem.lower(), str(x))):
            # стабильный id: не зависит от числа и порядка других плейлистов
            h = hashlib.md5(str(p).encode("utf-8", "replace")).hexdigest()[:8]
            slug = re.sub(r"[^\w\-]+", "_", p.stem)[:30] or "pl"
            pid = f"{slug}_{h}"
            if pid in used:
                continue
            used.add(pid)
            tracks = parse_local_m3u(p)
            playlists.append(LocalPlaylist(id=pid, title=p.stem, path=str(p), tracks=tracks))
        self._pl_list = playlists
        self._pl_ts = now
        log.info("USB playlists: %d (%s)", len(playlists),
                 ", ".join(str(f) for f in self.playlist_folders) or "roots")
        return playlists

    def get_playlist(self, pid: str) -> Optional[LocalPlaylist]:
        for pl in self.discover_playlists():
            if pl.id == pid:
                return pl
        return None
