"""
Персистентное состояние: пользователи (Я.Музыка аккаунты, вошедшие через
OAuth) и их шаринг-ссылки (slug -> плейлист конкретного пользователя).

SQLite-хранилище (state.db). Путь настраивается через STATE_FILE (по
умолчанию state.db рядом с проектом). В Docker должен быть смонтирован как
volume, иначе состояние теряется при пересборке образа.
"""

import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

_PATH = os.environ.get("STATE_FILE", os.path.join(os.path.dirname(__file__), "state.db"))
_LOCK = threading.Lock()


@contextmanager
def _db():
    conn = sqlite3.connect(_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _init_db() -> None:
    with _db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                uid TEXT PRIMARY KEY,
                token TEXT NOT NULL,
                login TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS links (
                slug TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                playlist TEXT NOT NULL,
                owner TEXT NOT NULL,
                created TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_links_owner ON links(owner)")


_init_db()


def get_user(uid: str) -> dict | None:
    """Возвращает {token, login} для аккаунта или None, если не логинился."""
    with _db() as conn:
        row = conn.execute("SELECT token, login FROM users WHERE uid = ?", (str(uid),)).fetchone()
    return {"token": row["token"], "login": row["login"]} if row else None


def set_user(uid: str, token: str, login: str = "") -> None:
    with _LOCK, _db() as conn:
        conn.execute(
            """
            INSERT INTO users (uid, token, login) VALUES (?, ?, ?)
            ON CONFLICT(uid) DO UPDATE SET token = excluded.token, login = excluded.login
            """,
            (str(uid), token, login),
        )


def _row_to_link(row: sqlite3.Row) -> dict:
    return {"label": row["label"], "playlist": row["playlist"], "owner": row["owner"], "created": row["created"]}


def list_links_for_user(uid: str) -> dict:
    """Возвращает {slug: {label, playlist, owner, created}} только этого пользователя."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT slug, label, playlist, owner, created FROM links WHERE owner = ?",
            (str(uid),),
        ).fetchall()
    return {row["slug"]: _row_to_link(row) for row in rows}


def get_link(slug: str) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT slug, label, playlist, owner, created FROM links WHERE slug = ?", (slug,)
        ).fetchone()
    return _row_to_link(row) if row else None


def add_link(owner_uid: str, playlist_ref: str, label: str) -> str:
    """label — название плейлиста на момент создания ссылки (для отображения)."""
    with _LOCK, _db() as conn:
        slug = secrets.token_urlsafe(6)
        while conn.execute("SELECT 1 FROM links WHERE slug = ?", (slug,)).fetchone():
            slug = secrets.token_urlsafe(6)
        conn.execute(
            "INSERT INTO links (slug, label, playlist, owner, created) VALUES (?, ?, ?, ?, ?)",
            (slug, label, playlist_ref, str(owner_uid), datetime.now(timezone.utc).isoformat()),
        )
    return slug


def delete_link(slug: str, owner_uid: str) -> bool:
    """Удаляет ссылку, только если она принадлежит owner_uid. Возвращает успех."""
    with _LOCK, _db() as conn:
        cur = conn.execute("DELETE FROM links WHERE slug = ? AND owner = ?", (slug, str(owner_uid)))
        return cur.rowcount > 0
