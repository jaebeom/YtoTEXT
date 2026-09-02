"""yt2text 로그인 · 계정 분리 · 30일 만료 테스트.

실행: python3 -m unittest discover -s tests   (프로젝트 루트에서)
네트워크는 안 씀 — 데이터 경로와 환경변수만 갈아끼움.
"""

import importlib.util
import io
import os
import shutil
import sys
import tempfile
import time
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

OWNER_PW, TEAM_PW = "1111", "2222"


class Base(unittest.TestCase):
    """비밀번호를 환경변수로 넣고, 데이터 경로를 임시 폴더로 돌림."""

    auth = True

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="yt2text_auth_"))
        self.saved_paths = (yt.HISTORY_FILE, yt.FOLDERS_FILE, yt.SECRET_FILE,
                            yt.RESULTS_DIR, yt.THUMBS_DIR, yt.ALLOWED_HOSTS)
        yt.HISTORY_FILE = self.tmp / "history.json"
        yt.FOLDERS_FILE = self.tmp / "folders.json"
        yt.SECRET_FILE = self.tmp / "secret.key"
        yt.RESULTS_DIR = self.tmp / "results"
        yt.THUMBS_DIR = self.tmp / "thumbs"
        yt.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        yt.ALLOWED_HOSTS = set()
        self.saved_env = {k: os.environ.get(k) for k in
                          ("YT2TEXT_OWNER_PW", "YT2TEXT_TEAM_PW",
                           "YT2TEXT_ALLOW_LAN")}
        for k in self.saved_env:
            os.environ.pop(k, None)
        if self.auth:
            os.environ["YT2TEXT_OWNER_PW"] = OWNER_PW
            os.environ["YT2TEXT_TEAM_PW"] = TEAM_PW
        self.saved_secret = yt.app.secret_key
        yt.app.secret_key = yt._secret_key()   # 임시 경로에 키 생성
        yt._login_tries.clear()

    def tearDown(self):
        (yt.HISTORY_FILE, yt.FOLDERS_FILE, yt.SECRET_FILE,
         yt.RESULTS_DIR, yt.THUMBS_DIR, yt.ALLOWED_HOSTS) = self.saved_paths
        for k, v in self.saved_env.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        yt.app.secret_key = self.saved_secret
        shutil.rmtree(self.tmp, ignore_errors=True)

    def client(self, pw=None):
        c = yt.app.test_client()
        if pw is not None:
            r = c.post("/api/login", json={"password": pw})
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c

    def result(self, vid, title="제목", source="caption"):
        return {"video_id": vid, "source": source, "title": title,
                "language": "Korean", "duration": "10:00",
                "paragraphs": ["본문"], "lines": []}


class LoginGate(Base):
    def test_anonymous_api_gets_401(self):
        c = yt.app.test_client()
        r = c.get("/api/history")
        self.assertEqual(r.status_code, 401)
        self.assertTrue(r.get_json()["login"])

    def test_anonymous_page_redirects(self):
        r = yt.app.test_client().get("/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])

    def test_login_page_is_reachable(self):
        self.assertEqual(yt.app.test_client().get("/login").status_code, 200)

    def test_wrong_password(self):
        r = yt.app.test_client().post("/api/login", json={"password": "9999"})
        self.assertEqual(r.status_code, 401)

    def test_owner_and_team_passwords(self):
        self.assertEqual(self.client(OWNER_PW).get("/api/me").get_json()["user"],
                         "owner")
        self.assertEqual(self.client(TEAM_PW).get("/api/me").get_json()["user"],
                         "team")

    def test_logout(self):
        c = self.client(OWNER_PW)
        self.assertEqual(c.post("/api/logout").status_code, 200)
        self.assertEqual(c.get("/api/history").status_code, 401)

    def test_session_survives_restart(self):
        """비밀 키를 파일에 두므로 프로세스가 바뀌어도 쿠키가 살아있어야 함."""
        first = yt.app.secret_key
        c = self.client(OWNER_PW)
        again = yt._secret_key()                  # 재기동해서 다시 읽은 셈
        self.assertEqual(again, first)            # 파일에서 같은 키가 나와야 함
        yt.app.secret_key = again
        self.assertEqual(c.get("/api/me").get_json()["user"], "owner")

    def test_new_key_when_file_missing(self):
        """키 파일이 없으면 새로 만들고, 기존 세션은 무효가 됨."""
        c = self.client(OWNER_PW)
        yt.SECRET_FILE.unlink()
        yt.app.secret_key = yt._secret_key()
        self.assertEqual(c.get("/api/me").status_code, 401)

    def test_brute_force_is_rate_limited(self):
        c = yt.app.test_client()
        codes = [c.post("/api/login", json={"password": "0"}).status_code
                 for _ in range(yt.LOGIN_MAX_TRIES + 3)]
        self.assertIn(429, codes)
        self.assertEqual(codes[-1], 429)

    def test_non_json_login_rejected(self):
        r = yt.app.test_client().post("/api/login", data="password=1111",
                                      content_type="text/plain")
        self.assertEqual(r.status_code, 400)


class NoAuthMode(Base):
    """비밀번호를 안 넣으면 예전처럼 로그인 없이 동작."""
    auth = False

    def test_open_when_no_passwords(self):
        c = yt.app.test_client()
        self.assertEqual(c.get("/api/history").status_code, 200)
        self.assertFalse(c.get("/api/me").get_json()["auth"])
        self.assertEqual(c.get("/api/me").get_json()["user"], "owner")

    def test_tailnet_gate_off(self):
        self.assertFalse(yt.tailnet_only())


class Separation(Base):
    def test_each_sees_only_own_history(self):
        yt.history_add(self.result("vid_owner_1", "주인 영상"), "owner")
        yt.history_add(self.result("vid_team_11", "팀 영상"), "team")

        owner = self.client(OWNER_PW).get("/api/history").get_json()
        team = self.client(TEAM_PW).get("/api/history").get_json()
        self.assertEqual([e["title"] for e in owner], ["주인 영상"])
        self.assertEqual([e["title"] for e in team], ["팀 영상"])

    def test_same_video_does_not_collide(self):
        """둘이 같은 영상을 뽑아도 서로 덮어쓰지 않아야 함."""
        yt.history_add(self.result("samevideo11", "주인이 뽑음"), "owner")
        yt.history_add(self.result("samevideo11", "팀이 뽑음"), "team")

        oc, tc = self.client(OWNER_PW), self.client(TEAM_PW)
        self.assertEqual([e["title"] for e in oc.get("/api/history").get_json()],
                         ["주인이 뽑음"])
        self.assertEqual([e["title"] for e in tc.get("/api/history").get_json()],
                         ["팀이 뽑음"])
        key = "samevideo11:caption"
        self.assertEqual(oc.get("/api/history/" + key).get_json()["title"],
                         "주인이 뽑음")
        self.assertEqual(tc.get("/api/history/" + key).get_json()["title"],
                         "팀이 뽑음")

    def test_cannot_read_others_transcript(self):
        yt.history_add(self.result("vid_owner_1", "주인 영상"), "owner")
        r = self.client(TEAM_PW).get("/api/history/vid_owner_1:caption")
        self.assertEqual(r.status_code, 404)

    def test_cannot_delete_others_entry(self):
        yt.history_add(self.result("vid_owner_1"), "owner")
        r = self.client(TEAM_PW).delete("/api/history/vid_owner_1:caption")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(len(self.client(OWNER_PW).get("/api/history").get_json()),
                         1)

    def test_folders_are_separate(self):
        oc, tc = self.client(OWNER_PW), self.client(TEAM_PW)
        of = oc.post("/api/folders", json={"name": "주인폴더"}).get_json()
        tf = tc.post("/api/folders", json={"name": "팀폴더"}).get_json()
        self.assertEqual([f["name"] for f in oc.get("/api/folders").get_json()],
                         ["주인폴더"])
        self.assertEqual([f["name"] for f in tc.get("/api/folders").get_json()],
                         ["팀폴더"])
        # 같은 이름도 계정이 다르면 만들 수 있음
        self.assertEqual(tc.post("/api/folders",
                                 json={"name": "주인폴더"}).status_code, 200)
        # 남의 폴더는 못 지우고 못 고침
        self.assertEqual(tc.delete("/api/folders/" + of["id"]).status_code, 404)
        self.assertEqual(oc.patch("/api/folders/" + tf["id"],
                                  json={"name": "x"}).status_code, 404)

    def test_cannot_move_into_others_folder(self):
        oc, tc = self.client(OWNER_PW), self.client(TEAM_PW)
        of = oc.post("/api/folders", json={"name": "주인폴더"}).get_json()
        yt.history_add(self.result("vid_team_11"), "team")
        r = tc.patch("/api/history/vid_team_11:caption",
                     json={"folder": of["id"]})
        self.assertEqual(r.status_code, 404)

    def test_legacy_entries_belong_to_owner(self):
        """user 필드가 없는 옛 기록은 주인 것으로 보여야 함."""
        yt._save_history([{"key": "old11111111:caption",
                           "video_id": "old11111111", "title": "옛날 기록",
                           "saved_at": "2026-01-01 00:00"}])
        self.assertEqual(
            [e["title"] for e in self.client(OWNER_PW).get("/api/history").get_json()],
            ["옛날 기록"])
        self.assertEqual(self.client(TEAM_PW).get("/api/history").get_json(), [])


class Expiry(Base):
    def old(self, days):
        return time.strftime("%Y-%m-%d %H:%M",
                             time.localtime(time.time() - days * 86400))

    def prune(self):
        err, sys.stderr = sys.stderr, io.StringIO()
        try:
            return yt.prune_expired()
        finally:
            sys.stderr = err

    def seed(self, *entries):
        yt._save_history(list(entries))
        for e in entries:
            yt._result_path(e["key"], e.get("user") or "owner").write_text("{}")

    def entry(self, key, user, days):
        e = {"key": key, "video_id": key.split(":")[0], "title": key,
             "saved_at": self.old(days)}
        if user != "owner":
            e["user"] = user
        return e

    def test_team_records_expire_owner_records_do_not(self):
        self.seed(self.entry("a1:caption", "team", 31),
                  self.entry("b2:caption", "team", 29),
                  self.entry("c3:caption", "owner", 400))
        self.assertEqual(self.prune(), 1)
        left = {e["key"] for e in yt._load_history()}
        self.assertEqual(left, {"b2:caption", "c3:caption"})

    def test_expired_transcript_file_is_removed(self):
        e = self.entry("a1:caption", "team", 31)
        self.seed(e)
        path = yt._result_path("a1:caption", "team")
        self.assertTrue(path.exists())
        self.prune()
        self.assertFalse(path.exists())

    def test_unparsable_date_is_kept(self):
        self.seed({"key": "a1:caption", "video_id": "a1", "title": "x",
                   "user": "team"})          # saved_at 없음
        self.assertEqual(self.prune(), 0)
        self.assertEqual(len(yt._load_history()), 1)

    def test_nothing_to_do_is_cheap(self):
        self.seed(self.entry("c3:caption", "owner", 400))
        self.assertEqual(self.prune(), 0)


class TailnetGate(Base):
    def test_gate_is_on_with_auth(self):
        self.assertTrue(yt.tailnet_only())

    def test_allow_lan_env_turns_it_off(self):
        os.environ["YT2TEXT_ALLOW_LAN"] = "1"
        self.assertFalse(yt.tailnet_only())

    def test_lan_address_is_blocked(self):
        r = yt.app.test_client().get("/login",
                                     environ_overrides={"REMOTE_ADDR": "192.168.0.5"})
        self.assertEqual(r.status_code, 403)

    def test_tailscale_address_is_allowed(self):
        r = yt.app.test_client().get("/login",
                                     environ_overrides={"REMOTE_ADDR": "100.101.102.103"})
        self.assertEqual(r.status_code, 200)

    def test_loopback_is_allowed(self):
        # `tailscale serve` 프록시가 루프백에서 들어옴
        r = yt.app.test_client().get("/login",
                                     environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
        self.assertEqual(r.status_code, 200)

    def test_lan_allowed_when_env_set(self):
        os.environ["YT2TEXT_ALLOW_LAN"] = "1"
        r = yt.app.test_client().get("/login",
                                     environ_overrides={"REMOTE_ADDR": "192.168.0.5"})
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
