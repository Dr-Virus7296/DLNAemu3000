"""Базовый провайдер сервиса для ContentTree."""
from __future__ import annotations
from typing import List, TYPE_CHECKING

if TYPE_CHECKING:
    from ..content import ContentTree


class ServiceProvider:
    """Один сервис (Deezer, Zvuk, …) с методом children(object_id)."""

    # id корня сервиса, например "deezer"
    root_id: str = ""

    def __init__(self, tree: "ContentTree"):
        self.t = tree

    def handles(self, object_id: str) -> bool:
        rid = self.root_id
        if not rid:
            return False
        return object_id == rid or object_id.startswith(rid + ":")

    def children(self, object_id: str) -> List[str]:
        raise NotImplementedError
