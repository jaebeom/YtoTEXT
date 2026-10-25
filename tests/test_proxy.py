"""사내망 신뢰 프록시 모드 · 경로 접두어 · 보안 고침 · 기록 옮기기 테스트.

실행: python3 -m pytest tests   (또는 python3 -m unittest discover -s tests)
네트워크·Whisper·yt-dlp 는 안 씀 — 받아쓰기 작업과 yt-dlp 는 가짜로 갈아끼움.
"""

import base64
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import time
import types
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

SECRET = "s" * 40                 # 가짜 비밀값 (테스트 전용)
VID, VID2 = "dQw4w9WgXcQ", "aaaaaaaaaaa"
ENV_KEYS = ("INTRANET_PROXY_SECRET_FILE", "YT2TEXT_BASE_PATH", "YT2TEXT_SAVE_PATH",
            "YT2TEXT_OWNER_PW", "YT2TEXT_TEAM_PW", "YT2TEXT_ALLOW_LAN", "YT2TEXT_KIT_PATH")


def emp(i, secret=SECRET):
    return {"X-Intranet-Proxy-Secret": secret, "X-Intranet-Employee-Id": str(i)}


class Base(unittest.TestCase):
    """데이터 경로를 임시 폴더로, 환경변수를 비우고, 기본은 프록시 모드 켬."""

    proxy = True

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="yt2text_proxy_"))
        self.data = self.tmp / "yt2text_data"
        self.saved = dict(
            HISTORY_FILE=yt.HISTORY_FILE, FOLDERS_FILE=yt.FOLDERS_FILE,
            SECRET_FILE=yt.SECRET_FILE, RESULTS_DIR=yt.RESULTS_DIR,
            THUMBS_DIR=yt.THUMBS_DIR, ALLOWED_HOSTS=yt.ALLOWED_HOSTS,
            HISTORY_MAX=yt.HISTORY_MAX, save_thumb=yt.save_thumb,
            stt_worker=yt.stt_worker)
        yt.HISTORY_FILE = self.data / "history.json"
        yt.FOLDERS_FILE = self.data / "folders.json"
        yt.SECRET_FILE = self.data / "secret.key"
        yt.RESULTS_DIR = self.data / "results"
        yt.THUMBS_DIR = self.data / "thumbs"
        yt.RESULTS_DIR.mkdir(parents=True)
        yt.ALLOWED_HOSTS = set()
        yt.save_thumb = lambda vid: None          # 썸네일 내려받기 안 함
        self.worker_calls = []
        yt.stt_worker = lambda *a: self.worker_calls.append(a)  # 받아쓰기 안 함
        yt.JOBS.clear()
        self.saved_env = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        self.secret_file = self.tmp / "proxy-secret"
        self.secret_file.write_text(SECRET + "\n", encoding="utf-8")
        if self.proxy:
            os.environ["INTRANET_PROXY_SECRET_FILE"] = str(self.secret_file)
        self.c = yt.app.test_client()

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(yt, k, v)
        for k, v in self.saved_env.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        yt.JOBS.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def result(self, vid=VID, source="caption", title="제목"):
        return {"video_id": vid, "source": source, "title": title,
                "language": "Korean", "duration": "10:00",
                "paragraphs": ["본문"], "lines": []}

    def assert_no_cookie(self, r):
        self.assertNotIn("Set-Cookie", r.headers)


class ProxyIdentity(Base):
    def test_no_or_wrong_secret_means_no_identity(self):
        for headers in ({}, {"X-Intranet-Employee-Id": "7"},
                        emp(7, secret="틀린값" * 10), emp(7, secret=SECRET[:-1]),
                        emp(7, secret=SECRET + "x")):
            with self.subTest(headers=headers):
                self.assertEqual(self.c.get("/api/me", headers=headers).status_code, 401)
                self.assertEqual(self.c.get("/api/history", headers=headers).status_code, 401)
                r = self.c.get("/", headers=headers)
                self.assertEqual(r.status_code, 401)
                self.assertNotIn("Location", r.headers)   # /login 으로 안 보냄

    def test_employee_id_must_be_positive_integer(self):
        for bad in ("", "0", "-1", "abc", "1;2", "emp:1", "01", "1 ", "12345678901"):
            with self.subTest(bad=bad):
                self.assertEqual(self.c.get("/api/me", headers=emp(bad)).status_code, 401)

    def test_right_secret_gives_employee_user(self):
        r = self.c.get("/api/me", headers=emp(7))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {"user": "emp:7", "auth": True, "proxy": True,
                                        "save_path": "/ytot-save"})
        self.assert_no_cookie(r)

    def test_secret_compare_is_constant_time(self):
        calls = []
        real = yt.hmac.compare_digest
        yt.hmac.compare_digest = lambda a, b: calls.append((a, b)) or real(a, b)
        try:
            self.c.get("/api/me", headers=emp(7))
        finally:
            yt.hmac.compare_digest = real
        self.assertEqual(calls, [(SECRET.encode(), SECRET.encode())])

    def test_short_or_missing_secret_file_locks_everyone_out(self):
        self.secret_file.write_text("짧음", encoding="utf-8")
        self.assertEqual(self.c.get("/api/me", headers=emp(7, secret="짧음")).status_code, 401)
        self.assertTrue(yt.check_config())
        self.secret_file.unlink()
        self.assertEqual(self.c.get("/api/me", headers=emp(7)).status_code, 401)
        self.assertTrue(yt.check_config())
        with self.assertRaises(SystemExit):
            yt.wsgi_app()

    def test_password_login_and_session_cookie_are_off(self):
        os.environ["YT2TEXT_OWNER_PW"] = "1111"   # 설정돼 있어도 안 씀
        for path in ("/login", "/api/login", "/api/logout"):
            with self.subTest(path=path):
                r = (self.c.get(path) if path == "/login"
                     else self.c.post(path, json={"password": "1111"}))
                self.assertEqual(r.status_code, 404)
                self.assert_no_cookie(r)
        r = self.c.post("/api/login", json={"password": "1111"}, headers=emp(7))
        self.assertEqual(r.status_code, 404)
        for r in (self.c.get("/", headers=emp(7)),
                  self.c.get("/api/history", headers=emp(7)),
                  self.c.post("/api/folders", json={"name": "a"}, headers=emp(7))):
            self.assert_no_cookie(r)

    def test_team_expiry_is_off(self):
        old = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() - 60 * 86400))
        yt._save_history([{"key": f"{VID}:caption", "video_id": VID, "user": "emp:1",
                           "saved_at": old}])
        self.assertEqual(yt.prune_expired(), 0)
        self.assertEqual(len(yt._load_history()), 1)


class ModeOff(Base):
    """모드를 끄면 예전 동작 그대로 — 사내망 헤더는 아무 뜻이 없음."""

    proxy = False

    def test_headers_ignored_without_passwords(self):
        r = self.c.get("/api/me", headers=emp(7))
        self.assertEqual(r.get_json(), {"user": "owner", "auth": False, "proxy": False})

    def test_headers_do_not_log_in_with_passwords(self):
        os.environ["YT2TEXT_OWNER_PW"] = "1111"
        r = self.c.get("/api/me", headers=emp(7))
        self.assertEqual(r.status_code, 401)
        r = self.c.get("/", headers=emp(7))
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/login"))


class MembersIsolated(Base):
    """구성원 둘이 서로의 기록·전문·폴더·작업 상태를 열거나 지우거나 취소하지 못함."""

    def setUp(self):
        super().setUp()
        self.A, self.B = emp(1), emp(2)
        yt.history_add(self.result(), "emp:1")
        self.key = f"{VID}:caption"

    def test_history_and_transcript(self):
        self.assertEqual(len(self.c.get("/api/history", headers=self.A).get_json()), 1)
        self.assertEqual(self.c.get("/api/history", headers=self.B).get_json(), [])
        path = "/api/history/" + self.key
        self.assertEqual(self.c.get(path, headers=self.A).status_code, 200)
        self.assertEqual(self.c.get(path, headers=self.B).status_code, 404)
        self.assertEqual(self.c.patch(path, json={"folder": None},
                                      headers=self.B).status_code, 404)
        self.assertEqual(self.c.delete(path, headers=self.B).status_code, 404)
        self.assertEqual(self.c.get(path, headers=self.A).status_code, 200)

    def test_same_video_kept_apart(self):
        yt.history_add(self.result(title="B 의 것"), "emp:2")
        a = self.c.get("/api/history/" + self.key, headers=self.A).get_json()
        b = self.c.get("/api/history/" + self.key, headers=self.B).get_json()
        self.assertEqual((a["title"], b["title"]), ("제목", "B 의 것"))
        self.c.delete("/api/history/" + self.key, headers=self.B)
        self.assertEqual(self.c.get("/api/history/" + self.key, headers=self.A).status_code, 200)

    def test_folders(self):
        f = self.c.post("/api/folders", json={"name": "A 폴더"}, headers=self.A).get_json()
        self.c.patch("/api/history/" + self.key, json={"folder": f["id"]}, headers=self.A)
        self.assertEqual(self.c.get("/api/folders", headers=self.B).get_json(), [])
        url = "/api/folders/" + f["id"]
        self.assertEqual(self.c.patch(url, json={"name": "뺏기"}, headers=self.B).status_code, 404)
        self.assertEqual(self.c.delete(url, headers=self.B).status_code, 404)
        # B 는 A 의 폴더로 자기 항목을 옮길 수도 없음
        yt.history_add(self.result(VID2), "emp:2")
        r = self.c.patch(f"/api/history/{VID2}:caption", json={"folder": f["id"]},
                         headers=self.B)
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.c.get("/api/folders", headers=self.A).get_json()[0]["name"], "A 폴더")
        self.assertEqual(self.c.get("/api/history", headers=self.A).get_json()[0]["folder"], f["id"])

    def test_folder_delete_only_unfiles_own_entries(self):
        # 폴더 id 가 우연히 겹쳐도 남의 항목은 안 건드리는지 (방어용)
        yt._save_folders([{"id": "same", "name": "A", "user": "emp:1"},
                          {"id": "same", "name": "B", "user": "emp:2"}])
        e = yt._load_history()
        e[0]["folder"] = "same"
        e.append({"key": f"{VID2}:caption", "video_id": VID2, "user": "emp:2",
                  "folder": "same"})
        yt._save_history(e)
        self.assertEqual(self.c.delete("/api/folders/same", headers=self.A).status_code, 200)
        left = {x["user"]: x.get("folder") for x in yt._load_history()}
        self.assertEqual(left, {"emp:1": None, "emp:2": "same"})
        self.assertEqual([f["user"] for f in yt._load_folders()], ["emp:2"])

    def test_jobs(self):
        r = self.c.post("/api/stt", json={"url": VID}, headers=self.A)
        self.assertEqual(r.status_code, 200)
        jid = r.get_json()["job_id"]
        self.assertEqual(self.c.get("/api/stt/status?id=" + jid, headers=self.B).status_code, 404)
        self.assertEqual(self.c.post("/api/stt/cancel", json={"id": jid},
                                     headers=self.B).status_code, 404)
        self.assertNotIn("cancel", yt.JOBS[jid])
        st = self.c.get("/api/stt/status?id=" + jid, headers=self.A)
        self.assertEqual(st.status_code, 200)
        self.assertNotIn("user", st.get_json())
        self.assertEqual(self.c.post("/api/stt/cancel", json={"id": jid},
                                     headers=self.A).status_code, 200)
        self.assertTrue(yt.JOBS[jid]["cancel"])

    def test_title_repair_touches_only_that_users_entry(self):
        yt.history_add(self.result(title=VID), "emp:2")       # B 의 것만 제목이 깨짐
        saved = yt.fetch_meta
        yt.fetch_meta = lambda vid: {"title": "고친 제목"}
        real_sleep = yt.time.sleep
        yt.time.sleep = lambda s: None
        try:
            yt._fix_missing_titles()
        finally:
            yt.fetch_meta, yt.time.sleep = saved, real_sleep
        a = self.c.get("/api/history/" + self.key, headers=self.A).get_json()
        b = self.c.get("/api/history/" + self.key, headers=self.B).get_json()
        self.assertEqual((a["title"], b["title"]), ("제목", "고친 제목"))


class ThumbsAndBootstrap(Base):
    def test_thumbnail_only_for_my_own_videos(self):
        yt.THUMBS_DIR.mkdir(parents=True, exist_ok=True)
        (yt.THUMBS_DIR / f"{VID}.jpg").write_bytes(b"jpg")
        yt.history_add(self.result(), "emp:1")
        self.assertEqual(self.c.get(f"/thumbs/{VID}.jpg", headers=emp(1)).status_code, 200)
        self.assertEqual(self.c.get(f"/thumbs/{VID}.jpg", headers=emp(2)).status_code, 404)

    def test_page_knows_proxy_mode_from_the_start(self):
        html = self.c.get("/", headers=emp(1)).get_data(as_text=True)
        self.assertIn('<meta name="ytot-proxy-save" content="/ytot-save">', html)
        os.environ.pop("INTRANET_PROXY_SECRET_FILE")
        html = self.c.get("/").get_data(as_text=True)
        self.assertIn('<meta name="ytot-proxy-save" content="">', html)



class IntranetMode(Base):
    """사내망 모드 화면 (사내망 결정 26, T-52) — 신뢰 프록시 헤더가 맞을 때만"""

    def page(self, path="/", **kw):
        return self.c.get(path, **kw).get_data(as_text=True)

    def test_on_only_with_trusted_proxy_headers(self):
        html = self.page(headers=emp(1))
        self.assertIn('<html lang="ko" data-intranet="1">', html)
        self.assertIn('<link rel="stylesheet" href="/apps/_kit/tool.css">', html)
        self.assertIn('<link rel="stylesheet" href="/intranet.css">', html)
        self.assertIn('placeholder="유튜브 주소"', html)
        # 키트·덧씌우기는 인라인 <style> 뒤 — 같은 이름 규칙을 이김
        self.assertLess(html.index("</style>"), html.index("/intranet.css"))
        # 직접 실행(프록시 모드 꺼짐)은 지금 화면 그대로
        os.environ.pop("INTRANET_PROXY_SECRET_FILE")
        html = self.page()
        self.assertIn("<html lang=\"ko\">", html)  # 스크립트에 이름은 있어도 속성은 없음
        self.assertNotIn("intranet.css", html)
        self.assertIn('placeholder="https://www.youtube.com/watch?v=..."', html)

    def test_theme_from_frame_query_only_light_or_dark(self):
        self.assertIn('data-intranet="1" data-theme="dark">', self.page("/?theme=dark", headers=emp(1)))
        self.assertIn('data-intranet="1" data-theme="light">', self.page("/?theme=light", headers=emp(1)))
        for bad in ("blue", '"><script>', ""):
            with self.subTest(theme=bad):
                html = self.page("/?theme=" + bad, headers=emp(1))
                self.assertIn('<html lang="ko" data-intranet="1">', html)
                self.assertNotIn("<script>alert", html)

    def test_theme_message_contract(self):
        """틀 화면의 postMessage {type:"intranet-theme"} 를 받는 쪽 확인 (사내망 docs/guides/tools.md)"""
        js = re.search(r"<script>(.*?)</script>", self.page(headers=emp(1)), re.S).group(1)
        i = js.index("'intranet-theme'")
        part = js[js.rindex("addEventListener('message'", 0, i):js.index("});", i)]
        for need in ("hasAttribute('data-intranet')", "ev.origin !== location.origin",
                     "ev.source !== window.parent", "['light','dark','system'].includes(theme)",
                     "delete document.documentElement.dataset.theme"):
            with self.subTest(need=need):
                self.assertIn(need, js if need.startswith("has") else part)
        # 「시스템」(속성 없음)은 키트처럼 시스템 설정을 따름
        css = self.c.get("/intranet.css", headers=emp(1)).get_data(as_text=True)
        self.assertIn('@media (prefers-color-scheme: dark){\n  :root[data-intranet]:not([data-theme="light"]){', css)

    def test_paths_follow_prefix_and_kit_setting(self):
        os.environ["YT2TEXT_BASE_PATH"] = "/apps/ytot"
        os.environ["YT2TEXT_KIT_PATH"] = "/apps/_kit/v2.css"
        html = self.page("/apps/ytot/", headers=emp(1))
        self.assertIn('href="/apps/_kit/v2.css"', html)
        self.assertIn('href="/apps/ytot/intranet.css"', html)
        os.environ["YT2TEXT_KIT_PATH"] = "//evil.example.test/x.css"   # 형식이 틀리면 기본 경로
        self.assertIn('href="/apps/_kit/tool.css"', self.page("/apps/ytot/", headers=emp(1)))

    def test_intranet_css_is_served_and_hides_title_bar(self):
        r = self.c.get("/intranet.css", headers=emp(1))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.mimetype == "text/css")
        css = r.get_data(as_text=True)
        self.assertIn(":root[data-intranet] .brand{display:none}", css)
        self.assertIn(':root[data-intranet][data-theme="dark"]', css)
        self.assertIn("@media (hover:none)", css)    # 터치 기기엔 단축키 안내 숨김
        self.assertIn("@media (pointer:coarse)", css)  # 카드 단추는 손가락 크기

    def test_kbd_hint_is_a_separate_span(self):
        self.assertIn('<span class="kbd"> · ⌘/Ctrl+Enter로 추출</span>', yt.PAGE)


class SsrfAndModels(Base):
    def test_only_canonical_watch_url_reaches_worker(self):
        for raw in (f"http://10.0.0.5/?x=youtu.be/{VID}",
                    f"http://127.0.0.1:3000/payroll?v=youtube.com/watch?v={VID}",
                    f"file:///etc/passwd#youtu.be/{VID}",
                    f"https://youtu.be/{VID}", VID):
            with self.subTest(raw=raw):
                self.worker_calls.clear()
                r = self.c.post("/api/stt", json={"url": raw}, headers=emp(1))
                self.assertEqual(r.status_code, 200)
                self.assertEqual(self.worker_calls[0][1],
                                 f"https://www.youtube.com/watch?v={VID}")

    def test_worker_hands_that_url_to_yt_dlp(self):
        seen = []

        class FakeYDL:
            def __init__(self, opts):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, url, download):
                seen.append(url)
                raise RuntimeError("가짜 — 여기서 멈춤")

        saved = sys.modules.get("yt_dlp")
        sys.modules["yt_dlp"] = types.SimpleNamespace(YoutubeDL=FakeYDL)
        try:
            yt.JOBS["j"] = {"status": "running", "user": "emp:1"}
            self.saved["stt_worker"]("j", f"https://www.youtube.com/watch?v={VID}",
                                     "small", None, "emp:1")
        finally:
            if saved is None:
                sys.modules.pop("yt_dlp", None)
            else:
                sys.modules["yt_dlp"] = saved
        self.assertEqual(seen, [f"https://www.youtube.com/watch?v={VID}"])
        self.assertEqual(yt.JOBS["j"]["status"], "error")

    def test_model_allow_list(self):
        for bad in ("../../etc/passwd", "/tmp/model", "Systran/faster-whisper-large-v3",
                    "tiny", "large-v3-turbo "):
            with self.subTest(bad=bad):
                r = self.c.post("/api/stt", json={"url": VID, "model": bad}, headers=emp(1))
                self.assertEqual(r.status_code, 400)
        self.assertEqual(self.worker_calls, [])
        for good in yt.WHISPER_MODELS:
            r = self.c.post("/api/stt", json={"url": VID, "model": good}, headers=emp(1))
            self.assertEqual(r.status_code, 200)
        # 화면의 선택지와 허용 목록이 같아야 함
        options = re.findall(r'<option value="([^"]+)"', yt.PAGE.split('id="model"')[1]
                             .split("</select>")[0])
        self.assertEqual(tuple(options), yt.WHISPER_MODELS)


class PerUserCap(Base):
    def test_cap_counts_each_user_separately(self):
        yt.HISTORY_MAX = 3
        yt.history_add(self.result("bbbbbbbbbbb"), "emp:2")
        for i in range(5):
            yt.history_add(self.result(f"a{i}aaaaaaaaa"), "emp:1")
        mine = [e["video_id"] for e in yt._load_history() if e.get("user") == "emp:1"]
        self.assertEqual(mine, ["a4aaaaaaaaa", "a3aaaaaaaaa", "a2aaaaaaaaa"])
        self.assertEqual(len(self.c.get("/api/history", headers=emp(2)).get_json()), 1)
        self.assertFalse(yt._result_path("a0aaaaaaaaa:caption", "emp:1").exists())
        self.assertTrue(yt._result_path("bbbbbbbbbbb:caption", "emp:2").exists())


class BasePath(Base):
    def setUp(self):
        super().setUp()
        os.environ["YT2TEXT_BASE_PATH"] = "/apps/ytot"

    def test_only_prefixed_paths_reach_app(self):
        self.assertEqual(self.c.get("/apps/ytot/api/me", headers=emp(1)).status_code, 200)
        for path in ("/", "/api/me", "/apps/ytotx/api/me", "/apps/api/me"):
            with self.subTest(path=path):
                self.assertEqual(self.c.get(path, headers=emp(1)).status_code, 404)
        r = self.c.get("/apps/ytot", headers=emp(1))
        self.assertEqual((r.status_code, r.headers["Location"]), (308, "/apps/ytot/"))

    def test_page_and_js_use_prefix(self):
        html = self.c.get("/apps/ytot/", headers=emp(1)).get_data(as_text=True)
        self.assertIn('<meta name="ytot-base" content="/apps/ytot">', html)
        script = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
        # 스크립트의 서버 경로는 전부 접두어(B)를 붙여서 부름
        self.assertEqual(re.findall(r"""fetch\(\s*['"`]/""", script), [])
        self.assertEqual(re.findall(r"""(?<![\w+ ])['"`]/(?:api|thumbs|login)""", script), [])
        self.assertGreaterEqual(script.count("B + '/api/"), 10)

    def test_password_mode_redirect_keeps_prefix(self):
        os.environ.pop("INTRANET_PROXY_SECRET_FILE")
        os.environ["YT2TEXT_OWNER_PW"] = "1111"
        r = self.c.get("/apps/ytot/")
        self.assertEqual((r.status_code, r.headers["Location"]), (302, "/apps/ytot/login"))
        self.assertIn('content="/apps/ytot"', self.c.get("/apps/ytot/login").get_data(as_text=True))

    def test_bad_prefix_stops_start(self):
        for bad in ("apps", "/apps/../x/", "/a b", "/apps/<x>"):
            with self.subTest(bad=bad):
                os.environ["YT2TEXT_BASE_PATH"] = bad
                self.assertTrue(yt.check_config())
                with self.assertRaises(SystemExit):
                    yt.wsgi_app()


class CspReady(Base):
    def test_no_inline_handlers_or_style_attributes(self):
        for name in ("PAGE", "LOGIN_PAGE"):
            html = getattr(yt, name)
            with self.subTest(page=name):
                self.assertEqual(re.findall(r"<[^>]*\son[a-z]+\s*=", html), [])
                self.assertEqual(re.findall(r"<[^>]*\sstyle\s*=", html), [])
                self.assertNotIn("javascript:", html)

    def test_served_inline_hashes_match_and_do_not_depend_on_settings(self):
        want = yt.inline_hashes()
        for base in ("", "/apps/ytot", "/x"):
            os.environ["YT2TEXT_BASE_PATH"] = base
            html = self.c.get((base or "") + "/", headers=emp(1)).get_data(as_text=True)
            for tag in ("script", "style"):
                got = ["'sha256-" + base64.b64encode(hashlib.sha256(b.encode()).digest()).decode() + "'"
                       for b in re.findall(rf"<{tag}>(.*?)</{tag}>", html, re.S)]
                self.assertEqual(got, want["PAGE"][tag], (base, tag))

    def test_save_link_only_in_proxy_mode(self):
        self.assertIn('id="savebtn" target="_top" hidden', yt.PAGE)  # 사내망 화면으로 가는 링크는 틀 밖(최상위)으로
        self.assertIn("save.hidden = !PROXY", yt.PAGE)
        os.environ["YT2TEXT_SAVE_PATH"] = "/docs/ytot-save"
        self.assertEqual(self.c.get("/api/me", headers=emp(3)).get_json()["save_path"],
                         "/docs/ytot-save")


class Migrate(Base):
    """주인 기록을 emp:<대표 id> 로 옮기고 팀 기록을 지우는 일회성 명령."""

    proxy = False

    def seed(self):
        k1, k2, k3 = f"{VID}:caption", f"{VID2}:whisper:small", "ccccccccccc:caption"
        yt._save_history([
            {"key": k1, "video_id": VID, "title": "주인 1", "folder": "f1"},
            {"key": k2, "video_id": VID2, "title": "주인 2"},
            {"key": k3, "video_id": "ccccccccccc", "title": "파일 없음"},
            {"key": k1, "video_id": VID, "title": "팀", "user": "team", "folder": "t1"},
            {"key": k2, "video_id": VID2, "title": "다른 직원", "user": "emp:5"},
        ])
        yt._save_folders([{"id": "f1", "name": "주인 폴더"},
                          {"id": "t1", "name": "팀 폴더", "user": "team"},
                          {"id": "e5", "name": "직원5", "user": "emp:5"}])
        for key, user, body in ((k1, "owner", "주인 전문 1"), (k2, "owner", "주인 전문 2"),
                                (k1, "team", "팀 전문"), (k2, "emp:5", "직원5 전문")):
            yt._result_path(key, user).write_text(json.dumps({"title": body}), encoding="utf-8")
        return k1, k2

    def test_dry_run_changes_nothing(self):
        self.seed()
        before = sorted(p.name for p in yt.RESULTS_DIR.iterdir())
        hist = yt.HISTORY_FILE.read_text(encoding="utf-8")
        counts = yt.migrate_to_employee("1")
        self.assertEqual({k: v for k, v in counts.items() if k != "backup"},
                         dict(history=3, folders=1, files=2, files_missing=1, collisions=0,
                              team_history=1, team_folders=1, team_files=1))
        self.assertIsNone(counts["backup"])
        self.assertEqual(sorted(p.name for p in yt.RESULTS_DIR.iterdir()), before)
        self.assertEqual(yt.HISTORY_FILE.read_text(encoding="utf-8"), hist)
        self.assertEqual([p.name for p in self.tmp.iterdir() if "before-migrate" in p.name], [])

    def test_apply_moves_owner_and_drops_team(self):
        k1, k2 = self.seed()
        counts = yt.migrate_to_employee("1", apply=True, backup_dir=self.tmp / "backups")
        self.assertEqual((counts["history"], counts["folders"], counts["files"],
                          counts["team_history"], counts["team_folders"], counts["team_files"]),
                         (3, 1, 2, 1, 1, 1))
        backup = Path(counts["backup"])
        self.assertTrue((backup / "results" / f"{VID}__caption.json").exists())  # 원본 그대로
        self.assertEqual(sorted({e.get("user") for e in yt._load_history()}), ["emp:1", "emp:5"])
        self.assertEqual(len(yt._load_history()), 4)
        self.assertEqual({f["id"]: f["user"] for f in yt._load_folders()},
                         {"f1": "emp:1", "e5": "emp:5"})
        self.assertEqual(sorted(p.name for p in yt.RESULTS_DIR.iterdir()),
                         sorted([f"emp__1__{VID}__caption.json", f"emp__1__{VID2}__whisper__small.json",
                                 f"emp__5__{VID2}__whisper__small.json"]))
        # 대표가 사내망을 거쳐 열면 전문·폴더가 그대로 보임
        os.environ["INTRANET_PROXY_SECRET_FILE"] = str(self.secret_file)
        r = self.c.get("/api/history/" + k1, headers=emp(1))
        self.assertEqual((r.status_code, r.get_json()["title"]), (200, "주인 전문 1"))
        self.assertEqual(self.c.get("/api/history/" + k2, headers=emp(1)).get_json()["title"],
                         "주인 전문 2")
        self.assertEqual([f["name"] for f in self.c.get("/api/folders", headers=emp(1)).get_json()],
                         ["주인 폴더"])
        hist = self.c.get("/api/history", headers=emp(1)).get_json()
        self.assertEqual([(e["title"], e.get("folder")) for e in hist],
                         [("주인 1", "f1"), ("주인 2", None), ("파일 없음", None)])
        self.assertEqual(self.c.get("/api/history/" + k2, headers=emp(5)).get_json()["title"],
                         "직원5 전문")
        # 다시 돌려도 더 옮길 것이 없음
        again = yt.migrate_to_employee("1", apply=True, backup_dir=self.tmp / "backups")
        self.assertEqual((again["history"], again["files"], again["team_history"]), (0, 0, 0))

    def test_apply_needs_backup_dir_outside_data(self):
        self.seed()
        with self.assertRaises(ValueError):
            yt.migrate_to_employee("1", apply=True)
        with self.assertRaises(ValueError):
            yt.migrate_to_employee("1", apply=True, backup_dir=self.data / "inside")
        self.assertIsNone(yt._load_history()[0].get("user"))  # 아무것도 안 바뀜

    def test_orphan_team_files_go_but_owner_file_named_team_stays(self):
        self.seed()
        orphan = yt.RESULTS_DIR / "team__orphanvid01__caption.json"
        orphan.write_text("{}", encoding="utf-8")
        # 영상 id 가 team__ 로 시작하는 주인 기록 (11자 id 로 가능)
        e = yt._load_history()
        e.append({"key": "team__abcde:caption", "video_id": "team__abcde", "title": "주인 3"})
        yt._save_history(e)
        owner_file = yt._result_path("team__abcde:caption")
        owner_file.write_text(json.dumps({"title": "주인 전문 3"}), encoding="utf-8")
        counts = yt.migrate_to_employee("1")
        self.assertEqual(counts["team_files"], 2)  # 기록 있는 팀 파일 1 + 기록 없는 것 1
        yt.migrate_to_employee("1", apply=True, backup_dir=self.tmp / "backups")
        self.assertFalse(orphan.exists())
        self.assertTrue(yt._result_path("team__abcde:caption", "emp:1").exists())  # 주인 파일은 옮겨짐

    def test_collision_is_reported_and_stops_before_any_change(self):
        k1, _ = self.seed()
        yt._result_path(k1, "emp:1").write_text("{}", encoding="utf-8")
        before = sorted(p.name for p in yt.RESULTS_DIR.iterdir())
        self.assertEqual(yt.migrate_to_employee("1")["collisions"], 1)
        with self.assertRaises(SystemExit):
            yt.migrate_to_employee("1", apply=True, backup_dir=self.tmp / "backups")
        self.assertEqual(sorted(p.name for p in yt.RESULTS_DIR.iterdir()), before)
        self.assertFalse((self.tmp / "backups").exists())
        self.assertIsNone(yt._load_history()[0].get("user"))

    def test_rejects_bad_employee_id(self):
        for bad in ("0", "abc", "1;rm", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                yt.migrate_to_employee(bad)


if __name__ == "__main__":
    unittest.main()
