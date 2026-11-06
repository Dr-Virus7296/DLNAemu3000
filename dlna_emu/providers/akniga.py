"""Провайдер akniga — ветка ContentDirectory."""
from __future__ import annotations

import logging
from typing import List

from .base import ServiceProvider
from ..content_ids import *  # noqa: F401,F403
from ..akniga_client import AkBook

log = logging.getLogger("dlna_emu.content.akniga")


class AknigaProvider(ServiceProvider):
    root_id = "akniga"

    def children(self, object_id: str) -> List[str]:
        if object_id == AKNIGA:
            items = []
            n_cur = self.t.current_playlist_count("akniga")
            if n_cur:
                items.append(self.t._container("akniga:current", AKNIGA, "* Текущий плейлист", n_cur))
            items.extend([
                self.t._container(AK_SAVED, AKNIGA, "🔍 Мои книги",
                                len(self.t.ak_saved_books())),
                self.t._container(AK_FAV, AKNIGA, "⭐ Избранное",
                                len(self.t.ak_favorites())),
                self.t._container(AK_HOME, AKNIGA, "На главной", 0),
                self.t._container(AK_GENRES, AKNIGA, "Жанры",
                                len(self.t.akniga.list_genres())),
            ])
            return items
        if object_id == "akniga:current" or object_id.startswith("akniga:current:"):
            return self.t._current_children("akniga", object_id)

        if object_id == AK_SAVED:
            saved = self.t._hist(self.t._ak_saved, self.t._ak_saved_lock)
            return [
                self.t._container(
                    f"{AK_BOOK_PREFIX}{s['slug']}",
                    AK_SAVED,
                    s.get("title") or s["slug"],
                    0,
                )
                for s in saved if s.get("slug")
            ]

        if object_id == AK_FAV:
            with self.t._ak_favorites_lock:
                favs = list(self.t._ak_favorites)
            return [
                self.t._container(
                    f"{AK_BOOK_PREFIX}{s['slug']}",
                    AK_FAV,
                    s.get("title") or s["slug"],
                    0,
                )
                for s in favs if s.get("slug")
            ]

        if object_id == AK_HOME:
            return self._ak_book_list(self.t.akniga.list_home(), object_id)

        if object_id == AK_GENRES:
            return [
                self.t._container(f"{AK_GENRE_PREFIX}{slug}", AK_GENRES, title, 0)
                for slug, title in self.t.akniga.list_genres()
            ]

        if object_id.startswith(AK_GENRE_PREFIX):
            slug = object_id[len(AK_GENRE_PREFIX):]
            return self._ak_section_pages(slug, object_id)

        if object_id.startswith(AK_SECPAGE_PREFIX):
            rest = object_id[len(AK_SECPAGE_PREFIX):]
            slug, _, n_str = rest.rpartition(":")
            if not slug:
                return []
            try:
                n = int(n_str)
            except ValueError:
                return []
            return self._ak_book_list(
                self.t.akniga.list_section_chunk(slug, n),
                object_id,
            )

        if object_id.startswith(AK_BOOK_PREFIX):
            slug = object_id[len(AK_BOOK_PREFIX):]
            return self._ak_chapters(slug, object_id)

        return []

    def _ak_label(self, b: AkBook) -> str:
        if b.author and b.author not in b.title:
            return f"{b.author} — {b.title}"
        return b.title


    def _ak_book_list(self, books: List[AkBook], parent_id: str) -> List[str]:
        # Скрываем книги, помеченные «Закрыто» — их ajax не отдаст.
        return [
            self.t._container(f"{AK_BOOK_PREFIX}{b.slug}", parent_id, self._ak_label(b), 0)
            for b in books if not getattr(b, "closed", False)
        ]


    def _ak_section_pages(self, slug: str, parent_id: str) -> List[str]:
        try:
            total_pages, per_page, chunks = self.t.akniga.section_layout(slug)
        except Exception as e:
            log.warning("akniga layout %s: %s", slug, e)
            return []
        if total_pages <= 0 or per_page <= 0:
            return []
        if chunks <= 1:
            return self._ak_book_list(
                self.t.akniga.list_section_chunk(slug, 0), parent_id,
            )
        est_total = total_pages * per_page
        bpc = self.t.akniga.section_books_per_chunk
        out: List[str] = []
        for i in range(chunks):
            remaining = max(0, est_total - i * bpc)
            est = min(bpc, remaining)
            out.append(self.t._container(
                f"{AK_SECPAGE_PREFIX}{slug}:{i}",
                parent_id,
                f"Страница {i + 1}",
                est,
            ))
        return out


    def _ak_chapters(self, slug: str, parent_id: str) -> List[str]:
        out: List[str] = []
        track_dicts = []
        try:
            book = self.t.akniga.get_book(slug)
        except Exception as e:
            log.warning("akniga book %s: %s", slug, e)
            return out
        if not book:
            return out
        if getattr(book, "closed", False) and not book.mp3:
            log.info("akniga: %s — закрыта по подписке, глав нет", slug)
            return out
        album = book.title
        artist = book.performer or book.author
        if not self.t.akniga.ffmpeg_ok or len(book.chapters) <= 1:
            dur = book.chapters[-1].end if book.chapters else 0
            oid = f"akniga:ch:{book.bid}:all"
            url = f"{self.t.base_url}/stream/akniga/{book.bid}"
            out.append(self.t._item(
                oid,
                parent_id,
                book.title,
                url,
                mime="audio/mpeg",
                artist=artist,
                duration=dur,
                album=album,
            ))
            track_dicts.append({
                "oid": oid, "title": book.title, "url": url,
                "mime": "audio/mpeg", "artist": artist or "",
                "duration": dur or 0, "album": album or "",
            })
            if track_dicts:
                self.t.set_current_playlist("akniga", book.title, track_dicts)
            return out
        for ch in book.chapters:
            oid = f"akniga:ch:{book.bid}:{ch.index}"
            url = f"{self.t.base_url}/stream/akniga/{book.bid}/{ch.index}"
            title = ch.title or f"Глава {ch.index + 1}"
            if len(book.chapters) > 1:
                title = f"{ch.index + 1}. {title}"
            out.append(self.t._item(
                oid,
                parent_id,
                title,
                url,
                mime="audio/mpeg",
                artist=artist,
                duration=ch.duration,
                album=album,
            ))
            track_dicts.append({
                "oid": oid, "title": title, "url": url,
                "mime": "audio/mpeg", "artist": artist or "",
                "duration": ch.duration or 0, "album": album or "",
            })
        if track_dicts:
            self.t.set_current_playlist("akniga", book.title, track_dicts)
        return out


