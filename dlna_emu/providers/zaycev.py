"""Провайдер zaycev — ветка ContentDirectory (zaycev.net).

Структура (всё подгружается с сайта по требованию, как каталоги Яндекс.Музыки):

  Zaycev.net
  ├─ * Текущий плейлист        — возобновляемый (как у остальных стримингов)
  ├─ 🔍 Мои поиски             — артисты, добавленные из веб-поиска
  ├─ 🆕 Новинки
  ├─ 🏆 Топ Zaycev.net         → За день / За неделю / За месяц
  ├─ Жанры                     → Жанры (все, с поджанрами) / Сборники по жанрам
  │                               жанр → Популярные / Новинки / Сборники жанра / поджанры …
  └─ Подборки                  → категория → подборка → треки

Длинные списки режутся на «Страницы» (zaycev.tracks_per_chunk треков).
"""
from __future__ import annotations

import logging
from typing import Dict, List, Tuple

from .base import ServiceProvider
from ..content_ids import *  # noqa: F401,F403
from ..zaycev_client import TOP_PERIODS, ZyTrack

log = logging.getLogger("dlna_emu.content.zaycev")

MAX_CHUNK_CONTAINERS = 80


class ZaycevProvider(ServiceProvider):
    root_id = "zaycev"

    def __init__(self, tree):
        super().__init__(tree)
        # slug/url → человекочитаемое название (чтобы не показывать id в «Текущем плейлисте»)
        self._titles: Dict[str, str] = {}

    # ------------------------------------------------------------ вход

    def children(self, object_id: str) -> List[str]:
        zy = self.t.zaycev
        if not zy or not zy.enabled:
            return []
        try:
            return self._children(object_id)
        except Exception as e:
            log.warning("zaycev browse %s: %s", object_id, e)
            return []

    def _children(self, object_id: str) -> List[str]:
        t = self.t
        zy = t.zaycev

        if object_id == ZAYCEV:
            items: List[str] = []
            n_cur = t.current_playlist_count("zaycev")
            if n_cur:
                items.append(t._container("zaycev:current", ZAYCEV, "* Текущий плейлист", n_cur))
            items.extend([
                t._container(ZY_SAVED, ZAYCEV, "🔍 Мои поиски", len(t.zy_saved_searches())),
                t._container(ZY_FAV, ZAYCEV, "⭐ Избранное", len(t.site_favorites("zaycev"))),
                t._container(ZY_NEW, ZAYCEV, "🆕 Новинки", 0),
                t._container(ZY_TOP, ZAYCEV, "🏆 Топ Zaycev.net", len(TOP_PERIODS)),
                t._container(ZY_GENRES, ZAYCEV, "Жанры", 0),
                t._container(ZY_SETS, ZAYCEV, "Подборки", 0),
            ])
            return items

        if object_id == "zaycev:current" or object_id.startswith("zaycev:current:"):
            return t._current_children("zaycev", object_id)

        # ---- мои поиски (артисты) ----
        if object_id in (ZY_SAVED, ZY_FAV):
            out = []
            src = t.site_favorites("zaycev") if object_id == ZY_FAV else t.zy_saved_searches()
            for s in src:
                if s.get("type") == "artist":
                    out.append(t._container(
                        f"{ZY_ARTIST_PREFIX}{s['artist_id']}", object_id, f"👤 {s['title']}", 0,
                    ))
            return out

        if object_id.startswith(ZY_ARTIST_PREFIX):
            aid = object_id[len(ZY_ARTIST_PREFIX):]
            return self._paginated("artist", aid, object_id, self._artist_title(aid))

        # ---- новинки / топ ----
        if object_id == ZY_NEW:
            return self._paginated("new", "_", object_id, "Zaycev · Новинки")

        if object_id == ZY_TOP:
            return [
                t._container(f"{ZY_LIST_PREFIX}top:{code}", ZY_TOP, title, 0)
                for code, title in TOP_PERIODS
            ]

        # ---- жанры ----
        if object_id == ZY_GENRES:
            cat = zy.genre_sets_category()
            return [
                t._container(ZY_GENRES_ALL, ZY_GENRES, "Жанры", 0),
                t._container(f"{ZY_SETCAT_PREFIX}{cat}", ZY_GENRES, "Сборники по жанрам", 0),
            ]

        if object_id == ZY_GENRES_ALL:
            out = []
            for slug, title in zy.list_genres():
                self._titles[f"genre:{slug}"] = title
                out.append(t._container(f"{ZY_GENRE_PREFIX}{slug}", object_id, title, 0))
            return out

        if object_id.startswith(ZY_GENRE_PREFIX):
            slug = object_id[len(ZY_GENRE_PREFIX):]
            title = self._titles.get(f"genre:{slug}") or zy.genre_title(slug)
            self._titles[f"genre:{slug}"] = title
            out = [
                t._container(f"{ZY_LIST_PREFIX}genre:{slug}", object_id, "🔥 Популярные", 0),
                t._container(f"{ZY_LIST_PREFIX}genrenew:{slug}", object_id, "🆕 Новинки", 0),
                t._container(f"{ZY_SETCAT_PREFIX}genre:{slug}", object_id, "Сборники жанра", 0),
            ]
            for sub, sub_title in zy.list_subgenres(slug):
                self._titles[f"genre:{sub}"] = f"{title} / {sub_title}"
                out.append(t._container(f"{ZY_GENRE_PREFIX}{sub}", object_id, sub_title, 0))
            return out

        # ---- подборки ----
        if object_id == ZY_SETS:
            out = []
            for tid, title in zy.list_musicset_categories():
                self._titles[f"setcat:{tid}"] = title
                out.append(t._container(f"{ZY_SETCAT_PREFIX}{tid}", ZY_SETS, title, 0))
            return out

        if object_id.startswith(ZY_SETCAT_PREFIX):
            cat = object_id[len(ZY_SETCAT_PREFIX):]
            return self._musicsets_container(cat, object_id)

        if object_id.startswith(ZY_SETPAGE_PREFIX):
            rest = object_id[len(ZY_SETPAGE_PREFIX):]
            if ":" not in rest:
                return []
            cat, chunk_s = rest.rsplit(":", 1)
            if not chunk_s.isdigit():
                return []
            return self._musicset_items(zy.get_musicsets_chunk(cat, int(chunk_s)), object_id)

        # ---- универсальные списки треков ----
        if object_id.startswith(ZY_LIST_PREFIX):
            rest = object_id[len(ZY_LIST_PREFIX):]
            if ":" not in rest:
                return []
            kind, key = rest.split(":", 1)
            return self._paginated(kind, key, object_id, self._list_title(kind, key))

        if object_id.startswith(ZY_PAGE_PREFIX):
            rest = object_id[len(ZY_PAGE_PREFIX):]
            if rest.count(":") < 2:
                return []
            head, chunk_s = rest.rsplit(":", 1)
            if not chunk_s.isdigit() or ":" not in head:
                return []
            kind, key = head.split(":", 1)
            tracks = zy.get_chunk(kind, key, int(chunk_s))
            title = f"{self._list_title(kind, key)} · стр.{int(chunk_s) + 1}"
            return self._track_items(tracks, object_id, title)

        return []

    # ------------------------------------------------------------ помощники

    def _artist_title(self, aid: str) -> str:
        for s in list(self.t.zy_saved_searches()) + list(self.t.site_favorites("zaycev")):
            if s.get("artist_id") == aid and s.get("title"):
                return f"Zaycev · {s['title']}"
        return f"Zaycev · артист {aid}"

    def _list_title(self, kind: str, key: str) -> str:
        if kind == "new":
            return "Zaycev · Новинки"
        if kind == "top":
            name = dict(TOP_PERIODS).get(key, key)
            return f"Zaycev · Топ {name.lower()}"
        if kind in ("genre", "genrenew"):
            name = self._titles.get(f"genre:{key}") or key
            return f"Zaycev · {name}" + (" (новинки)" if kind == "genrenew" else "")
        if kind == "mset":
            return f"Zaycev · {self._titles.get('mset:' + key) or key}"
        if kind == "artist":
            return self._artist_title(key)
        return f"Zaycev · {key}"

    def _paginated(self, kind: str, key: str, parent_id: str, title: str) -> List[str]:
        """Список треков: одна «Страница» — сразу треки, иначе контейнеры «Страница N»."""
        zy = self.t.zaycev
        try:
            _pc, _pp, chunks = zy.list_layout(kind, key)
        except Exception as e:
            log.warning("zaycev layout %s/%s: %s", kind, key, e)
            return []
        if chunks <= 0:
            return []
        if chunks == 1:
            return self._track_items(zy.get_chunk(kind, key, 0), parent_id, title)
        return [
            self.t._container(
                f"{ZY_PAGE_PREFIX}{kind}:{key}:{i}", parent_id, f"Страница {i + 1}",
                zy.tracks_per_chunk,
            )
            for i in range(min(chunks, MAX_CHUNK_CONTAINERS))
        ]

    def _musicsets_container(self, cat: str, parent_id: str) -> List[str]:
        zy = self.t.zaycev
        try:
            _pc, _pp, chunks = zy.musicsets_layout(cat)
        except Exception as e:
            log.warning("zaycev musicsets layout %s: %s", cat, e)
            return []
        if chunks <= 0:
            return []
        if chunks == 1:
            return self._musicset_items(zy.get_musicsets_chunk(cat, 0), parent_id)
        return [
            self.t._container(
                f"{ZY_SETPAGE_PREFIX}{cat}:{i}", parent_id, f"Страница {i + 1}",
                zy.tracks_per_chunk,
            )
            for i in range(min(chunks, MAX_CHUNK_CONTAINERS))
        ]

    def _musicset_items(self, sets, parent_id: str) -> List[str]:
        out = []
        for ms in sets:
            self._titles[f"mset:{ms.url}"] = ms.title
            out.append(self.t._container(
                f"{ZY_LIST_PREFIX}mset:{ms.url}", parent_id, ms.title, ms.track_count,
            ))
        return out

    def _track_items(self, tracks: List[ZyTrack], parent_id: str, title_hint: str = "") -> List[str]:
        out: List[str] = []
        dicts = []
        base = self.t.base_url
        for tr in tracks:
            oid = f"{ZY_TR_PREFIX}{tr.id}"
            url = f"{base}/stream/zaycev/{tr.id}"
            title = f"{tr.artist} — {tr.title}" if tr.artist else tr.title
            out.append(self.t._item(
                oid, parent_id, title, url,
                mime="audio/mpeg", artist=tr.artist, duration=tr.duration, album=tr.album,
            ))
            dicts.append({
                "oid": oid, "title": title, "url": url, "mime": "audio/mpeg",
                "artist": tr.artist or "", "duration": tr.duration or 0,
                "album": tr.album or "",
            })
        if dicts:
            # запоминаем просмотренный список: при play он станет «Текущим плейлистом»
            self.t.set_current_playlist("zaycev", title_hint or "Zaycev", dicts)
        return out
