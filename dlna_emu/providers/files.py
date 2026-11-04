"""Провайдер files — ветка ContentDirectory."""
from __future__ import annotations

import logging
import random
import threading
import time
from typing import Dict, List, Tuple

from .base import ServiceProvider
from urllib.parse import quote

from ..content_ids import *  # noqa: F401,F403
from ..files_lib import mime_for
from ..shuffle import artists_for_tracks, repeat_stats, smart_shuffle

log = logging.getLogger("dlna_emu.content.files")


SHUFFLE_SUFFIX = ":shuffle"
SHUFFLE_TITLE = "🔀 Перемешать плейлист"
# Повторный вход в «перемешанный» плейлист раньше этого срока (сек) отдаёт тот же порядок:
# ресиверы иногда дёргают Browse(0) дважды подряд (счётчик + список).
SHUFFLE_DEBOUNCE = 3.0


class FilesProvider(ServiceProvider):
    root_id = "files"

    def __init__(self, tree):
        super().__init__(tree)
        self._shuffle_lock = threading.Lock()
        # pid -> (время, число треков, порядок индексов)
        self._shuffles: Dict[str, Tuple[float, int, List[int]]] = {}

    def _shuffled_order(self, pid: str, pl) -> List[int]:
        """Порядок для «перемешанного» плейлиста.

        Новый порядок — при входе в папку (StartingIndex = 0); подгрузка следующих
        страниц (StartingIndex > 0) и служебные вызовы получают тот же снимок.
        """
        n = len(pl.tracks)
        start = getattr(self.t._request_tls, "start_index", None)
        now = time.time()
        with self._shuffle_lock:
            cur = self._shuffles.get(pid)
            fresh_needed = (
                cur is None
                or cur[1] != n
                or (start == 0 and now - cur[0] > SHUFFLE_DEBOUNCE)
            )
            if not fresh_needed:
                return cur[2]
        artists = artists_for_tracks([(tr.name, tr.path) for tr in pl.tracks])
        order = smart_shuffle(artists, random.Random())
        adj, skip = repeat_stats(artists, order)
        log.info("Перемешан плейлист %s: %d треков, повторов подряд=%d, через один=%d",
                 pid, n, adj, skip)
        with self._shuffle_lock:
            if len(self._shuffles) > 50:
                self._shuffles.pop(min(self._shuffles, key=lambda k: self._shuffles[k][0]))
            self._shuffles[pid] = (now, n, order)
        return order

    def handles(self, object_id: str) -> bool:
        return (
            object_id in ("files", "localpl")
            or object_id.startswith("files:")
            or object_id.startswith("localpl:")
        )

    def children(self, object_id: str) -> List[str]:
        if object_id == FILES and self.t.files:
            dirs, files = self.t.files.list_dir("")
            out = []
            for name, rel in dirs:
                out.append(self.t._container(f"files:{rel}", FILES, name, 0))
            for name, rel in files:
                url = f"{self.t.base_url}/stream/file/{rel}"
                out.append(self.t._item(f"file:{rel}", FILES, name, url, mime=mime_for(rel)))
            return out

        if object_id.startswith("files:") and self.t.files:
            rel = object_id.split(":", 1)[1]
            dirs, files = self.t.files.list_dir(rel)
            out = []
            for name, child in dirs:
                out.append(self.t._container(f"files:{child}", object_id, name, 0))
            for name, child in files:
                url = f"{self.t.base_url}/stream/file/{child}"
                out.append(
                    self.t._item(f"file:{child}", object_id, name, url, mime=mime_for(child))
                )
            return out

        if object_id == LOCALPL and self.t.files:
            return [
                self.t._container(f"localpl:{pl.id}", LOCALPL, pl.title, len(pl.tracks))
                for pl in self.t.files.discover_playlists()
            ]
        if object_id.startswith("localpl:") and self.t.files:
            pid = object_id.split(":", 1)[1]
            shuffled = pid.endswith(SHUFFLE_SUFFIX)
            if shuffled:
                pid = pid[: -len(SHUFFLE_SUFFIX)]
            pl = self.t.files.get_playlist(pid)
            if not pl:
                return []
            indices = self._shuffled_order(pid, pl) if shuffled else range(len(pl.tracks))
            out = []
            if not shuffled and pl.tracks:
                # опция внутри плейлиста: первая строка — перемешанная версия
                out.append(self.t._container(
                    f"localpl:{pid}{SHUFFLE_SUFFIX}", object_id, SHUFFLE_TITLE, len(pl.tracks),
                ))
            for i in indices:
                tr = pl.tracks[i]
                enc = quote(tr.path, safe="")
                url = f"{self.t.base_url}/stream/local/{enc}"
                out.append(
                    self.t._item(
                        f"localpl:{pid}:{i}",
                        object_id,
                        tr.name,
                        url,
                        mime=mime_for(tr.path),
                    )
                )
            return out

        return []
