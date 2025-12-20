FROM python:3.12-slim

# =======================
# 시스템 패키지 설치
# =======================
RUN apt-get update && \
    apt-get install -y ffmpeg curl && \
    rm -rf /var/lib/apt/lists/*

# =======================
# 작업 디렉토리
# =======================
WORKDIR /app

# =======================
# 파이썬 패키지
# =======================
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# =======================
# 소스 복사
# =======================
COPY . .

# =======================
# 실행
# =======================
CMD ["python", "main.py"]
