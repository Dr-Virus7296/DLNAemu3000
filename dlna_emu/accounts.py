"""Мультиаккаунты стриминговых сервисов (Deezer, Яндекс, Звук, КИОН).

Один сервис = один AccountPool = несколько клиентов (по одному на токен).
Пул ведёт себя как «текущий» клиент: любые атрибуты и методы
(`pool.search_tracks(...)`, `pool.enabled`, `pool.quality = ...`) пробрасываются
клиенту активного аккаунта. Активный аккаунт задаётся на время запроса:

    with pool.use(1):
        pool.get_playlists()      # второй аккаунт

Вне `use()` активен первый аккаунт — старый однопользовательский код,
который ничего не знает про аккаунты, продолжает работать как раньше.
"""

from __future__ import annotations

import contextlib
import logging
import re
import threading
import time
from typing import Any, Callable, Iterator, List, Optional, Sequence

log = logging.getLogger("dlna_emu.accounts")

# Сервисы, у которых есть выбор аккаунта (корневой id контейнера совпадает с ключом)
MULTI_SERVICES = ("deezer", "yandex", "zvuk", "kion")

# Ключ токена в config.yaml для каждого сервиса
TOKEN_KEYS = {
    "deezer": "arl",
    "yandex": "token",
    "zvuk": "token",
    "kion": "token",
}

_SPLIT_RE = re.compile(r"[,;\r\n]+")


def split_tokens(value: Any) -> List[str]:
    """'a, b,c' | ['a', 'b'] → ['a', 'b', 'c'] (без пустых и повторов, порядок сохранён).

    Делим только по запятой / «;» / переводу строки — пробелы внутри токена
    допустимы (например «OAuth xxx» у КИОН).
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        parts: List[str] = []
        for v in value:
            parts.extend(split_tokens(v))
        return parts
    out: List[str] = []
    seen = set()
    for raw in _SPLIT_RE.split(str(value)):
        tok = raw.strip().strip('"').strip("'").strip()
        if tok and tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def join_tokens(tokens: Any) -> str:
    """Список/строка токенов → "a,b,c" (как хранится в config.yaml)."""
    return ",".join(split_tokens(tokens))


class AccountPool:
    """Набор клиентов одного сервиса + прозрачный прокси к активному клиенту."""

    _OWN = frozenset((
        "service", "_factory", "_clients", "_tokens", "_names", "_tls", "_lock",
        "_ka_on", "_ka_thread", "_ka_due", "_ka_fail", "_ka_range",
    ))

    def __init__(self, service: str, factory: Callable[[str], Any], tokens: Any = ()):
        object.__setattr__(self, "service", service)
        object.__setattr__(self, "_factory", factory)
        object.__setattr__(self, "_clients", [])
        object.__setattr__(self, "_tokens", [])
        object.__setattr__(self, "_names", [])
        object.__setattr__(self, "_tls", threading.local())
        object.__setattr__(self, "_lock", threading.RLock())
        object.__setattr__(self, "_ka_on", False)
        object.__setattr__(self, "_ka_thread", None)
        object.__setattr__(self, "_ka_due", {})     # token -> когда проверять в следующий раз
        object.__setattr__(self, "_ka_fail", {})    # token -> подряд неудачных проверок
        object.__setattr__(self, "_ka_range", (40 * 60, 80 * 60))
        self._rebuild(split_tokens(tokens))

    # ------------------------------------------------------------ состав

    def _rebuild(self, tokens: List[str]) -> List[int]:
        """Привести список клиентов к списку токенов. Возвращает индексы новых клиентов.

        Клиенты с неизменившимся токеном переиспользуются — сессии и кэши остаются.
        Пустой список токенов → один «пустой» клиент (enabled=False), чтобы код,
        который обращается к клиенту, не падал.
        """
        with self._lock:
            old = dict(zip(self._tokens, self._clients))
            clients: List[Any] = []
            fresh: List[int] = []
            for i, tok in enumerate(tokens):
                cli = old.get(tok)
                if cli is None:
                    cli = self._factory(tok)
                    fresh.append(i)
                clients.append(cli)
            if not clients:
                clients = [old.get("") or self._factory("")]
            self._clients[:] = clients
            self._tokens[:] = list(tokens)
            self._names[:] = [""] * len(clients)
            return fresh

    def set_tokens(self, tokens: Any, login: bool = True) -> None:
        """Применить новый список токенов (из веб-панели) без перезапуска."""
        toks = split_tokens(tokens)
        fresh = self._rebuild(toks)
        if login and fresh:
            self.login_all(only=fresh)
        elif self._names:
            self.refresh_names()

    @property
    def count(self) -> int:
        """Сколько аккаунтов с токеном."""
        return len(self._tokens)

    @property
    def multi(self) -> bool:
        """Нужен ли уровень «выбор аккаунта» (аккаунтов два и больше)."""
        return len(self._tokens) > 1

    @property
    def tokens(self) -> List[str]:
        return list(self._tokens)

    @property
    def clients(self) -> List[Any]:
        return list(self._clients) if self._tokens else []

    def client(self, idx: int = 0) -> Any:
        with self._lock:
            if 0 <= idx < len(self._clients):
                return self._clients[idx]
            return self._clients[0]

    # ------------------------------------------------------- активный клиент

    @property
    def index(self) -> int:
        """Активный аккаунт в этом потоке (0 — если не задан явно)."""
        idx = getattr(self._tls, "idx", None)
        if idx is None or idx >= len(self._clients):
            return 0
        return idx

    @property
    def scoped(self) -> bool:
        """Задан ли аккаунт явно (внутри `with pool.use(i)`)."""
        return getattr(self._tls, "idx", None) is not None

    @contextlib.contextmanager
    def use(self, idx: int) -> Iterator["AccountPool"]:
        prev = getattr(self._tls, "idx", None)
        self._tls.idx = int(idx)
        try:
            yield self
        finally:
            self._tls.idx = prev

    def current(self) -> Any:
        return self.client(self.index)

    # ----------------------------------------------------------------- имена

    @staticmethod
    def _client_name(cli: Any) -> str:
        for attr in ("_user_name", "user_name", "account_name"):
            v = getattr(cli, attr, None)
            if callable(v):
                try:
                    v = v()
                except Exception:
                    v = None
            v = (str(v).strip() if v else "")
            if v and v not in ("?", "Deezer"):
                return v
        return ""

    def refresh_names(self) -> List[str]:
        """Пересчитать отображаемые имена (имя берётся из профиля, найденного по токену)."""
        with self._lock:
            names: List[str] = []
            used: dict = {}
            for i, cli in enumerate(self._clients):
                if not self._tokens:
                    break
                base = self._client_name(cli) or f"Аккаунт {i + 1}"
                n = used.get(base, 0) + 1
                used[base] = n
                names.append(base if n == 1 else f"{base} ({n})")
            self._names[:] = names or [""]
            return list(self._names)

    def names(self) -> List[str]:
        """Имена всех аккаунтов. Если профиль ещё не прочитан — пробуем войти."""
        with self._lock:
            need = [
                i for i, c in enumerate(self._clients)
                if self._tokens and not self._client_name(c)
                and getattr(c, "_login_tried", False) is False
            ]
        if need:
            self.login_all(only=need)
        return self.refresh_names() if self._tokens else []

    def name(self, idx: int) -> str:
        names = self.names()
        if 0 <= idx < len(names):
            return names[idx]
        return f"Аккаунт {idx + 1}"

    # ----------------------------------------------------------------- вход

    def login_all(self, only: Optional[Sequence[int]] = None) -> None:
        """Войти во все аккаунты (параллельно — чтобы старт не ждал каждый по очереди)."""
        if not self._tokens:
            return
        idxs = list(only) if only is not None else list(range(len(self._clients)))

        def _one(i: int) -> None:
            cli = self.client(i)
            try:
                if hasattr(cli, "login"):
                    cli.login()
            except Exception as e:  # noqa: BLE001
                log.warning("%s: вход в аккаунт #%d: %s", self.service, i + 1, e)
            finally:
                try:
                    cli._login_tried = True
                except Exception:
                    pass

        threads = [threading.Thread(target=_one, args=(i,), daemon=True) for i in idxs]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.refresh_names()
        log.info(
            "%s: аккаунтов %d (%s)",
            self.service, self.count, ", ".join(self._names) or "нет",
        )

    # ------------------------------------------------- поддержание сессии

    def set_keepalive(self, enabled: bool, min_minutes: float = 40, max_minutes: float = 80) -> None:
        """Раз в 40–80 минут (у каждого аккаунта свой случайный момент) дёргать профиль,
        чтобы «скользящий» токен не протухал от простоя. Включается/выключается на лету."""
        lo, hi = sorted((float(min_minutes), float(max_minutes)))
        self._ka_range = (max(1.0, lo) * 60, max(1.0, hi) * 60)
        self._ka_on = bool(enabled)
        if self._ka_on and (self._ka_thread is None or not self._ka_thread.is_alive()):
            t = threading.Thread(target=self._ka_loop, name=f"keepalive-{self.service}", daemon=True)
            self._ka_thread = t
            t.start()

    def _ka_schedule(self, tok: str, first: bool = False) -> None:
        import random
        lo, hi = self._ka_range
        # первый раз — случайно в пределах диапазона, чтобы аккаунты не стучались разом
        self._ka_due[tok] = time.time() + (random.uniform(lo * 0.25, hi) if first else random.uniform(lo, hi))

    def _ka_loop(self) -> None:
        while True:
            time.sleep(20)
            if not self._ka_on:
                continue
            try:
                with self._lock:
                    pairs = list(zip(self._tokens, self._clients))
                for tok, cli in pairs:
                    if tok not in self._ka_due:
                        self._ka_schedule(tok, first=True)
                        continue
                    if time.time() < self._ka_due[tok]:
                        continue
                    ok = False
                    try:
                        ok = bool(cli.login())
                    except Exception as e:  # noqa: BLE001
                        log.debug("%s keepalive: %s", self.service, e)
                    if ok:
                        self._ka_fail[tok] = 0
                        self._ka_schedule(tok)
                        log.info("%s: сессия аккаунта «%s» проверена", self.service,
                                 self._client_name(cli) or "?")
                    else:
                        n = self._ka_fail.get(tok, 0) + 1
                        self._ka_fail[tok] = n
                        # одиночный сбой сети не считаем: повтор через 5 минут
                        self._ka_due[tok] = time.time() + (300 if n < 2 else self._ka_range[0])
                        if n >= 2:
                            log.warning("%s: токен аккаунта «%s» не принят — нужен новый",
                                        self.service, self._client_name(cli) or "?")
                    self._ka_due = {t: d for t, d in self._ka_due.items() if t in self._tokens}
            except Exception as e:  # noqa: BLE001
                log.debug("%s keepalive loop: %s", self.service, e)

    # ------------------------------------------------------- прокси к клиенту

    @property
    def enabled(self) -> bool:
        """Включён ли сервис: есть хотя бы один аккаунт с токеном."""
        return bool(self._tokens) and any(getattr(c, "enabled", False) for c in self._clients)

    def __bool__(self) -> bool:
        return True

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") or name in AccountPool._OWN:
            raise AttributeError(name)
        return getattr(self.current(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in AccountPool._OWN:
            object.__setattr__(self, name, value)
        else:
            setattr(self.current(), name, value)

    def set_all(self, **attrs: Any) -> None:
        """Выставить атрибут у всех аккаунтов (например quality, cache_sec)."""
        for cli in self._clients:
            for k, v in attrs.items():
                try:
                    setattr(cli, k, v)
                except Exception:
                    pass
