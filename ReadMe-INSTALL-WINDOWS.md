# DLNA-emu — установка на Windows (ПК / ноутбук / мини-ПК)

---

## 1. Установи Python 3.12

Скачай установщик:

https://www.python.org/downloads/release/python-3129/

Файл: **Windows installer (64-bit)** — `python-3.12.9-amd64.exe`

### Важно при установке

На первом экране поставь галку внизу:

- [x] **Add python.exe to PATH**

Без неё команда `python` в cmd работать не будет.  
Жми **Install Now**, дождись конца.  
Открой **новое** окно командной строки (старое PATH не подхватит).

### Проверка

```bat
python --version
pip --version
```

Ожидается: Python 3.12.x и pip 24.x / 25.x.

---

## 2. Распакуй архив

Например в `C:\dlna-emu`.

Должно получиться:

```text
C:\dlna-emu\
├── dlna_emu\              ← папка с .py (пакет)
│   ├── __main__.py
│   ├── server.py
│   ├── webui.py
│   ├── providers\         ← площадки (deezer, zvuk, yandex, …)
│   └── …
├── radio\                 ← твои .m3u (создай папку)
├── config.yaml
├── requirements.txt
└── akniga_favorites.json  ← появится после «⭐ В избранное»
```

> Если в архиве только папка `dlna_emu` с модулями — положи её внутрь `C:\dlna-emu\`, чтобы путь был `C:\dlna-emu\dlna_emu\__main__.py`.

---

## 3. Командная строка

```bat
cd /D C:\dlna-emu
```

Должно стать: `C:\dlna-emu>`

---

## 4. Зависимости

```bat
pip install -r requirements.txt
```

Если несколько Python и pip ругается:

```bat
python -m pip install -r requirements.txt
```

| Пакет | Зачем |
|--------|--------|
| `requests` | сеть (Deezer, Яндекс, Звук, PromoDJ, …) |
| `PyYAML` | конфиг |
| `pycryptodome` | расшифровка Deezer / akniga |
| `Pillow` | (опционально) обложки для ресиверов |

**ffmpeg** (для akniga.org — главы книг):

1. Уже есть в архиве в папке ffmpeg-for-windows
2. Укажи путь, например D:/dlna-emu-3000/ffmpeg-for-windows/ffmpeg.exe 

---

## 5. Конфиг `config.yaml`

Создай рядом с папкой `dlna_emu` (в `C:\dlna-emu\config.yaml`).

**Минимум:**

```yaml
server:
  name: "DLNA-emu"
  announce_ip: "192.168.1.50"   # LAN-IP этого ПК
  port: 8200

web:
  enabled: true
  port: 8088

radio:
  folder: "C:/dlna-emu/radio/"

deezer:
  arl: ""

yandex:
  token: ""

akniga:
  enabled: true
  ffmpeg: "C:/dlna-emu/ffmpeg/bin/ffmpeg.exe"
  chapter_kbps: 128

drivemusic:
  enabled: true

files:
  roots: []
```

### Пути в YAML на Windows

Пиши **прямые** слэши и в **кавычках**:

| Правильно | Неправильно |
|-----------|-------------|
| `"C:/dlna-emu/radio/"` | `"C:\dlna-emu\radio"` |

### Как узнать LAN-IP

```bat
ipconfig
```

Строка **IPv4-адрес** у активного адаптера (Ethernet / Wi-Fi).  
**Не** бери `127.0.0.1`.

### ARL Deezer

1. Открой [deezer.com](https://www.deezer.com) → **F12**  
2. **Application** (или Storage) → **Cookies** → `https://www.deezer.com`  
3. Cookie **`arl`** → скопируй значение → вставь в `deezer.arl`

### Токен Яндекс.Музыки

В веб-панели DLNA-emu → **Настройки** есть ссылка на OAuth, или вставь готовый token в `yandex.token`.

---

## 6. Радио

Положи `.m3u` в `C:\dlna-emu\radio\` (создай папку, если нет).

---

## 7. Запуск

```bat
cd /D C:\dlna-emu
python -m dlna_emu -c config.yaml
```

В логе:

```text
DLNA MediaServer «DLNA-emu» at http://192.168.1.50:8200
Web UI: http://192.168.1.50:8088
SSDP OK LOCATION=http://192.168.1.50:8200/description.xml
```

### Брандмауэр Windows

При **первом** запуске появится окно «Разрешить доступ…» — разреши для **частных сетей**.  
Без этого ресивер сервер не увидит.

---

## 8. Веб-панель

**http://192.168.1.50:8088**  
(подставь свой IP)

| Раздел | Назначение |
|--------|------------|
| **Статус** | сервер, радио, Deezer, akniga, избранное |
| **Настройки** | порты, ARL, токен, radio, USB |
| **Радио** | список m3u, «Перечитать» |
| **🔍 Поиск** | Deezer / DriveMusic / Яндекс / **akniga.org** (страницы поиска) |
| **⭐ Избранное** | до **500** книг; файл `akniga_favorites.json` рядом с конфигом |
| **Консоль** | живой лог; кнопка «Скачать лог» |

На поиске akniga:

- **+ В DLNA** → на ресивере **🔍 Мои книги** (до перезапуска)  
- **⭐ В избранное** → **⭐ Избранное** (сохраняется на диск)

---

## 9. Ресивер (Yamaha, Onkyo, Denon, Pioneer…)

**Сеть → Server / DLNA → обновить список**

Найди **DLNA-emu** (или `server.name`):

- **Radio** — m3u  
- **Deezer** / **DriveMusic** / **Яндекс.Музыка**  
- **akniga.org (аудиокниги)** — Мои книги / Избранное / Жанры  
- **Файлы (USB)** — если задан `files.roots`

---

## 10. Остановка

В окне сервера: **Ctrl+C**.

---

## Автозапуск (Планировщик задач)

1. **Win+R** → `taskschd.msc` → Enter  
2. **Создать задачу…**  
3. **Общие:** имя `DLNA-emu`, [x] Выполнять с наивысшими правами  
4. **Триггеры → Создать:** «При входе в систему»  
5. **Действия → Создать:**  
   - Программа: путь к `python.exe` (узнать: `where python`)  
   - Аргументы: `-m dlna_emu -c config.yaml`  
   - Рабочая папка: `C:\dlna-emu`  
6. Сохранить. После перезагрузки сервер поднимется сам.

**Без чёрного окна:** вместо `python.exe` укажи `pythonw.exe`.  
Остановка тогда только через Диспетчер задач (`pythonw.exe`).

---

## Частые проблемы

| Симптом | Решение |
|---------|---------|
| `python` не является командой | При установке не поставили **Add to PATH** → переустанови Python с галкой; или `py -3.12 -m dlna_emu -c config.yaml` |
| Ресивер не видит сервер | `announce_ip` = LAN-IP; одна подсеть; брандмауэр → разрешить Python для **частных** сетей; UDP **1900** / TCP **8200** |
| Веб не открывается | `web.port` (8088); разрешить TCP 8088 в брандмауэре |
| Deezer пустой / не играет | ARL протух или пустой; `pip install pycryptodome`; в логе должно быть `Deezer login OK` |
| DriveMusic пустой | в логе «используем https://…»; если «ни одно зеркало» — сайт режет IP |
| akniga без звука | путь к `ffmpeg.exe` в `akniga.ffmpeg` |
| Избранное пропало | должен быть файл `C:\dlna-emu\akniga_favorites.json` |
| Не играет FLAC на старом ресивере | в качестве Deezer ставь `mp3_320` / `mp3_128` вместо FLAC |
