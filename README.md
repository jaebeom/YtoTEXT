# YtoTEXT (yt2text)

개인용 유튜브 → 텍스트 추출기. 로컬에서 돌아가는 단일 파일 웹앱입니다.

유튜브 링크를 붙여넣으면 **자막**(빠름) 또는 **Whisper 받아쓰기**(자막 없는 영상도 가능)로
전체 텍스트를 뽑아서 문단/타임스탬프 형태로 보여주고, `.txt` / `.md` / `.srt`로 저장할 수 있습니다.

## 실행

```bash
pip install -r requirements.txt
python yt2text.py
```

브라우저에서 → http://localhost:8765

## 우분투 노트북을 홈서버로 쓰기

```bash
git clone https://github.com/jaebeom/YtoTEXT.git
cd YtoTEXT
./install.sh    # venv + ffmpeg + systemd 서비스 등록까지 한 번에
```

- 기본으로 `0.0.0.0:8765`에 바인딩됩니다 — 같은 네트워크/Tailscale에서 접속 가능.
  이 컴퓨터에서만 쓰려면 `HOST=127.0.0.1 ./install.sh`
- 서비스 관리: `systemctl status|restart|stop yt2text` · 로그: `journalctl -u yt2text -f`
- **뚜껑 닫아도 계속 돌게**: `/etc/systemd/logind.conf`에서 `HandleLidSwitch=ignore`로 바꾸고 재부팅
- **폰에서 접속**: 노트북과 폰에 [Tailscale](https://tailscale.com) 설치 → `http://<노트북이름>:8765`
- 코드 업데이트: `git pull && sudo systemctl restart yt2text`
- NVIDIA GPU가 있으면 자동으로 CUDA 사용 (드라이버 설치 후 `.venv/bin/pip install nvidia-cublas-cu12 nvidia-cudnn-cu12` 필요). 없으면 CPU(int8)로 동작

## 기능

- **자막 모드 (빠름)** — `youtube-transcript-api`로 업로더/자동생성 자막을 바로 가져옴.
  언어 칩을 클릭해 다른 자막 언어로 전환 가능 (기본 선호: 한국어 → 영어)
- **Whisper 모드 (정확)** — `yt-dlp`로 오디오만 내려받아 `faster-whisper`로 받아쓰기.
  자막이 없는 영상도 처리 가능. 모델 선택(large-v3-turbo 추천), 언어 자동 감지/고정,
  GPU(CUDA) 자동 사용, 진행률 표시
- **히스토리** — 추출 결과를 로컬(`yt2text_data/`)에 최대 300개 저장.
  썸네일 카드 그리드로 표시, 클릭해서 다시 열기, 개별 삭제 가능.
  썸네일도 로컬 백업(영상이 지워져도 카드 유지)
- **내보내기** — 복사 / `.txt` / `.md` / `.srt` 다운로드, 타임스탬프 표시 토글

## v4 추가 기능

- **중복 감지** — 이미 추출한 영상을 다시 넣으면 알림 행이 떠서 [열기] / [다시 추출] 선택
- **배치 추출** — 링크 여러 줄을 한 번에 붙여넣기 (최대 10개), 행마다 개별 진행률 표시
  - 자막 모드: 전부 병렬 처리
  - Whisper 모드: 오디오 다운로드 3개 병렬 + 받아쓰기는 순차 큐 (CPU 보호)
- 자막 없는 영상은 실패 행에서 [Whisper로 추출] 버튼으로 바로 전환

## v4.1 개선 (안정성·보안)

- **원자적 저장** — 히스토리를 임시 파일에 쓴 뒤 교체. 저장 중 크래시로 기록 전체가 날아가는 문제 방지
- **전문 분리 저장** — transcript 전문을 `yt2text_data/results/` 항목별 파일로 분리해 목록 조회가 가벼워짐. 구버전 데이터는 실행 시 자동 마이그레이션
- **Whisper 작업 [취소] 버튼** — 다운로드/받아쓰기 도중 중단 가능
- 서버 재시작 시 진행 중이던 행이 영원히 "진행 중"으로 남던 문제 수정
- 끝난 STT 작업을 10분 뒤 메모리에서 정리 (누수 방지)
- 자막 배치 동시 요청을 2개로 제한 (유튜브 IP 차단 예방)
- Host 헤더 검사 + JSON Content-Type 강제 — 외부 사이트가 로컬 서버를 몰래 조작하는 것(CSRF/DNS 리바인딩) 방어
- 배치 진행 중 새 링크를 추가하면 큐를 리셋하지 않고 뒤에 이어붙임
- 영상 길이를 모를 때도 받아쓰기 진행 지점(시각) 표시
- `--host` / `--port` 실행 옵션 추가

## 버전 히스토리

| 버전 | 내용 |
|------|------|
| v4.1 | 안정성·보안 개선 (원자적 저장, 전문 분리, 작업 취소, CSRF 방어 등) |
| v4 | 중복 감지, 배치 추출(최대 10개, 큐/진행률) |
| v3 | 히스토리 (로컬 저장, 썸네일 카드, 재열기/삭제) |
| v2 | Whisper STT 모드 (자막 없는 영상 지원) |
| v1 | 자막 추출, 문단 정리, txt/md/srt 내보내기 |

## 참고

- 데이터는 전부 로컬 `yt2text_data/`에 저장됩니다 (목록 `history.json` · 전문 `results/` · 썸네일 `thumbs/`) — git에는 올라가지 않음
- Whisper 모델은 최초 사용 시 자동 다운로드됩니다 (large 계열은 수 GB, 수 분 소요)
- 기본은 `127.0.0.1:8765` 바인딩이고, Host 헤더 검사로 외부 사이트의 로컬 서버 조작을 차단합니다
- 포트 변경: `python yt2text.py --port 9000`
