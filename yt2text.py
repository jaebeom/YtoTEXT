#!/usr/bin/env python3
"""
yt2text v4 — 개인용 유튜브 → 텍스트 (자막 + Whisper STT + 히스토리 + 배치 큐)

실행:
    pip install youtube-transcript-api flask faster-whisper yt-dlp
    python yt2text.py
    → http://localhost:8765

v4 추가:
    - 중복 감지: 이미 추출한 영상이면 알림 행 표시 → [열기] / [다시 추출]
    - 배치 추출: 여러 줄 붙여넣기로 최대 10개 동시 처리, 개별 진행률 표시
      (자막: 병렬 / Whisper: 다운로드 3개 병렬 + 받아쓰기는 순차 큐)
"""

import os
import re
import glob
import json
import random
import subprocess
import time
import uuid
import shutil
import tempfile
import threading
from pathlib import Path
from urllib.request import urlopen
from urllib.parse import quote

from flask import Flask, request, jsonify, Response, send_file

from youtube_transcript_api import (
    YouTubeTranscriptApi,
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable,
    AgeRestricted,
    IpBlocked,
    RequestBlocked,
)

app = Flask(__name__)


@app.before_request
def _check_host():
    # 외부 사이트가 DNS 리바인딩으로 로컬 서버를 조작하는 것 방지
    if ALLOWED_HOSTS and request.host not in ALLOWED_HOSTS:
        return jsonify(error="허용되지 않은 요청이에요."), 403

PREFERRED = ["ko", "en"]
HISTORY_MAX = 300
BATCH_MAX = 50

DATA_DIR = Path(__file__).resolve().parent / "yt2text_data"
HISTORY_FILE = DATA_DIR / "history.json"
THUMBS_DIR = DATA_DIR / "thumbs"
RESULTS_DIR = DATA_DIR / "results"
_hist_lock = threading.Lock()

DL_SEM = threading.Semaphore(3)         # 오디오 다운로드 동시 3개
TRANSCRIBE_SEM = threading.Semaphore(1)  # 받아쓰기는 한 번에 하나 (CPU 보호)
CAPTION_SEM = threading.Semaphore(1)    # 자막 요청은 한 번에 하나 (봇 감지 회피)
CHUNK_SEC = 3600                        # 긴 영상은 1시간 단위로 잘라 받아쓰기 (OOM 방지)

# 자막 요청 페이싱: 요청 사이 랜덤 간격 + 차단 시 전체 쿨다운(지수 백오프)
_last_caption = 0.0
_cooldown_until = 0.0
_cooldown_len = 90  # 차단 시 첫 쿨다운(초). 반복되면 2배씩, 최대 15분

# 루프백 바인딩일 때 허용할 Host 헤더 (DNS 리바인딩 방어) — main에서 갱신
ALLOWED_HOSTS = {"localhost:8765", "127.0.0.1:8765", "[::1]:8765"}

VIDEO_ID_RE = re.compile(
    r"(?:youtu\.be/|youtube\.com/(?:watch\?v=|shorts/|live/|embed/)|^)"
    r"([A-Za-z0-9_-]{11})"
)

# ---------------------------------------------------------------- 공통 유틸

def extract_video_id(url: str):
    m = VIDEO_ID_RE.search(url.strip())
    return m.group(1) if m else None


def fetch_title(video_id: str):
    try:
        u = "https://www.youtube.com/oembed?url=" + quote(
            f"https://www.youtube.com/watch?v={video_id}", safe=""
        ) + "&format=json"
        with urlopen(u, timeout=5) as r:
            return json.loads(r.read().decode()).get("title")
    except Exception:
        return None


def to_paragraphs(items, gap=4.0):
    paras, cur, last_end = [], [], 0.0
    for it in items:
        text = it["text"].replace("\n", " ").strip()
        if not text:
            continue
        if cur and it["start"] - last_end > gap:
            paras.append(" ".join(cur))
            cur = []
        cur.append(text)
        last_end = it["start"] + (it["dur"] or 0)
    if cur:
        paras.append(" ".join(cur))
    return paras


def fmt_ts(sec: float, srt=False):
    ms = int(round((sec - int(sec)) * 1000))
    sec = int(sec)
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    if srt:
        return f"{h:02}:{m:02}:{s:02},{ms:03}"
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def build_result(video_id, title, items, language, source):
    items = [it for it in items if it["text"].strip()]
    end = items[-1]["start"] + (items[-1]["dur"] or 0) if items else 0
    return dict(
        video_id=video_id,
        title=title,
        language=language,
        source=source,  # 'caption' | 'whisper:<model>'
        duration=fmt_ts(end),
        paragraphs=to_paragraphs(items),
        lines=[{
            "t": fmt_ts(it["start"]),
            "s": int(it["start"]),
            "srt_a": fmt_ts(it["start"], srt=True),
            "srt_b": fmt_ts(it["start"] + (it["dur"] or 0), srt=True),
            "text": it["text"].replace("\n", " ").strip(),
        } for it in items],
    )

# ---------------------------------------------------------------- 히스토리

def _load_history():
    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _atomic_write(path: Path, text: str):
    """쓰다가 죽어도 원본이 깨지지 않게 임시 파일에 쓴 뒤 교체."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _save_history(entries):
    _atomic_write(HISTORY_FILE, json.dumps(entries, ensure_ascii=False))


def _result_path(key: str) -> Path:
    """항목 키 → 전문 파일 경로 (윈도우 금지 문자 ':'와 경로 탈출 제거)."""
    fname = key.replace(":", "__").replace("/", "_").replace("\\", "_")
    return RESULTS_DIR / f"{fname}.json"


def _migrate_history():
    """구버전(전문이 history.json에 통째로) → 항목별 결과 파일로 분리."""
    with _hist_lock:
        entries = _load_history()
        if not any("result" in e for e in entries):
            return
        for e in entries:
            res = e.pop("result", None)
            if res is not None and not _result_path(e["key"]).exists():
                _atomic_write(_result_path(e["key"]),
                              json.dumps(res, ensure_ascii=False))
        _save_history(entries)


def save_thumb(video_id: str):
    """아카이브용 썸네일 로컬 백업 (~15KB). 이미 있으면 스킵, 실패해도 무시."""
    try:
        THUMBS_DIR.mkdir(parents=True, exist_ok=True)
        dest = THUMBS_DIR / f"{video_id}.jpg"
        if dest.exists() and dest.stat().st_size > 0:
            return
        u = f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"
        with urlopen(u, timeout=10) as r:
            dest.write_bytes(r.read())
    except Exception:
        pass


def history_add(result):
    key = f"{result['video_id']}:{result['source']}"
    entry = {
        "key": key,
        "video_id": result["video_id"],
        "title": result.get("title") or result["video_id"],
        "thumb": f"https://i.ytimg.com/vi/{result['video_id']}/mqdefault.jpg",
        "source": result["source"],
        "language": result.get("language"),
        "duration": result.get("duration"),
        "saved_at": time.strftime("%Y-%m-%d %H:%M"),
    }
    with _hist_lock:
        _atomic_write(_result_path(key), json.dumps(result, ensure_ascii=False))
        entries = [e for e in _load_history() if e.get("key") != key]
        entries.insert(0, entry)
        for old in entries[HISTORY_MAX:]:  # 밀려난 항목은 전문 파일도 정리
            _result_path(old["key"]).unlink(missing_ok=True)
        _save_history(entries[:HISTORY_MAX])
    threading.Thread(target=save_thumb,
                     args=(result["video_id"],), daemon=True).start()


_title_fix_running = False


def _fix_missing_titles():
    """차단 등으로 제목을 못 가져와 영상 ID로 저장된 항목을 다시 채움."""
    global _title_fix_running
    try:
        with _hist_lock:
            broken = [e for e in _load_history()
                      if e.get("title") == e.get("video_id")]
        for e in broken:
            title = fetch_title(e["video_id"])
            if not title:
                continue  # 아직 차단 중이면 다음 기회에
            with _hist_lock:
                entries = _load_history()
                for x in entries:
                    if x.get("key") == e["key"]:
                        x["title"] = title
                _save_history(entries)
                p = _result_path(e["key"])  # 전문 파일 제목도 같이
                try:
                    res = json.loads(p.read_text(encoding="utf-8"))
                    res["title"] = title
                    _atomic_write(p, json.dumps(res, ensure_ascii=False))
                except Exception:
                    pass
            time.sleep(random.uniform(1.0, 3.0))  # 제목 조회도 살살
    finally:
        _title_fix_running = False


@app.get("/api/history")
def api_history():
    global _title_fix_running
    with _hist_lock:
        entries = _load_history()
    if (not _title_fix_running
            and any(e.get("title") == e.get("video_id") for e in entries)):
        _title_fix_running = True
        threading.Thread(target=_fix_missing_titles, daemon=True).start()
    slim = [{k: v for k, v in e.items() if k != "result"} for e in entries]
    return jsonify(slim)


@app.get("/api/history/<path:key>")
def api_history_get(key):
    p = _result_path(key)
    if p.exists():
        return Response(p.read_text(encoding="utf-8"),
                        mimetype="application/json")
    return jsonify(error="히스토리에 없어요."), 404


@app.get("/thumbs/<vid>")
def api_thumb(vid):
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}\.jpg", vid):
        return ("", 404)
    p = THUMBS_DIR / vid
    if p.exists() and p.stat().st_size > 0:
        return send_file(p, mimetype="image/jpeg")
    return ("", 404)


@app.delete("/api/history/<path:key>")
def api_history_delete(key):
    with _hist_lock:
        entries = [e for e in _load_history() if e.get("key") != key]
        _save_history(entries)
        _result_path(key).unlink(missing_ok=True)
    return jsonify(ok=True)


_migrate_history()

# ---------------------------------------------------------------- 자막 루트

@app.post("/api/transcript")
def api_transcript():
    global _last_caption, _cooldown_until, _cooldown_len
    data = request.get_json(silent=True)  # JSON 강제 (타 사이트 text/plain 방어)
    if not isinstance(data, dict):
        return jsonify(error="JSON 요청이 필요해요."), 400
    url, lang = data.get("url", ""), data.get("lang")

    video_id = extract_video_id(url)
    if not video_id:
        return jsonify(error="유튜브 링크를 인식하지 못했어요."), 400

    now = time.time()
    if _cooldown_until > now:  # 차단 쿨다운 중 — 클라이언트가 대기 후 자동 재시도
        return jsonify(error="유튜브 차단 쿨다운 중이에요.",
                       retry_after=int(_cooldown_until - now) + 1), 429

    try:
        with CAPTION_SEM:  # 한 번에 하나씩만
            # 사람처럼 보이게: 연속 요청 사이 랜덤 간격
            gap = random.uniform(2.0, 5.0)
            since = time.time() - _last_caption
            if since < gap:
                time.sleep(gap - since)
            _last_caption = time.time()

            yt = YouTubeTranscriptApi()  # 요청마다 새로 (스레드 안전)
            tlist = yt.list(video_id)
            available = [{"code": t.language_code, "name": t.language,
                          "generated": t.is_generated} for t in tlist]
            if lang:
                transcript = tlist.find_transcript([lang])
            else:
                transcript = tlist.find_transcript(
                    PREFERRED + [a["code"] for a in available])
            fetched = transcript.fetch()
            _cooldown_len = 90  # 성공 → 쿨다운 길이 리셋

    except (TranscriptsDisabled, NoTranscriptFound):
        return jsonify(error="자막이 없는 영상이에요.", no_captions=True), 404
    except (VideoUnavailable, AgeRestricted):
        return jsonify(error="영상에 접근할 수 없어요 (비공개/삭제/연령 제한)."), 404
    except (IpBlocked, RequestBlocked):
        # 전체 자막 요청을 잠시 멈춤 — 차단 상태에서 계속 두드리면 더 길어짐
        _cooldown_until = time.time() + _cooldown_len
        retry = int(_cooldown_len)
        _cooldown_len = min(_cooldown_len * 2, 900)
        return jsonify(error="유튜브가 요청을 차단했어요 — 쉬었다가 자동 재시도해요.",
                       retry_after=retry), 429
    except Exception as e:
        return jsonify(error=f"오류: {type(e).__name__}"), 500

    items = [{"text": s.text, "start": s.start, "dur": s.duration or 0}
             for s in fetched]
    res = build_result(video_id, fetch_title(video_id), items,
                       fetched.language, "caption")
    res["available"] = available
    res["language_code"] = fetched.language_code
    res["is_generated"] = fetched.is_generated
    history_add(res)
    return jsonify(res)

# ---------------------------------------------------------------- STT 루트

JOBS = {}
JOB_TTL = 600  # 끝난 작업은 10분 뒤 메모리에서 제거
_model_lock = threading.Lock()
_model_cache = {}


class JobCancelled(Exception):
    pass


def _prune_jobs():
    now = time.time()
    for jid in list(JOBS):
        j = JOBS[jid]
        if j.get("status") != "running" and now - j.get("ended_at", now) > JOB_TTL:
            JOBS.pop(jid, None)


def _ffprobe_duration(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30)
        return float(out.stdout.strip())
    except Exception:
        return None


def _preload_cuda_libs():
    """pip으로 설치한 CUDA 라이브러리(cuBLAS/cuDNN 등)를 LD_LIBRARY_PATH 설정
    없이도 쓸 수 있게 미리 로드. NVIDIA 패키지는 __init__.py 없는 네임스페이스
    패키지라 import로는 경로가 안 나오는 경우가 있어 site-packages를 직접 뒤짐."""
    try:
        import ctypes
        import sysconfig
        for d in sorted(glob.glob(os.path.join(
                sysconfig.get_paths()["purelib"], "nvidia", "*", "lib"))):
            for so in sorted(glob.glob(os.path.join(d, "*.so*"))):
                try:
                    ctypes.CDLL(so, mode=ctypes.RTLD_GLOBAL)
                except OSError:
                    pass
    except Exception:
        pass


_force_cpu = False  # GPU가 한 번이라도 깨지면 켜짐 — 이후 작업은 CPU로


def _is_cuda_error(e) -> bool:
    s = str(e).lower()
    return any(k in s for k in ("cublas", "cudnn", "cuda"))


def get_model(name: str, force_cpu=False):
    global _force_cpu
    from faster_whisper import WhisperModel
    import ctranslate2
    with _model_lock:
        if force_cpu and not _force_cpu:
            _force_cpu = True
            _model_cache.clear()  # GPU 모델 버리고 CPU로 다시
        if name in _model_cache:
            return _model_cache[name]
        _model_cache.clear()
        if not _force_cpu and ctranslate2.get_cuda_device_count() > 0:
            _preload_cuda_libs()
            device, compute = "cuda", "float16"
        else:
            device, compute = "cpu", "int8"
        try:
            model = WhisperModel(name, device=device, compute_type=compute)
        except Exception:
            if device != "cuda":
                raise
            _force_cpu = True  # GPU 로드 실패 → CPU로라도 진행
            device, compute = "cpu", "int8"
            model = WhisperModel(name, device=device, compute_type=compute)
        _model_cache[name] = model
        return model


def stt_worker(job_id, url, model_name, language):
    job = JOBS[job_id]
    tmp = Path(tempfile.mkdtemp(prefix="yt2text_"))

    def check_cancel():
        if job.get("cancel"):
            raise JobCancelled()

    try:
        # 1) 오디오 다운로드 (동시 3개 제한)
        job.update(phase="다운로드 대기", progress=1)
        with DL_SEM:
            check_cancel()
            job.update(phase="오디오 다운로드 중", progress=2)
            import yt_dlp

            def hook(d):
                check_cancel()  # 예외를 던져 다운로드 자체를 중단
                if d.get("status") == "downloading":
                    total = d.get("total_bytes") or d.get("total_bytes_estimate")
                    if total:
                        job["progress"] = 2 + 20 * d.get("downloaded_bytes", 0) / total

            opts = {
                "format": "bestaudio/best",
                "outtmpl": str(tmp / "%(id)s.%(ext)s"),
                "quiet": True, "no_warnings": True, "noplaylist": True,
                "progress_hooks": [hook],
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)

        check_cancel()
        audio = next(p for p in tmp.iterdir() if p.is_file())
        title = info.get("title")
        duration = info.get("duration") or 0
        video_id = info.get("id") or extract_video_id(url) or "video"
        job["title"] = title  # 큐 행 제목 갱신용

        # 1.5) 긴 영상은 1시간 단위로 분할 — 통짜 디코딩은 수 GB 메모리를 먹어
        #      OOM으로 프로세스가 죽을 수 있음 (7시간 영상 기준 ~10GB 관측)
        if duration and duration > CHUNK_SEC * 1.5:
            job.update(phase="오디오 분할 중 (긴 영상)", progress=23)
            chunk_dir = tmp / "chunks"
            chunk_dir.mkdir()
            try:
                subprocess.run(
                    ["ffmpeg", "-nostdin", "-loglevel", "error",
                     "-i", str(audio), "-f", "segment",
                     "-segment_time", str(CHUNK_SEC), "-c", "copy",
                     str(chunk_dir / f"part%04d{audio.suffix}")],
                    check=True, timeout=600)
                chunks = sorted(chunk_dir.iterdir())
            except Exception:  # ffmpeg 없음/실패 시 통짜로라도 진행
                chunks = [audio]
        else:
            chunks = [audio]

        # 2) 받아쓰기 (한 번에 하나 — 순번 대기)
        job.update(phase="받아쓰기 순번 대기 중", progress=24)
        with TRANSCRIBE_SEM:
            check_cancel()
            job.update(phase="모델 로딩 중 (최초 1회는 다운로드, 수 분 걸릴 수 있음)",
                       progress=25)
            model = get_model(model_name)

            job.update(phase="받아쓰는 중", progress=27)
            items, offset, lang = [], 0.0, language or None
            for chunk in chunks:
                check_cancel()
                for attempt in (1, 2):
                    mark = len(items)
                    try:
                        segments, seg_info = model.transcribe(
                            str(chunk),
                            language=lang,
                            vad_filter=True,
                        )
                        for seg in segments:
                            check_cancel()
                            items.append({"text": seg.text,
                                          "start": seg.start + offset,
                                          "dur": seg.end - seg.start})
                            pos = offset + seg.end
                            if duration:
                                job["progress"] = 27 + 73 * min(pos / duration, 1.0)
                            else:  # 길이를 모르면 진행 지점이라도 표시
                                job["phase"] = f"받아쓰는 중 · {fmt_ts(pos)} 지점"
                        break
                    except JobCancelled:
                        raise
                    except RuntimeError as e:
                        # GPU 라이브러리 문제는 받아쓰기 도중에도 터질 수 있음
                        # → 이 청크를 CPU 모델로 다시 시도하고 이후로도 CPU 유지
                        del items[mark:]
                        if attempt == 1 and _is_cuda_error(e):
                            job.update(phase="GPU 오류 — CPU로 전환해서 계속함")
                            model = get_model(model_name, force_cpu=True)
                        else:
                            raise
                lang = lang or seg_info.language  # 청크 간 언어 통일
                offset += _ffprobe_duration(chunk) or CHUNK_SEC

        res = build_result(video_id, title, items,
                           lang or seg_info.language, f"whisper:{model_name}")
        history_add(res)
        job.update(status="done", progress=100, result=res,
                   ended_at=time.time())

    except JobCancelled:
        job.update(status="cancelled", phase="취소됨", ended_at=time.time())
    except Exception as e:
        if job.get("cancel"):  # yt-dlp가 훅에서 난 예외를 감싸서 올리는 경우
            job.update(status="cancelled", phase="취소됨", ended_at=time.time())
        else:
            job.update(status="error", error=f"{type(e).__name__}: {e}",
                       ended_at=time.time())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.post("/api/stt")
def api_stt():
    _prune_jobs()
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="JSON 요청이 필요해요."), 400
    url = data.get("url", "")
    model_name = data.get("model", "large-v3-turbo")
    language = data.get("language") or None

    if not extract_video_id(url):
        return jsonify(error="유튜브 링크를 인식하지 못했어요."), 400

    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "running", "phase": "시작", "progress": 0}
    threading.Thread(target=stt_worker,
                     args=(job_id, url, model_name, language),
                     daemon=True).start()
    return jsonify(job_id=job_id)


@app.get("/api/stt/status")
def api_stt_status():
    _prune_jobs()
    job = JOBS.get(request.args.get("id", ""))
    if not job:
        return jsonify(error="작업을 찾을 수 없어요."), 404
    return jsonify(job)


@app.post("/api/stt/cancel")
def api_stt_cancel():
    data = request.get_json(silent=True) or {}
    job = JOBS.get(data.get("id", ""))
    if not job:
        return jsonify(error="작업을 찾을 수 없어요."), 404
    job["cancel"] = True
    return jsonify(ok=True)

# ---------------------------------------------------------------- 페이지

@app.get("/")
def index():
    return Response(PAGE.replace("__BATCH_MAX__", str(BATCH_MAX)),
                    mimetype="text/html")


PAGE = r"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>yt2text</title>
<style>
  :root{
    --bg:#20242B; --panel:#2B3038; --panel-hi:#343A44;
    --sheet:#FBFAF7; --ink:#22252A; --ink-soft:#6B7078;
    --line:#E4E1D9; --accent:#C63B2F; --chip:#EFEDE6; --warn:#E0B252;
    --fg:#FFFFFF; --fg-soft:#9AA0A8; --fg-mid:#C9CDD3; --ok:#9CC79A;
    --mono:ui-monospace,'SF Mono','JetBrains Mono',Menlo,monospace;
    --sans:-apple-system,BlinkMacSystemFont,'Apple SD Gothic Neo','Pretendard','Noto Sans KR',sans-serif;
  }
  *{box-sizing:border-box;margin:0}
  body{background:var(--bg);font-family:var(--sans);min-height:100vh;
       display:flex;flex-direction:column;align-items:center;padding:48px 20px 80px}
  .col{width:100%;max-width:760px}
  .brand{font-family:var(--mono);font-size:13px;letter-spacing:.14em;color:var(--fg-soft)}
  .brand b{color:var(--fg);font-weight:600}

  .mode{display:inline-flex;background:var(--panel);border-radius:10px;padding:3px;margin-top:16px}
  .mode button{border:0;background:none;color:var(--fg-soft);font-family:var(--sans);
       font-size:13.5px;font-weight:600;padding:8px 18px;border-radius:8px;cursor:pointer}
  .mode button.on{background:var(--accent);color:#fff}

  .inbar{display:flex;gap:10px;margin-top:12px;align-items:flex-start}
  .inbar textarea{flex:1;padding:14px 16px;font-size:14px;border:0;border-radius:10px;
       background:var(--panel);color:var(--fg);font-family:var(--mono);outline:none;
       min-width:0;resize:none;line-height:1.6;overflow-y:auto}
  .inbar textarea::placeholder{color:#6B7078}
  .inbar textarea:focus{box-shadow:0 0 0 2px var(--accent)}
  .btn{padding:14px 22px;font-size:15px;font-weight:600;border:0;border-radius:10px;
       background:var(--accent);color:#fff;cursor:pointer;font-family:var(--sans);white-space:nowrap}
  .btn:hover{background:#B23328}
  .btn:disabled{opacity:.5;cursor:wait}
  .hint{font-family:var(--mono);font-size:11.5px;color:#5A6068;margin-top:8px}

  .wopts{display:none;gap:10px;margin-top:12px;align-items:center;flex-wrap:wrap}
  .wopts.show{display:flex}
  .wopts select{padding:11px 12px;border:0;border-radius:10px;background:var(--panel);
       color:var(--fg-mid);font-family:var(--mono);font-size:13px;outline:none}
  .err{margin-top:12px;color:#F3A19A;font-size:14px;min-height:18px}

  .queue{margin-top:14px;display:none}
  .queue.show{display:block}
  .qrow{display:flex;gap:12px;background:var(--panel);border-radius:10px;
       padding:10px 12px;margin-bottom:8px;align-items:center}
  .qrow.dup{box-shadow:inset 3px 0 0 var(--warn)}
  .qrow.done{cursor:pointer}
  .qrow.done:hover{background:var(--panel-hi)}
  .qrow img{width:84px;aspect-ratio:16/9;border-radius:6px;object-fit:cover;
       background:#181B20;flex-shrink:0}
  .qmain{flex:1;min-width:0}
  .qtitle{font-size:13px;font-weight:600;color:var(--fg);
       white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .qstat{font-family:var(--mono);font-size:11.5px;color:var(--fg-soft);margin-top:4px}
  .qrow.dup .qstat{color:var(--warn)}
  .qrow.done .qstat{color:var(--ok)}
  .qrow.fail .qstat{color:#F3A19A}
  .qbar{height:3px;background:#20242B;border-radius:2px;margin-top:7px;
       overflow:hidden;display:none}
  .qbar.show{display:block}
  .qbar div{height:100%;width:0%;background:var(--accent);transition:width .5s}
  .qacts{display:flex;gap:6px;flex-shrink:0}
  .qbtn{font-size:12px;font-weight:600;border:0;border-radius:7px;padding:7px 11px;
       cursor:pointer;font-family:var(--sans);background:#20242B;color:var(--fg-mid)}
  .qbtn:hover{background:#181B20}
  .qbtn.hot{background:var(--accent);color:#fff}
  .qbtn.hot:hover{background:#B23328}

  .sheet{background:var(--sheet);border-radius:14px;margin-top:16px;
       box-shadow:0 20px 50px rgba(0,0,0,.35);display:none;overflow:hidden}
  .sheet.show{display:block}
  .cover{padding:26px 34px 20px;border-bottom:1px solid var(--line)}
  .cover h1{font-size:19px;line-height:1.4;color:var(--ink);font-weight:700}
  .meta{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px;align-items:center}
  .chip{font-family:var(--mono);font-size:11.5px;padding:4px 10px;border-radius:99px;
       background:var(--chip);color:var(--ink-soft)}
  .chip.lang{cursor:pointer;border:1px solid transparent}
  .chip.lang:hover{border-color:var(--ink-soft)}
  .chip.lang.on{background:var(--ink);color:#fff}
  .chip.ylink{color:var(--accent);border:1px solid var(--accent);background:none;
       text-decoration:none;font-weight:600}
  .chip.ylink:hover{background:var(--accent);color:#fff}
  .tools{display:flex;gap:14px;padding:12px 34px;border-bottom:1px solid var(--line);
       align-items:center;flex-wrap:wrap}
  .tools label{font-size:13px;color:var(--ink-soft);display:flex;gap:6px;align-items:center;cursor:pointer}
  .tools .sp{flex:1}
  .tbtn{font-size:13px;font-weight:600;color:var(--ink);background:none;border:1px solid var(--line);
       border-radius:8px;padding:7px 13px;cursor:pointer;font-family:var(--sans)}
  .tbtn:hover{border-color:var(--ink)}
  .tbtn.done{color:var(--accent);border-color:var(--accent)}
  .body{padding:30px 34px 40px;max-height:62vh;overflow-y:auto}
  .body p{font-size:15.5px;line-height:1.85;color:var(--ink);margin-bottom:1.2em;word-break:keep-all}
  .body .ts{font-family:var(--mono);font-size:12px;color:var(--accent);margin-right:10px;flex-shrink:0}
  .body a.ts{text-decoration:none}
  .body a.ts:hover{text-decoration:underline}
  .body .row{display:flex;margin-bottom:.5em}
  .body .row span:last-child{font-size:15px;line-height:1.7;color:var(--ink)}

  .histhead{display:flex;align-items:baseline;gap:10px;margin:40px 0 14px}
  .histhead .lab{font-family:var(--mono);font-size:12px;letter-spacing:.16em;color:var(--fg-soft)}
  .histhead .cnt{font-family:var(--mono);font-size:12px;color:#5A6068}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px}
  .card{background:var(--panel);border-radius:12px;overflow:hidden;cursor:pointer;
       position:relative;transition:background .15s}
  .card:hover{background:var(--panel-hi)}
  .card img{width:100%;aspect-ratio:16/9;object-fit:cover;display:block;background:#181B20}
  .card .cbody{padding:12px 14px 14px}
  .card .ctitle{font-size:13.5px;font-weight:600;color:var(--fg);line-height:1.45;
       display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;
       min-height:2.9em;word-break:keep-all}
  .card .cmeta{font-family:var(--mono);font-size:11px;color:var(--fg-soft);margin-top:8px}
  .card .src{display:inline-block;padding:1px 7px;border-radius:99px;margin-right:6px;
       font-size:10.5px;background:#20242B;color:var(--fg-mid)}
  .card .src.w{color:#F0A9A2}
  .card .del{position:absolute;top:8px;right:8px;width:24px;height:24px;border:0;border-radius:6px;
       background:rgba(0,0,0,.55);color:#C9CDD3;font-size:14px;line-height:1;cursor:pointer;
       display:none;align-items:center;justify-content:center}
  .card:hover .del{display:flex}
  .card .del:hover{background:var(--accent);color:#fff}
  .empty{font-size:13.5px;color:#5A6068;padding:8px 2px}
  @media (max-width:560px){ .cover,.tools,.body{padding-left:20px;padding-right:20px} }
</style>
</head>
<body>
  <div class="col">
    <div class="brand"><b>yt2text</b> · local · caption + whisper</div>

    <div class="mode" id="mode">
      <button id="m_cap" onclick="setMode('cap')">자막 (빠름)</button>
      <button id="m_stt" onclick="setMode('stt')">Whisper (정확)</button>
    </div>

    <div class="inbar">
      <textarea id="url" rows="1" placeholder="https://www.youtube.com/watch?v=..."
        autofocus oninput="growBox(this)"
        onkeydown="if(event.key==='Enter'&&(event.metaKey||event.ctrlKey)){event.preventDefault();go();}"></textarea>
      <button class="btn" id="gobtn" onclick="go()">추출</button>
    </div>
    <div class="hint">여러 개는 줄바꿈으로 구분 (최대 __BATCH_MAX__개) · ⌘/Ctrl+Enter로 추출</div>

    <div class="wopts" id="wopts">
      <select id="model">
        <option value="large-v3-turbo" selected>large-v3-turbo · 추천</option>
        <option value="large-v3">large-v3 · 최고 품질, 느림</option>
        <option value="medium">medium · 절충</option>
        <option value="small">small · 빠름, 품질 낮음</option>
      </select>
      <select id="wlang">
        <option value="" selected>언어 자동 감지</option>
        <option value="ko">한국어 고정</option>
        <option value="en">영어 고정</option>
      </select>
    </div>
    <div class="err" id="err"></div>
    <button class="qbtn" id="stopall" style="display:none"
      onclick="stopAllRetries()">차단 재시도 전체 중지</button>

    <div class="queue" id="queue"></div>

    <div class="sheet" id="sheet">
      <div class="cover">
        <h1 id="title"></h1>
        <div class="meta" id="meta"></div>
      </div>
      <div class="tools">
        <label><input type="checkbox" id="tsbox" onchange="render()"> 타임스탬프</label>
        <div class="sp"></div>
        <button class="tbtn" id="copybtn" onclick="copyText()">복사</button>
        <button class="tbtn" onclick="dl('txt')">.txt</button>
        <button class="tbtn" onclick="dl('md')">.md</button>
        <button class="tbtn" onclick="dl('srt')">.srt</button>
      </div>
      <div class="body" id="out"></div>
    </div>

    <div class="histhead">
      <span class="lab">HISTORY</span><span class="cnt" id="histcnt"></span>
    </div>
    <div class="grid" id="grid"></div>
    <div class="empty" id="empty" style="display:none">아직 추출한 영상이 없어요.</div>
  </div>

<script>
let D = null, HIST = [], ROWS = {}, POLL = null;
const $ = id => document.getElementById(id);
const IDRE = /(?:youtu\.be\/|youtube\.com\/(?:watch\?v=|shorts\/|live\/|embed\/)|^)([A-Za-z0-9_-]{11})/;
const BATCH_MAX = __BATCH_MAX__;

function growBox(el){
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 230) + 'px';
}

// ------------------------------------------------ 모드
function setMode(m){
  localStorage.setItem('yt2text_mode', m);
  $('m_cap').classList.toggle('on', m==='cap');
  $('m_stt').classList.toggle('on', m==='stt');
  $('wopts').classList.toggle('show', m==='stt');
}
function getMode(){ return localStorage.getItem('yt2text_mode') || 'cap'; }

// ------------------------------------------------ 배치 큐
function go(){
  $('err').textContent = '';
  const lines = $('url').value.split('\n').map(s=>s.trim()).filter(Boolean);
  if(!lines.length){ $('err').textContent = '링크를 붙여넣어 주세요.'; return; }

  const ids = [], bad = [];
  for(const line of lines){
    const m = line.match(IDRE);
    if(m && !ids.includes(m[1])) ids.push(m[1]);
    else if(!m) bad.push(line);
  }
  if(bad.length) $('err').textContent = `인식 실패 ${bad.length}건은 건너뛰어요.`;
  if(ids.length > BATCH_MAX){
    $('err').textContent += ` ${BATCH_MAX}개까지만 — 앞의 ${BATCH_MAX}개를 처리해요.`;
    ids.length = BATCH_MAX;
  }
  if(!ids.length) return;

  // 돌고 있는 작업이 있으면 큐를 리셋하지 않고 뒤에 이어붙임
  const running = Object.values(ROWS).some(r => r.busy);
  if(!running){ $('queue').innerHTML = ''; ROWS = {}; }
  $('queue').classList.add('show');

  for(const vid of ids){
    if(ROWS[vid]) continue;  // 이미 큐에 있는 영상
    const prev = HIST.find(h => h.video_id === vid);  // 중복 감지
    makeRow(vid, prev);
    if(!prev) startRow(vid);
  }
}

function makeRow(vid, prev){
  const row = document.createElement('div');
  row.className = 'qrow' + (prev ? ' dup' : '');
  row.innerHTML = `
    <img loading="lazy" alt="">
    <div class="qmain">
      <div class="qtitle"></div>
      <div class="qstat"></div>
      <div class="qbar"><div></div></div>
    </div>
    <div class="qacts"></div>`;
  const img = row.querySelector('img');
  img.src = '/thumbs/' + vid + '.jpg';
  img.onerror = ()=>{ img.onerror = null;
    img.src = 'https://i.ytimg.com/vi/' + vid + '/mqdefault.jpg'; };
  row.querySelector('.qtitle').textContent = prev ? prev.title : vid;

  if(prev){
    const srcLabel = prev.source === 'caption' ? 'caption' : 'whisper';
    row.querySelector('.qstat').textContent =
      `이전에 추출한 기록이 있어요 · ${srcLabel} · ${(prev.saved_at||'').slice(5,16)}`;
    const acts = row.querySelector('.qacts');
    const openB = btn('열기', 'hot', ()=>openHistory(prev.key));
    const reB = btn('다시 추출', '', ()=>{
      row.classList.remove('dup');
      acts.innerHTML = '';
      startRow(vid);
    });
    acts.append(openB, reB);
  }

  $('queue').appendChild(row);
  ROWS[vid] = {el: row, result: null, busy: false};
}

function btn(label, cls, fn){
  const b = document.createElement('button');
  b.className = 'qbtn' + (cls?' '+cls:''); b.textContent = label; b.onclick = fn;
  return b;
}

function rowStat(vid, text){ ROWS[vid].el.querySelector('.qstat').textContent = text; }
function rowTitle(vid, text){ if(text) ROWS[vid].el.querySelector('.qtitle').textContent = text; }
function rowBar(vid, pct){
  const bar = ROWS[vid].el.querySelector('.qbar');
  bar.classList.add('show');
  bar.firstElementChild.style.width = pct + '%';
}
function rowDone(vid, result){
  const r = ROWS[vid];
  r.result = result;
  r.busy = false;
  r.el.classList.add('done');
  r.el.querySelector('.qbar').classList.remove('show');
  r.el.querySelector('.qacts').innerHTML = '';
  rowTitle(vid, result.title);
  rowStat(vid, '완료 · 클릭해서 열기');
  r.el.onclick = ()=>{ D = result; show(); };
  loadHistory();
  // 단독 작업이면 바로 열어줌
  if(Object.keys(ROWS).length === 1){ D = result; show(); }
}
function rowFail(vid, msg){
  const r = ROWS[vid];
  r.busy = false;
  r.el.classList.add('fail');
  r.el.querySelector('.qbar').classList.remove('show');
  r.el.querySelector('.qacts').innerHTML = '';
  rowStat(vid, msg);
}

function startRow(vid){
  getMode() === 'cap' ? runCaption(vid) : runStt(vid);
}

// ------------------------------------------------ 자막 루트 (행 단위)
function retryLater(vid, sec, attempt, fn){
  const r = ROWS[vid];
  let left = sec;
  const tick = ()=>rowStat(vid,
    `유튜브 차단 감지 — ${left}초 후 자동 재시도 (${attempt}/5)`);
  tick();
  const acts = r.el.querySelector('.qacts');
  acts.innerHTML = '';
  acts.appendChild(btn('중지', '', ()=>stopRetry(vid)));
  r.retryTimer = setInterval(()=>{
    if(--left > 0) return tick();
    clearInterval(r.retryTimer); r.retryTimer = null;
    updateStopAll();
    fn();
  }, 1000);
  updateStopAll();
}

function stopRetry(vid){
  const r = ROWS[vid];
  if(r.retryTimer){ clearInterval(r.retryTimer); r.retryTimer = null; }
  rowFail(vid, '중지됨 — 나중에 다시 추출하세요');
  updateStopAll();
}

function stopAllRetries(){
  for(const vid in ROWS) if(ROWS[vid].retryTimer) stopRetry(vid);
}

function updateStopAll(){
  const any = Object.values(ROWS).some(r => r.retryTimer);
  $('stopall').style.display = any ? 'inline-block' : 'none';
}

async function runCaption(vid, lang, attempt){
  attempt = attempt || 0;
  ROWS[vid].busy = true;
  rowStat(vid, '자막 추출 중...');
  try{
    const r = await fetch('/api/transcript', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({url: vid, lang: lang || null})
    });
    const j = await r.json();
    if(!r.ok){
      // 차단(429)이면 서버가 알려준 쿨다운만큼 쉬었다가 자동 재시도
      if(r.status === 429 && attempt < 5){
        const wait = (j.retry_after || 60) + Math.floor(Math.random()*15);
        retryLater(vid, wait, attempt+1, ()=>runCaption(vid, lang, attempt+1));
        return;
      }
      rowFail(vid, j.no_captions
        ? '자막 없음 — Whisper 모드로 다시 추출하세요'
        : (j.error || '오류'));
      if(j.no_captions){
        const acts = ROWS[vid].el.querySelector('.qacts');
        acts.innerHTML = '';
        acts.appendChild(btn('Whisper로 추출', 'hot', ()=>{
          setMode('stt');
          ROWS[vid].el.classList.remove('fail');
          acts.innerHTML = '';
          runStt(vid);
        }));
      }
      return;
    }
    rowDone(vid, j);
  }catch(e){ rowFail(vid, '서버 연결 실패'); }
}

// ------------------------------------------------ STT 루트 (행 단위)
async function runStt(vid){
  ROWS[vid].busy = true;
  rowStat(vid, '작업 등록 중...');
  try{
    const r = await fetch('/api/stt', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        url: vid,
        model: $('model').value,
        language: $('wlang').value || null
      })
    });
    const j = await r.json();
    if(!r.ok){ rowFail(vid, j.error); return; }
    ROWS[vid].jobId = j.job_id;
    const acts = ROWS[vid].el.querySelector('.qacts');
    acts.innerHTML = '';
    acts.appendChild(btn('취소', '', ()=>cancelStt(vid)));
    ensurePolling();
  }catch(e){ rowFail(vid, '서버 연결 실패'); }
}

async function cancelStt(vid){
  const id = ROWS[vid].jobId;
  if(!id) return;
  rowStat(vid, '취소하는 중...');
  try{
    await fetch('/api/stt/cancel', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({id})
    });
  }catch(e){}
}

function ensurePolling(){
  if(POLL) return;
  POLL = setInterval(async ()=>{
    let active = false;
    for(const vid in ROWS){
      const row = ROWS[vid];
      if(!row.jobId || row.result || row.el.classList.contains('fail')) continue;
      active = true;
      try{
        const r = await fetch('/api/stt/status?id=' + row.jobId);
        const j = await r.json();
        if(!r.ok){  // 서버 재시작 등으로 작업이 사라진 경우
          row.jobId = null;
          rowFail(vid, j.error || '작업 정보를 잃어버렸어요 (서버 재시작?)');
          continue;
        }
        if(j.title) rowTitle(vid, j.title);
        if(j.status === 'running'){
          rowStat(vid, j.phase || '');
          rowBar(vid, j.progress || 0);
        }else if(j.status === 'done'){
          row.jobId = null; rowDone(vid, j.result);
        }else if(j.status === 'cancelled'){
          row.jobId = null; rowFail(vid, '취소됨');
        }else if(j.status === 'error'){
          row.jobId = null; rowFail(vid, j.error);
        }
      }catch(e){}
    }
    if(!active){ clearInterval(POLL); POLL = null; }
  }, 1000);
}

// ------------------------------------------------ 히스토리
async function loadHistory(){
  try{
    const r = await fetch('/api/history');
    HIST = await r.json();
    const grid = $('grid');
    grid.innerHTML = '';
    $('histcnt').textContent = HIST.length ? HIST.length + '개' : '';
    $('empty').style.display = HIST.length ? 'none' : 'block';
    HIST.forEach(e=>{
      const card = document.createElement('div');
      card.className = 'card';
      const isW = e.source !== 'caption';
      card.innerHTML = `
        <img loading="lazy" alt="">
        <button class="del" title="삭제">&times;</button>
        <div class="cbody">
          <div class="ctitle"></div>
          <div class="cmeta">
            <span class="src ${isW?'w':''}">${isW?'whisper':'caption'}</span>
            <span class="dur"></span> · <span class="when"></span>
          </div>
        </div>`;
      const img = card.querySelector('img');
      img.src = '/thumbs/' + e.video_id + '.jpg';
      img.onerror = ()=>{ img.onerror = null; img.src = e.thumb; };
      card.querySelector('.ctitle').textContent = e.title;
      card.querySelector('.dur').textContent = e.duration || '';
      card.querySelector('.when').textContent = (e.saved_at||'').slice(5,16);
      card.onclick = ()=>openHistory(e.key);
      card.querySelector('.del').onclick = ev=>{
        ev.stopPropagation(); delHistory(e.key);
      };
      grid.appendChild(card);
    });
    // 제목이 깨진 항목이 있으면 서버가 뒤에서 복구 중 — 잠시 뒤 다시 불러옴
    if(HIST.some(e => e.title === e.video_id) && !window._titlePoll){
      window._titlePoll = setTimeout(()=>{
        window._titlePoll = null; loadHistory();
      }, 8000);
    }
  }catch(e){}
}

async function openHistory(key){
  const r = await fetch('/api/history/' + encodeURIComponent(key));
  if(!r.ok) return;
  D = await r.json(); show();
  $('sheet').scrollIntoView({behavior:'smooth', block:'start'});
}

async function delHistory(key){
  await fetch('/api/history/' + encodeURIComponent(key), {method:'DELETE'});
  loadHistory();
}

// ------------------------------------------------ 결과 표시
function show(){
  $('sheet').classList.add('show');
  $('title').textContent = D.title || ('영상 ' + D.video_id);
  const meta = $('meta');
  meta.innerHTML = '';
  meta.appendChild(linkChip('YouTube ↗', 'https://youtu.be/' + D.video_id));
  meta.appendChild(chip(D.duration));
  if(D.source === 'caption'){
    meta.appendChild(chip(D.is_generated ? '자동 생성 자막' : '업로더 자막'));
    (D.available || []).forEach(a=>{
      const c = chip(a.code, 'lang' + (a.code===D.language_code?' on':''));
      c.title = a.name;
      c.onclick = ()=>recap(D.video_id, a.code);
      meta.appendChild(c);
    });
  }else{
    meta.appendChild(chip(D.source));
    meta.appendChild(chip('감지 언어: ' + D.language));
  }
  render();
}

async function recap(vid, lang){
  const r = await fetch('/api/transcript', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({url: vid, lang})
  });
  if(r.ok){ D = await r.json(); show(); loadHistory(); }
}

function chip(text, extra){
  const s = document.createElement('span');
  s.className = 'chip' + (extra?' '+extra:''); s.textContent = text;
  return s;
}

function linkChip(text, href){
  const a = document.createElement('a');
  a.className = 'chip ylink'; a.textContent = text;
  a.href = href; a.target = '_blank'; a.rel = 'noopener';
  return a;
}

function tsSec(l){  // 구버전 히스토리엔 s(초)가 없으니 "H:MM:SS"에서 계산
  if(l.s != null) return l.s;
  return l.t.split(':').map(Number).reduce((a,b)=>a*60+b, 0);
}

function render(){
  const out = $('out');
  out.innerHTML = '';
  if($('tsbox').checked){
    D.lines.forEach(l=>{
      const d = document.createElement('div'); d.className='row';
      const t = document.createElement('a'); t.className='ts'; t.textContent = l.t;
      t.href = 'https://youtu.be/' + D.video_id + '?t=' + tsSec(l);
      t.target = '_blank'; t.rel = 'noopener';
      t.title = '유튜브에서 이 지점 열기';
      const x = document.createElement('span'); x.textContent = l.text;
      d.append(t,x); out.appendChild(d);
    });
  }else{
    D.paragraphs.forEach(p=>{
      const el = document.createElement('p'); el.textContent = p; out.appendChild(el);
    });
  }
}

function plainText(){
  if($('tsbox').checked)
    return D.lines.map(l=>`[${l.t}] ${l.text}`).join('\n');
  return D.paragraphs.join('\n\n');
}

async function copyText(){
  const text = plainText();
  let ok = false;
  try{
    if(navigator.clipboard && window.isSecureContext){
      await navigator.clipboard.writeText(text);
      ok = true;
    }
  }catch(e){}
  if(!ok){
    // HTTP(IP/Tailscale) 접속은 clipboard API가 막혀 있어서 구식 방식으로
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.focus(); ta.select();
    try{ ok = document.execCommand('copy'); }catch(e){}
    ta.remove();
  }
  const b = $('copybtn');
  b.textContent = ok ? '복사됨' : '복사 실패';
  b.classList.toggle('done', ok);
  setTimeout(()=>{ b.textContent='복사'; b.classList.remove('done'); }, 1500);
}

function dl(kind){
  let text, name = (D.title || D.video_id).replace(/[\\/:*?"<>|]/g,'_');
  if(kind==='srt'){
    text = D.lines.map((l,i)=>`${i+1}\n${l.srt_a} --> ${l.srt_b}\n${l.text}\n`).join('\n');
  }else if(kind==='md'){
    text = `# ${D.title || D.video_id}\n\n> https://youtu.be/${D.video_id} · ${D.duration} · ${D.language}\n\n`
         + D.paragraphs.join('\n\n');
  }else{
    text = plainText();
  }
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([text], {type:'text/plain;charset=utf-8'}));
  a.download = `${name}.${kind}`; a.click(); URL.revokeObjectURL(a.href);
}

// ------------------------------------------------ 초기화
setMode(getMode());
loadHistory();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="yt2text — 유튜브 → 텍스트")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    if args.host in ("127.0.0.1", "localhost", "::1"):
        ALLOWED_HOSTS = {f"localhost:{args.port}", f"127.0.0.1:{args.port}",
                         f"[::1]:{args.port}"}
    else:  # 외부 바인딩은 사용자가 의도한 것 — Host 검사 생략
        ALLOWED_HOSTS = set()

    shown = "localhost" if args.host in ("127.0.0.1", "localhost", "::1") else args.host
    print(f"\n  yt2text →  http://{shown}:{args.port}\n")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
