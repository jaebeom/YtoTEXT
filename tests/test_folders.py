"""yt2text 폴더 기능 단위 테스트.

실행: python3 -m unittest discover -s tests   (프로젝트 루트에서)
네트워크는 안 씀 — 데이터 경로만 임시 폴더로 갈아끼움.
"""

import importlib.util
import json
import shutil
import sys
import tempfile
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


class FolderApi(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="yt2text_test_"))
        self.saved = (yt.HISTORY_FILE, yt.FOLDERS_FILE, yt.RESULTS_DIR,
                      yt.THUMBS_DIR, yt.ALLOWED_HOSTS)
        yt.HISTORY_FILE = self.tmp / "history.json"
        yt.FOLDERS_FILE = self.tmp / "folders.json"
        yt.RESULTS_DIR = self.tmp / "results"
        yt.THUMBS_DIR = self.tmp / "thumbs"
        yt.ALLOWED_HOSTS = set()          # 테스트 클라이언트 Host 허용
        self.client = yt.app.test_client()

    def tearDown(self):
        (yt.HISTORY_FILE, yt.FOLDERS_FILE, yt.RESULTS_DIR,
         yt.THUMBS_DIR, yt.ALLOWED_HOSTS) = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 도우미
    def add(self, name):
        r = self.client.post("/api/folders", json={"name": name})
        return r, r.get_json()

    def seed_history(self, *entries):
        yt._save_history(list(entries))

    def entry(self, key, **kw):
        e = {"key": key, "video_id": key.split(":")[0], "title": "제목"}
        e.update(kw)
        return e

    def history(self):
        return self.client.get("/api/history").get_json()

    # ---- 폴더 만들기
    def test_create_and_list(self):
        r, f = self.add("  머신러닝  강의 ")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(f["name"], "머신러닝 강의")   # 공백 정리
        self.assertTrue(f["id"])
        self.assertEqual(self.client.get("/api/folders").get_json(), [f])

    def test_blank_name_rejected(self):
        for bad in ("", "   ", "\n\t"):
            self.assertEqual(self.add(bad)[0].status_code, 400)
        self.assertEqual(self.client.get("/api/folders").get_json(), [])

    def test_duplicate_name_rejected(self):
        self.add("요리")
        self.assertEqual(self.add("요리")[0].status_code, 409)

    def test_name_length_capped(self):
        _, f = self.add("가" * 100)
        self.assertEqual(len(f["name"]), yt.FOLDER_NAME_MAX)

    def test_folder_count_capped(self):
        for i in range(yt.FOLDER_MAX):
            self.assertEqual(self.add(f"폴더{i}")[0].status_code, 200)
        self.assertEqual(self.add("하나 더")[0].status_code, 400)

    def test_non_json_rejected(self):
        r = self.client.post("/api/folders", data="name=x",
                             content_type="text/plain")
        self.assertEqual(r.status_code, 400)

    # ---- 이름 바꾸기 / 삭제
    def test_rename(self):
        _, f = self.add("옛이름")
        r = self.client.patch("/api/folders/" + f["id"], json={"name": "새이름"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/api/folders").get_json()[0]["name"],
                         "새이름")

    def test_rename_unknown_folder(self):
        r = self.client.patch("/api/folders/deadbeef", json={"name": "x"})
        self.assertEqual(r.status_code, 404)

    def test_rename_to_existing_name_rejected(self):
        self.add("A")
        _, b = self.add("B")
        r = self.client.patch("/api/folders/" + b["id"], json={"name": "A"})
        self.assertEqual(r.status_code, 409)

    def test_rename_to_own_name_allowed(self):
        _, f = self.add("A")
        r = self.client.patch("/api/folders/" + f["id"], json={"name": "A"})
        self.assertEqual(r.status_code, 200)

    def test_delete_folder_keeps_videos(self):
        _, f = self.add("임시")
        self.seed_history(self.entry("v1:caption", folder=f["id"]),
                          self.entry("v2:caption"))
        r = self.client.delete("/api/folders/" + f["id"])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/api/folders").get_json(), [])
        hist = self.history()
        self.assertEqual(len(hist), 2)                    # 영상은 그대로
        self.assertFalse(any(e.get("folder") for e in hist))  # 미분류로

    def test_delete_unknown_folder(self):
        self.assertEqual(self.client.delete("/api/folders/deadbeef").status_code,
                         404)

    # ---- 항목 옮기기
    def test_move_and_unset(self):
        _, f = self.add("강의")
        self.seed_history(self.entry("v1:caption"))
        r = self.client.patch("/api/history/v1:caption", json={"folder": f["id"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.history()[0]["folder"], f["id"])

        r = self.client.patch("/api/history/v1:caption", json={"folder": None})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("folder", self.history()[0])

    def test_move_to_unknown_folder(self):
        self.seed_history(self.entry("v1:caption"))
        r = self.client.patch("/api/history/v1:caption", json={"folder": "nope"})
        self.assertEqual(r.status_code, 404)
        self.assertNotIn("folder", self.history()[0])

    def test_move_unknown_entry(self):
        _, f = self.add("강의")
        r = self.client.patch("/api/history/없는키", json={"folder": f["id"]})
        self.assertEqual(r.status_code, 404)

    # ---- 다시 추출해도 폴더 유지 (재추출 버튼의 전제)
    def test_reextract_keeps_folder(self):
        _, f = self.add("보관")
        self.seed_history(self.entry("v1:caption", folder=f["id"]))
        yt.RESULTS_DIR.mkdir(parents=True, exist_ok=True)

        res = {"video_id": "v1", "source": "caption", "title": "새 제목",
               "language": "Korean", "duration": "1:00",
               "paragraphs": [], "lines": []}
        yt.history_add(res)

        hist = self.history()
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["title"], "새 제목")     # 새로 뽑은 결과로 갱신
        self.assertEqual(hist[0]["folder"], f["id"])      # 폴더는 그대로

    def test_new_entry_has_no_folder(self):
        yt.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        yt.history_add({"video_id": "v9", "source": "caption", "title": "새 영상",
                        "language": "Korean", "duration": "1:00",
                        "paragraphs": [], "lines": []})
        self.assertNotIn("folder", self.history()[0])

    # ---- 저장 형식
    def test_folders_file_is_json_list(self):
        self.add("가")
        data = json.loads(yt.FOLDERS_FILE.read_text(encoding="utf-8"))
        self.assertEqual([f["name"] for f in data], ["가"])


if __name__ == "__main__":
    unittest.main()
