"""Loopback-only local UI for the deck image engine."""
from __future__ import annotations

import argparse
import hashlib
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import socket
import sys
import threading
import time
import uuid
from urllib.parse import urlparse
import webbrowser

from engine import DeckViewError, EXAMPLE_CODE, generate, load_metadata

SOURCE_ROOT = Path(__file__).resolve().parent
ROOT = SOURCE_ROOT
ASSETS = {"/": "web/index.html", "/index.html": "web/index.html",
          "/styles.css": "web/styles.css", "/tokens.css": "web/tokens.css",
          "/app.js": "web/app.js", "/icon.svg": "web/icon.svg",
          "/example.png": "example.png"}
APP_ID = "deckview-local"


def build_signature():
    if getattr(sys, "frozen", False):
        marker = SOURCE_ROOT / "build-id.txt"
        return marker.read_text(encoding="utf-8").strip() if marker.is_file() else "desktop-1.0"
    return ":".join(hashlib.sha256((SOURCE_ROOT / name).read_bytes()).hexdigest()
                    for name in ("server.py", "engine.py", "renderer.py", "blizzard_provider.py", "local_library.py", "card_library.py"))


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def server_bind(self):
        # SO_REUSEADDR on Windows lets multiple processes bind the same port,
        # which can send requests to an obsolete instance after an update.
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(self, address, engine=generate, *, generated_dir=None):
        super().__init__(address, Handler)
        self.jobs = {}
        self.lock = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="deck-render")
        self.engine = engine
        self.generated_dir = Path(generated_dir) if generated_dir is not None else ROOT / "generated"
        self.started_at = time.time()
        self.build = build_signature()

    def server_close(self):
        super().server_close()
        # TCPServer also closes a partially constructed instance if bind fails.
        executor = getattr(self, "executor", None)
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    def run_job(self, job_id, code, locale):
        def progress(message):
            with self.lock:
                self.jobs[job_id].update(status="running", message=str(message))

        try:
            progress("Подготавливаем колоду…")
            result = dict(self.engine(code, locale, progress))
            result.pop("path", None)
            result["imageUrl"] = "/generated/" + result["filename"]
            with self.lock:
                self.jobs[job_id].update(status="done", message="Изображение готово", result=result)
        except DeckViewError as error:
            with self.lock:
                self.jobs[job_id].update(status="error", message=str(error))
        except Exception as error:
            print(f"Generation failed: {type(error).__name__}: {error}", flush=True)
            with self.lock:
                self.jobs[job_id].update(status="error", message="Не удалось создать изображение. Повтори попытку; подробности в server.log.")


class Handler(BaseHTTPRequestHandler):
    server: LocalServer

    def log_message(self, *_args):
        pass

    def local_request(self):
        host = self.headers.get("Host", "")
        port = self.server.server_port
        if host not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            self.discard_small_body()
            self.send_json({"error": "Разрешены только запросы с этого компьютера."}, 403)
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}:
            self.discard_small_body()
            self.send_json({"error": "Запрос из другого сайта отклонён."}, 403)
            return False
        return True

    def discard_small_body(self):
        # Closing a Windows socket with unread POST data can reset the peer
        # before it receives the rejection message. Drain only bounded bodies.
        if self.command == "POST":
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return
            if 0 < size <= 65536:
                self.rfile.read(size)

    def common_headers(self, content_type, size):
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")

    def send_json(self, data, status=200):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.common_headers("application/json; charset=utf-8", len(payload))
        self.end_headers()
        try:
            self.wfile.write(payload)
            if status >= 400 and self.command == "POST":
                # Malformed Content-Length prevents a normal body drain. Send
                # the complete rejection before closing with unread TCP data;
                # otherwise Windows may replace it with a connection reset.
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_WR)
                self.connection.settimeout(.1)
                remaining = 65536
                while remaining:
                    chunk = self.rfile.read1(min(8192, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
        except OSError:
            pass

    def do_GET(self):
        if not self.local_request():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/health":
            with self.server.lock:
                active = sum(job["status"] in {"queued", "running"} for job in self.server.jobs.values())
            return self.send_json({"app": APP_ID, "version": "1.0", "status": "ok",
                                   "pid": os.getpid(), "build": self.server.build, "activeJobs": active})
        if path == "/api/example":
            return self.send_json({"code": EXAMPLE_CODE})
        if path.startswith("/api/jobs/"):
            job_id = path.removeprefix("/api/jobs/")
            with self.server.lock:
                job = dict(self.server.jobs.get(job_id, {}))
            if not job:
                return self.send_json({"error": "Задание не найдено. Создай изображение ещё раз."}, 404)
            return self.send_json(job)
        if path in ASSETS:
            file = ROOT / ASSETS[path]
        elif re.fullmatch(r"/generated/[A-Za-z0-9_-]+\.png", path):
            file = self.server.generated_dir / path.rsplit("/", 1)[1]
        else:
            return self.send_json({"error": "Страница не найдена."}, 404)
        if not file.is_file():
            return self.send_json({"error": "Файл не найден."}, 404)
        payload = file.read_bytes()
        content_type = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        if file.suffix in {".html", ".css", ".js", ".svg"}:
            content_type += "; charset=utf-8"
        self.send_response(200)
        self.common_headers(content_type, len(payload))
        if parsed.query == "download=1" and path.startswith("/generated/"):
            self.send_header("Content-Disposition", f'attachment; filename="{file.name}"')
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        if not self.local_request():
            return
        if self.path != "/api/generate":
            self.discard_small_body()
            return self.send_json({"error": "Неизвестное действие."}, 404)
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            self.discard_small_body()
            return self.send_json({"error": "Ожидается JSON."}, 415)
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 40960:
                self.discard_small_body()
                return self.send_json({"error": "Слишком длинный текст. Вставь код одной колоды."}, 413)
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError
            code, locale = body.get("code"), body.get("locale", "ruRU")
            if not isinstance(code, str) or not code.strip():
                return self.send_json({"error": "Вставь код колоды из Hearthstone."}, 400)
            if not isinstance(locale, str) or locale not in {"ruRU", "enUS"}:
                return self.send_json({"error": "Выбери русский или английский язык карт."}, 400)
        except (ValueError, TypeError, UnicodeError):
            return self.send_json({"error": "Не удалось прочитать запрос. Повтори попытку."}, 400)
        job_id = uuid.uuid4().hex
        with self.server.lock:
            active = sum(job["status"] in {"queued", "running"} for job in self.server.jobs.values())
            if active >= 2:
                return self.send_json({"error": "Уже создаётся изображение. Дождись завершения и попробуй снова."}, 429)
            finished = [key for key, job in self.server.jobs.items() if job["status"] in {"done", "error"}]
            for key in finished[:-20]:
                self.server.jobs.pop(key, None)
            self.server.jobs[job_id] = {"status": "queued", "message": "Подготавливаем колоду…"}
        self.server.executor.submit(self.server.run_job, job_id, code, locale)
        return self.send_json({"id": job_id}, 202)


def main():
    parser = argparse.ArgumentParser(description="Local Hearthstone deck images")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    server = None
    for port in range(args.port, args.port + 10):
        try:
            server = LocalServer(("127.0.0.1", port))
            break
        except OSError:
            continue
    if server is None:
        raise SystemExit("Не удалось открыть локальный порт. Освободи порт 8787–8796 и повтори запуск.")
    url = f"http://127.0.0.1:{server.server_port}"
    (ROOT / "server-url.txt").write_text(url, encoding="utf-8")
    print(f"Deck View Local: {url}", flush=True)
    def update_library():
        for locale in ("ruRU", "enUS"):
            try:
                load_metadata(locale)
            except DeckViewError as error:
                print(str(error), flush=True)
    threading.Thread(target=update_library, name="card-library-update", daemon=True).start()
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
