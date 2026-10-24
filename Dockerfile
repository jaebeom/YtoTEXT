# yt2text 컨테이너 — 사내망(romanty-intranet) compose 에 서비스 하나로 붙일 때 씀.
# 사내망 쪽 compose·프록시 설정은 사내망 저장소(9a)가 맡음. 여기서는 이미지만.
#
#   - 비루트 사용자(uid 10001)로 WSGI 서버(gunicorn)를 띄움
#   - 작업 상태가 메모리에 있어서 워커 1개 + 스레드 (워커를 늘리면 진행률·취소가 깨짐)
#   - 127.0.0.1:8765 에만 리슨 — 사내망 사이드카 네트워크 안에서 프록시만 닿게
#   - 데이터는 /data (볼륨), 프록시 비밀값은 파일로 붙이고 경로만 환경변수로
#
# 환경변수 (README 「사내망 아래에서 쓰기」):
#   INTRANET_PROXY_SECRET_FILE  비밀값 파일 경로 → 신뢰 프록시 모드
#   YT2TEXT_BASE_PATH           경로 접두어 (예: /apps/ytot)
#   YT2TEXT_SAVE_PATH           「사내망 문서로 저장」 화면 경로 (기본 /ytot-save)

FROM python:3.11-slim

# ffmpeg: Whisper 오디오 디코딩·긴 영상 분할 (yt2text.py 의 ffmpeg·ffprobe 호출)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY yt2text.py .

RUN useradd --system --uid 10001 --home-dir /data --shell /usr/sbin/nologin ytot \
 && mkdir -p /data \
 && chown ytot:ytot /data

ENV YT2TEXT_DATA_DIR=/data \
    HF_HOME=/data/hf-cache \
    PYTHONUNBUFFERED=1

USER ytot
VOLUME ["/data"]
EXPOSE 8765

CMD ["gunicorn", "--workers", "1", "--threads", "8", "--worker-class", "gthread", \
     "--bind", "127.0.0.1:8765", "--timeout", "120", "--access-logfile", "-", \
     "yt2text:wsgi_app()"]
