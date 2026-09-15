# 단타 알림 시스템 — 엔진과 백오피스가 같은 이미지를 쓴다 (command 로 구분).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 ALERT_DATA_DIR=/data
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alertbot ./alertbot
COPY run_engine.py run_backoffice.py run_binance.py ./
RUN mkdir -p /data

# 기본은 엔진. 백오피스는 compose 에서 command 를 바꾼다.
CMD ["python", "run_engine.py"]
