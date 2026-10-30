"""Провайдер yandex — ветка ContentDirectory."""
from __future__ import annotations

import logging
from typing import List, Tuple
from urllib.parse import quote, unquote

from .base import ServiceProvider
from ..content_ids import *  # noqa: F401,F403

log = logging.getLogger("dlna_emu.content.yandex")

YA_LP_PREFIXES = (YA_LP_PAGE_PREFIX, YA_LP_TAB_PREFIX, YA_LP_SUB_PREFIX,
                  YA_LP_BLOCK_PREFIX, YA_LP_GROUP_PREFIX)


class YandexProvider(ServiceProvider):
    root_id = "yandex"

    def children(self, object_id: str) -> List[str]:
        if object_id == YANDEX:
            try:
                n_sec = len(self.t.yandex.list_metatag_sections())
            except Exception:
                n_sec = 0
            items = []
            n_cur = self.t.current_playlist_count("yandex")
            if n_cur:
                items.append(self.t._container("yandex:current", YANDEX, "* Текущий плейлист", n_cur))
            items.extend([
                self.t._container(YA_COLLECTION, YANDEX, "Коллекция", 2),
                self.t._container(f"{YA_LP_PAGE_PREFIX}foryou", YANDEX, "Для вас", 1),
                self.t._container(f"{YA_LP_PAGE_PREFIX}trends", YANDEX, "Тренды", 1),
                self.t._container(f"{YA_LP_PAGE_PREFIX}nonmusic", YANDEX, "Книги и подкасты", 1),
                self.t._container(f"{YA_LP_PAGE_PREFIX}kids", YANDEX, "Детям", 1),
                self.t._container(YA_SAVED, YANDEX, "🔍 Мои поиски",
                                len(self.t.ya_saved_searches())),
                self.t._container(YA_WAVE, YANDEX, "🌊 Моя волна", FLOW_MAX_CACHE),
                self.t._container(YA_CATALOG, YANDEX, "📂 Каталог", n_sec),
            ])
            return items
        if object_id == "yandex:current" or object_id.startswith("yandex:current:"):
            return self.t._current_children("yandex", object_id)

        # --- Разделы сайта (динамически из /landing/skeleton) ---
        if object_id.startswith(YA_LP_PREFIXES):
            return self._ya_landing_children(object_id)

        if object_id == YA_COLLECTION:
            try:
                likes_count = len(self.t.yandex.get_liked_tracks())
            except Exception as e:
                log.warning("yandex liked tracks: %s", e)
                likes_count = 0
            try:
                playlists = self.t.yandex.get_user_playlists()
            except Exception as e:
                log.warning("yandex user playlists: %s", e)
                playlists = []
            try:
                liked_playlists = self.t.yandex.get_liked_playlists()
            except Exception as e:
                log.warning("yandex liked playlists: %s", e)
                liked_playlists = []
            return [
                self.t._container(YA_LIKES, YA_COLLECTION, "Мне нравится", likes_count),
                self.t._container(f"{YA_USER_PL_PREFIX}index", YA_COLLECTION, "Мои плейлисты", 2),
            ]

        if object_id == YA_LIKES:
            try:
                tracks = self.t.yandex.get_liked_tracks()
            except Exception as e:
                log.warning("yandex liked tracks: %s", e)
                tracks = []
            return [self._ya_track_item(tr, YA_LIKES) for tr in tracks]

        if object_id == f"{YA_USER_PL_PREFIX}index":
            try:
                playlists = self.t.yandex.get_user_playlists()
            except Exception as e:
                log.warning("yandex user playlists: %s", e)
                playlists = []
            try:
                liked_playlists = self.t.yandex.get_liked_playlists()
            except Exception as e:
                log.warning("yandex liked playlists: %s", e)
                liked_playlists = []
            return [
                self.t._container(f"{YA_USER_PL_PREFIX}owned", object_id, "Вы собрали", len(playlists)),
                self.t._container(f"{YA_LIKED_PL_PREFIX}index", object_id, "Вам понравилось", len(liked_playlists)),
            ]

        if object_id == f"{YA_USER_PL_PREFIX}owned":
            try:
                playlists = self.t.yandex.get_user_playlists()
            except Exception as e:
                log.warning("yandex user playlists: %s", e)
                playlists = []
            out = []
            for pl in playlists:
                pid = f"{pl['owner_id']}:{pl['kind']}"
                title = pl.get("title") or f"Плейлист {pid}"
                self.t._ya_pl_titles[pid] = title
                out.append(self.t._container(
                    f"{YA_USER_PL_PREFIX}{pid}", object_id, title, pl.get("track_count") or 0
                ))
            return out

        if object_id == f"{YA_LIKED_PL_PREFIX}index":
            try:
                playlists = self.t.yandex.get_liked_playlists()
            except Exception as e:
                log.warning("yandex liked playlists: %s", e)
                playlists = []
            out = []
            for pl in playlists:
                pid = f"{pl['owner_id']}:{pl['kind']}"
                title = pl.get("title") or f"Плейлист {pid}"
                self.t._ya_pl_titles[pid] = title
                out.append(self.t._container(
                    f"{YA_LIKED_PL_PREFIX}{pid}", object_id, title, pl.get("track_count") or 0
                ))
            return out

        if object_id.startswith(YA_LIKED_PL_PREFIX):
            pid = object_id[len(YA_LIKED_PL_PREFIX):]
            if pid == "index":
                return []
            pl_title = self._ya_resolve_playlist_title(pid, liked=True)
            try:
                owner_id, kind = pid.rsplit(":", 1)
                tracks = self.t.yandex.get_playlist_tracks(owner_id=owner_id, kind=kind)
            except Exception as e:
                log.warning("yandex liked playlist %s: %s", pid, e)
                tracks = []
            out = [self._ya_track_item(tr, object_id) for tr in tracks]
            self._ya_cache_current(pl_title, tracks)
            return out

        if object_id.startswith(YA_USER_PL_PREFIX):
            pid = object_id[len(YA_USER_PL_PREFIX):]
            if pid in ("index", "owned"):
                return []
            pl_title = self._ya_resolve_playlist_title(pid, liked=False)
            try:
                owner_id, kind = pid.rsplit(":", 1)
                tracks = self.t.yandex.get_playlist_tracks(owner_id=owner_id, kind=kind)
            except Exception as e:
                log.warning("yandex user playlist %s: %s", pid, e)
                tracks = []
            out = [self._ya_track_item(tr, object_id) for tr in tracks]
            self._ya_cache_current(pl_title, tracks)
            return out

        if object_id == YA_SAVED:
            with self.t._ya_saved_lock:
                saved = list(self.t._ya_saved)
            out = []
            for s in saved:
                if s.get("type") == "artist":
                    out.append(self.t._container(
                        f"{YA_ARTIST_PREFIX}{s['artist_id']}",
                        YA_SAVED,
                        f"👤 {s['title']}",
                        0,
                    ))
                elif s.get("type") == "album":
                    out.append(self.t._container(
                        f"{YA_ALBUM_PREFIX}{s['album_id']}",
                        YA_SAVED,
                        f"💿 {s['title']}",
                        0,
                    ))
            return out

        if object_id == YA_WAVE:
            try:
                presets = self.t.yandex.list_wave_presets()
            except Exception as e:
                log.warning("yandex wave presets: %s", e)
                presets = []
            out = []
            for pr in presets:
                pid = pr.get("id") or "default"
                title = pr.get("title") or pid
                out.append(self.t._container(
                    f"{YA_WAVE_PRESET_PREFIX}{pid}",
                    YA_WAVE,
                    title,
                    FLOW_MAX_CACHE,
                ))
            return out

        if object_id == YA_CATALOG:
            try:
                sections = self.t.yandex.list_metatag_sections()
            except Exception as e:
                log.warning("yandex catalog: %s", e)
                sections = []
            return [
                self.t._container(f"{YA_SECTION_PREFIX}{s['id']}", YA_CATALOG, s["title"], 0)
                for s in sections
            ]

        if object_id.startswith(YA_SECTION_PREFIX):
            sec_id = object_id[len(YA_SECTION_PREFIX):]
            try:
                tags = self.t.yandex.list_metatags_in_section(sec_id)
            except Exception as e:
                log.warning("yandex section %s: %s", sec_id, e)
                tags = []
            return [
                self.t._container(f"{YA_TAG_PREFIX}{t['tag']}", object_id, t["title"], 0)
                for t in tags
            ]

        if object_id.startswith(YA_TAG_PREFIX):
            tag = object_id[len(YA_TAG_PREFIX):]
            try:
                playlists = self.t.yandex.get_metatag_playlists(tag, limit=40)
            except Exception as e:
                log.warning("yandex tag %s: %s", tag, e)
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
                out.append(self.t._container(f"{YA_PL_PREFIX}{pid}", object_id, title, n))
                self.t._ya_pl_titles[pid] = title
            return out

        if object_id.startswith(YA_PL_PREFIX):
            pid = object_id[len(YA_PL_PREFIX):]
            return self._ya_playlist_tracks(pid, object_id)

        if object_id.startswith(YA_ARTIST_PREFIX):
            artist_id = object_id[len(YA_ARTIST_PREFIX):]
            return self._ya_artist_tracks(artist_id, object_id)

        if object_id.startswith(YA_ALBUM_PREFIX):
            album_id = object_id[len(YA_ALBUM_PREFIX):]
            return self._ya_album_tracks(album_id, object_id)

        return []

    # ---------- разделы сайта: Для вас / Тренды / Книги и подкасты / Детям ----------

    @staticmethod
    def _lp_id(raw: str) -> str:
        return quote(str(raw), safe="")

    @staticmethod
    def _lp_unid(raw: str) -> str:
        return unquote(str(raw))

    def _ya_blocks_containers(self, blocks, parent_id: str) -> List[str]:
        """Полки страницы → контейнеры (только непустые, с числом элементов)."""
        out = []
        for b in self.t.yandex.ready_blocks(blocks):
            out.append(self.t._container(
                f"{YA_LP_BLOCK_PREFIX}{self._lp_id(b['id'])}", parent_id,
                b.get("title") or b["id"], int(b.get("count") or 1)))
        return out

    def _ya_landing_children(self, object_id: str) -> List[str]:
        ya = self.t.yandex
        try:
            # страница верхнего уровня
            if object_id.startswith(YA_LP_PAGE_PREFIX):
                page_key = object_id[len(YA_LP_PAGE_PREFIX):]
                page = ya.get_landing_page(page_key)
                return self._ya_page_view(page, object_id, f"{YA_LP_TAB_PREFIX}{page_key}:")

            # вкладка внутри страницы (например «Книги» / «Подкасты»)
            if object_id.startswith(YA_LP_TAB_PREFIX):
                page_key, _, idx = object_id[len(YA_LP_TAB_PREFIX):].rpartition(":")
                try:
                    blocks = ya.get_landing_tab(page_key, int(idx))
                except ValueError:
                    blocks = []
                return self._ya_blocks_containers(blocks, object_id)

            # вложенная страница (например «Песенки» в «Детям»)
            if object_id.startswith(YA_LP_SUB_PREFIX):
                rest = object_id[len(YA_LP_SUB_PREFIX):]
                if ":tab:" in rest:
                    sid_enc, _, idx = rest.partition(":tab:")
                    tabs = ya.get_sublanding(self._lp_unid(sid_enc)).get("tabs") or []
                    try:
                        blocks = tabs[int(idx)]["blocks"]
                    except (ValueError, IndexError):
                        blocks = []
                    return self._ya_blocks_containers(blocks, object_id)
                sid = self._lp_unid(rest)
                page = ya.get_sublanding(sid)
                return self._ya_page_view(page, object_id, f"{YA_LP_SUB_PREFIX}{self._lp_id(sid)}:tab:")

            # группа внутри полки (вкладки «В стиле»)
            if object_id.startswith(YA_LP_GROUP_PREFIX):
                bid_enc, _, idx = object_id[len(YA_LP_GROUP_PREFIX):].rpartition(":")
                items = ya.get_block_items(self._lp_unid(bid_enc))
                try:
                    grp = items[int(idx)]
                except (ValueError, IndexError):
                    return []
                return self._ya_entities(grp.get("items") or [], object_id, grp.get("title") or "")

            # полка
            if object_id.startswith(YA_LP_BLOCK_PREFIX):
                bid = self._lp_unid(object_id[len(YA_LP_BLOCK_PREFIX):])
                items = ya.get_block_items(bid)
                title = (getattr(ya, "_block_meta_map")().get(bid) or {}).get("title") or bid
                return self._ya_entities(items, object_id, title, block_id=bid)
        except Exception as e:
            log.warning("ya landing %s: %s", object_id, e)
        return []

    def _ya_page_view(self, page: dict, object_id: str, tab_prefix: str) -> List[str]:
        out = self._ya_blocks_containers(page.get("blocks") or [], object_id)
        for i, tab in enumerate(page.get("tabs") or []):
            if not (tab.get("blocks")):
                continue
            out.append(self.t._container(f"{tab_prefix}{i}", object_id,
                                         tab.get("title") or f"Вкладка {i + 1}",
                                         len(tab["blocks"])))
        return out

    def _ya_entities(self, items, parent_id: str, title: str = "", block_id: str = "") -> List[str]:
        """Элементы полки → DIDL (альбомы/плейлисты/артисты — контейнеры, треки — items)."""
        out: List[str] = []
        tracks = []
        for idx, e in enumerate(items or []):
            kind = e.get("kind")
            label = e.get("title") or ""
            pos = e.get("pos")
            if kind == "album":
                if e.get("artist") and e.get("meta", "music") == "music":
                    label = f"{e['artist']} — {label}"
                elif e.get("artist"):
                    label = f"{label} ({e['artist']})"
                if pos:
                    label = f"{pos}. {label}"
                out.append(self.t._container(f"{YA_ALBUM_PREFIX}{e['id']}", parent_id,
                                             label, e.get("count") or 1))
            elif kind == "playlist":
                if e.get("owner"):
                    label = f"{label} · {e['owner']}"
                self.t._ya_pl_titles[e["id"]] = e.get("title") or label
                out.append(self.t._container(f"{YA_PL_PREFIX}{e['id']}", parent_id,
                                             label, e.get("count") or 1))
            elif kind == "artist":
                out.append(self.t._container(f"{YA_ARTIST_PREFIX}{e['id']}", parent_id,
                                             f"👤 {label}", e.get("count") or 1))
            elif kind == "landing":
                out.append(self.t._container(f"{YA_LP_SUB_PREFIX}{self._lp_id(e['id'])}", parent_id,
                                             label, 1))
            elif kind == "group" and block_id:
                out.append(self.t._container(
                    f"{YA_LP_GROUP_PREFIX}{self._lp_id(block_id)}:{idx}", parent_id,
                    label, e.get("count") or 1))
            elif kind == "track" and e.get("track") is not None:
                tr = e["track"]
                out.append(self._ya_track_item(tr, parent_id))
                tracks.append(tr)
        if tracks:
            self._ya_cache_current(title or "Подборка", tracks)
        return out


    def _ya_mime(self) -> str:
        q = (getattr(self.t.yandex, "quality", "") or "").lower()
        return "audio/flac" if q in ("flac", "lossless") else "audio/mpeg"


    def _ya_resolve_playlist_title(self, pid: str, liked: bool = False) -> str:
        """Найти человекочитаемое название плейлиста по owner:kind."""
        cached = self.t._ya_pl_titles.get(pid)
        if cached:
            return cached
        try:
            playlists = (
                self.t.yandex.get_liked_playlists() if liked
                else self.t.yandex.get_user_playlists()
            )
            for pl in playlists or []:
                key = f"{pl.get('owner_id')}:{pl.get('kind')}"
                if key == pid:
                    title = (pl.get("title") or "").strip() or f"Плейлист {pid}"
                    self.t._ya_pl_titles[pid] = title
                    return title
        except Exception:
            pass
        return f"Плейлист {pid}"


    def _ya_cache_current(self, title: str, tracks) -> None:
        """Сложить треки Яндекса в browse-кэш текущего плейлиста."""
        if not tracks:
            return
        mime = self._ya_mime()
        track_dicts = []
        for tr in tracks:
            oid = f"{YA_TR_PREFIX}{tr.id}"
            url = f"{self.t.base_url}/stream/yandex/{tr.id}"
            ttitle = f"{tr.artist} — {tr.title}" if tr.artist else tr.title
            track_dicts.append({
                "oid": oid, "title": ttitle, "url": url,
                "mime": mime, "artist": tr.artist or "",
                "duration": tr.duration or 0, "album": tr.album or "",
            })
        if track_dicts:
            self.t.set_current_playlist("yandex", title or "Плейлист", track_dicts)


    def _ya_artist_tracks(self, artist_id: str, parent_id: str) -> List[str]:
        out: List[str] = []
        track_dicts = []
        mime = self._ya_mime()
        try:
            tracks = self.t.yandex.get_artist_tracks(artist_id, limit=100)
            for tr in tracks:
                oid = f"{YA_TR_PREFIX}{tr.id}"
                url = f"{self.t.base_url}/stream/yandex/{tr.id}"
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
                self.t.set_current_playlist("yandex", artist_name, track_dicts)
        except Exception as e:
            log.warning("yandex artist %s: %s", artist_id, e)
        return out


    def _ya_album_tracks(self, album_id: str, parent_id: str) -> List[str]:
        out: List[str] = []
        track_dicts = []
        mime = self._ya_mime()
        try:
            tracks = self.t.yandex.get_album_tracks(album_id)
            for tr in tracks:
                oid = f"{YA_TR_PREFIX}{tr.id}"
                url = f"{self.t.base_url}/stream/yandex/{tr.id}"
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
                self.t.set_current_playlist("yandex", album_name, track_dicts)
        except Exception as e:
            log.warning("yandex album %s: %s", album_id, e)
        return out

    def _browse_ya_wave(
        self, object_id: str, starting_index: int, requested_count: int
    ) -> Tuple[str, int, int]:
        """Волна: отдельные треки (как Deezer Flow).

        TotalMatches всегда = FLOW_MAX_CACHE, а не фактическое число уже
        загруженных треков. Иначе DLNA-клиент видит «всего 5» (размер партии
        Яндекса) и не запрашивает следующие страницы / не листает дальше.

        Непрерывный «как радио» поток убран: склейка FLAC→MP3 в одном
        HTTP-ответе ломает декодер ресивера.
        """
        preset_id = "default"
        if object_id.startswith(YA_WAVE_PRESET_PREFIX):
            preset_id = object_id[len(YA_WAVE_PRESET_PREFIX):] or "default"

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

        tracks = self.t.yandex.get_wave_tracks(preset_id=preset_id, append=False)

        # Сколько нужно иметь в кэше, чтобы отдать запрошенное окно
        need = starting_index + return_count
        # На первой странице подгружаем запас вперёд (партия API ≈ 5)
        if starting_index == 0:
            need = max(need, 20)

        grew = stall = 0
        while len(tracks) < need and grew < 12:
            before = len(tracks)
            self.t.yandex.get_wave_tracks(preset_id=preset_id, append=True)
            tracks = self.t.yandex.get_wave_tracks(preset_id=preset_id, append=False)
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
            url = f"{self.t.base_url}/stream/yandex/{tr.id}"
            title = f"{tr.artist} — {tr.title}" if tr.artist else tr.title
            didl.append(
                self.t._item(
                    f"{YA_TR_PREFIX}{tr.id}",
                    object_id,
                    title,
                    url,
                    mime=self._ya_mime(),
                    artist=tr.artist,
                    duration=tr.duration,
                    album=tr.album,
                )
            )
            returned += 1
            idx += 1
        didl.append("</DIDL-Lite>")
        return "\n".join(didl), returned, total

    def _ya_track_item(self, tr, parent_id: str) -> str:
        url = f"{self.t.base_url}/stream/yandex/{tr.id}"
        title = f"{tr.artist} — {tr.title}" if tr.artist else tr.title
        return self.t._item(f"{YA_TR_PREFIX}{tr.id}", parent_id, title, url,
                          mime=self._ya_mime(), artist=tr.artist,
                          duration=tr.duration, album=tr.album)


    def _ya_playlist_tracks(self, pid: str, parent_id: str) -> List[str]:
        out: List[str] = []
        track_dicts = []
        mime = self._ya_mime()
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
            tracks = self.t.yandex.get_playlist_tracks(
                owner_id=owner_id, kind=kind, uuid=uuid
            )
            for tr in tracks:
                oid = f"{YA_TR_PREFIX}{tr.id}"
                url = f"{self.t.base_url}/stream/yandex/{tr.id}"
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
                    self.t._ya_pl_titles.get(pid)
                    or self._ya_resolve_playlist_title(pid, liked=False)
                )
                if pl_title.startswith("Плейлист ") and ":" in pl_title:
                    # ещё раз по liked
                    t2 = self._ya_resolve_playlist_title(pid, liked=True)
                    if not t2.startswith("Плейлист "):
                        pl_title = t2
                self.t.set_current_playlist("yandex", pl_title, track_dicts)
        except Exception as e:
            log.warning("yandex playlist %s: %s", pid, e)
        return out


