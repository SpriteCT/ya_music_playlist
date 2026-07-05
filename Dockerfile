# --- Образ приложения ---
FROM python:3.12-slim

# Не создавать .pyc, не буферизовать вывод (логи сразу в docker logs)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Зависимости отдельным слоем — так пересборка кода не тянет переустановку пакетов
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Код приложения
COPY core.py web.py store.py ./
COPY templates ./templates

# Запуск от непривилегированного пользователя. /app/data — под state.json
# (токен админки + шаринг-ссылки), монтируется как volume в docker-compose.yml,
# поэтому должен быть доступен appuser на запись.
RUN useradd --create-home appuser && \
    mkdir -p /app/data && \
    chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Прод-сервер. ВАЖНО: токен, пароль админки и секрет сессий НЕ зашиты в образ —
# передаются через переменные окружения при запуске (см. docker-compose.yml).
#   -w 2            — 2 воркера (хватает для небольшого сервиса)
#   --timeout 60    — запас на неспешные ответы API Яндекса
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:8000", "--timeout", "60", "web:app"]
