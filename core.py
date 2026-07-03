"""
Ядро приложения: разбор ссылки на трек Я.Музыки и добавление его
в заранее заданный плейлист.

Используется неофициальная библиотека yandex-music (MarshalX):
https://github.com/MarshalX/yandex-music-api

Одна и та же логика применяется и в CLI (фаза 1), и в веб-оболочке (фаза 2).
"""

import os
import re

from yandex_music import Client


# --- Настройки (можно захардкодить или задать через переменные окружения) ---

# Токен аккаунта, которому принадлежит плейлист.
# Как получить — см. get_token.py и README.md.
TOKEN = os.environ.get("YM_TOKEN", "ВСТАВЬ_СЮДА_ТОКЕН")

# Плейлист, куда падают треки. Можно указать в любом виде:
#   - UUID:        41cb8a47-dc72-3f22-a151-f6ea27c34464
#   - полный URL:  https://music.yandex.ru/playlists/41cb8a47-dc72-3f22-a151-f6ea27c34464
#   - числовой kind (старый формат ссылок /users/<login>/playlists/<kind>)
PLAYLIST = os.environ.get(
    "YM_PLAYLIST",
    "https://music.yandex.ru/playlists/41cb8a47-dc72-3f22-a151-f6ea27c34464",
)


class TrackLinkError(ValueError):
    """Не удалось распознать ссылку на трек."""


class PlaylistError(RuntimeError):
    """Не удалось получить плейлист."""


# --- Разбор ссылки на трек ---

# Поддерживаемые форматы ссылок на трек:
#   https://music.yandex.ru/album/3192570/track/1710808
#   https://music.yandex.ru/track/1710808
#   домены .ru / .com / .by / .kz
_ALBUM_TRACK = re.compile(r"/album/(\d+)/track/(\d+)")
_TRACK_ONLY = re.compile(r"/track/(\d+)")


def parse_track_link(link: str):
    """Возвращает (track_id, album_id|None) из ссылки на трек."""
    link = link.strip()

    m = _ALBUM_TRACK.search(link)
    if m:
        return m.group(2), m.group(1)  # track_id, album_id

    m = _TRACK_ONLY.search(link)
    if m:
        return m.group(1), None

    raise TrackLinkError(
        "Не похоже на ссылку трека Я.Музыки. Пример: "
        "https://music.yandex.ru/album/3192570/track/1710808"
    )


# --- Разбор ссылки/идентификатора плейлиста ---

# UUID вида 41cb8a47-dc72-3f22-a151-f6ea27c34464
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                   r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def parse_playlist_ref(ref):
    """
    Приводит ссылку/идентификатор плейлиста к одному из двух видов:
      ('uuid', '<uuid>')  или  ('kind', <int>)
    """
    ref = str(ref).strip()

    m = _UUID.search(ref)
    if m:
        return "uuid", m.group(0)

    # старый формат: .../playlists/<kind> либо просто число
    m = re.search(r"/playlists/(\d+)", ref)
    if m:
        return "kind", int(m.group(1))
    if ref.isdigit():
        return "kind", int(ref)

    raise PlaylistError(f"Не понял, что за плейлист: {ref!r}")


def make_client(token: str | None = None) -> Client:
    """Создаёт и инициализирует клиент Я.Музыки."""
    token = token or TOKEN
    if not token or token == "ВСТАВЬ_СЮДА_ТОКЕН":
        raise RuntimeError(
            "Не задан токен. Укажи переменную окружения YM_TOKEN "
            "или впиши токен в core.py. Получить токен: python get_token.py"
        )
    return Client(token).init()


def resolve_playlist(client: Client, ref=PLAYLIST):
    """
    Достаёт объект плейлиста по UUID, числовому kind или URL.
    У объекта есть .kind, .revision, .owner.uid, .tracks — всё, что нужно
    для вставки трека.
    """
    kind_type, value = parse_playlist_ref(ref)

    if kind_type == "uuid":
        playlist = client.playlist(value)
    else:  # kind
        playlist = client.users_playlists(value)

    if not playlist:
        raise PlaylistError(f"Плейлист не найден: {ref!r}")
    return playlist


def _resolve_album_id(client: Client, track_id: str, album_id: str | None) -> str:
    """Если album_id не был в ссылке — узнаём его через API по track_id."""
    if album_id:
        return album_id
    tracks = client.tracks([track_id])
    if not tracks or not tracks[0].albums:
        raise TrackLinkError(f"Не удалось определить альбом для трека {track_id}")
    return str(tracks[0].albums[0].id)


def add_track_to_playlist(
    link: str,
    client: Client | None = None,
    playlist_ref=PLAYLIST,
) -> dict:
    """
    Добавляет трек по ссылке в плейлист.

    Возвращает словарь с результатом: имя трека, исполнители,
    и флаг already_exists, если трек уже был в плейлисте.
    """
    client = client or make_client()

    track_id, album_id = parse_track_link(link)
    album_id = _resolve_album_id(client, track_id, album_id)

    # Человекочитаемое название трека (не обязательно, но приятно в ответе).
    track = client.tracks([f"{track_id}:{album_id}"])[0]
    title = track.title or f"track {track_id}"
    artists = ", ".join(a.name for a in track.artists) or "—"

    # Актуальное состояние плейлиста: нужен revision (иначе API отклонит
    # вставку) и текущий список треков для проверки на дубликат.
    playlist = resolve_playlist(client, playlist_ref)
    owner_uid = playlist.owner.uid if playlist.owner else None

    existing_ids = {str(t.id) for t in (playlist.tracks or [])}
    if str(track_id) in existing_ids:
        return {
            "ok": True,
            "already_exists": True,
            "title": title,
            "artists": artists,
            "playlist": playlist.title,
        }

    client.users_playlists_insert_track(
        kind=playlist.kind,
        track_id=track_id,
        album_id=album_id,
        revision=playlist.revision,
        user_id=owner_uid,
    )

    return {
        "ok": True,
        "already_exists": False,
        "title": title,
        "artists": artists,
        "playlist": playlist.title,
    }
