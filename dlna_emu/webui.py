"""Веб-панель управления DLNA-emu (отдельный порт)."""

from __future__ import annotations

import html
import logging
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict
from urllib.parse import parse_qs, quote, urlparse

log = logging.getLogger("dlna_emu.webui")

_STATE: Dict[str, Any] = {}

# ============================================================
#  Буфер логов
# ============================================================

_LOG_BUFFER: deque = deque(maxlen=2000)
_LOG_LOCK = threading.Lock()
_LOG_COUNTER = 0
_LOG_COUNTER_LOCK = threading.Lock()
_LOG_HANDLER_INSTALLED = False
_STREAMS_INSTALLED = False


class _WebLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        global _LOG_COUNTER
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        with _LOG_COUNTER_LOCK:
            _LOG_COUNTER += 1
            idx = _LOG_COUNTER
        entry = {
            "i": idx,
            "t": record.created,
            "lvl": record.levelname,
            "n": record.name,
            "m": msg,
        }
        with _LOG_LOCK:
            _LOG_BUFFER.append(entry)


class _StreamTee:
    def __init__(self, original, name: str, level_name: str):
        self._original = original
        self._name = name
        self._level = level_name
        self._lock = threading.Lock()
        self._pending = ""

    def write(self, data):
        if not data:
            return 0
        try:
            self._original.write(data)
            self._original.flush()
        except Exception:
            pass
        with self._lock:
            self._pending += data
            while "\n" in self._pending:
                line, self._pending = self._pending.split("\n", 1)
                self._emit(line.rstrip("\r"))
        return len(data)

    def flush(self):
        try:
            self._original.flush()
        except Exception:
            pass

    def _emit(self, line: str):
        if not line:
            return
        global _LOG_COUNTER
        with _LOG_COUNTER_LOCK:
            _LOG_COUNTER += 1
            idx = _LOG_COUNTER
        with _LOG_LOCK:
            _LOG_BUFFER.append({
                "i": idx, "t": time.time(),
                "lvl": self._level, "n": self._name, "m": line,
            })

    def isatty(self):
        try:
            return self._original.isatty()
        except Exception:
            return False

    def fileno(self):
        return self._original.fileno()


def install_log_handler() -> None:
    global _LOG_HANDLER_INSTALLED, _STREAMS_INSTALLED
    if not _LOG_HANDLER_INSTALLED:
        h = _WebLogHandler()
        h.setLevel(logging.DEBUG)
        logging.getLogger().addHandler(h)
        _LOG_HANDLER_INSTALLED = True
    if not _STREAMS_INSTALLED:
        if not isinstance(sys.stdout, _StreamTee):
            sys.stdout = _StreamTee(sys.stdout, "stdout", "STDOUT")  # type: ignore
        if not isinstance(sys.stderr, _StreamTee):
            sys.stderr = _StreamTee(sys.stderr, "stderr", "STDERR")  # type: ignore
        _STREAMS_INSTALLED = True


# ============================================================
#  Состояние
# ============================================================

def set_state(**kwargs) -> None:
    _STATE.update(kwargs)


def _cfg() -> dict:
    return _STATE.get("cfg") or {}


def _config_path() -> str:
    return _STATE.get("config_path") or "config.yaml"


def _save_yaml(cfg: dict, path: str) -> None:
    try:
        import yaml
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    except Exception:
        lines = []
        for section, vals in cfg.items():
            if not isinstance(vals, dict):
                lines.append(f"{section}: {vals}")
                continue
            lines.append(f"{section}:")
            for k, v in vals.items():
                if isinstance(v, bool):
                    vv = "true" if v else "false"
                elif v is None:
                    vv = '""'
                else:
                    vv = str(v)
                lines.append(f"  {k}: {vv}")
            lines.append("")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))


# ============================================================
#  Шаблон
# ============================================================

def _page(title: str, body: str, flash: str = "", active: str = "") -> bytes:
    if flash:
        is_err = "лимит" in flash.lower() or "ошибка" in flash.lower() or "не удалось" in flash.lower()
        cls = "flash err" if is_err else "flash"
        flash_html = f'<div class="{cls}">{html.escape(flash)}</div>'
    else:
        flash_html = ""

    def cls(name: str) -> str:
        return ' class="active"' if active == name else ""

    doc = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} — DLNA-emu</title>
<style>
  :root {{ --bg:#0f1419; --card:#1a2332; --text:#e7ecf3; --muted:#8b9bb4; --acc:#3d8bfd; --ok:#3dd68c; --err:#f07178; --warn:#f0c36a; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font-family:system-ui,-apple-system,sans-serif; background:var(--bg); color:var(--text); line-height:1.45; }}
  header {{ background:var(--card); padding:14px 18px; border-bottom:1px solid #243044; display:flex; flex-wrap:wrap; gap:12px; align-items:center; }}
  header h1 {{ margin:0; font-size:18px; font-weight:600; }}
  header nav {{ display:flex; gap:18px; align-items:center; }}
  header nav a {{ color:var(--muted); text-decoration:none; font-size:14px; padding:4px 0; border-bottom:2px solid transparent; }}
  header nav a:hover {{ color:var(--text); }}
  header nav a.active {{ color:var(--acc); border-bottom-color:var(--acc); }}
  .console-btn {{ margin-left:auto; background:linear-gradient(135deg,#3d8bfd,#2563eb); color:#fff !important; padding:9px 18px; border-radius:8px; font-size:14px; font-weight:600; text-decoration:none; display:inline-flex; align-items:center; gap:6px; box-shadow:0 2px 8px rgba(61,139,253,.35); }}
  main {{ max-width:960px; margin:0 auto; padding:18px 14px 40px; }}
  .card {{ background:var(--card); border-radius:12px; padding:16px 18px; margin-bottom:14px; border:1px solid #243044; }}
  .card h2 {{ margin:0 0 12px; font-size:16px; }}
  label {{ display:block; margin:10px 0 4px; color:var(--muted); font-size:13px; }}
  input, select, textarea {{ width:100%; padding:10px 12px; border-radius:8px; border:1px solid #2d3a4f; background:#0d121a; color:var(--text); font-size:14px; }}
  textarea {{ min-height:72px; font-family:ui-monospace,monospace; font-size:12px; }}
  .row {{ display:flex; gap:10px; flex-wrap:wrap; margin-top:14px; align-items:center; }}
  button, .btn {{ appearance:none; border:0; border-radius:8px; padding:10px 16px; font-size:14px; cursor:pointer; background:var(--acc); color:#fff; text-decoration:none; display:inline-block; font-family:inherit; }}
  button.ghost, .btn.ghost {{ background:#243044; color:var(--text); }}
  button.small {{ padding:6px 12px; font-size:13px; }}
  .muted {{ color:var(--muted); font-size:13px; }}
  .badge {{ display:inline-block; padding:2px 8px; border-radius:999px; font-size:12px; }}
  .badge.ok {{ background:#1a3d2e; color:var(--ok); }}
  .badge.off {{ background:#3a2a2a; color:var(--err); }}
  .flash {{ background:#1a3d2e; color:var(--ok); padding:10px 14px; border-radius:8px; margin-bottom:14px; }}
  .flash.err {{ background:#3a2a2a; color:var(--err); }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  td, th {{ text-align:left; padding:6px 4px; border-bottom:1px solid #243044; }}
  code {{ background:#0d121a; padding:1px 6px; border-radius:4px; font-size:12px; }}
  .logs {{ background:#0d121a; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; line-height:1.5; padding:12px; border-radius:8px; height:65vh; overflow-y:auto; white-space:pre-wrap; word-break:break-word; color:#c8d3e0; border:1px solid #243044; }}
  .logs .lvl-ERROR, .logs .lvl-STDERR {{ color: #f07178; }}
  .logs .lvl-WARNING {{ color: #f0c36a; }}
  .logs .lvl-INFO {{ color: #a8d8ff; }}
  .logs .lvl-DEBUG {{ color: #8b9bb4; }}
  .logs .lvl-STDOUT {{ color: #c8d3e0; }}
  .logs .ts {{ color: #6b7c93; margin-right:6px; }}
  .logs .name {{ color: #7a95c9; }}
  .logs-toolbar {{ display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin-bottom:10px; }}
  .search-card {{ background:#0d121a; border:1px solid #243044; border-radius:8px; padding:12px 14px; margin-bottom:10px; display:flex; gap:12px; align-items:center; flex-wrap:wrap; }}
  .search-card .name {{ font-weight:600; color:var(--text); }}
  .search-card .desc {{ color:var(--muted); font-size:13px; }}
  .search-card .actions {{ margin-left:auto; display:flex; gap:8px; }}
</style>
</head>
<body>
<header>
  <h1>DLNA-emu</h1>
  <nav>
    <a href="/"{cls("status")}>Статус</a>
    <a href="/settings"{cls("settings")}>Настройки</a>
    <a href="/radio"{cls("radio")}>Радио</a>
    <a href="/search"{cls("search")}>🔍 Поиск</a>
    <a href="/favorites"{cls("favorites")}>⭐ Избранное</a>
    <a href="/current"{cls("current")}>🕘 Текущие плейлисты</a>
  </nav>
  <a href="/logs" class="console-btn">⌨ Консоль</a>
</header>
<main>
{flash_html}
{body}
</main>
</body>
</html>"""
    return doc.encode("utf-8")


# ============================================================
#  Перезапуск службы
# ============================================================

INIT_SCRIPT = "/opt/etc/init.d/S99dlna-emu"


def _exec_self() -> None:
    """Запасной вариант без init-скрипта: процесс заново запускает сам себя."""
    try:
        os.execv(sys.executable, [sys.executable, "-m", "dlna_emu"] + sys.argv[1:])
    except Exception as e:  # pragma: no cover
        log.error("restart exec failed: %s", e)


def restart_service() -> str:
    """Перезапуск после отправки ответа браузеру.

    Если есть init-скрипт — зовём stop/start в отдельной сессии (иначе stop
    убьёт и сам перезапуск). Нет скрипта — exec самого себя.
    Возвращает 'init' или 'exec'.
    """
    if os.path.isfile(INIT_SCRIPT) and os.access(INIT_SCRIPT, os.X_OK):
        # в команде нет подстроки «dlna_emu», чтобы pkill -f из stop её не задел
        cmd = f"sleep 1; {INIT_SCRIPT} stop; sleep 1; {INIT_SCRIPT} start"
        subprocess.Popen(
            ["/bin/sh", "-c", cmd],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )
        log.warning("Restart requested: %s stop/start", INIT_SCRIPT)
        return "init"
    log.warning("Restart requested: init-скрипт не найден, перезапуск через exec")
    threading.Timer(1.0, _exec_self).start()
    return "exec"


def _restarting_body(port: int) -> str:
    return """
<div class="card">
  <h2>Перезапуск службы…</h2>
  <p class="muted" id="rs-msg">Подожди несколько секунд, страница обновится сама.</p>
</div>
<script>
(function () {
  var port = __PORT__, tries = 0, started = Date.now();
  var url = location.protocol + '//' + location.hostname + ':' + port + '/';
  function ping() {
    tries++;
    fetch(url, {mode: 'no-cors', cache: 'no-store'}).then(function () {
      // сервер может ответить ещё до остановки — даём ему время упасть
      if (Date.now() - started < 3500) { setTimeout(ping, 800); return; }
      location.href = url;
    }).catch(function () {
      if (tries > 60) {
        document.getElementById('rs-msg').textContent =
          'Служба не отвечает. Проверь лог: /opt/var/log/dlna-emu.log';
        return;
      }
      setTimeout(ping, 1000);
    });
  }
  setTimeout(ping, 1500);
})();
</script>
""".replace("__PORT__", str(int(port)))


# ============================================================
#  Страницы
# ============================================================

def _devices_card() -> str:
    try:
        from .server import get_active_clients
        clients = get_active_clients()
    except Exception:
        clients = []
    if not clients:
        return """
<div class="card">
  <h2>Устройства онлайн</h2>
  <p class="muted">Сейчас никто не слушает (или активность старше 5 мин).</p>
</div>
"""
    rows = []
    for c in clients:
        ip = html.escape(str(c.get("ip") or "?"))
        svc = html.escape(str(c.get("service") or "?"))
        title = html.escape(str(c.get("title") or "?"))
        ts = c.get("ts") or 0
        ago = int(time.time() - ts)
        if ago < 60:
            ago_s = f"{ago} сек. назад"
        else:
            ago_s = f"{ago // 60} мин. назад"
        rows.append(
            f"<tr><td><code>{ip}</code></td>"
            f"<td>{svc}</td>"
            f"<td>{title}</td>"
            f"<td class='muted'>{ago_s}</td></tr>"
        )
    table = (
        "<table><tr><th>IP (LAN)</th><th>Площадка</th><th>Сейчас играет</th><th></th></tr>"
        + "".join(rows) + "</table>"
    )
    return f"""
<div class="card">
  <h2>Устройства онлайн</h2>
  <p class="muted">Клиенты, которые недавно запрашивали поток (до 5 мин.).</p>
  {table}
</div>
"""


def _acc_names(svc: str) -> str:
    """« · аккаунты: Alice, Bob» для статуса сервиса (если аккаунтов больше одного)."""
    pool = _STATE.get(svc)
    try:
        if pool is not None and getattr(pool, "count", 0) > 1:
            return " · аккаунты: <strong>" + html.escape(", ".join(pool.names())) + "</strong>"
    except Exception:
        pass
    return ""


def _status_body() -> str:
    cfg = _cfg()
    server = cfg.get("server") or {}
    radio = cfg.get("radio") or {}
    deezer = cfg.get("deezer") or {}
    web = cfg.get("web") or {}
    announce = _STATE.get("announce_ip") or server.get("announce_ip") or "?"
    dlna_port = server.get("port") or 8200
    web_port = web.get("port") or 8088
    radio_lib = _STATE.get("radio")
    deezer_cli = _STATE.get("deezer")
    dm_cli = _STATE.get("drivemusic")
    ya_cli = _STATE.get("yandex")
    zv_cli = _STATE.get("zvuk")
    pdj_cli = _STATE.get("promodj")
    zy_cli = _STATE.get("zaycev")
    tree = _STATE.get("tree")
    n_pl = len(radio_lib.list_playlists()) if radio_lib else 0
    n_zy_saved = len(tree.zy_saved_searches()) if (tree and hasattr(tree, "zy_saved_searches")) else 0
    n_saved = len(tree.saved_searches()) if (tree and hasattr(tree, "saved_searches")) else 0
    n_dm_saved = len(tree.dm_saved_searches()) if (tree and hasattr(tree, "dm_saved_searches")) else 0
    n_ya_saved = len(tree.ya_saved_searches()) if (tree and hasattr(tree, "ya_saved_searches")) else 0
    n_zv_saved = len(tree.zv_saved_searches()) if (tree and hasattr(tree, "zv_saved_searches")) else 0

    ki_cli = _STATE.get("kion")
    n_kion_saved = len(tree.kion_saved_searches()) if (tree and hasattr(tree, "kion_saved_searches")) else 0
    kion_cfg = cfg.get("kion") or {}
    kion_badge = (
        '<span class="badge ok">вход OK</span>'
        if (ki_cli and ki_cli.enabled and getattr(ki_cli, "_account_ok", None))
        else (
            '<span class="badge ok">токен задан</span>'
            if (kion_cfg.get("token") or "").strip()
            else '<span class="badge off">нет токена</span>'
        )
    )
    n_ak_saved = len(tree.ak_saved_books()) if (tree and hasattr(tree, "ak_saved_books")) else 0
    n_ak_fav = len(tree.ak_favorites()) if (tree and hasattr(tree, "ak_favorites")) else 0
    dz_on = bool(deezer_cli and deezer_cli.enabled and getattr(deezer_cli, "_api_token", None))
    dz_badge = '<span class="badge ok">вход OK</span>' if dz_on else (
        '<span class="badge ok">ARL задан</span>' if (deezer.get("arl") or "").strip()
        else '<span class="badge off">нет ARL</span>'
    )
    dm_badge = (
        '<span class="badge ok">вкл</span>'
        if (dm_cli and dm_cli.enabled)
        else '<span class="badge off">выкл</span>'
    )
    ak_cli = _STATE.get("akniga")
    ak_badge = (
        '<span class="badge ok">вкл</span>'
        if (ak_cli and ak_cli.enabled and getattr(ak_cli, "ffmpeg_ok", False))
        else (
            '<span class="badge off">нет ffmpeg</span>'
            if (ak_cli and ak_cli.enabled)
            else '<span class="badge off">выкл</span>'
        )
    )
    ya_cfg = cfg.get("yandex") or {}
    ya_badge = (
        '<span class="badge ok">вход OK</span>'
        if (ya_cli and ya_cli.enabled and getattr(ya_cli, "_account_ok", None))
        else (
            '<span class="badge ok">токен задан</span>'
            if (ya_cfg.get("token") or "").strip()
            else '<span class="badge off">нет токена</span>'
        )
    )
    zv_cfg = cfg.get("zvuk") or {}
    zv_badge = (
        '<span class="badge ok">вход OK</span>'
        if (zv_cli and zv_cli.enabled and getattr(zv_cli, "_account_ok", None))
        else (
            '<span class="badge ok">токен задан</span>'
            if (zv_cfg.get("token") or "").strip()
            else '<span class="badge off">нет токена</span>'
        )
    )
    pdj_badge = (
        '<span class="badge ok">вкл</span>'
        if (pdj_cli and pdj_cli.enabled)
        else '<span class="badge off">выкл</span>'
    )
    zy_badge = (
        '<span class="badge ok">вкл</span>'
        if (zy_cli and zy_cli.enabled)
        else '<span class="badge off">выкл</span>'
    )
    ssdp = _STATE.get("ssdp")
    ssdp_line = ""
    if ssdp is not None:
        if getattr(ssdp, "ok", False):
            ssdp_line = '<p>SSDP: <span class="badge ok">OK</span></p>'
        else:
            err = html.escape(str(getattr(ssdp, "last_error", "") or "не OK"))
            ssdp_line = f'<p>SSDP: <span class="badge off">проблема</span> <span class="muted">{err}</span></p>'
    desc_url = f"http://{announce}:{dlna_port}/description.xml"
    return f"""
<div class="card">
  <h2>Сервер</h2>
  <p>Имя в DLNA: <strong>{html.escape(str(server.get("name") or "DLNA-emu"))}</strong></p>
  <p class="muted">DLNA: <code>http://{html.escape(str(announce))}:{dlna_port}</code>
     · веб: <code>http://{html.escape(str(announce))}:{web_port}</code></p>
  <p class="muted">Проверка: <a href="{html.escape(desc_url)}" style="color:#3d8bfd">{html.escape(desc_url)}</a></p>
  {ssdp_line}
  <p>Радио-плейлистов: <strong>{n_pl}</strong> · папка <code>{html.escape(str(radio.get("folder") or ""))}</code></p>
  <p>Deezer: {dz_badge}{_acc_names("deezer")} · сохранённых: <strong>{n_saved}</strong></p>
  <p>DriveMusic: {dm_badge} · сохранённых: <strong>{n_dm_saved}</strong></p>
  <p>akniga.org: {ak_badge} · мои книги: <strong>{n_ak_saved}</strong> · избранное: <strong>{n_ak_fav}</strong> / 500</p>
  <p>Яндекс.Музыка: {ya_badge}{_acc_names("yandex")} · сохранённых: <strong>{n_ya_saved}</strong></p>
  <p>КИОН: {kion_badge}{_acc_names("kion")} · сохранённых: <strong>{n_kion_saved}</strong> · <a href="/search?site=kion" style="color:#3d8bfd">поиск</a></p>
  <p>Звук: {zv_badge}{_acc_names("zvuk")} · сохранённых: <strong>{n_zv_saved}</strong> · <a href="/search?site=zvuk" style="color:#3d8bfd">поиск</a></p>
  <p>PromoDJ: {pdj_badge} · <a href="/search?site=promodj" style="color:#3d8bfd">поиск</a></p>
  <p>Zaycev.net: {zy_badge} · сохранённых: <strong>{n_zy_saved}</strong> · <a href="/search?site=zaycev" style="color:#3d8bfd">поиск</a></p>
  <div class="row">
    <a class="btn" href="/search">🔍 Поиск</a>
    <a class="btn ghost" href="/logs">⌨ Консоль</a>
    <a class="btn ghost" href="/logs/download">⬇ Скачать лог</a>
  </div>
</div>
{_devices_card()}
<div class="card">
  <h2>На ресивере</h2>
  <p class="muted">Network / Server / DLNA → <strong>{html.escape(str(server.get("name") or "DLNA-emu"))}</strong></p>
  <form method="POST" action="/ssdp/announce" style="margin:10px 0">
    <button type="submit">Объявить сервер в сети (SSDP)</button>
  </form>
</div>
<div class="card">
  <h2>Служба</h2>
  <p class="muted">Полный перезапуск эмулятора (то же, что stop + start в SSH). Связь с панелью пропадёт на несколько секунд.</p>
  <form method="POST" action="/restart" onsubmit="return confirm('Перезапустить службу DLNA-emu?')" style="margin:10px 0">
    <button type="submit" class="ghost">🔄 Перезапустить службу</button>
  </form>
</div>
"""


def _tokens_widget(field: str, svc: str, tokens, label: str, hint: str = "") -> str:
    """Список токенов аккаунтов: строка на аккаунт, «＋» добавляет, «−» убирает.

    Итог собирается скриптом в скрытое поле `field` через запятую (как в config.yaml).
    Рядом с токеном — имя аккаунта, которое сервис вернул по этому токену.
    """
    from .accounts import split_tokens
    toks = split_tokens(tokens)
    pool = _STATE.get(svc)
    names: list = []
    try:
        if pool is not None and hasattr(pool, "names") and pool.tokens == toks:
            names = pool.names()
    except Exception:
        names = []
    rows = []
    for i, t in enumerate(toks or [""]):
        nm = names[i] if i < len(names) else ""
        badge = f'<span class="tkname">{html.escape(nm)}</span>' if nm else ""
        try:
            if pool is not None and pool.tokens == toks and getattr(pool.client(i), "_account_ok", None) is False:
                badge = '<span class="tkname" style="color:#e5534b">⚠ токен не принят</span>'
        except Exception:
            pass
        rows.append(
            '<div class="tkrow">'
            f'<input class="tk" value="{html.escape(t)}" placeholder="токен аккаунта" '
            'spellcheck="false" autocomplete="off">'
            f'{badge}<button type="button" class="ghost tkdel" onclick="tkDel(this)" '
            'title="Убрать аккаунт">−</button></div>'
        )
    hint_html = f'<p class="muted" style="margin-top:6px;font-size:12px">{hint}</p>' if hint else ""
    return (
        f'<div class="tkbox">'
        f'<label>{label}</label>'
        f'<div class="tkrows">{"".join(rows)}</div>'
        f'<input type="hidden" name="{field}" value="{html.escape(",".join(toks))}">'
        '<button type="button" class="ghost" onclick="tkAdd(this)">＋ Добавить аккаунт</button>'
        + hint_html + "</div>"
    )


_TOKENS_JS = """
<style>
.tkrow{display:flex;gap:8px;align-items:center;margin:4px 0}
.tkrow input.tk{flex:1;font-family:monospace;font-size:12px}
.tkname{color:#7fd18b;font-size:12px;white-space:nowrap}
.tkdel{padding:4px 12px}
</style>
<script>
function tkSync(box){
  var v=[].map.call(box.querySelectorAll('input.tk'),function(i){return i.value.trim();}).filter(Boolean);
  box.querySelector('input[type=hidden]').value=v.join(',');
}
function tkAdd(btn){
  var box=btn.closest('.tkbox'),row=document.createElement('div');
  row.className='tkrow';
  row.innerHTML='<input class="tk" placeholder="токен аккаунта" spellcheck="false" autocomplete="off">'+
    '<button type="button" class="ghost tkdel" onclick="tkDel(this)" title="Убрать аккаунт">\u2212</button>';
  box.querySelector('.tkrows').appendChild(row);
  row.querySelector('input').focus();
}
function tkDel(b){
  var box=b.closest('.tkbox'),rows=box.querySelectorAll('.tkrow');
  if(rows.length>1){b.parentNode.remove();}else{box.querySelector('input.tk').value='';}
  tkSync(box);
}
document.addEventListener('input',function(e){
  if(e.target.classList&&e.target.classList.contains('tk'))tkSync(e.target.closest('.tkbox'));
});
</script>
"""


def _settings_body(flash: str = "") -> str:
    cfg = _cfg()
    server = cfg.get("server") or {}
    radio = cfg.get("radio") or {}
    deezer = cfg.get("deezer") or {}
    dm = cfg.get("drivemusic") or {}
    ak = cfg.get("akniga") or {}
    ya = cfg.get("yandex") or {}
    zv = cfg.get("zvuk") or {}
    kion = cfg.get("kion") or {}
    pdj = cfg.get("promodj") or {}
    zy = cfg.get("zaycev") or {}
    web = cfg.get("web") or {}
    files = cfg.get("files") or {}
    covers = cfg.get("covers") or {}
    trace = cfg.get("trace") or {}

    roots = files.get("roots") or []
    if isinstance(roots, str):
        roots = [roots]
    roots_text = "\n".join(str(r) for r in roots if r)
    pl_folders = files.get("playlists_folder") or []
    if isinstance(pl_folders, str):
        pl_folders = [pl_folders]
    pl_folders_text = "\n".join(str(r) for r in pl_folders if r)

    try:
        import yaml as _yaml
        _ns_items = cfg.get("network_storages") or []
        ns_text = _yaml.safe_dump(_ns_items, allow_unicode=True, default_flow_style=False,
                                  sort_keys=False).rstrip() if _ns_items else ""
    except Exception:
        ns_text = ""
    _ns_mgr = getattr(_STATE.get("tree"), "netstorage", None)
    ns_status = ""
    if _ns_mgr is not None:
        for _st in _ns_mgr.all():
            ns_status += f"<div>✓ <b>{html.escape(_st.name)}</b> — {html.escape(_st.describe())}</div>"
        for _er in _ns_mgr.errors:
            ns_status += f"<div style=\"color:#e5534b\">✗ {html.escape(_er)}</div>"

    from .accounts import join_tokens
    arl = join_tokens(deezer.get("arl"))
    arl_show = arl if len(arl) < 8 else (arl[:6] + "…" + arl[-4:])
    ya_token = join_tokens(ya.get("token"))
    ya_show = ya_token if len(ya_token) < 10 else (ya_token[:6] + "…" + ya_token[-4:])
    zv_token = join_tokens(zv.get("token"))
    zv_show = zv_token if len(zv_token) < 10 else (zv_token[:6] + "…" + zv_token[-4:])

    log_level = str(cfg.get("log_level") or "info").lower()

    def _b(v, default=False):
        if isinstance(v, str):
            return v.lower() in ("1", "true", "yes", "on")
        if v is None:
            return default
        return bool(v)

    def _sel(name, value, options):
        opts = []
        for val, label in options:
            sel = " selected" if str(value) == str(val) else ""
            opts.append(
                f'<option value="{html.escape(str(val))}"{sel}>'
                f'{html.escape(label)}</option>'
            )
        return f'<select name="{name}">' + "".join(opts) + "</select>"

    def _chk(name, label, checked, hint: str = ""):
        h = (
            f'<span class="muted" style="font-size:12px;margin-left:6px">{hint}</span>'
            if hint else ""
        )
        return (
            f'<label style="display:flex;align-items:center;gap:8px;'
            f'color:var(--text);font-size:14px;margin:8px 0">'
            f'<input type="checkbox" name="{name}" value="1"'
            f'{" checked" if checked else ""} style="width:auto">'
            f"{html.escape(label)}{h}</label>"
        )

    _tree = _STATE.get("tree")

    return f"""
<form method="POST" action="/settings">

<div class="card">
  <h2>Сервер</h2>
  <label>Имя DLNA-сервера</label>
  <input name="name" value="{html.escape(str(server.get("name") or "DLNA-emu"))}">
  <label>Адрес прослушивания</label>
  <input name="host" value="{html.escape(str(server.get("host") or "0.0.0.0"))}"
         placeholder="0.0.0.0 — все интерфейсы">
  <label>Порт DLNA (для ресивера)</label>
  <input name="dlna_port" type="number" min="1" max="65535"
         value="{html.escape(str(server.get("port") or 8200))}">
  <label>IP для анонса (LAN IP роутера, пусто — автоопределение)</label>
  <input name="announce_ip" value="{html.escape(str(server.get("announce_ip") or ""))}"
         placeholder="192.168.1.1">
</div>

<div class="card">
  <h2>Веб-панель</h2>
  {_chk("web_enabled", "Включить веб-панель",
        _b(web.get("enabled"), True),
        "после выключения страница станет недоступна — вернуть только правкой config.yaml")}
  <label>Порт веб-панели</label>
  <input name="web_port" type="number" min="1" max="65535"
         value="{html.escape(str(web.get("port") or 8088))}">
</div>

<div class="card">
  <h2>Логи и трассировка</h2>
  <label>Уровень логирования</label>
  {_sel("log_level", log_level, [
      ("debug",   "DEBUG — всё, включая тела SOAP/DIDL"),
      ("info",    "INFO — обычный (рекомендуется)"),
      ("warning", "WARNING — только проблемы"),
      ("error",   "ERROR — только ошибки"),
  ])}
  {_chk("trace_enabled", "Логировать DLNA-трафик от ресиверов",
        _b(trace.get("enabled"), True),
        "логгер dlna_emu.trace: Browse / Search / Range / UA")}
  {_chk("trace_covers", "Показывать /cover/* на уровне INFO",
        _b(trace.get("covers"), False),
        "по умолчанию они идут на DEBUG, чтобы не забивать буфер")}
</div>

<div class="card">
  <h2>Deezer</h2>
  {_tokens_widget("arl", "deezer", arl, "ARL (аккаунты Deezer)",
      "Cookie <code>arl</code> с deezer.com. Несколько аккаунтов — «＋»; в config.yaml они пишутся через запятую. "
      "Имя аккаунта определяется по ARL.")}
  <label>Качество</label>
  {_sel("deezer_quality", deezer.get("quality") or "mp3_128", [
      ("mp3_128", "MP3 128 kbps"),
      ("mp3_320", "MP3 320 kbps"),
      ("flac",    "FLAC (нужен Premium)"),
  ])}
  <label>Кэш метаданных, сек</label>
  <input name="deezer_cache_sec" type="number" min="30" max="86400"
         value="{html.escape(str(deezer.get("cache_sec") or 300))}">
</div>

<div class="card">
  <h2>DriveMusic</h2>
  {_chk("dm_enabled", "Включить DriveMusic",
        _b(dm.get("enabled"), True))}
  <label>Кэш, сек</label>
  <input name="dm_cache_sec" type="number" min="30" max="86400"
         value="{html.escape(str(dm.get("cache_sec") or 300))}">
  <label>Базовый URL (зеркало; пусто = авто-выбор)</label>
  <input name="dm_base_url"
         value="{html.escape(str(dm.get("base_url") or "https://ru.drivemusic.me"))}">
</div>

<div class="card">
  <h2>akniga.org (аудиокниги)</h2>
  {_chk("ak_enabled", "Включить akniga.org",
        _b(ak.get("enabled"), True))}
  <label>Путь к ffmpeg (пусто — искать в $PATH)</label>
  <input name="ak_ffmpeg" value="{html.escape(str(ak.get("ffmpeg") or ""))}"
         placeholder="/opt/bin/ffmpeg">
  <label>Битрейт глав, kbps</label>
  <input name="ak_chapter_kbps" type="number" min="32" max="320"
         value="{html.escape(str(ak.get("chapter_kbps") or 128))}">
  <label>Книг на «Страницу» раздела</label>
  <input name="ak_books_per_chunk" type="number" min="10" max="5000"
         value="{html.escape(str(ak.get("section_books_per_chunk") or 500))}">
  <label>Кэш, сек</label>
  <input name="ak_cache_sec" type="number" min="30" max="86400"
         value="{html.escape(str(ak.get("cache_sec") or 600))}">
</div>

<div class="card">
  <h2>Яндекс.Музыка</h2>
  {_tokens_widget("yandex_token", "yandex", ya_token, "OAuth-токены (аккаунты Яндекса)")}
  <p class="muted" style="margin-top:4px;font-size:12px">
    Получить:
    <a href="https://oauth.yandex.ru/authorize?response_type=token&amp;client_id=23cabbbdc6cd418abb4b39c32c41195d"
       target="_blank" style="color:#3d8bfd">oauth.yandex.ru</a>
    → скопировать access_token из адресной строки после входа.
  </p>
  <label>Качество</label>
  {_sel("ya_quality", ya.get("quality") or "mp3_320", [
      ("mp3_128", "MP3 128 kbps"),
      ("mp3_320", "MP3 320 kbps"),
      ("flac",    "FLAC / Lossless (нужен Plus)"),
  ])}
  <label>Режим стрима</label>
  {_sel("ya_stream_mode", ya.get("stream_mode") or "auto", [
      ("auto",     "auto — буфер с таймаутом, потом chunked"),
      ("buffered", "buffered — всегда полный буфер (для Yamaha и т.п.)"),
      ("chunked",  "chunked — без буфера, сразу поток"),
  ])}
  <label>Таймаут буфера, сек</label>
  <input name="ya_buffer_timeout" type="number" min="1" max="60" step="0.5"
         value="{html.escape(str(ya.get("buffer_timeout") or 6.0))}">
  <label>Проверка FLAC при открытии списка, сек</label>
  <input name="ya_format_probe_timeout" type="number" min="0" max="30" step="0.5"
         value="{html.escape(str(4 if server.get("format_probe_timeout") in (None, "") else server.get("format_probe_timeout")))}">
  <p class="muted" style="margin-top:4px;font-size:12px">
    Сколько ждать проверку: есть ли у треков реальный FLAC (0 — не проверять, список открывается мгновенно). Где нет — ресиверу заявляется MP3
    (иначе Yamaha и подобные пропускают такие треки). Не успели — доделается в фоне.
  </p>
  <label>Помнить результат проверки FLAC, сек</label>
  <input name="ya_format_cache_sec" type="number" min="60" max="86400"
         value="{html.escape(str(server.get("format_cache_sec") or 1800))}">
  <label>Кэш, сек</label>
  <input name="ya_cache_sec" type="number" min="30" max="86400"
         value="{html.escape(str(ya.get("cache_sec") or 300))}">
</div>

<div class="card">
  <h2>Звук (zvuk.com)</h2>
  {_tokens_widget("zvuk_token", "zvuk", zv_token, "X-Auth-Token (аккаунты Звука)")}
  {_chk("zv_keepalive", "Поддерживать сессию",
        _b(zv.get("keepalive"), False),
        "раз в 40–80 мин (в случайное время) запрашивать профиль, чтобы токен не протухал от простоя")}
  <p class="muted" style="margin-top:4px;font-size:12px">
    Войти на <a href="https://zvuk.com" target="_blank" style="color:#3d8bfd">zvuk.com</a> →
    открыть <a href="https://zvuk.com/api/tiny/profile" target="_blank" style="color:#3d8bfd">/api/tiny/profile</a> →
    скопировать поле <code>token</code>. Нужна подписка для high/flac.
  </p>
  <label>Качество</label>
  {_sel("zv_quality", zv.get("quality") or "high", [
      ("mid",  "MP3 128 kbps (mid)"),
      ("high", "MP3 320 kbps (high)"),
      ("flac", "FLAC / Lossless"),
  ])}
  <label>Режим стрима</label>
  {_sel("zv_stream_mode", zv.get("stream_mode") or "auto", [
      ("auto",     "auto — буфер с таймаутом, потом chunked"),
      ("buffered", "buffered — всегда полный буфер"),
      ("chunked",  "chunked — без буфера"),
  ])}
  <label>Таймаут буфера, сек</label>
  <input name="zv_buffer_timeout" type="number" min="1" max="60" step="0.5"
         value="{html.escape(str(zv.get("buffer_timeout") or 6.0))}">
  <label>Кэш, сек</label>
  <input name="zv_cache_sec" type="number" min="30" max="86400"
         value="{html.escape(str(zv.get("cache_sec") or 300))}">
</div>

<div class="card">
  
  <h2>КИОН Музыка (music.mts.ru)</h2>
  {_tokens_widget("kion_token", "kion", kion.get("token") or "", "OAuth-токены (аккаунты КИОН)")}
  <p class="muted" style="margin-top:4px">
    Войти на <a href="https://music.mts.ru" target="_blank" style="color:#3d8bfd">music.mts.ru</a> /
    <a href="https://music.kion.ru" target="_blank" style="color:#3d8bfd">music.kion.ru</a> →
    DevTools → Network / Local Storage, либо через документацию
    <a href="https://trudenboy.github.io/ma-provider-kion-music/" target="_blank" style="color:#3d8bfd">ma-provider-kion-music</a>.
    Нужна подписка для FLAC. API: music.mts.ru/ya_api · Local Storage → ya_token
  </p>
  <label>Качество</label>
  {_sel("kion_quality", kion.get("quality") or "flac", [
      ("flac", "flac (lossless)"),
      ("mp3_320", "mp3_320"),
  ])}
</div>

<div class="card">
  <h2>PromoDJ</h2>
  {_chk("pd_enabled", "Включить PromoDJ",
        _b(pdj.get("enabled"), True))}
  <label>Кэш, сек</label>
  <input name="pd_cache_sec" type="number" min="30" max="86400"
         value="{html.escape(str(pdj.get("cache_sec") or 300))}">
  <label>Треков на «Страницу» раздела</label>
  <input name="pd_tracks_per_chunk" type="number" min="50" max="1000"
         value="{html.escape(str(pdj.get("tracks_per_chunk") or 500))}">
  <label>Режим стрима</label>
  {_sel("pd_stream_mode", pdj.get("stream_mode") or "auto", [
      ("auto", "auto — буфер с таймаутом, иначе chunked"),
      ("buffered", "buffered — всегда буфер (если размер ≤ лимита)"),
      ("chunked", "chunked — без буфера, без перемотки"),
  ])}
  <label>Таймаут буфера, сек</label>
  <input name="pd_buffer_timeout" type="number" min="1" max="60" step="0.5"
         value="{html.escape(str(pdj.get("buffer_timeout") or 6.0))}">
  <label>Лимит буфера, МБ</label>
  <input name="pd_buffer_limit_mb" type="number" min="4" max="256" step="1"
         value="{html.escape(str(pdj.get("buffer_limit_mb") or 64))}">
  <p class="muted" style="margin-top:4px;font-size:12px">
    Миксы длиннее лимита идут chunked (без перемотки). Таймаут не даёт ресиверу «зависнуть» на старте.
  </p>
</div>

<div class="card">
  <h2>Zaycev.net</h2>
  {_chk("zy_enabled", "Включить Zaycev.net",
        _b(zy.get("enabled"), True))}
  <label>Кэш, сек</label>
  <input name="zy_cache_sec" type="number" min="30" max="86400"
         value="{html.escape(str(zy.get("cache_sec") or 300))}">
  <label>Элементов на «Страницу» (треков / подборок)</label>
  <input name="zy_tracks_per_chunk" type="number" min="20" max="1000"
         value="{html.escape(str(zy.get("tracks_per_chunk") or 100))}">
  <label>Режим стрима</label>
  {_sel("zy_stream_mode", zy.get("stream_mode") or "auto", [
      ("auto", "auto — буфер с таймаутом, иначе chunked"),
      ("buffered", "buffered — всегда буфер (если размер ≤ лимита)"),
      ("chunked", "chunked — без буфера, без перемотки"),
  ])}
  <label>Таймаут буфера, сек</label>
  <input name="zy_buffer_timeout" type="number" min="1" max="60" step="0.5"
         value="{html.escape(str(zy.get("buffer_timeout") or 6.0))}">
  <label>Лимит буфера, МБ</label>
  <input name="zy_buffer_limit_mb" type="number" min="4" max="256" step="1"
         value="{html.escape(str(zy.get("buffer_limit_mb") or 64))}">
  <p class="muted" style="margin-top:4px;font-size:12px">
    Без авторизации. Каталоги (жанры, подборки, топ, новинки) подгружаются с сайта по мере
    открытия папок. Исполнители ищутся здесь: вкладка «Поиск» → Zaycev.net → «+ В DLNA».
  </p>
</div>

<div class="card">
  <h2>USB / файлы</h2>
  <label>Корни (по одному пути на строку)</label>
  <textarea name="files_roots" rows="3"
            placeholder="/tmp/mnt&#10;/tmp/mnt/6EE7-47CC">{html.escape(roots_text)}</textarea>
  <p class="muted" style="margin-top:4px;font-size:12px">
    Обычно достаточно <code>/tmp/mnt</code>. Подпапки подхватываются автоматически.
  </p>
  <label>playlists_folder — папка с плейлистами .m3u / .m3u8 (строка или список, по одному пути на строку)</label>
  <textarea name="files_playlists_folder" rows="2"
            placeholder="/tmp/mnt/6EE7-47CC/playlists">{html.escape(pl_folders_text)}</textarea>
  <p class="muted" style="margin-top:4px;font-size:12px">
    Пусто = искать .m3u по всем корням (roots). Если задана — плейлисты USB берутся только отсюда
    (быстрее на больших дисках). Пример: <code>/tmp/mnt/6EE7-47CC/playlists</code> или
    <code>D:/music/playlists</code> в Windows. Это музыка с USB (на ресивере «Плейлисты (USB)»), не радио.
    Применяется сразу, без перезапуска.
  </p>
</div>

<div class="card">
  <h2>Сетевые хранилища (WebDAV / FTP)</h2>
  <label>network_storages — список в формате YAML</label>
  <textarea name="netstorage_yaml" rows="14" spellcheck="false"
            style="font-family:monospace;font-size:12px"
            placeholder="- name: Мой NAS&#10;  type: webdav&#10;  url: https://nas.local:5006/music&#10;  user: me&#10;  password: secret&#10;&#10;- name: FTP&#10;  type: ftp или ftps&#10;  host: 192.168.1.50&#10;  port: 21&#10;  path: /music&#10;  user: me&#10;  password: secret">{html.escape(ns_text)}</textarea>
  <p class="muted" style="margin-top:4px;font-size:12px">
    На ресивере: «Сетевые хранилища» → имя. Поддерживаются <code>type: webdav | ftp | ftps</code>
    (<code>type: ftp</code> или <code>type: ftps</code> — явный TLS, AUTH TLS).
    WebDAV: порт указывается в <code>url</code> (по умолчанию 443 для https / 80 для http, можно сменить:
    <code>https://nas.local:5006/music</code>).
    FTP/FTPS: порт задаётся полем <code>port</code> вручную (по умолчанию <code>21</code>, можно любое, напр. <code>2121</code>) —
    эмулятор не дописывает порт в host сам.
    Остальное: <code>host / path</code>, <code>user</code>, <code>password</code>,
    <code>verify_ssl: false</code>, <code>encoding: cp1251</code>, <code>auth: digest</code> (WebDAV),
    <code>enabled: false</code>. Аудио идёт напрямую с хранилища. Применяется сразу, без перезапуска.
  </p>
  <div style="font-size:12px;margin-top:6px">{ns_status}</div>
</div>

<div class="card">
  <h2>Обложки</h2>
  {_chk("covers_radio", "Обложки для радиостанций",
        _b(covers.get("radio"), True),
        "логотип из m3u (tvg-logo) или значок с инициалами")}
  {_chk("covers_radio_nowplaying", "Радио: обложка играющего трека",
        _b(covers.get("radio_nowplaying"), True),
        "по ICY-названию ищется обложка (Deezer/iTunes); только для станции, которую слушают")}
  {_chk("covers_enabled", "Отдавать обложки в DLNA",
        _b(covers.get("enabled"), True))}
  {_chk("covers_dlna_profile", "DLNA-профиль для обложек",
        _b(covers.get("dlna_profile"), False),
        "добавляет dlna:profileID=\"JPEG_TN\" — нужно не всем ресиверам")}
  <label>Максимальный размер после сжатия, КБ</label>
  <input name="covers_max_kb" type="number" min="64" max="16384"
         value="{html.escape(str(covers.get("max_kb") or 2048))}">
  <label>Максимальная сторона, px (0 — без ресайза)</label>
  <input name="covers_max_px" type="number" min="0" max="4096"
         value="{html.escape(str(covers.get("max_px", 600) or 0))}">
  <label>Записей в кэше</label>
  <input name="covers_cache_items" type="number" min="4" max="512"
         value="{html.escape(str(covers.get("cache_items") or 48))}">
</div>

<div class="card">
  <div class="row" style="justify-content:flex-start;gap:10px">
    <button type="submit">💾 Сохранить</button>
    <button type="submit" name="restart" value="1" class="ghost">
      💾 Сохранить и 🔄 Перезапустить
    </button>
    <a class="btn ghost" href="/">Отмена</a>
  </div>
  <p class="muted" style="margin-top:12px">
    Порта, токены, качество, включение/выключение площадок и уровень логов
    применяются <strong>после перезапуска</strong>.
    ARL и остальные настройки применяются сразу.
  </p>
</div>

{_TOKENS_JS}
</form>
"""


def _radio_body(flash: str = "", view_pid: str = "") -> str:
    radio = _STATE.get("radio")
    if not radio:
        return '<div class="card"><p class="muted">Радио не инициализировано</p></div>'
    radio.maybe_reload()
    playlists = radio.list_playlists()
    folder = getattr(radio, "folder", None) or (_cfg().get("radio") or {}).get("folder") or ""

    # --- список плейлистов ---
    pl_rows = []
    for pl in playlists:
        pl_rows.append(
            f"<tr>"
            f"<td><a href='/radio?pl={html.escape(pl.id)}' style='color:var(--acc)'>"
            f"{html.escape(pl.title)}</a></td>"
            f"<td>{len(pl.stations)}</td>"
            f"<td><code>{html.escape(pl.path)}</code></td>"
            f"</tr>"
        )
    pl_table = (
        "<table><tr><th>Плейлист</th><th>Станций</th><th>Файл</th></tr>"
        + "".join(pl_rows) + "</table>"
        if pl_rows
        else '<p class="muted">Нет .m3u в папке</p>'
    )

    # --- опции select для добавления ---
    opts = []
    for pl in playlists:
        sel = " selected" if pl.id == view_pid else ""
        opts.append(f'<option value="{html.escape(pl.id)}"{sel}>{html.escape(pl.title)}</option>')
    select_html = (
        "<select name='pid' required>"
        + ("".join(opts) if opts else "<option value=''>— нет плейлистов —</option>")
        + "</select>"
    )

    # --- просмотр станций выбранного плейлиста ---
    stations_block = ""
    if view_pid:
        pl = radio.get_playlist(view_pid)
        if pl:
            st_rows = []
            n_st = len(pl.stations)
            for i, st in enumerate(pl.stations):
                up_btn = ""
                down_btn = ""
                if i > 0:
                    up_btn = f"""
    <form method="POST" action="/radio/move" style="display:inline">
      <input type="hidden" name="pid" value="{html.escape(pl.id)}">
      <input type="hidden" name="index" value="{i}">
      <input type="hidden" name="new_index" value="{i}">
      <button type="submit" class="small ghost" title="Выше">↑</button>
    </form>"""
                if i < n_st - 1:
                    down_btn = f"""
    <form method="POST" action="/radio/move" style="display:inline">
      <input type="hidden" name="pid" value="{html.escape(pl.id)}">
      <input type="hidden" name="index" value="{i}">
      <input type="hidden" name="new_index" value="{i + 2}">
      <button type="submit" class="small ghost" title="Ниже">↓</button>
    </form>"""
                st_rows.append(f"""
<tr data-pid="{html.escape(pl.id)}" data-index="{i}"
    data-name="{html.escape(st.name)}" data-url="{html.escape(st.url)}">
  <td style="width:2.5em;text-align:center;font-weight:700;color:var(--acc);font-size:15px">{i + 1}</td>
  <td class="st-name" title="Двойной клик — изменить название"
      style="cursor:pointer"><strong>{i + 1}. {html.escape(st.name)}</strong></td>
  <td class="st-url" title="Двойной клик — изменить ссылку"
      style="cursor:pointer"><code style="word-break:break-all;font-size:11px">{html.escape(st.url)}</code></td>
  <td style="white-space:nowrap">
    {up_btn}{down_btn}
    <form method="POST" action="/radio/move" style="display:inline-flex;align-items:center;gap:4px;margin-left:4px">
      <input type="hidden" name="pid" value="{html.escape(pl.id)}">
      <input type="hidden" name="index" value="{i}">
      <input type="number" name="new_index" value="{i + 1}" min="1" max="{n_st}"
             style="width:3.5em;padding:4px 6px;font-size:12px" title="Новый номер (1…{n_st})">
      <button type="submit" class="small ghost" title="Переместить на номер">OK</button>
    </form>
    <form method="POST" action="/radio/remove" style="display:inline;margin-left:6px">
      <input type="hidden" name="pid" value="{html.escape(pl.id)}">
      <input type="hidden" name="index" value="{i}">
      <button type="submit" class="small ghost">Удалить</button>
    </form>
  </td>
</tr>""")
            st_table = (
                "<table><tr><th>#</th><th>Станция</th><th>URL</th><th>Порядок</th></tr>"
                + "".join(st_rows) + "</table>"
                if st_rows
                else '<p class="muted">В плейлисте нет станций</p>'
            )
            stations_block = f"""
<div class="card">
  <h2>Станции: {html.escape(pl.title)}
    <span style="font-size:12px;background:#1a3d2e;color:#3dd68c;padding:2px 8px;border-radius:6px;margin-left:8px">v3-num</span>
  </h2>
  <p class="muted"><code>{html.escape(pl.path)}</code>
     · <a href="/radio" style="color:var(--acc)">← все плейлисты</a></p>
  <p class="muted" style="font-size:13px">
    В DLNA станции видны как <code>1. Название</code>, <code>2. Название</code>…
    Меняйте порядок кнопками ↑ ↓ или укажите номер и нажмите OK.
    <strong>Двойной клик</strong> по названию или URL — редактирование.
  </p>
  {st_table}
</div>
<script>
(function () {{
  function el(tag, attrs, text) {{
    var n = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (k) {{
      if (k === 'style') n.style.cssText = attrs[k];
      else n.setAttribute(k, attrs[k]);
    }});
    if (text != null) n.textContent = text;
    return n;
  }}
  function startEdit(cell, field) {{
    var tr = cell.closest('tr');
    if (!tr || cell.querySelector('form')) return;
    var pid = tr.getAttribute('data-pid') || '';
    var idx = tr.getAttribute('data-index') || '';
    var name = tr.getAttribute('data-name') || '';
    var url = tr.getAttribute('data-url') || '';
    var form = el('form', {{method: 'POST', action: '/radio/edit',
      style: 'display:flex;gap:6px;align-items:center;margin:0;width:100%'}});
    form.appendChild(el('input', {{type: 'hidden', name: 'pid', value: pid}}));
    form.appendChild(el('input', {{type: 'hidden', name: 'index', value: idx}}));
    form.appendChild(el('input', {{type: 'hidden',
      name: field === 'name' ? 'url' : 'name',
      value: field === 'name' ? url : name}}));
    var inp = el('input', {{
      name: field, value: field === 'name' ? name : url, required: 'required',
      style: 'flex:1;min-width:10em;font-size:12px;padding:4px 8px' +
        (field === 'url' ? ';font-family:monospace' : ';font-weight:600')
    }});
    form.appendChild(inp);
    form.appendChild(el('button', {{type: 'submit', class: 'small'}}, 'OK'));
    var cancel = el('button', {{type: 'button', class: 'small ghost'}}, '✕');
    cancel.addEventListener('click', function () {{ location.reload(); }});
    form.appendChild(cancel);
    cell.textContent = '';
    cell.appendChild(form);
    inp.focus();
    inp.select();
    inp.addEventListener('keydown', function (e) {{
      if (e.key === 'Escape') location.reload();
    }});
  }}
  document.querySelectorAll('td.st-name').forEach(function (td) {{
    td.addEventListener('dblclick', function () {{ startEdit(td, 'name'); }});
  }});
  document.querySelectorAll('td.st-url').forEach(function (td) {{
    td.addEventListener('dblclick', function () {{ startEdit(td, 'url'); }});
  }});
}})();
</script>
"""

    auto_mp3 = bool(getattr(radio, "auto_mp3_suffix", True))
    auto_chk = " checked" if auto_mp3 else ""
    auto_hint = (
        "При добавлении/редактировании к ссылке без аудио-расширения "
        "добавляется <code>?.mp3</code> "
        "(иначе ресивер может не выбрать декодер)."
        if auto_mp3 else
        "Суффикс <code>?.mp3</code> не добавляется — URL сохраняется как введён."
    )

    proxy_on = bool(getattr(radio, "proxy", True))
    proxy_chk = " checked" if proxy_on else ""
    proxy_hint = (
        "Поток станции открывает эмулятор и пересылает ресиверу: работают станции "
        "с https, редиректами и «ICY»-ответами, ресиверу не нужен прямой доступ к станции. "
        "HLS (.m3u8) отдаётся обычным редиректом."
        if proxy_on else
        "Выключено: ресивер получает редирект и сам подключается к станции."
    )
    pop = getattr(radio, "popular", None)
    pop_min = int(getattr(pop, "min_minutes", 15) or 15)
    pop_days = int(getattr(pop, "keep_days", 30) or 30)
    pop_n = pop.count() if pop is not None else 0
    pop_on = bool(getattr(radio, "popular_enabled", True)) and proxy_on
    pop_chk = " checked" if pop_on else ""
    pop_dis = "" if proxy_on else " disabled"
    pop_warn = "" if proxy_on else (
        '<p style="color:#f0a040;font-size:13px;margin:8px 0 0">⚠ Проксирование радио выключено — '
        'эмулятор не видит, сколько вы слушаете станцию, поэтому «Ваше популярное радио» отключено. '
        'Включите проксирование выше, после этого можно будет поставить галку.</p>'
    )
    return f"""
<div class="card">
  <h2>Совместимость с ресивером</h2>
  <form method="POST" action="/radio/auto_mp3">
    <input type="hidden" name="view_pid" value="{html.escape(view_pid or "")}">
    <label style="display:flex;align-items:flex-start;gap:10px;color:var(--text);font-size:14px;margin:0;cursor:pointer">
      <input type="checkbox" name="auto_mp3_suffix" value="1"{auto_chk}
             style="width:auto;margin-top:3px"
             onchange="this.form.submit()">
      <span>
        <strong>Автоматически добавлять <code>?.mp3</code></strong>
        <span class="muted" style="display:block;font-size:13px;margin-top:4px">{auto_hint}</span>
      </span>
    </label>
  </form>
</div>
<div class="card">
  <h2>Проксирование радио</h2>
  <form method="POST" action="/radio/proxy">
    <input type="hidden" name="view_pid" value="{html.escape(view_pid or "")}">
    <label style="display:flex;align-items:flex-start;gap:10px;color:var(--text);font-size:14px;margin:0;cursor:pointer">
      <input type="checkbox" name="radio_proxy" value="1"{proxy_chk}
             style="width:auto;margin-top:3px"
             onchange="this.form.submit()">
      <span>
        <strong>Отдавать радио через эмулятор (роутер)</strong>
        <span class="muted" style="display:block;font-size:13px;margin-top:4px">{proxy_hint}</span>
      </span>
    </label>
  </form>
</div>
<div class="card">
  <h2>Ваше популярное радио</h2>
  <p class="muted" style="font-size:13px;margin:0 0 8px">
    В разделе Radio есть папка «⭐ Ваше популярное радио» — станции, которые вы слушаете чаще всего
    (сверху самые частые за период). Время считается, пока устройство держит поток станции.
  </p>
  <form method="POST" action="/radio/popular_enable">
    <input type="hidden" name="view_pid" value="{html.escape(view_pid or "")}">
    <label style="display:flex;align-items:flex-start;gap:10px;color:var(--text);font-size:14px;margin:0 0 10px;cursor:pointer">
      <input type="checkbox" name="popular_enabled" value="1"{pop_chk}{pop_dis}
             style="width:auto;margin-top:3px"
             onchange="this.form.submit()">
      <span>
        <strong>Включить «Ваше популярное радио»</strong>
        <span class="muted" style="display:block;font-size:13px;margin-top:4px">
          Работает только при включённом проксировании радио.
        </span>
      </span>
    </label>
  </form>
  {pop_warn}
  <form method="POST" action="/radio/popular">
    <input type="hidden" name="view_pid" value="{html.escape(view_pid or "")}">
    <label>Добавлять станцию, если слушаете подряд дольше (минут)</label>
    <input name="popular_min_minutes" type="number" min="1" max="1440" value="{pop_min}">
    <label>Убирать станцию, если её не включали дольше (дней)</label>
    <input name="popular_keep_days" type="number" min="1" max="365" value="{pop_days}">
    <p class="muted" style="font-size:12px;margin-top:6px">
      Частота считается за те же дни. Сейчас в списке станций: <b>{pop_n}</b>.
    </p>
    <div class="row" style="margin-top:10px"><button type="submit">💾 Сохранить</button></div>
  </form>
</div>
<div class="card">
  <h2>Добавить радиостанцию</h2>
  <p class="muted">Выберите M3U-файл, укажите название и ссылку.
     {"Если в пути нет аудио-расширения, в конец добавляется <code>?.mp3</code> (например <code>…/ags96</code> → <code>…/ags96?.mp3</code>)." if auto_mp3 else "Ссылка сохраняется без изменений (авто-<code>?.mp3</code> выключен)."}</p>
  <form method="POST" action="/radio/add">
    <label>Плейлист (файл)</label>
    {select_html}
    <label>Название станции</label>
    <input name="name" placeholder="Например: Relax FM" required>
    <label>Ссылка на поток</label>
    <input name="url" placeholder="https://example.com/stream" required>
    <div class="row">
      <button type="submit">Добавить</button>
    </div>
  </form>
</div>
<div class="card">
  <h2>Создать новый плейлист</h2>
  <form method="POST" action="/radio/create">
    <label>Название</label>
    <input name="title" placeholder="Например: Jazz" required>
    <div class="row">
      <button type="submit" class="ghost">Создать .m3u</button>
    </div>
  </form>
</div>
{stations_block}
<div class="card">
  <h2>Радио M3U (файлы)</h2>
  <p class="muted">Папка: <code>{html.escape(str(folder))}</code>
     · нажмите на название, чтобы открыть станции</p>
  <form method="POST" action="/radio/reload" style="margin-bottom:12px">
    <button type="submit" class="ghost">Перечитать папку</button>
  </form>
  {pl_table}
</div>
"""


def _dmsearch_body(flash: str = "", q: str = "") -> str:
    dm = _STATE.get("drivemusic")
    tree = _STATE.get("tree")
    if not dm or not getattr(dm, "enabled", False):
        return '<div class="card"><p class="muted">DriveMusic выключен</p></div>'

    results_html = ""
    if q.strip():
        artists = []
        try:
            artists = dm.search_artists(q, limit=20)
        except Exception as e:
            log.warning("webui dm search_artists: %s", e)
        blocks = []
        for a in artists:
            sub = a.get("subtitle") or "исполнитель"
            slug = a.get("id") or ""
            name = a.get("name") or slug
            blocks.append(f"""
<div class="search-card">
  <div>
    <div class="name">👤 {html.escape(name)}</div>
    <div class="desc">{html.escape(sub)} · <code>/artist/{html.escape(slug)}.html</code></div>
  </div>
  <div class="actions">
    <form method="POST" action="/dmsearch/save" style="display:inline">
      <input type="hidden" name="type" value="artist">
      <input type="hidden" name="artist_id" value="{html.escape(slug)}">
      <input type="hidden" name="title" value="{html.escape(name)}">
      <input type="hidden" name="query" value="{html.escape(q)}">
      <button type="submit" class="small">+ В DLNA</button>
    </form>
  </div>
</div>""")
        if not blocks:
            blocks.append('<p class="muted">Артисты не найдены. Попробуйте другое написание (например the-beatles).</p>')
        results_html = (
            '<div style="margin-top:16px">'
            f'<p class="muted">Найдено артистов: {len(artists)}</p>'
            + "".join(blocks)
            + "</div>"
        )

    saved = tree.dm_saved_searches() if (tree and hasattr(tree, "dm_saved_searches")) else []
    saved_rows = []
    for s in saved:
        saved_rows.append(f"""
<div class="search-card">
  <div>
    <div class="name">👤 {html.escape(s['title'])}</div>
    <div class="desc">Артист{(' · запрос: ' + html.escape(s['query'])) if s.get('query') else ''}</div>
  </div>
  <div class="actions">
    <form method="POST" action="/dmsearch/remove" style="display:inline">
      <input type="hidden" name="sid" value="{html.escape(s['id'])}">
      <button type="submit" class="small ghost">Удалить</button>
    </form>
  </div>
</div>""")
    if not saved_rows:
        saved_block = '<p class="muted">Пока пусто — найдите артиста и нажмите «+ В DLNA»</p>'
    else:
        saved_block = "".join(saved_rows) + """
<form method="POST" action="/dmsearch/clear" style="margin-top:8px">
  <button type="submit" class="ghost small">Очистить все</button>
</form>"""

    flash_html = f'<div class="flash">{html.escape(flash)}</div>' if flash else ""

    return f"""
{flash_html}
<div class="card">
  <h2>Поиск DriveMusic</h2>
  <p class="muted">Ищет артистов (карточки) · без авторизации · ru.drivemusic.me</p>
  <form method="GET" action="/dmsearch" class="row" style="gap:8px;flex-wrap:wrap">
    <input type="text" name="q" value="{html.escape(q)}" placeholder="имя исполнителя, напр. The Beatles"
           style="flex:1;min-width:200px;padding:8px 12px;border-radius:8px;border:1px solid #333;background:#1a1a1a;color:#eee">
    <button type="submit">Искать</button>
  </form>
  {results_html}
</div>
<div class="card">
  <h2>В DLNA · Мои поиски</h2>
  <p class="muted">Появятся в ресивере: DriveMusic → 🔍 Мои поиски</p>
  {saved_block}
</div>
"""


def _search_body(flash: str = "", q: str = "", site: str = "deezer", page: int = 1) -> str:
    deezer = _STATE.get("deezer")
    dm = _STATE.get("drivemusic")
    ya = _STATE.get("yandex")
    zv = _STATE.get("zvuk")
    ki = _STATE.get("kion")
    ak = _STATE.get("akniga")
    pdj = _STATE.get("promodj")
    zy = _STATE.get("zaycev")
    tree = _STATE.get("tree")
    site = (site or "deezer").lower()
    if site not in ("deezer", "drivemusic", "yandex", "zvuk", "kion", "akniga", "promodj", "zaycev"):
        site = "deezer"
    try:
        page = max(1, int(page or 1))
    except (TypeError, ValueError):
        page = 1

    # availability
    dz_ok = bool(deezer and deezer.enabled)
    dm_ok = bool(dm and dm.enabled)
    ya_ok = bool(ya and ya.enabled)
    zv_ok = bool(zv and zv.enabled)
    kion_ok = bool(ki and (getattr(ki, 'enabled', False) or (getattr(ki, 'token', '') or '').strip()))
    ak_ok = bool(ak and ak.enabled)
    pdj_ok = bool(pdj and pdj.enabled)
    zy_ok = bool(zy and zy.enabled)

    def site_opt(val: str, label: str, ok: bool) -> str:
        sel = " selected" if site == val else ""
        dis = " disabled" if not ok else ""
        note = "" if ok else " (недоступен)"
        return f'<option value="{val}"{sel}{dis}>{label}{note}</option>'

    site_select = (
        '<label>Где искать</label>'
        '<select name="site" style="max-width:280px">'
        + site_opt("deezer", "Deezer", dz_ok)
        + site_opt("drivemusic", "DriveMusic", dm_ok)
        + site_opt("yandex", "Яндекс.Музыка", ya_ok)
        + site_opt("zvuk", "Звук", zv_ok)
        + site_opt("kion", "КИОН Музыка", kion_ok)
        + site_opt("akniga", "akniga.org (аудиокниги)", ak_ok)
        + site_opt("promodj", "PromoDJ", pdj_ok)
        + site_opt("zaycev", "Zaycev.net", zy_ok)
        + "</select>"
    )

    results_html = ""
    if q.strip():
        blocks = []
        artists = []
        albums = []
        zy_tail: list = []   # Zaycev: треки и пагинация — после карточек артистов
        if site == "deezer" and dz_ok:
            try:
                artists = deezer.search_artists(q, limit=15) if hasattr(deezer, "search_artists") else []
            except Exception as e:
                log.warning("webui search artists: %s", e)
            try:
                albums = deezer.search_albums(q, limit=15) if hasattr(deezer, "search_albums") else []
            except Exception as e:
                log.warning("webui search albums: %s", e)
        elif site == "drivemusic" and dm_ok:
            try:
                artists = dm.search_artists(q, limit=20) if hasattr(dm, "search_artists") else []
            except Exception as e:
                log.warning("webui dm search_artists: %s", e)

        elif site == "kion" and kion_ok:
            try:
                artists = ki.search_artists(q, limit=15) or []
            except Exception as e:
                log.warning("webui kion search_artists: %s", e)
                artists = []
            try:
                albums = ki.search_albums(q, limit=15) or []
            except Exception as e:
                log.warning("webui kion search_albums: %s", e)
                albums = []
            try:
                _ktracks = ki.search_tracks(q, limit=20) or []
            except Exception as e:
                log.warning("webui kion search_tracks: %s", e)
                _ktracks = []
            # Порядок как на сайте: исполнители → треки → альбомы
            # артисты/альбомы рендерятся ниже общим циклом; треки — после артистов
            # помечаем, что треки нужно вставить после artists
            _STATE["_kion_pending_tracks"] = _ktracks

        elif site == "yandex" and ya_ok:
            try:
                artists = ya.search_artists(q, limit=15)
            except Exception as e:
                log.warning("webui ya search_artists: %s", e)
            try:
                albums = ya.search_albums(q, limit=15)
            except Exception as e:
                log.warning("webui ya search_albums: %s", e)
        elif site == "zvuk" and zv_ok:
            try:
                artists = zv.search_artists(q, limit=15)
            except Exception as e:
                log.warning("webui zv search_artists: %s", e)
            try:
                albums = zv.search_albums(q, limit=15)
            except Exception as e:
                log.warning("webui zv search_albums: %s", e)
        elif site == "akniga" and ak_ok:
            try:
                books = ak.search_books(q, limit=40, page=page)
            except Exception as e:
                log.warning("webui akniga search: %s", e)
                books = []
            for b in books:
                sub = b.author or "аудиокнига"
                blocks.append(f"""
<div class="search-card">
  <div>
    <div class="name">📖 {html.escape(b.title)}</div>
    <div class="desc">{html.escape(sub)}</div>
  </div>
  <div class="actions">
    <form method="POST" action="/search/save" style="display:inline">
      <input type="hidden" name="site" value="akniga">
      <input type="hidden" name="type" value="book">
      <input type="hidden" name="slug" value="{html.escape(b.slug)}">
      <input type="hidden" name="title" value="{html.escape(b.title)}">
      <input type="hidden" name="query" value="{html.escape(q)}">
      <button type="submit" class="small">+ В DLNA</button>
    </form>
    <form method="POST" action="/search/favorite" style="display:inline">
      <input type="hidden" name="site" value="akniga">
      <input type="hidden" name="type" value="book">
      <input type="hidden" name="slug" value="{html.escape(b.slug)}">
      <input type="hidden" name="title" value="{html.escape(b.title)}">
      <input type="hidden" name="query" value="{html.escape(q)}">
      <button type="submit" class="small ghost">⭐ В избранное</button>
    </form>
  </div>
</div>""")
            # Пагинация akniga.org: /search/books/pageN/?q=...
            if books or page > 1:
                q_enc = quote(q)
                prev_link = (
                    f'<a class="btn ghost small" href="/search?site=akniga&amp;q={html.escape(q_enc)}&amp;page={page - 1}">← Стр. {page - 1}</a>'
                    if page > 1 else ""
                )
                next_link = (
                    f'<a class="btn small" href="/search?site=akniga&amp;q={html.escape(q_enc)}&amp;page={page + 1}">Стр. {page + 1} →</a>'
                    if books else ""
                )
                blocks.append(
                    f'<div class="row" style="margin-top:12px;justify-content:space-between">'
                    f'{prev_link}'
                    f'<span class="muted">Страница {page}</span>'
                    f'{next_link}'
                    f'</div>'
                )
        elif site == "zaycev" and zy_ok:
            # Исполнители ищем здесь (через веб-поиск) и кладём в DLNA кнопкой «+ В DLNA».
            # Треки поиска ресивер находит сам: DLNA Search → Zaycev.net.
            if page == 1:
                try:
                    artists = zy.search_artists(q, limit=20) or []
                except Exception as e:
                    log.warning("webui zaycev search_artists: %s", e)
                    artists = []
            try:
                zy_tracks, zy_pages = zy.search_tracks_page(q, page=page, limit=20)
            except Exception as e:
                log.warning("webui zaycev search_tracks: %s", e)
                zy_tracks, zy_pages = [], 0
            for tr in zy_tracks:
                label = f"{tr.artist} — {tr.title}" if tr.artist else tr.title
                dur = f"{tr.duration // 60}:{tr.duration % 60:02d}" if tr.duration else ""
                zy_tail.append(
                    '<div class="search-card"><div>'
                    f'<div class="name">🎵 {html.escape(label)}</div>'
                    f'<div class="desc">трек{(" · " + dur) if dur else ""} · id {html.escape(tr.id)}</div>'
                    '</div><div class="actions">'
                    '<span class="muted" style="font-size:12px">DLNA → Zaycev.net</span>'
                    '</div></div>'
                )
            if zy_tracks or page > 1:
                q_enc = quote(q)
                prev_link = (
                    f'<a class="btn ghost small" href="/search?site=zaycev&amp;q={html.escape(q_enc)}&amp;page={page - 1}">← Стр. {page - 1}</a>'
                    if page > 1 else ""
                )
                next_link = (
                    f'<a class="btn small" href="/search?site=zaycev&amp;q={html.escape(q_enc)}&amp;page={page + 1}">Стр. {page + 1} →</a>'
                    if zy_tracks and (zy_pages == 0 or page < zy_pages) else ""
                )
                zy_tail.append(
                    '<div class="row" style="margin-top:12px;justify-content:space-between">'
                    f'{prev_link}'
                    f'<span class="muted">Страница {page}{(" из " + str(zy_pages)) if zy_pages else ""}</span>'
                    f'{next_link}'
                    '</div>'
                )
        elif site == "promodj" and pdj_ok:
            # Люди (DJ) — mode=user + музыка mode=audio
            users = []
            try:
                users, users_total = pdj.search_users(q, page=page)
            except Exception as e:
                log.warning("webui promodj search_users: %s", e)
                users, users_total = [], 0
            try:
                tracks, total_pages = pdj.search(q, page=page)
            except Exception as e:
                log.warning("webui promodj search: %s", e)
                tracks, total_pages = [], 0
            for u in users:
                sub = (u.styles or "") + ((" · " + u.country) if u.country else "")
                sub = sub.strip(" ·") or "DJ / артист"
                blocks.append(f"""
<div class="search-card">
  <div>
    <div class="name">👤 {html.escape(u.name)}</div>
    <div class="desc">{html.escape(sub)} · @{html.escape(u.slug)}</div>
  </div>
  <div class="actions">
    <form method="POST" action="/search/save" style="display:inline">
      <input type="hidden" name="site" value="promodj">
      <input type="hidden" name="type" value="artist">
      <input type="hidden" name="artist_id" value="{html.escape(u.slug)}">
      <input type="hidden" name="title" value="{html.escape(u.name)}">
      <input type="hidden" name="query" value="{html.escape(q)}">
      <button type="submit" class="small">+ В DLNA</button>
    </form>
  </div>
</div>""")
            for tr in tracks:
                sub = tr.artist or tr.styles or "PromoDJ"
                blocks.append(f"""
<div class="search-card">
  <div>
    <div class="name">🎵 {html.escape(tr.title)}</div>
    <div class="desc">{html.escape(sub)}</div>
  </div>
  <div class="actions">
    <span class="muted" style="font-size:12px">DLNA → PromoDJ</span>
  </div>
</div>""")
            if users or tracks or page > 1:
                q_enc = quote(q)
                prev_link = (
                    f'<a class="btn ghost small" href="/search?site=promodj&amp;q={html.escape(q_enc)}&amp;page={page - 1}">← Стр. {page - 1}</a>'
                    if page > 1 else ""
                )
                next_link = (
                    f'<a class="btn small" href="/search?site=promodj&amp;q={html.escape(q_enc)}&amp;page={page + 1}">Стр. {page + 1} →</a>'
                    if (tracks or users) and (total_pages == 0 or page < max(total_pages, 1))
                    else ""
                )
                blocks.append(
                    f'<div class="row" style="margin-top:12px;justify-content:space-between">'
                    f'{prev_link}'
                    f'<span class="muted">Страница {page}'
                    f'{(" · треков ~" + str(total_pages * 10)) if total_pages else ""}'
                    f'{(" · людей " + str(users_total)) if users_total else ""}</span>'
                    f'{next_link}'
                    f'</div>'
                )
            # сохранённые
            saved = tree.pd_saved_searches() if (tree and hasattr(tree, "pd_saved_searches")) else []
            if saved:
                blocks.append('<h3 style="margin-top:18px">Сохранённые в DLNA · PromoDJ</h3>')
                for s in saved:
                    blocks.append(f"""
<div class="search-card">
  <div>
    <div class="name">👤 {html.escape(s.get("title") or s.get("slug") or "")}</div>
    <div class="desc">@{html.escape(s.get("slug") or s.get("id") or "")}</div>
  </div>
  <div class="actions">
    <form method="POST" action="/search/remove" style="display:inline">
      <input type="hidden" name="site" value="promodj">
      <input type="hidden" name="id" value="{html.escape(s.get("id") or s.get("slug") or "")}">
      <button type="submit" class="small ghost">Удалить</button>
    </form>
  </div>
</div>""")
        else:
            blocks.append(
                f'<p class="muted">Источник «{html.escape(site)}» недоступен. '
                "Проверьте настройки (ARL / токен) и перезапустите.</p>"
            )

        if site == "zaycev" and artists:
            blocks.append('<h3 style="margin-top:6px">Исполнители</h3>')
        for a in (artists or []):
            try:
                if not isinstance(a, dict):
                    continue
                aname = str(a.get("name") or a.get("title") or "")
                n = a.get("nb_album") or a.get("nb_tracks") or 0
                sub = (f"{n} треков" if site == "zaycev" else f"{n} альбомов/треков") if n else "исполнитель"
                blocks.append(
                    '<div class="search-card"><div>'
                    f'<div class="name">👤 {html.escape(aname)}</div>'
                    f'<div class="desc">{html.escape(str(sub))}</div>'
                    '</div><div class="actions">'
                    '<form method="POST" action="/search/save" style="display:inline">'
                    f'<input type="hidden" name="site" value="{html.escape(site)}">'
                    '<input type="hidden" name="type" value="artist">'
                    f'<input type="hidden" name="artist_id" value="{html.escape(str(a.get("id") or ""))}">'
                    f'<input type="hidden" name="title" value="{html.escape(aname)}">'
                    f'<input type="hidden" name="query" value="{html.escape(q)}">'
                    '<button type="submit" class="small">+ В DLNA</button>'
                    '</form></div></div>'
                )
            except Exception as e:
                log.warning("webui artist card: %s", e)
        if zy_tail:
            blocks.append('<h3 style="margin-top:18px">Треки</h3>')
        blocks.extend(zy_tail)
        # КИОН: треки после артистов, до альбомов
        for tr in (_STATE.pop("_kion_pending_tracks", None) or []):
            try:
                art = (getattr(tr, "artist", None) or "").strip()
                tit = (getattr(tr, "title", None) or "").strip() or str(getattr(tr, "id", ""))
                label = f"{art} — {tit}" if art else tit
                blocks.append(
                    '<div class="search-card"><div>'
                    f'<div class="name">🎵 {html.escape(str(label))}</div>'
                    f'<div class="desc">трек · id {html.escape(str(getattr(tr, "id", "")))}</div>'
                    "</div></div>"
                )
            except Exception as e:
                log.warning("webui kion track card: %s", e)
        for al in (albums or []):
            try:
                if not isinstance(al, dict):
                    continue
                title = str(al.get("title") or "")
                artist = str(al.get("artist") or "")
                nb = al.get("nb_tracks") or 0
                full = f"{artist} — {title}" if artist else title
                year = str(al.get("year") or "")
                sub = f"{nb} треков" + (f" · {artist}" if artist else "") + (f" · {year}" if year else "")
                blocks.append(
                    '<div class="search-card"><div>'
                    f'<div class="name">💿 {html.escape(full)}</div>'
                    f'<div class="desc">{html.escape(sub)}</div>'
                    '</div><div class="actions">'
                    '<form method="POST" action="/search/save" style="display:inline">'
                    f'<input type="hidden" name="site" value="{html.escape(site)}">'
                    '<input type="hidden" name="type" value="album">'
                    f'<input type="hidden" name="album_id" value="{html.escape(str(al.get("id") or ""))}">'
                    f'<input type="hidden" name="title" value="{html.escape(full)}">'
                    f'<input type="hidden" name="query" value="{html.escape(q)}">'
                    '<button type="submit" class="small">+ В DLNA</button>'
                    '</form></div></div>'
                )
            except Exception as e:
                log.warning("webui album card: %s", e)
        if not blocks and q.strip():
            blocks.append('<p class="muted">Ничего не найдено.</p>')
        if blocks:
            results_html = (
                '<div style="margin-top:16px">'
                f'<p class="muted">Найдено: {len(artists)} артистов'
                + (f", {len(albums)} альбомов" if albums or site not in ("drivemusic", "zaycev") else "")
                + (f", треки" if site == "kion" else "")
                + f" · источник: {html.escape(site)}</p>"
                + "".join(blocks)
                + "</div>"
            )

    # saved lists for current site
    if site == "deezer":
        saved = tree.saved_searches() if (tree and hasattr(tree, "saved_searches")) else []
        remove_action = "/search/remove"
        clear_action = "/search/clear"
        folder_hint = "Deezer → 🔍 Мои поиски"
    elif site == "drivemusic":
        saved = tree.dm_saved_searches() if (tree and hasattr(tree, "dm_saved_searches")) else []
        remove_action = "/search/remove"
        clear_action = "/search/clear"
        folder_hint = "DriveMusic → 🔍 Мои поиски"
    elif site == "akniga":
        saved = tree.ak_saved_books() if (tree and hasattr(tree, "ak_saved_books")) else []
        remove_action = "/search/remove"
        clear_action = "/search/clear"
        folder_hint = "akniga.org (аудиокниги) → 🔍 Мои книги / ⭐ Избранное"
    elif site == "kion":
        saved = tree.kion_saved_searches() if tree and hasattr(tree, "kion_saved_searches") else []
        remove_action = "/search/remove"
        clear_action = "/search/clear"
        folder_hint = "КИОН Музыка → 🔍 Мои поиски"
    elif site == "yandex":
        saved = tree.ya_saved_searches() if (tree and hasattr(tree, "ya_saved_searches")) else []
        remove_action = "/search/remove"
        clear_action = "/search/clear"
        folder_hint = "Яндекс.Музыка → 🔍 Мои поиски"
    elif site == "zvuk":
        saved = tree.zv_saved_searches() if (tree and hasattr(tree, "zv_saved_searches")) else []
        remove_action = "/search/remove"
        clear_action = "/search/clear"
        folder_hint = "Звук → 🔍 Мои поиски"
    elif site == "zaycev":
        saved = tree.zy_saved_searches() if (tree and hasattr(tree, "zy_saved_searches")) else []
        remove_action = "/search/remove"
        clear_action = "/search/clear"
        folder_hint = "Zaycev.net → 🔍 Мои поиски"
    else:
        saved = tree.ya_saved_searches() if (tree and hasattr(tree, "ya_saved_searches")) else []
        remove_action = "/search/remove"
        clear_action = "/search/clear"
        folder_hint = "Яндекс.Музыка → 🔍 Мои поиски"

    def _rows_for(items, remove_path, site_val, list_kind=""):
        rows = []
        for s in items:
            icon = "📖" if s.get("type") == "book" else ("👤" if s.get("type") == "artist" else "💿")
            kind = "Книга" if s.get("type") == "book" else ("Артист" if s.get("type") == "artist" else "Альбом")
            rows.append(f"""
<div class="search-card">
  <div>
    <div class="name">{icon} {html.escape(s.get('title') or '')}</div>
    <div class="desc">{kind}{(' · запрос: ' + html.escape(s.get('query') or '')) if s.get('query') else ''}</div>
  </div>
  <div class="actions">
    <form method="POST" action="{remove_path}" style="display:inline">
      <input type="hidden" name="site" value="{html.escape(site_val)}">
      <input type="hidden" name="sid" value="{html.escape(s.get('id') or '')}">
      {f'<input type="hidden" name="list" value="{html.escape(list_kind)}">' if list_kind else ''}
      <button type="submit" class="small ghost">Удалить</button>
    </form>
  </div>
</div>""")
        return rows

    if site == "akniga":
        favs = tree.ak_favorites() if (tree and hasattr(tree, "ak_favorites")) else []
        saved_rows = _rows_for(saved, "/search/remove", "akniga", "saved")
        fav_rows = _rows_for(favs, "/search/remove", "akniga", "favorite")
        saved_html = (
            "".join(saved_rows)
            if saved_rows
            else '<p class="muted">Пока пусто. Найди книгу и нажми «+ В DLNA».</p>'
        )
        fav_html = (
            "".join(fav_rows)
            if fav_rows
            else '<p class="muted">Пока пусто. Найди книгу и нажми «⭐ В избранное».</p>'
        )
        extra_cards = f"""
<div class="card">
  <h2>🔍 Мои книги · akniga.org</h2>
  <p class="muted">Появляются на ресивере в <strong>akniga.org (аудиокниги) → 🔍 Мои книги</strong> (до перезапуска).</p>
  {saved_html}
  {('<form method="POST" action="/search/clear" style="margin-top:12px"><input type="hidden" name="site" value="akniga"><input type="hidden" name="list" value="saved"><button type="submit" class="ghost">Очистить всё</button></form>') if saved else ''}
</div>
<div class="card">
  <h2>⭐ Избранное · akniga.org</h2>
  <p class="muted">Полное управление — на вкладке <a href="/favorites" style="color:#3d8bfd">⭐ Избранное</a>. Сохраняется в <code>akniga_favorites.json</code> (до 500). На ресивере: <strong>akniga.org (аудиокниги) → ⭐ Избранное</strong>.</p>
  {fav_html}
</div>
"""
        hint_text = (
            "Выбери сайт, найди книгу, нажми «+ В DLNA» (мои книги) или «⭐ В избранное» — "
            f"появится на ресивере в <strong>{html.escape(folder_hint)}</strong>."
        )
    else:
        saved_rows = _rows_for(saved, remove_action, site)
        if not saved_rows:
            saved_html = '<p class="muted">Пока пусто. Найди что-нибудь и нажми «+ В DLNA».</p>'
        else:
            saved_html = "".join(saved_rows)
        extra_cards = f"""
<div class="card">
  <h2>Сохранённые в DLNA · {html.escape({'deezer': 'Deezer', 'drivemusic': 'DriveMusic', 'yandex': 'Яндекс.Музыка', 'kion': 'КИОН Музыка', 'zvuk': 'Звук'}.get(site, site))}</h2>
  {saved_html}
  {('<form method="POST" action="' + clear_action + '" style="margin-top:12px"><input type="hidden" name="site" value="' + html.escape(site) + '"><button type="submit" class="ghost">Очистить всё</button></form>') if saved else ''}
</div>
"""
        hint_text = (
            f"Выбери сайт, найди артиста/альбом, нажми «+ В DLNA» — "
            f"появится на ресивере в <strong>{html.escape(folder_hint)}</strong>."
        )

    return f"""
<div class="card">
  <h2>🔍 Поиск</h2>
  <p class="muted">{hint_text}</p>
  <form method="GET" action="/search">
    {site_select}
    <label>Что искать</label>
    <input name="q" value="{html.escape(q)}" autofocus
           placeholder="например: aikko, pink floyd, the prodigy">
    <div class="row">
      <button type="submit">Искать</button>
      <a class="btn ghost" href="/">Отмена</a>
    </div>
  </form>
  {results_html}
</div>

{extra_cards}
"""


_SVC_TITLES = {
    "deezer": "Deezer", "drivemusic": "DriveMusic", "akniga": "AKniga",
    "yandex": "Яндекс Музыка", "zvuk": "Звук", "kion": "KION",
    "promodj": "PromoDJ", "zaycev": "Zaycev.net", "files": "Файлы",
}


def _cur_settings() -> dict:
    cp = _cfg().get("current_playlists") or {}

    def _int(v, d, lo, hi):
        try:
            return max(lo, min(int(v), hi))
        except (TypeError, ValueError):
            return d

    album = cp.get("skip_same_album")
    if isinstance(album, str):
        album = album.lower() in ("1", "true", "yes", "on")
    return {
        "max_idle_days": _int(cp.get("max_idle_days"), 0, 0, 3650),
        "max_folders": _int(cp.get("max_folders"), 100, 1, 1000),
        "skip_artist_repeats": _int(cp.get("skip_artist_repeats"), 6, 0, 1000),
        "skip_same_album": True if album is None else bool(album),
    }


def _ago(ts: float) -> str:
    if not ts:
        return "—"
    d = max(0.0, time.time() - ts)
    when = time.strftime("%d.%m.%Y %H:%M", time.localtime(ts))
    if d < 3600:
        return f"{when} ({int(d // 60)} мин. назад)"
    if d < 86400:
        return f"{when} ({int(d // 3600)} ч. назад)"
    return f"{when} ({int(d // 86400)} дн. назад)"


def _current_body(open_svcs=None) -> str:
    """Вкладка «Текущие плейлисты». open_svcs — площадки, раскрытые после действия."""
    open_svcs = set(open_svcs or ())
    st = _cur_settings()
    tree = _STATE.get("tree")
    rows = []
    try:
        rows = tree.current_overview() if tree is not None else []
    except Exception as e:
        log.warning("current overview: %s", e)

    def _split_key(key: str):
        """'yandex.a1@192.168.1.64' -> ('yandex', 1, '192.168.1.64')"""
        svc, _, ip = key.partition("@")
        acc = 0
        m = re.match(r"^(.*)\.a(\d+)$", svc)
        if m:
            svc, acc = m.group(1), int(m.group(2))
        return svc, acc, ip

    # площадка -> ключ истории (аккаунт + устройство) -> папки
    by_svc: Dict[str, Dict[str, list]] = {}
    for r in rows:
        svc = _split_key(r["key"])[0]
        by_svc.setdefault(svc, {}).setdefault(r["key"], []).append(r)

    def _group_title(key: str) -> str:
        _svc, acc, ip = _split_key(key)
        parts = []
        if acc:
            parts.append(f"аккаунт {acc + 1}")
        if ip:
            parts.append(f"устройство {ip}")
        return " · ".join(parts) or "общий список"

    sections = []
    gi = 0
    for svc in sorted(by_svc, key=lambda x: _SVC_TITLES.get(x, x).lower()):
        keys = by_svc[svc]
        n_svc = sum(len(v) for v in keys.values())
        blocks = []
        for key in sorted(keys, key=lambda k: _group_title(k).lower()):
            items = sorted(keys[key], key=lambda r: -r["updated"])
            trs = []
            for r in items:
                val = html.escape(f'{r["key"]}|{r["id"]}', quote=True)
                pos = f'{r["start"] + 1}/{r["tracks"]}' if r["start"] > 0 else f'—/{r["tracks"]}'
                trs.append(
                    f'<tr><td style="width:28px"><input type="checkbox" name="del" value="{val}" '
                    f'class="cur-row g{gi}" style="width:auto"></td>'
                    f'<td>{html.escape(r["title"])}</td>'
                    f'<td style="text-align:right;white-space:nowrap">{r["tracks"]}</td>'
                    f'<td style="text-align:right;white-space:nowrap">{pos}</td>'
                    f'<td class="muted" style="white-space:nowrap">{_ago(r["updated"])}</td></tr>'
                )
            blocks.append(
                '<div style="margin:10px 0 14px">'
                '<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:6px">'
                f'<b style="font-size:13px">{html.escape(_group_title(key))}</b>'
                f'<span class="muted" style="font-size:12px">плейлистов: {len(items)}</span>'
                '<label style="display:flex;align-items:center;gap:6px;margin:0;font-size:12px">'
                f'<input type="checkbox" class="cur-all" data-g="g{gi}" style="width:auto">'
                'выбрать все</label></div>'
                '<div style="overflow-x:auto"><table>'
                '<tr class="muted"><th></th><th style="text-align:left">Плейлист</th>'
                '<th style="text-align:right">Треков</th><th style="text-align:right">Позиция</th>'
                '<th style="text-align:left">Играл последний раз</th></tr>'
                + "".join(trs) + '</table></div></div>'
            )
            gi += 1
        is_open = " open" if svc in open_svcs else ""
        sections.append(
            f'<details class="svc"{is_open}>'
            f'<summary>{html.escape(_SVC_TITLES.get(svc, svc))}'
            f'<span class="muted" style="font-weight:400;font-size:13px;margin-left:8px">'
            f'— плейлистов: {n_svc}</span></summary>'
            f'<div style="padding:0 4px 4px 18px">{"".join(blocks)}</div></details>'
        )

    total = len(rows)
    undo_n = 0
    try:
        undo_n = tree.deleted_backup_count() if tree is not None else 0
    except Exception:
        undo_n = 0
    undo = ""
    if undo_n:
        undo = (
            '<form method="POST" action="/current/undo" style="margin:0 0 12px;padding:10px 12px;'
            'border:1px solid #2d3a4f;border-radius:8px;display:flex;align-items:center;gap:12px;'
            'flex-wrap:wrap">'
            f'<span style="font-size:13px">Последнее удаление: плейлистов — <b>{undo_n}</b>.</span>'
            '<button type="submit" class="ghost small">↩ Вернуть удалённые</button></form>'
        )

    manage = (
        undo
        + '<form method="POST" action="/current/delete" id="cur-form">'
        '<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:6px">'
        f'<span class="muted" style="font-size:13px">Всего сохранено: <b>{total}</b>. '
        'Нажмите на площадку, чтобы увидеть её плейлисты.</span>'
        '<button type="submit" id="cur-del" class="small" disabled style="margin-left:auto">'
        '🗑 Удалить отмеченные</button></div>'
        + ("".join(sections) if sections else
           '<p class="muted" style="margin-top:14px">Пока пусто. Плейлист появляется здесь, '
           'когда вы включаете из него трек на устройстве.</p>')
        + '</form>'
        '<style>'
        'details.svc{border-top:1px solid #243044}'
        'details.svc>summary{cursor:pointer;padding:12px 4px;font-weight:600;list-style:none}'
        'details.svc>summary::-webkit-details-marker{display:none}'
        'details.svc>summary::before{content:"▸";display:inline-block;width:18px;color:var(--muted)}'
        'details.svc[open]>summary::before{content:"▾"}'
        '</style>'
        '<script>(function(){'
        'var btn=document.getElementById("cur-del"),form=document.getElementById("cur-form");'
        'function upd(){var n=form.querySelectorAll("input.cur-row:checked").length;'
        'btn.disabled=!n;btn.textContent="🗑 Удалить отмеченные"+(n?" ("+n+")":"");}'
        'document.querySelectorAll(".cur-all").forEach(function(cb){'
        'cb.addEventListener("change",function(){document.querySelectorAll("input."+cb.dataset.g)'
        '.forEach(function(x){x.checked=cb.checked;});upd();});});'
        'form.addEventListener("change",upd);'
        'form.addEventListener("submit",function(e){'
        'var n=form.querySelectorAll("input.cur-row:checked").length;'
        'if(!n||!confirm("Удалить отмеченные плейлисты: "+n+" шт.?")){e.preventDefault();}});'
        'upd();})();</script>'
    )

    chk = " checked" if st["skip_same_album"] else ""
    return f"""
<div class="card">
  <h2>Что запоминать</h2>
  <p class="muted" style="font-size:12px;margin:0 0 8px">
    На устройстве папка «* Текущий плейлист» — история подборок, которые вы слушали. Эмулятор
    подрезает прослушанное у себя, а устройству отдаёт список как он был при старте плейлиста —
    подрезанный вариант вы увидите, когда включите другой плейлист и вернётесь в этот.
  </p>
  <form method="POST" action="/current/save">
    <label>Сколько плейлистов помнить (для каждой площадки и устройства)</label>
    <input name="cur_max_folders" type="number" min="1" max="1000" value="{st['max_folders']}">
    <p class="muted" style="font-size:12px;margin-top:4px">
      Когда набирается больше, самые старые (по последнему воспроизведению) удаляются.
    </p>

    <label>Не сохранять, если один исполнитель встречается в списке столько раз и более</label>
    <input name="cur_skip_artist" type="number" min="0" max="1000" value="{st['skip_artist_repeats']}">
    <p class="muted" style="font-size:12px;margin-top:4px">
      По умолчанию <code>6</code>: страницы артистов и «лучшее исполнителя» в историю не попадают.
      <code>0</code> — не проверять.
    </p>

    {_chk_simple("cur_skip_album", "Не сохранять, если у всех треков одинаковый ALBUM", chk,
                 "это альбом целиком. Если альбома в тегах нет — плейлист сохраняется")}

    <label>Удалять неигравшие более (дней)</label>
    <input name="cur_idle_days" type="number" min="0" max="3650" value="{st['max_idle_days']}">
    <p class="muted" style="font-size:12px;margin-top:4px">
      <code>0</code> — не удалять автоматически. Очистка выполняется при сохранении и при запуске.
    </p>

    <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">
      <button type="submit">💾 Сохранить</button>
      <button type="submit" class="ghost" formaction="/current/prune" formnovalidate
              onclick="return confirm('Удалить плейлисты, не игравшие дольше указанного срока?')">
        🗑 Удалить сейчас по сроку</button>
    </div>
  </form>
</div>

<div class="card">
  <h2>Сохранённые плейлисты</h2>
  {manage}
</div>
"""


def _chk_simple(name: str, label: str, checked_attr: str, hint: str = "") -> str:
    h = (f'<span class="muted" style="font-size:12px;margin-left:6px">{html.escape(hint)}</span>'
         if hint else "")
    return (
        '<label style="display:flex;align-items:center;gap:8px;color:var(--text);'
        'font-size:14px;margin:12px 0 4px">'
        f'<input type="checkbox" name="{name}" value="1"{checked_attr} style="width:auto">'
        f'{html.escape(label)}{h}</label>'
    )



def _favorites_body(flash: str = "") -> str:
    tree = _STATE.get("tree")
    favs = tree.ak_favorites() if (tree and hasattr(tree, "ak_favorites")) else []
    n = len(favs)
    rows = []
    for s in favs:
        rows.append(f"""
<div class="search-card">
  <div>
    <div class="name">📖 {html.escape(s.get('title') or '')}</div>
    <div class="desc">Книга{(' · запрос: ' + html.escape(s.get('query') or '')) if s.get('query') else ''}</div>
  </div>
  <div class="actions">
    <form method="POST" action="/favorites/remove" style="display:inline">
      <input type="hidden" name="sid" value="{html.escape(s.get('id') or '')}">
      <button type="submit" class="small ghost">Удалить</button>
    </form>
  </div>
</div>""")
    if not rows:
        list_html = (
            '<p class="muted">Пока пусто. Найди книгу на вкладке '
            '<a href="/search?site=akniga" style="color:#3d8bfd">🔍 Поиск</a> '
            'и нажми «⭐ В избранное».</p>'
        )
    else:
        list_html = "".join(rows)
    clear_btn = ""
    if favs:
        clear_btn = (
            '<form method="POST" action="/favorites/clear" style="margin-top:12px">'
            '<button type="submit" class="ghost">Очистить всё избранное</button></form>'
        )
    return f"""
<div class="card">
  <h2>⭐ Избранное · akniga.org</h2>
  <p class="muted">
    Книги сохраняются в <code>akniga_favorites.json</code> и переживают рестарт.
    На ресивере: <strong>akniga.org (аудиокниги) → ⭐ Избранное</strong>.
    Занято: <strong>{n}</strong> / 500.
  </p>
  {list_html}
  {clear_btn}
</div>
"""


def _logs_body() -> str:
    with _LOG_LOCK:
        n_in_buf = len(_LOG_BUFFER)
    with _LOG_COUNTER_LOCK:
        total = _LOG_COUNTER
    return f"""
<div class="card">
  <h2>Консоль сервера</h2>
  <p class="muted">
    Обновление — раз в секунду. В буфере: <span id="log-bufsize">{n_in_buf}</span> из 2000,
    всего записей: <span id="log-total">{total}</span>.
  </p>
  <div class="logs-toolbar">
    <button type="button" class="ghost small" id="log-pause">Пауза</button>
    <button type="button" class="ghost small" id="log-clear">Очистить</button>
    <button type="button" class="small" id="log-download">⬇ Скачать лог</button>
    <label style="display:flex;align-items:center;gap:6px;color:var(--muted);font-size:13px;margin:0">
      <input type="checkbox" id="log-autoscroll" checked style="width:auto">
      автоскролл
    </label>
    <span class="muted" id="log-status">загрузка…</span>
  </div>
  <div class="logs" id="logs"></div>
</div>
<script>
(function() {{
  const box = document.getElementById('logs');
  const status = document.getElementById('log-status');
  if (!box) return;
  let lastIdx = 0, autoScroll = true, paused = false;
  function esc(s) {{ return String(s).replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c])); }}
  function appendLine(l) {{
    const div = document.createElement('div');
    div.className = 'lvl-' + (l.lvl || 'INFO');
    const t = new Date((l.t || 0) * 1000);
    const ts = t.toTimeString().slice(0, 8);
    div.innerHTML = '<span class="ts">' + ts + '</span>' +
      '<span class="name">' + esc(l.n || '') + ':</span> ' + esc(l.m || '');
    box.appendChild(div);
  }}
  document.getElementById('log-clear').addEventListener('click', async () => {{
    try {{ await fetch('/logs/clear', {{ method: 'POST' }}); }} catch (e) {{}}
    box.innerHTML = ''; lastIdx = 0; status.textContent = 'очищено';
  }});
  document.getElementById('log-download').addEventListener('click', () => {{
    window.location.href = '/logs/download';
  }});
  document.getElementById('log-pause').addEventListener('click', (e) => {{
    paused = !paused;
    e.target.textContent = paused ? 'Продолжить' : 'Пауза';
    e.target.classList.toggle('ghost', !paused);
  }});
  document.getElementById('log-autoscroll').addEventListener('change', (e) => {{ autoScroll = e.target.checked; }});
  async function poll() {{
    if (!paused) {{
      try {{
        const r = await fetch('/logs/tail?from=' + lastIdx, {{ cache: 'no-store' }});
        const data = await r.json();
        if (data.bufsize !== undefined) {{
          const el = document.getElementById('log-bufsize'); if (el) el.textContent = data.bufsize;
        }}
        if (data.total !== undefined) {{
          const el = document.getElementById('log-total'); if (el) el.textContent = data.total;
        }}
        if (data.lines && data.lines.length) {{
          for (const l of data.lines) {{ appendLine(l); lastIdx = l.i; }}
          if (autoScroll) box.scrollTop = box.scrollHeight;
          status.textContent = 'последняя запись #' + lastIdx;
        }} else {{ status.textContent = 'последняя запись #' + lastIdx; }}
      }} catch (e) {{ status.textContent = 'ошибка: ' + e; }}
    }}
    setTimeout(poll, 1000);
  }}
  poll();
}})();
</script>
"""


# ============================================================
#  Handler
# ============================================================

class WebUIHandler(BaseHTTPRequestHandler):
    server_version = "DLNA-emu-web/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        try:
            path = urlparse(self.path).path
        except Exception:
            path = ""
        if path in ("/logs/tail", "/logs/clear"):
            return
        log.debug("%s - " + fmt, self.address_string(), *args)

    def _send_html(self, body: bytes, code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj, code: int = 200):
        import json as _json
        body = _json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _send_download(self, filename: str, body: bytes, mime: str = "text/plain; charset=utf-8"):
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _redirect(self, location: str):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        qs = parse_qs(urlparse(self.path).query)

        if path in ("/", "/status"):
            self._send_html(_page("Статус", _status_body(), active="status"))
            return
        if path == "/settings":
            self._send_html(_page("Настройки", _settings_body(), active="settings"))
            return
        if path == "/radio":
            view_pid = (qs.get("pl") or [""])[0]
            self._send_html(_page("Радио", _radio_body(view_pid=view_pid), active="radio"))
            return
        if path == "/search":
            q = (qs.get("q") or [""])[0]
            site = (qs.get("site") or ["deezer"])[0]
            try:
                page = int((qs.get("page") or ["1"])[0])
            except (TypeError, ValueError):
                page = 1
            try:
                body = _search_body(q=q, site=site, page=page)
            except Exception as e:
                log.exception("search page failed site=%s q=%r", site, q)
                body = (
                    f'<div class="card"><h2>Ошибка поиска</h2>'
                    f'<p class="muted">{html.escape(str(e))}</p>'
                    f'<p><a href="/search?site={html.escape(site)}">Назад</a></p></div>'
                )
            self._send_html(_page("Поиск", body, active="search"))
            return
        if path == "/current":
            self._send_html(_page("Текущие плейлисты", _current_body(), active="current"))
            return
        if path == "/favorites":
            self._send_html(_page(
                "Избранное",
                _favorites_body(),
                active="favorites",
            ))
            return
        if path == "/dmsearch":
            # совместимость: редирект на единый поиск
            q = (qs.get("q") or [""])[0]
            self._redirect(f"/search?site=drivemusic&q={quote(q)}" if q else "/search?site=drivemusic")
            return
        if path in ("/logs", "/console"):
            self._send_html(_page("Консоль", _logs_body(), active="logs"))
            return

        if path == "/logs/download":
            with _LOG_LOCK:
                lines = list(_LOG_BUFFER)
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            out = []
            for e in lines:
                t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e["t"]))
                out.append(f"{t} [{e['lvl']}] {e['n']}: {e['m']}")
            body = ("\n".join(out) + "\n").encode("utf-8")
            self._send_download(f"dlna-emu-{ts}.log", body)
            return

        if path == "/logs/tail":
            try:
                from_idx = int((qs.get("from") or ["0"])[0])
            except ValueError:
                from_idx = 0
            with _LOG_LOCK:
                lines = [e for e in _LOG_BUFFER if e["i"] > from_idx]
                bufsize = len(_LOG_BUFFER)
            with _LOG_COUNTER_LOCK:
                total = _LOG_COUNTER
            self._send_json({"lines": lines, "bufsize": bufsize, "total": total})
            return

        self._send_html(_page("404", "<div class='card'><p>Не найдено</p></div>"), 404)

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
        form = {k: (v[0] if v else "") for k, v in parse_qs(raw, keep_blank_values=True).items()}

        if path == "/current/delete":
            tree = _STATE.get("tree")
            multi = parse_qs(raw, keep_blank_values=True)
            n = 0
            touched = set()
            names = []
            if tree is not None:
                # Удаляем СТРОГО отмеченное: пары (ключ истории, id плейлиста)
                items = []
                for v in multi.get("del") or []:
                    key, _, fid = v.partition("|")
                    if key and fid:
                        items.append((key, fid))
                if items:
                    titles = {(r["key"], r["id"]): r["title"] for r in tree.current_overview()}
                    names = [titles.get(i, "?") for i in items]
                    n = tree.delete_current_folders(items)
                    for key, _fid in items:
                        touched.add(re.sub(r"\.a\d+$", "", key.partition("@")[0]))
            if n:
                shown = ", ".join(f"«{x}»" for x in names[:3]) + ("…" if len(names) > 3 else "")
                flash = f"Удалено плейлистов: {n} — {shown}"
            else:
                flash = "Ничего не выбрано"
            self._send_html(_page(
                "Текущие плейлисты", _current_body(open_svcs=touched),
                flash=flash, active="current",
            ))
            return

        if path == "/current/undo":
            tree = _STATE.get("tree")
            n = tree.restore_last_deleted() if tree is not None else 0
            self._send_html(_page(
                "Текущие плейлисты", _current_body(),
                flash=(f"Возвращено плейлистов: {n}" if n else "Нечего возвращать"),
                active="current",
            ))
            return

        if path == "/current/save":
            tree = _STATE.get("tree")
            cfg = _cfg()

            def _ci(name, default, lo, hi):
                try:
                    v = int(form.get(name) or default)
                except (TypeError, ValueError):
                    v = default
                return max(lo, min(v, hi))

            cp = cfg.setdefault("current_playlists", {})
            cp["max_folders"] = _ci("cur_max_folders", 100, 1, 1000)
            try:
                cp["skip_artist_repeats"] = max(0, min(int(form.get("cur_skip_artist", 6)), 1000))
            except (TypeError, ValueError):
                cp["skip_artist_repeats"] = 6
            cp["skip_same_album"] = bool(form.get("cur_skip_album"))
            cp["max_idle_days"] = _ci("cur_idle_days", 0, 0, 3650)
            msg = "Сохранено"
            try:
                _save_yaml(cfg, _config_path())
                _STATE["cfg"] = cfg
            except Exception as e:
                log.warning("save current_playlists: %s", e)
                msg = f"Не удалось записать config.yaml: {e}"
            if tree is not None:
                try:
                    cut = tree.configure_current(
                        max_folders=cp["max_folders"],
                        skip_artist_repeats=cp["skip_artist_repeats"],
                        skip_same_album=cp["skip_same_album"],
                    )
                    pr = tree.prune_current_playlists(cp["max_idle_days"]) if cp["max_idle_days"] > 0 else 0
                    if cut or pr:
                        msg += f". Удалено: по лимиту {cut}, по сроку {pr}"
                except Exception as e:
                    log.warning("apply current settings: %s", e)
            self._send_html(_page("Текущие плейлисты", _current_body(), flash=msg, active="current"))
            return

        if path == "/current/prune":
            tree = _STATE.get("tree")
            cfg = _cfg()
            try:
                days = int(form.get("cur_idle_days") or
                           ((cfg.get("current_playlists") or {}).get("max_idle_days")) or 0)
            except (TypeError, ValueError):
                days = 0
            n = 0
            if days > 0 and tree is not None and hasattr(tree, "prune_current_playlists"):
                n = tree.prune_current_playlists(days)
            try:
                cfg.setdefault("current_playlists", {})
                cfg["current_playlists"]["max_idle_days"] = max(0, int(days))
                _save_yaml(cfg, _config_path())
                _STATE["cfg"] = cfg
            except Exception as e:
                log.warning("save current_playlists: %s", e)
            self._send_html(_page(
                "Текущие плейлисты", _current_body(),
                flash=(f"Удалено плейлистов: {n} (срок {days} дн.)"
                       if days > 0 else "Укажите число дней больше 0"),
                active="current",
            ))
            return

        if path == "/restart":
            port = int(((_cfg().get("web") or {}).get("port")) or 8088)
            self._send_html(_page("Перезапуск", _restarting_body(port), active="status"))
            try:
                self.wfile.flush()
            except Exception:
                pass
            restart_service()
            return

        if path == "/radio/reload":
            radio = _STATE.get("radio")
            if radio:
                radio.reload()
            self._redirect("/radio")
            return

        if path == "/radio/add":
            radio = _STATE.get("radio")
            pid = (form.get("pid") or "").strip()
            name = (form.get("name") or "").strip()
            url = (form.get("url") or "").strip()
            if radio and hasattr(radio, "add_station"):
                ok, msg = radio.add_station(pid, name, url)
            else:
                ok, msg = False, "Радио не инициализировано"
            self._send_html(_page("Радио", _radio_body(view_pid=pid), flash=msg, active="radio"))
            return

        if path == "/radio/remove":
            radio = _STATE.get("radio")
            pid = (form.get("pid") or "").strip()
            try:
                idx = int(form.get("index") or -1)
            except (TypeError, ValueError):
                idx = -1
            if radio and hasattr(radio, "remove_station"):
                ok, msg = radio.remove_station(pid, idx)
            else:
                ok, msg = False, "Радио не инициализировано"
            self._send_html(_page("Радио", _radio_body(view_pid=pid), flash=msg, active="radio"))
            return

        if path == "/radio/edit":
            radio = _STATE.get("radio")
            pid = (form.get("pid") or "").strip()
            name = (form.get("name") or "").strip()
            url = (form.get("url") or "").strip()
            try:
                idx = int(form.get("index") or -1)
            except (TypeError, ValueError):
                idx = -1
            if radio and hasattr(radio, "edit_station"):
                ok, msg = radio.edit_station(pid, idx, name, url)
            else:
                ok, msg = False, "Радио не инициализировано"
            self._send_html(_page("Радио", _radio_body(view_pid=pid), flash=msg, active="radio"))
            return

        if path == "/radio/move":
            radio = _STATE.get("radio")
            pid = (form.get("pid") or "").strip()
            try:
                idx = int(form.get("index") or -1)
            except (TypeError, ValueError):
                idx = -1
            try:
                new_idx = int(form.get("new_index") or -1)
            except (TypeError, ValueError):
                new_idx = -1
            # new_index в форме — 1-based (номер, который видит пользователь)
            if new_idx >= 1:
                new_idx = new_idx - 1
            if radio and hasattr(radio, "move_station"):
                ok, msg = radio.move_station(pid, idx, new_idx)
            else:
                ok, msg = False, "Радио не инициализировано"
            self._send_html(_page("Радио", _radio_body(view_pid=pid), flash=msg, active="radio"))
            return

        if path == "/radio/create":
            radio = _STATE.get("radio")
            title = (form.get("title") or "").strip()
            if radio and hasattr(radio, "create_playlist"):
                ok, msg = radio.create_playlist(title)
            else:
                ok, msg = False, "Радио не инициализировано"
            self._send_html(_page("Радио", _radio_body(), flash=msg, active="radio"))
            return

        if path == "/radio/proxy":
            radio = _STATE.get("radio")
            view_pid = (form.get("view_pid") or "").strip()
            enabled = (form.get("radio_proxy") or "").strip() in ("1", "on", "true", "yes")
            if radio:
                radio.proxy = enabled
            cfg = _cfg()
            if "radio" not in cfg or not isinstance(cfg.get("radio"), dict):
                cfg["radio"] = {}
            cfg["radio"]["proxy"] = enabled
            if not enabled and radio is not None and getattr(radio, "popular", None) is not None:
                # без проксирования популярное радио работать не может — выключаем сами;
                # снова включить можно будет только после включения проксирования
                radio.popular_enabled = False
                cfg["radio"]["popular_enabled"] = False
            try:
                _save_yaml(cfg, _config_path())
            except Exception as e:
                log.warning("save radio proxy: %s", e)
            if enabled:
                msg = "Проксирование радио включено. «Ваше популярное радио» можно включить в своей карточке"
            else:
                msg = "Проксирование радио выключено. «Ваше популярное радио» тоже выключено"
            self._send_html(_page("Радио", _radio_body(view_pid=view_pid), flash=msg, active="radio"))
            return

        if path == "/radio/popular_enable":
            radio = _STATE.get("radio")
            view_pid = (form.get("view_pid") or "").strip()
            want = (form.get("popular_enabled") or "").strip() in ("1", "on", "true", "yes")
            if radio is None or getattr(radio, "popular", None) is None:
                msg = "Популярное радио не инициализировано"
            elif want and not bool(getattr(radio, "proxy", True)):
                msg = "Сначала включите проксирование радио — без него популярное радио не работает"
            else:
                radio.popular_enabled = want
                cfg = _cfg()
                if "radio" not in cfg or not isinstance(cfg.get("radio"), dict):
                    cfg["radio"] = {}
                cfg["radio"]["popular_enabled"] = want
                try:
                    _save_yaml(cfg, _config_path())
                except Exception as e:
                    log.warning("save popular_enabled: %s", e)
                msg = "«Ваше популярное радио» включено" if want else "«Ваше популярное радио» выключено"
            self._send_html(_page("Радио", _radio_body(view_pid=view_pid), flash=msg, active="radio"))
            return

        if path == "/radio/popular":
            radio = _STATE.get("radio")
            view_pid = (form.get("view_pid") or "").strip()

            def _pi(name, default, lo, hi):
                try:
                    v = int(form.get(name) or default)
                except (TypeError, ValueError):
                    v = default
                return max(lo, min(v, hi))

            mins = _pi("popular_min_minutes", 15, 1, 1440)
            days = _pi("popular_keep_days", 30, 1, 365)
            cfg = _cfg()
            if "radio" not in cfg or not isinstance(cfg.get("radio"), dict):
                cfg["radio"] = {}
            cfg["radio"]["popular_min_minutes"] = mins
            cfg["radio"]["popular_keep_days"] = days
            msg = f"Сохранено: от {mins} мин подряд, убирать через {days} дн."
            try:
                _save_yaml(cfg, _config_path())
            except Exception as e:
                log.warning("save radio popular: %s", e)
                msg = f"Не удалось записать config.yaml: {e}"
            pop = getattr(radio, "popular", None) if radio else None
            if pop is not None:
                pop.configure(min_minutes=mins, keep_days=days)
            self._send_html(_page("Радио", _radio_body(view_pid=view_pid), flash=msg, active="radio"))
            return

        if path == "/radio/auto_mp3":
            radio = _STATE.get("radio")
            view_pid = (form.get("view_pid") or "").strip()
            enabled = (form.get("auto_mp3_suffix") or "").strip() in ("1", "on", "true", "yes")
            if radio:
                radio.auto_mp3_suffix = enabled
            cfg = _cfg()
            if "radio" not in cfg or not isinstance(cfg.get("radio"), dict):
                cfg["radio"] = {}
            cfg["radio"]["auto_mp3_suffix"] = enabled
            try:
                _save_yaml(cfg, _config_path())
            except Exception as e:
                log.warning("save auto_mp3_suffix: %s", e)
            msg = (
                "Авто-суффикс ?.mp3 включён"
                if enabled else
                "Авто-суффикс ?.mp3 выключен"
            )
            self._send_html(_page("Радио", _radio_body(view_pid=view_pid), flash=msg, active="radio"))
            return

        if path == "/logs/clear":
            global _LOG_COUNTER
            with _LOG_LOCK:
                _LOG_BUFFER.clear()
            with _LOG_COUNTER_LOCK:
                _LOG_COUNTER = 0
            self._send_json({"ok": True})
            return

        if path == "/ssdp/announce":
            ssdp = _STATE.get("ssdp")
            if ssdp and hasattr(ssdp, "_notify"):
                for _ in range(5):
                    ssdp._notify()
                flash = "SSDP NOTIFY отправлен 5 раз"
            else:
                flash = "SSDP недоступен"
            self._send_html(_page(
                "Статус",
                _status_body() + f'<div class="flash">{html.escape(flash)}</div>',
                active="status",
            ))
            return

        if path == "/search/save":
            tree = _STATE.get("tree")
            site = (form.get("site") or "deezer").lower()
            added = None
            if tree:
                stype = form.get("type") or ""
                title = form.get("title") or ""
                query = form.get("query") or ""
                if site == "drivemusic":
                    if stype == "artist" and hasattr(tree, "add_dm_saved_artist"):
                        tree.add_dm_saved_artist(form.get("artist_id") or "", title, query)
                        added = f"Артист «{title}» → DriveMusic → 🔍 Мои поиски"
                elif site == "akniga":
                    if stype == "book" and hasattr(tree, "add_ak_saved_book"):
                        tree.add_ak_saved_book(form.get("slug") or "", title, query)
                        added = f"«{title}» → akniga.org → 🔍 Мои книги"
                elif site == "kion":
                    if stype == "artist" and hasattr(tree, "add_kion_saved_artist"):
                        tree.add_kion_saved_artist(form.get("artist_id") or "", title, query)
                        added = f"Артист «{title}» → КИОН → 🔍 Мои поиски"
                    elif stype == "album" and hasattr(tree, "add_kion_saved_album"):
                        tree.add_kion_saved_album(form.get("album_id") or "", title, query)
                        added = f"Альбом «{title}» → КИОН → 🔍 Мои поиски"
                elif site == "yandex":
                    if stype == "artist" and hasattr(tree, "add_ya_saved_artist"):
                        tree.add_ya_saved_artist(form.get("artist_id") or "", title, query)
                        added = f"Артист «{title}» → Яндекс.Музыка → 🔍 Мои поиски"
                    elif stype == "album" and hasattr(tree, "add_ya_saved_album"):
                        tree.add_ya_saved_album(form.get("album_id") or "", title, query)
                        added = f"Альбом «{title}» → Яндекс.Музыка → 🔍 Мои поиски"
                elif site == "zvuk":
                    if stype == "artist" and hasattr(tree, "add_zv_saved_artist"):
                        tree.add_zv_saved_artist(form.get("artist_id") or "", title, query)
                        added = f"Артист «{title}» → Звук → 🔍 Мои поиски"
                    elif stype == "album" and hasattr(tree, "add_zv_saved_album"):
                        tree.add_zv_saved_album(form.get("album_id") or "", title, query)
                        added = f"Альбом «{title}» → Звук → 🔍 Мои поиски"
                    elif stype == "playlist" and hasattr(tree, "add_zv_saved_playlist"):
                        tree.add_zv_saved_playlist(form.get("playlist_id") or "", title, query)
                        added = f"Плейлист «{title}» → Звук → 🔍 Мои поиски"
                elif site == "zaycev":
                    if stype == "artist" and hasattr(tree, "add_zy_saved_artist"):
                        tree.add_zy_saved_artist(form.get("artist_id") or "", title, query)
                        added = f"Артист «{title}» → Zaycev.net → 🔍 Мои поиски"
                elif site == "promodj":
                    if stype == "artist" and hasattr(tree, "add_pd_saved_artist"):
                        tree.add_pd_saved_artist(form.get("artist_id") or "", title, query)
                        added = f"Артист «{title}» → PromoDJ → 🔍 Мои поиски"
                else:
                    if stype == "artist" and hasattr(tree, "add_saved_artist"):
                        tree.add_saved_artist(form.get("artist_id") or "", title, query)
                        added = f"Артист «{title}» → Deezer → 🔍 Мои поиски"
                    elif stype == "album" and hasattr(tree, "add_saved_album"):
                        tree.add_saved_album(form.get("album_id") or "", title, query)
                        added = f"Альбом «{title}» → Deezer → 🔍 Мои поиски"
            self._send_html(_page(
                "Поиск",
                _search_body(q=form.get("query") or "", site=site),
                flash=added or "",
                active="search",
            ))
            return

        if path == "/search/favorite":
            tree = _STATE.get("tree")
            site = (form.get("site") or "akniga").lower()
            added = None
            if tree and site == "akniga":
                stype = form.get("type") or ""
                title = form.get("title") or ""
                query = form.get("query") or ""
                if stype == "book" and hasattr(tree, "add_ak_favorite"):
                    status = tree.add_ak_favorite(form.get("slug") or "", title, query)
                    if status == "ok":
                        added = f"«{title}» добавлена в избранное → akniga.org → ⭐ Избранное"
                    elif status == "exists":
                        added = f"«{title}» уже в избранном"
                    elif status == "limit":
                        added = "Вы достигли максимального лимита (500 книг в избранном)"
                    else:
                        added = "Не удалось добавить в избранное"
            self._send_html(_page(
                "Поиск",
                _search_body(q=form.get("query") or "", site=site),
                flash=added or "",
                active="search",
            ))
            return

        if path == "/search/remove":
            tree = _STATE.get("tree")
            site = (form.get("site") or "deezer").lower()
            list_kind = (form.get("list") or "").lower()
            if tree:
                if site == "drivemusic" and hasattr(tree, "remove_dm_saved"):
                    tree.remove_dm_saved(form.get("sid") or "")
                elif site == "akniga":
                    if list_kind == "favorite" and hasattr(tree, "remove_ak_favorite"):
                        tree.remove_ak_favorite(form.get("sid") or "")
                    elif hasattr(tree, "remove_ak_saved"):
                        tree.remove_ak_saved(form.get("sid") or "")
                elif site == "kion" and hasattr(tree, "remove_kion_saved"):
                    tree.remove_kion_saved(form.get("id") or "")
                elif site == "yandex" and hasattr(tree, "remove_ya_saved"):
                    tree.remove_ya_saved(form.get("sid") or "")
                elif site == "zvuk" and hasattr(tree, "remove_zv_saved"):
                    tree.remove_zv_saved(form.get("sid") or "")
                elif site == "zaycev" and hasattr(tree, "remove_zy_saved"):
                    tree.remove_zy_saved(form.get("sid") or form.get("id") or "")
                elif site == "promodj" and hasattr(tree, "remove_pd_saved"):
                    tree.remove_pd_saved(form.get("id") or form.get("sid") or "")
                elif hasattr(tree, "remove_saved"):
                    tree.remove_saved(form.get("sid") or "")
            self._send_html(_page(
                "Поиск", _search_body(q="", site=site), active="search",
            ))
            return

        if path == "/search/clear":
            tree = _STATE.get("tree")
            site = (form.get("site") or "deezer").lower()
            list_kind = (form.get("list") or "").lower()
            if tree:
                if site == "drivemusic" and hasattr(tree, "clear_dm_saved"):
                    tree.clear_dm_saved()
                elif site == "akniga":
                    if list_kind == "favorite" and hasattr(tree, "clear_ak_favorites"):
                        tree.clear_ak_favorites()
                    elif hasattr(tree, "clear_ak_saved"):
                        tree.clear_ak_saved()
                elif site == "kion" and hasattr(tree, "clear_kion_saved"):
                    tree.clear_kion_saved()
                elif site == "yandex" and hasattr(tree, "clear_ya_saved"):
                    tree.clear_ya_saved()
                elif site == "zvuk" and hasattr(tree, "clear_zv_saved"):
                    tree.clear_zv_saved()
                elif site == "zaycev" and hasattr(tree, "clear_zy_saved"):
                    tree.clear_zy_saved()
                elif hasattr(tree, "clear_saved"):
                    tree.clear_saved()
            self._send_html(_page(
                "Поиск", _search_body(q="", site=site), active="search",
            ))
            return

        if path == "/favorites/remove":
            tree = _STATE.get("tree")
            if tree and hasattr(tree, "remove_ak_favorite"):
                tree.remove_ak_favorite(form.get("sid") or "")
            self._send_html(_page(
                "Избранное",
                _favorites_body(),
                flash="Удалено из избранного",
                active="favorites",
            ))
            return

        if path == "/favorites/clear":
            tree = _STATE.get("tree")
            if tree and hasattr(tree, "clear_ak_favorites"):
                tree.clear_ak_favorites()
            self._send_html(_page(
                "Избранное",
                _favorites_body(),
                flash="Избранное очищено",
                active="favorites",
            ))
            return

        if path in ("/dmsearch/save", "/dmsearch/remove", "/dmsearch/clear"):
            self._redirect("/search?site=drivemusic")
            return

        if path == "/settings":
            cfg = _cfg()
            # network_storages: проверяем YAML ДО любых изменений конфига
            ns_items = None
            if form.get("netstorage_yaml") is not None:
                try:
                    import yaml as _yaml
                    raw_ns = form.get("netstorage_yaml") or ""
                    ns_items = _yaml.safe_load(raw_ns) if raw_ns.strip() else []
                    if isinstance(ns_items, dict):
                        ns_items = [ns_items]
                    if not isinstance(ns_items, list) or not all(isinstance(x, dict) for x in ns_items):
                        raise ValueError("ожидается список записей (- name: ...)")
                except Exception as e:
                    self._send_html(_page(
                        "Настройки", _settings_body(),
                        flash=f"Сетевые хранилища: ошибка в YAML — {e}. Настройки НЕ сохранены.",
                        active="settings",
                    ))
                    return
            for sec in ("server", "radio", "deezer", "drivemusic", "akniga",
                        "yandex", "kion", "promodj", "zaycev", "web", "files", "covers", "trace",
                        "current_playlists"):
                cfg.setdefault(sec, {})

            def _b(name: str) -> bool:
                v = form.get(name)
                return bool(v) and v not in ("0", "false", "no", "off", "")

            def _i(name: str, default: int, lo: int, hi: int) -> int:
                try:
                    v = int(form.get(name) or default)
                except (TypeError, ValueError):
                    v = default
                return max(lo, min(v, hi))

            def _f(name: str, default: float, lo: float, hi: float) -> float:
                try:
                    v = float(form.get(name) or default)
                except (TypeError, ValueError):
                    v = default
                return max(lo, min(v, hi))

            def _s(name: str, default: str = "") -> str:
                v = form.get(name)
                return (v if v is not None else default).strip()

            # --- общие ---
            lvl = _s("log_level", "info").lower()
            if lvl not in ("debug", "info", "warning", "error"):
                lvl = "info"
            cfg["log_level"] = lvl

            cfg["trace"]["enabled"] = _b("trace_enabled")
            cfg["trace"]["covers"] = _b("trace_covers")

            # --- сервер / веб ---
            cfg["server"]["name"] = _s("name", "DLNA-emu") or "DLNA-emu"
            cfg["server"]["host"] = _s("host", "0.0.0.0") or "0.0.0.0"
            cfg["server"]["port"] = _i("dlna_port", 8200, 1, 65535)
            cfg["server"]["announce_ip"] = _s("announce_ip")

            cfg["web"]["enabled"] = _b("web_enabled")
            cfg["web"]["port"] = _i("web_port", 8088, 1, 65535)

            # --- радио ---
            pass  # папка радио фиксирована (radio рядом с эмулятором)

            # --- deezer (ARL — только если непустой) ---
            from .accounts import join_tokens
            # Токены приходят через запятую («,»), поле всегда есть в форме;
            # пустое значение = аккаунтов нет (сервис выключается).
            if form.get("arl") is not None:
                cfg["deezer"]["arl"] = join_tokens(form.get("arl"))
            cfg["deezer"]["quality"] = _s("deezer_quality", "mp3_128") or "mp3_128"
            cfg["deezer"]["cache_sec"] = _i("deezer_cache_sec", 300, 30, 86400)

            # --- drivemusic ---
            cfg["drivemusic"]["enabled"] = _b("dm_enabled")
            cfg["drivemusic"]["cache_sec"] = _i("dm_cache_sec", 300, 30, 86400)
            cfg["drivemusic"]["base_url"] = (
                _s("dm_base_url", "https://ru.drivemusic.me")
                or "https://ru.drivemusic.me"
            )

            # --- akniga ---
            cfg["akniga"]["enabled"] = _b("ak_enabled")
            cfg["akniga"]["ffmpeg"] = _s("ak_ffmpeg")
            cfg["akniga"]["chapter_kbps"] = _i("ak_chapter_kbps", 128, 32, 320)
            cfg["akniga"]["section_books_per_chunk"] = _i("ak_books_per_chunk", 500, 10, 5000)
            cfg["akniga"]["cache_sec"] = _i("ak_cache_sec", 600, 30, 86400)


            # --- kion ---
            if form.get("kion_token") is not None:
                cfg.setdefault("kion", {})["token"] = join_tokens(form.get("kion_token"))
            kq = (form.get("kion_quality") or "").strip()
            if kq:
                cfg.setdefault("kion", {})["quality"] = kq

            # --- yandex (токен — только если непустой) ---
            cfg.setdefault("yandex", {})
            if form.get("yandex_token") is not None:
                cfg["yandex"]["token"] = join_tokens(form.get("yandex_token"))
            cfg["yandex"]["quality"] = _s("ya_quality", "mp3_320") or "mp3_320"
            cfg["yandex"]["stream_mode"] = _s("ya_stream_mode", "auto") or "auto"
            cfg["yandex"]["buffer_timeout"] = _f("ya_buffer_timeout", 6.0, 1.0, 60.0)
            cfg["yandex"]["cache_sec"] = _i("ya_cache_sec", 300, 30, 86400)
            cfg["server"]["format_probe_timeout"] = _f("ya_format_probe_timeout", 4.0, 0.0, 30.0)
            cfg["server"]["format_cache_sec"] = _i("ya_format_cache_sec", 1800, 60, 86400)

            cfg.setdefault("zvuk", {})
            if form.get("zvuk_token") is not None:
                cfg["zvuk"]["token"] = join_tokens(form.get("zvuk_token"))
            cfg["zvuk"]["keepalive"] = _b("zv_keepalive")
            cfg["zvuk"]["quality"] = _s("zv_quality", "high") or "high"
            cfg["zvuk"]["stream_mode"] = _s("zv_stream_mode", "auto") or "auto"
            cfg["zvuk"]["buffer_timeout"] = _f("zv_buffer_timeout", 6.0, 1.0, 60.0)
            cfg["zvuk"]["cache_sec"] = _i("zv_cache_sec", 300, 30, 86400)

            # --- promodj ---
            cfg.setdefault("promodj", {})
            cfg["promodj"]["enabled"] = _b("pd_enabled")
            cfg["promodj"]["cache_sec"] = _i("pd_cache_sec", 300, 30, 86400)
            cfg["promodj"]["tracks_per_chunk"] = _i("pd_tracks_per_chunk", 500, 50, 1000)
            cfg["promodj"]["stream_mode"] = _s("pd_stream_mode", "auto") or "auto"
            cfg["promodj"]["buffer_timeout"] = _f("pd_buffer_timeout", 6.0, 1.0, 60.0)
            cfg["promodj"]["buffer_limit_mb"] = _f("pd_buffer_limit_mb", 64.0, 4.0, 256.0)

            # --- zaycev ---
            cfg.setdefault("zaycev", {})
            cfg["zaycev"]["enabled"] = _b("zy_enabled")
            cfg["zaycev"]["cache_sec"] = _i("zy_cache_sec", 300, 30, 86400)
            cfg["zaycev"]["tracks_per_chunk"] = _i("zy_tracks_per_chunk", 100, 20, 1000)
            cfg["zaycev"]["stream_mode"] = _s("zy_stream_mode", "auto") or "auto"
            cfg["zaycev"]["buffer_timeout"] = _f("zy_buffer_timeout", 6.0, 1.0, 60.0)
            cfg["zaycev"]["buffer_limit_mb"] = _f("zy_buffer_limit_mb", 64.0, 4.0, 256.0)

            # --- files ---
            roots_raw = form.get("files_roots") or ""
            roots = [ln.strip() for ln in roots_raw.replace(",", "\n").splitlines() if ln.strip()]
            cfg["files"]["roots"] = roots
            pl_raw = form.get("files_playlists_folder") or ""
            pl_folders = [ln.strip() for ln in pl_raw.replace(",", "\n").splitlines() if ln.strip()]
            cfg["files"]["playlists_folder"] = pl_folders

            # --- network_storages ---
            if ns_items is not None:
                cfg["network_storages"] = ns_items

            # --- covers ---
            cfg["covers"]["enabled"] = _b("covers_enabled")
            cfg["covers"]["radio"] = _b("covers_radio")
            cfg["covers"]["radio_nowplaying"] = _b("covers_radio_nowplaying")
            cfg["covers"]["dlna_profile"] = _b("covers_dlna_profile")
            cfg["covers"]["max_kb"] = _i("covers_max_kb", 2048, 64, 16384)
            cfg["covers"]["max_px"] = _i("covers_max_px", 600, 0, 4096)
            cfg["covers"]["cache_items"] = _i("covers_cache_items", 48, 4, 512)

            path_cfg = _config_path()
            try:
                _save_yaml(cfg, path_cfg)
                _STATE["cfg"] = cfg

                # --- что можно применить «на лету», без перезапуска ---
                tree = _STATE.get("tree")
                if tree is not None:
                    tree.set_format_probe(
                        4.0 if cfg["server"].get("format_probe_timeout") in (None, "") else cfg["server"].get("format_probe_timeout"),
                        cfg["server"].get("format_cache_sec") or 1800,
                    )
                if tree is not None and getattr(tree, "files", None) is not None:
                    tree.files.set_playlist_folders(cfg["files"].get("playlists_folder") or [])

                ns_mgr = getattr(tree, "netstorage", None) if tree is not None else None
                if ns_mgr is not None and ns_items is not None:
                    ns_errs = ns_mgr.reload(cfg.get("network_storages") or [])
                    if ns_errs:
                        log.warning("network_storages: %s", "; ".join(ns_errs))

                radio = _STATE.get("radio")
                if radio:
                    from .radio_m3u import resolve_radio_folder
                    radio.folder = resolve_radio_folder(cfg["radio"].get("folder") or "")
                    radio.reload_on_browse = bool(cfg["radio"].get("reload_on_browse", True))
                    if "proxy" in (cfg.get("radio") or {}):
                        radio.proxy = bool(cfg["radio"].get("proxy", True))
                    if "auto_mp3_suffix" in (cfg.get("radio") or {}):
                        radio.auto_mp3_suffix = bool(cfg["radio"].get("auto_mp3_suffix", True))
                    if getattr(radio, "popular", None) is not None:
                        _pe = (cfg.get("radio") or {}).get("popular_enabled")
                        if isinstance(_pe, str):
                            _pe = _pe.lower() in ("1", "true", "yes", "on")
                        radio.popular_enabled = True if _pe is None else bool(_pe)
                        radio.popular.configure(
                            min_minutes=cfg["radio"].get("popular_min_minutes", 15),
                            keep_days=cfg["radio"].get("popular_keep_days", 30),
                        )
                    radio.reload()

                # Мультиаккаунты: применяем списки токенов без перезапуска.
                # Клиенты с прежним токеном сохраняются (сессии, кэш), новые — логинятся.
                for _svc, _sec, _key in (
                    ("deezer", "deezer", "arl"), ("yandex", "yandex", "token"),
                    ("zvuk", "zvuk", "token"), ("kion", "kion", "token"),
                ):
                    _pool = _STATE.get(_svc)
                    if _pool is not None and hasattr(_pool, "set_tokens"):
                        try:
                            _pool.set_tokens((cfg.get(_sec) or {}).get(_key))
                        except Exception as e:
                            log.warning("%s accounts apply: %s", _svc, e)
                _zk = _STATE.get("zvuk")
                if _zk is not None and hasattr(_zk, "set_keepalive"):
                    _zk.set_keepalive(bool(cfg["zvuk"]["keepalive"]))
                _kp = _STATE.get("kion")
                if _kp is not None and hasattr(_kp, "set_all") and (cfg.get("kion") or {}).get("quality"):
                    _kp.set_all(quality=str(cfg["kion"]["quality"]).lower())
                _tr = _STATE.get("tree")
                _cv = getattr(_tr, "covers_svc", None)
                if _tr is not None:
                    _tr.radio_covers = bool(cfg["covers"]["radio"])
                if _cv is not None:
                    _cv.radio_nowplaying = bool(cfg["covers"]["radio_nowplaying"])

                # живые флаги enabled у клиентов-источников
                dm_cli = _STATE.get("drivemusic")
                if dm_cli is not None:
                    dm_cli.enabled = bool(cfg["drivemusic"]["enabled"])
                ak_cli = _STATE.get("akniga")
                if ak_cli is not None:
                    ak_cli.enabled = bool(cfg["akniga"]["enabled"])
                pdj_cli = _STATE.get("promodj")
                if pdj_cli is not None:
                    pdj_cli.enabled = bool(cfg["promodj"]["enabled"])
                zy_cli = _STATE.get("zaycev")
                if zy_cli is not None:
                    zy_cli.enabled = bool(cfg["zaycev"]["enabled"])
                # Trace-флаги применяем в самом server-модуле (module-level dict)
                try:
                    from . import server as _srv
                    _srv._TRACE_CFG["enabled"] = bool(cfg["trace"]["enabled"])
                    _srv._TRACE_CFG["covers"] = bool(cfg["trace"]["covers"])
                except Exception:
                    pass

                flash = "Сохранено в " + path_cfg
                saved_ok = True
            except Exception as e:
                flash = f"Ошибка записи: {e}"
                saved_ok = False

            if saved_ok and form.get("restart"):
                self._send_html(_page(
                    "Перезапуск", _restarting_body(int(cfg["web"]["port"])),
                    active="settings",
                ))
                try:
                    self.wfile.flush()
                except Exception:
                    pass
                restart_service()
                return

            self._send_html(_page(
                "Настройки", _settings_body(), flash=flash, active="settings",
            ))
            return

        self._redirect("/")


def start_webui(host: str, port: int) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), WebUIHandler)
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True)
    t.start()
    log.info("Web UI at http://%s:%s", host if host != "0.0.0.0" else "0.0.0.0", port)
    return httpd