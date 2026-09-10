import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import MagicMock, mock_open, patch

import requests

from app.services.oauth_state import OAuthStateStore
from app.services.tiktok_publisher import TikTokPublisher


class TestTikTokPublisher(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.token_path = self.temp_path / "tiktok-token.json"
        self.state_path = self.temp_path / "tiktok-oauth-state.json"
        self.config = {
            "tiktok_direct_enabled": True,
            "tiktok_direct_client_key": "client-key",
            "tiktok_direct_client_secret": "client-secret",
            "tiktok_direct_redirect_uri": "https://localhost:8501/",
            "tiktok_direct_test_mode": True,
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def _path_patches(self):
        token_path = self.token_path
        state_path = self.state_path
        return (
            patch.object(
                TikTokPublisher,
                "token_path",
                new_callable=lambda: property(lambda _self: token_path),
            ),
            patch.object(
                OAuthStateStore,
                "path",
                new_callable=lambda: property(lambda _self: state_path),
            ),
        )

    @patch("app.services.tiktok_publisher.config.app")
    def test_authorization_url_uses_pkce_and_required_scopes(self, config_app):
        config_app.get.side_effect = self.config.get
        service = TikTokPublisher()
        token_patch, state_patch = self._path_patches()

        with token_patch, state_patch:
            state = service.begin_authorization()
            verifier = OAuthStateStore("tiktok").peek(state)["code_verifier"]
            url = service.build_authorization_url(state)

        params = parse_qs(urlparse(url).query)
        self.assertEqual(params["state"], [state])
        self.assertEqual(params["scope"], ["user.info.basic,video.publish"])
        self.assertEqual(params["code_challenge_method"], ["S256"])
        self.assertEqual(
            params["code_challenge"],
            [hashlib.sha256(verifier.encode("ascii")).hexdigest()],
        )

    @patch("app.services.tiktok_publisher.config.app")
    def test_authorization_rejects_non_https_redirect(self, config_app):
        invalid_config = {
            **self.config,
            "tiktok_direct_redirect_uri": "http://localhost:8501/",
        }
        config_app.get.side_effect = invalid_config.get
        service = TikTokPublisher()
        token_patch, state_patch = self._path_patches()

        with token_patch, state_patch:
            state = service.begin_authorization()
            with self.assertRaisesRegex(ValueError, "exact HTTPS URL"):
                service.build_authorization_url(state)

    @patch("app.services.tiktok_publisher.requests.post")
    @patch("app.services.tiktok_publisher.config.app")
    def test_exchange_code_uses_verifier_and_saves_restricted_token(
        self, config_app, post
    ):
        config_app.get.side_effect = self.config.get
        response = MagicMock(ok=True, status_code=200)
        response.json.return_value = {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_in": 86400,
            "refresh_expires_in": 31536000,
            "open_id": "creator-id",
            "scope": "user.info.basic,video.publish",
            "token_type": "Bearer",
        }
        post.return_value = response
        service = TikTokPublisher()
        token_patch, state_patch = self._path_patches()

        with token_patch, state_patch:
            state = service.begin_authorization()
            verifier = OAuthStateStore("tiktok").peek(state)["code_verifier"]
            self.assertTrue(service.consume_authorization_state(state))
            result = service.exchange_code("authorization-code")

        self.assertTrue(result["success"])
        self.assertEqual(post.call_args.kwargs["data"]["code_verifier"], verifier)
        saved = json.loads(self.token_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["refresh_token"], "refresh-token")
        self.assertEqual(os.stat(self.token_path).st_mode & 0o777, 0o600)

    @patch("app.services.tiktok_publisher.requests.post")
    @patch("app.services.tiktok_publisher.config.app")
    def test_exchange_rejects_token_without_required_publish_scope(
        self, config_app, post
    ):
        config_app.get.side_effect = self.config.get
        response = MagicMock(ok=True, status_code=200)
        response.json.return_value = {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "scope": "user.info.basic",
        }
        post.return_value = response
        service = TikTokPublisher()
        token_patch, state_patch = self._path_patches()

        with token_patch, state_patch:
            state = service.begin_authorization()
            self.assertTrue(service.consume_authorization_state(state))
            with self.assertRaisesRegex(RuntimeError, "video.publish"):
                service.exchange_code("authorization-code")

        self.assertFalse(self.token_path.exists())

    def test_authorized_requires_tokens_and_all_required_scopes(self):
        service = TikTokPublisher()
        token_patch, _state_patch = self._path_patches()
        self.token_path.write_text(
            json.dumps(
                {
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                    "scope": "user.info.basic",
                }
            ),
            encoding="utf-8",
        )

        with token_patch:
            self.assertFalse(service.is_authorized())

    @patch("app.services.tiktok_publisher.requests.put")
    @patch("app.services.tiktok_publisher.requests.post")
    @patch("app.services.tiktok_publisher.os.path.isfile", return_value=True)
    @patch("app.services.tiktok_publisher.os.path.getsize", return_value=4)
    @patch("builtins.open", mock_open(read_data=b"data"))
    @patch("app.services.tiktok_publisher.config.app")
    def test_private_publish_uploads_file_and_reports_completion(
        self, config_app, _getsize, _isfile, post, put
    ):
        config_app.get.side_effect = self.config.get
        creator = MagicMock(ok=True, status_code=200)
        creator.json.return_value = {
            "data": {
                "creator_nickname": "Creator",
                "privacy_level_options": ["SELF_ONLY"],
                "comment_disabled": False,
                "duet_disabled": False,
                "stitch_disabled": False,
                "max_video_post_duration_sec": 180,
            },
            "error": {"code": "ok"},
        }
        initialized = MagicMock(ok=True, status_code=200)
        initialized.json.return_value = {
            "data": {
                "publish_id": "publish-123",
                "upload_url": "https://open-upload.tiktokapis.com/upload/?id=secret",
            },
            "error": {"code": "ok"},
        }
        status = MagicMock(ok=True, status_code=200)
        status.json.return_value = {
            "data": {"status": "PUBLISH_COMPLETE"},
            "error": {"code": "ok"},
        }
        post.side_effect = [creator, initialized, status]
        put.return_value = MagicMock(status_code=201)
        service = TikTokPublisher()

        with patch.object(service, "_get_access_token", return_value="access-token"):
            result = service.publish_video(
                "/video.mp4",
                title="Title #video",
                privacy_level="SELF_ONLY",
                allow_comment=True,
                duration_seconds=30,
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["publish_id"], "publish-123")
        post_body = post.call_args_list[1].kwargs["json"]
        self.assertEqual(post_body["post_info"]["privacy_level"], "SELF_ONLY")
        self.assertFalse(post_body["post_info"]["disable_comment"])
        self.assertTrue(post_body["post_info"]["is_aigc"])
        self.assertEqual(put.call_args.kwargs["headers"]["Content-Range"], "bytes 0-3/4")

    @patch("app.services.tiktok_publisher.requests.put")
    @patch("app.services.tiktok_publisher.requests.post")
    @patch("app.services.tiktok_publisher.os.path.isfile", return_value=True)
    @patch("app.services.tiktok_publisher.os.path.getsize", return_value=4)
    @patch("builtins.open", mock_open(read_data=b"data"))
    @patch("app.services.tiktok_publisher.config.app")
    def test_status_failure_after_upload_preserves_publish_id_for_recovery(
        self, config_app, _getsize, _isfile, post, put
    ):
        config_app.get.side_effect = self.config.get
        creator = MagicMock(ok=True, status_code=200)
        creator.json.return_value = {
            "data": {
                "privacy_level_options": ["SELF_ONLY"],
                "max_video_post_duration_sec": 180,
            },
            "error": {"code": "ok"},
        }
        initialized = MagicMock(ok=True, status_code=200)
        initialized.json.return_value = {
            "data": {
                "publish_id": "publish-recoverable",
                "upload_url": "https://open-upload.tiktokapis.com/upload/?id=secret",
            },
            "error": {"code": "ok"},
        }
        status_error = MagicMock(ok=False, status_code=503)
        status_error.json.return_value = {
            "error": {"code": "internal_error", "message": "try again"}
        }
        post.side_effect = [creator, initialized, status_error]
        put.return_value = MagicMock(status_code=201)
        service = TikTokPublisher()

        with patch.object(service, "_get_access_token", return_value="access-token"):
            result = service.publish_video(
                "/video.mp4",
                title="Title",
                privacy_level="SELF_ONLY",
                duration_seconds=30,
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "processing")
        self.assertEqual(result["publish_id"], "publish-recoverable")
        self.assertEqual(result["processing_status"], "PROCESSING_UPLOAD")

    def test_files_above_64_mb_are_split_into_multiple_valid_chunks(self):
        mib = 1024 * 1024
        for file_size in (64 * mib + 1, 100 * mib, 128 * mib - 1, 4 * 1024 * mib):
            with self.subTest(file_size=file_size):
                chunk_size, chunk_count = TikTokPublisher._chunk_plan(file_size)
                final_size = file_size - (chunk_count - 1) * chunk_size
                self.assertGreaterEqual(chunk_count, 2)
                self.assertLessEqual(chunk_size, TikTokPublisher.MAX_CHUNK_SIZE)
                self.assertGreaterEqual(chunk_size, TikTokPublisher.MIN_CHUNK_SIZE)
                self.assertLessEqual(final_size, TikTokPublisher.MAX_FINAL_CHUNK_SIZE)

    @patch("app.services.tiktok_publisher.time.sleep")
    @patch("app.services.tiktok_publisher.requests.put")
    @patch("builtins.open", mock_open(read_data=b"data"))
    def test_upload_retries_temporary_server_errors(self, put, sleep):
        put.side_effect = [
            MagicMock(status_code=500),
            MagicMock(status_code=503),
            MagicMock(status_code=201),
        ]
        service = TikTokPublisher()

        service._upload_file(
            "/video.mp4",
            "https://open-upload.tiktokapis.com/upload/?id=secret",
            4,
            4,
            1,
        )

        self.assertEqual(put.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])

    @patch("app.services.tiktok_publisher.time.sleep")
    @patch("app.services.tiktok_publisher.requests.put")
    @patch("builtins.open", mock_open(read_data=b"data"))
    def test_upload_retries_temporary_network_errors(self, put, sleep):
        put.side_effect = [
            requests.ConnectionError("temporary failure"),
            MagicMock(status_code=201),
        ]
        service = TikTokPublisher()

        service._upload_file(
            "/video.mp4",
            "https://open-upload.tiktokapis.com/upload/?id=secret",
            4,
            4,
            1,
        )

        self.assertEqual(put.call_count, 2)
        sleep.assert_called_once_with(1)

    @patch("app.services.tiktok_publisher.os.path.isfile", return_value=True)
    @patch("app.services.tiktok_publisher.os.path.getsize", return_value=4)
    @patch("app.services.tiktok_publisher.config.app")
    def test_network_failure_after_initialization_preserves_publish_id(
        self, config_app, _getsize, _isfile
    ):
        config_app.get.side_effect = self.config.get
        service = TikTokPublisher()
        creator = {
            "privacy_level_options": ["SELF_ONLY"],
            "max_video_post_duration_sec": 180,
        }
        initialized = {
            "publish_id": "publish-recoverable",
            "upload_url": "https://open-upload.tiktokapis.com/upload/?id=secret",
        }

        with (
            patch.object(service, "_get_access_token", return_value="access-token"),
            patch.object(service, "_api_post", side_effect=[creator, initialized]),
            patch.object(
                service,
                "_upload_file",
                side_effect=requests.ConnectionError("temporary failure"),
            ),
        ):
            result = service.publish_video(
                "/video.mp4",
                title="Title",
                privacy_level="SELF_ONLY",
                duration_seconds=30,
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["publish_id"], "publish-recoverable")
        self.assertEqual(result["processing_status"], "UPLOAD_FAILED")
        self.assertEqual(result["error"], "TikTok publishing failed (ConnectionError)")

    @patch("app.services.tiktok_publisher.requests.post")
    @patch("app.services.tiktok_publisher.os.path.isfile", return_value=True)
    @patch("app.services.tiktok_publisher.os.path.getsize", return_value=4)
    @patch("app.services.tiktok_publisher.config.app")
    def test_test_mode_rejects_non_private_post(
        self, config_app, _getsize, _isfile, post
    ):
        config_app.get.side_effect = self.config.get
        creator = MagicMock(ok=True, status_code=200)
        creator.json.return_value = {
            "data": {"privacy_level_options": ["SELF_ONLY", "PUBLIC_TO_EVERYONE"]},
            "error": {"code": "ok"},
        }
        post.return_value = creator
        service = TikTokPublisher()

        with patch.object(service, "_get_access_token", return_value="access-token"):
            result = service.publish_video(
                "/video.mp4",
                title="Title",
                privacy_level="PUBLIC_TO_EVERYONE",
                duration_seconds=30,
            )

        self.assertFalse(result["success"])
        self.assertIn("SELF_ONLY", result["error"])
        self.assertEqual(post.call_count, 1)

    @patch("app.services.tiktok_publisher.logger.error")
    @patch("app.services.tiktok_publisher.time.sleep")
    @patch("app.services.tiktok_publisher.requests.put")
    @patch("app.services.tiktok_publisher.requests.post")
    @patch("app.services.tiktok_publisher.os.path.isfile", return_value=True)
    @patch("app.services.tiktok_publisher.os.path.getsize", return_value=4)
    @patch("builtins.open", mock_open(read_data=b"data"))
    @patch("app.services.tiktok_publisher.config.app")
    def test_upload_failure_does_not_expose_temporary_upload_url(
        self, config_app, _getsize, _isfile, post, put, _sleep, error_log
    ):
        config_app.get.side_effect = self.config.get
        secret = "sensitive-upload-id"
        creator = MagicMock(ok=True, status_code=200)
        creator.json.return_value = {
            "data": {
                "privacy_level_options": ["SELF_ONLY"],
                "max_video_post_duration_sec": 180,
            },
            "error": {"code": "ok"},
        }
        initialized = MagicMock(ok=True, status_code=200)
        initialized.json.return_value = {
            "data": {
                "publish_id": "publish-123",
                "upload_url": f"https://open-upload.tiktokapis.com/upload/?id={secret}",
            },
            "error": {"code": "ok"},
        }
        post.side_effect = [creator, initialized]
        put.return_value = MagicMock(status_code=500)
        service = TikTokPublisher()

        with patch.object(service, "_get_access_token", return_value="access-token"):
            result = service.publish_video(
                "/video.mp4",
                title="Title",
                privacy_level="SELF_ONLY",
                duration_seconds=30,
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "TikTok video transfer failed (HTTP 500)")
        self.assertEqual(result["publish_id"], "publish-123")
        self.assertEqual(result["processing_status"], "UPLOAD_FAILED")
        self.assertEqual(put.call_count, service.UPLOAD_RETRY_ATTEMPTS)
        self.assertNotIn(secret, result["error"])
        self.assertNotIn(secret, error_log.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
