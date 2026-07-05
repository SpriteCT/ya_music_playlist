"""
Веб-оболочка.

Любой человек логинится через /login своим аккаунтом Я.Музыки (OAuth
device-flow прямо в браузере) и получает личный кабинет на /me: свои
плейлисты и свои шаринг-ссылки. Каждая ссылка /s/<slug> ведёт на плейлист
конкретного пользователя. Публичная форма по такой ссылке не требует
авторизации — треки, которые вводят гости, добавляются от имени владельца
ссылки.

Локальный запуск (dev-сервер):
    pip install -r requirements.txt
    python web.py              # http://127.0.0.1:5000
    # открой /login, войди через Яндекс, создай ссылку на свой плейлист

Прод: см. Dockerfile / docker-compose.yml (gunicorn на 0.0.0.0:8000).
"""

import os
import secrets
import time
from functools import wraps

from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for
from yandex_music import Client as YMClient
from yandex_music.exceptions import DeviceAuthError

import store
from core import (
    add_album_to_playlist,
    add_track_to_playlist,
    classify_link,
    get_client_for_user,
    get_playlist_tracks,
    list_own_playlists,
    preview_album,
    TrackLinkError,
)

app = Flask(__name__)

_secret_key = os.environ.get("FLASK_SECRET_KEY")
if not _secret_key:
    print(
        "ВНИМАНИЕ: FLASK_SECRET_KEY не задан — используется временный ключ, "
        "сгенерированный при старте. Сессии пользователей слетят при "
        "перезапуске процесса. Задай FLASK_SECRET_KEY в .env для прод "
        '(python -c "import secrets; print(secrets.token_hex(32))").'
    )
    _secret_key = secrets.token_hex(32)
app.secret_key = _secret_key

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Включай SESSION_COOKIE_SECURE=1 в .env, когда приложение стоит за
    # HTTPS-прокси (см. README) — тогда cookie сессии не уйдёт по http.
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE") == "1",
)

# Наивный rate-limit на /oauth/start: без внешних зависимостей, per-process
# (при нескольких воркерах gunicorn лимит общий на приложение, а не точный
# per-IP — консервативно, но не идеально). Защищает от того, чтобы наш сервер
# использовали для спама device-code запросами в Яндекс.
_RATE_WINDOW_S = 300
_RATE_MAX_ATTEMPTS = 10
_oauth_start_attempts: dict[str, list[float]] = {}


def _rate_limited(bucket: dict[str, list[float]], key: str) -> bool:
    now = time.time()
    attempts = [t for t in bucket.get(key, []) if now - t < _RATE_WINDOW_S]
    bucket[key] = attempts
    return len(attempts) >= _RATE_MAX_ATTEMPTS


def _record_attempt(bucket: dict[str, list[float]], key: str) -> None:
    bucket.setdefault(key, []).append(time.time())


def login_required(view):
    """Гейт для HTML-страниц личного кабинета: редиректит на /login."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login_page"))
        return view(*args, **kwargs)

    return wrapped


def login_required_api(view):
    """Гейт для JSON-эндпоинтов личного кабинета: отвечает кодом, а не редиректом."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify(ok=False, error="Не авторизован"), 401
        return view(*args, **kwargs)

    return wrapped


def _get_link_or_404(slug: str) -> dict:
    link = store.get_link(slug)
    if not link:
        abort(404)
    return link


@app.route("/healthz")
def healthz():
    # Лёгкая проверка живости — без обращения к Яндексу.
    return jsonify(status="ok")


@app.route("/")
def root():
    return render_template("landing.html")


@app.errorhandler(404)
def not_found(_e):
    return render_template("404.html"), 404


# --- Вход через Яндекс ---


@app.route("/login")
def login_page():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("root"))


@app.route("/oauth/start", methods=["POST"])
def oauth_start():
    ip = request.remote_addr or "unknown"
    if _rate_limited(_oauth_start_attempts, ip):
        return jsonify(ok=False, error="Слишком много попыток входа. Подожди немного."), 429
    _record_attempt(_oauth_start_attempts, ip)

    client = YMClient()
    code = client.request_device_code()
    session["oauth"] = {
        "device_code": code.device_code,
        "expires_at": time.time() + code.expires_in,
    }
    return jsonify(
        ok=True,
        verification_url=code.verification_url,
        user_code=code.user_code,
        interval=code.interval,
    )


@app.route("/oauth/poll", methods=["POST"])
def oauth_poll():
    oauth = session.get("oauth")
    if not oauth:
        return jsonify(ok=False, error="Нет активного запроса на вход. Начни заново."), 400

    if time.time() > oauth["expires_at"]:
        session.pop("oauth", None)
        return jsonify(ok=True, status="error", error="Код устарел, попробуй войти заново.")

    client = YMClient()
    try:
        token = client.poll_device_token(oauth["device_code"])
    except DeviceAuthError as e:
        session.pop("oauth", None)
        return jsonify(ok=True, status="error", error=str(e))

    if token is None:
        return jsonify(ok=True, status="pending")

    client.token = token.access_token
    client.init()
    account = client.me.account if client.me else None
    if not account:
        session.pop("oauth", None)
        return jsonify(ok=True, status="error", error="Не удалось получить данные аккаунта.")

    uid = str(account.uid)
    login = account.display_name or account.login or ""
    store.set_user(uid, token.access_token, login)

    session.pop("oauth", None)
    session["user_id"] = uid
    return jsonify(ok=True, status="success")


# --- Личный кабинет ---


@app.route("/me")
@login_required
def dashboard():
    uid = session["user_id"]
    user = store.get_user(uid)
    return render_template(
        "dashboard.html",
        account_label=user.get("login") or uid,
        links=store.list_links_for_user(uid),
    )


@app.route("/me/playlists")
@login_required_api
def me_playlists():
    uid = session["user_id"]
    try:
        playlists = list_own_playlists(get_client_for_user(uid))
    except Exception as e:  # noqa: BLE001
        return jsonify(ok=False, error=f"Не удалось получить плейлисты: {e}"), 500
    return jsonify(ok=True, playlists=playlists)


@app.route("/me/links", methods=["POST"])
@login_required_api
def me_create_link():
    uid = session["user_id"]
    data = request.get_json(silent=True) or {}
    label = (data.get("label") or "").strip()
    playlist_kind = data.get("playlist_kind")
    playlist_title = (data.get("playlist_title") or "").strip()
    if not label or playlist_kind is None:
        return jsonify(ok=False, error="Нужны название и плейлист"), 400

    slug = store.add_link(uid, label, str(playlist_kind), playlist_title)
    return jsonify(ok=True, slug=slug, url=url_for("collect_page", slug=slug))


@app.route("/me/links/<slug>/delete", methods=["POST"])
@login_required_api
def me_delete_link(slug):
    uid = session["user_id"]
    ok = store.delete_link(slug, uid)
    if not ok:
        return jsonify(ok=False, error="Ссылка не найдена"), 404
    return jsonify(ok=True)


# --- Публичный сбор треков по шаринг-ссылке ---


@app.route("/s/<slug>")
def collect_page(slug):
    link = _get_link_or_404(slug)
    return render_template("index.html", api_base=f"/s/{slug}", link_label=link["label"])


@app.route("/s/<slug>/add", methods=["POST"])
def collect_add(slug):
    link = _get_link_or_404(slug)
    track_link = (request.get_json(silent=True) or {}).get("link", "")
    if not track_link.strip():
        return jsonify(ok=False, error="Пустая ссылка"), 400

    try:
        client = get_client_for_user(link["owner"])
        result = add_track_to_playlist(track_link, client=client, playlist_ref=link["playlist"])
    except TrackLinkError as e:
        return jsonify(ok=False, error=str(e)), 400
    except Exception as e:  # noqa: BLE001
        return jsonify(ok=False, error=f"Не удалось добавить: {e}"), 500

    return jsonify(result)


@app.route("/s/<slug>/album/preview", methods=["POST"])
def collect_album_preview(slug):
    link = _get_link_or_404(slug)
    track_link = (request.get_json(silent=True) or {}).get("link", "")
    if not track_link.strip():
        return jsonify(ok=False, error="Пустая ссылка"), 400

    try:
        kind, album_id, _ = classify_link(track_link)
        if kind != "album":
            return jsonify(ok=False, error="Это ссылка на трек, а не на альбом целиком"), 400
        client = get_client_for_user(link["owner"])
        result = preview_album(album_id, client=client, playlist_ref=link["playlist"])
    except TrackLinkError as e:
        return jsonify(ok=False, error=str(e)), 400
    except Exception as e:  # noqa: BLE001
        return jsonify(ok=False, error=f"Не удалось загрузить альбом: {e}"), 500

    return jsonify(ok=True, **result)


@app.route("/s/<slug>/album/add", methods=["POST"])
def collect_album_add(slug):
    link = _get_link_or_404(slug)
    track_link = (request.get_json(silent=True) or {}).get("link", "")
    if not track_link.strip():
        return jsonify(ok=False, error="Пустая ссылка"), 400

    try:
        kind, album_id, _ = classify_link(track_link)
        if kind != "album":
            return jsonify(ok=False, error="Это ссылка на трек, а не на альбом целиком"), 400
        client = get_client_for_user(link["owner"])
        result = add_album_to_playlist(album_id, client=client, playlist_ref=link["playlist"])
    except TrackLinkError as e:
        return jsonify(ok=False, error=str(e)), 400
    except Exception as e:  # noqa: BLE001
        return jsonify(ok=False, error=f"Не удалось добавить альбом: {e}"), 500

    return jsonify(ok=True, **result)


@app.route("/s/<slug>/tracks")
def collect_tracks(slug):
    link = _get_link_or_404(slug)
    try:
        client = get_client_for_user(link["owner"])
        result = get_playlist_tracks(client=client, playlist_ref=link["playlist"])
    except Exception as e:  # noqa: BLE001
        return jsonify(ok=False, error=f"Не удалось получить плейлист: {e}"), 500

    return jsonify(ok=True, **result)


if __name__ == "__main__":
    app.run(debug=True)
