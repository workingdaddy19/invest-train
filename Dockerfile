FROM python:3.11-slim

WORKDIR /app

# gcc/libgomp1: xgboost/sklearn 빌드·런타임, fonts-nanum: 학습 차트 한글 폰트
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libgomp1 fonts-nanum && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 학습 스크립트 + 학습 트리거 API 서버
COPY invest_train.py train_server.py .

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

EXPOSE 8080

# ⚠️ 단일 워커 필수 — 인메모리 학습 락/상태 일관성 보장 (train_server.py 주석 참조)
CMD ["uvicorn", "train_server:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
