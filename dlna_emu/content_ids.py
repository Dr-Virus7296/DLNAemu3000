"""Идентификаторы контейнеров ContentDirectory (общие для tree и провайдеров)."""

ROOT = "0"
RADIO = "radio"
# Папка «Ваше популярное радио». id с «__» не может совпасть с плейлистом (_safe_id их срезает)
RADIO_POPULAR = "radio:__popular__"
FILES = "files"
LOCALPL = "localpl"
NET = "net"
DEEZER = "deezer"
DZ_MINE = "deezer:mine"
DZ_EXPLORE = "deezer:explore"
DZ_SAVED = "deezer:saved"
DZ_FLOW = "deezer:flow"
DZ_FLOW_MOODS = "deezer:flow:moods"
DZ_FLOW_GENRES = "deezer:flow:genres"
DRIVEMUSIC = "drivemusic"
DM_GENRES = "drivemusic:genres"
DM_PLAYLISTS = "drivemusic:playlists"
DM_NEW = "drivemusic:new"
DM_SAVED = "drivemusic:saved"
DM_HITS = "drivemusic:hits"
DM_BEST = "drivemusic:best"
DM_PLGROUP_PREFIX = "drivemusic:plgroup:"
DM_PL_PREFIX = "drivemusic:pl:"
DM_GENRE_PREFIX = "drivemusic:genre:"
DM_ARTIST_PREFIX = "drivemusic:artist:"
DM_TR_PREFIX = "drivemusic:tr:"
DM_HIT_PREFIX = "drivemusic:hit:"
DM_BEST_PREFIX = "drivemusic:bestitem:"
AKNIGA = "akniga"
AK_HOME = "akniga:home"
AK_GENRES = "akniga:genres"
AK_SAVED = "akniga:saved"
AK_FAV = "akniga:favorites"
AK_GENRE_PREFIX = "akniga:genre:"
AK_SECPAGE_PREFIX = "akniga:secpage:"   # «Страница N» внутри раздела
AK_BOOK_PREFIX = "akniga:book:"
YANDEX = "yandex"
YA_COLLECTION = "yandex:collection"
YA_LIKES = "yandex:collection:likes"
YA_USER_PL_PREFIX = "yandex:collection:pl:"
YA_LIKED_PL_PREFIX = "yandex:collection:likedpl:"
YA_SAVED = "yandex:saved"
YA_WAVE = "yandex:wave"
YA_WAVE_PRESET_PREFIX = "yandex:wave:"
YA_CATALOG = "yandex:catalog"
YA_SECTION_PREFIX = "yandex:section:"
YA_TAG_PREFIX = "yandex:tag:"
YA_PL_PREFIX = "yandex:pl:"
YA_ARTIST_PREFIX = "yandex:artist:"
YA_ALBUM_PREFIX = "yandex:album:"
YA_TR_PREFIX = "yandex:tr:"
# Разделы как на сайте music.yandex.ru (строятся динамически из /landing/skeleton)
YA_LP_PAGE_PREFIX = "yandex:lp:"     # yandex:lp:{foryou|trends|nonmusic|kids}
YA_LP_TAB_PREFIX = "yandex:lt:"      # yandex:lt:{page}:{номер вкладки}
YA_LP_SUB_PREFIX = "yandex:lx:"      # yandex:lx:{id вложенной страницы}
YA_LP_BLOCK_PREFIX = "yandex:lb:"    # yandex:lb:{blockId} — полка
YA_LP_GROUP_PREFIX = "yandex:lg:"    # yandex:lg:{blockId}:{номер} — группа внутри полки («В стиле»)

# ===== KION (КИОН Музыка / МТС) =====
KION = "kion"
KION_COLLECTION = "kion:collection"
KION_LIKES = "kion:collection:likes"
KION_USER_PL_PREFIX = "kion:collection:pl:"
KION_LIKED_PL_PREFIX = "kion:collection:likedpl:"
KION_SAVED = "kion:saved"
KION_WAVE = "kion:wave"
KION_WAVE_PRESET_PREFIX = "kion:wave:"
KION_CATALOG = "kion:catalog"
KION_SECTION_PREFIX = "kion:section:"
KION_TAG_PREFIX = "kion:tag:"
KION_PL_PREFIX = "kion:pl:"
KION_ARTIST_PREFIX = "kion:artist:"
KION_ALBUM_PREFIX = "kion:album:"
KION_TR_PREFIX = "kion:tr:"
# Разделы как на сайте КИОН
KION_ACTUAL = "kion:actual"              # Актуально сейчас (лента)
KION_ACTUAL_EVENT_PREFIX = "kion:actual:ev:"
KION_CHART = "kion:chart"                # Чарт треков
KION_CHART_REAL = "kion:chart:real"      # Настоящий чарт (плейлист)
KION_COLLECTIONS = "kion:collections"    # Подборки
KION_COLLECTIONS_PL_PREFIX = "kion:collections:pl:"
KION_PODCASTS = "kion:podcasts"          # Чарт подкастов
KION_PODCAST_PREFIX = "kion:podcast:"


# ===== Zvuk (Звук) =====
ZVUK = "zvuk"
ZV_COLLECTION = "zvuk:collection"
ZV_LIKES = "zvuk:collection:likes"
ZV_USER_PL_PREFIX = "zvuk:collection:pl:"
ZV_SAVED = "zvuk:saved"
ZV_PL_PREFIX = "zvuk:pl:"
ZV_ARTIST_PREFIX = "zvuk:artist:"
ZV_ALBUM_PREFIX = "zvuk:album:"
ZV_TR_PREFIX = "zvuk:tr:"
ZV_GENRES = "zvuk:genres"
ZV_GENRE_PREFIX = "zvuk:genre:"
ZV_GTAB_PREFIX = "zvuk:gtab:"   # genre:tab playlists list
ZV_CURATED = "zvuk:curated"
ZV_CURATED_PREFIX = "zvuk:curated:"
ZV_ED_PROF_PREFIX = "zvuk:edprof:"

# PromoDJ
PROMODJ = "promodj"
PD_FEATURED = "promodj:featured"
PD_MUSIC = "promodj:music"
PD_TOP100 = "promodj:top100"
PD_GENRES = "promodj:genres"
PD_SEARCH = "promodj:search"
PD_TYPE_PREFIX = "promodj:type:"          # type slug (tracks/mixes/...)
PD_GENRE_PREFIX = "promodj:genre:"        # genre slug
PD_FEAT_PREFIX = "promodj:feat:"          # featured subsection
PD_TOP_PREFIX = "promodj:top:"            # список TOP 100: promodj:top:{type}/{period} или {type}/{style}/{period}
PD_TOPT_PREFIX = "promodj:topt:"          # тип TOP 100 → периоды
PD_TOPP_PREFIX = "promodj:topp:"          # период TOP 100 → «Все стили» + стили
PD_TALL_PREFIX = "promodj:tall:"          # все треки раздела музыки
PD_TGEN_PREFIX = "promodj:tgen:"          # жанры раздела музыки
PD_TG_PREFIX = "promodj:tg:"              # жанр раздела: promodj:tg:{type}/{genre}
PD_PAGE_PREFIX = "promodj:page:"          # paginated list: promodj:page:{kind}:{key}:{page}
PD_TR_PREFIX = "promodj:tr:"              # track id
PD_SRCH_PREFIX = "promodj:srch:"          # search query pages
PD_SAVED = "promodj:saved"                # сохранённые артисты из WEB
PD_USER_PREFIX = "promodj:user:"          # артист по slug

# ===== Zaycev.net =====
ZAYCEV = "zaycev"
ZY_SAVED = "zaycev:saved"                 # 🔍 Мои поиски (артисты из веб-поиска)
ZY_NEW = "zaycev:new"                     # Новинки
ZY_TOP = "zaycev:top"                     # Топ (день / неделя / месяц)
ZY_GENRES = "zaycev:genres"               # Жанры → «Жанры» + «Сборники по жанрам»
ZY_GENRES_ALL = "zaycev:genlist"          # все жанры и поджанры (динамически с сайта)
ZY_SETS = "zaycev:sets"                   # Подборки (категории динамически)
ZY_GENRE_PREFIX = "zaycev:genre:"         # zaycev:genre:{slug} — жанр / поджанр
ZY_SETCAT_PREFIX = "zaycev:setcat:"       # zaycev:setcat:{typeId} — категория подборок
ZY_SETPAGE_PREFIX = "zaycev:setpage:"     # zaycev:setpage:{typeId}:{chunk}
ZY_LIST_PREFIX = "zaycev:l:"              # zaycev:l:{kind}:{key} — список треков
ZY_PAGE_PREFIX = "zaycev:page:"           # zaycev:page:{kind}:{key}:{chunk}
ZY_ARTIST_PREFIX = "zaycev:artist:"       # сохранённый артист
ZY_TR_PREFIX = "zaycev:tr:"               # трек

SAVED_LIMIT = 40
AK_FAVORITES_LIMIT = 500

FLOW_MAX_PAGE = 50
# Виртуальный плейлист Flow: ресивер один раз читает каталог (100 слотов),
# реальные треки подгружаются с Deezer по 3 при прослушивании.
FLOW_MAX_CACHE = 100
FLOW_GROW_LIMIT = 8
FLOW_PREFETCH_MIN = 3
