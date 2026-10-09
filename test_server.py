"""HTTP regression checks using a fake renderer; no network downloads.

Run from this directory: python -m unittest test_server
"""
import contextlib
import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import server


class ServerTests(unittest.TestCase):
    def setUp(self):
        scratch = server.ROOT.parent.parent / "work" / "server-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.root = Path(self.temp.name)
        (self.root / "web").mkdir()
        (self.root / "web" / "index.html").write_text("<h1>Deck View</h1>", encoding="utf-8")
        (self.root / "web" / "app.js").write_text("console.log('local');", encoding="utf-8")
        (self.root / "generated").mkdir()
        (self.root / "generated" / "deck-test.png").write_bytes(b"PNG fixture")
        (self.root / "server.py").write_text("private server source", encoding="utf-8")
        (self.root / "cache").mkdir()
        (self.root / "cache" / "private.json").write_text("private cache", encoding="utf-8")
        self.root_patch = patch.object(server, "ROOT", self.root)
        self.root_patch.start()
        self.result = {"filename": "deck-test.png", "path": str(self.root / "generated" / "deck-test.png"),
                       "className": "Охотник", "cardCount": 30, "dust": 1000,
                       "locale": "ruRU", "sideboardCount": 0, "warnings": []}
        self.engine = Mock(return_value=self.result)
        self.server = server.LocalServer(("127.0.0.1", 0), engine=self.engine)
        self.worker = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.02}, daemon=True)
        self.worker.start()
        self.releases = []

    def tearDown(self):
        for event in self.releases:
            event.set()
        self.server.shutdown()
        self.worker.join(timeout=3)
        self.server.server_close()
        self.server.executor.shutdown(wait=True)
        self.root_patch.stop()
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def json_request(self, method, path, body=None, headers=None):
        status, response_headers, payload = self.request(method, path, body, headers)
        return status, response_headers, json.loads(payload)

    def generate(self, body=None):
        body = {"code": "AA-test-code"} if body is None else body
        return self.json_request("POST", "/api/generate", json.dumps(body).encode(),
                                 {"Content-Type": "application/json; charset=utf-8"})

    def wait_for_job(self, job_id, status=None):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            response_status, _, job = self.json_request("GET", f"/api/jobs/{job_id}")
            self.assertEqual(response_status, 200)
            if job["status"] == status or (status is None and job["status"] in {"done", "error"}):
                return job
            time.sleep(0.01)
        self.fail(f"Job {job_id} did not reach {status or 'a terminal state'}: {job}")

    def test_health_and_example_are_local_json_with_protective_headers(self):
        status, headers, health = self.json_request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["app"], "deckview-local")
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["build"], server.build_signature())
        self.assertEqual(health["pid"], server.os.getpid())
        self.assertEqual(health["activeJobs"], 0)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        status, _, example = self.json_request("GET", "/api/example")
        self.assertEqual(status, 200)
        self.assertEqual(example["code"], server.EXAMPLE_CODE)
        self.engine.assert_not_called()

    def test_another_instance_cannot_bind_the_same_loopback_port(self):
        with self.assertRaises(OSError):
            server.LocalServer(("127.0.0.1", self.server.server_port), engine=self.engine)

    def test_job_preserves_input_reports_progress_and_publishes_downloadable_result(self):
        release, started = threading.Event(), threading.Event()
        self.releases.append(release)

        def render(code, locale, progress):
            progress("Загружено 10 карт")
            started.set()
            if not release.wait(3):
                raise AssertionError("Test did not release renderer")
            return self.result

        self.engine.side_effect = render
        pasted = "### Моя колода\nAA-test-code\n# Экспорт"
        status, _, created = self.generate({"code": pasted, "locale": "enUS"})
        self.assertEqual(status, 202)
        self.assertRegex(created["id"], r"^[0-9a-f]{32}$")
        self.assertTrue(started.wait(3))
        running = self.wait_for_job(created["id"], "running")
        self.assertEqual(running["message"], "Загружено 10 карт")
        release.set()
        job = self.wait_for_job(created["id"])
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["result"]["imageUrl"], "/generated/deck-test.png")
        self.assertNotIn("path", job["result"])
        self.assertIn("path", self.result, "Server should not mutate engine result")
        self.assertEqual(self.engine.call_args.args[:2], (pasted, "enUS"))
        status, headers, payload = self.request("GET", job["result"]["imageUrl"] + "?download=1")
        self.assertEqual(status, 200)
        self.assertEqual(payload, b"PNG fixture")
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="deck-test.png"')

    def test_safe_engine_error_is_visible_and_default_locale_is_russian(self):
        self.engine.side_effect = server.DeckViewError("Некорректный код колоды")
        status, _, created = self.generate()
        self.assertEqual(status, 202)
        job = self.wait_for_job(created["id"])
        self.assertEqual(job, {"status": "error", "message": "Некорректный код колоды"})
        self.assertEqual(self.engine.call_args.args[:2], ("AA-test-code", "ruRU"))

    def test_unexpected_engine_exception_gets_safe_message_and_log(self):
        self.engine.side_effect = RuntimeError("private filesystem detail")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status, _, created = self.generate()
            self.assertEqual(status, 202)
            job = self.wait_for_job(created["id"])
        self.assertEqual(job["status"], "error")
        self.assertIn("server.log", job["message"])
        self.assertNotIn("private filesystem detail", job["message"])
        self.assertIn("RuntimeError: private filesystem detail", output.getvalue())

    def test_invalid_json_fields_and_encoding_do_not_schedule_jobs(self):
        bodies = [b"{", b"[]", b"null", b'"text"', b"\xff", b"{}",
                  b'{"code": null}', b'{"code": 123}', b'{"code": "  "}',
                  b'{"code": "AA", "locale": "frFR"}',
                  b'{"code": "AA", "locale": null}']
        for body in bodies:
            with self.subTest(body=body):
                status, _, error = self.json_request("POST", "/api/generate", body,
                                                    {"Content-Type": "application/json"})
                self.assertEqual(status, 400)
                self.assertIsInstance(error["error"], str)
        self.engine.assert_not_called()
        self.assertEqual(self.server.jobs, {})

    def test_content_type_and_body_size_are_validated(self):
        cases = [({}, b"{}", 415),
                 ({"Content-Type": "text/plain"}, b"{}", 415),
                 ({"Content-Type": "application/json"}, b"", 413),
                 ({"Content-Type": "application/json"}, b"x" * 40961, 413),
                 ({"Content-Type": "application/json", "Content-Length": "bad"}, b"{}", 400),
                 ({"Content-Type": "application/json", "Content-Length": "-1"}, b"{}", 413)]
        for headers, body, expected in cases:
            with self.subTest(headers=headers, size=len(body)):
                status, _, error = self.json_request("POST", "/api/generate", body, headers)
                self.assertEqual(status, expected)
                self.assertIn("error", error)
        self.engine.assert_not_called()

    def test_foreign_host_and_origin_cannot_read_files_or_create_jobs(self):
        cases = [{"Host": "attacker.example"},
                 {"Host": "127.0.0.1:1"},
                 {"Origin": "https://attacker.example"},
                 {"Origin": "null"},
                 {"Origin": f"https://127.0.0.1:{self.server.server_port}"},
                 {"Origin": f"http://localhost:{self.server.server_port + 1}"}]
        for supplied in cases:
            with self.subTest(headers=supplied):
                self.assertEqual(self.json_request("GET", "/", headers=supplied)[0], 403)
                headers = {"Content-Type": "application/json", **supplied}
                self.assertEqual(self.json_request("POST", "/api/generate", b'{"code":"AA"}', headers)[0], 403)
        allowed = {"Host": f"localhost:{self.server.server_port}",
                   "Origin": f"http://localhost:{self.server.server_port}"}
        self.assertEqual(self.json_request("GET", "/api/health", headers=allowed)[0], 200)
        self.engine.assert_not_called()

    def test_static_and_generated_files_are_whitelisted(self):
        for path in ("/", "/index.html?anything=1", "/app.js"):
            with self.subTest(path=path):
                status, headers, body = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertTrue(body)
                self.assertIn("charset=utf-8", headers["Content-Type"])
                self.assertNotIn("Content-Disposition", headers)
        blocked = ["/server.py", "/cache/private.json", "/web/index.html",
                   "/../server.py", "/%2e%2e/server.py", "/generated/../server.py",
                   "/generated/%2e%2e/server.py", "/generated/deck-test.png/../server.py",
                   "/generated/deck-test.PNG", "/generated/deck-test.png.exe",
                   "/generated/%64eck-test.png", "/generated/..\\server.py",
                   "/generated/missing.png", "/styles.css", "/favicon.ico"]
        for path in blocked:
            with self.subTest(path=path):
                status, _, body = self.json_request("GET", path)
                self.assertEqual(status, 404)
                self.assertIn("error", body)

    def test_unknown_job_and_post_endpoint_are_404(self):
        self.assertEqual(self.json_request("GET", "/api/jobs/not-a-real-job")[0], 404)
        self.assertEqual(self.json_request("POST", "/api/health", b"{}")[0], 404)
        self.assertEqual(self.json_request("POST", "/api/generate?other=1", b"{}")[0], 404)
        self.engine.assert_not_called()

    def test_two_concurrent_jobs_limit_work_and_capacity_recovers(self):
        release = threading.Event()
        self.releases.append(release)
        started = [threading.Event(), threading.Event()]
        counter_lock = threading.Lock()
        counter = 0

        def render(code, locale, progress):
            nonlocal counter
            with counter_lock:
                index = counter
                counter += 1
            if index < 2:
                started[index].set()
                if not release.wait(3):
                    raise AssertionError("Test did not release renderer")
            return self.result

        self.engine.side_effect = render
        created = []
        for _ in range(2):
            status, _, job = self.generate()
            self.assertEqual(status, 202)
            created.append(job["id"])
        self.assertTrue(all(event.wait(3) for event in started))
        status, _, error = self.generate()
        self.assertEqual(status, 429)
        self.assertIn("Дождись", error["error"])
        self.assertEqual(len(self.server.jobs), 2)
        release.set()
        for job_id in created:
            self.assertEqual(self.wait_for_job(job_id)["status"], "done")
        status, _, recovered = self.generate()
        self.assertEqual(status, 202)
        self.assertEqual(self.wait_for_job(recovered["id"])["status"], "done")
        self.assertEqual(self.engine.call_count, 3)

    def test_finished_history_is_bounded_without_removing_active_job(self):
        with self.server.lock:
            for index in range(25):
                self.server.jobs[f"old-{index}"] = {"status": "done", "message": "done"}
            self.server.jobs["active"] = {"status": "running", "message": "working"}
        status, _, created = self.generate()
        self.assertEqual(status, 202)
        self.assertEqual(self.wait_for_job(created["id"])["status"], "done")
        with self.server.lock:
            history = dict(self.server.jobs)
        self.assertIn("active", history)
        self.assertNotIn("old-0", history)
        self.assertNotIn("old-4", history)
        self.assertIn("old-5", history)
        self.assertEqual(len(history), 22)


if __name__ == "__main__":
    unittest.main()
