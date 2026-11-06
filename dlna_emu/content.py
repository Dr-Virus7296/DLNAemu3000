"""Виртуальное дерево контента для ContentDirectory."""

from __future__ import annotations

import html
import json
import logging
import os
import re
import threading
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

from .deezer_client import DeezerClient
from .drivemusic_client import DriveMusicClient, DmTrack
from .akniga_client import AknigaClient, AkBook
from .files_lib import FilesLibrary, mime_for
from .radio_m3u import RadioLibrary
from .yandex_client import YandexMusicClient
from .kion_client import KionMusicClient
from .promodj_client import PromoDjClient, PdTrack
from .zvuk_client import ZvukClient, ZvTrack
from .zaycev_client import ZaycevClient
from .accounts import AccountPool, MULTI_SERVICES
import contextlib

log = logging.getLogger("dlna_emu.content")

_XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def _esc(s: str) -> str:
    return html.escape(_XML_BAD.sub("", s or ""), quote=True)


from .content_ids import *  # noqa: F401,F403
from .providers import (
    RadioProvider, FilesProvider, DeezerProvider, DriveMusicProvider,
    AknigaProvider, YandexProvider, ZvukProvider, KionProvider, PromoDjProvider,
    ZaycevProvider, NetStorageProvider,
)

class ContentTree:
    def __init__(
        self,
        radio: RadioLibrary,
        deezer: DeezerClient,
        base_url: str,
        files: Optional[FilesLibrary] = None,
        drivemusic: Optional[DriveMusicClient] = None,
        yandex: Optional[YandexMusicClient] = None,
        akniga: Optional[AknigaClient] = None,
        promodj: Optional[PromoDjClient] = None,
        zvuk: Optional[ZvukClient] = None,
        kion: Optional[KionMusicClient] = None,
        zaycev: Optional[ZaycevClient] = None,
        netstorage=None,
        covers_enabled: bool = True,
        covers_dlna_profile: bool = False,
        favorites_path: Optional[str] = None,
    ):
        self.covers_enabled = bool(covers_enabled)
        self.covers_dlna_profile = bool(covers_dlna_profile)
        self.covers_svc = None  # CoverService, выставляется в run_server
        self.radio = radio
        self.deezer = deezer
        self.files = files
        self.netstorage = netstorage  # NetStorageManager (WebDAV/FTP) или None
        self.drivemusic = drivemusic or DriveMusicClient()
        self.yandex = yandex or YandexMusicClient()
        self.akniga = akniga or AknigaClient()
        self.promodj = promodj or PromoDjClient(enabled=False)
        self.zvuk = zvuk or ZvukClient()
        self.kion = kion or KionMusicClient()
        self.zaycev = zaycev or ZaycevClient(enabled=False)
        self.base_url = base_url.rstrip("/")
        # Пулы аккаунтов (если в ContentTree передали обычные клиенты — работаем как раньше)
        self._pools: Dict[str, AccountPool] = {
            svc: getattr(self, svc) for svc in MULTI_SERVICES
            if isinstance(getattr(self, svc, None), AccountPool)
        }
        self._providers = [
            RadioProvider(self),
            FilesProvider(self),
            NetStorageProvider(self),
            DeezerProvider(self),
            DriveMusicProvider(self),
            AknigaProvider(self),
            YandexProvider(self),
            ZvukProvider(self),
            KionProvider(self),
            PromoDjProvider(self),
            ZaycevProvider(self),
        ]
        self._favorites_path = favorites_path or ""

        self._saved: List[dict] = []
        self._saved_lock = threading.Lock()
        self._dm_saved: List[dict] = []
        self._dm_saved_lock = threading.Lock()
        self._ya_saved: List[dict] = []
        self._ya_saved_lock = threading.Lock()
        self._zv_saved: List[dict] = []
        self._kion_saved: List[dict] = []
        self._zv_pl_titles: Dict[str, str] = {}
        self._zv_artist_titles: Dict[str, str] = {}
        self._zv_album_titles: Dict[str, str] = {}
        self._zv_saved_lock = threading.Lock()
        self._kion_saved_lock = threading.Lock()
        self._ak_saved: List[dict] = []
        self._ak_saved_lock = threading.Lock()
        self._ak_favorites: List[dict] = []
        self._ak_favorites_lock = threading.Lock()
        self._pd_saved: List[dict] = []
        self._pd_saved_lock = threading.Lock()
        self._zy_saved: List[dict] = []
        self._zy_saved_lock = threading.Lock()
        self._system_update_id = 1
        self._load_ak_favorites()
        # избранное остальных площадок (артисты/альбомы/плейлисты), до 500 на площадку
        self._site_favs: Dict[str, List[dict]] = {}
        self._site_favs_lock = threading.Lock()
        _fdir = os.path.dirname(self._favorites_path) if self._favorites_path else ""
        self._site_favs_path = os.path.join(_fdir, "favorites_sites.json") if self._favorites_path else ""
        self._search_hist_path = os.path.join(_fdir, "search_history.json") if self._favorites_path else ""
        self.search_cfg_fn = None          # () -> {"persist": bool, "history_show": int}
        self._load_site_favorites()

        # История подборок (текущий плейлист): service -> list of folders
        # folder: {id, title, tracks: [...], start_index, updated}
        # Максимум CURRENT_FOLDERS_LIMIT папок на площадку; треков в папке — без лимита.
        self._current: Dict[str, list] = {}
        self._current_lock = threading.RLock()
        self._request_tls = threading.local()  # client_ip per request
        # Последний просмотренный список треков (только RAM) — чтобы при play
        # сохранить подборку. Browse НЕ пишет на диск и НЕ меняет UpdateID.
        self._last_browsed: Dict[str, dict] = {}
        # Замороженный список треков для «Текущий плейлист»:
        # key = "service:fid" -> list[dict] (отрезок с текущей позиции).
        # При первом Browse берём tracks[start_index:], дальше список
        # постепенно укорачивается спереди при advance_current (прослушанные
        # треки исчезают). Сброс кэша — только при входе в список папок
        # service:current (выбор другого плейлиста).
        self._frozen_current: Dict[str, list] = {}
        self._frozen_current_ts: Dict[str, float] = {}
        # Когда последний раз стартовал трек из папки: "{key}:{fid}" -> ts (только RAM).
        # Пока папка «играет», её снимок ресиверу не пересобираем.
        self._last_play: Dict[str, float] = {}
        self._playing_fid: Dict[str, str] = {}   # key -> fid папки, из которой играет трек
        # Какая папка «Текущего плейлиста» сейчас играет: key(service@ip) -> fid.
        # Нужна, когда один и тот же набор треков лежит в нескольких папках.
        self._active_fid: Dict[str, str] = {}
        # Кэш названий плейлистов Яндекса: pid -> title (чтобы не показывать ID)
        self._ya_pl_titles: Dict[str, str] = {}
        self._kion_pl_titles: Dict[str, str] = {}
        self._ya_wave_last_playing: Optional[str] = None
        self._ya_wave_totals: Dict[str, int] = {}
        # Каталог для per-service файлов current_<service>.json
        # (раньше был один огромный current_playlists.json — тормозил I/O)
        if favorites_path:
            self._current_dir = os.path.dirname(favorites_path) or "."
        else:
            self._current_dir = "."
        self._current_legacy_path = os.path.join(self._current_dir, "current_playlists.json")
        # Настройки (веб-панель → «Текущие плейлисты»), применяются через configure_current()
        self.current_max_folders: int = self.CURRENT_FOLDERS_LIMIT   # сколько папок помнить
        self.current_skip_artist_repeats: int = 6       # >= N треков одного исполнителя → не сохранять (0 = выкл.)
        self.current_skip_same_album: bool = True       # у всех треков один ALBUM → не сохранять
        self._skip_logged: set = set()
        self._check_current_dir_writable()
        try:
            self._load_current_playlists()
        except Exception as e:
            log.warning("load current_playlists failed: %s", e)

    CURRENT_FOLDERS_LIMIT = 100

    _FALLBACK_TITLE_RE = re.compile(r"^Плейлист(?:\s+\d+)?$")
    SAME_PLAYLIST_JACCARD = 0.8   # доля общих треков, при которой это один плейлист

    @classmethod
    def _is_fallback_title(cls, title: str) -> bool:
        return bool(cls._FALLBACK_TITLE_RE.match((title or "").strip()))

    @classmethod
    def _same_playlist(cls, title_a: str, oids_a: list, title_b: str, oids_b: list) -> bool:
        """Это один и тот же плейлист (даже если назван по-разному или изменился)?

        1) тот же набор треков в том же порядке;
        2) одинаковое «настоящее» название (запасное «Плейлист NNN» не считается);
        3) почти тот же состав (≥80% общих треков) — плейлист обновился.
        """
        if not oids_a or not oids_b:
            return False
        if oids_a == oids_b:
            return True
        ta, tb = (title_a or "").strip(), (title_b or "").strip()
        if ta and ta == tb and not cls._is_fallback_title(ta):
            return True
        sa, sb = set(oids_a), set(oids_b)
        if min(len(sa), len(sb)) >= 5:
            inter = len(sa & sb)
            union = len(sa | sb)
            if union and inter / union >= cls.SAME_PLAYLIST_JACCARD:
                return True
        return False

    @classmethod
    def _merge_into(cls, keep: dict, other: dict) -> None:
        """Влить дубль other в keep: берём более свежий (по updated) вариант целиком."""
        newer = other if (other.get("updated") or 0) > (keep.get("updated") or 0) else keep
        older = keep if newer is other else other
        title = newer.get("title") or ""
        if cls._is_fallback_title(title) and not cls._is_fallback_title(older.get("title") or ""):
            title = older.get("title") or title
        keep["tracks"] = list(newer.get("tracks") or [])
        keep["start_index"] = int(newer.get("start_index") or 0)
        keep["updated"] = max(other.get("updated") or 0, keep.get("updated") or 0)
        keep["title"] = title

    @classmethod
    def _dedupe_folders(cls, folders: list) -> list:
        """Склеить папки «Текущего плейлиста», которые на деле один плейлист.

        Остаётся папка, стоящая выше всех; позиция и состав — от более свежей копии,
        название — «нормальное», а не запасное «Плейлист NNN».
        """
        out: list = []
        for f in folders or []:
            oids = [t.get("oid") for t in (f.get("tracks") or [])]
            if not oids:
                out.append(f)
                continue
            prev = None
            for g in out:
                g_oids = [t.get("oid") for t in (g.get("tracks") or [])]
                if cls._same_playlist(g.get("title"), g_oids, f.get("title"), oids):
                    prev = g
                    break
            if prev is None:
                out.append(f)
                continue
            log.info("current: склеиваю дубль «%s» → «%s»", f.get("title"), prev.get("title"))
            cls._merge_into(prev, f)
        return out

    # ---------- мультиаккаунты ----------

    _ACC_RE = re.compile(r"^(deezer|yandex|zvuk|kion):a(\d+)(?::(.*))?$", re.S)
    _ACC_QUAL_RE = re.compile(r"^a\d+(?:/|:|$)")

    def _acc_pool(self, service: str) -> Optional[AccountPool]:
        pool = self._pools.get(service)
        return pool if (pool is not None and pool.multi) else None

    def _acc_enter(self, object_id: str):
        """`deezer:a1:mine` → (контекст-менеджер с активным 2-м аккаунтом, `deezer:mine`).

        Для обычных id (и когда аккаунт один) возвращает (nullcontext, id) без изменений.
        Если номер аккаунта неверный — (None, id): вызывающий вернёт пустой список.
        """
        m = self._ACC_RE.match(object_id or "")
        if not m:
            return contextlib.nullcontext(), object_id
        svc, idx, rest = m.group(1), int(m.group(2)), m.group(3)
        pool = self._acc_pool(svc)
        if pool is None or idx >= pool.count:
            return None, object_id
        stripped = f"{svc}:{rest}" if rest else svc
        return pool.use(idx), stripped

    def _acc_active(self, service: str) -> Optional[int]:
        """Индекс активного аккаунта сервиса, если мы внутри `use()` и аккаунтов > 1."""
        pool = self._acc_pool(service)
        if pool is not None and pool.scoped:
            return pool.index
        return None

    def _acc_id(self, oid: str) -> str:
        """Добавить `:aN` к id внутри контекста аккаунта: deezer:tr:1 → deezer:a1:tr:1."""
        if not oid or ":" not in oid and oid not in self._pools:
            return oid
        svc, _, rest = oid.partition(":")
        idx = self._acc_active(svc)
        if idx is None or self._ACC_QUAL_RE.match(rest):
            return oid
        return f"{svc}:a{idx}" + (f":{rest}" if rest else "")

    def _acc_url(self, url: str) -> str:
        """.../stream/deezer/123 → .../stream/deezer/a1/123 внутри контекста аккаунта."""
        prefix = f"{self.base_url}/stream/"
        if not url.startswith(prefix):
            return url
        rest = url[len(prefix):]
        svc, _, tail = rest.partition("/")
        idx = self._acc_active(svc)
        if idx is None or self._ACC_QUAL_RE.match(tail):
            return url
        return f"{prefix}{svc}/a{idx}/{tail}"

    def account_scope(self, service: str, idx: int):
        """Контекст «аккаунт idx сервиса service» для стрим-обработчиков."""
        pool = self._pools.get(service)
        if pool is None or not pool.multi:
            return contextlib.nullcontext()
        return pool.use(idx)

    def _svc_key(self, service: str) -> str:
        """Ключ истории «Текущий плейлист»: у 2-го и следующих аккаунтов — deezer.a1 и т.д."""
        idx = self._acc_active(service)
        if idx:
            return f"{service}.a{idx}"
        return service

    def _account_list(self, service: str, title_count: int = 3) -> List[str]:
        pool = self._pools[service]
        names = pool.names()
        return [
            self._container(f"{service}:a{i}", service, names[i] if i < len(names) else f"Аккаунт {i + 1}", title_count)
            for i in range(pool.count)
        ]

    def _current_file(self, service: str) -> str:
        """Путь к current_<service>.json для одной площадки.

        Ключ вида deezer@192.168.1.50 сохраняем как current_deezer@192.168.1.50.json,
        чтобы при загрузке svc совпал с _current_key (не превращать @ и . в _).
        """
        raw = (service or "unknown").strip().lower() or "unknown"
        # оставляем буквы/цифры/_.-@  (IP и service@ip)
        safe = re.sub(r"[^\w\-@.]+", "_", raw, flags=re.UNICODE)[:96] or "unknown"
        return os.path.join(self._current_dir, f"current_{safe}.json")

    def _load_current_playlists(self) -> None:
        """Загрузить историю по площадкам: current_deezer.json, current_yandex.json, …

        Если есть старый общий current_playlists.json — мигрируем и разбиваем.
        """
        fixed: Dict[str, list] = {}

        # 1) миграция со старого монолита
        legacy = self._current_legacy_path
        if legacy and os.path.isfile(legacy):
            try:
                with open(legacy, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    for svc, val in data.items():
                        if isinstance(val, list):
                            fixed[svc] = val
                        elif isinstance(val, dict) and val.get("tracks"):
                            fixed[svc] = [{
                                "id": "legacy",
                                "title": val.get("title") or "Плейлист",
                                "tracks": val.get("tracks") or [],
                                "start_index": int(val.get("index") or 0),
                                "updated": val.get("updated") or time.time(),
                            }]
                # разнести по файлам и убрать монолит
                for svc, folders in fixed.items():
                    try:
                        with open(self._current_file(svc), "w", encoding="utf-8") as f:
                            json.dump(folders, f, ensure_ascii=False, indent=0)
                    except Exception as e:
                        log.warning("migrate current_%s: %s", svc, e)
                try:
                    os.replace(legacy, legacy + ".migrated")
                    log.info("current_playlists.json → per-service files (legacy renamed .migrated)")
                except Exception:
                    pass
            except Exception as e:
                log.warning("load legacy current_playlists: %s", e)

        # 2) читать current_*.json
        try:
            if os.path.isdir(self._current_dir):
                def _mtime(n: str) -> float:
                    try:
                        return os.path.getmtime(os.path.join(self._current_dir, n))
                    except OSError:
                        return 0.0
                # Если на один ключ есть два файла (current_yandex_192_168_1_64.json и
                # current_yandex@192.168.1.64.json) — читаем от старого к новому, чтобы
                # побеждал тот, что записан позже, а не «кто случайно последний в listdir».
                for name in sorted(os.listdir(self._current_dir), key=_mtime):
                    if not (name.startswith("current_") and name.endswith(".json")):
                        continue
                    if name == "current_playlists.json":
                        continue
                    svc = name[len("current_"):-len(".json")]
                    if not svc or svc == "radio":
                        # История «Текущий плейлист» для радио отключена
                        continue
                    # Миграция старых имён current_deezer_192_168_1_50 → deezer@192.168.1.50
                    m_ip = re.match(
                        r"^([a-z0-9]+)_(\d{1,3})_(\d{1,3})_(\d{1,3})_(\d{1,3})$",
                        svc,
                    )
                    if m_ip:
                        svc = f"{m_ip.group(1)}@{m_ip.group(2)}.{m_ip.group(3)}.{m_ip.group(4)}.{m_ip.group(5)}"
                    path = os.path.join(self._current_dir, name)
                    try:
                        with open(path, "r", encoding="utf-8") as f:
                            val = json.load(f)
                        if isinstance(val, list):
                            fixed[svc] = val
                        elif isinstance(val, dict) and val.get("tracks"):
                            fixed[svc] = [{
                                "id": "legacy",
                                "title": val.get("title") or "Плейлист",
                                "tracks": val.get("tracks") or [],
                                "start_index": int(val.get("index") or 0),
                                "updated": val.get("updated") or time.time(),
                            }]
                    except Exception as e:
                        log.warning("load %s: %s", name, e)
        except Exception as e:
            log.warning("scan current_*.json: %s", e)

        for k in list(fixed.keys()):
            try:
                fixed[k] = self._dedupe_folders(fixed[k])
            except Exception as e:
                log.warning("dedupe current folders %s: %s", k, e)
        with self._current_lock:
            self._current = fixed

    def _save_current_playlists(self, service: Optional[str] = None) -> None:
        """Сохранить только указанную площадку (или все, если service=None)."""
        try:
            with self._current_lock:
                if service:
                    items = {service: list(self._current.get(service) or [])}
                else:
                    items = {k: list(v) for k, v in self._current.items()}
            for svc, folders in items.items():
                path = self._current_file(svc)
                self._write_json_file(path, folders)
                self._remove_legacy_file(svc)
        except Exception as e:
            log.warning(
                "save current playlists: %s — «Текущий плейлист» не сохранится после "
                "перезапуска (проверьте права на папку %s)", e, self._current_dir,
            )

    def _remove_legacy_file(self, key: str) -> None:
        """Удалить устаревший файл current_<svc>_<a>_<b>_<c>_<d>.json того же ключа."""
        m = re.match(r"^([a-z0-9]+)@(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$", key or "")
        if not m:
            return
        old = os.path.join(self._current_dir, "current_" + "_".join(m.groups()) + ".json")
        if old != self._current_file(key) and os.path.exists(old):
            try:
                os.remove(old)
                log.info("current: удалён устаревший файл %s", os.path.basename(old))
            except OSError as e:
                log.warning("remove legacy %s: %s", old, e)

    @staticmethod
    def _write_json_file(path: str, data) -> None:
        """Записать JSON надёжно.

        Сначала временный файл + os.replace: не остаётся обрезанного файла при сбое
        питания, и запись проходит даже если сам файл «чужой» (создан другим
        пользователем, права 644), лишь бы папка была доступна на запись.
        Новому файлу ставим права 666 (на ФС без chmod — USB NTFS/FAT — это просто
        игнорируется). Запасной вариант — запись «на месте».
        """
        payload = json.dumps(data, ensure_ascii=False, indent=0)
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
            try:
                os.chmod(tmp, 0o666)
            except OSError:
                pass
            os.replace(tmp, path)
            return
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
        with open(path, "w", encoding="utf-8") as f:
            f.write(payload)

    def _check_current_dir_writable(self) -> None:
        """При старте сразу сказать в логе, если историю «Текущий плейлист» некуда писать."""
        probe = os.path.join(self._current_dir, ".write_test")
        try:
            os.makedirs(self._current_dir, exist_ok=True)
            with open(probe, "w", encoding="utf-8") as f:
                f.write("1")
            os.remove(probe)
        except OSError as e:
            log.error(
                "Папка %s недоступна для записи (%s): «Текущий плейлист» не будет "
                "сохраняться между перезапусками. Дайте пользователю, под которым "
                "запущен эмулятор, права на эту папку.", os.path.abspath(self._current_dir), e,
            )
            return
        try:
            bad = [
                n for n in os.listdir(self._current_dir)
                if n.startswith("current_") and n.endswith(".json")
                and not os.access(os.path.join(self._current_dir, n), os.W_OK)
            ]
            if bad:
                log.warning(
                    "Файлы истории без прав записи для текущего пользователя: %s — "
                    "будут перезаписаны через временный файл", ", ".join(bad[:5]),
                )
        except OSError:
            pass

    def _folder_id(self, title: str, tracks: List[dict]) -> str:
        """Стабильный id папки по первому треку + названию."""
        first = (tracks[0].get("oid") if tracks else "") or ""
        raw = f"{title}|{first}"
        return re.sub(r"[^\w\-]+", "_", raw, flags=re.UNICODE)[:64] or "pl"


    def set_request_client(self, client_ip: str = "") -> None:
        """IP ресивера для текущего HTTP/UPnP-запроса (Browse/Search/stream)."""
        self._request_tls.client_ip = (client_ip or "").strip()

    def _request_client_ip(self) -> str:
        return getattr(self._request_tls, "client_ip", "") or ""

    def _current_key(self, service: str, client_ip: Optional[str] = None) -> str:
        """Ключ истории: service@ip — чтобы два ресивера не мешали start_index."""
        svc = self._svc_key((service or "").strip()) or "unknown"
        ip = (client_ip if client_ip is not None else self._request_client_ip()).strip()
        if ip:
            return f"{svc}@{ip}"
        return svc

    def _current_keys_for_service(self, service: str) -> List[str]:
        """Все ключи _current, относящиеся к площадке (с IP и без)."""
        svc = (service or "").strip()
        keys = []
        with self._current_lock:
            for k in self._current.keys():
                if k == svc or k.startswith(svc + "@"):
                    keys.append(k)
        return keys

    def set_current_playlist(self, service: str, title: str, tracks: List[dict]) -> None:
        """Запомнить последний просмотренный список (только RAM, без диска и UpdateID).

        Вызывается из browse. На диск пишем только при play (advance_current).
        Ключ service@IP — разные ресиверы не перебивают друг друга.
        """
        if not service or not tracks or service == "radio":
            return
        try:
            key = self._current_key(service)
            with self._current_lock:
                self._last_browsed[key] = {
                    "title": title or "Плейлист",
                    "tracks": tracks,
                }
        except Exception as e:
            log.debug("set_current_playlist (browse cache): %s", e)

    # ---------- настройки «Текущего плейлиста» ----------

    def configure_current(
        self,
        max_folders: Optional[int] = None,
        skip_artist_repeats: Optional[int] = None,
        skip_same_album: Optional[bool] = None,
    ) -> int:
        """Применить настройки. Возвращает, сколько папок убрано из-за нового лимита."""
        if max_folders is not None:
            try:
                self.current_max_folders = max(1, min(int(max_folders), 1000))
            except (TypeError, ValueError):
                pass
        if skip_artist_repeats is not None:
            try:
                self.current_skip_artist_repeats = max(0, min(int(skip_artist_repeats), 1000))
            except (TypeError, ValueError):
                pass
        if skip_same_album is not None:
            self.current_skip_same_album = bool(skip_same_album)
        return self._enforce_current_limit()

    def _forget_folder_state(self, key: str, fid: str) -> None:
        """Забыть всё RAM-состояние папки (снимок, «играет», активная). Под _current_lock."""
        for fk in [k for k in self._frozen_current if k == f"{key}:{fid}"]:
            self._frozen_current.pop(fk, None)
            self._frozen_current_ts.pop(fk, None)
        self._last_play.pop(f"{key}:{fid}", None)
        if self._playing_fid.get(key) == fid:
            self._playing_fid.pop(key, None)
        if self._active_fid.get(key) == fid:
            self._active_fid.pop(key, None)

    def _enforce_current_limit(self) -> int:
        """Оставить в каждой истории не больше current_max_folders самых свежих папок."""
        limit = int(self.current_max_folders)
        removed = 0
        touched: List[str] = []
        with self._current_lock:
            for key, folders in list(self._current.items()):
                if len(folders) > limit:
                    for f in folders[limit:]:
                        self._forget_folder_state(key, f.get("id") or "")
                    removed += len(folders) - limit
                    self._current[key] = folders[:limit]
                    touched.append(key)
        for key in touched:
            self._save_current_playlists(key)
        if removed:
            self.bump_update_id()
            log.info("current: лимит %d папок — удалено старых: %d", limit, removed)
        return removed

    def _current_skip_reason(self, tracks: List[dict]) -> str:
        """Причина, по которой список НЕ надо запоминать в «Текущем плейлисте» ('' — запоминать).

        1) один исполнитель встречается >= current_skip_artist_repeats раз (страница артиста,
           «лучшее у исполнителя» и т.п.);
        2) у всех треков одинаковый ALBUM (это альбом целиком). Если альбома в тегах нет
           (хотя бы у одного трека) — список сохраняется.
        """
        if not tracks:
            return ""
        n = int(self.current_skip_artist_repeats or 0)
        if n > 0:
            counts: Dict[str, int] = {}
            shown: Dict[str, str] = {}
            for t in tracks:
                a = (t.get("artist") or "").strip()
                if a:
                    k = a.casefold()
                    counts[k] = counts.get(k, 0) + 1
                    shown.setdefault(k, a)
            if counts:
                k, c = max(counts.items(), key=lambda kv: kv[1])
                if c >= n:
                    return f"исполнитель «{shown[k]}» встречается {c} раз (порог {n})"
        if self.current_skip_same_album and len(tracks) >= 2:
            albums = [(t.get("album") or "").strip().casefold() for t in tracks]
            if all(albums) and len(set(albums)) == 1:
                return f"у всех треков один альбом «{(tracks[0].get('album') or '').strip()}»"
        return ""

    # ---------- управление из веб-панели ----------

    def current_overview(self) -> List[dict]:
        """Все сохранённые «Текущие плейлисты» (все площадки и ресиверы) для веб-панели."""
        rows: List[dict] = []
        with self._current_lock:
            for key, folders in self._current.items():
                svc, _, ip = key.partition("@")
                for f in folders:
                    rows.append({
                        "key": key, "service": svc, "ip": ip,
                        "id": f.get("id") or "", "title": f.get("title") or "Плейлист",
                        "tracks": len(f.get("tracks") or []),
                        "start": int(f.get("start_index") or 0),
                        "updated": float(f.get("updated") or 0),
                    })
        return rows

    def delete_current_folders(self, items: List[Tuple[str, str]]) -> int:
        """Удалить выбранные папки: items = [(key, folder_id), ...]. Возвращает число удалённых."""
        removed = 0
        touched: List[str] = []
        trash: List[Tuple[str, dict, int]] = []
        with self._current_lock:
            for key, fid in items:
                folders = self._current.get(key)
                if not folders:
                    continue
                kept = [f for f in folders if f.get("id") != fid]
                if len(kept) == len(folders):
                    continue
                for pos, f in enumerate(folders):
                    if f.get("id") == fid:
                        trash.append((key, f, pos))
                removed += len(folders) - len(kept)
                self._forget_folder_state(key, fid)
                if kept:
                    self._current[key] = kept
                else:
                    self._current.pop(key, None)
                if key not in touched:
                    touched.append(key)
        for key in touched:
            self._save_current_playlists(key)
            with self._current_lock:
                still = key in self._current
            if not still:
                for path in (self._current_file(key),):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                self._remove_legacy_file(key)
        if removed:
            self._last_deleted = trash
            self.bump_update_id()
            log.info("current: удалено вручную папок: %d", removed)
        return removed

    def deleted_backup_count(self) -> int:
        """Сколько папок можно вернуть после последнего ручного удаления."""
        return len(getattr(self, "_last_deleted", None) or [])

    def restore_last_deleted(self) -> int:
        """Вернуть папки, удалённые последним ручным удалением (одноуровневая «корзина»)."""
        trash = list(getattr(self, "_last_deleted", None) or [])
        if not trash:
            return 0
        restored = 0
        touched: List[str] = []
        with self._current_lock:
            for key, folder, pos in sorted(trash, key=lambda x: x[2]):
                folders = list(self._current.get(key) or [])
                if any(f.get("id") == folder.get("id") for f in folders):
                    continue
                folders.insert(min(pos, len(folders)), folder)
                self._current[key] = folders
                restored += 1
                if key not in touched:
                    touched.append(key)
            self._last_deleted = []
        for key in touched:
            self._save_current_playlists(key)
        if restored:
            self.bump_update_id()
            log.info("current: возвращено папок: %d", restored)
        return restored

    def _promote_browsed(self, service: str, track_oid: str, client_ip: str = "") -> Optional[str]:
        """При play: если трек из последнего browse — сохранить подборку в историю.

        Возвращает "skip", если играют из списка, который по правилам не запоминаем.
        """
        service = self._svc_key(service)
        key = self._current_key(service, client_ip or None)
        with self._current_lock:
            browsed = self._last_browsed.get(key) or self._last_browsed.get(service)
            if not browsed:
                return
            tracks = browsed.get("tracks") or []
            if not any(t.get("oid") == track_oid for t in tracks):
                return
            title = browsed.get("title") or "Плейлист"
            fid = self._folder_id(title, tracks)
            folders = list(self._current.get(key) or [])
            # найти start_index
            start = 0
            for i, t in enumerate(tracks):
                if t.get("oid") == track_oid:
                    start = i
                    break
            new_oids = [t.get("oid") for t in tracks]
            # Все папки, которые на деле тот же плейлист (то же id, тот же состав,
            # то же название или почти тот же состав) — склеиваем в одну.
            matches = [
                f for f in folders
                if f.get("id") == fid or self._same_playlist(
                    f.get("title"), [t.get("oid") for t in (f.get("tracks") or [])],
                    title, new_oids)
            ]
            if not matches:
                reason = self._current_skip_reason(tracks)
                if reason:
                    mark = (key, fid)
                    if mark not in self._skip_logged:
                        self._skip_logged.add(mark)
                        if len(self._skip_logged) > 500:
                            self._skip_logged.clear()
                        log.info("current: «%s» не сохраняю — %s", title, reason)
                    return "skip"
            if len(matches) == 1:
                ex = matches[0]
                if ([t.get("oid") for t in (ex.get("tracks") or [])] == new_oids
                        and int(ex.get("start_index") or 0) == start
                        and not (self._is_fallback_title(ex.get("title") or "")
                                 and not self._is_fallback_title(title))):
                    # ничего не изменилось — только наверх
                    self._current[key] = [ex] + [f for f in folders if f is not ex]
                    self._active_fid[key] = ex.get("id") or fid
                    return
            keep_id, keep_title = fid, title
            if matches:
                keep_id = matches[0].get("id") or fid
                if self._is_fallback_title(title):
                    for m in matches:
                        if not self._is_fallback_title(m.get("title") or ""):
                            keep_title = m.get("title")
                            break
                if len(matches) > 1:
                    log.info("current: склеено дублей: %d → «%s»", len(matches), keep_title)
            entry = {
                "id": keep_id,
                "title": keep_title,
                "tracks": tracks,
                "start_index": start,
                "updated": time.time(),
                "client_ip": (client_ip or self._request_client_ip() or ""),
            }
            # метку «с какого трека заморожен список» переносим в новую запись
            for m in matches:
                if m.get("snap_start") is not None:
                    entry["snap_start"] = m["snap_start"]
                    break
            folders = [entry] + [f for f in folders if not any(f is m for m in matches)]
            for dropped in folders[self.current_max_folders:]:
                self._forget_folder_state(key, dropped.get("id") or "")
            folders = folders[: self.current_max_folders]
            self._current[key] = folders
            self._active_fid[key] = keep_id
        self._save_current_playlists(key)
        return None
        # не bump'аем UpdateID на каждый трек — иначе ресивер дёргается

    # Список «Текущего плейлиста» НЕ подрезается по времени и сам по себе. Играющая папка
    # держит замороженный снимок (с трека, с которого начали слушать), а его начало
    # запоминается на диске в folder["snap_start"] — поэтому и после выключения ресивера,
    # и после перезапуска эмулятора список остаётся прежним. Подрезается он только когда
    # у этого ресивера заиграло что-то другое (другой плейлист, другая площадка, радио,
    # файлы) — см. _release_key().

    def _release_key(self, key: str, except_fid: str = "") -> bool:
        """Снять «заморозку» со всех папок истории key, кроме except_fid.

        Вызывать под _current_lock (он реентерабельный). Возвращает True, если на диске
        надо обновить метку snap_start."""
        changed = False
        for f in (self._current.get(key) or []):
            fid = f.get("id") or ""
            if fid == except_fid:
                continue
            if f.get("snap_start") is not None:
                f.pop("snap_start", None)
                changed = True
            self._frozen_current.pop(f"{key}:{fid}", None)
            self._frozen_current_ts.pop(f"{key}:{fid}", None)
            self._last_play.pop(f"{key}:{fid}", None)
        pf = self._playing_fid.get(key)
        if pf and pf != except_fid:
            self._playing_fid.pop(key, None)
        return changed

    def _release_playing_if_foreign(self, service: str, key: str, track_oid: str,
                                    client_ip: str = "") -> None:
        """Играет трек, которого нет в играющей папке (другой список, не попавший в
        «Текущий плейлист»: альбом целиком, страница артиста, поиск, избранное…).
        Значит, прежняя папка больше не играет — её список при возврате подрежется."""
        changed = False
        with self._current_lock:
            folders = self._current.get(key) or self._current.get(service) or []
            for f in folders:
                if f.get("snap_start") is None and self._playing_fid.get(key) != f.get("id"):
                    continue
                if any(t.get("oid") == track_oid for t in (f.get("tracks") or [])):
                    return      # это трек из играющей папки — она продолжает играть
            changed = self._release_key(key)
        if changed:
            self._save_current_playlists(key)

    def release_playing(self, client_ip: str = "", except_service: str = "") -> int:
        """Ресивер client_ip начал играть что-то НЕ из «Текущего плейлиста» этой площадки
        (радио, файлы, другая площадка…): играющие папки этого ресивера перестают быть
        замороженными, при возврате в них список покажется подрезанным.

        except_service — площадка, чей трек стартовал сейчас (её папки обрабатывает
        advance_current по принадлежности трека к папке). Возвращает число сброшенных."""
        ip = (client_ip or self._request_client_ip() or "").strip()
        if not ip:
            return 0
        skip_svc = self._svc_key(except_service) if except_service else ""
        n = 0
        touched: List[str] = []
        with self._current_lock:
            keys = set(self._current.keys()) | set(self._playing_fid.keys())
            for key in keys:
                svc, _, kip = key.partition("@")
                if kip != ip or (skip_svc and svc == skip_svc):
                    continue
                active = bool(self._playing_fid.get(key)) or any(
                    f.get("snap_start") is not None for f in (self._current.get(key) or []))
                if not active:
                    continue
                if self._release_key(key):
                    touched.append(key)
                n += 1
        for key in touched:
            self._save_current_playlists(key)
        if n:
            log.debug("current: %s играет вне «Текущего плейлиста» — сброшено: %d", ip, n)
        return n

    def advance_current(self, service: str, track_oid: str, client_ip: str = "") -> None:
        """При воспроизведении — сохранить/обновить подборку и позицию.

        client_ip: IP ресивера (из stream-запроса). Без него — thread-local IP.
        Два ресивера на одной площадке не делят start_index.
        """
        if not service or not track_oid or service == "radio":
            return
        try:
            if client_ip:
                self.set_request_client(client_ip)
            # заиграла другая площадка → играющие папки остальных площадок больше не играют
            self.release_playing(client_ip, except_service=service)
            service = self._svc_key(service)
            key = self._current_key(service, client_ip or None)
            if self._promote_browsed(service, track_oid, client_ip=client_ip) == "skip":
                # список не из «запоминаемых» (правила в веб-панели) — но играет уже ОН,
                # а не прежняя папка: сбрасываем её снимок, чтобы при возврате она подрезалась
                self._release_playing_if_foreign(service, key, track_oid, client_ip)
                return
            need_save = False
            with self._current_lock:
                folders = self._current.get(key) or self._current.get(service) or []
                cands = [
                    f for f in folders
                    if any(t.get("oid") == track_oid for t in (f.get("tracks") or []))
                ]
                folder = None
                if cands:
                    active = self._active_fid.get(key)
                    folder = next((f for f in cands if f.get("id") == active), None) or cands[0]
                if folder is not None:
                    tracks = folder.get("tracks") or []
                    for i, t in enumerate(tracks):
                        if t.get("oid") == track_oid:
                            if int(folder.get("start_index") or 0) != i:
                                folder["start_index"] = i
                                folder["updated"] = time.time()
                                need_save = True
                            break
                    new_fid = folder.get("id") or ""
                    prev_fid = self._playing_fid.get(key)
                    # включили другой плейлист — прежние папки больше не держим замороженными
                    # (в т.ч. после перезапуска, когда _playing_fid пуст): список подрежется
                    if self._release_key(key, except_fid=new_fid):
                        need_save = True
                    # Плейлист начал играть: фиксируем снимок для ресивера СЕЙЧАС (с этого
                    # трека). Дальше эмулятор двигает позицию у себя, а ресиверу отдаёт этот
                    # снимок, пока не включат другой плейлист (по времени он не сбрасывается).
                    ek2 = self._svc_key(service)
                    ip2 = client_ip or self._request_client_ip()
                    fk_new = f"{ek2}@{ip2}:{new_fid}" if ip2 else f"{ek2}:{new_fid}"
                    if fk_new not in self._frozen_current:
                        snap = folder.get("snap_start")
                        cur_i = int(snap) if snap is not None else int(folder.get("start_index") or 0)
                        all_tr = list(folder.get("tracks") or [])
                        self._frozen_current[fk_new] = all_tr[cur_i:] if cur_i > 0 else all_tr
                        self._frozen_current_ts[fk_new] = time.time()
                        if snap is None:
                            folder["snap_start"] = cur_i      # на диск: переживёт перезапуск
                            need_save = True
                    self._playing_fid[key] = new_fid
                    self._last_play[f"{key}:{new_fid}"] = time.time()
                    self._active_fid[key] = new_fid
                    folders = [folder] + [f for f in folders if f is not folder]
                    self._current[key] = folders
                else:
                    # трека нет ни в одной папке (поиск, избранное, чужой список)
                    self._release_playing_if_foreign(service, key, track_oid, client_ip)
            if need_save:
                self._save_current_playlists(key)
        except Exception as e:
            log.debug("advance_current: %s", e)

    def clear_current_playlist(self, service: str) -> int:
        """Очистить «Текущий плейлист» площадки (все ресиверы): RAM + файлы current_*.json.

        Возвращает число удалённых папок. После очистки bump UpdateID — ресивер перечитает."""
        svc = (service or "").strip()
        if not svc:
            return 0
        removed = 0
        keys: List[str] = []
        with self._current_lock:
            for k in list(self._current.keys()):
                if k == svc or k.startswith(svc + "@") or k.startswith(svc + ".a"):
                    removed += len(self._current.get(k) or [])
                    keys.append(k)
                    del self._current[k]
            for k in list(self._last_browsed.keys()):
                if k == svc or k.startswith(svc + "@") or k.startswith(svc + ".a"):
                    del self._last_browsed[k]
            for k in list(self._frozen_current.keys()):
                if k.startswith((f"{svc}:", f"{svc}@", f"{svc}.a")):
                    del self._frozen_current[k]
        # файлы на диске (включая те, что не успели загрузиться в RAM)
        try:
            if os.path.isdir(self._current_dir):
                base = self._current_file(svc)
                prefix = os.path.basename(base)[:-5]          # current_<svc>
                for name in os.listdir(self._current_dir):
                    if not name.endswith(".json"):
                        continue
                    stem = name[:-5]
                    if stem == prefix or stem.startswith((prefix + "@", prefix + ".a")):
                        try:
                            os.remove(os.path.join(self._current_dir, name))
                        except OSError as e:
                            log.warning("clear current: remove %s: %s", name, e)
        except Exception as e:
            log.warning("clear current files %s: %s", svc, e)
        self.bump_update_id()
        log.info("current playlist cleared: %s (%d папок)", svc, removed)
        return removed

    def prune_current_playlists(self, max_idle_days: float) -> int:
        """Удалить папки «Текущий плейлист», не игравшие max_idle_days дней.

        Ориентир — поле updated (время последнего play через advance_current).
        max_idle_days <= 0 — ничего не делать. Возвращает число удалённых папок.
        """
        try:
            days = float(max_idle_days)
        except (TypeError, ValueError):
            return 0
        if days <= 0:
            return 0
        cutoff = time.time() - days * 86400.0
        removed = 0
        touched: List[str] = []
        with self._current_lock:
            for key in list(self._current.keys()):
                folders = list(self._current.get(key) or [])
                kept = []
                for f in folders:
                    upd = float(f.get("updated") or 0)
                    if upd >= cutoff:
                        kept.append(f)
                    else:
                        removed += 1
                        fid = f.get("id") or ""
                        # замороженные отрезки этой папки
                        for fk in list(self._frozen_current.keys()):
                            if fid and (fk.endswith(":" + fid) or f":{fid}" in fk):
                                self._frozen_current.pop(fk, None)
                if len(kept) != len(folders):
                    touched.append(key)
                    if kept:
                        self._current[key] = kept
                    else:
                        self._current.pop(key, None)
        for key in touched:
            try:
                self._save_current_playlists(key)
            except Exception as e:
                log.warning("prune current save %s: %s", key, e)
        if removed:
            try:
                self.bump_update_id()
            except Exception:
                pass
            log.info(
                "current playlists: удалено %d папок (не играли > %.0f дн.)",
                removed, days,
            )
        return removed

    def list_current_folders(self, service: str) -> List[dict]:

        service = self._svc_key(service)
        key = self._current_key(service)
        with self._current_lock:
            folders = self._current.get(key)
            if folders is None and key != service:
                # fallback на старый формат без IP (один ресивер / миграция)
                folders = self._current.get(service)
            return list(folders or [])

    def resolve_track_page_url(self, service: str, track_oid: str) -> str:
        """Страница трека (page_url) из сохранённых «Текущих плейлистов» (все IP)."""
        if not track_oid:
            return ""
        service = self._svc_key(service)
        with self._current_lock:
            for key, folders in self._current.items():
                if key != service and not key.startswith(service + "@"):
                    continue
                for folder in folders or []:
                    for t in folder.get("tracks") or []:
                        if t.get("oid") == track_oid and t.get("page_url"):
                            return t["page_url"]
            for key, br in self._last_browsed.items():
                if key == service or key.startswith(service + "@"):
                    for t in (br.get("tracks") or []):
                        if t.get("oid") == track_oid and t.get("page_url"):
                            return t["page_url"]
        return ""

    DM_WARM_COUNT = 8        # сколько треков drivemusic перепарсить при входе в папку

    def _dm_refresh_current_links(self, folder: dict) -> None:
        """Вход в «Текущий плейлист» drivemusic: заново распарсить ссылки на треки, начиная
        с того, на котором закончили слушать. Фоновый поток — Browse не ждёт сайт."""
        dm = getattr(self, "drivemusic", None)
        if dm is None or not hasattr(dm, "refresh_links"):
            return
        fid = folder.get("id") or ""
        running = getattr(self, "_dm_refresh_running", None)
        if running is None:
            running = self._dm_refresh_running = set()
        if fid in running:
            return
        tracks = list(folder.get("tracks") or [])
        start = int(folder.get("start_index") or 0)
        items = []
        for t in tracks[start:]:
            oid = t.get("oid") or ""
            tid = oid.rsplit(":", 1)[-1]
            if not tid:
                continue
            items.append((tid, t.get("page_url") or "", t.get("title") or ""))
        if not items:
            return
        running.add(fid)

        def work():
            try:
                n = dm.refresh_links(items, count=self.DM_WARM_COUNT)
                log.info("drivemusic: ссылки перепарсены с позиции %d: %d/%d",
                         start + 1, n, min(len(items), self.DM_WARM_COUNT))
            except Exception as e:
                log.warning("drivemusic refresh links: %s", e)
            finally:
                running.discard(fid)

        threading.Thread(target=work, name="dm-refresh", daemon=True).start()

    def resolve_track_title(self, service: str, track_oid: str) -> str:
        """Найти artist — title по oid среди текущих подборок (все IP)."""
        if not track_oid:
            return ""
        service = self._svc_key(service)
        with self._current_lock:
            keys = [service]
            for k in self._current.keys():
                if k.startswith(service + "@"):
                    keys.append(k)
            # сначала ключ текущего IP
            ip = self._request_client_ip()
            if ip:
                keys.insert(0, f"{service}@{ip}")
            seen = set()
            for key in keys:
                if key in seen:
                    continue
                seen.add(key)
                for folder in self._current.get(key) or []:
                    for t in folder.get("tracks") or []:
                        if t.get("oid") == track_oid:
                            return (t.get("title") or "").strip()
            for key in list(self._last_browsed.keys()):
                if key == service or key.startswith(service + "@"):
                    for t in (self._last_browsed[key].get("tracks") or []):
                        if t.get("oid") == track_oid:
                            return (t.get("title") or "").strip()
        return ""


    def get_current_folder(self, service: str, folder_id: str) -> Optional[dict]:
        for f in self.list_current_folders(service):
            if f.get("id") == folder_id:
                return dict(f)
        return None

    def current_playlist_count(self, service: str) -> int:
        """Число папок в истории (для childCount контейнера)."""
        try:
            return len(self.list_current_folders(service))
        except Exception:
            return 0

    def _current_children(self, service: str, object_id: str) -> List[str]:
        """Содержимое «Текущий плейлист»: список папок или треки выбранной папки.

        object_id:
          {service}:current           → список папок
          {service}:current:{fid}     → треки папки (с текущей позиции)
        """
        prefix = f"{service}:current"
        ek = self._svc_key(service)   # ключ хранения (с учётом аккаунта)
        if object_id == prefix:
            # Список папок: снимки играющих папок не трогаем (никакой подрезки по времени).
            out = []
            for f in self.list_current_folders(service):
                fid = f.get("id") or "x"
                n = len(f.get("tracks") or [])
                title = f.get("title") or "Плейлист"
                start = int(f.get("start_index") or 0)
                if start > 0 and n:
                    title = f"{title} (с {start + 1}/{n})"
                remaining = max(0, n - start) if start > 0 else n
                out.append(self._container(f"{prefix}:{fid}", prefix, title, remaining))
            return out
        if object_id.startswith(prefix + ":"):
            fid = object_id[len(prefix) + 1:]
            folder = self.get_current_folder(service, fid)
            if not folder:
                return []
            ip = self._request_client_ip()
            freeze_key = f"{ek}@{ip}:{fid}" if ip else f"{ek}:{fid}"
            # Список «замораживается» на время прослушивания: ресивер (dLeyna/GUPnP)
            # ведёт очередь по ИНДЕКСАМ папки («дай элемент N»). Если список подрезать
            # (убрать прозвучавшие) посреди воспроизведения, индексы съезжают, и
            # автопереход на следующий трек не происходит.
            # Поэтому позицию мы у себя двигаем (advance_current), а ресиверу отдаём
            # прежний снимок, пока папка играет. Подрезанный список он увидит, когда
            # включат другой плейлист или другую площадку (радио, файлы и т.д.).
            with self._current_lock:
                ckey = self._current_key(service)
                self._active_fid[ckey] = fid
                self._last_browsed.pop(ckey, None)
                tracks = self._frozen_current.get(freeze_key)
                if tracks is None:
                    # снимка нет (папка не играет или эмулятор перезапущен): берём с метки
                    # snap_start (если папка играла) или с текущей позиции
                    all_tracks = list(folder.get("tracks") or [])
                    snap = folder.get("snap_start")
                    start = int(snap) if snap is not None else int(folder.get("start_index") or 0)
                    tracks = all_tracks[start:] if start > 0 else all_tracks
                    self._frozen_current[freeze_key] = tracks
                    self._frozen_current_ts[freeze_key] = time.time()
            if service == "drivemusic":
                st_ = getattr(self._request_tls, "start_index", None)
                rc_ = getattr(self._request_tls, "req_count", None)
                if st_ is not None and int(st_ or 0) == 0 and int(rc_ or 0) != 1:
                    try:
                        self._dm_refresh_current_links(folder)
                    except Exception as e:
                        log.debug("dm refresh hook: %s", e)
            out = []
            for t in tracks:
                out.append(self._item(
                    t.get("oid") or "", object_id, t.get("title") or "?",
                    t.get("url") or "",
                    mime=t.get("mime") or "audio/mpeg",
                    artist=t.get("artist") or "",
                    duration=int(t.get("duration") or 0),
                    album=t.get("album") or "",
                ))
            return out
        return []

    # ---------- UPnP SystemUpdateID ----------

    @property
    def system_update_id(self) -> int:
        return int(getattr(self, "_system_update_id", 1) or 1)

    def bump_update_id(self) -> int:
        self._system_update_id = self.system_update_id + 1
        return self._system_update_id

    def on_yandex_track_playing(self, track_id: str) -> None:
        """Стрим трека Яндекса: feedback волны + догрузка очереди."""
        try:
            ya = self.yandex
            if not ya:
                return
            tid = str(track_id)
            # предыдущий трек волны считаем законченным → алгоритм сдвигается
            prev = getattr(self, "_ya_wave_last_playing", None)
            if prev and prev != tid and hasattr(ya, "wave_track_finished"):
                try:
                    ya.wave_track_finished(prev, played_seconds=45.0)
                except Exception:
                    pass
            self._ya_wave_last_playing = tid
            if hasattr(ya, "wave_track_started"):
                try:
                    ya.wave_track_started(tid)
                except Exception:
                    pass
            grown = ya.ensure_wave_ahead(tid, ahead=15)
            if grown:
                self.bump_update_id()
                log.info("wave grew +%s (playing %s), UpdateID=%s", grown, tid, self.system_update_id)
        except Exception as e:
            log.debug("on_yandex_track_playing: %s", e)

    # ---------- публичное API для веб-панели ----------

    def saved_searches(self) -> List[dict]:
        with self._saved_lock:
            return self._hist(self._saved)

    def add_saved_artist(self, artist_id: str, name: str, query: str = "") -> None:
        with self._saved_lock:
            sid = f"a{artist_id}"
            for s in self._saved:
                if s.get("id") == sid:
                    return
            self._saved.append({
                "id": sid,
                "title": name or f"Artist {artist_id}",
                "type": "artist",
                "artist_id": str(artist_id),
                "query": query,
            })
            if len(self._saved) > SAVED_LIMIT:
                del self._saved[:-SAVED_LIMIT]

    def add_saved_album(self, album_id: str, title: str, query: str = "") -> None:
        with self._saved_lock:
            sid = f"b{album_id}"
            for s in self._saved:
                if s.get("id") == sid:
                    return
            self._saved.append({
                "id": sid,
                "title": title or f"Album {album_id}",
                "type": "album",
                "album_id": str(album_id),
                "query": query,
            })
            if len(self._saved) > SAVED_LIMIT:
                del self._saved[:-SAVED_LIMIT]

    def remove_saved(self, sid: str) -> None:
        with self._saved_lock:
            self._saved[:] = [s for s in self._saved if s.get("id") != sid]

    def clear_saved(self) -> None:
        with self._saved_lock:
            self._saved.clear()

    def dm_saved_searches(self) -> List[dict]:
        with self._dm_saved_lock:
            return self._hist(self._dm_saved)

    def add_dm_saved_artist(self, artist_id: str, name: str, query: str = "") -> None:
        with self._dm_saved_lock:
            sid = f"dma{artist_id}"
            for s in self._dm_saved:
                if s.get("id") == sid:
                    return
            self._dm_saved.append({
                "id": sid,
                "title": name or f"Artist {artist_id}",
                "type": "artist",
                "artist_id": str(artist_id),
                "query": query,
            })
            if len(self._dm_saved) > SAVED_LIMIT:
                del self._dm_saved[:-SAVED_LIMIT]

    def remove_dm_saved(self, sid: str) -> None:
        with self._dm_saved_lock:
            self._dm_saved[:] = [s for s in self._dm_saved if s.get("id") != sid]

    def clear_dm_saved(self) -> None:
        with self._dm_saved_lock:
            self._dm_saved.clear()

    # ---------- «Мои книги» (из поиска, как раньше, в памяти) ----------

    def ak_saved_books(self) -> List[dict]:
        with self._ak_saved_lock:
            return self._hist(self._ak_saved)

    def add_ak_saved_book(self, slug: str, title: str, query: str = "") -> None:
        slug = (slug or "").strip()
        if not slug:
            return
        with self._ak_saved_lock:
            for s in self._ak_saved:
                if s.get("slug") == slug:
                    return
            self._ak_saved.append({
                "id": slug,
                "type": "book",
                "slug": slug,
                "title": title or slug,
                "query": query,
            })
            if len(self._ak_saved) > SAVED_LIMIT:
                del self._ak_saved[:-SAVED_LIMIT]
        self.bump_update_id()

    def remove_ak_saved(self, sid: str) -> None:
        with self._ak_saved_lock:
            before = len(self._ak_saved)
            self._ak_saved[:] = [s for s in self._ak_saved if s.get("id") != sid]
            changed = len(self._ak_saved) != before
        if changed:
            self.bump_update_id()

    def clear_ak_saved(self) -> None:
        with self._ak_saved_lock:
            if not self._ak_saved:
                return
            self._ak_saved.clear()
        self.bump_update_id()


    # ---------- история поиска: сколько показывать и хранить на диске ----------

    def search_cfg(self) -> dict:
        try:
            raw = (self.search_cfg_fn() if self.search_cfg_fn else None) or {}
        except Exception:  # noqa: BLE001
            raw = {}
        v = raw.get("persist", False)
        persist = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
        try:
            show = int(raw.get("history_show", SEARCH_HISTORY_SHOW_DEFAULT))
        except (TypeError, ValueError):
            show = SEARCH_HISTORY_SHOW_DEFAULT
        return {"persist": persist, "history_show": max(1, min(50, show))}

    def _hist(self, lst: List[dict], lock=None) -> List[dict]:
        """Последние N записей истории поиска (N — настройка «показывать в истории»)."""
        n = self.search_cfg()["history_show"]
        if lock is not None:
            with lock:
                return list(lst)[-n:]
        return list(lst)[-n:]

    _HIST_ATTRS = {
        "deezer": "_saved", "drivemusic": "_dm_saved", "yandex": "_ya_saved", "zvuk": "_zv_saved",
        "kion": "_kion_saved", "akniga": "_ak_saved", "promodj": "_pd_saved", "zaycev": "_zy_saved",
    }

    def save_search_history(self) -> None:
        """Записать историю поиска на диск — только если включено «сохранять после перезагрузки»."""
        path = self._search_hist_path
        if not path or not self.search_cfg()["persist"]:
            return
        data: Dict[str, List[dict]] = {}
        for site, attr in self._HIST_ATTRS.items():
            with getattr(self, attr + "_lock"):
                data[site] = list(getattr(self, attr))
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        except Exception as e:  # noqa: BLE001
            log.warning("Не удалось сохранить историю поиска в %s: %s", path, e)

    def load_search_history(self) -> None:
        path = self._search_hist_path
        if not path or not self.search_cfg()["persist"] or not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            n = 0
            for site, attr in self._HIST_ATTRS.items():
                items = [x for x in (data.get(site) or []) if isinstance(x, dict) and x.get("id")]
                with getattr(self, attr + "_lock"):
                    lst = getattr(self, attr)
                    have = {x.get("id") for x in lst}
                    lst[:0] = [x for x in items if x.get("id") not in have][-SAVED_LIMIT:]
                    del lst[:-SAVED_LIMIT]
                n += len(items)
            log.info("История поиска загружена: %d записей из %s", n, path)
        except Exception as e:  # noqa: BLE001
            log.warning("Не удалось загрузить историю поиска из %s: %s", path, e)

    def delete_search_history_file(self) -> None:
        try:
            if self._search_hist_path and os.path.isfile(self._search_hist_path):
                os.remove(self._search_hist_path)
        except OSError:
            pass

    # ---------- «Избранное» остальных площадок (до 500 на площадку) ----------

    FAV_SITES = ("deezer", "drivemusic", "yandex", "zvuk", "kion", "zaycev", "promodj")

    @staticmethod
    def _fav_item(site: str, stype: str, ident: str, title: str, query: str) -> Optional[dict]:
        ident = str(ident or "").strip()
        if not ident:
            return None
        stype = stype if stype in ("artist", "album", "playlist") else "artist"
        if site == "deezer":
            sid, key = ("a" if stype == "artist" else "b") + ident, stype + "_id"
        elif site == "drivemusic":
            stype, sid, key = "artist", "dma" + ident, "artist_id"
        elif site == "yandex":
            sid, key = f"ya-{stype}-{ident}", stype + "_id"
        elif site == "kion":
            sid, key = f"kion-{stype}-{ident}", stype + "_id"
        elif site == "zvuk":
            sid, key = f"zv-{stype}-{ident}", stype + "_id"
        elif site == "zaycev":
            stype, sid, key = "artist", "zy-artist-" + ident, "artist_id"
        elif site == "promodj":
            return {"id": ident, "type": "artist", "slug": ident, "title": title or ident, "query": query}
        else:
            return None
        return {"id": sid, "title": title or ident, "type": stype, key: ident, "query": query}

    def site_favorites(self, site: str) -> List[dict]:
        with self._site_favs_lock:
            return list(self._site_favs.get(site, []))

    def _load_site_favorites(self) -> None:
        path = self._site_favs_path
        if not path or not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for site in self.FAV_SITES:
                    items = [x for x in (data.get(site) or []) if isinstance(x, dict) and x.get("id")]
                    self._site_favs[site] = items[:SITE_FAVORITES_LIMIT]
                log.info("Избранное площадок загружено: %s",
                         ", ".join(f"{k}={len(v)}" for k, v in self._site_favs.items() if v) or "пусто")
        except Exception as e:  # noqa: BLE001
            log.warning("Не удалось загрузить избранное из %s: %s", path, e)

    def _save_site_favorites(self) -> None:
        path = self._site_favs_path
        if not path:
            return
        try:
            with self._site_favs_lock:
                data = {k: list(v) for k, v in self._site_favs.items()}
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        except Exception as e:  # noqa: BLE001
            log.warning("Не удалось сохранить избранное в %s: %s", path, e)

    def add_site_favorite(self, site: str, stype: str, ident: str, title: str, query: str = "") -> str:
        """'ok' / 'exists' / 'limit' / 'empty' / 'unsupported'. Лимит — SITE_FAVORITES_LIMIT на площадку."""
        if site not in self.FAV_SITES:
            return "unsupported"
        item = self._fav_item(site, stype, ident, title, query)
        if item is None:
            return "empty"
        with self._site_favs_lock:
            lst = self._site_favs.setdefault(site, [])
            if any(x.get("id") == item["id"] for x in lst):
                return "exists"
            if len(lst) >= SITE_FAVORITES_LIMIT:
                return "limit"
            lst.append(item)
        self._save_site_favorites()
        self.bump_update_id()
        return "ok"

    def remove_site_favorite(self, site: str, sid: str) -> None:
        with self._site_favs_lock:
            lst = self._site_favs.get(site, [])
            before = len(lst)
            lst[:] = [x for x in lst if x.get("id") != sid]
            changed = len(lst) != before
        if changed:
            self._save_site_favorites()
            self.bump_update_id()

    def clear_site_favorites(self, site: str) -> None:
        with self._site_favs_lock:
            if not self._site_favs.get(site):
                return
            self._site_favs[site] = []
        self._save_site_favorites()
        self.bump_update_id()

    # ---------- «Избранное» (отдельный конфиг, до 500 книг) ----------

    def ak_favorites(self) -> List[dict]:
        with self._ak_favorites_lock:
            return list(self._ak_favorites)

    def _load_ak_favorites(self) -> None:
        path = self._favorites_path
        if not path or not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                return
            loaded: List[dict] = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                slug = (item.get("slug") or item.get("id") or "").strip()
                if not slug:
                    continue
                loaded.append({
                    "id": slug,
                    "type": "book",
                    "slug": slug,
                    "title": item.get("title") or slug,
                    "query": item.get("query") or "",
                })
                if len(loaded) >= AK_FAVORITES_LIMIT:
                    break
            with self._ak_favorites_lock:
                self._ak_favorites = loaded
            log.info("Akniga favorites loaded: %d books from %s", len(loaded), path)
        except Exception as e:
            log.warning("Failed to load akniga favorites from %s: %s", path, e)

    def _save_ak_favorites(self) -> None:
        path = self._favorites_path
        if not path:
            return
        try:
            with self._ak_favorites_lock:
                data = list(self._ak_favorites)
            dirname = os.path.dirname(path)
            if dirname:
                os.makedirs(dirname, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            log.warning("Failed to save akniga favorites to %s: %s", path, e)

    def add_ak_favorite(self, slug: str, title: str, query: str = "") -> str:
        """Добавить книгу в избранное.

        Возвращает:
          "ok"      — добавлено
          "exists"  — уже есть
          "limit"   — достигнут лимит AK_FAVORITES_LIMIT
          "empty"   — пустой slug
        """
        slug = (slug or "").strip()
        if not slug:
            return "empty"
        with self._ak_favorites_lock:
            for s in self._ak_favorites:
                if s.get("slug") == slug:
                    return "exists"
            if len(self._ak_favorites) >= AK_FAVORITES_LIMIT:
                return "limit"
            self._ak_favorites.append({
                "id": slug,
                "type": "book",
                "slug": slug,
                "title": title or slug,
                "query": query,
            })
        self._save_ak_favorites()
        self.bump_update_id()
        return "ok"

    def remove_ak_favorite(self, sid: str) -> None:
        with self._ak_favorites_lock:
            before = len(self._ak_favorites)
            self._ak_favorites[:] = [s for s in self._ak_favorites if s.get("id") != sid]
            changed = len(self._ak_favorites) != before
        if changed:
            self._save_ak_favorites()
            self.bump_update_id()

    def clear_ak_favorites(self) -> None:
        with self._ak_favorites_lock:
            if not self._ak_favorites:
                return
            self._ak_favorites.clear()
        self._save_ak_favorites()
        self.bump_update_id()

    def ya_saved_searches(self) -> List[dict]:
        with self._ya_saved_lock:
            return self._hist(self._ya_saved)

    def add_ya_saved_artist(self, artist_id: str, name: str, query: str = "") -> None:
        with self._ya_saved_lock:
            for s in self._ya_saved:
                if s.get("type") == "artist" and s.get("artist_id") == artist_id:
                    return
            self._ya_saved.append({
                "id": f"ya-artist-{artist_id}",
                "type": "artist",
                "artist_id": artist_id,
                "title": name,
                "query": query,
            })
            if len(self._ya_saved) > SAVED_LIMIT:
                del self._ya_saved[:-SAVED_LIMIT]

    def add_ya_saved_album(self, album_id: str, title: str, query: str = "") -> None:
        with self._ya_saved_lock:
            for s in self._ya_saved:
                if s.get("type") == "album" and s.get("album_id") == album_id:
                    return
            self._ya_saved.append({
                "id": f"ya-album-{album_id}",
                "type": "album",
                "album_id": album_id,
                "title": title,
                "query": query,
            })
            if len(self._ya_saved) > SAVED_LIMIT:
                del self._ya_saved[:-SAVED_LIMIT]

    def remove_ya_saved(self, sid: str) -> None:
        with self._ya_saved_lock:
            self._ya_saved[:] = [s for s in self._ya_saved if s.get("id") != sid]

    def clear_ya_saved(self) -> None:
        with self._ya_saved_lock:
            self._ya_saved.clear()

    def kion_saved_searches(self) -> List[dict]:
        with self._kion_saved_lock:
            return self._hist(self._kion_saved)

    def add_kion_saved_artist(self, artist_id: str, name: str, query: str = "") -> None:
        with self._kion_saved_lock:
            for s in self._kion_saved:
                if s.get("type") == "artist" and s.get("artist_id") == artist_id:
                    return
            self._kion_saved.append({
                "id": f"kion-artist-{artist_id}",
                "type": "artist",
                "artist_id": artist_id,
                "title": name,
                "query": query,
            })
            if len(self._kion_saved) > SAVED_LIMIT:
                del self._kion_saved[:-SAVED_LIMIT]

    def add_kion_saved_album(self, album_id: str, title: str, query: str = "") -> None:
        with self._kion_saved_lock:
            for s in self._kion_saved:
                if s.get("type") == "album" and s.get("album_id") == album_id:
                    return
            self._kion_saved.append({
                "id": f"kion-album-{album_id}",
                "type": "album",
                "album_id": album_id,
                "title": title,
                "query": query,
            })
            if len(self._kion_saved) > SAVED_LIMIT:
                del self._kion_saved[:-SAVED_LIMIT]

    def remove_kion_saved(self, sid: str) -> None:
        with self._kion_saved_lock:
            self._kion_saved[:] = [s for s in self._kion_saved if s.get("id") != sid]

    def clear_kion_saved(self) -> None:
        with self._kion_saved_lock:
            self._kion_saved.clear()


    def zv_saved_searches(self) -> List[dict]:
        with self._zv_saved_lock:
            return self._hist(self._zv_saved)

    def add_zv_saved_artist(self, artist_id: str, name: str, query: str = "") -> None:
        with self._zv_saved_lock:
            for s in self._zv_saved:
                if s.get("type") == "artist" and s.get("artist_id") == artist_id:
                    return
            self._zv_saved.append({
                "id": f"zv-artist-{artist_id}",
                "type": "artist",
                "artist_id": artist_id,
                "title": name,
                "query": query,
            })
            if len(self._zv_saved) > SAVED_LIMIT:
                del self._zv_saved[:-SAVED_LIMIT]

    def add_zv_saved_album(self, album_id: str, title: str, query: str = "") -> None:
        with self._zv_saved_lock:
            for s in self._zv_saved:
                if s.get("type") == "album" and s.get("album_id") == album_id:
                    return
            self._zv_saved.append({
                "id": f"zv-album-{album_id}",
                "type": "album",
                "album_id": album_id,
                "title": title,
                "query": query,
            })
            if len(self._zv_saved) > SAVED_LIMIT:
                del self._zv_saved[:-SAVED_LIMIT]


    def add_zv_saved_playlist(self, playlist_id: str, title: str, query: str = "") -> None:
        with self._zv_saved_lock:
            for s in self._zv_saved:
                if s.get("type") == "playlist" and s.get("playlist_id") == playlist_id:
                    return
            self._zv_saved.append({
                "id": f"zv-playlist-{playlist_id}",
                "type": "playlist",
                "playlist_id": playlist_id,
                "title": title,
                "query": query,
            })
            if len(self._zv_saved) > SAVED_LIMIT:
                del self._zv_saved[:-SAVED_LIMIT]

    def remove_zv_saved(self, sid: str) -> None:
        with self._zv_saved_lock:
            self._zv_saved[:] = [s for s in self._zv_saved if s.get("id") != sid]

    def clear_zv_saved(self) -> None:
        with self._zv_saved_lock:
            self._zv_saved.clear()

    def pd_saved_searches(self) -> List[dict]:
        with self._pd_saved_lock:
            return self._hist(self._pd_saved)

    def add_pd_saved_artist(self, slug: str, name: str, query: str = "") -> None:
        slug = (slug or "").strip()
        if not slug:
            return
        with self._pd_saved_lock:
            for s in self._pd_saved:
                if s.get("slug") == slug or s.get("id") == slug:
                    return
            self._pd_saved.append({
                "id": slug,
                "type": "artist",
                "slug": slug,
                "title": name or slug,
                "query": query,
            })
            if len(self._pd_saved) > SAVED_LIMIT:
                del self._pd_saved[:-SAVED_LIMIT]

    def remove_pd_saved(self, sid: str) -> None:
        with self._pd_saved_lock:
            self._pd_saved[:] = [
                s for s in self._pd_saved
                if s.get("id") != sid and s.get("slug") != sid
            ]

    def clear_pd_saved(self) -> None:
        with self._pd_saved_lock:
            self._pd_saved.clear()

    # ---------- Zaycev.net: «🔍 Мои поиски» (артисты из веб-поиска) ----------

    def zy_saved_searches(self) -> List[dict]:
        with self._zy_saved_lock:
            return self._hist(self._zy_saved)

    def add_zy_saved_artist(self, artist_id: str, name: str, query: str = "") -> None:
        artist_id = str(artist_id or "").strip()
        if not artist_id:
            return
        with self._zy_saved_lock:
            for s in self._zy_saved:
                if s.get("artist_id") == artist_id:
                    return
            self._zy_saved.append({
                "id": f"zy-artist-{artist_id}",
                "type": "artist",
                "artist_id": artist_id,
                "title": name or f"Artist {artist_id}",
                "query": query,
            })
            if len(self._zy_saved) > SAVED_LIMIT:
                del self._zy_saved[:-SAVED_LIMIT]
        self.bump_update_id()

    def remove_zy_saved(self, sid: str) -> None:
        with self._zy_saved_lock:
            before = len(self._zy_saved)
            self._zy_saved[:] = [s for s in self._zy_saved if s.get("id") != sid]
            changed = len(self._zy_saved) != before
        if changed:
            self.bump_update_id()

    def clear_zy_saved(self) -> None:
        with self._zy_saved_lock:
            if not self._zy_saved:
                return
            self._zy_saved.clear()
        self.bump_update_id()

    # ---------- Browse ----------

    def browse(
        self, object_id: str, starting_index: int = 0, requested_count: int = 50
    ) -> Tuple[str, int, int]:
        object_id = object_id or ROOT
        # StartingIndex нужен «Текущему плейлисту»: вход в папку (индекс 0) показывает
        # актуальный хвост, а подгрузка следующих страниц — тот же снимок списка.
        self._request_tls.start_index = int(starting_index or 0)
        self._request_tls.req_count = int(requested_count or 0)
        try:
            scope, object_id = self._acc_enter(object_id)
            if scope is None:
                return self._empty_didl(), 0, 0
            with scope:
                return self._browse_inner(object_id, starting_index, requested_count)
        finally:
            # Не оставляем значения в потоке: keep-alive соединение обслуживает
            # следующие запросы тем же потоком, а внутренние вызовы (BrowseMetadata
            # → _children) не должны считаться «входом в папку».
            self._request_tls.start_index = None
            self._request_tls.req_count = None

    # ---------- реальный формат в списке (FLAC или MP3) ----------

    _FLAC_RES_RE = re.compile(
        r'<res protocolInfo="http-get:\*:audio/flac:[^"]*"[^>]*>'
        r'[^<]*?/stream/(yandex)/(?:a(\d+)/)?(\d+)</res>'
    )
    probe_timeout = 4.0   # сек на проверку FLAC при открытии списка

    def set_format_probe(self, timeout=None, cache_sec=None) -> None:
        """Применить настройки проверки FLAC (из конфига / веб-панели) на лету."""
        if timeout is not None:
            self.probe_timeout = float(timeout)
        if cache_sec is not None:
            ya = self.yandex
            if hasattr(ya, "set_all"):
                ya.set_all(flac_cache_sec=float(cache_sec))
            else:
                ya.flac_cache_sec = float(cache_sec)

    def reconcile_formats(self, didl: str) -> str:
        """Заменить заявленный audio/flac на audio/mpeg у треков Яндекса,
        где FLAC реально недоступен (нет Plus/подписки, трек без lossless).

        Ресиверы вроде Yamaha RX-V777 выбирают декодер по формату из списка;
        если заявлен FLAC, а приходит mp3 — трек пропускается. Результаты
        кэшируются; что не успели проверить за probe_timeout — доделывается
        в фоне и учтётся при следующем открытии списка.
        """
        if not didl or "audio/flac" not in didl:
            return didl
        if self.probe_timeout <= 0:      # проверка выключена (format_probe_timeout: 0)
            return didl
        found: Dict[Tuple[str, int], List[str]] = {}
        for m in self._FLAC_RES_RE.finditer(didl):
            svc, acc, tid = m.group(1), int(m.group(2) or 0), m.group(3)
            found.setdefault((svc, acc), [])
            if tid not in found[(svc, acc)]:
                found[(svc, acc)].append(tid)
        if not found:
            return didl

        import concurrent.futures as cf

        def client_for(svc: str, acc: int):
            pool = self._pools.get(svc)
            if pool is not None:
                return pool.client(acc)
            return getattr(self, svc, None)

        ex = cf.ThreadPoolExecutor(max_workers=8)
        futs: Dict[object, Tuple[str, int, List[str]]] = {}
        results: Dict[Tuple[str, int, str], Optional[bool]] = {}
        try:
            for (svc, acc), ids in found.items():
                cli = client_for(svc, acc)
                if cli is None:
                    continue
                if hasattr(cli, "probe_flac"):
                    for tid in ids:
                        futs[ex.submit(cli.probe_flac, tid)] = (svc, acc, [tid])
            done, _pending = cf.wait(list(futs), timeout=self.probe_timeout)
            for f in done:
                svc, acc, ids = futs[f]
                try:
                    r = f.result()
                except Exception:
                    continue
                if isinstance(r, dict):
                    for tid, ok in r.items():
                        results[(svc, acc, str(tid))] = ok
                else:
                    results[(svc, acc, ids[0])] = r
        finally:
            ex.shutdown(wait=False)   # недождавшиеся проверки доделаются в фоне и лягут в кэш

        def fix(m: "re.Match") -> str:
            key = (m.group(1), int(m.group(2) or 0), m.group(3))
            if results.get(key) is False:
                s = m.group(0)
                return s.replace("audio/flac", "audio/mpeg", 1).replace(
                    "DLNA.ORG_PN=FLAC", "DLNA.ORG_PN=MP3", 1
                )
            return m.group(0)

        return self._FLAC_RES_RE.sub(fix, didl)

    @staticmethod
    def _empty_didl() -> str:
        return (
            '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
            'xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/"></DIDL-Lite>'
        )

    def _browse_inner(
        self, object_id: str, starting_index: int = 0, requested_count: int = 50
    ) -> Tuple[str, int, int]:
        empty = (
            '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
            'xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/"></DIDL-Lite>'
        )
        try:
            if object_id.startswith("deezer:flowcfg:"):
                return self._browse_flow(object_id, starting_index, requested_count)

            if object_id.startswith(YA_WAVE_PRESET_PREFIX):
                return self._browse_ya_wave(object_id, starting_index, requested_count)

            children = self._children(object_id, starting_index=starting_index)
            total = len(children)
            if requested_count <= 0:
                requested_count = total or 1
            page = children[starting_index : starting_index + requested_count]
            didl = [
                '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
                'xmlns:dc="http://purl.org/dc/elements/1.1/" '
                'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
                'xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">'
            ]
            didl.extend(page)
            didl.append("</DIDL-Lite>")
            return "\n".join(didl), len(page), total
        except Exception as e:
            log.error("browse %r failed: %s", object_id, e)
            return empty, 0, 0

    # ---------- Browse Flow ----------

    @staticmethod
    def _flow_title(tr, fallback: str) -> Tuple[str, str, int, str]:
        """Нормальный заголовок: «Артист — Трек», без пустого FLOW."""
        artist = (getattr(tr, "artist", None) or "").strip()
        name = (getattr(tr, "title", None) or "").strip()
        album = (getattr(tr, "album", None) or "").strip()
        duration = int(getattr(tr, "duration", 0) or 0)
        if artist and name:
            title = f"{artist} — {name}"
        elif name:
            title = name
        elif artist:
            title = artist
        else:
            title = fallback
        return title, artist, duration, album

    def _browse_flow(
        self, object_id: str, starting_index: int, requested_count: int
    ) -> Tuple[str, int, int]:
        """Виртуальный плейлист Flow: 100 слотов, без массовой догрузки на Browse.

        Browse только отдаёт каталог (TotalMatches=100). К Deezer ходим минимум:
        одна партия из 3 треков, и только если кэш пуст (первый заход).
        Дальнейшие треки — по 3 при /stream/deezer_flow/... (прослушивание).
        """
        cfg_id = object_id.split(":", 2)[-1]
        if cfg_id == "default":
            cfg_id = ""
        cfg_key = cfg_id or "default"
        total = FLOW_MAX_CACHE

        if requested_count <= 0:
            page_count = FLOW_MAX_PAGE
        else:
            page_count = min(requested_count, FLOW_MAX_PAGE)

        empty_didl = (
            '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
            'xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">'
            "</DIDL-Lite>"
        )
        if starting_index >= total:
            return empty_didl, 0, total

        end = min(starting_index + page_count, total)
        mc = FLOW_MAX_CACHE
        # Только чтение кэша. Догрузка — максимум одна партия (3), и только
        # если кэш пустой. Browse start=50 count=50 НЕ должен тянуть 50 треков.
        tracks = self.deezer.get_flow_tracks(cfg_id, append=False, max_cache=mc)
        if not tracks:
            tracks = self.deezer.get_flow_tracks(cfg_id, append=True, max_cache=mc)

        didl = [
            '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
            'xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">'
        ]
        for i in range(starting_index, end):
            url = f"{self.base_url}/stream/deezer_flow/{quote(cfg_key, safe='')}/{i}"
            oid = f"deezer:flowslot:{cfg_key}:{i}"
            if i < len(tracks):
                title, artist, duration, album = self._flow_title(
                    tracks[i], f"Flow · {i + 1}"
                )
            else:
                title, artist, duration, album = f"Flow · {i + 1}", "", 0, ""
            didl.append(
                self._item(
                    oid,
                    object_id,
                    title,
                    url,
                    mime="audio/flac",
                    artist=artist,
                    duration=duration,
                    album=album,
                )
            )
        didl.append("</DIDL-Lite>")
        return "\n".join(didl), end - starting_index, total

    def _browse_ya_wave(
        self, object_id: str, starting_index: int, requested_count: int
    ) -> Tuple[str, int, int]:
        """Моя волна Яндекса — делегируем в YandexProvider."""
        for p in self._providers:
            if getattr(p, "root_id", None) == "yandex" and hasattr(p, "_browse_ya_wave"):
                return p._browse_ya_wave(object_id, starting_index, requested_count)
        empty = (
            '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
            'xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/"></DIDL-Lite>'
        )
        return empty, 0, 0

    # ---------- Search ----------

    def search(
        self,
        container_id: str,
        query: str,
        starting_index: int = 0,
        requested_count: int = 50,
    ) -> Tuple[str, int, int]:
        q = (query or "").strip()
        cid = container_id or ROOT
        items: List[str] = []

        if q and self.deezer.enabled:
            try:
                tracks = self.deezer.search_tracks(q, limit=max(requested_count, 100))
                for tr in tracks:
                    url = f"{self.base_url}/stream/deezer/{tr.id}"
                    title = f"{tr.artist} — {tr.title}" if tr.artist else tr.title
                    items.append(self._item(
                        f"deezer:tr:{tr.id}",
                        cid,
                        title,
                        url,
                        mime="audio/flac",
                        artist=tr.artist,
                        duration=tr.duration,
                        album=tr.album,
                    ))
            except Exception as e:
                log.warning("search deezer: %s", e)

        if q and self.drivemusic and self.drivemusic.enabled:
            try:
                tracks = self.drivemusic.search_tracks(q, limit=max(requested_count, 40))
                for tr in tracks:
                    url = f"{self.base_url}/stream/drivemusic/{tr.id}"
                    title = f"{tr.artist} — {tr.title}" if tr.artist else tr.title
                    items.append(self._item(
                        f"drivemusic:tr:{tr.id}",
                        cid,
                        title,
                        url,
                        mime="audio/mpeg",
                        artist=tr.artist,
                        duration=tr.duration,
                        album=tr.album,
                    ))
            except Exception as e:
                log.warning("search drivemusic: %s", e)

        if q and self.zaycev and self.zaycev.enabled:
            try:
                tracks = self.zaycev.search_tracks(q, limit=max(requested_count, 40))
                for tr in tracks:
                    url = f"{self.base_url}/stream/zaycev/{tr.id}"
                    title = f"{tr.artist} — {tr.title}" if tr.artist else tr.title
                    items.append(self._item(
                        f"zaycev:tr:{tr.id}",
                        cid,
                        title,
                        url,
                        mime="audio/mpeg",
                        artist=tr.artist,
                        duration=tr.duration,
                        album=tr.album,
                    ))
            except Exception as e:
                log.warning("search zaycev: %s", e)

        if q and self.akniga and self.akniga.enabled:
            try:
                books = self.akniga.search_books(q, limit=30)
                for b in books:
                    if getattr(b, "closed", False):
                        continue
                    items.append(self._container(
                        f"{AK_BOOK_PREFIX}{b.slug}",
                        cid,
                        self._ak_label(b),
                        0,
                    ))
            except Exception as e:
                log.warning("search akniga: %s", e)

        if q and self.yandex and self.yandex.enabled:
            try:
                tracks = self.yandex.search_tracks(q, limit=max(requested_count, 40))
                for tr in tracks:
                    url = f"{self.base_url}/stream/yandex/{tr.id}"
                    title = f"{tr.artist} — {tr.title}" if tr.artist else tr.title
                    items.append(self._item(
                        f"yandex:tr:{tr.id}",
                        cid,
                        title,
                        url,
                        mime=self._ya_mime(),
                        artist=tr.artist,
                        duration=tr.duration,
                        album=tr.album,
                    ))
            except Exception as e:
                log.warning("search yandex: %s", e)

        if q and self.kion and self.kion.enabled:
            try:
                tracks = self.kion.search_tracks(q, limit=max(requested_count, 40))
                for tr in tracks:
                    url = f"{self.base_url}/stream/kion/{tr.id}"
                    title = f"{tr.artist} — {tr.title}" if tr.artist else tr.title
                    items.append(self._item(
                        f"kion:tr:{tr.id}",
                        cid,
                        title,
                        url,
                        mime=self._kion_mime(),
                        artist=tr.artist,
                        duration=tr.duration,
                        album=tr.album,
                    ))
            except Exception as e:
                log.warning("search kion: %s", e)

        if q and self.zvuk and self.zvuk.enabled:
            try:
                tracks = self.zvuk.search_tracks(q, limit=max(requested_count, 40))
                for tr in tracks:
                    url = f"{self.base_url}/stream/zvuk/{tr.id}"
                    title = f"{tr.artist} — {tr.title}" if tr.artist else tr.title
                    items.append(self._item(
                        f"zvuk:tr:{tr.id}",
                        cid,
                        title,
                        url,
                        mime=self._zv_mime(),
                        artist=tr.artist,
                        duration=tr.duration,
                        album=tr.album,
                    ))
            except Exception as e:
                log.warning("search zvuk: %s", e)

        if q:
            ql = q.lower()
            for pl in self.radio.list_playlists():
                for i, st in enumerate(pl.stations):
                    if ql in st.name.lower():
                        url = f"{self.base_url}/stream/radio/{pl.id}/{i}"
                        items.append(self._item(
                            f"radio:{pl.id}:{i}", cid, f"{i + 1}. {st.name}", url,
                        ))

        total = len(items)
        if requested_count <= 0:
            requested_count = total or 1
        page = items[starting_index : starting_index + requested_count]

        didl = [
            '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
            'xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">'
        ]
        didl.extend(page)
        didl.append("</DIDL-Lite>")
        return "\n".join(didl), len(page), total

    # ---------- дерево ----------

    def _children(self, object_id: str, starting_index: int = 0) -> List[str]:
        """Корень + делегирование в ServiceProvider по префиксу id."""
        scope, object_id = self._acc_enter(object_id)
        if scope is None:
            return []
        with scope:
            return self._children_inner(object_id, starting_index)

    def _children_inner(self, object_id: str, starting_index: int = 0) -> List[str]:
        # Корень сервиса с несколькими аккаунтами → сначала выбор аккаунта
        pool = self._acc_pool(object_id)
        if pool is not None and not pool.scoped:
            return self._account_list(object_id)
        if object_id == ROOT:
            items = [
                self._container(RADIO, ROOT, "Radio", self._radio_count()),
            ]
            if self.files and self.files.roots:
                items.append(self._container(FILES, ROOT, "Файлы (USB)", 0))
            if self.files and (self.files.roots or self.files.playlist_folders):
                items.append(self._container(LOCALPL, ROOT, "Плейлисты (USB)", 0))
            if self.netstorage and self.netstorage.storages:
                items.append(self._container(NET, ROOT, "Сетевые хранилища", len(self.netstorage.storages)))
            if self.deezer and self.deezer.enabled:
                items.append(self._container(DEEZER, ROOT, "Deezer", 3))
            if self.drivemusic and self.drivemusic.enabled:
                items.append(self._container(DRIVEMUSIC, ROOT, "DriveMusic", 4))
            if self.akniga and self.akniga.enabled:
                items.append(self._container(AKNIGA, ROOT, "akniga.org (аудиокниги)", 4))
            if self.yandex and self.yandex.enabled:
                items.append(self._container(YANDEX, ROOT, "Яндекс.Музыка", 3))
            if self.kion and self.kion.enabled:
                items.append(self._container(KION, ROOT, "КИОН Музыка", 3))
            if self.zvuk and self.zvuk.enabled:
                items.append(self._container(ZVUK, ROOT, "Звук", 3))
            if self.promodj and self.promodj.enabled:
                items.append(self._container(PROMODJ, ROOT, "PromoDJ", 4))
            if self.zaycev and self.zaycev.enabled:
                items.append(self._container(ZAYCEV, ROOT, "Zaycev.net", 5))
            return items

        for prov in self._providers:
            if prov.handles(object_id):
                return prov.children(object_id)
        return []

    def _radio_count(self) -> int:
        for p in self._providers:
            if getattr(p, "root_id", None) == "radio" and hasattr(p, "_radio_count"):
                return p._radio_count()
        return len(self.radio.list_playlists()) if self.radio else 0

    def _container(self, oid: str, parent: str, title: str, child_count: int = 0) -> str:
        oid, parent = self._acc_id(oid), self._acc_id(parent)
        return (
            f'<container id="{_esc(oid)}" parentID="{_esc(parent)}" restricted="1" '
            f'childCount="{int(child_count)}">'
            f"<dc:title>{_esc(title)}</dc:title>"
            f"<upnp:class>object.container.storageFolder</upnp:class>"
            f"</container>"
        )

    def _item(
        self,
        oid: str,
        parent: str,
        title: str,
        url: str,
        mime: str = "audio/mpeg",
        artist: str = "",
        duration: int = 0,
        album: str = "",
    ) -> str:
        oid, parent, url = self._acc_id(oid), self._acc_id(parent), self._acc_url(url)
        # исполнитель/длительность для статистики прослушивания
        try:
            if "/stream/" in url:
                from . import stats as _stats
                _st = _stats.get_stats()
                if _st is not None:
                    _rest = url.split("/stream/", 1)[1].split("?")[0].split("/")
                    if _rest[0] in ("deezer", "yandex", "kion", "zvuk", "drivemusic", "promodj", "zaycev") and _rest[-1]:
                        _st.meta(_rest[0], _rest[-1], artist, title, duration)
        except Exception:
            pass
        if duration and duration > 0:
            h = duration // 3600
            m = (duration % 3600) // 60
            s = duration % 60
            dur = f"{h}:{m:02d}:{s:02d}.000"
        else:
            dur = "0:00:00.000"

        if "flac" in mime:
            protocol = (
                f"http-get:*:{mime}:"
                "DLNA.ORG_PN=FLAC;DLNA.ORG_OP=01;DLNA.ORG_CI=0"
            )
        elif "mpeg" in mime:
            protocol = (
                f"http-get:*:{mime}:"
                "DLNA.ORG_PN=MP3;DLNA.ORG_OP=01;DLNA.ORG_CI=0"
            )
        else:
            protocol = f"http-get:*:{mime}:*"

        res = f'<res protocolInfo="{_esc(protocol)}" duration="{dur}">{_esc(url)}</res>'
        parts = [
            f'<item id="{_esc(oid)}" parentID="{_esc(parent)}" restricted="1">',
            f"<dc:title>{_esc(title)}</dc:title>",
            "<upnp:class>object.item.audioItem.musicTrack</upnp:class>",
        ]
        if artist:
            parts.append(f"<dc:creator>{_esc(artist)}</dc:creator>")
            parts.append(f"<upnp:artist>{_esc(artist)}</upnp:artist>")
        if album:
            parts.append(f"<upnp:album>{_esc(album)}</upnp:album>")
        art = self._art_uri(url)
        if art:
            prof = ' dlna:profileID="JPEG_TN"' if self.covers_dlna_profile else ""
            parts.append(f"<upnp:albumArtURI{prof}>{_esc(art)}</upnp:albumArtURI>")
        parts.append(res)
        parts.append("</item>")
        return "".join(parts)

    def _art_uri(self, stream_url: str) -> str:
        if not self.covers_enabled:
            return ""
        prefix = f"{self.base_url}/stream/"
        if not stream_url.startswith(prefix):
            return ""
        rest = stream_url[len(prefix):]
        kind = rest.split("/", 1)[0] if rest else ""
        if kind in ("file", "local"):
            # /stream/file/<urlencoded path> → /cover/file/<md5>.jpg
            # Ресиверы (и HTTP) ломают пути с %2F; короткий id надёжнее.
            enc = rest.split("/", 1)[1] if "/" in rest else ""
            from urllib.parse import unquote
            abs_path = unquote(enc)
            svc = getattr(self, "covers_svc", None)
            if svc is not None and abs_path and hasattr(svc, "register_file_path"):
                fid = svc.register_file_path(abs_path)
                if fid:
                    return f"{self.base_url}/cover/file/{fid}.jpg"
            return f"{self.base_url}/cover/{rest}.jpg"
        if kind == "deezer_flow":
            # /stream/deezer_flow/<cfg>/<idx> → /cover/deezer/<track_id>.jpg
            # Обложка только если слот уже заполнен реальным треком (кэш Flow).
            parts = rest.split("/")
            if len(parts) >= 3 and self.deezer:
                from urllib.parse import unquote
                cfg_key = unquote(parts[1] or "default")
                try:
                    idx = int(parts[2].split("?")[0])
                except ValueError:
                    return ""
                config_id = "" if cfg_key == "default" else cfg_key
                try:
                    tracks = self.deezer.get_flow_tracks(
                        config_id, append=False, max_cache=FLOW_MAX_CACHE
                    )
                except Exception:
                    return ""
                if 0 <= idx < len(tracks) and tracks[idx].id:
                    return f"{self.base_url}/cover/deezer/{tracks[idx].id}.jpg"
            return ""
        if kind in ("deezer", "drivemusic", "yandex", "zvuk", "zaycev", "kion"):
            return f"{self.base_url}/cover/{rest}.jpg"
        if kind == "radio":
            # /stream/radio/<pid>/<i> → /cover/radio/<pid>/<i>.jpg
            if getattr(self, "radio_covers", True):
                return f"{self.base_url}/cover/{rest}.jpg"
            return ""
        if rest.startswith("akniga/"):
            parts = rest.split("/")
            if len(parts) >= 2 and parts[1]:
                return f"{self.base_url}/cover/akniga/{parts[1]}.jpg"
        return ""

