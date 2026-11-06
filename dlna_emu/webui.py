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
#  Шаблон (чёрно-белая тема: боковая панель + плеер-строка)
# ============================================================

_THEME_BOOT = (
    "(function(){try{var t=localStorage.getItem('dlna-theme');"
    "if(t==='light'||t==='dark')document.documentElement.setAttribute('data-theme',t);}catch(e){}})();"
)

_CSS = """
:root{
  --bg:#050505; --bg2:#0a0a0a; --card:#0e0e0e; --card2:#161616;
  --line:#222; --line2:#333; --text:#f5f5f5; --text2:#c8c8c8; --muted:#8a8a8a;
  --acc:#fff; --acc-fg:#000; --acc-soft:rgba(255,255,255,.08);
  --ok:#fff; --ok-bg:#1a1a1a; --err:#fff; --err-bg:#262626; --warn:#bdbdbd;
  --shadow:0 1px 0 rgba(255,255,255,.04) inset,0 10px 30px rgba(0,0,0,.45);
  --side:252px; --player:0px;
  --sans:"Inter",ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  --mono:ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,monospace;
  color-scheme:dark;
}
:root[data-theme=light]{
  --bg:#f4f4f4; --bg2:#fff; --card:#fff; --card2:#efefef;
  --line:#e3e3e3; --line2:#cfcfcf; --text:#0a0a0a; --text2:#333; --muted:#6c6c6c;
  --acc:#000; --acc-fg:#fff; --acc-soft:rgba(0,0,0,.06);
  --ok:#000; --ok-bg:#ececec; --err:#000; --err-bg:#e2e2e2; --warn:#555;
  --shadow:0 1px 2px rgba(0,0,0,.06),0 10px 30px rgba(0,0,0,.06);
  color-scheme:light;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;font-family:var(--sans);background:var(--bg);color:var(--text);line-height:1.5;font-size:14px;
  -webkit-font-smoothing:antialiased;letter-spacing:-.003em}
::selection{background:var(--acc);color:var(--acc-fg)}
a{color:var(--text);text-decoration-color:var(--line2);text-underline-offset:3px}
a:hover{text-decoration-color:var(--text)}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:var(--line2);border-radius:10px;border:2px solid var(--bg)}
::-webkit-scrollbar-thumb:hover{background:var(--muted)}
::-webkit-scrollbar-track{background:transparent}

/* ---------- каркас ---------- */
.side{position:fixed;inset:0 auto 0 0;width:var(--side);background:var(--bg);border-right:1px solid var(--line);
  display:flex;flex-direction:column;padding:18px 14px 14px;z-index:30;overflow-y:auto;transition:transform .25s ease}
.brand{display:flex;align-items:center;gap:12px;padding:4px 8px 22px}
.mark{width:38px;height:38px;border-radius:11px;background:var(--acc);color:var(--acc-fg);display:grid;place-items:center;flex:none}
.mark svg{width:22px;height:22px}
.bname{font-weight:700;font-size:17px;letter-spacing:-.02em;line-height:1.1}
.bsub{font-family:var(--mono);font-size:10px;letter-spacing:.14em;color:var(--muted);margin-top:3px;text-transform:uppercase}
.navgrp{font-family:var(--mono);font-size:10px;letter-spacing:.16em;text-transform:uppercase;color:var(--muted);padding:16px 12px 6px}
.side a.nav{display:flex;align-items:center;gap:12px;padding:9px 12px;border-radius:10px;color:var(--muted);
  text-decoration:none;font-size:14px;font-weight:500;position:relative;transition:background .15s,color .15s}
.side a.nav svg{width:18px;height:18px;flex:none;fill:none;stroke:currentColor;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round}
.side a.nav:hover{color:var(--text);background:var(--acc-soft)}
.side a.nav.active{color:var(--text);background:var(--card2);box-shadow:inset 0 0 0 1px var(--line2)}
.side a.nav.active::before{content:"";position:absolute;left:-14px;top:9px;bottom:9px;width:3px;border-radius:0 3px 3px 0;background:var(--acc)}
.side-foot{margin-top:auto;padding-top:14px;display:flex;flex-direction:column;gap:10px}
.srv{display:flex;align-items:center;gap:10px;padding:11px 12px;border:1px solid var(--line);border-radius:12px;background:var(--card)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--acc);box-shadow:0 0 0 3px var(--acc-soft);flex:none;animation:pulse 2.4s infinite}
@keyframes pulse{0%,100%{box-shadow:0 0 0 3px var(--acc-soft)}50%{box-shadow:0 0 0 6px transparent}}
.srv b{display:block;font-size:12.5px;font-weight:600}
.srv span{font-family:var(--mono);font-size:11px;color:var(--muted)}
.tgl{display:flex;gap:6px}
.tgl button{flex:1;background:transparent;border:1px solid var(--line);color:var(--muted);border-radius:9px;padding:8px;font-size:12px;cursor:pointer;
  display:flex;align-items:center;justify-content:center;gap:6px;font-family:inherit}
.tgl button:hover{color:var(--text);border-color:var(--line2)}
.tgl svg{width:15px;height:15px;fill:none;stroke:currentColor;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round}
.wrap{margin-left:var(--side);min-height:100vh}
.topbar{display:none;position:sticky;top:0;z-index:20;background:color-mix(in srgb,var(--bg) 88%,transparent);backdrop-filter:blur(10px);
  border-bottom:1px solid var(--line);padding:10px 14px;align-items:center;gap:12px}
.burger{background:transparent;border:1px solid var(--line);color:var(--text);border-radius:10px;width:38px;height:38px;padding:0;display:grid;place-items:center;cursor:pointer}
.burger svg{width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round}
.topbar .t{font-weight:600}
.scrim{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:25}
main{max-width:1060px;margin:0 auto;padding:34px 34px calc(var(--player) + 48px)}
.pagehead{margin:0 0 26px}
.eyebrow{font-family:var(--mono);font-size:11px;letter-spacing:.18em;text-transform:uppercase;color:var(--muted);margin-bottom:8px}
.pagehead h1{margin:0;font-size:40px;line-height:1.05;font-weight:700;letter-spacing:-.035em}

/* ---------- плеер-строка ---------- */
.player{position:fixed;left:var(--side);right:0;bottom:0;height:var(--player);z-index:20;display:flex;align-items:center;gap:16px;
  padding:0 26px;background:color-mix(in srgb,var(--bg) 90%,transparent);backdrop-filter:blur(14px);border-top:1px solid var(--line)}
.np-tile{width:48px;height:48px;border-radius:10px;background:var(--card2);border:1px solid var(--line2);display:grid;place-items:center;
  font-weight:700;font-size:18px;flex:none;text-transform:uppercase}
.np-main{min-width:0;flex:1}
.np-title{font-weight:600;font-size:14.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.np-sub{font-family:var(--mono);font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px}
.eq{display:flex;align-items:flex-end;gap:3px;height:22px;flex:none}
.eq i{display:block;width:3px;background:var(--acc);border-radius:2px;height:30%;animation:eq 1s ease-in-out infinite}
.eq i:nth-child(2){animation-delay:.18s}.eq i:nth-child(3){animation-delay:.36s}.eq i:nth-child(4){animation-delay:.1s}.eq i:nth-child(5){animation-delay:.28s}
@keyframes eq{0%,100%{height:25%}50%{height:100%}}
.player.idle .eq i{animation:none;height:18%;background:var(--line2)}
.player.idle .np-title{color:var(--muted);font-weight:500}

/* спойлеры настроек — не вылезают за экран */
details.settings-spoiler{
  background:var(--card);border:1px solid var(--line);border-radius:16px;
  margin-bottom:16px;box-shadow:var(--shadow);overflow:hidden;max-width:100%;
}
details.settings-spoiler>summary{
  cursor:pointer;list-style:none;padding:16px 22px;user-select:none;
  font-size:12px;font-weight:600;font-family:var(--mono);letter-spacing:.16em;
  text-transform:uppercase;color:var(--muted);display:flex;align-items:center;gap:10px;
}
details.settings-spoiler>summary::-webkit-details-marker{display:none}
details.settings-spoiler>summary::before{
  content:"";width:0;height:0;border-left:5px solid var(--acc);
  border-top:4px solid transparent;border-bottom:4px solid transparent;
  transition:transform .15s;flex:none;
}
details.settings-spoiler[open]>summary::before{transform:rotate(90deg)}
details.settings-spoiler[open]>summary{border-bottom:1px solid var(--line);color:var(--text)}
details.settings-spoiler .settings-body{padding:8px 22px 20px;max-width:100%;box-sizing:border-box}
details.settings-spoiler .settings-body label{margin-top:12px}
.dev-row{display:flex;gap:12px;align-items:center;padding:10px 0;border-bottom:1px solid var(--line)}
.dev-row:last-child{border-bottom:none}
.dev-art{width:56px;height:56px;border-radius:10px;object-fit:cover;background:var(--card2);border:1px solid var(--line2);flex:none}
.dev-art-ph{width:56px;height:56px;border-radius:10px;background:var(--card2);border:1px solid var(--line2);
  display:grid;place-items:center;color:var(--muted);font-size:20px;flex:none}
.np-tile.has-cover{background-size:cover;background-position:center;color:transparent}

.np-count{font-family:var(--mono);font-size:11px;letter-spacing:.08em;color:var(--muted);border:1px solid var(--line);border-radius:999px;padding:5px 11px;white-space:nowrap;text-decoration:none}
.np-count:hover{color:var(--text);border-color:var(--line2)}

/* ---------- карточки и формы ---------- */
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:20px 22px;margin-bottom:16px;box-shadow:var(--shadow);transition:border-color .2s}
.card:hover{border-color:var(--line2)}
.card h2{margin:0 0 14px;font-size:12px;font-weight:600;font-family:var(--mono);letter-spacing:.16em;text-transform:uppercase;color:var(--muted)}
.card h3{font-size:16px;letter-spacing:-.01em}
label{display:block;margin:14px 0 6px;color:var(--muted);font-size:12.5px;font-weight:500}
input,select,textarea{width:100%;padding:11px 13px;border-radius:10px;border:1px solid var(--line2);background:var(--bg2);color:var(--text);
  font-size:14px;font-family:inherit;transition:border-color .15s,box-shadow .15s}
input::placeholder,textarea::placeholder{color:var(--muted);opacity:.7}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--text);box-shadow:0 0 0 3px var(--acc-soft)}
input[type=checkbox],input[type=radio]{width:auto;accent-color:var(--acc)}
textarea{min-height:76px;font-family:var(--mono);font-size:12px}
.row{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px;align-items:center}
button,.btn{appearance:none;border:1px solid var(--acc);border-radius:10px;padding:10px 18px;font-size:13.5px;font-weight:600;cursor:pointer;
  background:var(--acc);color:var(--acc-fg);text-decoration:none;display:inline-block;font-family:inherit;transition:transform .08s,opacity .15s,background .15s}
button:hover,.btn:hover{opacity:.88}
button:active,.btn:active{transform:translateY(1px)}
button:disabled{opacity:.35;cursor:not-allowed}
button.ghost,.btn.ghost{background:transparent;color:var(--text);border-color:var(--line2)}
button.ghost:hover,.btn.ghost:hover{background:var(--acc-soft);opacity:1;border-color:var(--muted)}
button.small,.btn.small{padding:6px 12px;font-size:12.5px;border-radius:8px}
.muted{color:var(--muted);font-size:13px}
.badge{white-space:nowrap;flex:none;display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:999px;font-size:11.5px;font-weight:600;font-family:var(--mono);letter-spacing:.04em}
.badge.ok{background:var(--acc);color:var(--acc-fg)}
.badge.off{background:transparent;color:var(--muted);border:1px dashed var(--line2)}
.flash{background:var(--ok-bg);color:var(--text);border:1px solid var(--line2);border-left:3px solid var(--acc);padding:12px 16px;border-radius:12px;margin-bottom:18px}
.flash.err{background:var(--err-bg);border-left-style:double;border-left-width:5px}
.flash.err::before{content:"! ";font-weight:800}
table{width:100%;border-collapse:collapse;font-size:13px}
th{font-family:var(--mono);font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);font-weight:500}
td,th{text-align:left;padding:10px 8px;border-bottom:1px solid var(--line)}
tr:last-child td{border-bottom:0}
/* списки плейлистов: путь к файлу не раздувает строку, на телефоне — карточками */
table.pl-table td.pl-path code{display:block;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
@media (max-width:700px){
  table.pl-table,table.pl-table tbody{display:block}
  table.pl-table tr.pl-head{display:none}
  table.pl-table tr{display:block;padding:12px 0;border-bottom:1px solid var(--line)}
  table.pl-table tr:last-child{border-bottom:0}
  table.pl-table td{display:block;padding:2px 0;border:0}
  table.pl-table td.pl-name{font-size:15px;overflow-wrap:anywhere}
  table.pl-table td.pl-count{color:var(--muted)}
  table.pl-table td.pl-count::after{content:" " attr(data-unit)}
  table.pl-table td.pl-path code{max-width:100%;font-size:11px}
  table.pl-table td.pl-act{padding-top:8px;white-space:normal !important}
}
code{background:var(--card2);border:1px solid var(--line);padding:1px 7px;border-radius:6px;font-size:12px;font-family:var(--mono)}
hr{border:0;border-top:1px solid var(--line);margin:18px 0}
details>summary{cursor:pointer}
.logs{background:var(--bg2);font-family:var(--mono);font-size:12px;line-height:1.55;padding:14px;border-radius:12px;height:62vh;overflow-y:auto;
  white-space:pre-wrap;word-break:break-word;color:var(--text2);border:1px solid var(--line)}
.logs .lvl-ERROR,.logs .lvl-STDERR{color:var(--acc-fg);background:var(--acc);font-weight:700;padding:0 4px;border-radius:3px}
.logs .lvl-WARNING{color:var(--text);font-weight:600;text-decoration:underline;text-decoration-style:dotted}
.logs .lvl-INFO{color:var(--text2)}
.logs .lvl-DEBUG{color:var(--muted)}
.logs .lvl-STDOUT{color:var(--text2)}
.logs .ts{color:var(--muted);margin-right:6px}
.logs .name{color:var(--text);opacity:.65}
.logs-toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
.search-card{background:var(--bg2);border:1px solid var(--line);border-radius:12px;padding:13px 16px;margin-bottom:10px;display:flex;gap:14px;align-items:center;flex-wrap:wrap;transition:border-color .15s,background .15s}
.search-card:hover{border-color:var(--line2);background:var(--card2)}
.search-card .name{font-weight:600;color:var(--text)}
.search-card .desc{color:var(--muted);font-size:13px}
.search-card .actions{margin-left:auto;display:flex;gap:8px}
img{border-radius:6px}
img:not(.dev-art){filter:grayscale(1)}
/* интерфейс монохромный, но карточка «Устройства онлайн» показывает цветные обложки */
main>*:not(.keep-color){filter:grayscale(1)}
.keep-color h2,.keep-color p,.keep-color .dev-meta,.keep-color .dev-title,.keep-color .dev-art-ph{filter:grayscale(1)}
.keep-color .dev-art{filter:none}
.dev-art-wrap{display:block;flex:none;line-height:0}
.icon-btn{display:inline-grid;place-items:center;width:28px;height:28px;padding:0;margin-left:6px;vertical-align:middle;border-radius:50%;
  background:transparent;border:1px solid var(--line2);color:var(--muted);cursor:pointer}
.icon-btn:hover{color:var(--text);border-color:var(--text)}
.icon-btn svg{width:15px;height:15px;fill:none;stroke:currentColor;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round}
.icon-btn.spin svg{animation:rot .8s linear infinite}
@keyframes rot{to{transform:rotate(360deg)}}
a.stat-link{display:block;text-decoration:none;color:inherit}
a.stat-link:hover{border-color:var(--text)}
.stat form.inl{display:inline}
.ex-row{display:flex;align-items:center;gap:10px;padding:7px 4px;border-bottom:1px solid var(--line)}
.ex-row:last-child{border-bottom:0}
.ex-row a{color:var(--text);text-decoration:none}
.ex-row a:hover{text-decoration:underline}
.ex-sel{display:flex;gap:8px;align-items:center;justify-content:space-between;padding:5px 0;border-bottom:1px solid var(--line);font-size:13px;overflow-wrap:anywhere}
/* статистика */
.bar{height:8px;background:var(--card2);border:1px solid var(--line);border-radius:6px;overflow:hidden}
.bar>i{display:block;height:100%;background:var(--acc)}
.ybars{display:grid;grid-template-columns:repeat(12,1fr);gap:8px;align-items:end}
.ybar{display:flex;flex-direction:column;align-items:center;gap:6px;min-width:0}
.ycol{width:100%;height:110px;display:flex;align-items:flex-end;background:var(--bg2);border:1px solid var(--line);border-radius:8px;overflow:hidden}
.ycol>i{display:block;width:100%;background:var(--acc);min-height:2px}
.ybar span{font-family:var(--mono);font-size:10.5px;color:var(--muted)}
.st-table td:nth-child(2){overflow-wrap:anywhere}
.rh-table td{vertical-align:middle}
/* топ треков на телефоне: карточкой — название в строку, ниже площадка · прослушивания · время */
@media (max-width:700px){
  table.tt-table,table.tt-table tbody{display:block}
  table.tt-table tr.tt-head{display:none}
  table.tt-table tr{display:flex;flex-wrap:wrap;align-items:baseline;gap:2px 12px;padding:10px 0;border-bottom:1px solid var(--line)}
  table.tt-table tr:last-child{border-bottom:0}
  table.tt-table td{display:block;padding:0;border:0}
  table.tt-table td.tt-n{flex:0 0 1.8em;color:var(--muted);font-family:var(--mono);font-size:12px}
  table.tt-table td.tt-t{flex:1 1 calc(100% - 2.6em);min-width:0;overflow-wrap:break-word;word-break:normal}
  table.tt-table td.tt-s,table.tt-table td.tt-p,table.tt-table td.tt-d{font-size:12px;color:var(--muted)}
  table.tt-table td.tt-s{margin-left:calc(1.8em + 12px)}
  table.tt-table td.tt-p::after{content:" " attr(data-unit)}
}
@media (max-width:700px){.ybars{gap:4px}.ybar span{font-size:9px}.stats{grid-template-columns:1fr 1fr}}


/* ---------- статус ---------- */
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:16px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px 18px;min-width:0;transition:border-color .2s}
.stat:hover{border-color:var(--line2)}
.stat-k{font-family:var(--mono);font-size:10.5px;letter-spacing:.16em;text-transform:uppercase;color:var(--muted)}
.stat-v{font-size:26px;font-weight:700;letter-spacing:-.03em;margin:8px 0 4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.stat-s{font-size:12.5px;color:var(--muted)}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:12px}
.tile{position:relative;background:var(--bg2);border:1px solid var(--line);border-radius:14px;padding:15px 16px 14px;min-height:112px;transition:border-color .15s,transform .15s,background .15s}
.tile:hover{border-color:var(--text);background:var(--card2);transform:translateY(-2px)}
.tile-top{display:flex;justify-content:space-between;align-items:center;gap:8px}
.tile-name{font-weight:600;font-size:15px;letter-spacing:-.01em}
.tile-meta{margin-top:12px;font-size:12.5px;color:var(--muted);line-height:1.55}
.tile-meta b{color:var(--text)}
.tile-meta code{display:block;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-top:4px;direction:rtl;text-align:left}
.tile-name{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tile-go{position:absolute;right:14px;bottom:12px;text-decoration:none;font-size:18px;color:var(--muted);transition:transform .15s,color .15s}
.tile:hover .tile-go{color:var(--text);transform:translateX(3px)}
@media (max-width:900px){.stats{grid-template-columns:repeat(2,1fr)}.stat-v{font-size:21px}}

/* ---------- адаптив ---------- */
@media (max-width:900px){
  .side{transform:translateX(-100%);box-shadow:var(--shadow)}
  body.nav-open .side{transform:none}
  body.nav-open .scrim{display:block}
  .wrap{margin-left:0}
  .topbar{display:flex}
  .player{left:0;padding:0 14px;gap:12px}
  main{padding:22px 14px calc(var(--player) + 36px)}
  .pagehead h1{font-size:30px}
  .np-count{display:none}
}
"""

_ICONS = {
    "status": '<path d="M3 11l9-8 9 8"/><path d="M5 10v10h14V10"/>',
    "current": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "radio": '<circle cx="12" cy="12" r="2"/><path d="M16.2 7.8a6 6 0 010 8.4M7.8 16.2a6 6 0 010-8.4M19 5a10 10 0 010 14M5 19A10 10 0 015 5"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>',
    "favorites": '<path d="M12 3l2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1L3.2 9.5l6.1-.9z"/>',
    "settings": '<path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>',
    "logs": '<path d="M4 17l6-5-6-5M12 19h8"/>',
    "usb": '<path d="M9 18V6l10-2v12"/><circle cx="6" cy="18" r="3"/><circle cx="16" cy="16" r="3"/>',
    "stats": '<path d=\"M4 20V10M10 20V4M16 20v-8M22 20H2\"/>',
    "radiohist": '<path d=\"M3 12a9 9 0 109-9 9 9 0 00-6.4 2.6L3 8\"/><path d=\"M3 3v5h5M12 7v5l3 2\"/>',
}

_NAV = [
    ("Обзор", [("status", "/", "Статус")]),
    ("Музыка", [
        ("search", "/search", "Поиск"),
        ("radio", "/radio", "Радио"),
        ("radiohist", "/radio-history", "История эфира"),
        ("stats", "/stats", "Статистика"),
        ("usb", "/usb", "Плейлисты USB"),
        ("favorites", "/favorites", "Избранное"),
        ("current", "/current", "Текущие плейлисты"),
    ]),
    ("Система", [("settings", "/settings", "Настройки"), ("logs", "/logs", "Консоль")]),
]

_EYEBROW = {
    "status": "Обзор", "settings": "Конфигурация", "radio": "Источники",
    "stats": "Музыка", "radiohist": "Музыка", "usb": "Источники", "search": "Источники",
    "favorites": "Библиотека", "current": "Библиотека", "logs": "Система",
}

_PAGE_JS = """
(function(){
  var b=document.body;
  function $(i){return document.getElementById(i);}
  var burger=$('burger'),scrim=$('scrim');
  if(burger)burger.onclick=function(){b.classList.toggle('nav-open');};
  if(scrim)scrim.onclick=function(){b.classList.remove('nav-open');};
  function theme(t){
    document.documentElement.setAttribute('data-theme',t);
    try{localStorage.setItem('dlna-theme',t);}catch(e){}
  }
  var d=$('th-dark'),l=$('th-light');
  if(d)d.onclick=function(){theme('dark');};
  if(l)l.onclick=function(){theme('light');};
})();
"""


def _svg(name: str) -> str:
    return (
        '<svg viewBox="0 0 24 24" aria-hidden="true">' + _ICONS.get(name, "") + "</svg>"
    )


def _page(title: str, body: str, flash: str = "", active: str = "") -> bytes:
    if flash:
        is_err = "лимит" in flash.lower() or "ошибка" in flash.lower() or "не удалось" in flash.lower()
        cls = "flash err" if is_err else "flash"
        flash_html = f'<div class="{cls}">{html.escape(flash)}</div>'
    else:
        flash_html = ""

    nav_html = []
    for grp, items in _NAV:
        nav_html.append(f'<div class="navgrp">{html.escape(grp)}</div>')
        for key, href, label in items:
            on = " active" if active == key else ""
            nav_html.append(f'<a class="nav{on}" href="{href}">{_svg(key)}<span>{html.escape(label)}</span></a>')
    nav_s = "".join(nav_html)

    try:
        cfg = _cfg()
        srv_cfg = cfg.get("server") or {}
        announce = _STATE.get("announce_ip") or srv_cfg.get("announce_ip") or ""
        dlna_port = srv_cfg.get("port") or 8200
        srv_name = srv_cfg.get("name") or "DLNA-emu"
    except Exception:
        announce, dlna_port, srv_name = "", 8200, "DLNA-emu"
    srv_addr = f"{announce}:{dlna_port}" if announce else f":{dlna_port}"
    eyebrow = _EYEBROW.get(active, "DLNA-emu")

    doc = f"""<!DOCTYPE html>
<html lang="ru" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark light">
<title>{html.escape(title)} — DLNA-emu</title>
<script>{_THEME_BOOT}</script>
<style>{_CSS}</style>
</head>
<body>
<aside class="side" id="side">
  <div class="brand">
    <div class="mark"><svg viewBox="0 0 24 24" fill="currentColor"><rect x="3" y="10" width="3.2" height="4" rx="1.6"/><rect x="8.2" y="6" width="3.2" height="12" rx="1.6"/><rect x="13.4" y="3" width="3.2" height="18" rx="1.6"/><rect x="18.6" y="8" width="2.4" height="8" rx="1.2"/></svg></div>
    <div><div class="bname">DLNA·emu</div><div class="bsub">media server</div></div>
  </div>
  <nav>{nav_s}</nav>
  <div class="side-foot">
    <div class="srv"><i class="dot"></i><div><b>{html.escape(str(srv_name))}</b><span>{html.escape(srv_addr)}</span></div></div>
    <div class="tgl">
      <button type="button" class="ghost" id="th-dark" title="Тёмная тема"><svg viewBox="0 0 24 24"><path d="M20 14.5A8 8 0 019.5 4a8 8 0 1010.5 10.5z"/></svg>Тёмная</button>
      <button type="button" class="ghost" id="th-light" title="Светлая тема"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>Светлая</button>
    </div>
  </div>
</aside>
<div class="scrim" id="scrim"></div>
<div class="wrap">
  <header class="topbar">
    <button type="button" class="burger" id="burger" aria-label="Меню"><svg viewBox="0 0 24 24"><path d="M4 7h16M4 12h16M4 17h16"/></svg></button>
    <span class="t">{html.escape(title)}</span>
  </header>
  <main>
    <div class="pagehead"><div class="eyebrow">{html.escape(eyebrow)}</div><h1>{html.escape(title)}</h1></div>
    {flash_html}
    {body}
  </main>
</div>
<script>{_PAGE_JS}</script>
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

def _dlna_cover_url(rel: str) -> str:
    """Префикс обложки хостом DLNA-порта (веб и DLNA могут быть на разных портах)."""
    if not rel:
        return ""
    if rel.startswith("http://") or rel.startswith("https://"):
        return rel
    try:
        from .server import detect_lan_ip
        cfg = _cfg()
        srv = cfg.get("server") or {}
        host = detect_lan_ip(srv.get("announce_ip"))
        port = int(srv.get("port") or 8200)
        if not rel.startswith("/"):
            rel = "/" + rel
        return f"http://{host}:{port}{rel}"
    except Exception:
        return rel


# ============================================================
#  Статистика, история эфира радио, «живые» устройства онлайн
# ============================================================

_MONTHS_RU = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
              "сентября", "октября", "ноября", "декабря"]
_MONTHS_NOM = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август",
               "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
_MONTHS_SHORT = ["Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]

_SVC_LABELS = {
    "usb": "USB / файлы", "file": "USB / файлы", "local": "Локальные файлы",
    "net": "Сетевое хранилище", "yandex": "Яндекс.Музыка", "kion": "КИОН Музыка",
    "zvuk": "Звук", "deezer": "Deezer", "radio": "Радио", "drivemusic": "DriveMusic",
    "promodj": "PromoDJ", "zaycev": "Zaycev.net", "akniga": "akniga.org",
}


def _svc_label(svc: str) -> str:
    return _SVC_LABELS.get(svc, svc or "?")


def _fmt_dur(sec) -> str:
    """12345 → «3 ч 25 мин»; меньше часа → «42 мин»; меньше минуты → «35 с»."""
    sec = int(sec or 0)
    if sec < 60:
        return f"{sec} с"
    m = sec // 60
    if m < 60:
        return f"{m} мин"
    h, m = divmod(m, 60)
    if h < 100:
        return f"{h} ч {m:02d} мин"
    return f"{h} ч"


def _bar(pct: float) -> str:
    pct = max(0.0, min(100.0, pct))
    return f'<div class="bar"><i style="width:{pct:.1f}%"></i></div>'


def _stats_settings() -> dict:
    from . import stats as _st
    return _st.clean_settings(_cfg().get("stats"))


def _stats_body(year: int = 0, flash_extra: str = "", month: str = "") -> str:
    from . import stats as _st
    S = _st.get_stats()
    if S is None:
        return '<div class="card"><p class="muted">Статистика не инициализирована.</p></div>'
    cfg_s = _stats_settings()
    if month and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        month = ""
    d = S.summary(top_artists=cfg_s["top_artists"], top_tracks=cfg_s["top_tracks"], year=year or None, month=month)
    total = d["total"] or 1
    esc = html.escape

    def _month_label(m: str) -> str:
        return f"{_MONTHS_NOM[int(m[5:7]) - 1]} {m[:4]}"

    # --- выбор месяца ---
    months_avail = S.available_months()
    cur_m = time.strftime("%Y-%m")
    mopts = "".join(
        f'<option value="{m}"{" selected" if m == month else ""}>{esc(_month_label(m))}</option>'
        for m in months_avail
    )
    ly, lm = time.localtime().tm_year, time.localtime().tm_mon - 1
    if lm == 0:
        ly, lm = ly - 1, 12
    prev_m = f"{ly}-{lm:02d}"
    picker = f"""
<div class="card" style="padding:14px 22px">
  <form method="GET" action="/stats" style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:0">
    <label style="margin:0">Период</label>
    <select name="month" style="max-width:220px" onchange="this.form.submit()">
      <option value=""{"" if month else " selected"}>За всё время</option>{mopts}
    </select>
    <a class="btn small ghost" href="/stats?month={prev_m}">Прошлый месяц</a>
    {'<a class="btn small ghost" href="/stats">← За всё время</a>' if month else ''}
  </form>
  <p class="muted" style="font-size:12px;margin:8px 0 0">Прошлые месяцы хранятся сжатыми (gzip); при просмотре они распаковываются в памяти и сразу забываются.</p>
</div>"""
    disabled_note = ("" if cfg_s["enabled"] else
                     '<div class="flash err">Сбор статистики выключен — новые прослушивания не записываются '
                     '(включить можно внизу страницы).</div>')

    # --- верхние карточки ---
    ts = d["top_service"]
    top_svc = _svc_label(ts["svc"]) if ts else "—"
    top_svc_s = f"{_fmt_dur(ts['sec'])} · {ts['plays']} прослуш." if ts else "пока нет данных"
    head_month = f'<h2 style="font-size:20px;margin:0 0 12px;text-transform:none;letter-spacing:0;color:var(--text)">{esc(_month_label(month))}</h2>' if month else ""
    cards = f"""{head_month}
<div class="stats">
  <div class="stat"><div class="stat-k">Всего слушал</div><div class="stat-v">{esc(_fmt_dur(d['total']))}</div><div class="stat-s">{d['plays']} прослушиваний</div></div>
  <div class="stat"><div class="stat-k">Музыка</div><div class="stat-v">{esc(_fmt_dur(d['music']))}</div><div class="stat-s">треки с USB и стримингов</div></div>
  <div class="stat"><div class="stat-k">Радио</div><div class="stat-v">{esc(_fmt_dur(d['radio']))}</div><div class="stat-s">интернет-потоки</div></div>
  <div class="stat"><div class="stat-k">Любимая площадка</div><div class="stat-v">{esc(top_svc)}</div><div class="stat-s">{esc(top_svc_s)}</div></div>
</div>"""
    if d["books"]:
        cards += f'<p class="muted" style="margin:-6px 0 14px">Аудиокниги: {esc(_fmt_dur(d["books"]))}</p>'

    # --- площадки ---
    rows = []
    for r in d["services"]:
        share = r["sec"] * 100.0 / total
        rows.append(
            f"<tr><td>{esc(_svc_label(r['svc']))}</td><td>{esc(_fmt_dur(r['sec']))}</td>"
            f"<td>{r['plays']}</td><td style='width:34%'>{_bar(share)}</td><td>{share:.0f}%</td></tr>"
        )
    svc_card = (
        '<div class="card"><h2>Площадки</h2>'
        + ("<table><tr><th>Площадка</th><th>Время</th><th>Прослуш.</th><th></th><th></th></tr>"
           + "".join(rows) + "</table>" if rows else '<p class="muted">Пока нет данных — включите музыку на ресивере.</p>')
        + "</div>"
    )

    # --- топ исполнителей ---
    arow = []
    for i, a in enumerate(d["artists"], 1):
        arow.append(f"<tr><td>{i}</td><td><strong>{esc(a['name'])}</strong></td><td>{a['plays']}</td><td>{esc(_fmt_dur(a['sec']))}</td></tr>")
    art_card = (
        f'<div class="card"><h2>Топ исполнителей ({cfg_s["top_artists"]})</h2>'
        + ("<table class='st-table'><tr><th>#</th><th>Исполнитель</th><th>Прослуш.</th><th>Время</th></tr>"
           + "".join(arow) + "</table>" if arow else '<p class="muted">Исполнители появятся после первых прослушиваний.</p>')
        + "</div>"
    )

    # --- топ треков ---
    trow = []
    for i, t in enumerate(d["tracks"], 1):
        who = f"<div class='muted' style='font-size:12px'>{esc(t['artist'])}</div>" if t.get("artist") else ""
        trow.append(
            f"<tr><td class='tt-n'>{i}</td><td class='tt-t'><strong>{esc(t['title'])}</strong>{who}</td>"
            f"<td class='tt-s'>{esc(_svc_label(t['svc']))}</td><td class='tt-p' data-unit='прослуш.'>{t['plays']}</td>"
            f"<td class='tt-d'>{esc(_fmt_dur(t['sec']))}</td></tr>"
        )
    trk_card = (
        f'<div class="card"><h2>Топ треков ({cfg_s["top_tracks"]})</h2>'
        + ("<table class='st-table tt-table'><tr class='tt-head'><th>#</th><th>Трек</th><th>Площадка</th><th>Прослуш.</th><th>Время</th></tr>"
           + "".join(trow) + "</table>" if trow else '<p class="muted">Треки появятся после первых прослушиваний.</p>')
        + "</div>"
    )

    # --- по месяцам ---
    mx = max([m["sec"] for m in d["months"]] or [1]) or 1
    mrow = []
    for m in d["months"]:
        y, mo = m["month"].split("-")
        label = f"{_MONTHS_NOM[int(mo) - 1]} {y}"
        mrow.append(
            f"<tr><td><a href='/stats?month={m['month']}'>{esc(label)}</a></td><td>{esc(_fmt_dur(m['sec']))}</td><td>{m['plays']}</td>"
            f"<td>{esc(_svc_label(m['top_svc']))}</td><td style='width:28%'>{_bar(m['sec'] * 100.0 / mx)}</td></tr>"
        )
    month_card = (
        '<div class="card"><h2>По месяцам</h2>'
        + ("<table class='st-table'><tr><th>Месяц</th><th>Время</th><th>Прослуш.</th><th>Площадка №1</th><th></th></tr>"
           + "".join(mrow) + "</table>" if mrow else '<p class="muted">Пока нет данных.</p>')
        + "</div>"
    )

    # --- за год ---
    y = d["year"]
    ys = d["year_sum"]
    ymax = max([m["sec"] for m in ys["months"]] or [1]) or 1
    year_opts = "".join(f'<option value="{yy}"{" selected" if yy == y else ""}>{yy}</option>' for yy in d["years"])
    bars = "".join(
        f'<div class="ybar"><div class="ycol"><i style="height:{m["sec"] * 100.0 / ymax:.0f}%" title="{esc(_fmt_dur(m["sec"]))}"></i></div>'
        f'<span>{_MONTHS_SHORT[i]}</span></div>'
        for i, m in enumerate(ys["months"])
    )
    ta, tt = ys["top_artist"], ys["top_track"]
    year_lines = [
        f"<li>Всего: <b>{esc(_fmt_dur(ys['sec']))}</b>, прослушиваний: <b>{ys['plays']}</b></li>",
        f"<li>Главная площадка: <b>{esc(_svc_label(ys['top_service']) if ys['top_service'] else '—')}</b></li>",
    ]
    if ta:
        year_lines.append(f"<li>Исполнитель года: <b>{esc(ta['name'])}</b> ({ta['plays']} прослуш.)</li>")
    if tt:
        who = f"{tt['artist']} — " if tt.get("artist") else ""
        year_lines.append(f"<li>Трек года: <b>{esc(who + tt['title'])}</b> ({tt['plays']} прослуш.)</li>")
    year_card = f"""
<div class="card"><h2>Статистика за год</h2>
  <form method="GET" action="/stats" style="display:flex;gap:8px;align-items:center;margin-bottom:10px">
    <label style="margin:0">Год</label>
    <select name="year" onchange="this.form.submit()" style="max-width:120px">{year_opts}</select>
  </form>
  <ul style="margin:0 0 14px;padding-left:18px;line-height:1.8">{''.join(year_lines)}</ul>
  <div class="ybars">{bars}</div>
</div>"""

    # --- радио ---
    srow = []
    smax = max([s["sec"] for s in d["stations"]] or [1]) or 1
    for s in d["stations"][:30]:
        last = datetime.fromtimestamp(s["last"]).strftime("%d.%m.%Y") if s["last"] else ""
        srow.append(
            f"<tr><td><strong>{esc(s['name'])}</strong></td><td>{esc(_fmt_dur(s['sec']))}</td>"
            f"<td>{s['sessions']}</td><td>{esc(last)}</td><td style='width:26%'>{_bar(s['sec'] * 100.0 / smax)}</td></tr>"
        )
    pop_html = ""
    try:
        radio = _STATE.get("radio")
        pop = radio.popular.top()[:10] if radio is not None and getattr(radio, "popular", None) else []
        if pop:
            pop_html = (
                "<h3 style='margin:18px 0 8px'>Ваши популярные радиостанции</h3>"
                "<p class='muted' style='margin:0 0 6px;font-size:12.5px'>Станции, которые вы слушали подолгу "
                "(тот же список, что в «Радио → Ваше популярное радио»).</p>"
                "<table><tr><th>Станция</th><th>Долгих прослушиваний</th><th>Время</th></tr>"
                + "".join(f"<tr><td>{esc(p['name'])}</td><td>{p['count']}</td><td>{esc(_fmt_dur(p['sec']))}</td></tr>" for p in pop)
                + "</table>"
            )
    except Exception:  # noqa: BLE001
        pass
    radio_card = (
        f'<div class="card"><h2>Радио</h2><p style="margin-top:0">Всего слушал радио: <b>{esc(_fmt_dur(d["radio"]))}</b>'
        f' · станций: <b>{len(d["stations"])}</b></p>'
        + ("<table class='st-table'><tr><th>Станция</th><th>Время</th><th>Включений</th><th>Последний раз</th><th></th></tr>"
           + "".join(srow) + "</table>" if srow else '<p class="muted">Время радио считается, пока эмулятор проксирует поток '
           '(Настройки → Радио → проксирование). Пока записей нет.</p>')
        + pop_html + "</div>"
    )

    # --- автоплейлист + настройки ---
    auto_status = esc(S.auto_last_status() or "ещё не собирался")
    chk = " checked" if cfg_s["auto_playlist"] else ""
    enabled_chk = " checked" if cfg_s["enabled"] else ""
    si = S.storage_info()
    storage_line = (f"Хранилище: текущий месяц {si['plain'] // 1024} КБ; архив — {si['months_gz']} мес. "
                    f"в сжатом виде, {si['gz'] // 1024} КБ.")
    settings_card = f"""
<div class="card"><h2>Настройки статистики и автоплейлист USB</h2>
  <form method="POST" action="/stats/settings">
    <label style="display:flex;gap:8px;align-items:center;margin:0 0 6px"><input type="checkbox" name="enabled" value="1"{enabled_chk} style="width:auto">
      <b>Собирать статистику прослушивания</b></label>
    <p class="muted" style="font-size:12.5px;margin:0 0 12px">{esc(storage_line)}</p>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px">
      <div><label>Топ исполнителей (1–100)</label><input type="number" name="top_artists" min="1" max="100" value="{cfg_s['top_artists']}"></div>
      <div><label>Топ треков (1–500)</label><input type="number" name="top_tracks" min="1" max="500" value="{cfg_s['top_tracks']}"></div>
      <div><label>Засчитывать прослушивание, если играло дольше (сек)</label><input type="number" name="min_sec" min="1" max="3600" value="{cfg_s['min_sec']}"></div>
      <div><label>Автоочистка: если статистика больше (МБ; 0 — выкл.)</label><input type="number" name="max_mb" min="0" max="100000" value="{cfg_s['max_mb']}"></div>
    </div>
    <p class="muted" style="font-size:12.5px;margin:8px 0 0">Короче порога (по умолчанию 30 секунд) — пропуск, в статистику не попадает.
      Когда данные занимают больше указанного размера, автоматически удаляются 3 самых старых месяца (текущий не трогается).</p>
    <hr>
    <label style="display:flex;gap:8px;align-items:center;margin:0 0 10px"><input type="checkbox" name="auto_playlist" value="1"{chk} style="width:auto">
      Автоматически создавать и обновлять USB-плейлист по статистике</label>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px">
      <div><label>Название плейлиста</label><input name="auto_title" value="{esc(cfg_s['auto_title'])}" maxlength="80"></div>
      <div><label>Макс. треков (10–1000)</label><input type="number" name="auto_max" min="10" max="1000" value="{cfg_s['auto_max']}"></div>
      <div><label>Обновлять раз в (часов)</label><input type="number" name="auto_hours" min="1" max="720" value="{cfg_s['auto_hours']}"></div>
    </div>
    <p class="muted" style="font-size:12.5px">В плейлист попадают файлы с USB — ваши самые часто прослушиваемые треки по статистике
      (прослушивания трека на разных площадках и на флешке складываются; если трека со стриминга нет на флешке, он пропускается).
      Берутся только прослушанные треки, а не все песни топ-исполнителей; количество ограничено «Макс. треков».
      Порядок перемешан так, чтобы исполнители не шли подряд. Плейлист появится в «Плейлисты USB» и на ресивере.</p>
    <p class="muted" style="font-size:12.5px;margin-top:0">Состояние: {auto_status}</p>
    <div style="display:flex;gap:10px;flex-wrap:wrap">
      <button type="submit">Сохранить</button>
      <button type="submit" formaction="/stats/playlist" class="ghost">🎧 Собрать плейлист сейчас</button>
      <button type="submit" formaction="/stats/clear" class="ghost"
              onclick="return confirm('Удалить всю статистику прослушивания? Это необратимо.')">🗑 Сбросить статистику</button>
    </div>
  </form>
</div>"""

    import json as _json
    exclude_card = f"""
<div class="card"><h2>Не учитывать папки USB</h2>
  <p class="muted" style="margin-top:0;font-size:12.5px">Отметьте галочкой папки, которые не должны попадать в статистику
    (в них не будут записываться новые прослушивания, а уже записанные скрываются из отчётов). Клик по названию папки — открыть её и отметить вложенные.</p>
  <form method="POST" action="/stats/exclude" id="ex-form">
    <input type="hidden" name="exclude_dirs" id="ex-val">
    <div class="muted" style="font-size:12.5px;margin-bottom:6px">Сейчас открыто: <code id="ex-path">…</code></div>
    <div id="ex-list" style="max-height:340px;overflow:auto;border:1px solid var(--line);border-radius:10px;padding:4px 10px"></div>
    <h3 style="margin:14px 0 6px;font-size:14px">Исключены (<span id="ex-count">0</span>)</h3>
    <div id="ex-sel"></div>
    <div style="margin-top:12px"><button type="submit">Сохранить список</button></div>
  </form>
</div>
<script>window.EX_SEL={_json.dumps(cfg_s["exclude_dirs"], ensure_ascii=False)};</script>
<script>{_EXCLUDE_JS}</script>
"""
    note = (f'<p class="muted" style="font-size:12.5px;margin:0 0 14px">Время — оценка по запросам ресивера: '
            'для радио и потоковых треков точно, для треков, которые ресивер загружает в буфер целиком — '
            f'по моменту запуска следующего трека (не больше длительности). Короче {cfg_s["min_sec"]} сек. не считаем.</p>')
    if month:
        return picker + disabled_note + cards + svc_card + art_card + trk_card + radio_card + flash_extra
    return (picker + disabled_note + cards + note + svc_card + art_card + trk_card + month_card + year_card
            + radio_card + settings_card + exclude_card + flash_extra)


# ---------- История эфира ----------

def _radiohist_settings() -> dict:
    return _stats_settings()


def _radio_names() -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        radio = _STATE.get("radio")
        if radio is not None:
            for pl in radio.list_playlists():
                for st in pl.stations:
                    out[st.url] = st.name
    except Exception:  # noqa: BLE001
        pass
    return out


def _radio_history_data() -> dict:
    from . import stats as _st
    rh = _st.get_radio_history()
    cfg_s = _radiohist_settings()
    items = rh.latest(cfg_s["radio_stations"], cfg_s["radio_tracks"], _radio_names()) if rh else []
    return {"now": time.time(), "stations": items,
            "n_stations": cfg_s["radio_stations"], "n_tracks": cfg_s["radio_tracks"]}


_EXCLUDE_JS = r"""
(function(){
  function $(i){return document.getElementById(i);}
  function esc(t){var d=document.createElement('div');d.textContent=t==null?'':t;return d.innerHTML;}
  var sel=(window.EX_SEL||[]).slice(), cur='';
  function covered(p){ // папка внутри уже исключённой (выше по дереву)
    for(var i=0;i<sel.length;i++){var s=sel[i]; if(p!==s && p.indexOf(s+'/')===0) return s;} return '';
  }
  function renderSel(){
    $('ex-count').textContent=sel.length;
    var h='';
    sel.forEach(function(p,i){h+='<div class="ex-sel"><span>📁 '+esc(p)+'</span><button type="button" class="ghost small" data-rm="'+i+'">✕</button></div>';});
    $('ex-sel').innerHTML=h||'<p class="muted" style="margin:0">Ничего не исключено — учитываются все папки.</p>';
    $('ex-val').value=sel.join('\n');
  }
  function load(path){
    var list=$('ex-list');list.innerHTML='<p class="muted">Загрузка…</p>';
    fetch('/api/usb/ls?path='+encodeURIComponent(path||''),{cache:'no-store'}).then(function(r){return r.json();}).then(function(j){
      cur=j.path||'';$('ex-path').textContent=cur||'(корни USB)';
      var h='';
      if(j.error)h+='<p class="muted">'+esc(j.error)+'</p>';
      if(j.parent!==null&&j.parent!==undefined&&cur)h+='<div class="ex-row"><a href="#" data-go="'+esc(j.parent)+'">⬆ Вверх</a></div>';
      (j.dirs||[]).forEach(function(d){
        var up=covered(d.path), on=sel.indexOf(d.path)>=0||!!up;
        h+='<div class="ex-row"><input type="checkbox" data-p="'+esc(d.path)+'"'+(on?' checked':'')+(up?' disabled':'')+'> '+
           '<a href="#" data-go="'+esc(d.path)+'">📁 '+esc(d.name)+'</a>'+(up?' <span class="muted" style="font-size:11.5px">(исключена папкой выше)</span>':'')+'</div>';
      });
      if(!(j.dirs||[]).length&&!j.error)h+='<p class="muted">Вложенных папок нет</p>';
      list.innerHTML=h;
    }).catch(function(){list.innerHTML='<p class="muted">Не удалось загрузить каталог</p>';});
  }
  $('ex-list').addEventListener('click',function(e){
    var a=e.target.closest?e.target.closest('a[data-go]'):null;
    if(a){e.preventDefault();load(a.getAttribute('data-go'));}
  });
  $('ex-list').addEventListener('change',function(e){
    var t=e.target;if(!t.getAttribute||!t.getAttribute('data-p'))return;
    var p=t.getAttribute('data-p'), i=sel.indexOf(p);
    if(t.checked&&i<0){sel=sel.filter(function(x){return x.indexOf(p+'/')!==0;});sel.push(p);}  // вложенные поглощаются
    if(!t.checked&&i>=0)sel.splice(i,1);
    renderSel();load(cur);
  });
  $('ex-sel').addEventListener('click',function(e){
    var b=e.target.closest?e.target.closest('button[data-rm]'):null;
    if(b){sel.splice(+b.getAttribute('data-rm'),1);renderSel();load(cur);}
  });
  renderSel();load('');
})();
"""


_RADIOHIST_JS = r"""
(function(){
  function $(i){return document.getElementById(i);}
  function esc(t){var d=document.createElement('div');d.textContent=t==null?'':t;return d.innerHTML;}
  var data=window.RH_DATA||{stations:[]}, off=0;
  function ago(s){s=Math.max(0,Math.round(s));
    if(s<60)return 'только что';
    var m=Math.floor(s/60);if(m<60)return m+' мин назад';
    var h=Math.floor(m/60);return h+' ч '+(m%60)+' мин назад';}
  function hm(t){var d=new Date(t*1000);return ('0'+d.getHours()).slice(-2)+':'+('0'+d.getMinutes()).slice(-2);}
  function nowS(){return Date.now()/1000+off;}
  function render(){
    var box=$('rh-list');if(!box)return;
    var st=data.stations||[];
    if(!st.length){box.innerHTML='<div class="card"><p class="muted">Пока пусто. Названия треков появятся, когда вы включите радиостанцию, '+
      'которая передаёт их в потоке (ICY), при включённом проксировании радио.</p></div>';return;}
    var h='';
    st.forEach(function(s){
      h+='<div class="card"><h2>'+esc(s.name)+' <span class="muted" style="text-transform:none;letter-spacing:0;font-family:inherit">· слушали '+ago(nowS()-s.last)+'</span></h2>'+
         '<table class="rh-table"><tr><th style="width:4.5em">Время</th><th style="width:9em">Прошло</th><th>Трек</th><th style="width:7em"></th></tr>';
      (s.tracks||[]).forEach(function(t){
        var name=(t.artist?t.artist+' — ':'')+t.title;
        var q=(t.artist?t.artist+' ':'')+t.title;
        h+='<tr><td>'+hm(t.t)+'</td><td class="muted" data-ago="'+t.t+'">'+ago(nowS()-t.t)+'</td><td><strong>'+esc(name)+'</strong></td>'+
           '<td><a class="btn small ghost" href="/search?pick=1&q='+encodeURIComponent(q)+'">🔍 Найти</a></td></tr>';
      });
      h+='</table></div>';
    });
    box.innerHTML=h;
  }
  function tickAgo(){
    var cells=document.querySelectorAll('#rh-list [data-ago]');
    for(var i=0;i<cells.length;i++)cells[i].textContent=ago(nowS()-(+cells[i].getAttribute('data-ago')));
  }
  function load(){
    fetch('/api/radio/history',{cache:'no-store'}).then(function(r){return r.json();}).then(function(j){
      data=j;off=j.now-Date.now()/1000;render();
    }).catch(function(){});
  }
  off=(data.now||0)-Date.now()/1000;render();
  setInterval(function(){if(!document.hidden)load();},10000);
  setInterval(tickAgo,15000);
})();
"""


def _radiohist_body() -> str:
    import json as _json
    cfg_s = _radiohist_settings()
    data = _radio_history_data()
    return f"""
<div class="card">
  <h2>Настройки</h2>
  <form method="POST" action="/radio-history/settings" style="display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end">
    <div><label>Сколько последних радиопотоков (1–10)</label>
      <input type="number" name="radio_stations" min="1" max="10" value="{cfg_s['radio_stations']}" style="max-width:150px"></div>
    <div><label>Сколько последних треков на поток (1–30)</label>
      <input type="number" name="radio_tracks" min="1" max="30" value="{cfg_s['radio_tracks']}" style="max-width:150px"></div>
    <button type="submit">Сохранить</button>
    <button type="submit" formaction="/radio-history/clear" class="ghost"
            onclick="return confirm('Очистить историю эфира?')">Очистить историю</button>
  </form>
  <p class="muted" style="font-size:12.5px;margin-bottom:0">Показываются потоки, которые вы слушали последними, и что на них играло
    (по названиям трека, которые станция передаёт в потоке). Список обновляется сам. Кнопка «Найти» откроет поиск с готовым
    названием — останется выбрать площадку.</p>
</div>
<div id="rh-list"></div>
<script>window.RH_DATA={_json.dumps(data, ensure_ascii=False)};</script>
<script>{_RADIOHIST_JS}</script>
"""


def _search_pick_body(q: str) -> str:
    """Поиск с готовым названием: пользователь выбирает площадку."""
    sites = [
        ("deezer", "Deezer", "deezer"), ("yandex", "Яндекс.Музыка", "yandex"),
        ("kion", "КИОН Музыка", "kion"), ("zvuk", "Звук", "zvuk"),
        ("drivemusic", "DriveMusic", "drivemusic"), ("promodj", "PromoDJ", "promodj"),
        ("zaycev", "Zaycev.net", "zaycev"),
    ]
    btns = []
    for key, label, state_key in sites:
        cl = _STATE.get(state_key)
        ok = bool(cl and (getattr(cl, "enabled", False) or (getattr(cl, "token", "") or "").strip()))
        dis = "" if ok else ' disabled title="Площадка не подключена"'
        btns.append(f'<button type="submit" name="site" value="{key}"{dis}>{html.escape(label)}</button>')
    return f"""
<div class="card">
  <h2>Найти трек</h2>
  <form method="GET" action="/search">
    <label>Что искать (можно поправить)</label>
    <input name="q" value="{html.escape(q, quote=True)}" autofocus>
    <p class="muted" style="font-size:12.5px;margin:10px 0 8px">Выберите, где искать:</p>
    <div style="display:flex;gap:8px;flex-wrap:wrap">{''.join(btns)}</div>
  </form>
</div>
"""


# ---------- Лог-файл: размер, предупреждение, очистка ----------

def _log_files() -> list:
    """Файлы, куда пишет служба (stdout/stderr, если это файлы) + типовой путь на Entware."""
    paths: list = []
    for fd in (1, 2):
        try:
            pth = os.readlink(f"/proc/self/fd/{fd}")
            if pth.startswith("/") and os.path.isfile(pth):
                paths.append(pth)
        except OSError:
            pass
    for pth in ((_cfg().get("log_file") or ""), "/opt/var/log/dlna-emu.log"):
        if pth and os.path.isfile(pth):
            paths.append(pth)
    out: list = []
    for pth in paths:
        if pth not in out:
            out.append(pth)
    return out


def _log_size_bytes() -> int:
    total = 0
    for pth in _log_files():
        try:
            total += os.path.getsize(pth)
        except OSError:
            pass
    return total


def _fmt_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} МБ"
    return f"{n / 1024:.0f} КБ"


def _log_clear() -> int:
    """Обнулить лог-файлы; указатель записи у stdout/stderr возвращаем в начало (иначе остались бы «дыры»)."""
    n = 0
    for pth in _log_files():
        try:
            os.truncate(pth, 0)
            n += 1
            for fd in (1, 2):
                try:
                    if os.readlink(f"/proc/self/fd/{fd}") == pth:
                        os.lseek(fd, 0, os.SEEK_SET)
                except OSError:
                    pass
        except OSError as e:
            log.warning("не удалось очистить %s: %s", pth, e)
    return n


LOG_MAX_MB_DEFAULT = 0.5
LOG_MAX_MB_LIMIT = 10.0


def _log_max_mb() -> float:
    """Порог автоочистки лога, МБ: 0 — выключена, максимум 10, по умолчанию 0.5 (500 КБ)."""
    raw = _cfg().get("log_max_mb", LOG_MAX_MB_DEFAULT)
    try:
        v = float(str(raw).replace(",", "."))
    except (TypeError, ValueError):
        v = LOG_MAX_MB_DEFAULT
    return max(0.0, min(LOG_MAX_MB_LIMIT, v))


def _fmt_mb(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".") or "0"


def _log_autoclean_once() -> bool:
    limit = _log_max_mb()
    if limit <= 0:
        return False
    if _log_size_bytes() <= limit * 1024 * 1024:
        return False
    n = _log_clear()
    if n:
        log.info("Лог-файл превысил %s МБ — очищен автоматически", _fmt_mb(limit))
    return bool(n)


def _log_autoclean_loop() -> None:
    while True:
        time.sleep(30)
        try:
            _log_autoclean_once()
        except Exception as e:  # noqa: BLE001
            log.debug("автоочистка лога: %s", e)


def _service_card() -> str:
    return """
<div class="card">
  <h2>Служба</h2>
  <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">
    <form method="POST" action="/restart" onsubmit="return confirm('Перезапустить службу DLNA-emu?')" style="margin:0">
      <button type="submit" class="ghost">Перезапустить службу</button>
    </form>
    <span class="muted" style="font-size:12.5px">Полный перезапуск (как stop + start в SSH); панель пропадёт на несколько секунд.</span>
  </div>
</div>
"""


def _log_file_card() -> str:
    size = _log_size_bytes()
    files = _log_files()
    where = ", ".join(files) if files else "лог пишется не в файл (консоль/syslog)"
    limit = _log_max_mb()
    return f"""
<div class="card">
  <h2>Лог-файл</h2>
  <p style="margin:0 0 8px">Размер лога: <b>{html.escape(_fmt_size(size))}</b>
    <span class="muted" style="font-size:12px"> · {html.escape(where)}</span></p>
  <form method="POST" action="/service/logmax" style="display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end;margin:0">
    <div><label>Очищать лог автоматически, если он больше (МБ, не более {_fmt_mb(LOG_MAX_MB_LIMIT)}; 0 — не очищать)</label>
      <input type="number" name="log_max_mb" min="0" max="{_fmt_mb(LOG_MAX_MB_LIMIT)}" step="0.1" value="{_fmt_mb(limit)}" style="max-width:140px"></div>
    <button type="submit">Сохранить</button>
    <button type="submit" formaction="/service/logclear" class="ghost"
            onclick="return confirm('Очистить лог-файл службы?')">Очистить лог сейчас</button>
  </form>
  <p class="muted" style="font-size:12.5px;margin:8px 0 0">По умолчанию 0.5 МБ (500 КБ). Проверка — раз в 30 секунд; файл обнуляется целиком.</p>
</div>
"""


# ---------- «Устройства онлайн» в реальном времени ----------

_DEVLIVE_JS = r"""
(function(){
  var box=document.getElementById('dev-rows');if(!box)return;
  var L=window.DEV_LABELS||{}, off=0, rows={};
  function esc(t){var d=document.createElement('div');d.textContent=t==null?'':t;return d.innerHTML;}
  function ago(s){s=Math.max(0,Math.round(s));return s<60?s+' сек. назад':Math.floor(s/60)+' мин. назад';}
  function nowS(){return Date.now()/1000+off;}
  function metaText(x){
    var st=x.streaming?' ▶':'';
    var lbl=L[x.service]||x.service||'?';
    var extra=(x.service==='radio'&&x.track)?' · '+esc(x.title):'';
    return '<code>'+esc(x.ip||'?')+'</code> · '+esc(lbl)+st+extra+' · <span data-ts="'+x.ts+'">'+ago(nowS()-x.ts)+'</span>';
  }
  function art(x){
    if(x.cover){return '<img class="dev-art" src="'+esc(x.cover)+'" alt="" onerror="this.outerHTML=\'<div class=dev-art-ph>♪</div>\'">';}
    return '<div class="dev-art-ph">♪</div>';
  }
  function update(j){
    off=j.now-Date.now()/1000;
    var list=(j.clients||[]);
    var empty=document.getElementById('dev-empty');
    if(!list.length){
      for(var k in rows){rows[k].el.remove();delete rows[k];}
      if(empty)empty.style.display='';return;
    }
    if(empty)empty.style.display='none';
    var seen={};
    list.forEach(function(x){
      seen[x.ip]=1;
      var title=(x.service==='radio'&&x.track)?x.track:(x.title||'?');
      var r=rows[x.ip];
      if(!r){
        var el=document.createElement('div');el.className='dev-row';
        el.innerHTML='<span class="dev-art-wrap"></span><div style="min-width:0;flex:1"><div class="dev-title" style="font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis"></div><div class="muted dev-meta" style="font-size:12.5px;margin-top:2px"></div></div>';
        box.appendChild(el);
        r=rows[x.ip]={el:el,cover:null,title:null};
      }
      if(r.cover!==(x.cover||'')){r.cover=x.cover||'';r.el.querySelector('.dev-art-wrap').innerHTML=art(x);}
      if(r.title!==title){r.title=title;r.el.querySelector('.dev-title').textContent=title;}
      r.el.querySelector('.dev-meta').innerHTML=metaText(x);
    });
    for(var ip in rows){if(!seen[ip]){rows[ip].el.remove();delete rows[ip];}}
  }
  function load(){
    fetch('/api/now',{cache:'no-store'}).then(function(r){return r.json();}).then(update).catch(function(){});
  }
  function tick(){
    var c=box.querySelectorAll('[data-ts]');
    for(var i=0;i<c.length;i++)c[i].textContent=ago(nowS()-(+c[i].getAttribute('data-ts')));
  }
  load();
  setInterval(function(){if(!document.hidden)load();},2000);
  setInterval(tick,1000);
})();
"""


def _devices_card() -> str:
    """Карточка «Устройства онлайн»: рисуется на клиенте и обновляется каждые 2 с."""
    import json as _json
    return f"""
<div class="card keep-color" id="dev-card">
  <h2>Устройства онлайн</h2>
  <p class="muted" id="dev-empty">Сейчас никто не слушает. Пока ресивер играет, здесь видно трек и обложку в реальном времени.</p>
  <div id="dev-rows"></div>
</div>
<script>window.DEV_LABELS={_json.dumps(_SVC_LABELS, ensure_ascii=False)};</script>
<script>{_DEVLIVE_JS}</script>
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
    radio_dir = str(getattr(radio_lib, "folder", "") or radio.get("folder") or "")
    srv_name = html.escape(str(server.get("name") or "DLNA-emu"))
    ssdp_ok = ssdp is not None and getattr(ssdp, "ok", False)
    ssdp_html = (
        '<span class="badge ok">OK</span>' if ssdp_ok else
        ('<span class="badge off">проблема</span>' if ssdp is not None else '<span class="badge off">—</span>')
    )
    ssdp_err = ""
    if ssdp is not None and not ssdp_ok:
        ssdp_err = f'<div class="muted" style="margin-top:6px">{html.escape(str(getattr(ssdp, "last_error", "") or "не OK"))}</div>'

    def _tile(name, badge, extra, meta, site=""):
        link = (f'<a class="tile-go" href="/search?site={site}" title="Поиск">→</a>' if site else "")
        return (
            f'<div class="tile"><div class="tile-top"><span class="tile-name">{name}</span>{badge}</div>'
            f'{("<div class=tile-meta style=margin-top:6px>" + extra.replace(" · ", "", 1) + "</div>") if extra else ""}<div class="tile-meta">{meta}</div>{link}</div>'
        )

    tiles = "".join([
        _tile("Radio", '<span class="badge ok">M3U</span>', "",
              f'плейлистов: <b>{n_pl}</b><br><code title="{html.escape(radio_dir)}">{html.escape(radio_dir or "—")}</code>'),
        _tile("Deezer", dz_badge, _acc_names("deezer"), f'сохранённых: <b>{n_saved}</b>', "deezer"),
        _tile("Яндекс.Музыка", ya_badge, _acc_names("yandex"), f'сохранённых: <b>{n_ya_saved}</b>', "yandex"),
        _tile("Звук", zv_badge, _acc_names("zvuk"), f'сохранённых: <b>{n_zv_saved}</b>', "zvuk"),
        _tile("КИОН", kion_badge, _acc_names("kion"), f'сохранённых: <b>{n_kion_saved}</b>', "kion"),
        _tile("DriveMusic", dm_badge, "", f'сохранённых: <b>{n_dm_saved}</b>', "drivemusic"),
        _tile("Zaycev.net", zy_badge, "", f'сохранённых: <b>{n_zy_saved}</b>', "zaycev"),
        _tile("PromoDJ", pdj_badge, "", "каталог миксов и треков", "promodj"),
        _tile("akniga.org", ak_badge, "", f'книги: <b>{n_ak_saved}</b> · избранное: <b>{n_ak_fav}</b>/500'),
    ])
    return f"""
<div class="stats">
  <div class="stat"><div class="stat-k">DLNA-сервер</div><div class="stat-v">{srv_name}</div>
    <div class="stat-s"><code>{html.escape(str(announce))}:{dlna_port}</code></div></div>
  <div class="stat"><div class="stat-k">Веб-панель</div><div class="stat-v">:{web_port}</div>
    <div class="stat-s"><a href="{html.escape(desc_url)}">description.xml</a></div></div>
  <div class="stat"><div class="stat-k">SSDP</div><div class="stat-v">{ssdp_html}</div>
    <div class="stat-s">анонс в сети{ssdp_err}
      <form class="inl" method="POST" action="/ssdp/announce"><button type="submit" class="icon-btn" title="Объявить сервер в сети заново (SSDP)" onclick="this.classList.add('spin')"><svg viewBox="0 0 24 24"><path d="M21 12a9 9 0 11-2.6-6.4L21 8"/><path d="M21 3v5h-5"/></svg></button></form>
    </div></div>
  <a class="stat stat-link" href="/radio" title="Открыть радио"><div class="stat-k">Радио</div><div class="stat-v">{n_pl}</div>
    <div class="stat-s">плейлистов M3U</div></a>
</div>
{_service_card()}
<div class="card">
  <h2>Источники</h2>
  <div class="tiles">{tiles}</div>
  <div class="row">
    <a class="btn" href="/search">Поиск</a>
  </div>
</div>
{_devices_card()}
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
                badge = '<span class="tkname" style="color:var(--text)">⚠ токен не принят</span>'
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
.tkname{color:var(--muted);font-size:12px;white-space:nowrap}
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
            ns_status += f"<div style=\"color:var(--text)\">✗ {html.escape(_er)}</div>"

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

<details class="settings-spoiler">
  <summary>Сервер</summary>
  <div class="settings-body">
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
</details>


<details class="settings-spoiler">
  <summary>Веб-панель</summary>
  <div class="settings-body">
{_chk("web_enabled", "Включить веб-панель",
        _b(web.get("enabled"), True),
        "после выключения страница станет недоступна — вернуть только правкой config.yaml")}
  <label>Порт веб-панели</label>
  <input name="web_port" type="number" min="1" max="65535"
         value="{html.escape(str(web.get("port") or 8088))}">
  </div>
</details>


<details class="settings-spoiler">
  <summary>Логи и трассировка</summary>
  <div class="settings-body">
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
</details>


<details class="settings-spoiler">
  <summary>Deezer</summary>
  <div class="settings-body">
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
</details>


<details class="settings-spoiler">
  <summary>DriveMusic</summary>
  <div class="settings-body">
{_chk("dm_enabled", "Включить DriveMusic",
        _b(dm.get("enabled"), True))}
  <label>Кэш, сек</label>
  <input name="dm_cache_sec" type="number" min="30" max="86400"
         value="{html.escape(str(dm.get("cache_sec") or 300))}">
  <label>Базовый URL (зеркало; пусто = авто-выбор)</label>
  <input name="dm_base_url"
         value="{html.escape(str(dm.get("base_url") or "https://ru.drivemusic.me"))}">
  </div>
</details>


<details class="settings-spoiler">
  <summary>akniga.org (аудиокниги)</summary>
  <div class="settings-body">
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
</details>


<details class="settings-spoiler">
  <summary>Яндекс.Музыка</summary>
  <div class="settings-body">
{_tokens_widget("yandex_token", "yandex", ya_token, "OAuth-токены (аккаунты Яндекса)")}
  <p class="muted" style="margin-top:4px;font-size:12px">
    Получить:
    <a href="https://oauth.yandex.ru/authorize?response_type=token&amp;client_id=23cabbbdc6cd418abb4b39c32c41195d"
       target="_blank" style="color:var(--text)">oauth.yandex.ru</a>
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
</details>


<details class="settings-spoiler">
  <summary>Звук (zvuk.com)</summary>
  <div class="settings-body">
{_tokens_widget("zvuk_token", "zvuk", zv_token, "X-Auth-Token (аккаунты Звука)")}
  {_chk("zv_keepalive", "Поддерживать сессию",
        _b(zv.get("keepalive"), False),
        "раз в 40–80 мин (в случайное время) запрашивать профиль, чтобы токен не протухал от простоя")}
  <p class="muted" style="margin-top:4px;font-size:12px">
    Войти на <a href="https://zvuk.com" target="_blank" style="color:var(--text)">zvuk.com</a> →
    открыть <a href="https://zvuk.com/api/tiny/profile" target="_blank" style="color:var(--text)">/api/tiny/profile</a> →
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
</details>


<details class="settings-spoiler">
  <summary>КИОН Музыка (music.mts.ru)</summary>
  <div class="settings-body">
{_tokens_widget("kion_token", "kion", kion.get("token") or "", "OAuth-токены (аккаунты КИОН)")}
  <p class="muted" style="margin-top:4px">
    Войти на <a href="https://music.mts.ru" target="_blank" style="color:var(--text)">music.mts.ru</a> /
    <a href="https://music.kion.ru" target="_blank" style="color:var(--text)">music.kion.ru</a> →
    DevTools → Network / Local Storage, либо через документацию
    <a href="https://trudenboy.github.io/ma-provider-kion-music/" target="_blank" style="color:var(--text)">ma-provider-kion-music</a>.
    Нужна подписка для FLAC. API: music.mts.ru/ya_api · Local Storage → ya_token
  </p>
  <label>Качество</label>
  {_sel("kion_quality", kion.get("quality") or "flac", [
      ("flac", "flac (lossless)"),
      ("mp3_320", "mp3_320"),
  ])}
  </div>
</details>


<details class="settings-spoiler">
  <summary>PromoDJ</summary>
  <div class="settings-body">
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
</details>


<details class="settings-spoiler">
  <summary>Zaycev.net</summary>
  <div class="settings-body">
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
</details>


<details class="settings-spoiler">
  <summary>USB / файлы</summary>
  <div class="settings-body">
<label>Корни (по одному пути на строку)</label>
  <textarea name="files_roots" rows="3"
            placeholder="/tmp/mnt&#10;/tmp/mnt/6EE7-47CC">{html.escape(roots_text)}</textarea>
  <p class="muted" style="margin-top:4px;font-size:12px">
    Обычно достаточно <code>/tmp/mnt</code>. Подпапки подхватываются автоматически.
  </p>
  <p class="muted" style="margin-top:8px;font-size:12px">
    Папка с плейлистами (<code>playlists_folder</code>) и создание плейлистов — в разделе
    <a href="/usb" style="color:var(--acc)">Плейлисты USB</a>.
  </p>
  </div>
</details>


<details class="settings-spoiler">
  <summary>Сетевые хранилища (WebDAV / FTP)</summary>
  <div class="settings-body">
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
</details>


<details class="settings-spoiler">
  <summary>Обложки</summary>
  <div class="settings-body">
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
</details>


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


_USB_JS = r"""
(function(){
  var cfg = window.USB_CFG || {};
  var sel = [];                 // [{k:'f'|'d', p:path}]
  var cur = '';
  function $(i){return document.getElementById(i);}
  function esc(t){var d=document.createElement('div');d.textContent=t;return d.innerHTML;}
  function base(p){var a=p.replace(/[\\/]+$/,'').split(/[\\/]/);return a[a.length-1]||p;}
  function has(k,p){for(var i=0;i<sel.length;i++)if(sel[i].k===k&&sel[i].p===p)return i;return -1;}
  function toggle(k,p,on){var i=has(k,p);if(on&&i<0)sel.push({k:k,p:p});if(!on&&i>=0)sel.splice(i,1);renderSel();}
  function renderSel(){
    var box=$('usb-sel'),n=$('usb-count');
    n.textContent=sel.length;
    if(!sel.length){box.innerHTML='<p class="muted">Ничего не выбрано. Отмечайте файлы и каталоги галочками слева.</p>';}
    else{
      var h='<table><tr><th style="width:2.2em">#</th><th>Что добавить</th><th style="width:9em"></th></tr>';
      sel.forEach(function(x,i){
        h+='<tr><td>'+(i+1)+'</td><td>'+(x.k==='d'?'📁 ':'🎵 ')+'<strong>'+esc(base(x.p))+'</strong>'+
           (x.k==='d'?' <span class="muted">(все файлы внутри)</span>':'')+
           '<div class="muted" style="font-size:11px;word-break:break-all">'+esc(x.p)+'</div></td>'+
           '<td style="white-space:nowrap"><button type="button" class="small ghost" data-up="'+i+'">↑</button> '+
           '<button type="button" class="small ghost" data-dn="'+i+'">↓</button> '+
           '<button type="button" class="small ghost" data-rm="'+i+'">✕</button></td></tr>';
      });
      box.innerHTML=h+'</table>';
    }
    // синхронизировать галочки в обзоре
    var cbs=document.querySelectorAll('#usb-list input[data-k]');
    for(var j=0;j<cbs.length;j++){cbs[j].checked=has(cbs[j].getAttribute('data-k'),cbs[j].getAttribute('data-p'))>=0;}
  }
  document.addEventListener('click',function(e){
    var t=e.target;
    if(t.hasAttribute&&t.hasAttribute('data-rm')){sel.splice(+t.getAttribute('data-rm'),1);renderSel();}
    if(t.hasAttribute&&t.hasAttribute('data-up')){var i=+t.getAttribute('data-up');if(i>0){var x=sel[i];sel[i]=sel[i-1];sel[i-1]=x;renderSel();}}
    if(t.hasAttribute&&t.hasAttribute('data-dn')){var i2=+t.getAttribute('data-dn');if(i2<sel.length-1){var y=sel[i2];sel[i2]=sel[i2+1];sel[i2+1]=y;renderSel();}}
  });
  function load(path){
    var list=$('usb-list');list.innerHTML='<p class="muted">Загрузка…</p>';
    fetch('/api/usb/ls?path='+encodeURIComponent(path||''),{cache:'no-store'}).then(function(r){return r.json();}).then(function(j){
      cur=j.path||'';
      var h='';
      $('usb-path').textContent=cur||'(корни USB)';
      if(j.error){h+='<p class="muted">'+esc(j.error)+'</p>';}
      if(j.parent!==null&&j.parent!==undefined&&cur){h+='<div class="usb-row"><a href="#" data-go="'+esc(j.parent)+'">⬆ Вверх</a></div>';}
      (j.dirs||[]).forEach(function(d){
        h+='<div class="usb-row"><input type="checkbox" data-k="d" data-p="'+esc(d.path)+'" title="Добавить весь каталог"> '+
           '<a href="#" data-go="'+esc(d.path)+'">📁 '+esc(d.name)+'</a></div>';
      });
      (j.files||[]).forEach(function(f){
        h+='<div class="usb-row"><input type="checkbox" data-k="f" data-p="'+esc(f.path)+'"> 🎵 '+esc(f.name)+'</div>';
      });
      if(!(j.dirs||[]).length&&!(j.files||[]).length&&!j.error)h+='<p class="muted">Пусто</p>';
      if((j.files||[]).length)h='<div class="usb-row"><label style="margin:0"><input type="checkbox" id="usb-all"> Выбрать все файлы в этом каталоге</label></div>'+h;
      list.innerHTML=h;renderSel();
    }).catch(function(){list.innerHTML='<p class="muted">Не удалось загрузить каталог</p>';});
  }
  $('usb-list').addEventListener('click',function(e){
    var a=e.target.closest?e.target.closest('a[data-go]'):null;
    if(a){e.preventDefault();load(a.getAttribute('data-go'));}
  });
  $('usb-list').addEventListener('change',function(e){
    var t=e.target;
    if(t.id==='usb-all'){
      var cbs=document.querySelectorAll('#usb-list input[data-k=f]');
      for(var i=0;i<cbs.length;i++)toggle('f',cbs[i].getAttribute('data-p'),t.checked);
      return;
    }
    if(t.getAttribute&&t.getAttribute('data-k'))toggle(t.getAttribute('data-k'),t.getAttribute('data-p'),t.checked);
  });
  var undo=null;
  $('usb-shuffle').addEventListener('click',function(){
    if(!sel.length){return;}
    var btn=this,msg=$('usb-shuffle-msg');
    btn.disabled=true;msg.textContent='Перемешиваю…';
    var body=new URLSearchParams();
    body.set('items',sel.map(function(x){return x.k+':'+x.p;}).join('\n'));
    fetch('/usb/shuffle',{method:'POST',body:body,cache:'no-store'}).then(function(r){return r.json();}).then(function(j){
      btn.disabled=false;
      if(j.error||!j.items){msg.textContent=j.error||'Не удалось перемешать';return;}
      undo=sel.slice();
      sel=j.items.map(function(p){return {k:'f',p:p};});
      renderSel();
      $('usb-undo').style.display='';
      msg.textContent='Готово: '+j.items.length+' треков, одинаковый исполнитель подряд: '+j.adj+', через один трек: '+j.skip+
        (j.expanded?' (каталоги раскрыты в файлы)':'')+'. Не забудьте сохранить плейлист.';
    }).catch(function(){btn.disabled=false;msg.textContent='Ошибка запроса';});
  });
  $('usb-undo').addEventListener('click',function(){
    if(!undo){return;}
    sel=undo;undo=null;renderSel();this.style.display='none';$('usb-shuffle-msg').textContent='Порядок возвращён.';
  });
  $('usb-form').addEventListener('submit',function(e){
    if(!sel.length){e.preventDefault();alert('Выберите хотя бы один файл или каталог');return;}
    $('usb-items').value=sel.map(function(x){return x.k+':'+x.p;}).join('\n');
  });
  (cfg.initial||[]).forEach(function(p){sel.push({k:'f',p:p});});
  renderSel();
  load('');
})();
"""


def _usb_body(flash: str = "", edit_pid: str = "", new: bool = False) -> str:
    tree = _STATE.get("tree")
    files = getattr(tree, "files", None) if tree is not None else None
    cfg = _cfg()
    pl_folders = (cfg.get("files") or {}).get("playlists_folder") or []
    if isinstance(pl_folders, str):
        pl_folders = [pl_folders]
    pl_folders_text = "\n".join(str(r) for r in pl_folders if r)

    folder_card = f"""
<div class="card">
  <h2>Папка с плейлистами</h2>
  <form method="POST" action="/usb/folder">
    <label>playlists_folder — папка с плейлистами .m3u / .m3u8 (строка или список, по одному пути на строку)</label>
    <textarea name="files_playlists_folder" rows="2"
              placeholder="/tmp/mnt/6EE7-47CC/playlists">{html.escape(pl_folders_text)}</textarea>
    <p class="muted" style="margin-top:4px;font-size:12px">
      Пусто = искать .m3u по всем корням (roots) USB, новые плейлисты сохраняются в <code>&lt;корень&gt;/playlists</code>.
      Если задана — плейлисты берутся только отсюда (быстрее на больших дисках), а новые сохраняются в первую папку списка.
      Пример: <code>/tmp/mnt/6EE7-47CC/playlists</code> или <code>D:/music/playlists</code> в Windows.
      Применяется сразу, без перезапуска.
    </p>
    <button type="submit">Сохранить папку</button>
  </form>
</div>"""

    if files is None or not (files.roots or files.playlist_folders):
        return folder_card + """
<div class="card"><p class="muted">Корни USB не заданы. Укажите их в «Настройки → USB / файлы»
(обычно <code>/tmp/mnt</code>), затем вернитесь сюда.</p></div>"""

    import json as _json

    # ---------- редактор ----------
    if new or edit_pid:
        pl = files.get_playlist(edit_pid) if edit_pid else None
        if edit_pid and not pl:
            return folder_card + '<div class="card"><p class="muted">Плейлист не найден. <a href="/usb">← к списку</a></p></div>'
        initial = [t.path for t in (pl.tracks if pl else [])]
        title = pl.title if pl else ""
        target = files.playlist_write_dir()
        where = pl.path if pl else (str(target) if target else "—")
        heading = f"Редактирование: {html.escape(pl.title)}" if pl else "Новый плейлист"
        return f"""
<style>
.usb-grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}
.usb-row{{padding:6px 4px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:8px;font-size:13.5px}}
.usb-row a{{color:var(--acc);text-decoration:none;word-break:break-all}}
.usb-row input[type=checkbox]{{width:auto;flex:none}}
#usb-list{{max-height:60vh;overflow:auto}}
#usb-sel{{max-height:60vh;overflow:auto}}
@media (max-width:900px){{.usb-grid{{grid-template-columns:1fr}}}}
</style>
<form method="POST" action="/usb/save" id="usb-form">
<div class="card">
  <h2>{heading}</h2>
  <input type="hidden" name="pid" value="{html.escape(edit_pid)}">
  <input type="hidden" name="items" id="usb-items">
  <label>Название плейлиста</label>
  <input name="title" value="{html.escape(title)}" placeholder="Например: Дорога" required maxlength="80">
  <p class="muted" style="font-size:12px;margin:6px 0 0">Сохранится как <code>.m3u8</code>: <code>{html.escape(where)}</code></p>
</div>
<div class="usb-grid">
  <div class="card">
    <h2>Накопитель USB</h2>
    <p class="muted" style="font-size:12px;margin-top:0">Тут: <code id="usb-path"></code>.
      Галочка у каталога добавляет все файлы внутри (с подпапками), клик по названию — открывает его.</p>
    <div id="usb-list"></div>
  </div>
  <div class="card">
    <h2>В плейлисте (<span id="usb-count">0</span>)</h2>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:0 0 10px">
      <button type="button" class="small ghost" id="usb-shuffle"
              title="Исполнители не повторяются подряд и через трек">🔀 Перемешать</button>
      <button type="button" class="small ghost" id="usb-undo" style="display:none">↩ Вернуть порядок</button>
      <span class="muted" id="usb-shuffle-msg" style="font-size:12px"></span>
    </div>
    <div id="usb-sel"></div>
  </div>
</div>
<div class="card" style="display:flex;gap:10px;flex-wrap:wrap">
  <button type="submit">💾 Сохранить плейлист</button>
  <a class="btn ghost" href="/usb">Отмена</a>
</div>
</form>
<script>window.USB_CFG={_json.dumps({"initial": initial}, ensure_ascii=False)};</script>
<script>{_USB_JS}</script>
"""

    # ---------- список ----------
    pls = files.discover_playlists(force=True)
    rows = []
    for pl in pls:
        rows.append(f"""
<tr>
  <td class="pl-name"><a href="/usb?edit={html.escape(pl.id)}" style="color:var(--acc)"><strong>{html.escape(pl.title)}</strong></a></td>
  <td class="pl-count" data-unit="треков">{len(pl.tracks)}</td>
  <td class="pl-path"><code title="{html.escape(pl.path, quote=True)}">{html.escape(pl.path)}</code></td>
  <td class="pl-act" style="white-space:nowrap">
    <a class="btn small ghost" href="/usb?edit={html.escape(pl.id)}">Изменить</a>
    <form method="POST" action="/usb/delete" style="display:inline"
          onsubmit="return confirm('Удалить плейлист «{html.escape(pl.title, quote=True)}»?')">
      <input type="hidden" name="pid" value="{html.escape(pl.id)}">
      <button type="submit" class="small ghost">Удалить</button>
    </form>
  </td>
</tr>""")
    table = (
        "<table class=\"pl-table\"><tr class=\"pl-head\"><th>Плейлист</th><th>Треков</th><th>Файл</th><th></th></tr>" + "".join(rows) + "</table>"
        if rows else '<p class="muted">Плейлистов пока нет — создайте первый.</p>'
    )
    return f"""
<div class="card">
  <h2>Плейлисты USB</h2>
  <p class="muted" style="margin-top:0">На ресивере: «Плейлисты (USB)». Собирайте музыку с накопителя в плейлисты —
  отмечая файлы и целые каталоги.</p>
  <p><a class="btn" href="/usb?new=1">➕ Создать плейлист</a></p>
  {table}
</div>
{folder_card}
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
            f"<td class='pl-name'><a href='/radio?pl={html.escape(pl.id)}' style='color:var(--acc)'>"
            f"{html.escape(pl.title)}</a></td>"
            f"<td class='pl-count' data-unit='станций'>{len(pl.stations)}</td>"
            f"<td class='pl-path'><code title='{html.escape(pl.path, quote=True)}'>{html.escape(pl.path)}</code></td>"
            f"</tr>"
        )
    pl_table = (
        "<table class='pl-table'><tr class='pl-head'><th>Плейлист</th><th>Станций</th><th>Файл</th></tr>"
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
    <span style="font-size:12px;background:var(--ok-bg);color:var(--text);padding:2px 8px;border-radius:6px;margin-left:8px">v3-num</span>
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
    retries_n = int(getattr(radio, "proxy_retries", 5) or 0)
    retries_hint = (
        f"При таймауте или обрыве потока эмулятор переподключается к той же станции (до {retries_n} раз подряд), "
        "ресивер этого не замечает. Если все попытки провалены — поток завершается и ресивер сам "
        "переходит к следующему."
        if retries_n > 0 else
        "Повторов нет: при таймауте поток завершается, и ресивер сразу переходит к следующему."
    )
    pop = getattr(radio, "popular", None)
    pop_min = int(getattr(pop, "min_minutes", 15) or 15)
    pop_days = int(getattr(pop, "keep_days", 30) or 30)
    pop_n = pop.count() if pop is not None else 0
    pop_on = bool(getattr(radio, "popular_enabled", True)) and proxy_on
    pop_chk = " checked" if pop_on else ""
    pop_dis = "" if proxy_on else " disabled"
    pop_warn = "" if proxy_on else (
        '<p style="color:var(--warn);font-size:13px;margin:8px 0 0">⚠ Проксирование радио выключено — '
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
  <form method="POST" action="/radio/proxy_retries" style="margin-top:14px">
    <input type="hidden" name="view_pid" value="{html.escape(view_pid or "")}">
    <label>Повторов при таймауте/обрыве потока (0 — не повторять)</label>
    <input name="proxy_retries" type="number" min="0" max="20" value="{retries_n}">
    <p class="muted" style="font-size:12px;margin-top:6px">{retries_hint}</p>
    <div class="row" style="margin-top:10px"><button type="submit">💾 Сохранить</button></div>
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
           style="flex:1;min-width:200px;padding:8px 12px;border-radius:8px;border:1px solid var(--line2);background:var(--bg2);color:var(--text)">
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


_SITE_LABELS = {
    "deezer": "Deezer", "drivemusic": "DriveMusic", "yandex": "Яндекс.Музыка", "zvuk": "Звук",
    "kion": "КИОН Музыка", "zaycev": "Zaycev.net", "promodj": "PromoDJ", "akniga": "akniga.org",
}


def _fav_form(site: str, stype: str, id_field: str, ident: str, title: str, query: str) -> str:
    """Кнопка «⭐ В избранное» рядом с «+ В DLNA»."""
    e = html.escape
    return (
        '<form method="POST" action="/search/favorite" style="display:inline">'
        f'<input type="hidden" name="site" value="{e(site)}">'
        f'<input type="hidden" name="type" value="{e(stype)}">'
        f'<input type="hidden" name="{e(id_field)}" value="{e(str(ident))}">'
        f'<input type="hidden" name="title" value="{e(title)}">'
        f'<input type="hidden" name="query" value="{e(query)}">'
        '<button type="submit" class="small ghost">⭐ В избранное</button></form>'
    )


def _search_settings() -> dict:
    raw = (_cfg().get("search") or {})
    v = raw.get("persist", False)
    persist = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
    try:
        show = int(raw.get("history_show", 10))
    except (TypeError, ValueError):
        show = 10
    return {"persist": persist, "history_show": max(1, min(50, show))}


def _search_settings_card() -> str:
    st = _search_settings()
    chk = " checked" if st["persist"] else ""
    return f"""
<div class="card">
  <h2>Настройки истории поиска</h2>
  <form method="POST" action="/search/settings" style="margin:0">
    <label style="display:flex;gap:8px;align-items:center;margin:0 0 10px"><input type="checkbox" name="persist" value="1"{chk} style="width:auto">
      Сохранять результаты поиска («Мои поиски») после перезагрузки</label>
    <div style="display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end">
      <div><label>Показывать в истории поиска (1–50) результатов</label>
        <input type="number" name="history_show" min="1" max="50" value="{st['history_show']}" style="max-width:140px"></div>
      <button type="submit">Сохранить</button>
    </div>
    <p class="muted" style="font-size:12.5px;margin:8px 0 0">По умолчанию выключено: история живёт до перезапуска службы.
      Показываются последние добавленные записи (и в веб-панели, и на ресивере); хранится до 50 на площадку.
      «⭐ Избранное» сохраняется всегда, до 500 записей на каждую площадку.</p>
  </form>
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
    {_fav_form("promodj", "artist", "artist_id", u.slug, u.name, q)}
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
                    '</form> '
                    + _fav_form(site, "artist", "artist_id", str(a.get("id") or ""), aname, q)
                    + '</div></div>'
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
                    '</form> '
                    + _fav_form(site, "album", "album_id", str(al.get("id") or ""), full, q)
                    + '</div></div>'
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
        saved_rows = _rows_for(saved, "/search/remove", "akniga", "saved")
        saved_html = (
            "".join(saved_rows)
            if saved_rows
            else '<p class="muted">Пока пусто. Найди книгу и нажми «+ В DLNA».</p>'
        )
        extra_cards = f"""
<div class="card">
  <h2>🔍 Мои книги · akniga.org</h2>
  <p class="muted">Появляются на ресивере в <strong>akniga.org (аудиокниги) → 🔍 Мои книги</strong> (до перезапуска).</p>
  {saved_html}
  {('<form method="POST" action="/search/clear" style="margin-top:12px"><input type="hidden" name="site" value="akniga"><input type="hidden" name="list" value="saved"><button type="submit" class="ghost">Очистить всё</button></form>') if saved else ''}
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
{_search_settings_card()}
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
            'border:1px solid var(--line2);border-radius:8px;display:flex;align-items:center;gap:12px;'
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
        'details.svc{border-top:1px solid var(--line)}'
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



def _fav_rows(site: str, favs: list, remove_action: str) -> str:
    rows = []
    for s in favs:
        t = s.get("type")
        icon = "📖" if t == "book" else ("👤" if t == "artist" else ("📂" if t == "playlist" else "💿"))
        kind = {"book": "Книга", "artist": "Артист", "playlist": "Плейлист"}.get(t, "Альбом")
        rows.append(f"""
<div class="search-card">
  <div>
    <div class="name">{icon} {html.escape(s.get('title') or '')}</div>
    <div class="desc">{kind}{(' · запрос: ' + html.escape(s.get('query') or '')) if s.get('query') else ''}</div>
  </div>
  <div class="actions">
    <form method="POST" action="{remove_action}" style="display:inline">
      <input type="hidden" name="site" value="{html.escape(site)}">
      <input type="hidden" name="list" value="favorite">
      <input type="hidden" name="sid" value="{html.escape(s.get('id') or '')}">
      <button type="submit" class="small ghost">Удалить</button>
    </form>
  </div>
</div>""")
    return "".join(rows)


def _favorites_body(flash: str = "") -> str:
    tree = _STATE.get("tree")
    sections = []
    total = 0
    order = ("akniga", "deezer", "drivemusic", "yandex", "zvuk", "kion", "zaycev", "promodj")
    for site in order:
        if site == "akniga":
            favs = tree.ak_favorites() if (tree and hasattr(tree, "ak_favorites")) else []
        else:
            favs = tree.site_favorites(site) if (tree and hasattr(tree, "site_favorites")) else []
        total += len(favs)
        if not favs and site != "akniga":
            continue
        label = _SITE_LABELS.get(site, site)
        body = _fav_rows(site, favs, "/favorites/remove") if favs else (
            '<p class="muted">Пока пусто. Найдите книгу на вкладке '
            '<a href="/search?site=akniga" style="color:var(--text)">🔍 Поиск</a> и нажмите «⭐ В избранное».</p>')
        clear = (f'<form method="POST" action="/favorites/clear" style="margin-top:12px">'
                 f'<input type="hidden" name="site" value="{site}">'
                 f'<button type="submit" class="ghost">Очистить избранное · {html.escape(label)}</button></form>') if favs else ""
        file_note = "akniga_favorites.json" if site == "akniga" else "favorites_sites.json"
        sections.append(f"""
<div class="card">
  <h2>⭐ Избранное · {html.escape(label)}</h2>
  <p class="muted">
    Хранится в <code>{file_note}</code> и переживает рестарт.
    На ресивере: <strong>{html.escape(label)} → ⭐ Избранное</strong>.
    Занято: <strong>{len(favs)}</strong> / 500.
  </p>
  {body}
  {clear}
</div>""")
    intro = ""
    if total == 0:
        intro = ('<div class="card"><p class="muted" style="margin:0">Пока ничего нет. На вкладке '
                 '<a href="/search" style="color:var(--text)">🔍 Поиск</a> рядом с «+ В DLNA» есть кнопка «⭐ В избранное» '
                 '(до 500 записей на каждую площадку).</p></div>')
    return intro + "".join(sections)


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
{_log_file_card()}
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

        if path == "/api/now":
            try:
                from .server import get_active_clients
                clients = get_active_clients()
            except Exception:
                clients = []
            for c in clients:
                cov = c.get("cover") or ""
                if cov and not cov.startswith("http"):
                    c["cover"] = _dlna_cover_url(cov)
                if c.get("service") == "radio":
                    # что сейчас играет на станции (ICY из проксируемого потока)
                    try:
                        from . import radio_proxy
                        _, pid, idx = str(c.get("track_id") or "").split(":", 2)
                        stn = _STATE["radio"].get_station(pid, int(idx))
                        live = radio_proxy.live_title(stn.url) if stn else None
                        if live:
                            c["track"] = live
                    except Exception:
                        pass
            self._send_json({"now": time.time(), "clients": clients})
            return
        if path == "/api/radio/history":
            self._send_json(_radio_history_data())
            return
        if path == "/stats":
            try:
                yr = int((qs.get("year") or ["0"])[0])
            except (TypeError, ValueError):
                yr = 0
            self._send_html(_page("Статистика", _stats_body(year=yr, month=(qs.get("month") or [""])[0]), active="stats"))
            return
        if path == "/radio-history":
            self._send_html(_page("История эфира", _radiohist_body(), active="radiohist"))
            return
        if path in ("/", "/status"):
            self._send_html(_page("Статус", _status_body(), active="status"))
            return
        if path == "/settings":
            self._send_html(_page("Настройки", _settings_body(), active="settings"))
            return
        if path == "/usb":
            self._send_html(_page(
                "Плейлисты USB",
                _usb_body(edit_pid=(qs.get("edit") or [""])[0], new=bool(qs.get("new"))),
                active="usb",
            ))
            return
        if path == "/api/usb/ls":
            tree = _STATE.get("tree")
            files = getattr(tree, "files", None) if tree is not None else None
            if files is None:
                self._send_json({"path": "", "parent": None, "dirs": [], "files": [], "error": "USB не настроен"})
            else:
                self._send_json(files.browse((qs.get("path") or [""])[0]))
            return
        if path == "/radio":
            view_pid = (qs.get("pl") or [""])[0]
            self._send_html(_page("Радио", _radio_body(view_pid=view_pid), active="radio"))
            return
        if path == "/search" and (qs.get("pick") or [""])[0]:
            self._send_html(_page("Поиск", _search_pick_body((qs.get("q") or [""])[0]), active="search"))
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

        if path in ("/service/logmax", "/service/logclear"):
            msg = ""
            if path == "/service/logclear":
                n = _log_clear()
                msg = f"Лог очищен ({n} файл.)" if n else "Нет лог-файла, который можно очистить"
            else:
                cfg = _cfg()
                try:
                    v = float(str(form.get("log_max_mb", "")).replace(",", "."))
                except ValueError:
                    v = LOG_MAX_MB_DEFAULT
                v = max(0.0, min(LOG_MAX_MB_LIMIT, v))
                cfg["log_max_mb"] = round(v, 2)
                msg = (f"Автоочистка лога: больше {_fmt_mb(v)} МБ" if v > 0 else "Автоочистка лога выключена")
                try:
                    _save_yaml(cfg, _config_path())
                    _STATE["cfg"] = cfg
                except Exception as e:
                    msg = f"Ошибка сохранения: {e}"
                else:
                    try:
                        _log_autoclean_once()
                    except Exception:  # noqa: BLE001
                        pass
            self._send_html(_page("Консоль", _logs_body(), flash=msg, active="logs"))
            return

        if path in ("/stats/settings", "/stats/playlist", "/stats/clear"):
            from . import stats as _st
            S = _st.get_stats()
            if path == "/stats/clear":
                if S is not None:
                    S.clear()
                self._send_html(_page("Статистика", _stats_body(), flash="Статистика сброшена", active="stats"))
                return
            cfg = _cfg()
            cur = _st.clean_settings(cfg.get("stats"))
            cur.update({
                "enabled": bool(form.get("enabled")),
                "top_artists": form.get("top_artists", cur["top_artists"]),
                "top_tracks": form.get("top_tracks", cur["top_tracks"]),
                "min_sec": form.get("min_sec", cur["min_sec"]),
                "max_mb": form.get("max_mb", cur["max_mb"]),
                "auto_playlist": bool(form.get("auto_playlist")),
                "auto_title": form.get("auto_title", cur["auto_title"]),
                "auto_max": form.get("auto_max", cur["auto_max"]),
                "auto_hours": form.get("auto_hours", cur["auto_hours"]),
            })
            clean = _st.clean_settings(cur)
            cfg["stats"] = {**(cfg.get("stats") or {}), **clean}
            msg = "Настройки статистики сохранены"
            try:
                _save_yaml(cfg, _config_path())
                _STATE["cfg"] = cfg
            except Exception as e:
                msg = f"Ошибка сохранения: {e}"
            if S is not None:
                S._set_cache = (0.0, {})
                try:
                    n_del = S.prune_if_needed()
                    if n_del:
                        msg += f"; удалено старых месяцев: {n_del}"
                except Exception:  # noqa: BLE001
                    pass
            if path == "/stats/playlist" and S is not None:
                tree = _STATE.get("tree")
                files = getattr(tree, "files", None) if tree is not None else None
                if files is None:
                    msg = "Ошибка: USB не настроен (корни USB в настройках)"
                elif S.run_auto_async(files, clean):
                    msg = "Сборка плейлиста запущена в фоне — обновите страницу через несколько секунд"
                else:
                    msg = "Плейлист уже собирается — подождите"
            self._send_html(_page("Статистика", _stats_body(), flash=msg, active="stats"))
            return

        if path == "/stats/exclude":
            from . import stats as _st
            cfg = _cfg()
            cur = _st.clean_settings(cfg.get("stats"))
            cur["exclude_dirs"] = form.get("exclude_dirs", "")
            clean = _st.clean_settings(cur)
            cfg["stats"] = {**(cfg.get("stats") or {}), **clean}
            msg = f"Исключённых папок: {len(clean['exclude_dirs'])}"
            try:
                _save_yaml(cfg, _config_path())
                _STATE["cfg"] = cfg
            except Exception as e:
                msg = f"Ошибка сохранения: {e}"
            S0 = _st.get_stats()
            if S0 is not None:
                S0._set_cache = (0.0, {})
            self._send_html(_page("Статистика", _stats_body(), flash=msg, active="stats"))
            return

        if path in ("/radio-history/settings", "/radio-history/clear"):
            from . import stats as _st
            if path == "/radio-history/clear":
                rh = _st.get_radio_history()
                if rh is not None:
                    rh.clear()
                self._send_html(_page("История эфира", _radiohist_body(), flash="История эфира очищена", active="radiohist"))
                return
            cfg = _cfg()
            cur = _st.clean_settings(cfg.get("stats"))
            cur["radio_stations"] = form.get("radio_stations", cur["radio_stations"])
            cur["radio_tracks"] = form.get("radio_tracks", cur["radio_tracks"])
            clean = _st.clean_settings(cur)
            cfg["stats"] = {**(cfg.get("stats") or {}), **clean}
            msg = "Настройки сохранены"
            try:
                _save_yaml(cfg, _config_path())
                _STATE["cfg"] = cfg
            except Exception as e:
                msg = f"Ошибка сохранения: {e}"
            self._send_html(_page("История эфира", _radiohist_body(), flash=msg, active="radiohist"))
            return

        if path == "/usb/folder":
            cfg = _cfg()
            cfg.setdefault("files", {})
            pl_raw = form.get("files_playlists_folder") or ""
            cfg["files"]["playlists_folder"] = [
                ln.strip() for ln in pl_raw.replace(",", "\n").splitlines() if ln.strip()
            ]
            msg = "Папка плейлистов сохранена"
            try:
                _save_yaml(cfg, _config_path())
                _STATE["cfg"] = cfg
                tree = _STATE.get("tree")
                if tree is not None and getattr(tree, "files", None) is not None:
                    tree.files.set_playlist_folders(cfg["files"]["playlists_folder"])
            except Exception as e:
                msg = f"Ошибка сохранения: {e}"
            self._send_html(_page("Плейлисты USB", _usb_body(), flash=msg, active="usb"))
            return

        if path == "/usb/shuffle":
            tree = _STATE.get("tree")
            files = getattr(tree, "files", None) if tree is not None else None
            if files is None:
                self._send_json({"error": "USB не настроен"})
                return
            items = [ln.strip() for ln in (form.get("items") or "").splitlines() if ln.strip()]
            expanded = any(it.startswith("d:") for it in items)
            paths = files.expand_items(items)
            if not paths:
                self._send_json({"error": "Нет аудиофайлов для перемешивания"})
                return
            try:
                import os as _os
                from .shuffle import artists_for_tracks, repeat_stats, smart_shuffle
                pairs = [(_os.path.splitext(_os.path.basename(p))[0], p) for p in paths]
                artists = artists_for_tracks(pairs)
                order = smart_shuffle(artists)
                adj, skip = repeat_stats(artists, order)
            except Exception as e:
                log.warning("usb shuffle: %s", e)
                self._send_json({"error": f"Ошибка перемешивания: {e}"})
                return
            self._send_json({
                "items": [paths[i] for i in order],
                "adj": adj, "skip": skip, "expanded": expanded,
            })
            return

        if path == "/usb/save":
            tree = _STATE.get("tree")
            files = getattr(tree, "files", None) if tree is not None else None
            if files is None:
                self._send_html(_page("Плейлисты USB", _usb_body(), flash="Ошибка: USB не настроен", active="usb"))
                return
            items = [ln.strip() for ln in (form.get("items") or "").splitlines() if ln.strip()]
            pl, err = files.save_playlist(form.get("title") or "", items, pid=(form.get("pid") or "").strip())
            if err:
                self._send_html(_page(
                    "Плейлисты USB",
                    _usb_body(edit_pid=(form.get("pid") or "").strip(), new=not form.get("pid")),
                    flash=f"Ошибка: {err}", active="usb",
                ))
                return
            try:
                tree.bump_update_id()
            except Exception:
                pass
            self._send_html(_page(
                "Плейлисты USB", _usb_body(),
                flash=f"Плейлист «{pl.title}» сохранён: {len(pl.tracks)} треков", active="usb",
            ))
            return

        if path == "/usb/delete":
            tree = _STATE.get("tree")
            files = getattr(tree, "files", None) if tree is not None else None
            err = files.delete_playlist((form.get("pid") or "").strip()) if files is not None else "USB не настроен"
            if not err:
                try:
                    tree.bump_update_id()
                except Exception:
                    pass
            self._send_html(_page(
                "Плейлисты USB", _usb_body(),
                flash=(f"Ошибка: {err}" if err else "Плейлист удалён"), active="usb",
            ))
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

        if path == "/radio/proxy_retries":
            from .radio_m3u import clamp_proxy_retries
            radio = _STATE.get("radio")
            view_pid = (form.get("view_pid") or "").strip()
            n = clamp_proxy_retries(form.get("proxy_retries"), default=5)
            if radio:
                radio.proxy_retries = n
            cfg = _cfg()
            if "radio" not in cfg or not isinstance(cfg.get("radio"), dict):
                cfg["radio"] = {}
            cfg["radio"]["proxy_retries"] = n
            msg = f"Сохранено: повторов при обрыве потока — {n}" if n else "Сохранено: повторы выключены"
            try:
                _save_yaml(cfg, _config_path())
            except Exception as e:
                log.warning("save radio proxy_retries: %s", e)
                msg = f"Не удалось записать config.yaml: {e}"
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
            if tree and hasattr(tree, "save_search_history"):
                tree.save_search_history()
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
            if tree:
                stype = form.get("type") or ""
                title = form.get("title") or ""
                query = form.get("query") or ""
                label = _SITE_LABELS.get(site, site)
                if site == "akniga":
                    if stype == "book" and hasattr(tree, "add_ak_favorite"):
                        status = tree.add_ak_favorite(form.get("slug") or "", title, query)
                    else:
                        status = "empty"
                elif hasattr(tree, "add_site_favorite"):
                    ident = form.get("artist_id") or form.get("album_id") or form.get("playlist_id") or ""
                    status = tree.add_site_favorite(site, stype, ident, title, query)
                else:
                    status = "empty"
                if status == "ok":
                    added = f"«{title}» добавлено в избранное → {label} → ⭐ Избранное"
                elif status == "exists":
                    added = f"«{title}» уже в избранном"
                elif status == "limit":
                    added = f"Достигнут лимит избранного для площадки «{label}»: 500"
                else:
                    added = "Не удалось добавить в избранное"
            self._send_html(_page(
                "Поиск",
                _search_body(q=form.get("query") or "", site=site),
                flash=added or "",
                active="search",
            ))
            return

        if path == "/search/settings":
            cfg = _cfg()
            try:
                show = int(form.get("history_show", "10"))
            except ValueError:
                show = 10
            new = {"persist": bool(form.get("persist")), "history_show": max(1, min(50, show))}
            was = _search_settings()["persist"]
            cfg["search"] = {**(cfg.get("search") or {}), **new}
            msg = "Настройки поиска сохранены"
            try:
                _save_yaml(cfg, _config_path())
                _STATE["cfg"] = cfg
            except Exception as e:
                msg = f"Ошибка сохранения: {e}"
            tree = _STATE.get("tree")
            if tree is not None:
                if new["persist"]:
                    tree.save_search_history()
                elif was:
                    tree.delete_search_history_file()
            self._send_html(_page("Поиск", _search_body(q="", site=(form.get("site") or "deezer")),
                                  flash=msg, active="search"))
            return

        if path == "/search/remove":
            tree = _STATE.get("tree")
            site = (form.get("site") or "deezer").lower()
            list_kind = (form.get("list") or "").lower()
            if tree and list_kind == "favorite" and site != "akniga" and hasattr(tree, "remove_site_favorite"):
                tree.remove_site_favorite(site, form.get("sid") or "")
            elif tree:
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
            if tree and hasattr(tree, "save_search_history"):
                tree.save_search_history()
            self._send_html(_page(
                "Поиск", _search_body(q="", site=site), active="search",
            ))
            return

        if path == "/search/clear":
            tree = _STATE.get("tree")
            site = (form.get("site") or "deezer").lower()
            list_kind = (form.get("list") or "").lower()
            if tree and list_kind == "favorite" and site != "akniga" and hasattr(tree, "clear_site_favorites"):
                tree.clear_site_favorites(site)
            elif tree:
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
            if tree and hasattr(tree, "save_search_history"):
                tree.save_search_history()
            self._send_html(_page(
                "Поиск", _search_body(q="", site=site), active="search",
            ))
            return

        if path == "/favorites/remove":
            tree = _STATE.get("tree")
            site = (form.get("site") or "akniga").lower()
            if tree:
                if site == "akniga" and hasattr(tree, "remove_ak_favorite"):
                    tree.remove_ak_favorite(form.get("sid") or "")
                elif hasattr(tree, "remove_site_favorite"):
                    tree.remove_site_favorite(site, form.get("sid") or "")
            self._send_html(_page(
                "Избранное",
                _favorites_body(),
                flash="Удалено из избранного",
                active="favorites",
            ))
            return

        if path == "/favorites/clear":
            tree = _STATE.get("tree")
            site = (form.get("site") or "akniga").lower()
            if tree:
                if site == "akniga" and hasattr(tree, "clear_ak_favorites"):
                    tree.clear_ak_favorites()
                elif hasattr(tree, "clear_site_favorites"):
                    tree.clear_site_favorites(site)
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
            # playlists_folder теперь правится в разделе «Плейлисты USB»;
            # из этой формы трогаем только если поле пришло (старые вкладки)
            if form.get("files_playlists_folder") is not None:
                pl_raw = form.get("files_playlists_folder") or ""
                cfg["files"]["playlists_folder"] = [
                    ln.strip() for ln in pl_raw.replace(",", "\n").splitlines() if ln.strip()
                ]

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
                    if "proxy_retries" in (cfg.get("radio") or {}):
                        from .radio_m3u import clamp_proxy_retries
                        radio.proxy_retries = clamp_proxy_retries(cfg["radio"].get("proxy_retries"))
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
    threading.Thread(target=_log_autoclean_loop, name="log-autoclean", daemon=True).start()
    log.info("Web UI at http://%s:%s", host if host != "0.0.0.0" else "0.0.0.0", port)
    return httpd