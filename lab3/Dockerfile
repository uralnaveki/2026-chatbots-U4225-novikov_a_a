# Образ бота: Python (сам бот) + Node.js (официальный TickTick CLI).
FROM node:20-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# TickTick CLI ставится глобально, команда `ticktick` будет в PATH контейнера.
RUN npm install -g @ticktick/ticktick-cli

WORKDIR /app
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py integrations.py entrypoint.sh ./
# На случай, если файл сохранили в Windows с переводами строк CRLF.
RUN sed -i 's/\r$//' entrypoint.sh && chmod +x entrypoint.sh

# Всё, что должно переживать перезапуск (вход в TickTick, база, кэш), лежит в /data.
# HOME тоже указывает туда, поэтому CLI хранит там свой вход.
ENV PYTHONUNBUFFERED=1 \
    HOME=/data/home \
    DATA_FILE=/data/data.json \
    DB_FILE=/data/bot.db
VOLUME /data

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "bot.py"]
