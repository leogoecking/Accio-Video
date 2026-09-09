import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

import requests

from app.services.public_media import _single_video_handler


class TestSingleVideoHandler(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.video = Path(self.temp_dir.name) / "video.mp4"
        self.video.write_bytes(b"0123456789")
        handler = _single_video_handler(self.video, "/random.mp4")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp_dir.cleanup()

    def test_serves_only_configured_video_path_with_range_support(self):
        response = requests.get(
            f"{self.base_url}/random.mp4",
            headers={"Range": "bytes=2-5"},
            timeout=5,
        )

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b"2345")
        self.assertEqual(response.headers["Content-Range"], "bytes 2-5/10")
        self.assertEqual(response.headers["Accept-Ranges"], "bytes")

        hidden = requests.get(f"{self.base_url}/video.mp4", timeout=5)
        self.assertEqual(hidden.status_code, 404)

    def test_rejects_invalid_or_out_of_bounds_range(self):
        response = requests.get(
            f"{self.base_url}/random.mp4",
            headers={"Range": "bytes=20-30"},
            timeout=5,
        )

        self.assertEqual(response.status_code, 416)
        self.assertEqual(response.headers["Content-Range"], "bytes */10")


if __name__ == "__main__":
    unittest.main()
