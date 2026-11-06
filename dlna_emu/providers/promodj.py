"""Провайдер promodj — ветка ContentDirectory."""
from __future__ import annotations

import logging
from typing import List

from .base import ServiceProvider
from ..content_ids import *  # noqa: F401,F403
from ..promodj_client import PdTrack

log = logging.getLogger("dlna_emu.content.promodj")


class PromoDjProvider(ServiceProvider):
    root_id = "promodj"

    def children(self, object_id: str) -> List[str]:
        if object_id == PROMODJ or object_id.startswith("promodj:"):
            return self._promodj_children(object_id)
        return []

    def _promodj_children(self, object_id: str) -> List[str]:
        if not self.t.promodj or not self.t.promodj.enabled:
            return []

        if object_id == PROMODJ:
            items = []
            try:
                n_cur = self.t.current_playlist_count("promodj")
                if n_cur:
                    items.append(self.t._container("promodj:current", PROMODJ, "* Текущий плейлист", n_cur))
            except Exception as e:
                log.warning("promodj current: %s", e)
            try:
                n_saved = len(self.t.pd_saved_searches())
                if n_saved:
                    items.append(self.t._container(PD_SAVED, PROMODJ, "🔍 Мои поиски", n_saved))
                n_fav = len(self.t.site_favorites("promodj"))
                if n_fav:
                    items.append(self.t._container(PD_FAV, PROMODJ, "⭐ Избранное", n_fav))
            except Exception as e:
                log.warning("promodj saved: %s", e)
            try:
                n_feat = len(self.t.promodj.list_featured_sections())
            except Exception as e:
                log.warning("promodj featured: %s", e)
                n_feat = 1
            items.append(self.t._container(PD_FEATURED, PROMODJ, "Featured", n_feat))
            items.append(self.t._container(PD_MUSIC, PROMODJ, "Музыка", 10))
            try:
                n_top = len(self.t.promodj.list_top100_sections())
            except Exception as e:
                log.warning("promodj top100: %s", e)
                n_top = 1
            items.append(self.t._container(PD_TOP100, PROMODJ, "TOP 100", n_top))
            return items

        if object_id == "promodj:current" or object_id.startswith("promodj:current:"):
            return self.t._current_children("promodj", object_id)

        if object_id in (PD_SAVED, PD_FAV):
            out = []
            src = self.t.site_favorites("promodj") if object_id == PD_FAV else self.t.pd_saved_searches()
            for s in src:
                slug = s.get("slug") or s.get("id") or ""
                title = s.get("title") or slug
                out.append(self.t._container(f"{PD_USER_PREFIX}{slug}", object_id, title, 1))
            return out

        if object_id.startswith(PD_USER_PREFIX):
            slug = object_id[len(PD_USER_PREFIX):]
            tracks = self.t.promodj.get_user_tracks(slug)
            return self._pd_track_items(tracks, object_id, title_hint=slug)

        if object_id == PD_FEATURED:
            out = []
            for slug, title, _path in self.t.promodj.list_featured_sections():
                out.append(self.t._container(f"{PD_FEAT_PREFIX}{slug}", PD_FEATURED, title, 1))
            return out

        if object_id.startswith(PD_FEAT_PREFIX):
            slug = object_id[len(PD_FEAT_PREFIX):]
            path = next((p for s, t, p in self.t.promodj.list_featured_sections() if s == slug), None)
            if not path:
                return []
            return self._pd_paginated(path, f"Featured · {slug}", object_id, kind="feat", key=slug)

        if object_id == PD_MUSIC:
            out = []
            for slug, title, _path in self.t.promodj.list_music_types():
                out.append(self.t._container(f"{PD_TYPE_PREFIX}{slug}", PD_MUSIC, title, 1))
            out.append(self.t._container(PD_GENRES, PD_MUSIC, "Жанры", len(self.t.promodj.list_genres()) or 1))
            return out

        if object_id.startswith(PD_TYPE_PREFIX):
            slug = object_id[len(PD_TYPE_PREFIX):]
            title = next((t for s, t, p in self.t.promodj.list_music_types() if s == slug), slug)
            return [
                self.t._container(f"{PD_TALL_PREFIX}{slug}", object_id, f"🔥 Все: {title}", 1),
                self.t._container(f"{PD_TGEN_PREFIX}{slug}", object_id, "Жанры", 1),
            ]

        if object_id.startswith(PD_TALL_PREFIX):
            slug = object_id[len(PD_TALL_PREFIX):]
            path = self._pd_path_for("type", slug)
            if not path:
                return []
            title = next((t for s, t, p in self.t.promodj.list_music_types() if s == slug), slug)
            return self._pd_paginated(path, title, object_id, kind="type", key=slug)

        if object_id.startswith(PD_TGEN_PREFIX):
            slug = object_id[len(PD_TGEN_PREFIX):]
            section = self._pd_section(slug)
            return [
                self.t._container(f"{PD_TG_PREFIX}{slug}/{g}", object_id, title, 1)
                for g, title in self.t.promodj.list_genres(section)
            ]

        if object_id.startswith(PD_TG_PREFIX):
            key = object_id[len(PD_TG_PREFIX):]
            path = self._pd_path_for("tg", key)
            if not path:
                return []
            slug, g = key.split("/", 1)
            title = next((t for s, t in self.t.promodj.list_genres(self._pd_section(slug)) if s == g), g)
            return self._pd_paginated(path, title, object_id, kind="tg", key=key)

        if object_id == PD_GENRES:
            out = []
            for slug, title in self.t.promodj.list_genres():
                out.append(self.t._container(f"{PD_GENRE_PREFIX}{slug}", PD_GENRES, title, 1))
            return out

        if object_id.startswith(PD_GENRE_PREFIX):
            slug = object_id[len(PD_GENRE_PREFIX):]
            path = f"/music/{slug}"
            title = next((t for s, t in self.t.promodj.list_genres() if s == slug), slug)
            return self._pd_paginated(path, title, object_id, kind="genre", key=slug)

        if object_id == PD_TOP100:
            return [
                self.t._container(f"{PD_TOPT_PREFIX}{slug}", PD_TOP100, title, 1)
                for slug, title in self.t.promodj.list_top100_types()
            ]

        if object_id.startswith(PD_TOPT_PREFIX):
            typ = object_id[len(PD_TOPT_PREFIX):]
            return [
                self.t._container(f"{PD_TOPP_PREFIX}{typ}/{per}", object_id, title, 1)
                for per, title in self.t.promodj.list_top100_periods(typ)
            ]

        if object_id.startswith(PD_TOPP_PREFIX):
            typ, per = self._pd_split_top(object_id[len(PD_TOPP_PREFIX):])
            styles = self.t.promodj.list_top100_styles(typ, per)
            tail = f"{typ}/{per}"
            if not styles:
                return self._pd_paginated(f"/top100/{tail}", f"TOP 100 · {tail}", object_id,
                                          kind="top", key=tail)
            out = [self.t._container(f"{PD_TOP_PREFIX}{tail}", object_id, "🔥 Все стили", 1)]
            for slug, title in styles:
                out.append(self.t._container(f"{PD_TOP_PREFIX}{typ}/{slug}/{per}", object_id, title, 1))
            return out

        if object_id.startswith(PD_TOP_PREFIX):
            tail = self._pd_norm_top(object_id[len(PD_TOP_PREFIX):])
            return self._pd_paginated(f"/top100/{tail}", f"TOP 100 · {tail}", object_id,
                                      kind="top", key=tail)

        # promodj:page:{kind}:{key}:{chunk}  — chunk 0-based (как akniga secpage)
        if object_id.startswith(PD_PAGE_PREFIX):
            rest = object_id[len(PD_PAGE_PREFIX):]
            parts = rest.split(":")
            if len(parts) < 3:
                return []
            kind, key, chunk_s = parts[0], parts[1], parts[2]
            try:
                chunk = int(chunk_s)
            except ValueError:
                return []
            path = self._pd_path_for(kind, key)
            if not path:
                return []
            tracks = self.t.promodj.get_chunk(path, chunk)
            title = key
            if kind == "genre":
                title = next((t for s, t in self.t.promodj.list_genres() if s == key), key)
            elif kind == "type":
                title = next((t for s, t, p in self.t.promodj.list_music_types() if s == key), key)
            elif kind == "feat":
                title = next((t for s, t, p in self.t.promodj.list_featured_sections() if s == key), key)
            elif kind == "top":
                title = f"TOP 100 · {key}"
            elif kind == "tg":
                title = key.split("/", 1)[-1]
            return self._pd_track_items(
                tracks, object_id, title_hint=f"{title} · стр.{chunk + 1}"
            )

        # promodj:srch:{query} or promodj:srch:{query}:{chunk}
        if object_id.startswith(PD_SRCH_PREFIX):
            rest = object_id[len(PD_SRCH_PREFIX):]
            # query may contain almost anything except we use last :N as chunk
            if ":" in rest and rest.rsplit(":", 1)[-1].isdigit():
                query, chunk_s = rest.rsplit(":", 1)
                chunk = int(chunk_s)
            else:
                query, chunk = rest, -1  # -1 = show page list or single chunk
            if chunk < 0:
                _tp, _pp, chunks = self.t.promodj.search_layout(query)
                if chunks <= 1:
                    tracks = self.t.promodj.search_chunk(query, 0)
                    return self._pd_track_items(
                        tracks, object_id, title_hint=f"Поиск: {query}"
                    )
                bpc = self.t.promodj.tracks_per_chunk
                out = []
                for i in range(min(chunks, 80)):
                    out.append(self.t._container(
                        f"{PD_SRCH_PREFIX}{query}:{i}",
                        f"{PD_SRCH_PREFIX}{query}",
                        f"Страница {i + 1}",
                        bpc,
                    ))
                return out
            tracks = self.t.promodj.search_chunk(query, chunk)
            return self._pd_track_items(
                tracks, object_id, title_hint=f"Поиск: {query} · стр.{chunk + 1}"
            )

        return []


    def _pd_section(self, type_slug: str) -> str:
        """Путь раздела для жанров: all → music, tracks → tracks …"""
        return "music" if type_slug in ("all", "") else type_slug

    @staticmethod
    def _pd_split_top(rest: str):
        typ, _, per = rest.partition("/")
        return typ, (per or "1y")

    @staticmethod
    def _pd_norm_top(tail: str) -> str:
        """Старые id (promodj:top:tracks) → tracks/1y."""
        return tail if "/" in tail else f"{tail}/1y"

    def _pd_path_for(self, kind: str, key: str) -> str:
        if kind == "feat":
            return next((p for s, t, p in self.t.promodj.list_featured_sections() if s == key), "")
        if kind == "type":
            return next((p for s, t, p in self.t.promodj.list_music_types() if s == key), "")
        if kind == "genre":
            return f"/music/{key}"
        if kind == "tg":
            if "/" not in key:
                return ""
            slug, g = key.split("/", 1)
            return f"/{self._pd_section(slug)}/{g}"
        if kind == "top":
            return f"/top100/{self._pd_norm_top(key)}"
        return ""


    def _pd_paginated(
        self, path: str, title: str, parent_id: str, kind: str, key: str
    ) -> List[str]:
        """Пагинация как akniga: чанки по tracks_per_chunk (≈500) треков."""
        try:
            _total_pages, _per_page, chunks = self.t.promodj.section_layout(path)
        except Exception as e:
            log.warning("promodj layout %s: %s", path, e)
            return []
        if chunks <= 0:
            return []
        if chunks == 1:
            tracks = self.t.promodj.get_chunk(path, 0)
            return self._pd_track_items(tracks, parent_id, title_hint=title)
        bpc = self.t.promodj.tracks_per_chunk
        out = []
        for i in range(min(chunks, 80)):
            out.append(self.t._container(
                f"{PD_PAGE_PREFIX}{kind}:{key}:{i}",
                parent_id,
                f"Страница {i + 1}",
                bpc,
            ))
        return out


    def _pd_track_items(
        self, tracks: List[PdTrack], parent_id: str, title_hint: str = ""
    ) -> List[str]:
        out: List[str] = []
        track_dicts = []
        for tr in tracks:
            oid = f"{PD_TR_PREFIX}{tr.id}"
            url = f"{self.t.base_url}/stream/promodj/{tr.id}"
            title = tr.title or f"Track {tr.id}"
            out.append(self.t._item(
                oid, parent_id, title, url,
                mime="audio/mpeg",
                artist=tr.artist or "",
                duration=int(tr.duration or 0),
                album=tr.styles or "",
            ))
            track_dicts.append({
                "oid": oid, "title": title, "url": url,
                "mime": "audio/mpeg", "artist": tr.artist or "",
                "duration": int(tr.duration or 0), "album": tr.styles or "",
            })
        if track_dicts:
            self.t.set_current_playlist("promodj", title_hint or "PromoDJ", track_dicts)
        return out


