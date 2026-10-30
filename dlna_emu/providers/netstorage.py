"""Провайдер net — «Сетевые хранилища» (WebDAV / FTP / FTPS)."""
from __future__ import annotations

import logging
from typing import List
from urllib.parse import quote, unquote

from .base import ServiceProvider
from ..content_ids import *  # noqa: F401,F403
from ..files_lib import mime_for
from ..netstorage import NetStorageError, norm_rel

log = logging.getLogger("dlna_emu.content.net")


class NetStorageProvider(ServiceProvider):
    """
    id объектов:
      net                       — список хранилищ
      net:<sid>                 — корень хранилища
      net:<sid>:<путь>          — папка (путь в percent-encoding, '/' как есть)
      netf:<sid>:<путь>         — файл
    URL потока: /stream/net/<sid>/<путь> (слэши настоящие — ресиверы так надёжнее
    и расширение файла остаётся в конце URL).
    """

    root_id = "net"

    def handles(self, object_id: str) -> bool:
        return object_id == "net" or object_id.startswith("net:")

    def _mgr(self):
        return getattr(self.t, "netstorage", None)

    def children(self, object_id: str) -> List[str]:
        mgr = self._mgr()
        if mgr is None:
            return []

        if object_id == NET:
            return [
                self.t._container(f"net:{st.sid}", NET, st.name, 0)
                for st in mgr.all()
            ]

        parts = object_id.split(":", 2)  # net, sid, [path]
        st = mgr.get(parts[1]) if len(parts) > 1 else None
        if st is None:
            return []
        rel = norm_rel(unquote(parts[2])) if len(parts) > 2 else ""

        try:
            entries = st.list_dir(rel)
        except NetStorageError as e:
            log.warning("%s /%s: %s", st.name, rel, e)
            # на ресивере видно причину, а не «пустая папка»
            return [self.t._container(f"neterr:{st.sid}", object_id, f"⚠ {e}"[:90], 0)]
        except Exception as e:
            log.warning("%s /%s: %s", st.name, rel, e)
            return [self.t._container(f"neterr:{st.sid}", object_id, f"⚠ Ошибка: {e}"[:90], 0)]

        out: List[str] = []
        for e in entries:
            child = f"{rel}/{e.name}" if rel else e.name
            enc = quote(child, safe="/")
            if e.is_dir:
                out.append(self.t._container(f"net:{st.sid}:{enc}", object_id, e.name, 0))
            else:
                url = f"{self.t.base_url}/stream/net/{st.sid}/{enc}"
                out.append(
                    self.t._item(
                        f"netf:{st.sid}:{enc}", object_id, e.name, url, mime=mime_for(e.name)
                    )
                )
        return out
