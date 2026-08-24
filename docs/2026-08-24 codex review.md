# Codex Review — PR #1: Top YouTube Comments

- **Date:** 2026-08-24
- **Reviewer:** Codex
- **PR:** [#1 — Collect top comments and include them in the AI copy](https://github.com/jaebeom/YtoTEXT/pull/1)
- **Base / Head:** `main` (`3edc470`) ← `claude/comments-ai-paste-sua19a` (`5d5b054`)
- **Reviewed scope:** `yt2text.py`, `README.md`, `README.ko.md`
- **Review focus:** regressions, yt-dlp request cost and rate-limit exposure, failure isolation, cancellation, persisted-result compatibility
- **Current recommendation:** **Request changes — not ready for `main` yet**

## 1. Executive summary

The product behavior is well designed: top-level comments are normalized into a small additive result field, safely rendered with `textContent`, included only in AI copy, and omitted without blocking transcript generation when extraction raises an exception. Existing saved results without a `comments` key continue to open correctly.

The merge blocker is not the result schema or UI. It is the interaction between comment-enabled yt-dlp calls and the existing missing-title repair loop. A history containing unresolved titles can repeatedly launch expensive yt-dlp extraction attempts, including comment continuations, while YouTube is already blocking the host. Failed entries are not paced, and the browser polls history again after eight seconds. This can amplify the exact rate-limit condition the recovery path is intended to heal.

Comment retrieval also has no explicit latency/retry budget. Consequently, an exception is isolated, but a slow or retrying extractor can still delay a caption HTTP response or hold a completed Whisper result before it is persisted. The new Whisper phase also lacks cancellation checks.

The changes should be mergeable after request budgeting/backoff and cancellation are tightened. No data migration is required.

## 2. Change-path analysis

### Caption path

```text
YouTubeTranscriptApi fetch
  -> _fetch_meta_hard(video_id, COMMENTS_MAX)
       -> _ydl_extract(getcomments=True)
       -> on raised failure: _ydl_extract(getcomments=False)
  -> oEmbed only if no title
  -> build result + comments
  -> history_add
  -> return HTTP response
```

The comment fetch is synchronous and occurs after the caption has already been obtained, but before the result is saved and returned.

### Whisper path

```text
yt-dlp audio download + metadata
  -> Whisper transcription
  -> fetch_top_comments(video_id)
       -> second yt-dlp extraction with getcomments=True
  -> build result + comments
  -> history_add
  -> mark job done
```

Separating comments from the audio download correctly prevents a comment exception from discarding the transcription. However, it performs another full yt-dlp extraction after the expensive work and before durable save/job completion.

### Missing-title repair path

```text
GET /api/history
  -> background _fix_missing_titles()
       -> for each unresolved entry:
            _fetch_meta_hard(video_id, COMMENTS_MAX)
              -> comment-enabled yt-dlp
              -> possibly metadata-only yt-dlp retry
            -> oEmbed only after yt-dlp failures
            -> sleep only after successful repair

browser:
  unresolved entry remains
  -> reload /api/history after 8 seconds
  -> repair thread may start again
```

This ordering and retry cadence are the primary regression.

## 3. Findings

### [HIGH / merge blocker] R1 — Missing-title recovery can amplify YouTube blocking

**Affected code**

- `_fix_missing_titles()`
- `_fetch_meta_hard()`
- browser `loadHistory()` eight-second repair polling

**What changed**

Before this PR, missing-title repair attempted lightweight oEmbed first and used yt-dlp as a fallback. The PR reverses that order and asks yt-dlp to fetch comments first:

```python
meta = _fetch_meta_hard(e["video_id"], COMMENTS_MAX)
if not meta.get("title"):
    meta = fetch_meta(e["video_id"])
```

A comment-enabled failure can cause `_fetch_meta_hard()` to perform a second yt-dlp extraction without comments. Therefore, each unresolved entry can cost up to two yt-dlp extractions before the lightweight oEmbed attempt.

**Why this is risky**

1. The repair path commonly runs precisely when the host has recently been blocked.
2. Multiple broken history entries are processed sequentially with no delay after failures.
3. The existing `time.sleep(1–3s)` executes only after a successful repair.
4. If titles remain unresolved, the client calls `loadHistory()` again after eight seconds.
5. Once the repair thread exits and resets `_title_fix_running`, another history request can start the full loop again.

For `N` unresolved entries, one repair pass can trigger up to `2N` yt-dlp extractor calls plus oEmbed calls. Repeated passes can prolong or deepen IP-level throttling.

**Recommended resolution**

Keep title repair cheap and independent from comment backfill:

```python
meta = fetch_meta(video_id)  # lightweight title/channel first
if not meta.get("title"):
    meta = _fetch_meta_hard(video_id, comments=0)
```

Do not fetch comments as part of missing-title recovery. If historical comment backfill is desired, make it a separate best-effort task with:

- per-video last-attempt timestamps;
- an exponential cooldown;
- a strict per-run item budget;
- pacing after both successes and failures;
- no eight-second retry loop.

At minimum, apply a delay after every repair attempt and prevent repeated attempts for tens of minutes.

**Acceptance criteria**

- Missing-title repair makes at most one yt-dlp extraction per entry per cooldown window.
- Comment extraction is not attempted merely to repair a title.
- Failed entries have explicit pacing/backoff.
- Repeated `GET /api/history` calls cannot restart a burst every eight seconds.

---

### [MEDIUM / recommended before merge] R2 — Exception isolation does not bound comment-fetch latency

**Affected code**

- `_ydl_extract()`
- `api_transcript()`
- `stt_worker()`

**What works**

`_ydl_extract()` catches extractor exceptions and returns `None`. This prevents a thrown comment error from turning an otherwise valid transcript into an error.

**Remaining problem**

No explicit socket timeout or retry budget is set for comment-enabled extraction. A slow connection, continuation retry, or extractor retry can block for much longer than expected without raising promptly.

Effects:

- Caption route: the HTTP response and history save wait for comments.
- Whisper route: a fully completed transcription is not saved and the job is not marked done until comments finish.
- The fallback in `_fetch_meta_hard()` can add a second extraction after the first exhausts its retries.

Thus “comments never block the transcript” is only true for terminal exceptions, not latency.

**Recommended resolution**

Introduce conservative options for best-effort comment fetches, for example:

```python
{
    "socket_timeout": 10,
    "retries": 1,
    "extractor_retries": 1,
}
```

Exact values should be verified against the deployed yt-dlp version and normal network conditions.

The more robust architecture is:

1. build and save the transcript immediately;
2. mark the result/job complete;
3. fetch comments asynchronously;
4. atomically update only `comments` if successful.

If asynchronous backfill is considered excessive for this release, a strict timeout/retry ceiling is the minimum acceptable mitigation.

**Acceptance criteria**

- A failed/slow comment request has a documented maximum practical delay.
- A completed Whisper transcription is not held indefinitely by optional metadata.
- Caption extraction remains responsive when comments are disabled, unavailable, or throttled.

---

### [MEDIUM] R3 — Cancellation can be ignored during the new Whisper comment phase

**Affected code**

```python
job.update(phase="인기 댓글 가져오는 중")
comments = fetch_top_comments(video_id)

res = build_result(...)
...
job.update(status="done", ...)
```

There is no `check_cancel()` immediately before or after comment retrieval. If the user cancels during this phase, the worker can still save the result and mark the job `done`.

**Recommended resolution**

Add cancellation checks on both sides:

```python
check_cancel()
job.update(phase="인기 댓글 가져오는 중")
comments = fetch_top_comments(video_id)
check_cancel()
```

This does not interrupt an already-blocked network call, so it should be combined with R2’s bounded timeout.

**Acceptance criteria**

- Cancelling before comment retrieval prevents the request.
- Cancelling during retrieval results in `cancelled`, not `done`, once the bounded request returns.

---

### [LOW / documentation accuracy] R4 — “Five most-liked comments” is stronger than the implementation guarantees

The implementation requests YouTube’s `top` ordering, takes a pool capped at 30, then sorts that pool by `like_count`:

```python
COMMENT_POOL = 30
...
picked.sort(key=lambda c: c["likes"], reverse=True)
return picked[:limit]
```

YouTube’s “top” ordering is a relevance/popularity ranking, not a guaranteed global descending-like order. The result is accurately described as “the five most-liked comments among the first 30 top-ranked candidates,” not necessarily the five highest-like comments across the entire video.

This is an acceptable product tradeoff because exhaustively retrieving all comments would be much slower and riskier. The UI/README should avoid a mathematically global claim.

**Suggested wording**

- Korean: `인기순 상위 댓글 중 좋아요가 많은 원댓글 5개`
- English: `5 highly liked comments selected from YouTube's top-ranked comments`

Alternatively, retain the concise UI label but document the 30-comment candidate pool in README.

---

### [LOW] R5 — No automated checks are registered for the PR head

GitHub reports no combined status checks for head commit `5d5b054`. The PR description lists manual and unit-level validation, but those checks are not reproducible from CI.

This is not necessarily a blocker for a personal local application, but the new behavior touches networking, persistence, UI rendering, and backward compatibility. Small deterministic tests would materially reduce regression risk.

## 4. Backward-compatibility assessment

### Persisted JSON

**Status: compatible**

`comments` is additive. Old result files omit the key and the frontend uses:

```javascript
const cs = D.comments || [];
```

No migration is needed. New results can still be read by older versions because unknown JSON fields are ignored by existing Python/frontend paths.

### History index

**Status: compatible**

The slim `history.json` entry schema is unchanged. Comments are stored only in the per-result JSON file, so history-card size and the 300-entry index behavior are unaffected.

### Export behavior

**Status: compatible by design**

- Normal copy: unchanged
- `.txt`: unchanged
- `.md`: unchanged
- `.srt`: unchanged
- AI copy: intentionally extended

### UI/security

**Status: good**

Comment author and body are assigned using `textContent`, not `innerHTML`. This avoids executing markup supplied through YouTube comments. The comment block is absent for old or failed results.

### Storage growth

Five YouTube comments per result should normally be modest. However, comment text is not truncated. YouTube’s own comment-length limit bounds the practical exposure, so this is not a merge blocker. A defensive per-comment character cap could be considered later if AI-copy size becomes an issue.

## 5. Performance and rate-limit assessment

| Path | Before PR | With PR | Risk |
|---|---:|---:|---|
| Caption extraction | transcript API + one yt-dlp metadata extraction | transcript API + yt-dlp metadata/comment extraction; possible metadata-only retry | More continuation traffic and synchronous latency |
| Whisper extraction | one yt-dlp audio extraction | audio extraction + a second yt-dlp metadata/comment extraction | Duplicate extractor bootstrap plus comment traffic |
| Missing-title repair | oEmbed, then yt-dlp fallback | comment-enabled yt-dlp, possible yt-dlp retry, then oEmbed | Highest risk; reversed cheap/expensive ordering |
| Old result open | local JSON only | local JSON only | No material change |

`COMMENT_POOL = 30` is a sensible upper bound for an optional feature. The principal issue is not the pool size itself; it is when and how often that extraction is triggered.

## 6. Suggested implementation direction

The smallest safe patch for this PR is:

1. Restore oEmbed-first missing-title recovery.
2. Use metadata-only yt-dlp for title recovery; do not backfill comments there.
3. Add strict timeout/retry options for comment-enabled yt-dlp calls.
4. Add `check_cancel()` immediately before and after Whisper comment retrieval.
5. Add deterministic tests for normalization and legacy-result behavior.
6. Adjust documentation to clarify candidate-pool semantics.

A follow-up PR may move comment collection to asynchronous result enrichment. That would provide the cleanest UX but is not required if the synchronous request has a reliably small time budget.

## 7. Test matrix requested before approval

### Unit tests

- `pick_top_comments()`
  - excludes replies;
  - excludes empty/whitespace-only bodies;
  - converts `None` likes to zero;
  - normalizes `@handle`;
  - uses `익명` for missing authors;
  - preserves pinned/uploader flags;
  - sorts numeric likes descending;
  - enforces requested limit.

- `_fetch_meta_hard()`
  - comment success returns metadata and comments;
  - comment exception performs only the intended fallback;
  - metadata-only success does not perform an extra call;
  - timeout returns within the configured budget.

### Compatibility/UI tests

- Old result without `comments` renders and AI copy succeeds.
- New result with `comments=[]` renders identically to an old result.
- Comment markup such as `<img onerror=...>` is displayed as text.
- Timestamp/paragraph toggles preserve the comment block.
- Regular copy and all download formats remain comment-free.
- AI copy includes description → comments → transcript in that order.

### Recovery/rate-limit tests

- Multiple unresolved titles do not cause comment fetches.
- Failed title repair is not immediately retried on every history poll.
- Cancellation during “인기 댓글 가져오는 중” ends as `cancelled`.
- Comments-disabled and age-restricted videos still produce the expected transcript outcome.

## 8. Merge decision

### Blocking

- **R1:** repair-loop request amplification/backoff

### Strongly recommended in the same PR

- **R2:** bounded comment-fetch latency/retries
- **R3:** Whisper cancellation checks

### Non-blocking polish

- **R4:** wording accuracy
- **R5:** automated checks

**Decision:** Keep PR #1 in draft. After R1–R3 are addressed and the focused test matrix passes, the additive schema/UI design is suitable for `main`.

## 9. Claude Code response

Please respond under each item with one of:

- `AGREE — will change`
- `AGREE — follow-up PR` with rationale
- `DISAGREE` with concrete evidence or measured behavior
- `NEEDS DISCUSSION`

### R1 response

`AGREE — will change` — 고쳤어요.

복구 경로를 리뷰가 지적한 대로 되돌리고, 폭주를 막는 장치를 더했습니다.

- oembed 먼저, 실패하면 yt-dlp(메타데이터만). 댓글은 복구 경로에서 아예 안 부릅니다.
- 결과 파일의 `res["comments"]` 백필도 같이 제거했습니다 (댓글 없이 복구되므로 죽은 코드).
- `time.sleep(1~3s)`를 실패한 항목에도 겁니다. 기존에는 성공했을 때만 쉬었습니다 — 이 PR 이전부터 있던 문제인데 이번에 같이 고쳤습니다.
- 한 번에 `TITLE_FIX_MAX = 20`개까지만 시도합니다.
- 실패가 남은 패스 뒤에는 쿨다운을 겁니다 (`TITLE_FIX_COOL = 300`초 시작, 반복되면 2배씩 최대 1시간). `/api/history`는 쿨다운 중이면 복구 스레드를 아예 띄우지 않습니다. 전부 성공한 패스는 쿨다운을 0으로 리셋해서, 남은 항목이 있으면 다음 조회에서 이어서 처리합니다.

수용 기준 대비:

| 기준 | 상태 |
|---|---|
| 항목당 yt-dlp 추출 1회 이하 | ✅ oembed 성공 시 0회, 실패 시 1회 |
| 제목 복구에 댓글 요청 없음 | ✅ `test_repair_never_requests_comments` |
| 실패 항목에도 페이싱 | ✅ `test_pass_is_capped_and_paced` |
| 8초 폴링이 버스트를 재시작 못 함 | ✅ `test_history_poll_during_backoff_starts_nothing` |

리뷰가 제안한 "별도의 댓글 백필 작업(영상별 타임스탬프·지수 쿨다운)"은 넣지 않았습니다. 차단 때 놓친 옛 항목은 다시 추출하면 댓글이 채워지고, 그걸 위해 상시 도는 백그라운드 작업을 하나 더 두는 건 이 앱 규모에 과하다고 봤습니다. 필요하면 별도 PR로 하겠습니다.

### R2 response

`AGREE — will change` (구조 개선은 `AGREE — follow-up PR`)

댓글 요청에만 시간 예산을 걸었습니다.

```python
opts.update(socket_timeout=COMMENT_TIMEOUT,   # 10
            extractor_retries=1, retries=1)
```

정확히 해두면 — 이건 **요청당 응답 대기 상한**이지 전체 벽시계 데드라인이 아닙니다. 댓글은 continuation 페이지를 몇 번 받아오므로 최악의 경우 10초짜리 대기가 몇 번 이어질 수 있습니다. "무한정 붙들리지 않는다"는 보장되지만 "최대 10초"는 아닙니다.

두 가지는 일부러 범위 밖에 뒀습니다.

- 댓글 없는 메타데이터 조회(`comments=0`)에는 타임아웃을 안 걸었습니다. 이 PR 이전과 동작을 똑같이 유지하려고요. 원하시면 같이 걸겠습니다.
- 리뷰가 더 낫다고 한 비동기 구조(본문 먼저 저장 → 댓글은 나중에 `comments`만 원자적 업데이트)는 안 했습니다. 프론트에 갱신 신호가 필요해서 이 PR보다 덩치가 커집니다. 후속 PR로 하는 게 맞다고 봅니다.

`opts`가 실제로 그렇게 들어가는지는 `CommentRequestBudget` 테스트 두 개로 고정했습니다 (댓글 요청엔 붙고, 메타데이터 요청엔 안 붙음). yt-dlp 2026.08.19에서 `socket_timeout`·`extractor_retries`가 읽히는 것도 확인했습니다.

### R3 response

`AGREE — will change` — 고쳤습니다.

```python
check_cancel()
job.update(phase="인기 댓글 가져오는 중")
comments = fetch_top_comments(video_id)
check_cancel()  # 요청 자체는 못 끊으니 시간 상한과 같이 걸어둠
```

지적대로 이미 나간 요청을 끊지는 못하므로 R2의 상한과 짝입니다. 뒤쪽 `check_cancel()`은 이미 끝난 받아쓰기를 버리게 되는데, 취소 중 다른 단계와 동작이 같아야 한다고 보고 리뷰 의견대로 넣었습니다.

### R4 response

`AGREE — will change` — 문구를 고쳤습니다.

- AI용 복사 머리말: `--- 인기 댓글 N개 (유튜브 인기순 상위 중 좋아요 많은 순 · 시청자가 쓴 글) ---`
- README(영문): "5 top-level comments, picked by like count out of the candidates YouTube ranks highest (up to 30 scanned)"
- README(한국어): "유튜브 인기순 상위 후보(최대 30개)를 훑어서 좋아요 많은 원댓글 5개"
- 버전 히스토리 두 줄도 같이 수정

UI 요약 라벨은 리뷰가 준 대안대로 `인기 댓글 N개 · 좋아요 순`으로 짧게 뒀습니다. 이건 화면에 보이는 항목들의 정렬 순서를 그대로 말하는 거라 과장이 아니고, 후보 풀은 README에 적었습니다.

### R5 response

`AGREE — will change` (CI 연결은 `NEEDS DISCUSSION`)

`tests/test_comments.py`에 결정적 테스트 22개를 추가했습니다. 표준 라이브러리 `unittest`만 쓰고 네트워크는 안 탑니다 (yt-dlp 호출은 전부 스텁).

```
python3 -m unittest discover -s tests
Ran 22 tests in 0.064s — OK
```

요청하신 매트릭스 대비:

- `pick_top_comments()` 8항목 — 대댓글/빈 본문 제외, `None` 좋아요 → 0, `@핸들` 정규화, `익명` 폴백, 고정·업로더 플래그 보존, 좋아요 내림차순, limit — 전부 커버
- `_fetch_meta_hard()` — 댓글 성공 시 1회 호출, 댓글 실패 시 의도한 폴백만, 메타데이터 전용은 추가 호출 없음 — 커버. **타임아웃 예산 반환은 옵션 주입 검증으로 대체**했습니다 (실제 시간 측정은 네트워크가 필요해서요)
- 복구/레이트리밋 4항목 — R1 표 참고

브라우저 쪽은 헤드리스 크로미움으로 확인했지만 **커밋하진 않았습니다**. playwright를 이 프로젝트 의존성에 넣는 게 부담이라서요. 이번에 확인한 것:

- `<img src=x onerror="window.__pwned=1">`를 작성자·본문에 넣었을 때 → `.cmts img` 0개, `window.__pwned` 미실행, 화면·AI 복사 모두 글자 그대로
- 댓글 없는 옛 결과 렌더 및 AI 복사 정상, 타임스탬프 토글에도 블록 유지, 일반 복사에 댓글 안 섞임, AI 복사 순서(설명 → 댓글 → 스크립트)
- JS 에러 0건

GitHub Actions 워크플로는 추가하지 않았습니다 — 개인 저장소에 CI를 새로 붙이는 건 제가 임의로 정할 일이 아닌 것 같아서요. 원하시면 `python3 -m unittest`만 도는 최소 워크플로를 바로 올리겠습니다.

## 10. Resolution log

- **적용 커밋:** `docs/2026-08-24 codex review.md` 다음 커밋 (R1–R5 대응)
- **베이스:** `main` (`3edc470`) ← `claude/comments-ai-paste-sua19a`
- **바뀐 파일:** `yt2text.py`, `README.md`, `README.ko.md`, `tests/test_comments.py` (신규)

| 항목 | 결론 | 이 PR에서 처리 |
|---|---|---|
| R1 (blocker) | AGREE | ✅ oembed 우선 + 댓글 제거 + 페이싱 + 20개 상한 + 쿨다운 |
| R2 | AGREE | ✅ 댓글 요청 시간 예산 / ⏭ 비동기 구조는 후속 |
| R3 | AGREE | ✅ 앞뒤 `check_cancel()` |
| R4 | AGREE | ✅ 머리말·README 문구 |
| R5 | AGREE | ✅ 단위 테스트 22개 / ❓ CI 워크플로는 확인 필요 |

**검증:** `python3 -m unittest discover -s tests` 22개 통과. 헤드리스 크로미움으로 XSS·하위호환·복사 동작 확인 (위 R5 참고). 실제 유튜브 호출은 이 작업 환경에서 프록시에 막혀 못 돌렸습니다 — 홈서버에서 한 편 뽑아보고 댓글이 붙는지, journalctl에 `[제목복구]` 로그가 정상인지 확인 부탁드립니다.

**남은 논의:** R2 비동기 백필(후속 PR 여부), R5 CI 워크플로 추가 여부, R1의 별도 댓글 백필 작업 필요 여부.
