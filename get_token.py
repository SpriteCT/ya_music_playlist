"""
Получение токена Я.Музыки через OAuth Device Flow.

Запусти:
    python get_token.py

Скрипт покажет ссылку и код. Открой ссылку, введи код, подтверди вход —
после этого в консоли появится access_token. Сохрани его, например, так:

    export YM_TOKEN="ПОЛУЧЕННЫЙ_ТОКЕН"     # Linux / macOS
    set YM_TOKEN=ПОЛУЧЕННЫЙ_ТОКЕН          # Windows (cmd)

Токен долгоживущий (примерно год), но НЕ вечный — при истечении получи заново.
Своё OAuth-приложение Яндекс создать не даёт; библиотека использует
клиентский id официального приложения Я.Музыки.
"""

from yandex_music import Client


def on_code(code):
    print("\n1) Открой ссылку:", code.verification_url)
    print("2) Введи код:     ", code.user_code)
    print("3) Подтверди вход. Жду...\n")


def main() -> None:
    client = Client()
    token = client.device_auth(on_code=on_code)
    print("Готово! Твой токен:\n")
    print(token.access_token)
    print(f"\n(действует ~{token.expires_in // 86400} дней)")


if __name__ == "__main__":
    main()
