"""
Персистентное состояние админки: токен, полученный через OAuth-логин
в браузере, и шаринг-ссылки (slug -> плейлист).

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
    return {"token": None, "links": {}}


def _load() -> dict:
    try:
        with open(_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return _empty_state()

    data.setdefault("token", None)
    data.setdefault("links", {})
    return data


def _save(data: dict) -> None:
    # Атомарная запись: пишем во временный файл рядом и переименовываем —
    # чтобы конкурентное чтение никогда не увидело битый/недописанный JSON.
    tmp_path = f"{_PATH}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    # Файл содержит боевой токен — читать может только владелец процесса.
    # На Windows это no-op (нет POSIX-битов), но безвредно; в Docker/Linux
    # прод-деплое реально ограничивает права.
    os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp_path, _PATH)


def get_token() -> str | None:
    return _load().get("token")


def set_token(token: str) -> None:
    with _LOCK:
        data = _load()
        data["token"] = token
        _save(data)


def list_links() -> dict:
    """Возвращает {slug: {label, playlist, created}}."""
    return _load().get("links", {})


def get_link(slug: str) -> dict | None:
    return list_links().get(slug)


def add_link(label: str, playlist_ref: str, playlist_title: str = "") -> str:
    with _LOCK:
        data = _load()
        slug = secrets.token_urlsafe(6)
        while slug in data["links"]:
            slug = secrets.token_urlsafe(6)
        data["links"][slug] = {
            "label": label,
            "playlist": playlist_ref,
            "playlist_title": playlist_title,
            "created": datetime.now(timezone.utc).isoformat(),
        }
        _save(data)
    return slug


def delete_link(slug: str) -> None:
    with _LOCK:
        data = _load()
        data["links"].pop(slug, None)
        _save(data)
