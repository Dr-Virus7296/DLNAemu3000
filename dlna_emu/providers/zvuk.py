"""Провайдер zvuk — ветка ContentDirectory."""
from __future__ import annotations

import logging
from typing import List

from .base import ServiceProvider
from ..content_ids import *  # noqa: F401,F403

log = logging.getLogger("dlna_emu.content.zvuk")


class ZvukProvider(ServiceProvider):
    root_id = "zvuk"

    def children(self, object_id: str) -> List[str]:
        if object_id == ZVUK:
            items = []
            n_cur = self.t.current_playlist_count("zvuk")
            if n_cur:
                items.append(self.t._container("zvuk:current", ZVUK, "* Текущий плейлист", n_cur))
            try:
                n_genres = len(self.t.zvuk.list_genres())
            except Exception:
                n_genres = 0
            try:
                n_curated = len(self.t.zvuk.list_curated_sections())
            except Exception:
                n_curated = 0
            items.extend([
                self.t._container(ZV_COLLECTION, ZVUK, "Коллекция", 2),
                self.t._container(ZV_CURATED, ZVUK, "📌 Подборки", n_curated),
                self.t._container(ZV_GENRES, ZVUK, "📂 Жанры", n_genres),
                self.t._container(ZV_SAVED, ZVUK, "🔍 Мои поиски",
                                len(self.t.zv_saved_searches())),
                self.t._container(ZV_FAV, ZVUK, "⭐ Избранное", len(self.t.site_favorites("zvuk"))),
            ])
            return items
        if object_id == "zvuk:current" or object_id.startswith("zvuk:current:"):
            return self.t._current_children("zvuk", object_id)

        if object_id == ZV_COLLECTION:
            try:
                likes_count = len(self.t.zvuk.get_liked_tracks())
            except Exception as e:
                log.warning("zvuk liked tracks: %s", e)
                likes_count = 0
            try:
                playlists = self.t.zvuk.get_user_playlists()
            except Exception as e:
                log.warning("zvuk user playlists: %s", e)
                playlists = []
            return [
                self.t._container(ZV_LIKES, ZV_COLLECTION, "Мне нравится", likes_count),
                self.t._container(f"{ZV_USER_PL_PREFIX}index", ZV_COLLECTION, "Мои плейлисты",
                                len(playlists)),
            ]

        if object_id == ZV_LIKES:
            try:
                tracks = self.t.zvuk.get_liked_tracks()
            except Exception as e:
                log.warning("zvuk liked tracks: %s", e)
                tracks = []
            out = [self._zv_track_item(tr, ZV_LIKES) for tr in tracks]
            self._zv_cache_current("Мне нравится", tracks)
            return out

        if object_id == f"{ZV_USER_PL_PREFIX}index":
            try:
                playlists = self.t.zvuk.get_user_playlists()
            except Exception as e:
                log.warning("zvuk user playlists: %s", e)
                playlists = []
            out = []
            for pl in playlists:
                self.t._zv_pl_titles[str(pl.id)] = (pl.title or str(pl.id)).strip()
                out.append(self.t._container(
                    f"{ZV_USER_PL_PREFIX}{pl.id}",
                    object_id,
                    pl.title,
                    pl.track_count,
                ))
            return out

        if object_id.startswith(ZV_USER_PL_PREFIX) and object_id != f"{ZV_USER_PL_PREFIX}index":
            pid = object_id[len(ZV_USER_PL_PREFIX):]
            try:
                tracks = self.t.zvuk.get_playlist_tracks(pid)
            except Exception as e:
                log.warning("zvuk playlist %s: %s", pid, e)
                tracks = []
            pl_title = self._zv_resolve_playlist_title(pid)
            out = [self._zv_track_item(tr, object_id) for tr in tracks]
            self._zv_cache_current(pl_title, tracks)
            return out

        if object_id == ZV_CURATED:
            try:
                sections = self.t.zvuk.list_curated_sections()
            except Exception as e:
                log.warning("zvuk curated: %s", e)
                sections = []
            return [
                self.t._container(
                    f"{ZV_CURATED_PREFIX}{s['id']}",
                    ZV_CURATED,
                    s["title"],
                    0,
                )
                for s in sections
            ]

        if object_id.startswith(ZV_CURATED_PREFIX):
            sid = object_id[len(ZV_CURATED_PREFIX):]
            # Редакция → список профилей
            if sid == "editorial":
                try:
                    profiles = self.t.zvuk.list_editorial_profiles()
                except Exception as e:
                    log.warning("zvuk editorial profiles: %s", e)
                    profiles = []
                return [
                    self.t._container(
                        f"{ZV_ED_PROF_PREFIX}{pr['id']}",
                        object_id,
                        f"📌 {pr['title']}",
                        0,
                    )
                    for pr in profiles
                ]
            try:
                playlists = self.t.zvuk.get_curated_playlists(sid, limit=50)
            except Exception as e:
                log.warning("zvuk curated %s: %s", sid, e)
                playlists = []
            out = []
            for pl in playlists:
                pid = str(pl.get("id") or "")
                title = (pl.get("title") or pid).strip()
                if pid and title:
                    self.t._zv_pl_titles[pid] = title
                out.append(self.t._container(
                    f"{ZV_PL_PREFIX}{pid}",
                    object_id,
                    title,
                    pl.get("track_count") or 0,
                ))
            return out

        if object_id.startswith(ZV_ED_PROF_PREFIX):
            pid = object_id[len(ZV_ED_PROF_PREFIX):]
            try:
                playlists = self.t.zvuk.get_profile_playlists(pid, limit=100)
            except Exception as e:
                log.warning("zvuk profile %s: %s", pid, e)
                playlists = []
            out = []
            for pl in playlists:
                plid = str(pl.get("id") or "")
                title = (pl.get("title") or plid).strip()
                if plid and title:
                    self.t._zv_pl_titles[plid] = title
                out.append(self.t._container(
                    f"{ZV_PL_PREFIX}{plid}",
                    object_id,
                    title,
                    pl.get("track_count") or 0,
                ))
            return out

        if object_id == ZV_GENRES:
            try:
                genres = self.t.zvuk.list_genres()
            except Exception as e:
                log.warning("zvuk genres: %s", e)
                genres = []
            return [
                self.t._container(
                    f"{ZV_GENRE_PREFIX}{g['id']}",
                    ZV_GENRES,
                    g["title"],
                    len(g.get("tabs") or []) + 1,
                )
                for g in genres
            ]

        if object_id.startswith(ZV_GENRE_PREFIX):
            gid = object_id[len(ZV_GENRE_PREFIX):]
            genre = next((g for g in self.t.zvuk.list_genres() if g["id"] == gid), None)
            if not genre:
                return []
            items = [
                self.t._container(
                    f"{ZV_GTAB_PREFIX}{gid}:",
                    object_id,
                    "Все (поиск по жанру)",
                    0,
                )
            ]
            for tab in (genre.get("tabs") or []):
                tid = tab.replace(" ", "_").replace("&", "and")
                items.append(self.t._container(
                    f"{ZV_GTAB_PREFIX}{gid}:{tid}",
                    object_id,
                    tab,
                    0,
                ))
            return items

        if object_id.startswith(ZV_GTAB_PREFIX):
            rest = object_id[len(ZV_GTAB_PREFIX):]
            if ":" not in rest:
                return []
            gid, tab_enc = rest.split(":", 1)
            tab = None
            if tab_enc:
                tab = tab_enc.replace("_", " ").replace("and", "&")
            try:
                playlists = self.t.zvuk.get_genre_playlists(gid, tab=tab, limit=40)
            except Exception as e:
                log.warning("zvuk genre playlists %s/%s: %s", gid, tab, e)
                playlists = []
            out = []
            for pl in playlists:
                pid = str(pl.get("id") or "")
                title = (pl.get("title") or pid).strip()
                if pid and title:
                    self.t._zv_pl_titles[pid] = title
                out.append(self.t._container(
                    f"{ZV_PL_PREFIX}{pid}",
                    object_id,
                    title,
                    pl.get("track_count") or 0,
                ))
            return out

        if object_id in (ZV_SAVED, ZV_FAV):
            saved = (self.t.site_favorites("zvuk") if object_id == ZV_FAV
                     else self.t._hist(self.t._zv_saved, self.t._zv_saved_lock))
            out = []
            for s in saved:
                if s.get("type") == "artist":
                    out.append(self.t._container(
                        f"{ZV_ARTIST_PREFIX}{s['artist_id']}",
                        object_id,
                        f"👤 {s['title']}",
                        0,
                    ))
                elif s.get("type") == "album":
                    out.append(self.t._container(
                        f"{ZV_ALBUM_PREFIX}{s['album_id']}",
                        object_id,
                        f"💿 {s['title']}",
                        0,
                    ))
                elif s.get("type") == "playlist":
                    out.append(self.t._container(
                        f"{ZV_PL_PREFIX}{s['playlist_id']}",
                        object_id,
                        f"📂 {s['title']}",
                        0,
                    ))
            return out

        if object_id.startswith(ZV_ARTIST_PREFIX):
            artist_id = object_id[len(ZV_ARTIST_PREFIX):]
            try:
                tracks = self.t.zvuk.get_artist_tracks(artist_id, limit=100)
            except Exception as e:
                log.warning("zvuk artist %s: %s", artist_id, e)
                tracks = []
            artist_title = self._zv_resolve_artist_title(artist_id)
            out = [self._zv_track_item(tr, object_id) for tr in tracks]
            self._zv_cache_current(artist_title, tracks)
            return out

        if object_id.startswith(ZV_ALBUM_PREFIX):
            album_id = object_id[len(ZV_ALBUM_PREFIX):]
            try:
                tracks = self.t.zvuk.get_release_tracks(album_id)
            except Exception as e:
                log.warning("zvuk album %s: %s", album_id, e)
                tracks = []
            album_title = self._zv_resolve_album_title(album_id)
            out = [self._zv_track_item(tr, object_id) for tr in tracks]
            self._zv_cache_current(album_title, tracks)
            return out

        if object_id.startswith(ZV_PL_PREFIX):
            pid = object_id[len(ZV_PL_PREFIX):]
            try:
                tracks = self.t.zvuk.get_playlist_tracks(pid)
            except Exception as e:
                log.warning("zvuk pl %s: %s", pid, e)
                tracks = []
            pl_title = self._zv_resolve_playlist_title(pid)
            out = [self._zv_track_item(tr, object_id) for tr in tracks]
            self._zv_cache_current(pl_title, tracks)
            return out

        return []

    def _zv_mime(self) -> str:
        q = (getattr(self.t.zvuk, "quality", "") or "").lower()
        return "audio/flac" if q in ("flac", "lossless") else "audio/mpeg"


    def _zv_track_item(self, tr, parent_id: str) -> str:
        url = f"{self.t.base_url}/stream/zvuk/{tr.id}"
        title = f"{tr.artist} — {tr.title}" if tr.artist else tr.title
        return self.t._item(
            f"{ZV_TR_PREFIX}{tr.id}",
            parent_id,
            title,
            url,
            mime=self._zv_mime(),
            artist=tr.artist,
            duration=tr.duration,
            album=tr.album,
        )



    def _zv_resolve_playlist_title(self, pid: str) -> str:
        """Человекочитаемое имя плейлиста Звука."""
        pid = str(pid or "").strip()
        if not pid:
            return "Плейлист"
        cached = self.t._zv_pl_titles.get(pid)
        if cached:
            return cached
        try:
            for pl in (self.t.zvuk.get_user_playlists() or []):
                if str(pl.id) == pid:
                    title = (pl.title or "").strip() or ("Плейлист " + pid)
                    self.t._zv_pl_titles[pid] = title
                    return title
        except Exception:
            pass
        try:
            data = self.t.zvuk._gql(
                "query($ids: [ID!]!) { getPlaylists(ids: $ids) { id title } }",
                {"ids": [pid]},
                "getPlaylists",
            )
            pls = (data or {}).get("getPlaylists") or (data or {}).get("get_playlists") or []
            if pls and isinstance(pls[0], dict):
                title = (pls[0].get("title") or "").strip()
                if title:
                    self.t._zv_pl_titles[pid] = title
                    return title
        except Exception:
            pass
        return "Плейлист " + pid


    def _zv_resolve_artist_title(self, aid: str) -> str:
        aid = str(aid or "").strip()
        if not aid:
            return "Артист"
        if aid in self.t._zv_artist_titles:
            return self.t._zv_artist_titles[aid]
        try:
            data = self.t.zvuk._gql(
                "query($ids: [ID!]!) { getArtists(ids: $ids) { id title } }",
                {"ids": [aid]},
                "getArtists",
            )
            arts = (data or {}).get("getArtists") or (data or {}).get("get_artists") or []
            if arts and isinstance(arts[0], dict):
                title = (arts[0].get("title") or arts[0].get("name") or "").strip()
                if title:
                    self.t._zv_artist_titles[aid] = title
                    return title
        except Exception:
            pass
        return "Артист " + aid


    def _zv_resolve_album_title(self, rid: str) -> str:
        rid = str(rid or "").strip()
        if not rid:
            return "Альбом"
        if rid in self.t._zv_album_titles:
            return self.t._zv_album_titles[rid]
        try:
            data = self.t.zvuk._gql(
                "query($ids: [ID!]!) { getReleases(ids: $ids) { id title } }",
                {"ids": [rid]},
                "getReleases",
            )
            rels = (data or {}).get("getReleases") or (data or {}).get("get_releases") or []
            if rels and isinstance(rels[0], dict):
                title = (rels[0].get("title") or "").strip()
                if title:
                    self.t._zv_album_titles[rid] = title
                    return title
        except Exception:
            pass
        return "Альбом " + rid


    def _zv_cache_current(self, title: str, tracks) -> None:
        if not tracks:
            return
        mime = self._zv_mime()
        track_dicts = []
        for tr in tracks:
            oid = f"{ZV_TR_PREFIX}{tr.id}"
            url = f"{self.t.base_url}/stream/zvuk/{tr.id}"
            ttitle = f"{tr.artist} — {tr.title}" if tr.artist else tr.title
            track_dicts.append({
                "oid": oid, "title": ttitle, "url": url,
                "mime": mime, "artist": tr.artist or "",
                "duration": tr.duration or 0, "album": tr.album or "",
            })
        if track_dicts:
            self.t.set_current_playlist("zvuk", title or "Плейлист", track_dicts)


