"""Desktop path, bridge and listener checks; no GUI or external downloads."""
import http.client
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import desktop
import engine
import renderer
import server


class Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self, *args):
        return [handler(*args) for handler in self.handlers]


class DesktopTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).resolve().parent.parent.parent / "work" / "desktop-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = desktop.AppPaths(self.root / "readonly", self.root / "user")
        (self.paths.resources / "web").mkdir(parents=True)
        (self.paths.resources / "web" / "index.html").write_text("<form id=\"deck-form\">Deckviewer</form>", encoding="utf-8")
        for module, names in ((engine, ("BASE_DIR", "CACHE_DIR", "GENERATED_DIR", "LIBRARY_DIR")),
                              (renderer, ("BASE_DIR",)), (server, ("ROOT",))):
            for name in names:
                original = getattr(module, name)
                self.addCleanup(setattr, module, name, original)
        desktop.configure_paths(self.paths)

    def fixture(self):
        file = self.paths.generated / "deck-test.png"
        file.write_bytes(b"PNG fixture")
        return file

    def api(self, *, origin="http://127.0.0.1:32145/", current=None):
        webview = SimpleNamespace(FileDialog=SimpleNamespace(SAVE=42))
        api = desktop.DesktopApi(self.paths.generated, origin, webview)
        api._window = Mock()
        api._window.get_current_url.return_value = current if current is not None else origin
        api._guard = SimpleNamespace(installed=True)
        return api

    def test_packaged_resources_and_mutable_user_files_are_separate(self):
        self.assertEqual(engine.CACHE_DIR, self.paths.data / "cache")
        self.assertEqual(engine.GENERATED_DIR, self.paths.data / "generated")
        self.assertEqual(engine.LIBRARY_DIR, self.paths.resources / "library")
        self.assertEqual(renderer.BASE_DIR, self.paths.resources)
        self.assertEqual(server.ROOT, self.paths.resources)
        self.assertTrue(self.paths.logs.is_dir())
        self.assertTrue(self.paths.webview.is_dir())
        self.assertFalse((self.paths.resources / "cache").exists())
        with patch.object(desktop.sys, "_MEIPASS", str(self.paths.resources), create=True):
            self.assertEqual(desktop.resource_root(), self.paths.resources.resolve())
        with patch.dict(desktop.os.environ, {"LOCALAPPDATA": str(self.root / "local-app-data")}):
            self.assertEqual(desktop.user_data_root(), self.root / "local-app-data" / "deckviewer")

    def test_window_origin_requires_exact_loopback_port_and_ui_document(self):
        origin = "http://127.0.0.1:32145/"
        for url in (origin, origin + "index.html", origin + "#preview"):
            self.assertTrue(desktop.is_app_page(url, origin), url)
        for url in (None, "http://example.com/", "http://127.0.0.1:32146/",
                    "https://127.0.0.1:32145/", "http://user@127.0.0.1:32145/",
                    "http://localhost:32145/", origin + "generated/deck-test.png",
                    origin + "?redirect=https://example.com", "file:///C:/Windows/win.ini",
                    "http://127.0.0.1:invalid/"):
            self.assertFalse(desktop.is_app_page(url, origin), url)

    def test_native_guard_cancels_external_redirects_and_new_windows(self):
        core = SimpleNamespace(NewWindowRequested=Event())
        control = SimpleNamespace(NavigationStarting=Event(),
                                  CoreWebView2InitializationCompleted=Event(), CoreWebView2=core)
        window = SimpleNamespace(native=SimpleNamespace(webview=control))
        guard = desktop.NavigationGuard(window, "http://127.0.0.1:32145/")
        guard.install()
        self.assertTrue(guard.installed)
        for url, blocked in ((guard.origin, False), ("https://example.com/", True),
                             (guard.origin + "generated/deck-test.png", True)):
            args = SimpleNamespace(Uri=url, Cancel=False)
            control.NavigationStarting.fire(None, args)
            self.assertEqual(args.Cancel, blocked)
        popup = SimpleNamespace(Handled=False)
        core.NewWindowRequested.fire(None, popup)
        self.assertTrue(popup.Handled)

    def test_generated_filename_cannot_read_other_files(self):
        file = self.fixture()
        self.assertEqual(desktop.generated_file(self.paths.generated, file.name), file.resolve())
        for value in ("../private.png", str(file), "deck-test.png/../secret", "deck-test.png?x=1",
                      "deck-test.exe", "not-created.png", None):
            with self.assertRaises(ValueError, msg=str(value)):
                desktop.generated_file(self.paths.generated, value)

    def test_native_save_uses_dialog_choice_and_adds_png_extension(self):
        source = self.fixture()
        api = self.api()
        target = self.root / "selected-export"
        api._window.create_file_dialog.return_value = (str(target),)
        result = api.save_image(source.name)
        self.assertEqual(result, {"ok": True, "filename": "selected-export.png"})
        self.assertEqual(target.with_suffix(".png").read_bytes(), source.read_bytes())
        api._window.create_file_dialog.assert_called_once_with(
            42, save_filename=source.name, file_types=("Изображение PNG (*.png)",))

    def test_cancelled_save_creates_no_export(self):
        source = self.fixture()
        api = self.api()
        api._window.create_file_dialog.return_value = None
        self.assertEqual(api.save_image(source.name), {"cancelled": True})
        self.assertEqual(list(self.root.glob("*.png")), [])

    def test_failed_export_preserves_existing_selected_file(self):
        source = self.fixture()
        target = self.root / "export.png"
        target.write_bytes(b"existing export")
        with patch.object(desktop.os, "replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                desktop.copy_png(source, target)
        self.assertEqual(target.read_bytes(), b"existing export")
        self.assertEqual(list(self.root.glob(".deckviewer-*.tmp")), [])

    def test_untrusted_page_and_inactive_guard_cannot_open_or_save(self):
        source = self.fixture()
        api = self.api(current="https://example.com/")
        with patch.object(desktop.os, "startfile", create=True) as opener:
            self.assertIn("error", api.open_image(source.name))
            self.assertIn("error", api.save_image(source.name))
            opener.assert_not_called()
            api._window.create_file_dialog.assert_not_called()
            api._window.get_current_url.return_value = api._origin
            api._guard.installed = False
            self.assertIn("error", api.open_image(source.name))
            opener.assert_not_called()

    def test_open_png_uses_only_the_validated_generated_path(self):
        source = self.fixture()
        api = self.api()
        with patch.object(desktop.os, "startfile", create=True) as opener:
            self.assertEqual(api.open_image(source.name), {"ok": True})
            opener.assert_called_once_with(str(source.resolve()))

    def request(self, runtime, method, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", runtime.server.server_port, timeout=3)
        try:
            headers = {"Content-Type": "application/json"} if body is not None else {}
            connection.request(method, path, json.dumps(body) if body is not None else None, headers)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_private_listener_serves_user_png_and_stops_with_window_runtime(self):
        source = self.fixture()
        result = {"filename": source.name, "path": str(source), "cardCount": 30, "dust": 1000}
        with desktop.DesktopRuntime(self.paths, render=Mock(return_value=result)) as runtime:
            self.assertGreater(runtime.server.server_port, 0)
            self.assertEqual(runtime.server.server_address[0], "127.0.0.1")
            self.assertEqual(self.request(runtime, "GET", "/generated/deck-test.png"), (200, source.read_bytes()))
            self.assertEqual(self.request(runtime, "GET", "/desktop.py")[0], 404)
            code, payload = self.request(runtime, "POST", "/api/generate", {"code": "fixture"})
            self.assertEqual(code, 202)
            job_id = json.loads(payload)["id"]
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                _, payload = self.request(runtime, "GET", "/api/jobs/" + job_id)
                job = json.loads(payload)
                if job["status"] == "done":
                    break
                time.sleep(.01)
            self.assertEqual(job["result"]["imageUrl"], "/generated/deck-test.png")
            self.assertEqual(runtime.active_jobs(), 0)
            worker, port = runtime.worker, runtime.server.server_port
        self.assertFalse(worker.is_alive())
        runtime.close()  # The window close event and finally may both call close.
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=.5)
        try:
            with self.assertRaises(OSError):
                connection.request("GET", "/api/health")
        finally:
            connection.close()

    def test_two_desktop_instances_get_independent_ports(self):
        with desktop.DesktopRuntime(self.paths) as first, desktop.DesktopRuntime(self.paths) as second:
            self.assertNotEqual(first.server.server_port, second.server.server_port)

    def test_runtime_cleanup_also_runs_after_gui_failure(self):
        runtime = desktop.DesktopRuntime(self.paths)
        with self.assertRaisesRegex(RuntimeError, "GUI failure"):
            with runtime:
                raise RuntimeError("GUI failure")
        self.assertIsNone(runtime.server)
        self.assertFalse(runtime.worker.is_alive())


if __name__ == "__main__":
    unittest.main()
