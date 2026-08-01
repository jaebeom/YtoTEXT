#!/usr/bin/env bash
# yt2text 우분투 서버 설치 스크립트
#
# 사용:
#   ./install.sh                  # 기본: 0.0.0.0:8765 (Tailscale/같은 네트워크에서 접속 가능)
#   HOST=127.0.0.1 ./install.sh   # 이 컴퓨터에서만 접속
#   PORT=9000 ./install.sh        # 포트 변경
set -euo pipefail

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8765}"
DIR="$(cd "$(dirname "$0")" && pwd)"

echo "==> 시스템 패키지 설치 (python3-venv, ffmpeg)"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv ffmpeg

echo "==> 파이썬 가상환경 + 의존성 설치 (faster-whisper 포함, 몇 분 걸릴 수 있어요)"
python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q -U pip
"$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"

echo "==> systemd 서비스 등록 (부팅 시 자동 시작)"
sudo tee /etc/systemd/system/yt2text.service >/dev/null <<EOF
[Unit]
Description=yt2text - YouTube to text
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python $DIR/yt2text.py --host $HOST --port $PORT
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now yt2text

echo
echo "  설치 완료!  →  http://localhost:$PORT"
echo
echo "  상태 확인:   systemctl status yt2text"
echo "  로그 보기:   journalctl -u yt2text -f"
echo
echo "  다음 단계 (선택):"
echo "  1) 뚜껑 닫아도 계속 돌게:"
echo "     /etc/systemd/logind.conf 에서 HandleLidSwitch=ignore 로 바꾸고 재부팅"
echo "  2) 폰/다른 기기에서 접속 (Tailscale):"
echo "     curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up"
echo "     폰에도 Tailscale 앱 설치 → http://<노트북이름>:$PORT"
