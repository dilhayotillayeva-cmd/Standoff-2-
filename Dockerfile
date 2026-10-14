FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    MALLOC_ARENA_MAX=2 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/bot.db

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py db.py ./

# SQLite ishlatilsa /data papkasiga doimiy disk (volume) ulang, aks holda deploy'da ma'lumot yo'qoladi.
# Yoki DATABASE_URL bilan tashqi PostgreSQL ishlating.
CMD ["python", "bot.py"]
