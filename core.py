"""
Ядро приложения: разбор ссылки на трек Я.Музыки и добавление его
в заранее заданный плейлист.

Используется неофициальная библиотека yandex-music (MarshalX):
https://github.com/MarshalX/yandex-music-api

Вся логика вынесена сюда и используется веб-оболочкой (web.py).
"""

import os
import re

from dotenv import load_dotenv
from yandex_music import Client

# Подхватываем .env, если он есть рядом (для локального запуска без Docker —
# там переменные окружения передаёт docker-compose, а не этот вызов).
load_dotenv()


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
# Ссылка на альбом целиком: https://music.yandex.ru/album/3192570
_ALBUM_ONLY = re.compile(r"/album/(\d+)(?:[/?#]|$)")


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


def classify_link(link: str):
    """
    Определяет, что за ссылка: трек или альбом целиком.

    Возвращает ('track', track_id, album_id|None) или ('album', album_id, None).
    """
    link = link.strip()

    m = _ALBUM_TRACK.search(link)
    if m:
        return "track", m.group(2), m.group(1)

    m = _TRACK_ONLY.search(link)
    if m:
        return "track", m.group(1), None

    m = _ALBUM_ONLY.search(link)
    if m:
        return "album", m.group(1), None

    raise TrackLinkError(
        "Не похоже на ссылку трека или альбома Я.Музыки. Примеры: "
        "https://music.yandex.ru/album/3192570/track/1710808 (трек), "
        "https://music.yandex.ru/album/3192570 (альбом целиком)"
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


def playlist_url(playlist) -> str:
    """Ссылка на плейлист в вебе Я.Музыки."""
    if playlist.playlist_uuid:
        return f"https://music.yandex.ru/playlists/{playlist.playlist_uuid}"
    if playlist.owner and playlist.kind is not None:
        return f"https://music.yandex.ru/users/{playlist.owner.uid}/playlists/{playlist.kind}"
    return ""


def get_playlist_tracks(client: Client | None = None, playlist_ref=PLAYLIST) -> dict:
    """
    Возвращает содержимое плейлиста: название, ссылку и список треков
    (title, artists) в том порядке, в котором они лежат в плейлисте
    (новые — первыми, т.к. add_track_to_playlist вставляет в начало).
    """
    client = client or make_client()
    playlist = resolve_playlist(client, playlist_ref)

    info = {"playlist": playlist.title, "playlist_url": playlist_url(playlist)}

    short_tracks = playlist.tracks or []
    if not short_tracks:
        return {**info, "tracks": []}

    full_tracks = client.tracks([t.track_id for t in short_tracks])
    tracks = [
        {
            "title": t.title or f"track {t.id}",
            "artists": ", ".join(a.name for a in t.artists) or "—",
        }
        for t in full_tracks
    ]
    return {**info, "tracks": tracks}


def _album_with_tracks(client: Client, album_id: str):
    album = client.albums_with_tracks(album_id)
    if not album:
        raise TrackLinkError(f"Альбом не найден: {album_id}")
    return album


def preview_album(
    album_id: str,
    client: Client | None = None,
    playlist_ref=PLAYLIST,
) -> dict:
    """
    Возвращает название альбома и список его треков (title, artists,
    already_exists) — для показа диалога подтверждения перед добавлением.
    """
    client = client or make_client()
    album = _album_with_tracks(client, album_id)
    playlist = resolve_playlist(client, playlist_ref)
    existing_ids = {str(t.id) for t in (playlist.tracks or [])}

    tracks = [
        {
            "title": track.title or f"track {track.id}",
            "artists": ", ".join(a.name for a in track.artists) or "—",
            "already_exists": str(track.id) in existing_ids,
        }
        for volume in (album.volumes or [])
        for track in volume
    ]

    return {
        "album": album.title or f"альбом {album_id}",
        "tracks": tracks,
    }


def add_album_to_playlist(
    album_id: str,
    client: Client | None = None,
    playlist_ref=PLAYLIST,
) -> dict:
    """
    Добавляет все треки альбома в плейлист (уже имеющиеся — пропускает).
    Возвращает название альбома и списки добавленных/уже существующих треков.
    """
    client = client or make_client()
    album = _album_with_tracks(client, album_id)

    added = []
    already_exists = []

    for volume in album.volumes or []:
        for track in volume:
            link = f"https://music.yandex.ru/album/{album_id}/track/{track.id}"
            result = add_track_to_playlist(link, client=client, playlist_ref=playlist_ref)
            entry = {"title": result["title"], "artists": result["artists"]}
            (already_exists if result["already_exists"] else added).append(entry)

    return {
        "album": album.title or f"альбом {album_id}",
        "added": added,
        "already_exists": already_exists,
    }
