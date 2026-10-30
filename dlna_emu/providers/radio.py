"""Провайдер radio — ветка ContentDirectory."""
from __future__ import annotations

import logging
from typing import List

from .base import ServiceProvider
from ..content_ids import *  # noqa: F401,F403

log = logging.getLogger("dlna_emu.content.radio")


class RadioProvider(ServiceProvider):
    root_id = "radio"

    def handles(self, object_id: str) -> bool:
        return object_id == "radio" or object_id.startswith("radio:")

    def children(self, object_id: str) -> List[str]:
        if object_id == RADIO:
            # Только плейлисты из m3u — история «Текущий плейлист» для радио не нужна
            items = []
            for pl in self.t.radio.list_playlists():
                items.append(self.t._container(f"radio:{pl.id}", RADIO, pl.title, len(pl.stations)))
            return items
        if object_id.startswith("radio:"):
            parts = object_id.split(":")
            if len(parts) == 2:
                pl = self.t.radio.get_playlist(parts[1])
                if not pl:
                    return []
                out = []
                for i, st in enumerate(pl.stations):
                    oid = f"radio:{parts[1]}:{i}"
                    url = f"{self.t.base_url}/stream/radio/{parts[1]}/{i}"
                    title = f"{i + 1}. {st.name}"
                    out.append(self.t._item(oid, object_id, title, url))
                return out


        return []

    def _radio_count(self) -> int:
        return len(self.t.radio.list_playlists())


