"""Temporarily expose one local video through a Cloudflare Quick Tunnel."""

from __future__ import annotations

import contextlib
import os
import queue
import re
import secrets
import shutil
import subprocess
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import requests


_QUICK_TUNNEL_URL = re.compile(
    r"https://[a-z0-9-]+\.trycloudflare\.com", re.IGNORECASE
)


def _single_video_handler(video_path: Path, public_path: str):
    class SingleVideoHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_HEAD(self) -> None:  # noqa: N802
            self._serve(include_body=False)

        def do_GET(self) -> None:  # noqa: N802
            self._serve(include_body=True)

        def _serve(self, *, include_body: bool) -> None:
            if urlsplit(self.path).path != public_path:
                self.send_error(404)
                return

            size = video_path.stat().st_size
            start, end, partial = self._parse_range(size)
            if start is None or end is None:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            length = end - start + 1
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(length))
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if not include_body:
                return

            with video_path.open("rb") as source:
                source.seek(start)
                remaining = length
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

        def _parse_range(self, size: int) -> tuple[int | None, int | None, bool]:
            value = self.headers.get("Range")
            if not value:
                return 0, size - 1, False
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
            if not match or size <= 0:
                return None, None, True

            first, last = match.groups()
            if not first and not last:
                return None, None, True
            if first:
                start = int(first)
                end = int(last) if last else size - 1
            else:
                suffix = int(last)
                if suffix <= 0:
                    return None, None, True
                start = max(0, size - suffix)
                end = size - 1
            if start >= size or start > end:
                return None, None, True
            return start, min(end, size - 1), True

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return SingleVideoHandler


class TemporaryPublicVideo:
    """Serve one file and expose it with a short-lived, random public URL."""

    STARTUP_TIMEOUT_SECONDS = 45
    READINESS_ATTEMPTS = 20

    def __init__(self, video_path: str):
        self.video_path = Path(video_path).resolve()
        self.public_path = f"/{secrets.token_urlsafe(24)}.mp4"
        self._server: ThreadingHTTPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._tunnel: subprocess.Popen[str] | None = None

    def __enter__(self) -> str:
        executable = shutil.which("cloudflared")
        if not executable:
            raise RuntimeError(
                "cloudflared is not installed; rebuild the application image"
            )
        if not self.video_path.is_file():
            raise FileNotFoundError(str(self.video_path))

        handler = _single_video_handler(self.video_path, self.public_path)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._server_thread = threading.Thread(
            target=self._server.serve_forever,
            name="instagram-video-server",
            daemon=True,
        )
        self._server_thread.start()
        port = self._server.server_address[1]

        self._tunnel = subprocess.Popen(
            [
                executable,
                "tunnel",
                "--no-autoupdate",
                "--url",
                f"http://127.0.0.1:{port}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env={**os.environ, "NO_AUTOUPDATE": "true"},
        )
        try:
            public_base = self._read_tunnel_url()
            public_url = f"{public_base}{self.public_path}"
            self._wait_until_public(public_url)
            return public_url
        except Exception:
            self.close()
            raise

    def _read_tunnel_url(self) -> str:
        if not self._tunnel or not self._tunnel.stdout:
            raise RuntimeError("Cloudflare tunnel did not start")

        lines: queue.Queue[str | None] = queue.Queue()

        def consume_output() -> None:
            assert self._tunnel is not None and self._tunnel.stdout is not None
            for line in self._tunnel.stdout:
                lines.put(line)
            lines.put(None)

        threading.Thread(target=consume_output, daemon=True).start()
        deadline = time.monotonic() + self.STARTUP_TIMEOUT_SECONDS
        recent: list[str] = []
        while time.monotonic() < deadline:
            if self._tunnel.poll() is not None and lines.empty():
                break
            try:
                line = lines.get(timeout=min(1, max(0.01, deadline - time.monotonic())))
            except queue.Empty:
                continue
            if line is None:
                break
            cleaned = line.strip()
            if cleaned:
                recent.append(cleaned[-300:])
                recent = recent[-3:]
            match = _QUICK_TUNNEL_URL.search(line)
            if match:
                return match.group(0).rstrip("/")
        detail = "; ".join(recent) or "no diagnostic output"
        raise RuntimeError(f"Cloudflare Quick Tunnel failed to start: {detail}")

    def _wait_until_public(self, url: str) -> None:
        for attempt in range(self.READINESS_ATTEMPTS):
            if self._tunnel and self._tunnel.poll() is not None:
                raise RuntimeError("Cloudflare Quick Tunnel stopped unexpectedly")
            try:
                response = requests.get(
                    url,
                    headers={"Range": "bytes=0-0"},
                    timeout=10,
                )
                if response.status_code == 206 and response.content:
                    return
            except requests.RequestException:
                pass
            if attempt + 1 < self.READINESS_ATTEMPTS:
                time.sleep(1)
        raise RuntimeError("the temporary public video URL did not become available")

    def close(self) -> None:
        if self._tunnel and self._tunnel.poll() is None:
            self._tunnel.terminate()
            try:
                self._tunnel.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._tunnel.kill()
                self._tunnel.wait(timeout=5)
        if self._tunnel and self._tunnel.stdout:
            self._tunnel.stdout.close()
        self._tunnel = None

        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._server_thread:
            self._server_thread.join(timeout=5)
        self._server = None
        self._server_thread = None

    def __exit__(self, *_exc: object) -> None:
        self.close()


@contextlib.contextmanager
def temporary_public_video(video_path: str) -> Iterator[str]:
    with TemporaryPublicVideo(video_path) as public_url:
        yield public_url
