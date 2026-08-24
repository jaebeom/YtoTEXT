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

_Pending._

### R2 response

_Pending._

### R3 response

_Pending._

### R4 response

_Pending._

### R5 response

_Pending._

## 10. Resolution log

Use this section after discussion to record the final agreement, commits, verification evidence, and merge decision.

_Pending._
