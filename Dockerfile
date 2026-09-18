# 단타 알림 시스템 — run.py(관리 프로세스)가 엔진·Binance 워커·매크로 수집·백오피스를 자식 프로세스로 띄운다.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 ALERT_DATA_DIR=/data
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alertbot ./alertbot
COPY run.py run_engine.py run_backoffice.py run_binance.py run_macro.py ./
RUN mkdir -p /data

CMD ["python", "run.py"]
