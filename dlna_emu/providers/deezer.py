"""Провайдер deezer — ветка ContentDirectory."""
from __future__ import annotations

import logging
from typing import List

from .base import ServiceProvider
from ..content_ids import *  # noqa: F401,F403

log = logging.getLogger("dlna_emu.content.deezer")


class DeezerProvider(ServiceProvider):
    root_id = "deezer"

    def children(self, object_id: str) -> List[str]:
        if object_id == DEEZER:
            items = []
            n_cur = self.t.current_playlist_count("deezer")
            if n_cur:
                items.append(self.t._container("deezer:current", DEEZER, "* Текущий плейлист", n_cur))
            items.extend([
                self.t._container(DZ_SAVED, DEEZER, "🔍 Мои поиски",
                                len(self.t.saved_searches())),
                self.t._container(DZ_FLOW, DEEZER, "Flow", 2),
                self.t._container(DZ_MINE, DEEZER, "Мои плейлисты", 0),
                self.t._container(DZ_EXPLORE, DEEZER, "Подборки", 0),
            ])
            return items
        if object_id == "deezer:current" or object_id.startswith("deezer:current:"):
            return self.t._current_children("deezer", object_id)

        if object_id == DZ_FLOW:
            return [
                self.t._container(DZ_FLOW_MOODS, DZ_FLOW, "Настроения", 0),
                self.t._container(DZ_FLOW_GENRES, DZ_FLOW, "Жанры", 0),
            ]
        if object_id == DZ_FLOW_MOODS:
            moods, _ = self.t.deezer._flow_configs()
            return [
                self.t._container(
                    f"deezer:flowcfg:{m['id'] or 'default'}",
                    DZ_FLOW_MOODS,
                    m["title"],
                    FLOW_MAX_CACHE,
                )
                for m in moods
            ]
        if object_id == DZ_FLOW_GENRES:
            _, genres = self.t.deezer._flow_configs()
            return [
                self.t._container(
                    f"deezer:flowcfg:{g['id'] or 'default'}",
                    DZ_FLOW_GENRES,
                    g["title"],
                    FLOW_MAX_CACHE,
                )
                for g in genres
            ]
        if object_id.startswith("deezer:flowcfg:"):
            return []

        if object_id == DZ_SAVED:
            with self.t._saved_lock:
                saved = list(self.t._saved)
            out = []
            for s in saved:
                if s["type"] == "artist":
                    out.append(self.t._container(
                        f"deezer:saved:artist:{s['artist_id']}",
                        DZ_SAVED,
                        f"👤 {s['title']}",
                        0,
                    ))
                elif s["type"] == "album":
                    out.append(self.t._container(
                        f"deezer:saved:album:{s['album_id']}",
                        DZ_SAVED,
                        f"💿 {s['title']}",
                        0,
                    ))
            return out

        if object_id.startswith("deezer:saved:artist:"):
            artist_id = object_id.split(":")[-1]
            return self._artist_contents(artist_id, object_id)

        if object_id.startswith("deezer:saved:album:"):
            album_id = object_id.split(":")[-1]
            return self._album_contents(album_id, object_id)

        if object_id.startswith("deezer:artist:") and object_id.endswith(":albums"):
            artist_id = object_id.split(":")[-2]
            albums = self.t.deezer.get_artist_albums(artist_id, limit=50)
            out = []
            for al in albums:
                title = al["title"]
                if al.get("release_date"):
                    year = al["release_date"][:4]
                    if year:
                        title = f"{title} ({year})"
                out.append(self.t._container(
                    f"deezer:album:{al['id']}",
                    object_id,
                    f"💿 {title}",
                    al.get("nb_tracks") or 0,
                ))
            return out

        if object_id.startswith("deezer:artist:"):
            artist_id = object_id.split(":")[-1]
            return self._artist_contents(artist_id, object_id)

        if object_id.startswith("deezer:album:"):
            album_id = object_id.split(":")[-1]
            return self._album_contents(album_id, object_id)

        if object_id == DZ_MINE:
            return [
                self.t._container(f"deezer:pl:{pl.id}", DZ_MINE, pl.title, pl.track_count)
                for pl in self.t.deezer.get_playlists()
            ]
        if object_id == DZ_EXPLORE:
            return [
                self.t._container(f"deezer:pl:{pl.id}", DZ_EXPLORE, pl.title, pl.track_count)
                for pl in self.t.deezer.get_explore()
            ]
        if object_id.startswith("deezer:pl:"):
            pl_id = object_id.split(":")[-1]
            # название плейлиста (не ID)
            pl_title = ""
            try:
                for pl in list(self.t.deezer.get_playlists() or []) + list(self.t.deezer.get_explore() or []):
                    if str(getattr(pl, "id", "")) == str(pl_id):
                        pl_title = (getattr(pl, "title", None) or "").strip()
                        break
            except Exception:
                pass
            if not pl_title:
                pl_title = f"Плейлист {pl_id}"
            tracks = self.t.deezer.get_tracks(pl_id)
            out = []
            track_dicts = []
            for tr in tracks:
                oid = f"deezer:tr:{tr.id}"
                url = f"{self.t.base_url}/stream/deezer/{tr.id}"
                title = f"{tr.artist} — {tr.title}" if tr.artist else tr.title
                out.append(
                    self.t._item(
                        oid,
                        object_id,
                        title,
                        url,
                        mime="audio/flac",
                        artist=tr.artist,
                        duration=tr.duration,
                        album=tr.album,
                    )
                )
                track_dicts.append({
                    "oid": oid, "title": title, "url": url,
                    "mime": "audio/flac", "artist": tr.artist or "",
                    "duration": tr.duration or 0, "album": tr.album or "",
                })
            if track_dicts:
                self.t.set_current_playlist("deezer", pl_title, track_dicts)
            return out

        return []

    def _artist_contents(self, artist_id: str, parent_id: str) -> List[str]:
        out: List[str] = []
        out.append(self.t._container(
            f"deezer:artist:{artist_id}:albums",
            parent_id,
            "💿 Альбомы",
            0,
        ))
        try:
            tracks = self.t.deezer.get_artist_top_tracks(artist_id, limit=50)
            for tr in tracks:
                url = f"{self.t.base_url}/stream/deezer/{tr.id}"
                title = f"{tr.artist} — {tr.title}" if tr.artist else tr.title
                out.append(self.t._item(
                    f"deezer:tr:{tr.id}",
                    parent_id,
                    f"🎵 {title}",
                    url,
                    mime="audio/flac",
                    artist=tr.artist,
                    duration=tr.duration,
                    album=tr.album,
                ))
        except Exception as e:
            log.warning("artist contents %s: %s", artist_id, e)
        return out


    def _album_contents(self, album_id: str, parent_id: str) -> List[str]:
        out: List[str] = []
        try:
            tracks = self.t.deezer.get_album_tracks(album_id)
            for tr in tracks:
                url = f"{self.t.base_url}/stream/deezer/{tr.id}"
                title = f"{tr.artist} — {tr.title}" if tr.artist else tr.title
                out.append(self.t._item(
                    f"deezer:tr:{tr.id}",
                    parent_id,
                    title,
                    url,
                    mime="audio/flac",
                    artist=tr.artist,
                    duration=tr.duration,
                    album=tr.album,
                ))
        except Exception as e:
            log.warning("album contents %s: %s", album_id, e)
        return out

    # ---------- PromoDJ ----------


