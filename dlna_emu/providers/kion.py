"""Провайдер kion — ветка ContentDirectory."""
from __future__ import annotations

import logging
from typing import List, Tuple
from urllib.parse import quote

from .base import ServiceProvider
from ..content_ids import *  # noqa: F401,F403

log = logging.getLogger("dlna_emu.content.kion")


class KionProvider(ServiceProvider):
    root_id = "kion"

    def children(self, object_id: str) -> List[str]:
        if object_id == KION:
            try:
                n_sec = len(self.t.kion.list_metatag_sections())
            except Exception:
                n_sec = 0
            items = []
            n_cur = self.t.current_playlist_count("kion")
            if n_cur:
                items.append(self.t._container("kion:current", KION, "* Текущий плейлист", n_cur))
            items.extend([
                self.t._container(KION_COLLECTION, KION, "Коллекция", 2),
                self.t._container(KION_ACTUAL, KION, "Актуально сейчас", 1),
                self.t._container(KION_CHART, KION, "Чарт", 100),
                self.t._container(KION_CHART_REAL, KION, "Настоящий чарт", 100),
                self.t._container(KION_COLLECTIONS, KION, "Подборки", 1),
                self.t._container(KION_PODCASTS, KION, "Подкасты", 50),
                self.t._container(KION_SAVED, KION, "🔍 Мои поиски",
                                len(self.t.kion_saved_searches())),
                self.t._container(KION_WAVE, KION, "🌊 Моя волна", FLOW_MAX_CACHE),
                self.t._container(KION_CATALOG, KION, "📂 Каталог", n_sec),
            ])
            return items
        if object_id == "kion:current" or object_id.startswith("kion:current:"):
            return self.t._current_children("kion", object_id)

        # --- Актуально сейчас (лента) ---
        if object_id == KION_ACTUAL:
            try:
                actual = self.t.kion.get_feed_actual()
            except Exception as e:
                log.warning("kion actual: %s", e)
                actual = {"tracks": [], "events": []}
            items = []
            tracks = actual.get("tracks") or []
            if tracks:
                items.append(self.t._container(
                    f"{KION_ACTUAL_EVENT_PREFIX}play",
                    KION_ACTUAL,
                    f"Слушать сейчас ({len(tracks)})",
                    len(tracks),
                ))
            for ev in (actual.get("events") or []):
                items.append(self.t._container(
                    f"{KION_ACTUAL_EVENT_PREFIX}{ev['id']}",
                    KION_ACTUAL,
                    ev.get("title") or "Подборка",
                    len(ev.get("tracks") or []),
                ))
            return items

        if object_id.startswith(KION_ACTUAL_EVENT_PREFIX):
            eid = object_id[len(KION_ACTUAL_EVENT_PREFIX):]
            try:
                actual = self.t.kion.get_feed_actual()
            except Exception as e:
                log.warning("kion actual event: %s", e)
                return []
            if eid == "play":
                tracks = actual.get("tracks") or []
            else:
                tracks = []
                for ev in (actual.get("events") or []):
                    if str(ev.get("id")) == eid:
                        tracks = ev.get("tracks") or []
                        break
            return [self._kion_track_item(tr, object_id) for tr in tracks]

        # --- Чарт ---
        if object_id == KION_CHART:
            try:
                tracks = self.t.kion.get_chart_tracks()
            except Exception as e:
                log.warning("kion chart: %s", e)
                tracks = []
            return [self._kion_track_item(tr, KION_CHART) for tr in tracks]

        if object_id == KION_CHART_REAL:
            try:
                tracks = self.t.kion.get_real_chart_tracks()
            except Exception as e:
                log.warning("kion real chart: %s", e)
                tracks = []
            return [self._kion_track_item(tr, KION_CHART_REAL) for tr in tracks]

        # --- Подборки ---
        if object_id == KION_COLLECTIONS:
            try:
                playlists = self.t.kion.get_collection_playlists()
            except Exception as e:
                log.warning("kion collections: %s", e)
                playlists = []
            items = []
            for pl in playlists:
                pid = f"{pl.get('owner_id')}:{pl.get('kind')}"
                items.append(self.t._container(
                    f"{KION_COLLECTIONS_PL_PREFIX}{pid}",
                    KION_COLLECTIONS,
                    pl.get("title") or "Подборка",
                    int(pl.get("track_count") or 0) or 1,
                ))
            return items

        if object_id.startswith(KION_COLLECTIONS_PL_PREFIX):
            pid = object_id[len(KION_COLLECTIONS_PL_PREFIX):]
            try:
                owner_id, kind = pid.split(":", 1)
            except ValueError:
                return []
            try:
                tracks = self.t.kion.get_playlist_tracks(owner_id=owner_id, kind=kind)
            except Exception as e:
                log.warning("kion collection pl %s: %s", pid, e)
                tracks = []
            return [self._kion_track_item(tr, object_id) for tr in tracks]

        # --- Подкасты (чарт) ---
        if object_id == KION_PODCASTS:
            try:
                pods = self.t.kion.get_podcast_chart()
            except Exception as e:
                log.warning("kion podcasts: %s", e)
                pods = []
            items = []
            for pod in pods:
                label = pod.get("title") or "Подкаст"
                if pod.get("artist"):
                    label = f"{pod['artist']} — {label}"
                items.append(self.t._container(
                    f"{KION_PODCAST_PREFIX}{pod['id']}",
                    KION_PODCASTS,
                    label,
                    int(pod.get("track_count") or 0) or 1,
                ))
            return items

        if object_id.startswith(KION_PODCAST_PREFIX):
            album_id = object_id[len(KION_PODCAST_PREFIX):]
            try:
                tracks = self.t.kion.get_album_tracks(album_id)
            except Exception as e:
                log.warning("kion podcast %s: %s", album_id, e)
                tracks = []
            return [self._kion_track_item(tr, object_id) for tr in tracks]


        if object_id == KION_COLLECTION:
            try:
                likes_count = len(self.t.kion.get_liked_tracks())
            except Exception as e:
                log.warning("kion liked tracks: %s", e)
                likes_count = 0
            try:
                playlists = self.t.kion.get_user_playlists()
            except Exception as e:
                log.warning("kion user playlists: %s", e)
                playlists = []
            try:
                liked_playlists = self.t.kion.get_liked_playlists()
            except Exception as e:
                log.warning("kion liked playlists: %s", e)
                liked_playlists = []
            return [
                self.t._container(KION_LIKES, KION_COLLECTION, "Мне нравится", likes_count),
                self.t._container(f"{KION_USER_PL_PREFIX}index", KION_COLLECTION, "Мои плейлисты", 2),
            ]

        if object_id == KION_LIKES:
            try:
                tracks = self.t.kion.get_liked_tracks()
            except Exception as e:
                log.warning("kion liked tracks: %s", e)
                tracks = []
            return [self._kion_track_item(tr, KION_LIKES) for tr in tracks]

        if object_id == f"{KION_USER_PL_PREFIX}index":
            try:
                playlists = self.t.kion.get_user_playlists()
            except Exception as e:
                log.warning("kion user playlists: %s", e)
                playlists = []
            try:
                liked_playlists = self.t.kion.get_liked_playlists()
            except Exception as e:
                log.warning("kion liked playlists: %s", e)
                liked_playlists = []
            return [
                self.t._container(f"{KION_USER_PL_PREFIX}owned", object_id, "Вы собрали", len(playlists)),
                self.t._container(f"{KION_LIKED_PL_PREFIX}index", object_id, "Вам понравилось", len(liked_playlists)),
            ]

        if object_id == f"{KION_USER_PL_PREFIX}owned":
            try:
                playlists = self.t.kion.get_user_playlists()
            except Exception as e:
                log.warning("kion user playlists: %s", e)
                playlists = []
            out = []
            for pl in playlists:
                pid = f"{pl['owner_id']}:{pl['kind']}"
                title = pl.get("title") or f"Плейлист {pid}"
                self.t._kion_pl_titles[pid] = title
                out.append(self.t._container(
                    f"{KION_USER_PL_PREFIX}{pid}", object_id, title, pl.get("track_count") or 0
                ))
            return out

        if object_id == f"{KION_LIKED_PL_PREFIX}index":
            try:
                playlists = self.t.kion.get_liked_playlists()
            except Exception as e:
                log.warning("kion liked playlists: %s", e)
                playlists = []
            out = []
            for pl in playlists:
                pid = f"{pl['owner_id']}:{pl['kind']}"
                title = pl.get("title") or f"Плейлист {pid}"
                self.t._kion_pl_titles[pid] = title
                out.append(self.t._container(
                    f"{KION_LIKED_PL_PREFIX}{pid}", object_id, title, pl.get("track_count") or 0
                ))
            return out

        if object_id.startswith(KION_LIKED_PL_PREFIX):
            pid = object_id[len(KION_LIKED_PL_PREFIX):]
            if pid == "index":
                return []
            pl_title = self._kion_resolve_playlist_title(pid, liked=True)
            try:
                owner_id, kind = pid.rsplit(":", 1)
                tracks = self.t.kion.get_playlist_tracks(owner_id=owner_id, kind=kind)
            except Exception as e:
                log.warning("kion liked playlist %s: %s", pid, e)
                tracks = []
            out = [self._kion_track_item(tr, object_id) for tr in tracks]
            self._kion_cache_current(pl_title, tracks)
            return out

        if object_id.startswith(KION_USER_PL_PREFIX):
            pid = object_id[len(KION_USER_PL_PREFIX):]
            if pid in ("index", "owned"):
                return []
            pl_title = self._kion_resolve_playlist_title(pid, liked=False)
            try:
                owner_id, kind = pid.rsplit(":", 1)
                tracks = self.t.kion.get_playlist_tracks(owner_id=owner_id, kind=kind)
            except Exception as e:
                log.warning("kion user playlist %s: %s", pid, e)
                tracks = []
            out = [self._kion_track_item(tr, object_id) for tr in tracks]
            self._kion_cache_current(pl_title, tracks)
            return out

        if object_id == KION_SAVED:
            with self.t._kion_saved_lock:
                saved = list(self.t._kion_saved)
            out = []
            for s in saved:
                if s.get("type") == "artist":
                    out.append(self.t._container(
                        f"{KION_ARTIST_PREFIX}{s['artist_id']}",
                        KION_SAVED,
                        f"👤 {s['title']}",
                        0,
                    ))
                elif s.get("type") == "album":
                    out.append(self.t._container(
                        f"{KION_ALBUM_PREFIX}{s['album_id']}",
                        KION_SAVED,
                        f"💿 {s['title']}",
                        0,
                    ))
            return out

        if object_id == KION_WAVE:
            try:
                presets = self.t.kion.list_wave_presets()
            except Exception as e:
                log.warning("kion wave presets: %s", e)
                presets = []
            out = []
            for pr in presets:
                pid = pr.get("id") or "default"
                title = pr.get("title") or pid
                out.append(self.t._container(
                    f"{KION_WAVE_PRESET_PREFIX}{pid}",
                    KION_WAVE,
                    title,
                    FLOW_MAX_CACHE,
                ))
            return out

        if object_id == KION_CATALOG:
            try:
                sections = self.t.kion.list_metatag_sections()
            except Exception as e:
                log.warning("kion catalog: %s", e)
                sections = []
            return [
                self.t._container(f"{KION_SECTION_PREFIX}{s['id']}", KION_CATALOG, s["title"], 0)
                for s in sections
            ]

        if object_id.startswith(KION_SECTION_PREFIX):
            sec_id = object_id[len(KION_SECTION_PREFIX):]
            try:
                tags = self.t.kion.list_metatags_in_section(sec_id)
            except Exception as e:
                log.warning("kion section %s: %s", sec_id, e)
                tags = []
            return [
                self.t._container(f"{KION_TAG_PREFIX}{t['tag']}", object_id, t["title"], 0)
                for t in tags
            ]

        if object_id.startswith(KION_TAG_PREFIX):
            tag = object_id[len(KION_TAG_PREFIX):]
            try:
                playlists = self.t.kion.get_metatag_playlists(tag, limit=40)
            except Exception as e:
                log.warning("kion tag %s: %s", tag, e)
                playlists = []
            out = []
            for pl in playlists:
                if pl.get("uuid"):
                    pid = f"uuid:{pl['uuid']}"
                elif pl.get("owner_id") is not None and pl.get("kind") is not None:
                    pid = f"{pl['owner_id']}:{pl['kind']}"
                else:
                    continue
                title = pl.get("title") or "Плейлист"
                owner = pl.get("owner") or ""
                if owner:
                    title = f"{title} · {owner}"
                n = pl.get("track_count") or 0
                out.append(self.t._container(f"{KION_PL_PREFIX}{pid}", object_id, title, n))
                self.t._kion_pl_titles[pid] = title
            return out

        if object_id.startswith(KION_PL_PREFIX):
            pid = object_id[len(KION_PL_PREFIX):]
            return self._kion_playlist_tracks(pid, object_id)

        if object_id.startswith(KION_ARTIST_PREFIX):
            artist_id = object_id[len(KION_ARTIST_PREFIX):]
            return self._kion_artist_tracks(artist_id, object_id)

        if object_id.startswith(KION_ALBUM_PREFIX):
            album_id = object_id[len(KION_ALBUM_PREFIX):]
            return self._kion_album_tracks(album_id, object_id)

        return []

    def _kion_mime(self) -> str:
        q = (getattr(self.t.kion, "quality", "") or "").lower()
        return "audio/flac" if q in ("flac", "lossless") else "audio/mpeg"


    def _kion_resolve_playlist_title(self, pid: str, liked: bool = False) -> str:
        """Найти человекочитаемое название плейлиста по owner:kind."""
        cached = self.t._kion_pl_titles.get(pid)
        if cached:
            return cached
        try:
            playlists = (
                self.t.kion.get_liked_playlists() if liked
                else self.t.kion.get_user_playlists()
            )
            for pl in playlists or []:
                key = f"{pl.get('owner_id')}:{pl.get('kind')}"
                if key == pid:
                    title = (pl.get("title") or "").strip() or f"Плейлист {pid}"
                    self.t._kion_pl_titles[pid] = title
                    return title
        except Exception:
            pass
        return f"Плейлист {pid}"


    def _kion_cache_current(self, title: str, tracks) -> None:
        """Сложить треки КИОНа в browse-кэш текущего плейлиста."""
        if not tracks:
            return
        mime = self._kion_mime()
        track_dicts = []
        for tr in tracks:
            oid = f"{KION_TR_PREFIX}{tr.id}"
            url = f"{self.t.base_url}/stream/kion/{tr.id}"
            ttitle = f"{tr.artist} — {tr.title}" if tr.artist else tr.title
            track_dicts.append({
                "oid": oid, "title": ttitle, "url": url,
                "mime": mime, "artist": tr.artist or "",
                "duration": tr.duration or 0, "album": tr.album or "",
            })
        if track_dicts:
            self.t.set_current_playlist("kion", title or "Плейлист", track_dicts)


    def _kion_artist_tracks(self, artist_id: str, parent_id: str) -> List[str]:
        out: List[str] = []
        track_dicts = []
        mime = self._kion_mime()
        try:
            tracks = self.t.kion.get_artist_tracks(artist_id, limit=100)
            for tr in tracks:
                oid = f"{KION_TR_PREFIX}{tr.id}"
                url = f"{self.t.base_url}/stream/kion/{tr.id}"
                title = f"{tr.artist} — {tr.title}" if tr.artist else tr.title
                out.append(
                    self.t._item(
                        oid,
                        parent_id,
                        title,
                        url,
                        mime=mime,
                        artist=tr.artist,
                        duration=tr.duration,
                        album=tr.album,
                    )
                )
                track_dicts.append({
                    "oid": oid, "title": title, "url": url,
                    "mime": mime, "artist": tr.artist or "",
                    "duration": tr.duration or 0, "album": tr.album or "",
                })
            if track_dicts:
                # имя артиста из первого трека
                artist_name = (tracks[0].artist if tracks else "") or f"Артист {artist_id}"
                self.t.set_current_playlist("kion", artist_name, track_dicts)
        except Exception as e:
            log.warning("kion artist %s: %s", artist_id, e)
        return out


    def _kion_album_tracks(self, album_id: str, parent_id: str) -> List[str]:
        out: List[str] = []
        track_dicts = []
        mime = self._kion_mime()
        try:
            tracks = self.t.kion.get_album_tracks(album_id)
            for tr in tracks:
                oid = f"{KION_TR_PREFIX}{tr.id}"
                url = f"{self.t.base_url}/stream/kion/{tr.id}"
                title = f"{tr.artist} — {tr.title}" if tr.artist else tr.title
                out.append(
                    self.t._item(
                        oid,
                        parent_id,
                        title,
                        url,
                        mime=mime,
                        artist=tr.artist,
                        duration=tr.duration,
                        album=tr.album,
                    )
                )
                track_dicts.append({
                    "oid": oid, "title": title, "url": url,
                    "mime": mime, "artist": tr.artist or "",
                    "duration": tr.duration or 0, "album": tr.album or "",
                })
            if track_dicts:
                album_name = (tracks[0].album if tracks else "") or f"Альбом {album_id}"
                self.t.set_current_playlist("kion", album_name, track_dicts)
        except Exception as e:
            log.warning("kion album %s: %s", album_id, e)
        return out

    def _browse_kion_wave(
        self, object_id: str, starting_index: int, requested_count: int
    ) -> Tuple[str, int, int]:
        """Волна: отдельные треки (как Deezer Flow).

        TotalMatches всегда = FLOW_MAX_CACHE, а не фактическое число уже
        загруженных треков. Иначе DLNA-клиент видит «всего 5» (размер партии
        КИОНа) и не запрашивает следующие страницы / не листает дальше.

        Непрерывный «как радио» поток убран: склейка FLAC→MP3 в одном
        HTTP-ответе ломает декодер ресивера.
        """
        preset_id = "default"
        if object_id.startswith(KION_WAVE_PRESET_PREFIX):
            preset_id = object_id[len(KION_WAVE_PRESET_PREFIX):] or "default"

        if requested_count <= 0:
            return_count = FLOW_MAX_PAGE
        else:
            return_count = min(requested_count, FLOW_MAX_PAGE)

        empty_didl = (
            '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
            'xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">'
            "</DIDL-Lite>"
        )

        # Всегда сообщаем «бесконечный» контейнер (как Deezer Flow)
        total = FLOW_MAX_CACHE

        if starting_index >= total:
            return empty_didl, 0, total

        tracks = self.t.kion.get_wave_tracks(preset_id=preset_id, append=False)

        # Сколько нужно иметь в кэше, чтобы отдать запрошенное окно
        need = starting_index + return_count
        # На первой странице подгружаем запас вперёд (партия API ≈ 5)
        if starting_index == 0:
            need = max(need, 20)

        grew = stall = 0
        while len(tracks) < need and grew < 12:
            before = len(tracks)
            self.t.kion.get_wave_tracks(preset_id=preset_id, append=True)
            tracks = self.t.kion.get_wave_tracks(preset_id=preset_id, append=False)
            grew += 1
            if len(tracks) == before:
                stall += 1
                if stall >= 3:
                    break
            else:
                stall = 0

        if starting_index >= len(tracks):
            # Кэш не догнал — всё равно TotalMatches большой, клиент повторит Browse
            return empty_didl, 0, total

        didl = [
            '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
            'xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">'
        ]
        returned = 0
        idx = starting_index
        while returned < return_count and idx < len(tracks):
            tr = tracks[idx]
            url = f"{self.t.base_url}/stream/kion/{tr.id}"
            title = f"{tr.artist} — {tr.title}" if tr.artist else tr.title
            didl.append(
                self.t._item(
                    f"{KION_TR_PREFIX}{tr.id}",
                    object_id,
                    title,
                    url,
                    mime=self._kion_mime(),
                    artist=tr.artist,
                    duration=tr.duration,
                    album=tr.album,
                )
            )
            returned += 1
            idx += 1
        didl.append("</DIDL-Lite>")
        return "\n".join(didl), returned, total

    def _kion_track_item(self, tr, parent_id: str) -> str:
        url = f"{self.t.base_url}/stream/kion/{tr.id}"
        title = f"{tr.artist} — {tr.title}" if tr.artist else tr.title
        return self.t._item(f"{KION_TR_PREFIX}{tr.id}", parent_id, title, url,
                          mime=self._kion_mime(), artist=tr.artist,
                          duration=tr.duration, album=tr.album)


    def _kion_playlist_tracks(self, pid: str, parent_id: str) -> List[str]:
        out: List[str] = []
        track_dicts = []
        mime = self._kion_mime()
        try:
            uuid = ""
            owner_id = ""
            kind = None
            if pid.startswith("uuid:"):
                uuid = pid[5:]
            elif ":" in pid:
                owner_id, kind_s = pid.split(":", 1)
                try:
                    kind = int(kind_s)
                except ValueError:
                    kind = kind_s
            tracks = self.t.kion.get_playlist_tracks(
                owner_id=owner_id, kind=kind, uuid=uuid
            )
            for tr in tracks:
                oid = f"{KION_TR_PREFIX}{tr.id}"
                url = f"{self.t.base_url}/stream/kion/{tr.id}"
                title = f"{tr.artist} — {tr.title}" if tr.artist else tr.title
                out.append(
                    self.t._item(
                        oid,
                        parent_id,
                        title,
                        url,
                        mime=mime,
                        artist=tr.artist,
                        duration=tr.duration,
                        album=tr.album,
                    )
                )
                track_dicts.append({
                    "oid": oid, "title": title, "url": url,
                    "mime": mime, "artist": tr.artist or "",
                    "duration": tr.duration or 0, "album": tr.album or "",
                })
            if track_dicts:
                pl_title = (
                    self.t._kion_pl_titles.get(pid)
                    or self._kion_resolve_playlist_title(pid, liked=False)
                )
                if pl_title.startswith("Плейлист ") and ":" in pl_title:
                    # ещё раз по liked
                    t2 = self._kion_resolve_playlist_title(pid, liked=True)
                    if not t2.startswith("Плейлист "):
                        pl_title = t2
                self.t.set_current_playlist("kion", pl_title, track_dicts)
        except Exception as e:
            log.warning("kion playlist %s: %s", pid, e)
        return out


