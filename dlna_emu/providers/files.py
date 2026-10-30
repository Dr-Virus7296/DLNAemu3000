"""Провайдер files — ветка ContentDirectory."""
from __future__ import annotations

import logging
from typing import List

from .base import ServiceProvider
from urllib.parse import quote

from ..content_ids import *  # noqa: F401,F403
from ..files_lib import mime_for

log = logging.getLogger("dlna_emu.content.files")


class FilesProvider(ServiceProvider):
    root_id = "files"

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
            pl = self.t.files.get_playlist(pid)
            if not pl:
                return []
            out = []
            for i, tr in enumerate(pl.tracks):
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
