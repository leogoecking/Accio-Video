import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

import requests

from app.services.youtube_publisher import YouTubePublisher


class TestYouTubePublisher(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.token_path = Path(self.temp_dir.name) / "youtube-token.json"
        self.config = {
            "youtube_direct_enabled": True,
            "youtube_direct_client_id": "client-id",
            "youtube_direct_client_secret": "client-secret",
            "youtube_direct_redirect_uri": "http://localhost:8501",
            "youtube_direct_privacy_status": "private",
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def _service(self):
        service = YouTubePublisher()
        token_path = self.token_path
        patcher = patch.object(
            YouTubePublisher,
            "token_path",
            new_callable=lambda: property(lambda _self: token_path),
        )
        return service, patcher

    @patch("app.services.youtube_publisher.config.app")
    def test_authorization_url_uses_offline_access_and_state(self, config_app):
        config_app.get.side_effect = self.config.get
        service = YouTubePublisher()

        url = service.build_authorization_url("csrf-state")

        self.assertIn("access_type=offline", url)
        self.assertIn("state=csrf-state", url)
        self.assertIn("youtube.upload", url)

    @patch("app.services.youtube_publisher.config.app")
    @patch("app.services.youtube_publisher.requests.post")
    def test_exchange_code_saves_restricted_token(self, post, config_app):
        config_app.get.side_effect = self.config.get
        response = MagicMock()
        response.json.return_value = {
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_in": 3600,
            "token_type": "Bearer",
        }
        post.return_value = response
        service, token_path_patch = self._service()

        with token_path_patch:
            result = service.exchange_code("authorization-code")

        self.assertTrue(result["success"])
        saved = json.loads(self.token_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["refresh_token"], "refresh")
        self.assertEqual(os.stat(self.token_path).st_mode & 0o777, 0o600)

    @patch("app.services.youtube_publisher.config.app")
    @patch("app.services.youtube_publisher.os.path.isfile", return_value=True)
    @patch("app.services.youtube_publisher.os.path.getsize", return_value=4)
    @patch("builtins.open", mock_open(read_data=b"data"))
    @patch("app.services.youtube_publisher.requests.put")
    @patch("app.services.youtube_publisher.requests.post")
    def test_resumable_upload_returns_platform_url(
        self,
        post,
        put,
        _getsize,
        _isfile,
        config_app,
    ):
        config_app.get.side_effect = self.config.get
        session_response = MagicMock()
        session_response.headers = {"Location": "https://upload.example/session"}
        post.return_value = session_response
        upload_response = MagicMock()
        upload_response.json.return_value = {
            "id": "video-123",
            "status": {"privacyStatus": "private"},
        }
        put.return_value = upload_response
        service = YouTubePublisher()

        with patch.object(service, "_get_access_token", return_value="token"):
            result = service.upload_video(
                "/video.mp4",
                title="A title",
                description="Description",
                tags=["#shorts", "shorts", "news"],
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["platform"], "youtube")
        self.assertEqual(result["provider"], "youtube_direct")
        self.assertEqual(result["post_url"], "https://www.youtube.com/watch?v=video-123")
        request_body = post.call_args.kwargs["json"]
        self.assertEqual(request_body["snippet"]["tags"], ["shorts", "news"])
        self.assertTrue(request_body["status"]["containsSyntheticMedia"])
        self.assertEqual(put.call_args.args[0], "https://upload.example/session")

    @patch("app.services.youtube_publisher.config.app")
    @patch("app.services.youtube_publisher.os.path.isfile", return_value=True)
    @patch("app.services.youtube_publisher.requests.post")
    def test_network_failure_is_returned_without_raising(
        self, post, _isfile, config_app
    ):
        config_app.get.side_effect = self.config.get
        service = YouTubePublisher()
        post.side_effect = requests.Timeout("offline")

        with patch.object(service, "_load_token", return_value={"refresh_token": "r"}):
            result = service.upload_video("/video.mp4", title="Title")

        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "YouTube upload failed (Timeout)")

    @patch("app.services.youtube_publisher.logger.warning")
    @patch("app.services.youtube_publisher.config.app")
    @patch("app.services.youtube_publisher.requests.post")
    def test_disconnect_does_not_put_token_in_url_or_log(
        self, post, config_app, warning
    ):
        config_app.get.side_effect = self.config.get
        secret = "sensitive-refresh-token"
        response = MagicMock()
        error = requests.HTTPError(
            f"400 Client Error for url: https://oauth2.googleapis.com/revoke?token={secret}"
        )
        error.response = MagicMock(status_code=400)
        response.raise_for_status.side_effect = error
        post.return_value = response
        service, token_path_patch = self._service()

        with token_path_patch, patch.object(
            service, "_load_token", return_value={"refresh_token": secret}
        ):
            service.disconnect()

        self.assertEqual(post.call_args.kwargs["data"], {"token": secret})
        self.assertNotIn("params", post.call_args.kwargs)
        self.assertNotIn(secret, warning.call_args.args[0])
        self.assertEqual(
            warning.call_args.args[0], "YouTube token revocation failed (HTTP 400)"
        )

    @patch("app.services.youtube_publisher.logger.error")
    @patch("app.services.youtube_publisher.config.app")
    @patch("app.services.youtube_publisher.os.path.isfile", return_value=True)
    @patch("app.services.youtube_publisher.os.path.getsize", return_value=4)
    @patch("builtins.open", mock_open(read_data=b"data"))
    @patch("app.services.youtube_publisher.requests.put")
    @patch("app.services.youtube_publisher.requests.post")
    def test_upload_failure_does_not_expose_resumable_session_url(
        self,
        post,
        put,
        _getsize,
        _isfile,
        config_app,
        error_log,
    ):
        config_app.get.side_effect = self.config.get
        secret = "sensitive-upload-session"
        upload_url = f"https://upload.example/session?upload_id={secret}"
        session_response = MagicMock()
        session_response.headers = {"Location": upload_url}
        post.return_value = session_response
        upload_response = MagicMock()
        error = requests.HTTPError(f"500 Server Error for url: {upload_url}")
        error.response = MagicMock(status_code=500)
        upload_response.raise_for_status.side_effect = error
        put.return_value = upload_response
        service = YouTubePublisher()

        with patch.object(service, "_get_access_token", return_value="token"):
            result = service.upload_video("/video.mp4", title="Title")

        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "YouTube upload failed (HTTP 500)")
        self.assertNotIn(secret, result["error"])
        self.assertNotIn(secret, error_log.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
