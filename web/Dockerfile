FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY core.py app.py ./
COPY templates ./templates

# 抽查单落盘目录（compose 挂载卷持久化，重启后仍可续取）
ENV PORT=8000 \
    WEB_DATA_DIR=/data

EXPOSE 8000

CMD ["flask", "--app", "app", "run", "--host", "0.0.0.0", "--port", "8000"]
