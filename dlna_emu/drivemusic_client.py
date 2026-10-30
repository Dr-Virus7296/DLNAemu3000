"""DriveMusic (ru.drivemusic.me) — бесплатный поиск и стрим без авторизации."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote, urljoin

import requests

log = logging.getLogger("dlna_emu.drivemusic")

BASE = "https://ru.drivemusic.me"

# Зеркала сайта — используется, если основное не отвечает (WAF, регион, DNS).
# Порядок = приоритет. Если пользователь задал base_url в конфиге явно —
# авто-выбор не запускается, используется только он.
MIRRORS: List[str] = [
    "https://ru.drivemusic.me",
    "https://drivemusic.me",
    "https://drivemusic.top",
    "https://drivemusic.su",
    "https://drivemusic.com",
]

# Максимально «браузерный» UA — с ним WAF отдаёт контент чаще.
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Хиты / Чарт (hits.html + годовые)
HITS: List[Tuple[str, str]] = [
    ("hits.html", "Хиты 2026 (чарт)"),
] + [
    (f"hits_{y}.html", f"Хиты {y}")
    for y in range(2016, 1999, -1)
]

# Лучшая музыка по годам
BEST_MUSIC: List[Tuple[str, str]] = [
    ("best_music.html", "Лучшая 2026"),
] + [
    (f"best_music_{y}.html", f"Лучшая {y}")
    for y in range(2026, 1995, -1)
]

# Жанры с /genres.html (slug, title)
GENRES: List[Tuple[str, str]] = [
    ("pop_music", "Поп"),
    ("club_music", "Клубная"),
    ("rock_music", "Рок"),
    ("dance_music", "Танцевальная"),
    ("shanson", "Шансон"),
    ("russian_rap_2026", "Рэп"),
    ("trance", "Транс"),
    ("relax_music", "Релакс"),
    ("dubstep", "Дабстеп"),
    ("hip_hop", "Хип-хоп"),
    ("trap", "Трэп"),
    ("dnb", "Drum & Bass"),
    ("techno", "Техно"),
    ("house", "Хаус"),
    ("electro", "Электро"),
    ("alternative", "Альтернатива"),
    ("metal", "Метал"),
    ("indie", "Инди"),
    ("soundtracks", "Саундтреки"),
    ("klassicheskaya_muzyka", "Классика"),
    ("instrumental", "Инструментал"),
    ("blues", "Блюз"),
    ("jazz", "Джаз"),
    ("country", "Кантри"),
    ("funk", "Фанк"),
    ("soul", "Соул"),
    ("reggae", "Рэгги"),
    ("chillout", "Чиллаут"),
    ("ambient", "Эмбиент"),
    ("lounge", "Лаунж"),
    ("romansy", "Романсы"),
    ("narodnie", "Народные"),
    ("italo_disco", "Итало диско"),
    ("synthpop", "Синти-поп"),
    ("eurodance", "Евродэнс"),
    ("russian_metal", "Русский метал"),
    ("edm", "EDM"),
    ("hardstyle", "Hardstyle"),
    ("enigmatic", "Enigmatic"),
    ("new_age", "New Age"),
    ("neoclassical", "Неоклассика"),
    ("punk", "Панк"),
    ("synthwave", "Synthwave"),
    ("breakbeat", "Брейкбит"),
    ("downtempo", "Downtempo"),
    ("experimental", "Experimental"),
    ("folk", "Фолк"),
    ("grunge", "Гранж"),
    ("darkwave", "Дарквейв"),
    ("industrial", "Индастриал"),
    ("disco", "Диско"),
    ("latina", "Латина"),
    ("reggaeton", "Реггетон"),
    ("bards", "Авторская песня"),
    ("r_n_b", "R&B"),
    ("trip_hop", "Трип-хоп"),
    ("jungle", "Джангл"),
    ("nu_disco", "Ню-диско"),
    ("phonk", "Фонк"),
    ("vocal", "Вокал"),
    ("musical", "Мюзикл"),
    ("estrada", "Эстрада"),
    ("argentine_tango", "Танго"),
    ("rave", "Рейв"),
    ("gospel", "Госпел"),
    ("boogie_woogie", "Буги-вуги"),
    ("bossa_nova", "Босса-нова"),
    ("ska", "Ска"),
    ("new_wave", "New Wave"),
    ("gangsta_rap", "Гангста"),
    ("k_pop", "K-pop"),
    ("rusrock", "Русский рок"),
    ("pesni_60.html", "60-е"),
    ("pesni_70.html", "70-е"),
    ("pesni_80.html", "80-е"),
    ("pesni_90.html", "90-е"),
    ("pesni_00.html", "00-е"),
    ("bebop", "Бибоп"),
    ("psychobilly", "Психобилли"),
]

# Подборки сверены с живыми страницами ru.drivemusic.me/<группа>.html.
# Прежний список содержал выдуманные slug-и (из-за чего папки были пустыми) —
# теперь это реальные адреса вида /<slug>/. Актуальный список группы эмулятор
# дополнительно подтягивает с сайта (см. DriveMusicClient.list_playlist_subs).
PLAYLIST_GROUPS: List[Tuple[str, str, List[Tuple[str, str]]]] = [
    ("ritmichnye_pesni", "Ритмичная", [
        ("diskoteka_90", "Дискотека 90"),
        ("diskoteka_80", "Дискотека 80"),
        ("energy", "Энергичная"),
        ("joy", "Бодрая"),
        ("eurobeat", "Евробит"),
        ("brazilian_phonk", "Бразильский фонк"),
        ("progressive_trance", "Progressive Trance"),
        ("deep_house", "Deep House"),
        ("twist", "Твист"),
        ("bachata", "Бачата"),
        ("german_dance", "Немецкий дэнс"),
        ("italian_rap", "Итальянский рэп"),
        ("eurodance", "Eurodance"),
        ("electro_swing", "Electro Swing"),
        ("dream_house", "Dream House"),
        ("russian_metal", "Русский метал"),
        ("salsa", "Сальса"),
        ("edm", "В стиле EDM"),
        ("progressive_house", "Progressive House"),
        ("hardstyle", "Hardstyle"),
        ("tech_house", "Tech House"),
        ("uplifting_trance", "Uplifting Trance"),
        ("electro_house", "Electro House"),
        ("metal_covers", "Метал каверы"),
        ("psy_trance", "Psy Trance"),
        ("hard_phonk", "Жёсткий фонк"),
        ("agressive_phonk", "Агрессивный фонк"),
        ("boyish_phonk", "Пацанский фонк"),
        ("phonk_house", "Phonk House"),
    ]),
    ("medlennye_pesni", "Медленная", [
        ("love_music", "Любовь"),
        ("romantic_music", "Романтика"),
        ("krasivye_pesni", "Красивая"),
        ("soul_music", "Для души"),
        ("nezhnaya", "Нежность"),
        ("happy", "Счастье"),
        ("meditation", "Медитация"),
        ("massage", "Массаж"),
        ("yoga", "Йога"),
        ("dreams", "Мечтание"),
        ("peace", "Спокойствие"),
        ("loneliness", "Одиночество"),
        ("ballads", "Баллады"),
        ("background_music", "Фоновая музыка"),
        ("nature", "Энергия природы"),
        ("soft_soul", "Soft Soul"),
        ("saxophone", "Саксофон"),
        ("lyrical_blues", "Лирика блюза"),
        ("enigmatic", "Enigmatic"),
        ("modern_classical", "Современная классика"),
        ("relaxing_house", "Relaxing Deep House"),
        ("new_age", "New Age"),
        ("smooth_jazz", "Smooth Jazz"),
        ("piano_music", "Музыка пианино"),
        ("atmospheric", "Атмосферная"),
        ("shaman", "Шаманская"),
        ("restoring", "Восстановление энергии"),
        ("thoughts", "Размышления"),
        ("sleep", "Музыка для сна"),
        ("russian_jazz", "Русский джаз"),
    ]),
    ("detskie_pesni", "Детские", [
        ("kids_songs", "Малышам"),
        ("kolybelnye_pesni", "Колыбельные"),
        ("pesni_iz_multfilmov", "Из мультиков"),
        ("sovremennye_detskie_pesni", "Современные"),
        ("pesni_o_more", "Море и корабли"),
        ("animals_songs", "Животные"),
        ("pesni_mama", "Мама"),
        ("pesni_papa", "Папа"),
        ("pesni_babushka", "Бабушка"),
        ("pesni_dedushka", "Дедушка"),
        ("pesni_malchikam", "Мальчикам"),
        ("pesni_devochkam", "Девочкам"),
        ("baby_charging", "Зарядка"),
        ("shkolnie_pesni", "Школа"),
        ("magic", "Волшебство"),
        ("kindness", "Доброта"),
        ("toys", "Игрушки"),
        ("friends", "Друзья"),
        ("childhood_dreams", "Мечты"),
        ("smile", "Улыбка"),
        ("fairytales_heroes", "Герои сказок"),
        ("sky", "Небо и облака"),
        ("child_sun", "Солнце"),
        ("novogodnie_detskie_pesni", "Новогодние детские"),
        ("junior_eurovision_2014", "Детское Евро 2014"),
        ("junior_eurovision_2015", "Детское Евро 2015"),
        ("junior_eurovision_2016", "Детское Евро 2016"),
    ]),
    ("zastolnye_pesni", "Застольные", [
        ("pesni_den_rozhdeniya", "День рождения"),
        ("party_music", "Вечеринка"),
        ("svadebnye_pesni", "Свадьба"),
        ("lezginka", "Лезгинка"),
    ]),
    ("car", "В машину", [
        ("new_music", "Новая"),
        ("powerful_bass", "Мощные басы"),
        ("road_country", "Кантри в дорогу"),
        ("road_rock", "Рок в дорогу"),
        ("russian_phonk", "Русский фонк"),
        ("slow_phonk", "Медленный фонк"),
        ("drift_phonk", "Дрифт-фонк"),
        ("crush_phonk", "Краш фонк"),
        ("garage", "В гараже"),
        ("traffic", "В пробке"),
        ("rudder", "За рулем"),
        ("doroga", "Дорога"),
        ("voditeli", "Водители"),
        ("gibdd", "Гаишники"),
        ("dalnoboyschiki", "Дальнобойщики"),
        ("taxi", "Такси"),
    ]),
    ("sport", "Спорт", [
        ("fitness", "Фитнес"),
        ("run", "Бег"),
        ("motivation", "Мотивация"),
        ("kachalka", "Качалка"),
        ("training", "Спортзал"),
        ("cardio", "Кардиотренировка"),
        ("morning_run", "Утренняя пробежка"),
        ("gymnastics", "Гимнастика"),
        ("exercise", "Зарядка"),
        ("stretching", "Растяжка"),
        ("power_trainings", "Силовая тренировка"),
        ("bike", "Велосипед"),
        ("snowboard", "Сноуборд"),
        ("extreme", "Экстрим"),
        ("basketball", "Баскетбол"),
        ("pilates", "Пилатес"),
        ("meditation", "Медитация"),
        ("diving", "Дайвинг"),
    ]),
    ("world", "Народы мира", [
        ("kavkazskaya", "Музыка Кавказа"),
        ("tatarskie", "Татарские"),
        ("kazakhskie", "Казахские"),
        ("uzbekskie", "Узбекские"),
        ("turetskie", "Турецкие"),
        ("armenian", "Армянские"),
        ("german_dance", "Немецкий дэнс"),
        ("kirgizskie", "Киргизские"),
        ("bashkirskie", "Башкирские"),
        ("chechenskie", "Чеченские"),
        ("italian_rap", "Итальянский рэп"),
        ("czechs_80", "Хиты 80: чешские"),
        ("italian_80", "Хиты 80: итальянские"),
        ("brazilian_disco", "Бразильское диско"),
        ("african", "Африканская"),
        ("east", "Восточная"),
        ("balkan", "Балканская"),
        ("celtic", "Кельтская"),
        ("evreyskie", "Еврейские"),
        ("anthems", "Гимны стран мира"),
    ]),
    ("events", "События и премии", [
        ("evrovidenie-2026", "Евровидение 2026"),
        ("evrovidenie-2025", "Евровидение 2025"),
        ("evrovidenie-2024", "Евровидение 2024"),
        ("evrovidenie-2023", "Евровидение 2023"),
        ("evrovidenie-2022", "Евровидение 2022"),
        ("evrovidenie-2021", "Евровидение 2021"),
        ("evrovidenie-2020", "Евровидение 2020"),
        ("evrovidenie-2019", "Евровидение 2019"),
        ("football_anthems", "Футбольные гимны"),
        ("evrovidenie-2018", "Евровидение 2018"),
        ("oscar_2016", "Оскар 2016"),
        ("grammy_2016", "Грэмми 2016"),
        ("junior_eurovision_2016", "Детское Евро 2016"),
        ("grammy_2015", "Грэмми 2015"),
        ("oscar_2015", "Оскар 2015"),
        ("oscar_2014", "Оскар 2014"),
        ("billboard_2014", "Billboard 2014"),
        ("brit_awards_2014", "Brit Awards 2014"),
        ("junior_eurovision_2014", "Детское Евро 2014"),
        ("junior_eurovision_2015", "Детское Евро 2015"),
    ]),
    ("mood", "Настроения", [
        ("home", "Дома хорошо"),
        ("evil_mood", "Злое настроение"),
        ("lazy", "Ленивые выходные"),
        ("summer_vibe", "Летний вайб"),
        ("positive", "На позитиве"),
        ("good", "У меня все гуд"),
        ("jump", "Прыгать на скакалке"),
        ("relaxed", "Полный релакс"),
        ("walk", "Прогуляться"),
        ("work", "На работе"),
        ("coffee", "Кофебрейк"),
        ("concentration", "Сосредоточение"),
        ("cry", "Хочу плакать"),
        ("happiness", "Радостный день"),
        ("indifference", "Безразличие"),
        ("night_city", "Ночной город"),
        ("calm", "Спокойствие"),
        ("emotional", "Поток эмоций"),
    ]),
    ("woman", "Для женщин", [
        ("pesni_8_marta", "8 марта"),
        ("girlfriend", "Подруги"),
        ("devushki", "Девушки"),
        ("nezhnaya", "Нежность"),
        ("romantic_music", "Романтика"),
    ]),
    ("man", "Для мужчин", [
        ("pesni_23_fevralya", "23 февраля"),
        ("pesni_druzya", "Друзья"),
        ("banya", "Баня"),
        ("hunt", "Охота"),
        ("fishing", "Рыбалка"),
        ("beer", "Пиво"),
        ("wine", "Вино"),
        ("vodka", "Водка"),
        ("alcohol", "Другой алкоголь"),
        ("barbecue", "Шашлык"),
        ("pesni_pod_gitaru", "Гитара"),
        ("parni", "Парни"),
        ("armejskie_pesni", "Армия"),
        ("voennye_pesni", "Война"),
    ]),
    ("nostalgia", "Ностальгия", [
        ("detstvo", "Детство"),
        ("rodina", "Родина"),
        ("memories", "Воспоминания"),
        ("retro_80", "Назад в 80-е"),
        ("sovetskie", "Советские"),
        ("old_country", "Старое кантри"),
        ("hits_50-60", "Хиты 50-х и старше"),
        ("rock_n_roll", "Рок-н-ролл"),
        ("new_york_soul", "New York Soul"),
        ("australian_rock", "Австралийский рок"),
        ("engrock_70", "Зарубежный рок 70-х"),
        ("eng_rock_80", "Рок 80-х: зарубежный"),
        ("blues_rock", "Блюз-рок"),
        ("hair_metal", "Hair Metal 80"),
        ("russian_rock_covers", "Русские рок-каверы"),
        ("pionerskie", "Пионерские"),
        ("perestroyka", "Хиты Перестройки"),
        ("greatest_singers", "Величайшие исполнители"),
        ("road_rock", "Рок в дорогу"),
        ("funk_70", "Фанк 70-х"),
        ("old_soul", "Старый соул"),
        ("old_rock", "Старый рок"),
        ("old_ussr", "Вечные хиты СССР"),
        ("old_blues", "Старый блюз"),
        ("old_jazz", "Старый джаз"),
        ("via_ussr", "Советские ВИА"),
        ("newyear_90", "Новый год 90-х"),
        ("newyear_00", "Новый год 00-х"),
    ]),
    ("vacation", "Отпуск", [
        ("travels", "Путешествие"),
        ("resort", "Курорт"),
        ("beach", "Пляж"),
        ("hike", "Поход"),
        ("mountains", "Горы"),
        ("doroga", "Дорога"),
        ("hitchhiking", "Автостоп"),
        ("barbecue", "Шашлык"),
    ]),
    ("prazdniki", "Праздники", [
        ("pesni_pro_studentov", "День студента"),
        ("pesni-na-den-svjatogo-valentina", "День влюбленных"),
        ("pesni_23_fevralya", "23 февраля"),
        ("blokada_leningrada", "Блокада Ленинграда"),
        ("pesni_8_marta", "8 марта"),
        ("patrick", "День св. Патрика"),
        ("1_aprelya", "1 апреля"),
        ("1_maya", "1 мая"),
        ("maslenitsa", "Масленица"),
        ("radio", "День радио"),
        ("9_maya", "9 мая"),
        ("family", "День семьи"),
        ("russia", "День России"),
        ("friday_13th", "Пятница 13"),
        ("halloween", "Хэллоуин"),
        ("newyear_old", "Старый новый год"),
        ("christmas_music", "Рождество"),
    ]),
    ("vremena_goda", "Времена года", [
        ("pesni_zima", "Зима"),
        ("pesni_vesna", "Весна"),
        ("pesni_pro_leto", "Лето"),
        ("pesni_pro_osen", "Осень"),
    ]),
    ("weather", "Погода", [
        ("dozhd", "Дождь"),
        ("veter", "Ветер"),
        ("sneg", "Снег"),
        ("metel", "Метель"),
        ("moroz", "Мороз"),
    ]),
    ("nature", "Природа", [
        ("sun", "Солнце"),
        ("moon", "Луна"),
        ("nature_sounds", "Звуки природы"),
        ("animals_songs", "Животные"),
    ]),
    ("profession", "Профессии", [
        ("muzykanty", "Музыканты"),
        ("hudozhniki", "Художники"),
        ("journalist", "Журналисты"),
        ("voditeli", "Водители"),
        ("letchiki", "Летчики"),
        ("stuardessa", "Стюардессы"),
        ("railwayman", "Железнодорожники"),
        ("medicine", "Медики"),
        ("povar", "Повара"),
        ("pochta", "Почтальоны"),
        ("gibdd", "Гаишники"),
        ("dalnoboyschiki", "Дальнобойщики"),
    ]),
    ("top_playlists", "Популярные", [
        ("powerful_bass", "Мощные басы"),
        ("love_music", "Любовь"),
        ("pesni_den_rozhdeniya", "День рождения"),
        ("diskoteka_80", "Дискотека 80"),
        ("diskoteka_90", "Дискотека 90"),
        ("rus_pesni_00", "Русские хиты 00-х"),
        ("evrovidenie-2026", "Евровидение 2026"),
        ("kavkazskaya", "Музыка Кавказа"),
        ("kazakhskie", "Казахские"),
        ("saxophone", "Саксофон"),
        ("ballads", "Баллады"),
        ("romantic_music", "Романтика"),
        ("happy", "Счастье"),
        ("svadebnye_pesni", "Свадьба"),
        ("pesni_iz_multfilmov", "Из мультиков"),
        ("sovetskie", "Советские"),
        ("pesni_mama", "Мама"),
        ("fitness", "Фитнес"),
    ]),
]

MP3_RE = re.compile(
    r"https://[a-z0-9.-]*drivemusic\.me/dl/[^\"\s<>]+\.mp3",
    re.IGNORECASE,
)
DATA_URL_RE = re.compile(
    r'data-url="(https://[^"]+\.mp3)"',
    re.IGNORECASE,
)

TRACK_BLOCK_RE = re.compile(
    r'data-url="(?P<stream>https://[^"]+\.mp3)".{0,800}?'
    r'href="(?P<path>(?:https?://(?:ru\.)?drivemusic\.me)?/(?:song|[a-z0-9_]+)/(?P<id>\d+)-[^"]+\.html)"'
    r'[^>]*class="popular-play-author"[^>]*>(?P<title>[^<]+)</a>.{0,600}?'
    r'class="popular-play-composition">(?P<artists>.*?)</div>.{0,500}?'
    r'class="popular-download-number">(?P<dur>[^<]+)',
    re.IGNORECASE | re.DOTALL,
)
SEARCH_ITEM_RE = re.compile(
    r'href="(?P<path>(?:https?://(?:ru\.)?drivemusic\.me)?/(?:song|[a-z0-9_]+)/(?P<id>\d+)-[^"]+\.html)"'
    r'[^>]*class="popular-play-author"[^>]*>(?P<title>[^<]+)</a>.{0,600}?'
    r'class="popular-play-composition">(?P<artists>.*?)</div>',
    re.IGNORECASE | re.DOTALL,
)
PLAYER_ITEM_RE = re.compile(
    r'data-url="(?P<stream>https://[^"]+\.mp3)"[^>]*>.*?'
    r'href="(?P<path>(?:https?://(?:ru\.)?drivemusic\.me)?/(?P<slug>[a-z0-9_-]+)/(?P<id>\d+)-[^"]+\.html)"'
    r'[^>]*class="music-name-text"[^>]*>(?P<title>[^<]+)</a>.*?'
    r'class="player-time"[^>]*>(?P<dur>[^<]+)</span>',
    re.IGNORECASE | re.DOTALL,
)
CARD_LINK_RE = re.compile(
    r'<a href="(?:https?://(?:ru\.)?drivemusic\.me)?/(?P<slug>[a-z0-9_\-]+)/?"[^>]*'
    r'class="picture-text__link"[^>]*>(?P<title>[^<]*)</a>',
    re.IGNORECASE,
)
MAX_PAGE_RE = re.compile(
    r'/(?:artist/)?[a-z0-9_-]+/(?:page[-/])?(?P<n>\d+)\.html|/page/(?P<n2>\d+)/',
    re.IGNORECASE,
)
ARTIST_LINK_RE = re.compile(
    r'href="(?:https?://ru\.drivemusic\.me)?/artist/(?P<slug>[a-z0-9_-]+)\.html"[^>]*>\s*(?P<name>[^<]{1,80})\s*<',
    re.IGNORECASE,
)
ARTIST_CARD_RE = re.compile(
    r'href="(?:https?://ru\.drivemusic\.me)?/artist/(?P<slug>[a-z0-9_-]+)\.html"[^>]*class="picture-text__link"[^>]*>'
    r'(?P<name>[^<]+)</a>.*?(?P<sub>\d[\d\s]*тыс\.\s*слушателей|\d+\s*слушателей)?',
    re.IGNORECASE | re.DOTALL,
)


@dataclass
class DmTrack:
    id: str
    title: str
    artist: str = ""
    duration: int = 0
    page_url: str = ""
    stream_url: str = ""
    album: str = ""


def _parse_duration(s: str) -> int:
    s = (s or "").strip()
    m = re.match(r"(\d+):(\d+)", s)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    return 0


def _artists_from_html(html: str) -> str:
    names = re.findall(r">([^<]+)</a>", html or "")
    names = [n.strip() for n in names if n.strip()]
    if names:
        return ", ".join(names)
    text = re.sub(r"<[^>]+>", " ", html or "")
    text = re.sub(r"\s+", " ", text).replace("&amp;", "&").strip()
    return text


def _title_artist_split(title: str) -> Tuple[str, str]:
    t = (title or "").strip()
    for sep in (" — ", " – ", " - ", " —", "–", " -"):
        if sep in t:
            parts = t.split(sep, 1)
            if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                return parts[1].strip(), parts[0].strip()
    return t, ""


class DriveMusicClient:
    def __init__(
        self,
        cache_sec: int = 300,
        base_url: str = BASE,
        enabled: bool = True,
    ):
        self._enabled = enabled

        # Явный base_url (не дефолтный) — используем только его, без авто-выбора.
        # Дефолтный — стартуем с BASE, потом попробуем зеркала.
        self._base_user_set = bool(base_url) and (base_url.rstrip("/") != BASE)
        self.base = (base_url or BASE).rstrip("/")
        self._base_resolved = self._base_user_set

        self.cache_sec = cache_sec
        self.session = requests.Session()
        # Максимально браузерный набор заголовков — WAF пропускает чаще.
        self.session.headers.update({
            "User-Agent": UA,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8"
            ),
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
        })
        self._search_cache: Dict[str, Tuple[float, List[DmTrack]]] = {}
        self._genre_cache: Dict[str, Tuple[float, List[DmTrack]]] = {}
        self._stream_cache: Dict[str, Tuple[float, str]] = {}
        self._page_cache: Dict[str, str] = {}
        self._unavailable: set = set()
        # подборки группы, подтянутые с сайта: group -> (ts, [(slug, title)])
        self._subs_cache: Dict[str, Tuple[float, List[Tuple[str, str]]]] = {}
        # каталоги, подтянутые с сайта: ключ -> (ts, [(slug, title)])
        self._catalog_cache: Dict[str, Tuple[float, List[Tuple[str, str]]]] = {}
        # треки-превью с карточек подборок: slug -> [DmTrack]
        self._preview_cache: Dict[str, List[DmTrack]] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = bool(value)

    # ---------- авто-выбор зеркала ----------

    def _try_mirror(self, base: str) -> bool:
        """HEAD на hits.html. 200/301/302 — зеркало живое."""
        try:
            r = self.session.head(
                base.rstrip("/") + "/hits.html",
                timeout=8,
                allow_redirects=True,
            )
            return r.status_code < 400
        except Exception as e:
            log.debug("DriveMusic mirror %s: %s", base, e)
            return False

    def _resolve_base(self) -> None:
        """Один раз выбираем рабочее зеркало из списка."""
        if self._base_resolved:
            return
        # 1. Текущий base — если живой, оставляем как есть.
        if self._try_mirror(self.base):
            log.info("DriveMusic: используем %s (первичный)", self.base)
            self._base_resolved = True
            return
        # 2. Перебираем зеркала по порядку.
        for mirror in MIRRORS:
            if mirror == self.base:
                continue
            if self._try_mirror(mirror):
                log.warning(
                    "DriveMusic: %s недоступен, переключаемся на %s",
                    self.base, mirror,
                )
                self.base = mirror
                self._base_resolved = True
                return
        log.error("DriveMusic: ни одно зеркало не отвечает — раздел будет пуст")
        self._base_resolved = True  # чтобы не долбить каждый раз

    # ---------- HTTP ----------

    def _get(self, url: str, referer: Optional[str] = None) -> Optional[str]:
        self._resolve_base()
        headers = {}
        if referer:
            headers["Referer"] = referer
        try:
            r = self.session.get(url, headers=headers, timeout=25)
            if r.status_code != 200:
                log.warning("DriveMusic GET %s → %s", url[:80], r.status_code)
                return None
            r.encoding = r.apparent_encoding or "utf-8"
            return r.text
        except Exception as e:
            log.warning("DriveMusic GET error %s: %s", url[:60], e)
            return None

    def _decode(self, s: str) -> str:
        return (
            (s or "")
            .replace("&amp;", "&")
            .replace("&quot;", '"')
            .replace("&#39;", "'")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&nbsp;", " ")
            .strip()
        )

    def _main_html(self, body: str) -> str:
        if not body:
            return ""
        body = re.sub(r"<aside\b[^>]*>[\s\S]*?</aside>", " ", body, flags=re.I)
        body = re.sub(
            r'class="sidebar[^"]*"[\s\S]*?(?=<div class="main-music"|$)',
            " ",
            body,
            flags=re.I,
        )
        for pat in (
            r'class="[^"]*inline_player_playlist_main[^"]*"[\s\S]*?(?=<div class="l-playlist-page__similar|class="song-comments"|$)',
            r'class="[^"]*music-popular[^"]*"[\s\S]*?(?=<div class="genre-pagination|class="song-comments"|$)',
            r'class="main-music-content"[\s\S]*',
            r'class="playlists-list[\s\S]*',
            r'class="box-player-music[\s\S]*',
        ):
            mm = re.search(pat, body, re.I)
            if mm and ("data-url=" in mm.group(0) or "popular-play-author" in mm.group(0) or "music-name-text" in mm.group(0)):
                return mm.group(0)
        return body

    def _parse_tracks(self, body: str) -> List[DmTrack]:
        body = self._main_html(body)
        results: List[DmTrack] = []
        seen = set()

        for m in TRACK_BLOCK_RE.finditer(body):
            tid = m.group("id")
            if tid in seen:
                continue
            seen.add(tid)
            path = m.group("path")
            title = self._decode(m.group("title"))
            artist = self._decode(_artists_from_html(m.group("artists")))
            dur = _parse_duration(m.group("dur") or "")
            stream = m.group("stream") or ""
            page = urljoin(self.base, path)
            results.append(DmTrack(
                id=tid, title=title or tid, artist=artist,
                duration=dur, page_url=page, stream_url=stream,
            ))
            self._page_cache[tid] = page
            if stream:
                self._stream_cache[tid] = (time.time(), stream)
        if results:
            return results

        for m in PLAYER_ITEM_RE.finditer(body):
            tid = m.group("id")
            if tid in seen:
                continue
            seen.add(tid)
            path = m.group("path")
            raw_title = self._decode(m.group("title"))
            title, artist = _title_artist_split(raw_title)
            dur = _parse_duration(m.group("dur") or "")
            stream = m.group("stream") or ""
            page = urljoin(self.base, path)
            results.append(DmTrack(
                id=tid, title=title or raw_title or tid, artist=artist,
                duration=dur, page_url=page, stream_url=stream,
            ))
            self._page_cache[tid] = page
            if stream:
                self._stream_cache[tid] = (time.time(), stream)
        if results:
            return results

        for m in SEARCH_ITEM_RE.finditer(body):
            tid = m.group("id")
            if tid in seen:
                continue
            path = m.group("path") or ""
            if "/new_music/" in path:
                continue
            seen.add(tid)
            title = self._decode(m.group("title"))
            artist = self._decode(_artists_from_html(m.group("artists")))
            page = urljoin(self.base, path)
            results.append(DmTrack(id=tid, title=title or tid, artist=artist, page_url=page))
            self._page_cache[tid] = page

        if not results:
            for m in re.finditer(
                r'data-url="(?P<stream>https://[^"]+\.mp3)".{0,400}?'
                r'href="(?P<path>[^"]*?/(?P<id>\d+)-[^"]+\.html)"[^>]*>(?P<title>[^<]{2,80})</a>',
                body, re.I | re.DOTALL,
            ):
                tid = m.group("id")
                if tid in seen:
                    continue
                seen.add(tid)
                raw = self._decode(m.group("title"))
                title, artist = _title_artist_split(raw)
                stream = m.group("stream")
                page = urljoin(self.base, m.group("path"))
                results.append(DmTrack(
                    id=tid, title=title or raw or tid, artist=artist,
                    page_url=page, stream_url=stream,
                ))
                self._page_cache[tid] = page
                if stream:
                    self._stream_cache[tid] = (time.time(), stream)

        return results

    def _list_url(self, slug: str, page: int = 1) -> str:
        slug = (slug or "").strip().strip("/")
        if slug.endswith(".html"):
            base_path = slug[:-5]
            if page <= 1:
                return f"{self.base}/{slug}"
            return f"{self.base}/{base_path}/page/{page}/"
        if page <= 1:
            return f"{self.base}/{slug}/"
        return f"{self.base}/{slug}/page/{page}/"

    def search_tracks(self, query: str, limit: int = 40) -> List[DmTrack]:
        q = (query or "").strip()
        if not q:
            return []
        key = q.lower()
        now = time.time()
        if key in self._search_cache:
            ts, tracks = self._search_cache[key]
            if now - ts < self.cache_sec:
                return tracks[:limit]

        encoded = quote(q)
        url = f"{self.base}/?do=search&subaction=search&story={encoded}"
        body = self._get(url, referer=self.base)
        if not body:
            return []

        results = self._parse_tracks(body)
        self._search_cache[key] = (now, results)
        log.info("DriveMusic search %r → %d tracks", q, len(results))
        return results[:limit]

    def get_genre_tracks(self, slug: str, page: int = 1, limit: int = 50) -> List[DmTrack]:
        slug = (slug or "").strip().strip("/")
        if not slug:
            return []

        if page > 1:
            cache_key = f"list:{slug}:{page}"
            now = time.time()
            if cache_key in self._genre_cache:
                ts, tracks = self._genre_cache[cache_key]
                if now - ts < self.cache_sec and tracks:
                    return tracks[:limit]
            body = self._get(self._list_url(slug, page), referer=self.base)
            if not body:
                return []
            results = self._parse_tracks(body)
            if results:
                self._genre_cache[cache_key] = (now, results)
            return results[:limit]

        def url_for_page(p: int) -> str:
            return self._list_url(slug, p)

        tracks = self._fetch_list_pages(
            url_for_page, f"list:{slug}", limit=limit, max_pages=5
        )
        if not tracks:
            # страница подборки не открылась / не разобралась — показываем хотя бы
            # треки-превью с карточки подборки на странице группы
            prev = self._preview_cache.get(slug)
            if prev:
                log.info("DriveMusic %s: страница пуста, отдаю %d превью-треков", slug, len(prev))
                return prev[:limit]
        return tracks

    # ---------- каталоги (динамически с сайта, кэш 1 час, запасной список — встроенный) ----------

    CATALOG_TTL = 3600

    def _dyn(self, key: str, url_path: str, parser, fetch: bool = True):
        """Список со страницы сайта: кэш → сайт → None (если не удалось / fetch=False)."""
        now = time.time()
        hit = self._catalog_cache.get(key)
        if hit and now - hit[0] < max(self.CATALOG_TTL, self.cache_sec):
            return list(hit[1])
        if not fetch or not self._enabled:
            return list(hit[1]) if hit else None
        try:
            body = self._get(f"{self.base}/{url_path.lstrip('/')}", referer=self.base)
        except Exception as e:
            log.warning("DriveMusic %s: %s", url_path, e)
            body = None
        items = parser(body or "") if body else []
        if items:
            self._catalog_cache[key] = (now, items)
            return list(items)
        return list(hit[1]) if hit else None

    @staticmethod
    def _slug_from_href(href: str) -> str:
        """https://host/pop_music/ → pop_music ; https://host/rap.html → rap.html"""
        path = re.sub(r"^https?://[^/]+", "", href or "").strip()
        return path.strip("/")

    def _parse_b_genres(self, body: str, plst: bool) -> List[Tuple[str, str]]:
        """Плитки жанров: <ul class="b_list_genres[ plst]"> … <a class="b_genre">."""
        out: List[Tuple[str, str]] = []
        seen = set()
        for um in re.finditer(r'<ul class="b_list_genres( plst)?">(.*?)</ul>', body, re.S):
            if bool(um.group(1)) != plst:
                continue
            for m in re.finditer(
                r'<a\s+href="([^"]+)"\s+class="b_genre"[^>]*>(.*?)</a>', um.group(2), re.S,
            ):
                slug = self._slug_from_href(m.group(1))
                title = self._decode(re.sub(r"<[^>]+>", "", m.group(2)))
                if not slug or not title or slug in seen:
                    continue
                seen.add(slug)
                out.append((slug, title))
        return out

    def _parse_playlist_links(self, body: str) -> List[Tuple[str, str]]:
        out: List[Tuple[str, str]] = []
        seen = set()
        for m in re.finditer(
            r'<a\b[^>]*?(?:class="playlists-list__link[^"]*"[^>]*?href="([^"]+)"|'
            r'href="([^"]+)"[^>]*?class="playlists-list__link[^"]*")[^>]*>([^<]+)</a>', body,
        ):
            slug = self._slug_from_href(m.group(1) or m.group(2))
            title = self._decode(m.group(3))
            if slug and title and slug not in seen:
                seen.add(slug)
                out.append((slug, title))
        return out

    @staticmethod
    def _year_sorted(items: List[Tuple[str, str]], prefix: str) -> List[Tuple[str, str]]:
        """Не-годы (Избранные, Популярные, Все годы…) сверху, годы по убыванию («Хиты 2016»)."""
        plain = [(s, t) for s, t in items if not t.isdigit()]
        years = sorted(((s, f"{prefix} {t}") for s, t in items if t.isdigit()),
                       key=lambda x: x[1], reverse=True)
        return plain + years

    def list_genres(self, fetch: bool = True) -> List[Tuple[str, str]]:
        res = self._dyn(
            "genres", "genres.html",
            lambda b: [(s, t[:1].upper() + t[1:]) for s, t in self._parse_b_genres(b, plst=False)],
            fetch,
        )
        return res or list(GENRES)

    def list_hits(self, fetch: bool = True) -> List[Tuple[str, str]]:
        res = self._dyn(
            "hits", "hits.html",
            lambda b: self._year_sorted(self._parse_playlist_links(b), "Хиты"), fetch,
        )
        return res or list(HITS)

    def list_best_music(self, fetch: bool = True) -> List[Tuple[str, str]]:
        res = self._dyn(
            "best", "best_music.html",
            lambda b: self._year_sorted(self._parse_playlist_links(b), "Лучшая"), fetch,
        )
        return res or list(BEST_MUSIC)

    def list_playlist_groups(self, fetch: bool = True) -> List[Tuple[str, str]]:
        """Группы подборок со страницы /playlists.html (+ проверенные встроенные, которых там нет)."""
        static = [(slug, title) for slug, title, _ in PLAYLIST_GROUPS]
        names = dict(static)

        def parse(b: str):
            found = []
            for slug, text in self._parse_b_genres(b, plst=True):
                slug = slug[:-5] if slug.endswith(".html") else slug
                found.append((slug, names.get(slug) or f"Для {text}"))
            return found

        res = self._dyn("groups", "playlists.html", parse, fetch)
        if not res:
            return static
        have = {s for s, _ in res}
        return res + [(s, t) for s, t in static if s not in have]

    def _static_subs(self, group_slug: str) -> List[Tuple[str, str]]:
        for slug, _title, subs in PLAYLIST_GROUPS:
            if slug == group_slug:
                return list(subs)
        return []

    def _parse_playlist_cards(self, body: str):
        """Карточки подборок со страницы группы → [(slug, title, [превью-треки])]."""
        cards = []
        seen = set()
        for part in (body or "").split('class="box-player__item"')[1:]:
            m = CARD_LINK_RE.search(part[:4000])
            if not m:
                continue          # хвост страницы (комментарии и т.п.)
            slug = m.group("slug")
            if slug in seen:
                continue
            seen.add(slug)
            title = self._decode(m.group("title")) or slug
            try:
                previews = self._parse_tracks(part)
            except Exception:
                previews = []
            cards.append((slug, title, previews))
        return cards

    def list_playlist_subs(self, group_slug: str, fetch: bool = True) -> List[Tuple[str, str]]:
        """Подборки группы. Берём актуальный список с сайта, при сбое — встроенный.

        fetch=False — без сети (для подсчёта childCount в списке групп)."""
        group_slug = (group_slug or "").strip().strip("/").replace(".html", "")
        if not group_slug:
            return []
        now = time.time()
        hit = self._subs_cache.get(group_slug)
        if hit and now - hit[0] < max(self.cache_sec, 3600):
            return list(hit[1])
        static = self._static_subs(group_slug)
        if not fetch or not self._enabled:
            return list(hit[1]) if hit else static
        try:
            body = self._get(f"{self.base}/{group_slug}.html", referer=self.base)
            cards = self._parse_playlist_cards(body) if body else []
        except Exception as e:
            log.warning("DriveMusic group %s: %s", group_slug, e)
            cards = []
        if cards:
            for slug, _t, previews in cards:
                if previews:
                    self._preview_cache[slug] = previews
            subs = [(slug, title) for slug, title, _p in cards]
            self._subs_cache[group_slug] = (now, subs)
            return list(subs)
        # сайт не ответил — отдаём встроенный список, но не кэшируем надолго
        return static

    def list_playlists(self) -> List[Tuple[str, str]]:
        out: List[Tuple[str, str]] = []
        for _gs, gt, subs in PLAYLIST_GROUPS:
            out.append((_gs, gt))
            for ss, st in subs:
                out.append((ss, f"{gt} / {st}"))
        return out

    def search_artists(self, query: str, limit: int = 20) -> List[dict]:
        q = (query or "").strip()
        if not q:
            return []
        encoded = quote(q)
        url = f"{self.base}/?do=search&subaction=search&story={encoded}"
        body = self._get(url, referer=self.base)
        if not body:
            return []

        by_slug: Dict[str, dict] = {}
        for m in ARTIST_CARD_RE.finditer(body):
            slug = m.group("slug").lower()
            name = self._decode(m.group("name"))
            if slug and name:
                by_slug[slug] = {"id": slug, "name": name, "nb_fan": 0}
        for m in ARTIST_LINK_RE.finditer(body):
            slug = m.group("slug").lower()
            name = self._decode(m.group("name"))
            if slug and name and slug not in by_slug:
                by_slug[slug] = {"id": slug, "name": name, "nb_fan": 0}
        return list(by_slug.values())[:limit]

    def get_artist_tracks(self, slug: str, limit: int = 200) -> List[DmTrack]:
        slug = (slug or "").strip().strip("/").replace(".html", "")
        if not slug:
            return []

        def url_for_page(p: int) -> str:
            if p <= 1:
                return f"{self.base}/artist/{slug}.html"
            return f"{self.base}/artist/{slug}/page-{p}.html"

        return self._fetch_list_pages(
            url_for_page, f"artist:{slug}", limit=limit, max_pages=15
        )

    def _max_page_from_html(self, body: str) -> int:
        if not body:
            return 1
        nums = []
        for m in MAX_PAGE_RE.finditer(body):
            n = m.group("n") or m.group("n2")
            if n:
                try:
                    nums.append(int(n))
                except ValueError:
                    pass
        return max(nums) if nums else 1

    def _fetch_list_pages(
        self,
        url_for_page,
        cache_prefix: str,
        limit: int = 50,
        max_pages: int = 5,
    ) -> List[DmTrack]:
        now = time.time()
        cache_key = f"{cache_prefix}:1"
        if cache_key in self._genre_cache:
            ts, tracks = self._genre_cache[cache_key]
            if now - ts < self.cache_sec and tracks:
                return tracks[:limit]

        all_tracks: List[DmTrack] = []
        seen = set()
        detected_max = max_pages

        for p in range(1, max_pages + 1):
            if p > detected_max:
                break
            body = self._get(url_for_page(p), referer=self.base)
            if not body:
                break
            if p == 1:
                detected_max = min(max_pages, max(1, self._max_page_from_html(body)))
            page_tracks = self._parse_tracks(body)
            if not page_tracks:
                break
            for tr in page_tracks:
                if tr.id not in seen:
                    seen.add(tr.id)
                    all_tracks.append(tr)
            if len(all_tracks) >= limit:
                break

        if all_tracks:
            self._genre_cache[cache_key] = (now, all_tracks)
        log.info(
            "DriveMusic pages %s → %d tracks (pages≤%s)",
            cache_prefix, len(all_tracks), detected_max,
        )
        return all_tracks[:limit]

    def resolve_stream_url(self, track_id: str, page_url: str = "") -> Optional[str]:
        tid = str(track_id)
        if tid in self._unavailable:
            return None
        now = time.time()
        if tid in self._stream_cache:
            ts, url = self._stream_cache[tid]
            if now - ts < self.cache_sec and url:
                return url

        url = page_url or self._page_cache.get(tid)
        if not url:
            log.warning("DriveMusic: no page_url for track %s", tid)
            return None

        body = self._get(url, referer=self.base)
        if not body:
            self._unavailable.add(tid)
            return None

        data_urls = DATA_URL_RE.findall(body)
        urls = data_urls + MP3_RE.findall(body)
        if not urls:
            log.warning("DriveMusic: no mp3 links on %s", url[:80])
            self._unavailable.add(tid)
            return None
        urls = list(dict.fromkeys(urls))
        online = [u for u in urls if "/dl/online/" in u]
        chosen = online[0] if online else urls[0]
        self._stream_cache[tid] = (now, chosen)
        return chosen

    def open_stream(self, track_id: str, page_url: str = ""):
        self._resolve_base()
        stream_url = self.resolve_stream_url(track_id, page_url)
        if not stream_url:
            return None
        try:
            headers = {
                "User-Agent": UA,
                "Referer": page_url or self.base,
                "Origin": self.base,
                "Accept": "audio/mpeg,application/octet-stream,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                "Sec-Fetch-Dest": "audio",
                "Sec-Fetch-Mode": "no-cors",
                "Sec-Fetch-Site": "same-origin",
            }
            r = self.session.get(
                stream_url, headers=headers, stream=True, timeout=30
            )
            if r.status_code != 200:
                log.warning("DriveMusic stream %s → %s", track_id, r.status_code)
                r.close()
                self._unavailable.add(str(track_id))
                return None
            mime = r.headers.get("Content-Type") or "audio/mpeg"
            cl = r.headers.get("Content-Length")
            content_length = int(cl) if cl and cl.isdigit() else None
            return r, mime, content_length, True
        except Exception as e:
            log.warning("DriveMusic open_stream %s: %s", track_id, e)
            return None