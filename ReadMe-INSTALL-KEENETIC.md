# DLNA-emu — установка на Keenetic / Netcraze (NDMS + Entware)

**Модель KN-1812 (Ultra)** = архитектура **aarch64**

---

## Часть A. Entware (один раз, если ещё не стоит)

### 1. Компоненты в веб-морде роутера

**Управление → Обновления и компоненты → Компоненты**

Включи:

- Поддержка открытых пакетов  
- Интерфейс USB  
- Файловая система Ext  
- Сервер SSH  

Сохрани. Перезагрузи роутер, если просит.

### 2. Флешка

Флешка в **ext4** → вставь в USB роутера.  
(Или используй встроенный storage, если OPKG уже на нём.)

### 3. Установка Entware

Открой: `http://192.168.1.1/a`  
(или через SSH — см. ниже)

**Для KN-1812 / Ultra (aarch64)** — одна команда:

```text
opkg disk storage: https://bin.entware.net/aarch64-k3.10/installer/aarch64-installer.tar.gz
```

Другая модель — другой URL (`mipsel` / `mips` / `aarch64`). Смотри наклейку **KN-xxxx**.

### 4. SSH на роутер

```bash
ssh root@192.168.1.1 -p 222
```

Пароль: обычно `keenetic` или пароль админа из веб-морды.

### 5. Пакеты Python и ffmpeg

```bash
opkg update
opkg install python3 python3-pip python3-setuptools
pip3 install requests PyYAML pycryptodome
opkg install ffmpeg
```

| Пакет | Зачем |
|--------|--------|
| `requests`, `PyYAML` | сеть и конфиг |
| `pycryptodome` | расшифровка Deezer (FLAC/MP3) |
| `ffmpeg` | главы akniga.org (аудиокниги) |

---

## Часть B. Положить DLNA-emu

### 6. Каталог на роутере

```bash
mkdir -p /opt/dlna-emu
```

### 7. Скопировать файлы

С компьютера (WinSCP, scp, SMB) в `/opt/dlna-emu/`.

Должно получиться так:

```text
/opt/dlna-emu/
├── dlna_emu/              ← пакет (__main__.py, server.py, providers/, …)
├── radio/                 ← твои .m3u
├── config.yaml            ← конфиг (создашь на шаге 8)
├── requirements.txt
└── akniga_favorites.json  ← появится сам после добавления в избранное
```

> **Важно:** запускается как модуль `python3 -m dlna_emu`, поэтому папка должна называться именно `dlna_emu` и лежать внутри `/opt/dlna-emu/`.

### 8. Конфиг

```bash
cd /opt/dlna-emu
# если есть пример:
cp config.example.yaml config.yaml
vi config.yaml
```

**Минимум:**

```yaml
server:
  name: "DLNA-emu"
  announce_ip: "192.168.1.1"   # IP роутера в LAN
  port: 8200                   # DLNA

web:
  enabled: true
  port: 8088                   # веб-панель

radio:
  folder: "/opt/dlna-emu/radio"

deezer:
  arl: ""                      # cookie arl с deezer.com (по желанию)

yandex:
  token: ""                    # OAuth-токен Яндекс.Музыки (по желанию)

akniga:
  enabled: true
  ffmpeg: "ffmpeg"             # из opkg; или полный путь
  chapter_kbps: 128

drivemusic:
  enabled: true

files:
  roots: []                    # например: ["/opt/share/Music"]
```

### 9. Запуск вручную (проверка)

```bash
cd /opt/dlna-emu
python3 -m dlna_emu -c config.yaml
```

В логе ожидается:

```text
DLNA MediaServer «DLNA-emu» at http://192.168.1.1:8200
Web UI: http://192.168.1.1:8088
```

---

## Часть C. Веб-меню и ресивер

### 10. Веб-панель

В браузере (та же Wi-Fi):

**http://192.168.1.1:8088**

| Раздел | Назначение |
|--------|------------|
| **Статус** | жив ли сервер, счётчики |
| **Настройки** | порты, ARL, токен, папка radio |
| **Радио** | список m3u, «Перечитать» |
| **🔍 Поиск** | Deezer / DriveMusic / Яндекс / akniga.org |
| **⭐ Избранное** | до 500 книг akniga, хранится в `akniga_favorites.json` |
| **Консоль** | живой лог |

Сменил порт веба → **Сохранить** → перезапусти службу (часть D).

### 11. На ресивере (Yamaha и др.)

**Сеть → Server / DLNA → обновить → DLNA-emu**

- Radio  
- Deezer / DriveMusic / Яндекс.Музыка  
- **akniga.org (аудиокниги)** → 🔍 Мои книги / ⭐ Избранное / Жанры  

---

## Часть D. Автозапуск

### 12. Init-скрипт

```bash
cat > /opt/etc/init.d/S99dlna-emu << 'END'
#!/bin/sh
DIR=/opt/dlna-emu
case "$1" in
  start)
    mkdir -p /opt/var/log /opt/var/run
    cd "$DIR" && nohup python3 -m dlna_emu -c config.yaml \
      >>/opt/var/log/dlna-emu.log 2>&1 &
    echo $! > /opt/var/run/dlna-emu.pid
    ;;
  stop)
    [ -f /opt/var/run/dlna-emu.pid ] && kill "$(cat /opt/var/run/dlna-emu.pid)" 2>/dev/null
    pkill -f "dlna_emu" 2>/dev/null
    rm -f /opt/var/run/dlna-emu.pid
    ;;
esac
END

chmod +x /opt/etc/init.d/S99dlna-emu
```

### 13. Старт / стоп

```bash
/opt/etc/init.d/S99dlna-emu start
/opt/etc/init.d/S99dlna-emu stop
```

### 14. Лог

```bash
cat /opt/var/log/dlna-emu.log
```

---

## Часть E. Если не работает

| Проблема | Что проверить |
|----------|----------------|
| Не виден на ресивере | `announce_ip` = IP роутера (не `127.0.0.1`); `ps \| grep dlna`; `http://192.168.1.1:8200/description.xml` |
| Веб не открывается | `web.port` (по умолчанию 8088) → `http://192.168.1.1:8088` |
| Нет станций Radio | `.m3u` в `/opt/dlna-emu/radio/`; веб → Радио → Перечитать |
| Нет Deezer | `arl` в Настройках или `config.yaml`; нужен `pycryptodome` |
| Нет akniga / нет звука глав | `ffmpeg` установлен (`opkg install ffmpeg`); `akniga.ffmpeg` в конфиге |
| Избранное пропало после рестарта | файл `akniga_favorites.json` рядом с `config.yaml` (должен писаться при добавлении) |
