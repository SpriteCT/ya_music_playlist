"""
Веб-оболочка.

Владелец логинится в /admin (пароль из ADMIN_PASSWORD) и авторизуется в
Я.Музыке через OAuth device-flow прямо в браузере — токен сохраняется в
store.py. Там же он создаёт шаринг-ссылки: каждая ссылка /s/<slug> ведёт на
свой плейлист. Публичная форма по такой ссылке не требует авторизации —
все треки, которые вводят гости, добавляются от имени владельца токена.

Локальный запуск (dev-сервер):
    pip install -r requirements.txt
    python web.py              # http://127.0.0.1:5000
    # открой /admin, залогинься паролем (ADMIN_PASSWORD в .env),
    # затем войди через Яндекс и создай ссылку на плейлист

Прод: см. Dockerfile / docker-compose.yml (gunicorn на 0.0.0.0:8000).
"""

import hmac
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
    get_playlist_tracks,
    get_shared_client,
    list_own_playlists,
    preview_album,
    TrackLinkError,
)

app = Flask(__name__)

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

_secret_key = os.environ.get("FLASK_SECRET_KEY")
if not _secret_key:
    print(
        "ВНИМАНИЕ: FLASK_SECRET_KEY не задан — используется временный ключ, "
        "сгенерированный при старте. Сессии админки слетят при перезапуске "
        "процесса. Задай FLASK_SECRET_KEY в .env для прод "
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

# Наивный rate-limit на /admin/login: без внешних зависимостей, per-process
# (при нескольких воркерах gunicorn лимит общий на приложение, а не точный
# per-IP — консервативно, но не идеально; для личного проекта достаточно).
_LOGIN_WINDOW_S = 300
_LOGIN_MAX_ATTEMPTS = 10
_login_attempts: dict[str, list[float]] = {}


def _login_rate_limited(ip: str) -> bool:
    now = time.time()
    attempts = [t for t in _login_attempts.get(ip, []) if now - t < _LOGIN_WINDOW_S]
    _login_attempts[ip] = attempts
    return len(attempts) >= _LOGIN_MAX_ATTEMPTS


def _record_login_attempt(ip: str) -> None:
    _login_attempts.setdefault(ip, []).append(time.time())


def _admin_configured() -> bool:
    return bool(ADMIN_PASSWORD)


def admin_page_required(view):
    """Гейт для HTML-страниц админки: редиректит на форму логина."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if not _admin_configured():
            return "Админка не настроена: задай ADMIN_PASSWORD в .env", 503
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)

    return wrapped


def admin_api_required(view):
    """Гейт для JSON-эндпоинтов админки: отвечает кодом, а не редиректом."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if not _admin_configured():
            return jsonify(ok=False, error="Админка не настроена: задай ADMIN_PASSWORD в .env"), 503
        if not session.get("admin"):
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


# --- Админка ---


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if not _admin_configured():
        return "Админка не настроена: задай ADMIN_PASSWORD в .env", 503

    error = None
    if request.method == "POST":
        ip = request.remote_addr or "unknown"
        if _login_rate_limited(ip):
            error = "Слишком много попыток. Подожди немного и попробуй снова."
        else:
            password = request.form.get("password", "")
            if hmac.compare_digest(password, ADMIN_PASSWORD):
                session["admin"] = True
                return redirect(url_for("admin_dashboard"))
            _record_login_attempt(ip)
            error = "Неверный пароль"

    return render_template("admin_login.html", error=error)


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


@app.route("/admin")
@admin_page_required
def admin_dashboard():
    connected = False
    account_label = None
    try:
        client = get_shared_client()
        connected = True
        account = getattr(client.me, "account", None) if client.me else None
        if account:
            account_label = account.display_name or account.login
    except Exception:
        pass

    return render_template(
        "admin.html",
        connected=connected,
        account_label=account_label,
        links=store.list_links(),
    )


@app.route("/admin/oauth/start", methods=["POST"])
@admin_api_required
def admin_oauth_start():
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


@app.route("/admin/oauth/poll", methods=["POST"])
@admin_api_required
def admin_oauth_poll():
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

    store.set_token(token.access_token)
    session.pop("oauth", None)
    return jsonify(ok=True, status="success")


@app.route("/admin/playlists")
@admin_api_required
def admin_playlists():
    try:
        playlists = list_own_playlists()
    except Exception as e:  # noqa: BLE001
        return jsonify(ok=False, error=f"Не удалось получить плейлисты: {e}"), 500
    return jsonify(ok=True, playlists=playlists)


@app.route("/admin/links", methods=["POST"])
@admin_api_required
def admin_create_link():
    data = request.get_json(silent=True) or {}
    label = (data.get("label") or "").strip()
    playlist_kind = data.get("playlist_kind")
    playlist_title = (data.get("playlist_title") or "").strip()
    if not label or playlist_kind is None:
        return jsonify(ok=False, error="Нужны название и плейлист"), 400

    slug = store.add_link(label, str(playlist_kind), playlist_title)
    return jsonify(ok=True, slug=slug, url=url_for("collect_page", slug=slug))


@app.route("/admin/links/<slug>/delete", methods=["POST"])
@admin_api_required
def admin_delete_link(slug):
    store.delete_link(slug)
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
        result = add_track_to_playlist(
            track_link, client=get_shared_client(), playlist_ref=link["playlist"]
        )
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
        result = preview_album(album_id, client=get_shared_client(), playlist_ref=link["playlist"])
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
        result = add_album_to_playlist(album_id, client=get_shared_client(), playlist_ref=link["playlist"])
    except TrackLinkError as e:
        return jsonify(ok=False, error=str(e)), 400
    except Exception as e:  # noqa: BLE001
        return jsonify(ok=False, error=f"Не удалось добавить альбом: {e}"), 500

    return jsonify(ok=True, **result)


@app.route("/s/<slug>/tracks")
def collect_tracks(slug):
    link = _get_link_or_404(slug)
    try:
        result = get_playlist_tracks(client=get_shared_client(), playlist_ref=link["playlist"])
    except Exception as e:  # noqa: BLE001
        return jsonify(ok=False, error=f"Не удалось получить плейлист: {e}"), 500

    return jsonify(ok=True, **result)


if __name__ == "__main__":
    app.run(debug=True)
