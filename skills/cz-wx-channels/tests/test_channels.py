from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from channels_core import VideoError, normalize_inputs, safe_filename, unwrap


def share(eid):
    return "https://channels.weixin.qq.com/finder-preview/pages/feed?" + urlencode({"eid": eid, "token": "0"})


class InputTests(unittest.TestCase):
    def test_nested_messages_keep_context_and_deduplicate(self):
        url = share("export/good")
        jobs, count = normalize_inputs({"group": "示例群", "messages": [
            {"text": "<url>" + url.replace("&", "&amp;") + "</url>", "send_time": "2026-01-01"},
            {"url": url + "&tracking=another", "title": "重复视频"},
            {"url": "https://weixin.qq.com/sph/ShareExample"}]})
        self.assertEqual((count, len(jobs)), (3, 2))
        self.assertEqual(jobs[0]["query"], {"eid": "export/good"})
        self.assertEqual(jobs[0]["contexts"][0]["group"], "示例群")
        self.assertEqual(jobs[0]["occurrences"], 2)

    def test_nested_platform_error_is_not_success(self):
        with self.assertRaises(VideoError) as raised:
            unwrap({"code": 0, "data": {"errCode": 0, "data": {"errMsg": {"type": 2, "title": "作品已删除"}}}})
        self.assertEqual(raised.exception.code, "profile_error")
        with self.assertRaises(VideoError):
            unwrap({"code": 0, "errCode": 1001, "errMsg": "failed"})

    def test_filenames_stay_inside_video_directory(self):
        name = safe_filename("../../视频\\标题:测试", "012345")
        self.assertNotIn("/", name)
        self.assertNotIn("\\", name)
        self.assertTrue(name.endswith("--012345.mp4"))


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg enables file delivery tests")
class DeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_dir = tempfile.TemporaryDirectory()
        movie = Path(cls.fixture_dir.name) / "fixture.mp4"
        subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                        "color=c=black:s=160x90:r=10:d=0.5", "-f", "lavfi", "-i",
                        "anullsrc=r=44100:cl=mono", "-t", "0.5", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart",
                        "-y", str(movie)], check=True, timeout=30)
        cls.movie = movie.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.fixture_dir.cleanup()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.output = Path(self.directory.name) / "output"
        self.calls = Counter()
        self.connected = True
        self.cached = set()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, value, status=200):
                raw = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query)
                owner.calls[parsed.path] += 1
                if parsed.path == "/api/channels/status":
                    self.reply({"code": 0, "data": {"available": owner.connected}})
                elif parsed.path == "/api/channels/feed/profile":
                    eid = query.get("eid", query.get("url", [""]))[0].rsplit("/", 1)[-1]
                    owner.calls["profile:" + eid] += 1
                    if not owner.connected and eid not in owner.cached:
                        self.reply({"code": 400, "msg": "please initialize the client socket connection first"})
                    elif eid == "deleted":
                        self.reply({"code": 0, "data": {"errCode": 0, "data": {"errMsg": {"type": 2, "title": "作品已删除"}}}})
                    elif eid == "retry" and owner.calls["profile:retry"] == 1:
                        self.reply({"code": 503}, 503)
                    else:
                        self.reply({"code": 0, "data": {"errCode": 0, "data": {"object": {
                            "objectDesc": {"description": "示例/视频 " + eid, "media": [{
                                "url": "https://finder.video.qq.com/" + eid,
                                "urlToken": "?token=fixture", "decodeKey": "fixture-key",
                                "mediaType": 4, "width": 160, "height": 90}]},
                            "contact": {"nickname": "示例作者"}, "urlValidDuration": 3600}}}})
                elif parsed.path == "/play":
                    if query.get("key") != ["fixture-key"]:
                        self.reply({"code": 403}, 403)
                        return
                    eid = urlsplit(query["url"][0]).path.strip("/")
                    ranged = bool(self.headers.get("Range"))
                    body = owner.movie[:4096] if ranged else owner.movie
                    if not ranged:
                        owner.calls["full_download"] += 1
                    self.send_response(206 if ranged else 200)
                    self.send_header("Content-Type", "video/mp4")
                    if eid == "truncated" and not ranged:
                        self.send_header("Content-Length", str(len(body) + 500))
                    else:
                        self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    self.close_connection = True
                else:
                    self.reply({"code": 404}, 404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.directory.cleanup()

    def invoke(self, urls, retries=0):
        command = [sys.executable, str(ROOT / "scripts/channels.py"), "download",
                   "--backend-url", self.base, "--output", str(self.output), "--retries", str(retries)]
        for url in urls:
            command += ["--url", url]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        manifest = json.loads((self.output / "manifest.json").read_text())
        return result, manifest

    def test_batch_decrypt_download_deduplicate_and_reuse(self):
        urls = [share("export/good"), share("export/good"), "https://weixin.qq.com/sph/ShareExample"]
        result, report = self.invoke(urls)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["duplicates_removed"], 1)
        self.assertEqual(report["summary"]["completed"], 2)
        self.assertTrue(all(item["file"]["video_codec"] == "h264" for item in report["items"]))
        self.assertTrue(all(item["file"]["audio_codec"] == "aac" for item in report["items"]))
        self.assertTrue(all(item["media"]["requires_decryption"] for item in report["items"]))
        self.assertEqual(os.stat(self.output / "manifest.json").st_mode & 0o777, 0o600)
        calls = self.calls.copy()
        result, report = self.invoke(urls)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(all(item["status"] == "reused" for item in report["items"]))
        self.assertEqual(self.calls, calls)

    def test_partial_failure_keeps_successful_file(self):
        result, report = self.invoke([share("export/good"), share("export/deleted")])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report["summary"]["completed"], 1)
        self.assertEqual(report["summary"]["failed"], 1)
        self.assertEqual(report["items"][1]["error"]["code"], "profile_error")
        self.assertTrue(Path(report["items"][0]["file"]["path"]).is_file())

    def test_transient_profile_error_retries(self):
        result, report = self.invoke([share("export/retry")], retries=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["items"][0]["attempts"], 2)
        self.assertEqual(self.calls["profile:retry"], 2)

    def test_incomplete_file_is_not_delivered(self):
        result, report = self.invoke([share("export/truncated")])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report["summary"]["failed"], 1)
        self.assertEqual(list(self.output.glob("videos/*.mp4")), [])
        self.assertEqual(list(self.output.glob("videos/*.part")), [])

    def test_disconnected_page_records_environment_once(self):
        self.connected = False
        result, report = self.invoke([share("export/good"), share("export/another")])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report["summary"]["failed"], 2)
        self.assertEqual(self.calls["/api/channels/status"], 0)
        self.assertEqual(self.calls["/api/channels/feed/profile"], 1)

    def test_cached_profile_downloads_without_page_connection(self):
        self.connected = False
        self.cached.add("good")
        result, report = self.invoke([share("export/good")])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["items"][0]["status"], "downloaded")


class AdapterAndInstallTests(unittest.TestCase):
    def test_adapter_keeps_exports_outside_collection_root(self):
        repository = next((path for path in (ROOT, *ROOT.parents) if (path / ".git").exists()), None)
        if repository is None:
            self.skipTest("Collection-root check applies to a Git checkout")
        with tempfile.TemporaryDirectory() as name:
            source = Path(name) / "messages.json"
            source.write_text(json.dumps([share("export/good")]))
            destination = repository / "accidental-collection-export.json"
            result = subprocess.run([sys.executable, str(ROOT / "adapters/wecom/export_links.py"),
                                     "--input", str(source), "--output", str(destination)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(destination.exists())

    def test_adapter_keeps_exports_outside_publish_source(self):
        with tempfile.TemporaryDirectory() as name:
            source = Path(name) / "messages.json"
            source.write_text(json.dumps([share("export/good")]))
            destination = ROOT / "accidental-export.json"
            result = subprocess.run([sys.executable, str(ROOT / "adapters/wecom/export_links.py"),
                                     "--input", str(source), "--output", str(destination)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(destination.exists())

    def test_exported_messages_feed_standard_input(self):
        with tempfile.TemporaryDirectory() as name:
            source, output = Path(name) / "messages.json", Path(name) / "links.json"
            source.write_text(json.dumps({"messages": [{"text": share("export/good"), "user_name": "示例用户"}]}))
            result = subprocess.run([sys.executable, str(ROOT / "adapters/wecom/export_links.py"),
                                     "--input", str(source), "--output", str(output)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            exported = json.loads(output.read_text())
            jobs, _ = normalize_inputs(exported)
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["contexts"][0]["user_name"], "示例用户")

    def test_install_is_a_separate_copy(self):
        with tempfile.TemporaryDirectory() as name:
            destination = Path(name) / "installed"
            before = (ROOT / "SKILL.md").read_bytes()
            result = subprocess.run([sys.executable, str(ROOT / "scripts/install_local.py"),
                                     "--destination", str(destination)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(destination.is_symlink())
            (destination / "SKILL.md").write_text("local customization")
            self.assertEqual((ROOT / "SKILL.md").read_bytes(), before)
            self.assertFalse((destination / ".git").exists())

    def test_replace_keeps_backups_outside_skill_discovery(self):
        with tempfile.TemporaryDirectory() as name:
            skills = Path(name) / "skills"
            destination = skills / "cz-wx-channels"
            backups = Path(name) / "skill-backups"
            command = [sys.executable, str(ROOT / "scripts/install_local.py"),
                       "--destination", str(destination), "--backup-dir", str(backups)]
            subprocess.run(command, capture_output=True, text=True, check=True)
            (destination / "SKILL.md").write_text("local previous version")
            subprocess.run(command + ["--replace"], capture_output=True, text=True, check=True)
            self.assertEqual(len(list(skills.glob("*/SKILL.md"))), 1)
            archived = list(backups.glob("*/SKILL.md"))
            self.assertEqual(len(archived), 1)
            self.assertEqual(archived[0].read_text(), "local previous version")


if __name__ == "__main__":
    unittest.main()
