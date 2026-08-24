"""yt2text 인기 댓글 수집 단위 테스트.

실행: python3 -m unittest discover -s tests   (프로젝트 루트에서)
네트워크는 안 씀 — yt-dlp 호출은 전부 스텁으로 갈아끼움.
"""

import importlib.util
import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("yt2text", ROOT / "yt2text.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["yt2text"] = mod
    spec.loader.exec_module(mod)
    return mod


yt = _load()


def comment(text="댓글", likes=0, author="@쓴사람", parent="root", **kw):
    c = {"text": text, "like_count": likes, "author": author, "parent": parent}
    c.update(kw)
    return c


class PickTopComments(unittest.TestCase):
    def test_sorts_by_likes_desc(self):
        got = yt.pick_top_comments([comment("a", 3), comment("b", 70),
                                    comment("c", 12)])
        self.assertEqual([c["text"] for c in got], ["b", "c", "a"])

    def test_drops_replies(self):
        got = yt.pick_top_comments([comment("원댓글", 1),
                                    comment("대댓글", 9999, parent="Ug_xyz")])
        self.assertEqual([c["text"] for c in got], ["원댓글"])

    def test_drops_blank_bodies(self):
        got = yt.pick_top_comments([comment("", 50), comment("   \n ", 40),
                                    comment("본문", 1)])
        self.assertEqual([c["text"] for c in got], ["본문"])

    def test_strips_surrounding_whitespace(self):
        got = yt.pick_top_comments([comment("  여백 있는 댓글\n", 1)])
        self.assertEqual(got[0]["text"], "여백 있는 댓글")

    def test_missing_likes_become_zero(self):
        got = yt.pick_top_comments([comment("좋아요 없음", None)])
        self.assertEqual(got[0]["likes"], 0)

    def test_normalizes_author_handle(self):
        got = yt.pick_top_comments([comment("ㅋ", 1, author="@handle")])
        self.assertEqual(got[0]["author"], "handle")

    def test_missing_author_falls_back(self):
        got = yt.pick_top_comments([comment("ㅋ", 1, author=None),
                                    comment("ㅋㅋ", 2, author="")])
        self.assertEqual({c["author"] for c in got}, {"익명"})

    def test_keeps_pinned_and_uploader_flags(self):
        got = yt.pick_top_comments([comment("공지", 1, author_is_uploader=True,
                                            is_pinned=True)])
        self.assertTrue(got[0]["uploader"] and got[0]["pinned"])
        plain = yt.pick_top_comments([comment("보통", 1)])[0]
        self.assertFalse(plain["uploader"] or plain["pinned"])

    def test_respects_limit(self):
        many = [comment(str(i), i) for i in range(20)]
        self.assertEqual(len(yt.pick_top_comments(many, 5)), 5)
        self.assertEqual(yt.pick_top_comments(many, 0), [])

    def test_empty_inputs(self):
        self.assertEqual(yt.pick_top_comments(None), [])
        self.assertEqual(yt.pick_top_comments([]), [])


class FetchMetaHard(unittest.TestCase):
    """_ydl_extract를 스텁으로 갈아끼워 호출 횟수·폴백 경로를 확인."""

    def setUp(self):
        self.calls = []
        self._real = yt._ydl_extract

    def tearDown(self):
        yt._ydl_extract = self._real

    def stub(self, *results):
        queue = list(results)

        def fake(video_id, comments=0):
            self.calls.append(comments)
            return queue.pop(0)
        yt._ydl_extract = fake

    def test_comment_success_is_one_call(self):
        self.stub({"title": "제목", "uploader": "채널", "description": "설명",
                   "comments": [comment("좋은 영상", 5)]})
        meta = yt._fetch_meta_hard("vid00000001", yt.COMMENTS_MAX)
        self.assertEqual(self.calls, [yt.COMMENTS_MAX])
        self.assertEqual(meta["title"], "제목")
        self.assertEqual(meta["channel"], "채널")
        self.assertEqual([c["text"] for c in meta["comments"]], ["좋은 영상"])

    def test_comment_failure_falls_back_without_comments(self):
        self.stub(None, {"title": "제목", "description": "설명"})
        meta = yt._fetch_meta_hard("vid00000001", yt.COMMENTS_MAX)
        self.assertEqual(self.calls, [yt.COMMENTS_MAX, 0])
        self.assertEqual(meta["title"], "제목")
        self.assertEqual(meta["comments"], [])

    def test_metadata_only_never_asks_for_comments(self):
        self.stub({"title": "제목"})
        meta = yt._fetch_meta_hard("vid00000001")
        self.assertEqual(self.calls, [0])
        self.assertEqual(meta["comments"], [])

    def test_total_failure_returns_empty(self):
        self.stub(None, None)
        self.assertEqual(yt._fetch_meta_hard("vid00000001", yt.COMMENTS_MAX), {})

    def test_fetch_top_comments_survives_failure(self):
        self.stub(None)
        self.assertEqual(yt.fetch_top_comments("vid00000001"), [])


class CommentRequestBudget(unittest.TestCase):
    """댓글 요청에만 시간 예산이 붙는지 (R2)."""

    def opts_for(self, comments):
        seen = {}

        class FakeYDL:
            def __init__(self, opts):
                seen.update(opts)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, url, download=False):
                return {"title": "제목"}

        import yt_dlp
        real, yt_dlp.YoutubeDL = yt_dlp.YoutubeDL, FakeYDL
        try:
            yt._ydl_extract("vid00000001", comments)
        finally:
            yt_dlp.YoutubeDL = real
        return seen

    def test_comment_call_is_time_boxed(self):
        opts = self.opts_for(yt.COMMENTS_MAX)
        self.assertEqual(opts["socket_timeout"], yt.COMMENT_TIMEOUT)
        self.assertEqual(opts["extractor_retries"], 1)
        self.assertTrue(opts["getcomments"])

    def test_metadata_call_keeps_previous_behaviour(self):
        opts = self.opts_for(0)
        self.assertNotIn("socket_timeout", opts)
        self.assertNotIn("getcomments", opts)


class TitleRepairBudget(unittest.TestCase):
    """제목 복구가 댓글을 안 부르고, 실패 뒤엔 바로 재시도 안 하는지 (R1)."""

    def setUp(self):
        self.saved = {n: getattr(yt, n) for n in
                      ("_load_history", "_save_history", "_result_path",
                       "fetch_meta", "_fetch_meta_hard")}
        self.hard_calls, self.pauses = [], []
        yt._save_history = lambda entries: None
        yt._result_path = lambda key: ROOT / "no-such-result.json"
        yt.fetch_meta = lambda vid: {}          # oembed 실패 (차단 상황)
        self._sleep, yt.time.sleep = yt.time.sleep, self.pauses.append
        yt._title_fix_running = False
        yt._title_fix_until, yt._title_fix_cool = 0.0, yt.TITLE_FIX_COOL

    def tearDown(self):
        for name, fn in self.saved.items():
            setattr(yt, name, fn)
        yt.time.sleep = self._sleep
        yt._title_fix_running = False
        yt._title_fix_until, yt._title_fix_cool = 0.0, yt.TITLE_FIX_COOL

    def set_broken(self, n):
        entries = [{"key": f"vid{i:08d}:caption", "video_id": f"vid{i:08d}",
                    "title": f"vid{i:08d}"} for i in range(n)]
        yt._load_history = lambda: list(entries)

    def run_repair(self, hard_result={}):
        def fake_hard(video_id, comments=0):
            self.hard_calls.append(comments)
            return dict(hard_result)
        yt._fetch_meta_hard = fake_hard
        err, sys.stderr = sys.stderr, io.StringIO()
        try:
            yt._fix_missing_titles()
        finally:
            sys.stderr = err

    def test_repair_never_requests_comments(self):
        self.set_broken(3)
        self.run_repair()
        self.assertEqual(self.hard_calls, [0, 0, 0])

    def test_pass_is_capped_and_paced(self):
        self.set_broken(yt.TITLE_FIX_MAX + 15)
        self.run_repair()
        self.assertEqual(len(self.hard_calls), yt.TITLE_FIX_MAX)
        # 실패한 항목에도 간격을 둠 (예전엔 성공했을 때만 쉬었음)
        self.assertEqual(len(self.pauses), yt.TITLE_FIX_MAX)
        self.assertTrue(all(p >= 1.0 for p in self.pauses))

    def test_failed_pass_sets_backoff(self):
        self.set_broken(2)
        self.run_repair()
        self.assertGreater(yt._title_fix_until, yt.time.time())
        self.assertEqual(yt._title_fix_cool, yt.TITLE_FIX_COOL * 2)

    def test_clean_pass_clears_backoff(self):
        self.set_broken(2)
        yt._title_fix_cool = yt.TITLE_FIX_COOL * 4
        self.run_repair({"title": "채워진 제목"})
        self.assertEqual(yt._title_fix_until, 0.0)
        self.assertEqual(yt._title_fix_cool, yt.TITLE_FIX_COOL)

    def test_history_poll_during_backoff_starts_nothing(self):
        self.set_broken(2)
        started = []

        class FakeThread:
            def __init__(self, **kw):
                started.append(kw.get("target"))

            def start(self):
                pass

        real_thread, yt.threading.Thread = yt.threading.Thread, FakeThread
        real_hosts, yt.ALLOWED_HOSTS = yt.ALLOWED_HOSTS, set()
        try:
            client = yt.app.test_client()
            self.assertEqual(client.get("/api/history").status_code, 200)
            self.assertEqual(len(started), 1)   # 첫 조회는 복구를 띄움
            yt._title_fix_running = False        # 복구 스레드가 끝난 척
            yt._title_fix_cooldown(failed=True)  # 실패로 끝났다고 치고
            for _ in range(3):                   # 8초마다 다시 불러도
                client.get("/api/history")
            self.assertEqual(len(started), 1)    # 재시도 안 함
        finally:
            yt.threading.Thread = real_thread
            yt.ALLOWED_HOSTS = real_hosts


if __name__ == "__main__":
    unittest.main()
