"""
Фаза 1 — простое добавление трека из командной строки.

Использование:
    python cli.py "https://music.yandex.ru/album/3192570/track/1710808"

Токен и id плейлиста берутся из core.py / переменных окружения.
"""

import sys

from core import add_track_to_playlist, TrackLinkError


def main() -> int:
    if len(sys.argv) < 2:
        print('Использование: python cli.py "<ссылка на трек>"')
        return 2

    link = sys.argv[1]
    try:
        result = add_track_to_playlist(link)
    except TrackLinkError as e:
        print(f"Ошибка ссылки: {e}")
        return 1
    except Exception as e:  # noqa: BLE001 — показываем любую ошибку API/токена
        print(f"Не удалось добавить: {e}")
        return 1

    who = f'{result["title"]} — {result["artists"]}'
    if result["already_exists"]:
        print(f'Уже в плейлисте «{result["playlist"]}»: {who}')
    else:
        print(f'Добавлено в «{result["playlist"]}»: {who}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
