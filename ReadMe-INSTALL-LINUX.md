# DLNA-emu — установка на Linux (ПК / NAS / мини-ПК)

---

## 1. Python 3.9+

```bash
python3 --version
```

Нужен **3.9 или новее**.

---

## 2. Распаковка

```bash
unzip dlna_emu_updated.zip
cd dlna_emu   # или каталог, куда распаковал
```

Структура:

```text
dlna-emu/                    # рабочая папка (любое имя)
├── dlna_emu/                # Python-пакет (обязательно это имя)
│   ├── __main__.py
│   ├── server.py
│   ├── webui.py
│   ├── content.py
│   ├── providers/           # один файл = одна площадка (deezer, zvuk, …)
│   ├── zvuk_client.py
│   └── …
├── radio/                   # .m3u (создай сам)
├── config.yaml
├── requirements.txt
└── akniga_favorites.json    # появится после «⭐ В избранное»
```

---

## 3. Зависимости

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

| Пакет | Зачем |
|--------|--------|
| `requests` | HTTP: Deezer, Яндекс, Звук, PromoDJ, DriveMusic, akniga |
| `PyYAML` | `config.yaml` |
| `pycryptodome` | расшифровка Deezer / akniga |
| `Pillow` | (опционально) сжатие обложек для ресиверов |

**ffmpeg** (для глав akniga.org):

```bash
# Debian / Ubuntu
sudo apt install ffmpeg

# Fedora
sudo dnf install ffmpeg

# Arch
sudo pacman -S ffmpeg
```

---

## 4. Конфиг

```bash
cp config.example.yaml config.yaml   # если есть пример
nano config.yaml
```

**Минимум:**

```yaml
server:
  name: "DLNA-emu"
  announce_ip: "192.168.1.50"   # LAN-IP этой машины
  port: 8200

web:
  enabled: true
  port: 8088

radio:
  folder: "./radio"             # или абсолютный путь

deezer:
  arl: ""                       # cookie arl с deezer.com

yandex:
  token: ""                     # OAuth-токен Яндекс.Музыки

akniga:
  enabled: true
  ffmpeg: "ffmpeg"              # или /usr/bin/ffmpeg
  chapter_kbps: 128

drivemusic:
  enabled: true

files:
  roots: []                     # например: ["/mnt/music"]
```

**Как узнать IP:**

```bash
ip -4 addr
# или
hostname -I
```

Не указывай `127.0.0.1` в `announce_ip`.

---

## 5. Радио

```bash
mkdir -p radio
# положи .m3u в radio/
ls radio/
```

---

## 6. Запуск

```bash
source .venv/bin/activate
python3 -m dlna_emu -c config.yaml
```

В логе:

```text
DLNA MediaServer «DLNA-emu» at http://192.168.1.50:8200
Web UI: http://192.168.1.50:8088
```

---

## 7. Веб-панель

**http://IP-компьютера:8088**

| Раздел | Назначение |
|--------|------------|
| **Статус** | сервер, счётчики |
| **Настройки** | порты, ARL, токен, radio, USB-корни |
| **Радио** | m3u, «Перечитать» |
| **🔍 Поиск** | Deezer / DriveMusic / Яндекс / **akniga.org** (с пагинацией) |
| **⭐ Избранное** | до 500 книг, файл `akniga_favorites.json` |
| **Консоль** | лог в браузере |

На akniga: кнопки **+ В DLNA** (мои книги) и **⭐ В избранное**.

---

## 8. Ресивер

**Сеть → Server / DLNA → обновить → DLNA-emu**

---

## 9. Остановка

В терминале: **Ctrl+C**.

---

## Автозапуск (systemd, по желанию)

Файл `/etc/systemd/system/dlna-emu.service`:

```ini
[Unit]
Description=DLNA-emu
After=network.target

[Service]
WorkingDirectory=/home/USER/dlna-emu
ExecStart=/home/USER/dlna-emu/.venv/bin/python -m dlna_emu -c config.yaml
Restart=on-failure
User=USER

[Install]
WantedBy=multi-user.target
```

Подставь свой `USER` и путь. Затем:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now dlna-emu
```

Лог: `journalctl -u dlna-emu -f`

---

## Проблемы

| Проблема | Решение |
|----------|---------|
| Ресивер не видит сервер | `announce_ip` = LAN-IP; ПК и ресивер в одной подсети; UDP **1900**, TCP **8200** не блокировать |
| Веб не открывается | `web.port` в конфиге; `sudo ufw allow 8088/tcp` |
| Deezer пустой | ARL в конфиге/вебе; `pip install pycryptodome` |
| akniga без звука | установлен `ffmpeg`; путь в `akniga.ffmpeg` |
| Избранное после рестарта | `akniga_favorites.json` рядом с `config.yaml` |
