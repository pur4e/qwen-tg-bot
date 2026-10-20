FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY tgbot ./tgbot
RUN mkdir -p /app/data

EXPOSE 7860
CMD ["python", "-m", "tgbot"]
