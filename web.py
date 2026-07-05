"""
Фаза 2 — веб-оболочка.

Пользователь вставляет ссылку на трек в поле, трек уходит в захардкоженный
плейлист. Плейлист и токен настраиваются в core.py / переменных окружения.

Локальный запуск (dev-сервер):
    pip install -r requirements.txt
    export YM_TOKEN="..."      # токен владельца плейлиста
    python web.py              # http://127.0.0.1:5000

Прод: см. Dockerfile / docker-compose.yml (gunicorn на 0.0.0.0:8000).

Один токен на всё приложение — все треки, которые вводят пользователи,
попадают в один и тот же плейлист владельца токена. От пользователей
никакой авторизации не требуется.
"""

from flask import Flask, jsonify, render_template, request

from core import (
    add_track_to_playlist,
    get_playlist_tracks,
    make_client,
    TrackLinkError,
    PLAYLIST,
)

app = Flask(__name__)

# Клиент создаётся один раз при старте — не логинимся на каждый запрос.
_client = None


def get_client():
    global _client
    if _client is None:
        _client = make_client()
    return _client


@app.route("/healthz")
def healthz():
    # Лёгкая проверка живости — без обращения к Яндексу.
    return jsonify(status="ok")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/add", methods=["POST"])
def add():
    link = (request.get_json(silent=True) or {}).get("link", "")
    if not link.strip():
        return jsonify(ok=False, error="Пустая ссылка"), 400

    try:
        result = add_track_to_playlist(
            link, client=get_client(), playlist_ref=PLAYLIST
        )
    except TrackLinkError as e:
        return jsonify(ok=False, error=str(e)), 400
    except Exception as e:  # noqa: BLE001
        return jsonify(ok=False, error=f"Не удалось добавить: {e}"), 500

    return jsonify(result)


@app.route("/tracks")
def tracks():
    try:
        result = get_playlist_tracks(client=get_client(), playlist_ref=PLAYLIST)
    except Exception as e:  # noqa: BLE001
        return jsonify(ok=False, error=f"Не удалось получить плейлист: {e}"), 500

    return jsonify(ok=True, **result)


if __name__ == "__main__":
    app.run(debug=True)
