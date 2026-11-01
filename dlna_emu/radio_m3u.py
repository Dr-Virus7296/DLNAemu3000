"""Парсер папки с M3U/M3U8 радио-плейлистами + управление станциями через WEB."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("dlna_emu.radio")


@dataclass
class RadioStation:
    name: str
    url: str
    playlist: str  # имя файла без расширения
    logo: str = ""  # tvg-logo из #EXTINF (логотип станции для обложки)


@dataclass
class RadioPlaylist:
    id: str          # безопасный id (имя файла)
    title: str       # отображаемое имя
    path: str
    stations: List[RadioStation] = field(default_factory=list)


def _safe_id(name: str) -> str:
    s = re.sub(r"[^\w\-]+", "_", name, flags=re.UNICODE).strip("_")
    return s or "playlist"


_AUDIO_EXTS = (".mp3", ".aac", ".ogg", ".m4a", ".flac", ".wav", ".opus", ".m3u", ".m3u8")


def _ensure_mp3_suffix(url: str) -> str:
    """Суффикс ?.mp3 для совместимости с ресиверами, если в path нет аудио-расширения.

    Правильный вид:  https://host/stream?.mp3
    Неверно:        …stream.mp3?  …stream??.mp3  …stream?.mp3?
    """
    url = (url or "").strip()
    if not url:
        return url

    # Убрать хвосты от прошлых кривых преобразований
    while True:
        low = url.lower()
        if low.endswith("?.mp3?"):
            url = url[:-1]  # ?.mp3? → ?.mp3
            continue
        if low.endswith("??.mp3"):
            url = url[:-6] + "?.mp3"  # ??.mp3 (6 символов) → ?.mp3
            continue
        if low.endswith(".mp3?"):
            url = url[:-1]  # .mp3? → .mp3
            continue
        if low.endswith("?") and not low.endswith("?.mp3"):
            url = url[:-1]
            continue
        break

    if url.lower().endswith("?.mp3"):
        return url

    path = url.split("?", 1)[0].rstrip("/")
    lower_path = path.lower()
    if any(lower_path.endswith(ext) for ext in _AUDIO_EXTS):
        # Уже есть расширение в path — сохраняем URL (с query, если есть)
        return url if "?" in url else path

    # Нет аудио-расширения → ровно ?.mp3 в конце
    return path + "?.mp3"


def parse_m3u(path: Path) -> List[RadioStation]:
    """Разобрать один .m3u/.m3u8 файл."""
    stations: List[RadioStation] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        log.warning("cannot read %s: %s", path, e)
        return stations

    pl_name = path.stem
    current_title: Optional[str] = None
    current_logo = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#EXTM3U"):
            continue
        if line.startswith("#EXTINF:"):
            m = re.match(r"#EXTINF:[^,]*,\s*(.+)$", line)
            current_title = (m.group(1).strip() if m else line) or None
            lm = re.search(r'tvg-logo\s*=\s*"([^"]*)"', line, re.I)
            current_logo = lm.group(1).strip() if lm else ""
            continue
        if line.startswith("#"):
            continue
        url = line
        title = current_title or url
        logo = current_logo
        current_title = None
        current_logo = ""
        if url.startswith("http://") or url.startswith("https://"):
            stations.append(RadioStation(name=title, url=url, playlist=pl_name, logo=logo))
    return stations


def write_m3u(path: Path, stations: List[RadioStation]) -> None:
    """Записать список станций в M3U."""
    lines = ["#EXTM3U"]
    for st in stations:
        logo = f' tvg-logo="{st.logo}"' if getattr(st, "logo", "") else ""
        lines.append(f"#EXTINF:-1{logo},{st.name}")
        lines.append(st.url)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


APP_DIR = Path(__file__).resolve().parent.parent


def resolve_radio_folder(value: str = "") -> Path:
    """Пусто → <папка эмулятора>/radio; относительный путь считается от папки эмулятора."""
    value = (value or "").strip()
    if not value:
        return APP_DIR / "radio"
    p = Path(value).expanduser()
    return p if p.is_absolute() else APP_DIR / p


def clamp_proxy_retries(v, default: int = 5) -> int:
    """Сколько раз переподключаться к станции при таймауте/обрыве (0..20)."""
    try:
        n = int(v)
    except (TypeError, ValueError):
        n = default
    return max(0, min(n, 20))


class RadioLibrary:
    def __init__(
        self,
        folder: str,
        reload_on_browse: bool = True,
        auto_mp3_suffix: bool = True,
        proxy: bool = True,
        proxy_retries: int = 5,
    ):
        self.folder = resolve_radio_folder(folder)
        self.reload_on_browse = reload_on_browse
        self.auto_mp3_suffix = bool(auto_mp3_suffix)
        # True: поток станции идёт через эмулятор; False: редирект 302 на станцию
        self.proxy = bool(proxy)
        # Повторы при таймауте/обрыве потока станции (0 — не повторять)
        self.proxy_retries = clamp_proxy_retries(proxy_retries)
        # «Ваше популярное радио» (RadioPopular) — подключается в run_server
        self.popular = None
        # Галка «Ваше популярное радио» в админке (по умолчанию включено)
        self.popular_enabled = True
        self._playlists: Dict[str, RadioPlaylist] = {}
        self._mtime: float = 0.0
        self.reload()

    def reload(self) -> None:
        if not self.folder.is_dir():
            try:
                self.folder.mkdir(parents=True, exist_ok=True)
            except OSError:
                log.warning("radio folder missing and cannot create: %s", self.folder)
                self._playlists = {}
                return
        playlists: Dict[str, RadioPlaylist] = {}
        for path in sorted(self.folder.iterdir()):
            if path.suffix.lower() not in (".m3u", ".m3u8"):
                continue
            if path.name.startswith("."):
                continue
            stations = parse_m3u(path)
            pid = _safe_id(path.stem)
            base = pid
            n = 2
            while pid in playlists:
                pid = f"{base}_{n}"
                n += 1
            title = path.stem.replace("-", " ").replace("_", " ").strip() or path.stem
            playlists[pid] = RadioPlaylist(
                id=pid,
                title=title,
                path=str(path),
                stations=stations,
            )
            log.info("radio playlist %s: %d stations", path.name, len(stations))
        self._playlists = playlists
        try:
            self._mtime = self.folder.stat().st_mtime
        except OSError:
            self._mtime = 0.0

    def maybe_reload(self) -> None:
        if not self.reload_on_browse:
            return
        try:
            m = self.folder.stat().st_mtime
        except OSError:
            return
        if m != self._mtime:
            log.info("radio folder changed, reloading")
            self.reload()

    def list_playlists(self) -> List[RadioPlaylist]:
        self.maybe_reload()
        return list(self._playlists.values())

    def get_playlist(self, pid: str) -> Optional[RadioPlaylist]:
        self.maybe_reload()
        return self._playlists.get(pid)

    def popular_active(self) -> bool:
        """Работает ли «Ваше популярное радио»: нужна галка И проксирование радио.

        Без проксирования эмулятор не видит, сколько вы слушаете станцию.
        """
        return self.popular is not None and bool(self.popular_enabled) and bool(self.proxy)

    def popular_stations(self) -> List[Tuple[str, int, RadioStation]]:
        """Станции «Ваше популярное радио»: (id плейлиста, индекс, станция) по убыванию популярности.

        Станция ищется по ссылке в текущих M3U; если её из плейлистов убрали — не показываем.
        """
        pop = self.popular
        if pop is None or not self.popular_active():
            return []
        where: Dict[str, Tuple[str, int, RadioStation]] = {}
        for pl in self.list_playlists():
            for i, st in enumerate(pl.stations):
                where.setdefault(st.url, (pl.id, i, st))
        out: List[Tuple[str, int, RadioStation]] = []
        for row in pop.top():
            hit = where.get(row["url"])
            if hit:
                out.append(hit)
        return out

    def get_station(self, pid: str, index: int) -> Optional[RadioStation]:
        pl = self.get_playlist(pid)
        if not pl or index < 0 or index >= len(pl.stations):
            return None
        return pl.stations[index]

    # ---------- управление станциями через WEB ----------

    def add_station(self, pid: str, name: str, url: str) -> Tuple[bool, str]:
        """Добавить станцию в существующий M3U-плейлист. URL получает .mp3 при необходимости."""
        name = (name or "").strip()
        url = (url or "").strip()
        pid = (pid or "").strip()
        if not pid:
            return False, "Выберите плейлист"
        if not name:
            return False, "Укажите название станции"
        if not url:
            return False, "Укажите ссылку на поток"
        if not (url.startswith("http://") or url.startswith("https://")):
            return False, "Ссылка должна начинаться с http:// или https://"
        if self.auto_mp3_suffix:
            url = _ensure_mp3_suffix(url)
        self.maybe_reload()
        pl = self._playlists.get(pid)
        if not pl:
            return False, "Плейлист не найден"
        for st in pl.stations:
            if st.url == url:
                return False, "Такая ссылка уже есть в этом плейлисте"
        stations = list(pl.stations)
        stations.append(RadioStation(name=name, url=url, playlist=pl.id))
        try:
            write_m3u(Path(pl.path), stations)
        except Exception as e:
            log.warning("write m3u %s: %s", pl.path, e)
            return False, f"Не удалось сохранить: {e}"
        self.reload()
        return True, f"Добавлено «{name}» в «{pl.title}»"

    def remove_station(self, pid: str, index: int) -> Tuple[bool, str]:
        """Удалить станцию из M3U по индексу."""
        self.maybe_reload()
        pl = self._playlists.get(pid)
        if not pl or index < 0 or index >= len(pl.stations):
            return False, "Станция не найдена"
        name = pl.stations[index].name
        stations = [st for i, st in enumerate(pl.stations) if i != index]
        try:
            write_m3u(Path(pl.path), stations)
        except Exception as e:
            log.warning("write m3u %s: %s", pl.path, e)
            return False, f"Не удалось сохранить: {e}"
        self.reload()
        return True, f"Удалено: {name}"

    def edit_station(
        self, pid: str, index: int, name: str, url: str
    ) -> Tuple[bool, str]:
        """Изменить название и/или URL станции в M3U."""
        name = (name or "").strip()
        url = (url or "").strip()
        pid = (pid or "").strip()
        if not pid:
            return False, "Выберите плейлист"
        if not name:
            return False, "Укажите название станции"
        if not url:
            return False, "Укажите ссылку на поток"
        if not (url.startswith("http://") or url.startswith("https://")):
            return False, "Ссылка должна начинаться с http:// или https://"
        if self.auto_mp3_suffix:
            url = _ensure_mp3_suffix(url)
        self.maybe_reload()
        pl = self._playlists.get(pid)
        if not pl or index < 0 or index >= len(pl.stations):
            return False, "Станция не найдена"
        for i, st in enumerate(pl.stations):
            if i != index and st.url == url:
                return False, "Такая ссылка уже есть в этом плейлисте"
        stations = list(pl.stations)
        old = stations[index]
        stations[index] = RadioStation(name=name, url=url, playlist=pl.id, logo=old.logo)
        try:
            write_m3u(Path(pl.path), stations)
        except Exception as e:
            log.warning("write m3u %s: %s", pl.path, e)
            return False, f"Не удалось сохранить: {e}"
        self.reload()
        return True, f"Обновлено: «{old.name}» → «{name}»"

    def move_station(self, pid: str, index: int, new_index: int) -> Tuple[bool, str]:
        """Переместить станцию на новую позицию (0-based). new_index может быть за пределами — прижмём."""
        self.maybe_reload()
        pl = self._playlists.get(pid)
        if not pl or index < 0 or index >= len(pl.stations):
            return False, "Станция не найдена"
        n = len(pl.stations)
        new_index = max(0, min(int(new_index), n - 1))
        if new_index == index:
            return True, "Уже на этой позиции"
        stations = list(pl.stations)
        st = stations.pop(index)
        stations.insert(new_index, st)
        try:
            write_m3u(Path(pl.path), stations)
        except Exception as e:
            log.warning("write m3u %s: %s", pl.path, e)
            return False, f"Не удалось сохранить: {e}"
        self.reload()
        return True, f"«{st.name}» → позиция {new_index + 1}"

    def create_playlist(self, title: str) -> Tuple[bool, str]:
        """Создать новый пустой M3U-файл."""
        title = (title or "").strip()
        if not title:
            return False, "Укажите название плейлиста"
        # безопасное имя файла
        fname = re.sub(r"[^\w\-]+", "-", title, flags=re.UNICODE).strip("-") or "playlist"
        path = self.folder / f"{fname}.m3u"
        if path.exists():
            return False, f"Файл уже существует: {path.name}"
        try:
            write_m3u(path, [])
        except Exception as e:
            return False, str(e)
        self.reload()
        return True, f"Создан плейлист «{title}»"
