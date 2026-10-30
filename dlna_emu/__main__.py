#!/usr/bin/env python3
"""Запуск: python -m dlna_emu -c config.yaml"""

from __future__ import annotations

import argparse
import logging
import os
import sys

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore


def _read_text(path: str) -> str:
    """Читает файл, перебирая типичные кодировки (UTF-8 / Windows / CP1251)."""
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8", "utf-8-sig", "cp1251", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    # крайний случай — с заменой битых байт
    return raw.decode("utf-8", errors="replace")


def load_config(path: str) -> dict:
    if not os.path.isfile(path):
        print(f"Config not found: {path}", file=sys.stderr)
        print("Copy config.example.yaml → config.yaml and edit.", file=sys.stderr)
        sys.exit(1)
    text = _read_text(path)
    if yaml is not None:
        return yaml.safe_load(text) or {}
    cfg: dict = {"server": {}, "radio": {}, "deezer": {}, "drivemusic": {}}
    section = None
    for raw in text.splitlines():
        line = raw.split("#")[0].rstrip()
        if not line.strip():
            continue
        if line.endswith(":") and not line.startswith(" "):
            section = line[:-1].strip()
            cfg.setdefault(section, {})
            continue
        if ":" in line and section:
            k, v = line.split(":", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if v.lower() in ("true", "yes"):
                val: object = True
            elif v.lower() in ("false", "no"):
                val = False
            elif v.isdigit():
                val = int(v)
            else:
                val = v
            cfg[section][k] = val
    return cfg


def main() -> None:
    ap = argparse.ArgumentParser(description="DLNA-emu — Radio + Deezer virtual MediaServer")
    ap.add_argument("-c", "--config", default="config.yaml", help="path to config.yaml")
    args = ap.parse_args()
    cfg = load_config(args.config)

    level = str(cfg.get("log_level") or "info").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Подключаем буфер логов для веб-панели /logs
    from . import webui
    webui.install_log_handler()

    from .server import run_server

    run_server(cfg, config_path=args.config)


if __name__ == "__main__":
    main()