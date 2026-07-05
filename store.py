"""
Персистентное состояние: пользователи (Я.Музыка аккаунты, вошедшие через
OAuth) и их шаринг-ссылки (slug -> плейлист конкретного пользователя).

Простое JSON-хранилище — без БД, в духе остального проекта. Путь настраивается
через STATE_FILE (по умолчанию state.json рядом с проектом). В Docker должен
быть смонтирован как volume, иначе состояние теряется при пересборке образа.
"""

import json
import os
import secrets
import stat
import threading
from datetime import datetime, timezone

_PATH = os.environ.get("STATE_FILE", os.path.join(os.path.dirname(__file__), "state.json"))
_LOCK = threading.Lock()


def _empty_state() -> dict:
    return {"users": {}, "links": {}}


def _load() -> dict:
    try:
        with open(_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return _empty_state()

    data.setdefault("users", {})
    data.setdefault("links", {})
    return data


def _save(data: dict) -> None:
    # Атомарная запись: пишем во временный файл рядом и переименовываем —
    # чтобы конкурентное чтение никогда не увидело битый/недописанный JSON.
    tmp_path = f"{_PATH}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    # Файл содержит боевые токены всех пользователей — читать может только
    # владелец процесса. На Windows это no-op (нет POSIX-битов), но безвредно;
    # в Docker/Linux прод-деплое реально ограничивает права.
    os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp_path, _PATH)


def get_user(uid: str) -> dict | None:
    """Возвращает {token, login} для аккаунта или None, если не логинился."""
    return _load().get("users", {}).get(str(uid))


def set_user(uid: str, token: str, login: str = "") -> None:
    with _LOCK:
        data = _load()
        data["users"][str(uid)] = {"token": token, "login": login}
        _save(data)


def list_links_for_user(uid: str) -> dict:
    """Возвращает {slug: {label, playlist, owner, created}} только этого пользователя."""
    uid = str(uid)
    return {
        slug: link
        for slug, link in _load().get("links", {}).items()
        if link.get("owner") == uid
    }


def get_link(slug: str) -> dict | None:
    return _load().get("links", {}).get(slug)


def add_link(owner_uid: str, playlist_ref: str, label: str) -> str:
    """label — название плейлиста на момент создания ссылки (для отображения)."""
    with _LOCK:
        data = _load()
        slug = secrets.token_urlsafe(6)
        while slug in data["links"]:
            slug = secrets.token_urlsafe(6)
        data["links"][slug] = {
            "label": label,
            "playlist": playlist_ref,
            "owner": str(owner_uid),
            "created": datetime.now(timezone.utc).isoformat(),
        }
        _save(data)
    return slug


def delete_link(slug: str, owner_uid: str) -> bool:
    """Удаляет ссылку, только если она принадлежит owner_uid. Возвращает успех."""
    with _LOCK:
        data = _load()
        link = data["links"].get(slug)
        if not link or link.get("owner") != str(owner_uid):
            return False
        data["links"].pop(slug)
        _save(data)
        return True
