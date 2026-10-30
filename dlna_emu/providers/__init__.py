"""Сервис-провайдеры ContentDirectory (один класс = одна площадка)."""
from .base import ServiceProvider
from .radio import RadioProvider
from .files import FilesProvider
from .netstorage import NetStorageProvider
from .deezer import DeezerProvider
from .drivemusic import DriveMusicProvider
from .akniga import AknigaProvider
from .yandex import YandexProvider
from .zvuk import ZvukProvider
from .kion import KionProvider
from .promodj import PromoDjProvider
from .zaycev import ZaycevProvider

__all__ = [
    "ServiceProvider",
    "RadioProvider",
    "FilesProvider",
    "NetStorageProvider",
    "DeezerProvider",
    "DriveMusicProvider",
    "AknigaProvider",
    "YandexProvider",
    "ZvukProvider",
    "KionProvider",
    "PromoDjProvider",
    "ZaycevProvider",
]
