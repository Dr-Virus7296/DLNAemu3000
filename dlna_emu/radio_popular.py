"""«Ваше популярное радио»: станции, которые вы реально слушаете.

Станция попадает в список, если вы слушали её непрерывно не меньше ``min_minutes``
минут (один поток, без разрыва соединения). Из списка она уходит, если её не
включали дольше ``keep_days`` дней.

Порядок в списке — по частоте: сколько таких «долгих» прослушиваний было за последние
``keep_days`` дней; при равенстве — по суммарному времени, затем по давности.

Время меряется на потоке, который отдаёт эмулятор (радио через проксирование):
пока устройство не отключилось, соединение живо, и это и есть «слушаете подряд».
При обычном редиректе (проксирование выключено) эмулятор не видит, когда вы
остановили радио, поэтому такие прослушивания не считаются.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Dict, List, Optional

log = logging.getLogger("dlna_emu.radio_popular")

DAY = 86400.0


class RadioSession:
    """Одно соединение устройства со станцией. tick() — на каждый отправленный кусок."""

    def __init__(self, owner: "RadioPopular", url: str, name: str):
        self._owner = owner
        self.url = url
        self.name = name
        self.t0 = time.time()
        self._touched = False
        self._entry: Optional[dict] = None     # запись о прослушивании в статистике
        self._ended = False

    def tick(self) -> None:
        if self._ended:
            return
        el = time.time() - self.t0
        if not self._touched and el >= RadioPopular.TOUCH_SEC:
            self._touched = True
            self._owner._touch(self.url)
        if self._entry is None and el >= self._owner.min_sec:
            self._entry = self._owner._qualify(self.url, self.name, self.t0)

    def end(self) -> None:
        if self._ended:
            return
        self._ended = True
        self._owner._end(self.url, self._entry, self.t0)


class RadioPopular:
    TOUCH_SEC = 30          # короче — это проба/случайное включение, «последним включением» не считаем
    MAX_SESSIONS = 500      # потолок записей на станцию

    def __init__(self, path: str, min_minutes: int = 15, keep_days: int = 30):
        self.path = path
        self._lock = threading.RLock()
        self.min_minutes = 15
        self.keep_days = 30
        self.configure(min_minutes=min_minutes, keep_days=keep_days, prune=False)
        self._stats: Dict[str, dict] = {}
        self._active: Dict[str, int] = {}       # url -> сколько соединений играет сейчас
        self._warned = False
        self._load()
        self.prune()

    # ---------- настройки ----------

    @property
    def min_sec(self) -> float:
        return self.min_minutes * 60.0

    def configure(self, min_minutes=None, keep_days=None, prune: bool = True) -> None:
        if min_minutes is not None:
            try:
                self.min_minutes = max(1, min(int(min_minutes), 1440))
            except (TypeError, ValueError):
                pass
        if keep_days is not None:
            try:
                self.keep_days = max(1, min(int(keep_days), 365))
            except (TypeError, ValueError):
                pass
        if prune:
            self.prune()

    # ---------- учёт прослушивания ----------

    def begin(self, url: str, name: str) -> RadioSession:
        with self._lock:
            self._active[url] = self._active.get(url, 0) + 1
        return RadioSession(self, url, name)

    def _touch(self, url: str) -> None:
        """Станция из списка включена (не проба) — отодвигаем срок удаления."""
        with self._lock:
            e = self._stats.get(url)
            if e:
                e["last"] = time.time()

    def _qualify(self, url: str, name: str, t0: float) -> dict:
        """Прослушивание достигло порога — станция попадает в список."""
        now = time.time()
        with self._lock:
            e = self._stats.setdefault(url, {"name": name, "sessions": [], "last": now})
            e["name"] = name or e.get("name") or url
            sess = {"ts": t0, "sec": int(now - t0)}
            e["sessions"].append(sess)
            if len(e["sessions"]) > self.MAX_SESSIONS:
                e["sessions"] = e["sessions"][-self.MAX_SESSIONS:]
            e["last"] = now
            self._save()
            log.info("popular radio: «%s» — прослушивание %d мин подряд, станция в списке",
                     e["name"], self.min_minutes)
        return sess

    def _end(self, url: str, sess: Optional[dict], t0: float) -> None:
        now = time.time()
        with self._lock:
            n = self._active.get(url, 0) - 1
            if n > 0:
                self._active[url] = n
            else:
                self._active.pop(url, None)
            e = self._stats.get(url)
            if sess is not None and e is not None:
                sess["sec"] = int(now - t0)
                e["last"] = now
                self._save()
            elif e is not None and now - t0 >= self.TOUCH_SEC:
                e["last"] = now
                self._save()

    # ---------- чистка и список ----------

    def prune(self) -> int:
        """Убрать станции, которые не включали дольше keep_days; старые записи — тоже."""
        now = time.time()
        cutoff = now - self.keep_days * DAY
        removed = 0
        with self._lock:
            for url in list(self._stats):
                e = self._stats[url]
                if self._active.get(url):
                    continue                    # сейчас играет — не трогаем
                if float(e.get("last") or 0) < cutoff:
                    del self._stats[url]
                    removed += 1
                    log.info("popular radio: «%s» не включали более %d дн. — убрана",
                             e.get("name"), self.keep_days)
                    continue
                e["sessions"] = [s for s in e.get("sessions", []) if float(s.get("ts") or 0) >= cutoff]
            if removed:
                self._save()
        return removed

    def top(self) -> List[dict]:
        """Станции списка по популярности: [{url, name, count, sec, last}, ...]."""
        self.prune()
        cutoff = time.time() - self.keep_days * DAY
        rows = []
        with self._lock:
            for url, e in self._stats.items():
                win = [s for s in e.get("sessions", []) if float(s.get("ts") or 0) >= cutoff]
                rows.append({
                    "url": url, "name": e.get("name") or url,
                    "count": len(win), "sec": sum(int(s.get("sec") or 0) for s in win),
                    "last": float(e.get("last") or 0),
                })
        rows.sort(key=lambda r: (-r["count"], -r["sec"], -r["last"]))
        return rows

    def count(self) -> int:
        with self._lock:
            return len(self._stats)

    # ---------- файл ----------

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            stats = data.get("stations") if isinstance(data, dict) else None
            if isinstance(stats, dict):
                for url, e in stats.items():
                    if isinstance(e, dict):
                        e.setdefault("sessions", [])
                        e.setdefault("last", 0)
                        self._stats[url] = e
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("radio_popular: не удалось прочитать %s: %s", self.path, e)

    def _save(self) -> None:
        """Записать JSON (временный файл + replace; запасной вариант — запись «на месте»)."""
        payload = json.dumps({"version": 1, "stations": self._stats}, ensure_ascii=False)
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, self.path)
            return
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(payload)
        except OSError as e:
            if not self._warned:
                self._warned = True
                log.warning("radio_popular: нет записи в %s (%s) — список не переживёт перезапуск",
                            self.path, e)
