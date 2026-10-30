"""Akniga.org — каталог аудиокниг и стрим глав (HLS → MP3 через ffmpeg).

Разметка сайта (проверено на живых HTML, 2026):
Основной список книг в разделе/поиске — это карточки
`content__main__articles--item content__main__articles--pageBooks`
с `data-id`/`data-bid`. Заголовок — `<h2 class="caption__article-main ">`.
Ссылка — `<a href="/<slug>" class="content__article-main-link">`.
Автор — `<a href="/author/...">Имя</a>`.

Боковая панель «Новинки»/«Интересное за неделю» лежит в отдельном
`<div class="content__main__side--wrapper">` и использует
`<span class="caption">…</span>` — её НЕ разбираем.

Книги, помеченные «Закрыто» (`caption__article-closed`), не показываются
в списках и не открываются: ajax /ajax/b/<bid> для них всегда возвращает
error=true.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from html import unescape
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

import requests
from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes
from Crypto.Util.Padding import pad, unpad

log = logging.getLogger("dlna_emu.akniga")

BASE = "https://akniga.org"
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

_HRES_PASSWORD = b"EKxtcg46V"

_TAG = re.compile(r"<[^>]+>")
_KEY_RE = re.compile(r"LIVESTREET_SECURITY_KEY\s*=\s*'([a-fA-F0-9]+)'")
_BID_RE = re.compile(
    r'<article[^>]*class="[^"]*book--page[^"]*"[^>]*data-bid="(\d+)"'
    r'|data-bid="(\d+)"[^>]*class="[^"]*book--page',
    re.I,
)

# ---- Разметка списка книг в разделе / поиске ------------------------------
_BOOKS_MARK = "content__main__articles--pageBooks"

_BOOK_SLUG = re.compile(
    r'<a\s+href="(?:https?://akniga\.org)?/(?P<slug>[a-z0-9][a-z0-9\-]+)"'
    r'\s+class="[^"]*content__article-main-link[^"]*"',
    re.I,
)
_BOOK_TITLE = re.compile(
    r'<h2\s+class="caption__article-main\s*">\s*(?P<t>.+?)\s*</h2>',
    re.I | re.S,
)
_BOOK_AUTHOR = re.compile(
    r'href="(?:https?://akniga\.org)?/author/[^"]*"[^>]*>\s*(?P<a>[^<]+?)\s*</a>',
    re.I,
)

# Признак «книга закрыта по подписке» в карточке списка.
_CLOSED_RE = re.compile(r'caption__article-closed', re.I)

_END_MARKERS = (
    'class="content__main__side--wrapper"',
    'class="paging"',
    '<button type="button" data-url=',
)

_PAGES_MAX = re.compile(r'/section/[a-z0-9_]+/page(\d+)/?', re.I)

_SECTION = re.compile(
    r'href="(?:https://akniga.org)?/section/(?P<slug>[a-z0-9_]+)/?"[^>]*>\s*(?:<[^>]+>\s*)*(?P<name>[^<]{2,80})',
    re.I,
)

_SKIP_SLUGS = {
    "section", "author", "performer", "search", "sections", "authors",
    "page", "login", "register", "help", "about", "contacts", "news",
    "tag", "tags", "audio", "genre", "genres", "series", "book", "books",
    "ajax", "player", "static", "img", "css", "js", "rss", "sitemap",
}

DEFAULT_SECTION_BOOKS_PER_CHUNK = 500
SECTION_INFO_CACHE_SEC = 1800
SECTION_CHUNK_CACHE_SEC = 1800
PARALLEL_WORKERS = 4
FETCH_DELAY = 0.05

GENRES: List[Tuple[str, str]] = [
    ("fantasy", "Фэнтези"),
    ("detective", "Детективы"),
    ("roman", "Романы"),
    ("uzhasy_mistika", "Ужасы и мистика"),
    ("classic", "Классика"),
    ("psihologiya", "Психология"),
    ("audiospektakli", "Аудиоспектакли"),
    ("priklucheniya", "Приключения"),
    ("istoriya", "История"),
    ("ranobe", "Ранобэ"),
    ("nauchno_populyarnye", "Научпоп"),
    ("humor", "Юмор"),
    ("spirituality", "Эзотерика"),
    ("business", "Бизнес"),
    ("obuchenie", "Обучение"),
    ("fantastika", "Фантастика"),
    ("detskie", "Детские"),
    ("poeziya", "Поэзия"),
    ("biografii", "Биографии"),
]


def _assets_password() -> str:
    chars = [0x79, 0x6D, 0x58, 0x45, 0x4B, 0x7A, 0x76, 0x55, 0x6B, 0x75, 0x6F, 0x35, 0x47, 0x30]
    result = "".join(chr(c) for c in chars)
    pi = f"{math.pi:.15f}"[:18]
    even = {0: "A", 2: "B", 4: "C", 6: "D", 8: "E"}
    for ch in pi:
        if ch.isdigit():
            d = int(ch)
            result += even[d] if d % 2 == 0 else ch
        else:
            result += ch
    return result


def _evp_bytes_to_key(password: bytes, salt: bytes, key_len: int = 32, iv_len: int = 16):
    derived = b""
    block = b""
    while len(derived) < key_len + iv_len:
        h = hashlib.md5()
        if block:
            h.update(block)
        h.update(password)
        if salt:
            h.update(salt)
        block = h.digest()
        derived += block
    return derived[:key_len], derived[key_len:key_len + iv_len]


def _encrypt_hash(security_key: str) -> str:
    plaintext = json.dumps(security_key).encode("utf-8")
    salt = get_random_bytes(8)
    key, iv = _evp_bytes_to_key(_assets_password().encode("utf-8"), salt)
    ct = AES.new(key, AES.MODE_CBC, iv).encrypt(pad(plaintext, 16))
    return json.dumps({"ct": base64.b64encode(ct).decode(), "s": salt.hex()}, separators=(",", ":"))


def _decrypt_hres(hres: str) -> str:
    data = json.loads(hres)
    ct = base64.b64decode(str(data["ct"]).replace("\\/", "/"))
    iv = bytes.fromhex(data["iv"])
    salt = bytes.fromhex(data["s"])
    key, _ = _evp_bytes_to_key(_HRES_PASSWORD, salt)
    raw = unpad(AES.new(key, AES.MODE_CBC, iv).decrypt(ct), 16)
    url = raw.decode("utf-8", errors="replace").strip().strip('"').replace("\\", "")
    if not url.startswith("http"):
        raise ValueError("hres is not a url")
    return url


def _clean(s: str) -> str:
    return unescape(_TAG.sub("", s or "")).strip()


@dataclass
class AkChapter:
    index: int
    title: str
    start: int
    end: int

    @property
    def duration(self) -> int:
        if self.end > self.start:
            return int(self.end - self.start)
        return 0


@dataclass
class AkBook:
    slug: str
    title: str
    author: str = ""
    performer: str = ""
    bid: str = ""
    cover: str = ""
    page_url: str = ""
    chapters: List[AkChapter] = field(default_factory=list)
    m3u8: str = ""
    mp3: str = ""
    fetched_at: float = 0.0
    closed: bool = False   # «Закрыто» — по подписке, ajax не отдаст


class AknigaClient:
    def __init__(
        self,
        cache_sec: int = 600,
        enabled: bool = True,
        ffmpeg: str = "",
        section_books_per_chunk: int = DEFAULT_SECTION_BOOKS_PER_CHUNK,
        chapter_kbps: int = 128,
    ):
        self.chapter_kbps = max(32, int(chapter_kbps or 128))
        self.cache_sec = int(cache_sec or 600)
        self._enabled = bool(enabled)
        self._ffmpeg = ffmpeg or shutil.which("ffmpeg") or ""
        self.section_books_per_chunk = max(
            1, int(section_books_per_chunk or DEFAULT_SECTION_BOOKS_PER_CHUNK)
        )
        self._http = requests.Session()
        self._http.headers.update({
            "User-Agent": UA,
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
        })
        self._lock = threading.Lock()
        self._books: Dict[str, AkBook] = {}
        self._by_bid: Dict[str, str] = {}
        # slug → (ts, total_pages, books_on_first_page)
        self._section_info: Dict[str, Tuple[float, int, int]] = {}
        # (slug, chunk_index) → (ts, [AkBook, ...])
        self._section_chunks: Dict[Tuple[str, int], Tuple[float, List[AkBook]]] = {}
        # slug → ts последней ajax-ошибки (негативный кэш)
        self._ajax_failed: Dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = bool(value)

    @property
    def ffmpeg_ok(self) -> bool:
        return bool(self._ffmpeg and os.path.isfile(self._ffmpeg))

    def _get(self, url: str, referer: str = BASE + "/") -> Optional[str]:
        try:
            r = self._http.get(url, headers={"Referer": referer}, timeout=25)
            if r.status_code != 200:
                log.warning("akniga GET %s -> %s", url, r.status_code)
                return None
            r.encoding = r.apparent_encoding or "utf-8"
            return r.text
        except Exception as e:
            log.warning("akniga GET %s: %s", url, e)
            return None

    # ------------------------------------------------------------------ #
    #  Жанры / главная / поиск
    # ------------------------------------------------------------------ #

    def list_genres(self) -> List[Tuple[str, str]]:
        body = self._get(f"{BASE}/sections/")
        found: List[Tuple[str, str]] = []
        seen = set()
        if body:
            for m in _SECTION.finditer(body):
                slug, name = m.group("slug"), _clean(m.group("name"))
                if slug in seen or not name or name.lower().startswith("показать"):
                    continue
                seen.add(slug)
                found.append((slug, name))
        return found or list(GENRES)

    def list_home(self, limit: int = 40) -> List[AkBook]:
        body = self._get(f"{BASE}/")
        return self._parse_cards(body or "")[:limit]

    def search_books(
        self,
        query: str,
        limit: int = 30,
        page: int = 0,
    ) -> List[AkBook]:
        """Поиск книг на akniga.org.

        page=0  — собрать несколько страниц до limit (для DLNA Search).
        page>=1 — только указанная страница (пагинация веб-поиска).
                  URL: /search/books/pageN/?q=...  (page1 = /search/books/?q=...)
        """
        q = (query or "").strip()
        if not q:
            return []
        limit = max(1, int(limit))
        page = int(page or 0)

        def _url(p: int) -> str:
            if p <= 1:
                return f"{BASE}/search/books/?q={quote(q)}"
            return f"{BASE}/search/books/page{p}/?q={quote(q)}"

        # Одна конкретная страница (веб-UI)
        if page >= 1:
            body = self._get(_url(page))
            if not body:
                return []
            return self._parse_cards(body)[:limit]

        # Несколько страниц до limit (DLNA / автосбор)
        out: List[AkBook] = []
        seen: set = set()
        p = 1
        max_pages = 10
        while len(out) < limit and p <= max_pages:
            body = self._get(_url(p))
            if not body:
                break
            cards = self._parse_cards(body)
            if not cards:
                break
            added = 0
            for b in cards:
                if b.slug in seen:
                    continue
                seen.add(b.slug)
                out.append(b)
                added += 1
                if len(out) >= limit:
                    break
            if added == 0:
                break
            p += 1
        return out[:limit]

    # ------------------------------------------------------------------ #
    #  Разделы
    # ------------------------------------------------------------------ #

    def list_section(self, slug: str, limit: int = 40) -> List[AkBook]:
        slug = (slug or "").strip().strip("/")
        if not slug:
            return []
        out: List[AkBook] = []
        seen: set = set()
        page = 1
        max_pages = 60
        while len(out) < limit and page <= max_pages:
            if page == 1:
                url = f"{BASE}/section/{quote(slug)}/"
            else:
                url = f"{BASE}/section/{quote(slug)}/page{page}/"
            body = self._get(url)
            if not body:
                break
            cards = self._parse_cards(body)
            if not cards:
                break
            added = 0
            for b in cards:
                if b.slug in seen:
                    continue
                seen.add(b.slug)
                out.append(b)
                added += 1
                if len(out) >= limit:
                    break
            if added == 0:
                break
            page += 1
        return out[:limit]

    def _detect_total_pages(self, html: str, slug: str) -> int:
        nums: List[int] = []
        rx = re.compile(re.escape(f"/section/{slug}/") + r"page(\d+)/?", re.I)
        for m in rx.finditer(html):
            try:
                n = int(m.group(1))
                if n > 0:
                    nums.append(n)
            except ValueError:
                pass
        return max(nums) if nums else 1

    def _load_section_info(self, slug: str) -> Tuple[int, int]:
        slug = (slug or "").strip().strip("/")
        if not slug:
            return (0, 0)
        now = time.time()
        with self._lock:
            cached = self._section_info.get(slug)
        if cached and (now - cached[0]) < SECTION_INFO_CACHE_SEC:
            return (cached[1], cached[2])

        body = self._get(f"{BASE}/section/{quote(slug)}/")
        if not body:
            return (0, 0)

        total_pages = self._detect_total_pages(body, slug)
        first_page_books = len(self._parse_cards(body))
        log.info(
            "akniga section info %s: pages=%d, books_on_first=%d",
            slug, total_pages, first_page_books,
        )
        with self._lock:
            self._section_info[slug] = (now, total_pages, first_page_books)
        return (total_pages, first_page_books)

    def section_layout(self, slug: str) -> Tuple[int, int, int]:
        slug = (slug or "").strip().strip("/")
        if not slug:
            return (0, 0, 0)
        total_pages, per_page = self._load_section_info(slug)
        if total_pages <= 0 or per_page <= 0:
            return (total_pages, per_page, 0)
        pages_per_chunk = max(1, self.section_books_per_chunk // per_page)
        n = max(1, (total_pages + pages_per_chunk - 1) // pages_per_chunk)
        return (total_pages, per_page, n)

    def list_section_chunk(self, slug: str, chunk_index: int) -> List[AkBook]:
        slug = (slug or "").strip().strip("/")
        if not slug:
            return []
        try:
            chunk_index = int(chunk_index)
        except (TypeError, ValueError):
            return []
        if chunk_index < 0:
            return []

        now = time.time()
        with self._lock:
            cached = self._section_chunks.get((slug, chunk_index))
        if cached and (now - cached[0]) < SECTION_CHUNK_CACHE_SEC:
            log.debug(
                "akniga chunk %s:%d — cache hit (%d books)",
                slug, chunk_index, len(cached[1]),
            )
            return list(cached[1])

        total_pages, per_page = self._load_section_info(slug)
        if total_pages <= 0 or per_page <= 0:
            return []
        pages_per_chunk = max(1, self.section_books_per_chunk // per_page)
        first = chunk_index * pages_per_chunk + 1
        last = min((chunk_index + 1) * pages_per_chunk, total_pages)
        if first > total_pages:
            return []

        urls: Dict[int, str] = {}
        for p in range(first, last + 1):
            if p == 1:
                urls[p] = f"{BASE}/section/{quote(slug)}/"
            else:
                urls[p] = f"{BASE}/section/{quote(slug)}/page{p}/"

        pages_html: Dict[int, Optional[str]] = {}
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as pool:
            futs = {pool.submit(self._get, u): p for p, u in urls.items()}
            for fut in as_completed(futs):
                p = futs[fut]
                try:
                    pages_html[p] = fut.result()
                except Exception as e:
                    log.debug("akniga page %s fetch: %s", p, e)
                    pages_html[p] = None
                if FETCH_DELAY > 0:
                    time.sleep(FETCH_DELAY)

        out: List[AkBook] = []
        seen: set = set()
        pages_ok = 0
        for p in range(first, last + 1):
            body = pages_html.get(p)
            if not body:
                continue
            pages_ok += 1
            cards = self._parse_cards(body)
            for b in cards:
                if b.slug in seen:
                    continue
                seen.add(b.slug)
                out.append(b)
                if len(out) >= self.section_books_per_chunk:
                    break
            if len(out) >= self.section_books_per_chunk:
                break

        log.info(
            "akniga chunk %s:%d — %d books (pages %d..%d, fetched %d/%d, %.1fs)",
            slug, chunk_index, len(out), first, last, pages_ok, last - first + 1,
            time.time() - t0,
        )
        with self._lock:
            self._section_chunks[(slug, chunk_index)] = (now, out)
        return out

    def invalidate_section(self, slug: str = "") -> None:
        slug = (slug or "").strip().strip("/")
        with self._lock:
            if slug:
                self._section_info.pop(slug, None)
                for k in [k for k in self._section_chunks if k[0] == slug]:
                    self._section_chunks.pop(k, None)
            else:
                self._section_info.clear()
                self._section_chunks.clear()

    # ------------------------------------------------------------------ #
    #  Парсинг карточек
    # ------------------------------------------------------------------ #

    def _remember(self, book: AkBook) -> AkBook:
        with self._lock:
            prev = self._books.get(book.slug)
            if prev and prev.bid and not book.bid:
                book.bid = prev.bid
                book.cover = prev.cover or book.cover
                book.chapters = prev.chapters
                book.mp3 = prev.mp3
                book.performer = prev.performer or book.performer
                book.fetched_at = prev.fetched_at
                book.closed = book.closed or prev.closed
            elif prev and prev.bid and book.bid == prev.bid and not book.mp3:
                book.mp3 = prev.mp3
                book.chapters = prev.chapters or book.chapters
                book.cover = book.cover or prev.cover
                book.fetched_at = prev.fetched_at
                book.closed = book.closed or prev.closed
            self._books[book.slug] = book
            if book.bid:
                self._by_bid[book.bid] = book.slug
        return book

    def _parse_cards(self, html: str) -> List[AkBook]:
        if not html:
            return []

        idxs: List[int] = []
        pos = 0
        while True:
            i = html.find(_BOOKS_MARK, pos)
            if i < 0:
                break
            idxs.append(i)
            pos = i + len(_BOOKS_MARK)
        if not idxs:
            log.debug("akniga parse: маркер %s не найден", _BOOKS_MARK)
            return []

        end_last = len(html)
        for m in _END_MARKERS:
            j = html.rfind(m)
            if j > idxs[-1] and j < end_last:
                end_last = j

        out: List[AkBook] = []
        seen: set = set()
        for k, start in enumerate(idxs):
            end = idxs[k + 1] if k + 1 < len(idxs) else end_last
            chunk = html[start:end]

            sm = _BOOK_SLUG.search(chunk)
            tm = _BOOK_TITLE.search(chunk)
            if not sm or not tm:
                continue
            slug = sm.group("slug")
            if slug in seen or slug in _SKIP_SLUGS:
                continue
            seen.add(slug)

            raw_title = _clean(tm.group("t"))
            if not raw_title:
                continue

            title = raw_title
            author = ""
            for sep in (" – ", " — ", " - "):
                if sep in raw_title:
                    a, _, t = raw_title.partition(sep)
                    if a.strip() and t.strip():
                        author, title = a.strip(), t.strip()
                        break

            am = _BOOK_AUTHOR.search(chunk)
            if am:
                a = _clean(am.group("a"))
                if a:
                    author = a

            out.append(self._remember(AkBook(
                slug=slug, title=title, author=author,
                page_url=f"{BASE}/{slug}",
                closed=bool(_CLOSED_RE.search(chunk)),
            )))

        if out:
            log.info(
                "akniga parse: %d карточек, первая slug=%s title=%s",
                len(out), out[0].slug, out[0].title[:40],
            )
        return out

    # ------------------------------------------------------------------ #
    #  Книга целиком
    # ------------------------------------------------------------------ #

    def get_book(self, slug: str, force: bool = False) -> Optional[AkBook]:
        slug = (slug or "").strip().strip("/")
        with self._lock:
            cached = self._books.get(slug)
        if (
            cached and cached.bid and cached.mp3 and cached.chapters
            and not force and time.time() - cached.fetched_at < self.cache_sec
        ):
            return cached

        # Книга помечена «Закрыто» — ajax её не отдаст.
        if cached and cached.closed and not force:
            log.debug("akniga: %s — закрыта по подписке, пропускаю", slug)
            cached.fetched_at = time.time()
            return cached

        # Негативный кэш: недавно уже была ajax-ошибка — не долбим сайт.
        with self._lock:
            fail_ts = self._ajax_failed.get(slug)
        if fail_ts and (time.time() - fail_ts) < self.cache_sec and not force:
            log.debug("akniga: %s — недавно была ошибка, пропускаю", slug)
            return cached

        page = cached.page_url if cached and cached.page_url else f"{BASE}/{slug}"
        html = self._get(page)
        if not html:
            return cached
        km = _KEY_RE.search(html)
        bm = _BID_RE.search(html)
        bid = ""
        if bm:
            bid = bm.group(1) or bm.group(2) or ""
        if not bid:
            m2 = re.search(r'data-bid="(\d+)"', html)
            bid = m2.group(1) if m2 else ""
        if not km or not bid:
            log.warning("akniga: no key/bid for %s", slug)
            return cached

        # Если страница явно помечена «Закрыто», не пытаемся дёргать ajax.
        if _CLOSED_RE.search(html):
            if not cached:
                cached = AkBook(slug=slug, title=slug, page_url=page)
            cached.closed = True
            cached.fetched_at = time.time()
            self._remember(cached)
            log.info("akniga: %s — закрыта (страница книги), пропускаю", slug)
            return cached

        security = km.group(1)
        headers = {
            "Referer": page,
            "Origin": BASE,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }
        try:
            tr = self._http.post(
                f"{BASE}/ajax/player/token",
                data={"bid": bid, "ts": str(int(time.time() * 1000)), "security_ls_key": security},
                headers=headers,
                timeout=25,
            )
            tr.raise_for_status()
            token = (tr.json() or {}).get("token") or ""
            if not token:
                log.warning("akniga: empty token for %s", slug)
                return cached
            r = self._http.post(
                f"{BASE}/ajax/b/{bid}",
                data={"bid": bid, "token": token, "hls": "false", "security_ls_key": security},
                headers=headers,
                timeout=25,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.warning("akniga ajax %s: %s", slug, e)
            with self._lock:
                self._ajax_failed[slug] = time.time()
            return cached

        if data.get("error"):
            reason = data.get("error_reason") or data.get("error")
            log.warning(
                "akniga ajax %s: error=%r (поля: %s)",
                slug, reason, ",".join(list(data.keys())[:12]) or "-",
            )
            log.debug(
                "akniga ajax %s raw: %s",
                slug, json.dumps(data, ensure_ascii=False)[:1000],
            )
            with self._lock:
                self._ajax_failed[slug] = time.time()
            if not cached:
                cached = AkBook(slug=slug, title=slug, page_url=page)
            cached.closed = True
            cached.fetched_at = time.time()
            self._remember(cached)
            return cached

        try:
            mp3 = _decrypt_hres(data.get("res") or "")
        except Exception as e:
            log.warning("akniga res %s: %s", slug, e)
            with self._lock:
                self._ajax_failed[slug] = time.time()
            return cached

        chapters: List[AkChapter] = []
        raw_items = data.get("items") or "[]"
        try:
            items = json.loads(raw_items) if isinstance(raw_items, str) else raw_items
        except Exception:
            items = []
        for i, ch in enumerate(items or []):
            chapters.append(AkChapter(
                index=i,
                title=_clean(str(ch.get("title") or f"Глава {i + 1}")) or f"Глава {i + 1}",
                start=int(ch.get("time_from_start") or 0),
                end=int(ch.get("time_finish") or 0),
            ))
        if not chapters:
            chapters.append(AkChapter(0, "Вся книга", 0, 0))

        perf = _clean(re.sub(r"<[^>]+>", " ", str(data.get("performer") or data.get("sTextPerformer") or "")))
        title = _clean(str(data.get("titleonly") or data.get("title") or (cached.title if cached else slug)))
        author = _clean(str(data.get("author") or (cached.author if cached else "")))
        cover = str(data.get("preview") or "")
        book = AkBook(
            slug=slug,
            title=title or (cached.title if cached else slug),
            author=author,
            performer=perf,
            bid=str(bid),
            cover=cover,
            page_url=page,
            chapters=chapters,
            mp3=mp3,
            fetched_at=time.time(),
            closed=False,
        )
        return self._remember(book)

    def book_by_bid(self, bid: str) -> Optional[AkBook]:
        with self._lock:
            slug = self._by_bid.get(str(bid))
            cached = self._books.get(slug) if slug else None
        if cached and cached.mp3 and time.time() - cached.fetched_at < self.cache_sec:
            return cached
        if slug:
            return self.get_book(slug, force=True)
        return cached

    def cover_url(self, bid: str) -> str:
        book = self.book_by_bid(bid)
        return book.cover if book else ""

    # ------------------------------------------------------------------ #
    #  Стриминг
    # ------------------------------------------------------------------ #

    def open_mp3(self, bid: str, range_header: str = ""):
        book = self.book_by_bid(bid)
        if not book or not book.mp3:
            return None
        headers = {"User-Agent": UA, "Referer": book.page_url or BASE + "/"}
        if range_header:
            headers["Range"] = range_header
        try:
            r = self._http.get(book.mp3, headers=headers, stream=True, timeout=30)
            if r.status_code not in (200, 206):
                log.warning("akniga mp3 %s -> %s", bid, r.status_code)
                r.close()
                return None
            return r
        except Exception as e:
            log.warning("akniga mp3 %s: %s", bid, e)
            return None

    # CBR MP3 @ 48 кГц: кадр = 1152 сэмплов = 24 мс = ровно 3*kbps байт.
    # Поэтому размер главы известен ДО кодирования → можно отдавать
    # Content-Length + Range (длина трека и перемотка, без обрывов).
    CH_SR = 48000
    CH_FRAME_SEC = 1152 / 48000.0   # 0.024

    @property
    def chapter_frame_bytes(self) -> int:
        return 3 * self.chapter_kbps

    def find_chapter(self, bid: str, index: int):
        book = self.book_by_bid(bid)
        if not book or not book.mp3:
            return None, None
        ch = None
        for c in book.chapters:
            if c.index == index:
                ch = c
                break
        if ch is None and book.chapters:
            ch = book.chapters[0]
        return book, ch

    def chapter_total_bytes(self, ch) -> int:
        """Точный размер главы в байтах (0 — длина главы неизвестна)."""
        if ch is None or ch.duration <= 0:
            return 0
        frames = int(math.ceil(ch.duration / self.CH_FRAME_SEC))
        return frames * self.chapter_frame_bytes

    def open_chapter_mp3(
        self, bid: str, index: int, frame_offset: int = 0,
    ) -> Optional[subprocess.Popen]:
        """ffmpeg → CBR MP3 в stdout, начиная с кадра frame_offset главы."""
        if not self.ffmpeg_ok:
            log.error("akniga: ffmpeg не найден, главу не отдать")
            return None
        book, ch = self.find_chapter(bid, index)
        if book is None or ch is None:
            log.warning("akniga: нет mp3/глав bid=%s", bid)
            return None
        off = frame_offset * self.CH_FRAME_SEC
        cmd = [
            self._ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-user_agent", UA,
            "-referer", book.page_url or (BASE + "/"),
            # не обрывать выдачу при сетевых сбоях источника
            "-reconnect", "1", "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
        ]
        if ch.start + off > 0:
            cmd += ["-ss", f"{ch.start + off:.3f}"]
        cmd += ["-i", book.mp3]
        if ch.duration > 0:
            # +0.5с запас: сервер всё равно обрежет ровно по Content-Length;
            # apad дополняет тишиной, если файл короче заявленной главы
            cmd += ["-af", "apad", "-t", f"{max(0.0, ch.duration - off) + 0.5:.3f}"]
        cmd += [
            "-vn", "-acodec", "libmp3lame", "-ar", str(self.CH_SR), "-ac", "2",
            "-b:a", f"{self.chapter_kbps}k",
            "-write_xing", "0", "-id3v2_version", "0", "-write_id3v1", "0",
            "-f", "mp3", "pipe:1",
        ]
        try:
            return subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            log.warning("akniga ffmpeg: %s", e)
            return None
