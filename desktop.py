"""Windows desktop shell for the existing local DeckViewer interface.

Resources stay beside the source (or inside PyInstaller's resource directory).
Downloaded cards, exported images, browser state and logs belong to the user.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import http.client
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import threading
from urllib.parse import urlsplit

import engine
import renderer
import server

APP_NAME = "deckviewer"
LOGGER = logging.getLogger(APP_NAME)
PNG_FILENAME = re.compile(r"[A-Za-z0-9_-]+\.png\Z")
WEBVIEW2_DOWNLOAD = "https://developer.microsoft.com/microsoft-edge/webview2/"


@dataclass(frozen=True)
class AppPaths:
    resources: Path
    data: Path

    @property
    def cache(self):
        return self.data / "cache"

    @property
    def generated(self):
        return self.data / "generated"

    @property
    def logs(self):
        return self.data / "logs"

    @property
    def webview(self):
        return self.data / "webview"

    def create(self):
        for directory in (self.cache, self.generated, self.logs, self.webview):
            directory.mkdir(parents=True, exist_ok=True)


def resource_root() -> Path:
    """PyInstaller onedir and onefile both set _MEIPASS to bundled assets."""
    bundled = getattr(sys, "_MEIPASS", None)
    return Path(bundled).resolve() if bundled else Path(__file__).resolve().parent


def user_data_root() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local).resolve() / APP_NAME
    return Path.home() / "AppData" / "Local" / APP_NAME


def configure_paths(paths: AppPaths):
    paths.create()
    engine.BASE_DIR = paths.resources
    engine.CACHE_DIR = paths.cache
    engine.GENERATED_DIR = paths.generated
    engine.LIBRARY_DIR = paths.resources / "library"
    renderer.BASE_DIR = paths.resources
    server.ROOT = paths.resources


def configure_logging(paths: AppPaths):
    handler = RotatingFileHandler(paths.logs / "deckviewer.log", maxBytes=2 * 1024 * 1024,
                                  backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    # A windowed PyInstaller executable has no console. Existing renderer/server
    # diagnostics use print, so keep a small stream for those diagnostics too.
    if sys.stdout is None or sys.stderr is None:
        stream = (paths.logs / "server.log").open("a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = stream
        if sys.stderr is None:
            sys.stderr = stream


def show_message(message: str, *, error=False):
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "DeckViewer", 0x10 if error else 0x40)
    elif sys.stderr is not None:
        print(message, file=sys.stderr)


def is_app_page(url: str | None, origin: str) -> bool:
    """Only the UI document may use the native bridge or replace the window."""
    if not isinstance(url, str):
        return False
    try:
        actual, expected = urlsplit(url), urlsplit(origin)
        return (actual.scheme == expected.scheme == "http" and
                actual.hostname == expected.hostname == "127.0.0.1" and
                actual.port == expected.port and actual.username is None and
                actual.password is None and actual.path in {"", "/", "/index.html"} and
                not actual.query)
    except ValueError:
        return False


def generated_file(directory: Path, filename: str) -> Path:
    if not isinstance(filename, str) or not PNG_FILENAME.fullmatch(filename):
        raise ValueError("Некорректное имя изображения.")
    directory = directory.resolve()
    file = (directory / filename).resolve()
    if file.parent != directory or not file.is_file():
        raise ValueError("Изображение не найдено. Создай колоду ещё раз.")
    return file


def copy_png(source: Path, target: Path) -> Path:
    """Publish the selected export atomically, preserving an older file on error."""
    if target.suffix.lower() != ".png":
        target = target.with_suffix(".png")
    target = target.resolve()
    if source.resolve() == target:
        return target
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".deckviewer-",
                                         suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            with source.open("rb") as input_file:
                shutil.copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
        return target
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class DesktopApi:
    """Expose just PNG export/open actions, never arbitrary file paths."""
    def __init__(self, generated_dir: Path, origin: str, webview):
        self._generated_dir = generated_dir
        self._origin = origin
        self._webview = webview
        self._window = None
        self._guard = None
        self._dialog_lock = threading.Lock()

    def _source(self, filename):
        if (self._window is None or self._guard is None or not self._guard.installed or
                not is_app_page(self._window.get_current_url(), self._origin)):
            raise ValueError("Действие разрешено только в окне DeckViewer.")
        return generated_file(self._generated_dir, filename)

    def save_image(self, filename):
        try:
            source = self._source(filename)
            # A second rapid click should not open another native dialog.
            if not self._dialog_lock.acquire(blocking=False):
                return {"cancelled": True}
            try:
                selected = self._window.create_file_dialog(
                    self._webview.FileDialog.SAVE, save_filename=source.name,
                    file_types=("Изображение PNG (*.png)",))
                if not selected:
                    return {"cancelled": True}
                destination = selected[0] if isinstance(selected, (tuple, list)) else selected
                saved = copy_png(source, Path(destination))
                return {"ok": True, "filename": saved.name}
            finally:
                self._dialog_lock.release()
        except (ValueError, OSError) as error:
            LOGGER.warning("Save image failed: %s", error)
            return {"error": str(error) if isinstance(error, ValueError) else
                    "Не удалось сохранить PNG. Выбери другую папку."}

    def open_image(self, filename):
        try:
            source = self._source(filename)
            os.startfile(str(source))
            return {"ok": True}
        except (ValueError, OSError) as error:
            LOGGER.warning("Open image failed: %s", error)
            return {"error": str(error) if isinstance(error, ValueError) else
                    "Не удалось открыть PNG. Попробуй сохранить изображение."}


class NavigationGuard:
    """Block navigation before another document can receive Python privileges."""
    def __init__(self, window, origin):
        self.window = window
        self.origin = origin
        self.installed = False
        self.failure = None

    def install(self):
        control = self.window.native.webview
        control.NavigationStarting += self._navigation_starting
        control.CoreWebView2InitializationCompleted += self._initialization_completed
        self.installed = True
        if control.CoreWebView2 is not None:
            self._install_core_guard(control.CoreWebView2)

    def _navigation_starting(self, _sender, args):
        if not is_app_page(str(args.Uri), self.origin):
            args.Cancel = True
            LOGGER.info("Blocked navigation outside the DeckViewer UI")

    def _new_window(self, _sender, args):
        args.Handled = True

    def _install_core_guard(self, core):
        core.NewWindowRequested += self._new_window

    def _initialization_completed(self, sender, args):
        if args.IsSuccess:
            self._install_core_guard(sender.CoreWebView2)
        else:
            self.failure = RuntimeError("WebView2 initialization failed")
            LOGGER.error("WebView2 initialization failed: %s", args.InitializationException)
            show_message("Не удалось запустить окно приложения. Установи Microsoft Edge WebView2 Runtime.\n"
                         + WEBVIEW2_DOWNLOAD, error=True)
            self.window.native.Close()


DESKTOP_SCRIPT = r"""
(() => {
  if (window.__deckviewerNativeActions) return;
  window.__deckviewerNativeActions = true;
  document.addEventListener('click', async event => {
    const link = event.target.closest('a');
    if (!link) return;
    if (!['open-image', 'download-image'].includes(link.id)) {
      event.preventDefault();
      return;
    }
    event.preventDefault();
    event.stopImmediatePropagation();
    if (link.dataset.nativeBusy === 'true') return;
    let url;
    try { url = new URL(link.href); } catch { return; }
    const filename = url.pathname.split('/').pop();
    if (url.origin !== location.origin || !/^\/generated\/[A-Za-z0-9_-]+\.png$/.test(url.pathname)) return;
    const status = document.getElementById('status');
    if (!window.pywebview?.api) {
      status.textContent = 'Окно ещё запускается. Повтори попытку через секунду.';
      return;
    }
    link.dataset.nativeBusy = 'true';
    try {
      const result = await window.pywebview.api[
        link.id === 'download-image' ? 'save_image' : 'open_image'
      ](filename);
      if (result.error) status.textContent = result.error;
      else if (result.ok && link.id === 'download-image') status.textContent = `PNG сохранён: ${result.filename}`;
    } catch {
      status.textContent = 'Не удалось выполнить действие. Повтори попытку.';
    } finally { delete link.dataset.nativeBusy; }
  }, true);
})();
"""


class DesktopRuntime:
    def __init__(self, paths: AppPaths, *, render=None):
        self.paths = paths
        self.server = None
        self.worker = None
        self._render = render

    @property
    def url(self):
        if self.server is None:
            raise RuntimeError("The local server has not started")
        return f"http://127.0.0.1:{self.server.server_port}/"

    def start(self):
        if self.server is not None:
            raise RuntimeError("The local server has already started")
        options = {"generated_dir": self.paths.generated}
        if self._render is not None:
            options["engine"] = self._render
        self.server = server.LocalServer(("127.0.0.1", 0), **options)
        self.worker = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": .1}, daemon=True,
                                       name="deckviewer-http")
        self.worker.start()
        return self

    def active_jobs(self):
        if self.server is None:
            return 0
        with self.server.lock:
            return sum(job["status"] in {"queued", "running"}
                       for job in self.server.jobs.values())

    def close(self):
        instance, self.server = self.server, None
        if instance is not None:
            instance.shutdown()
            if self.worker is not None:
                self.worker.join(timeout=3)
            instance.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *_args):
        self.close()


def update_library():
    for locale in ("ruRU", "enUS"):
        try:
            engine.load_metadata(locale)
        except engine.DeckViewError as error:
            LOGGER.warning("Library update: %s", error)
        except Exception:
            LOGGER.exception("Library update failed")


def run_desktop(paths: AppPaths, webview):
    LOGGER.info("Creating desktop window")
    webview.settings["ALLOW_DOWNLOADS"] = False
    webview.settings["ALLOW_FILE_URLS"] = False
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = False
    with DesktopRuntime(paths) as runtime:
        api = DesktopApi(paths.generated, runtime.url, webview)
        window = webview.create_window("DeckViewer", runtime.url, js_api=api,
                                       width=1440, height=960, min_size=(900, 650),
                                       background_color="#14252d", text_select=True)
        guard = NavigationGuard(window, runtime.url)
        api._window, api._guard = window, guard

        def before_show():
            LOGGER.info("Installing navigation guard")
            try:
                guard.install()
                LOGGER.info("Navigation guard installed")
            except Exception as error:
                guard.failure = error
                LOGGER.exception("Cannot protect desktop navigation")
                window.native.Close()

        def on_loaded():
            LOGGER.info("Desktop interface loaded")
            if not guard.installed or not is_app_page(window.get_current_url(), runtime.url):
                window.destroy()
                return
            # run_js does not use eval, so the UI keeps its existing strict CSP.
            window.run_js(DESKTOP_SCRIPT)

        def on_closing():
            if runtime.active_jobs():
                show_message("Идёт создание изображения. Дождись завершения, затем закрой окно.")
                return False
            return True

        window.events.before_show += before_show
        window.events.loaded += on_loaded
        window.events.closing += on_closing
        window.events.closed += runtime.close
        threading.Thread(target=update_library, name="deckviewer-library", daemon=True).start()
        icon = paths.resources / "web" / "icon.ico"
        LOGGER.info("Starting WebView2")
        webview.start(gui="edgechromium", debug=False, private_mode=True,
                      storage_path=str(paths.webview), icon=str(icon) if icon.is_file() else None)
        if guard.failure is not None:
            raise RuntimeError("Не удалось запустить окно DeckViewer.") from guard.failure


def smoke_test(paths: AppPaths):
    """Verify packaged data, UI serving and listener shutdown without a GUI."""
    from local_library import unpack_cards
    counts = {}
    for locale in ("ruRU", "enUS"):
        counts[locale] = len(unpack_cards((paths.resources / "library" /
                                         f"cards.{locale}.json.gz").read_bytes()))
    with DesktopRuntime(paths) as runtime:
        port = runtime.server.server_port
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("GET", "/api/health")
            response = connection.getresponse()
            health = json.loads(response.read())
            if response.status != 200 or health.get("status") != "ok":
                raise RuntimeError("The packaged local server failed its health check")
            connection.request("GET", "/")
            response = connection.getresponse()
            page = response.read()
            if response.status != 200 or b'id="deck-form"' not in page:
                raise RuntimeError("The packaged UI is missing")
            for path in ("/app.js", "/styles.css", "/tokens.css", "/example.png"):
                connection.request("GET", path)
                response = connection.getresponse()
                if response.status != 200 or not response.read():
                    raise RuntimeError(f"Missing packaged resource: {path}")
        finally:
            connection.close()
    if runtime.worker.is_alive():
        raise RuntimeError("The local server did not stop")
    return {"ok": True, "app": APP_NAME, "cardCounts": counts,
            "dataDir": str(paths.data), "resources": str(paths.resources),
            "privatePort": port, "serverStopped": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description="DeckViewer desktop application")
    parser.add_argument("--data-dir", type=Path, help="Override the per-user data directory")
    parser.add_argument("--smoke-test", action="store_true", help="Check packaging without opening a window")
    parser.add_argument("--smoke-output", type=Path, help="Write smoke-check results as JSON")
    args = parser.parse_args(argv)
    paths = AppPaths(resource_root(), (args.data_dir or user_data_root()).resolve())
    try:
        configure_paths(paths)
        configure_logging(paths)
        if args.smoke_test:
            result = smoke_test(paths)
            if args.smoke_output:
                args.smoke_output.parent.mkdir(parents=True, exist_ok=True)
                args.smoke_output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            elif sys.stdout is not None:
                print(json.dumps(result, ensure_ascii=False))
        else:
            if sys.platform != "win32":
                raise RuntimeError("Эта сборка DeckViewer рассчитана на Windows.")
            LOGGER.info("Loading desktop runtime")
            import webview
            LOGGER.info("Desktop runtime imported")
            run_desktop(paths, webview)
        return 0
    except Exception as error:
        LOGGER.exception("DeckViewer startup failed")
        if args.smoke_output:
            args.smoke_output.parent.mkdir(parents=True, exist_ok=True)
            args.smoke_output.write_text(json.dumps({"ok": False, "error": str(error)},
                                                   ensure_ascii=False, indent=2), encoding="utf-8")
        if not args.smoke_test:
            show_message("Не удалось запустить DeckViewer.\n\n"
                         "Для окна приложения нужен Microsoft Edge WebView2 Runtime:\n"
                         f"{WEBVIEW2_DOWNLOAD}\n\nПодробности: {paths.logs / 'deckviewer.log'}", error=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
