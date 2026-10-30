"""Провайдер drivemusic — ветка ContentDirectory."""
from __future__ import annotations

import logging
from typing import List

from .base import ServiceProvider
from ..content_ids import *  # noqa: F401,F403

log = logging.getLogger("dlna_emu.content.drivemusic")


class DriveMusicProvider(ServiceProvider):
    root_id = "drivemusic"

    def children(self, object_id: str) -> List[str]:
        if object_id == DRIVEMUSIC:
            groups = self.t.drivemusic.list_playlist_groups(fetch=False)
            items = []
            n_cur = self.t.current_playlist_count("drivemusic")
            if n_cur:
                items.append(self.t._container("drivemusic:current", DRIVEMUSIC, "* Текущий плейлист", n_cur))
            items.extend([
                self.t._container(DM_SAVED, DRIVEMUSIC, "🔍 Мои поиски",
                                len(self.t.dm_saved_searches())),
                self.t._container(DM_NEW, DRIVEMUSIC, "🔥 Новинки", 0),
                self.t._container(DM_HITS, DRIVEMUSIC, "🏆 Хиты (чарт)",
                                len(self.t.drivemusic.list_hits(fetch=False))),
                self.t._container(DM_BEST, DRIVEMUSIC, "⭐ Лучшая",
                                len(self.t.drivemusic.list_best_music(fetch=False))),
                self.t._container(DM_GENRES, DRIVEMUSIC, "Жанры",
                                len(self.t.drivemusic.list_genres(fetch=False))),
                self.t._container(DM_PLAYLISTS, DRIVEMUSIC, "Подборки",
                                len(groups)),
            ])
            return items
        if object_id == "drivemusic:current" or object_id.startswith("drivemusic:current:"):
            return self.t._current_children("drivemusic", object_id)

        if object_id == DM_SAVED:
            with self.t._dm_saved_lock:
                saved = list(self.t._dm_saved)
            out = []
            for s in saved:
                if s.get("type") == "artist":
                    out.append(self.t._container(
                        f"{DM_ARTIST_PREFIX}{s['artist_id']}",
                        DM_SAVED,
                        f"👤 {s['title']}",
                        0,
                    ))
            return out

        if object_id.startswith(DM_ARTIST_PREFIX):
            slug = object_id[len(DM_ARTIST_PREFIX):]
            return self._dm_artist_tracks(slug, object_id)

        if object_id == DM_NEW:
            return self._dm_tracks("novinki_muzyki", object_id)

        if object_id == DM_HITS:
            return [
                self.t._container(f"{DM_HIT_PREFIX}{slug}", DM_HITS, title, 0)
                for slug, title in self.t.drivemusic.list_hits()
            ]

        if object_id == DM_BEST:
            return [
                self.t._container(f"{DM_BEST_PREFIX}{slug}", DM_BEST, title, 0)
                for slug, title in self.t.drivemusic.list_best_music()
            ]

        if object_id.startswith(DM_HIT_PREFIX):
            slug = object_id[len(DM_HIT_PREFIX):]
            return self._dm_tracks(slug, object_id)

        if object_id.startswith(DM_BEST_PREFIX):
            slug = object_id[len(DM_BEST_PREFIX):]
            return self._dm_tracks(slug, object_id)

        if object_id == DM_GENRES:
            return [
                self.t._container(f"{DM_GENRE_PREFIX}{slug}", DM_GENRES, title, 0)
                for slug, title in self.t.drivemusic.list_genres()
            ]

        if object_id == DM_PLAYLISTS:
            return [
                self.t._container(
                    f"{DM_PLGROUP_PREFIX}{slug}",
                    DM_PLAYLISTS,
                    title,
                    len(self.t.drivemusic.list_playlist_subs(slug, fetch=False)),
                )
                for slug, title in self.t.drivemusic.list_playlist_groups()
            ]

        if object_id.startswith(DM_PLGROUP_PREFIX):
            group_slug = object_id[len(DM_PLGROUP_PREFIX):]
            subs = self.t.drivemusic.list_playlist_subs(group_slug)
            return [
                self.t._container(f"{DM_PL_PREFIX}{sub_slug}", object_id, sub_title, 0)
                for sub_slug, sub_title in subs
            ]

        if object_id.startswith(DM_GENRE_PREFIX):
            slug = object_id[len(DM_GENRE_PREFIX):]
            return self._dm_tracks(slug, object_id)

        if object_id.startswith(DM_PL_PREFIX):
            slug = object_id[len(DM_PL_PREFIX):]
            return self._dm_tracks(slug, object_id)

        return []

    def _dm_tracks(self, slug: str, parent_id: str) -> List[str]:
        out: List[str] = []
        track_dicts = []
        try:
            tracks = self.t.drivemusic.get_genre_tracks(slug, page=1, limit=80)
            for tr in tracks:
                oid = f"{DM_TR_PREFIX}{tr.id}"
                url = f"{self.t.base_url}/stream/drivemusic/{tr.id}"
                title = f"{tr.artist} — {tr.title}" if tr.artist else tr.title
                out.append(
                    self.t._item(
                        oid,
                        parent_id,
                        title,
                        url,
                        mime="audio/mpeg",
                        artist=tr.artist,
                        duration=tr.duration,
                    )
                )
                track_dicts.append({
                    "oid": oid, "title": title, "url": url,
                    "mime": "audio/mpeg", "artist": tr.artist or "",
                    "duration": tr.duration or 0, "album": "",
                })
            if track_dicts:
                self.t.set_current_playlist("drivemusic", f"Жанр {slug}", track_dicts)
        except Exception as e:
            log.warning("drivemusic list %s: %s", slug, e)
        return out


    def _dm_artist_tracks(self, slug: str, parent_id: str) -> List[str]:
        out: List[str] = []
        track_dicts = []
        try:
            tracks = self.t.drivemusic.get_artist_tracks(slug, limit=200)
            for tr in tracks:
                oid = f"{DM_TR_PREFIX}{tr.id}"
                url = f"{self.t.base_url}/stream/drivemusic/{tr.id}"
                title = f"{tr.artist} — {tr.title}" if tr.artist else tr.title
                out.append(
                    self.t._item(
                        oid,
                        parent_id,
                        title,
                        url,
                        mime="audio/mpeg",
                        artist=tr.artist,
                        duration=tr.duration,
                    )
                )
                track_dicts.append({
                    "oid": oid, "title": title, "url": url,
                    "mime": "audio/mpeg", "artist": tr.artist or "",
                    "duration": tr.duration or 0, "album": "",
                })
            if track_dicts:
                self.t.set_current_playlist("drivemusic", f"Артист {slug}", track_dicts)
        except Exception as e:
            log.warning("drivemusic artist %s: %s", slug, e)
        return out


