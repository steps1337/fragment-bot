#!/bin/bash
# Запускаем FastAPI через gunicorn (для Mini App)
gunicorn bot:api -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:$PORT &

# Запускаем Aiogram бота (для уведомлений в Telegram)
python3 bot.py