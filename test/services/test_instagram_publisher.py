import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

from app.services.instagram_publisher import InstagramPublisher


class TestInstagramPublisher(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.token_path = Path(self.temp_dir.name) / "instagram-token.json"
        self.config = {
            "instagram_direct_enabled": True,
            "instagram_direct_app_id": "instagram-app-id",
            "instagram_direct_app_secret": "instagram-app-secret",
            "instagram_direct_redirect_uri": "http://localhost:8501/",
            "instagram_direct_api_version": "v26.0",
            "instagram_direct_share_to_feed": True,
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def _token_path_patch(self):
        token_path = self.token_path
        return patch.object(
            InstagramPublisher,
            "token_path",
            new_callable=lambda: property(lambda _self: token_path),
        )

    @patch("app.services.instagram_publisher.config.app")
    def test_authorization_url_uses_instagram_login_permissions(self, config_app):
        config_app.get.side_effect = self.config.get
        url = InstagramPublisher().build_authorization_url("csrf-state")
        parsed = urlparse(url)
        query = parse_qs(parsed.query)

        self.assertEqual(parsed.netloc, "www.instagram.com")
        self.assertEqual(parsed.path, "/oauth/authorize")
        self.assertIn("instagram_business_basic", query["scope"][0])
        self.assertIn("instagram_business_content_publish", query["scope"][0])
        self.assertNotIn("pages_show_list", query["scope"][0])
        self.assertEqual(query["enable_fb_login"], ["false"])
        self.assertEqual(query["force_reauth"], ["true"])
        self.assertEqual(query["state"], ["csrf-state"])

    @patch("app.services.instagram_publisher.config.app")
    @patch("app.services.instagram_publisher.requests.get")
    @patch("app.services.instagram_publisher.requests.post")
    def test_exchange_code_saves_instagram_user_token(self, post, get, config_app):
        config_app.get.side_effect = self.config.get
        short = MagicMock(ok=True)
        short.json.return_value = {
            "data": [
                {
                    "access_token": "short-token",
                    "user_id": "ig-123",
                    "permissions": ["instagram_business_basic"],
                }
            ]
        }
        long = MagicMock(ok=True)
        long.json.return_value = {
            "access_token": "long-token",
            "expires_in": 5000,
        }
        profile = MagicMock(ok=True)
        profile.json.return_value = {
            "user_id": "ig-123",
            "username": "my_business",
            "account_type": "BUSINESS",
        }
        post.return_value = short
        get.side_effect = [long, profile]

        with self._token_path_patch():
            result = InstagramPublisher().exchange_code("authorization-code")

        self.assertTrue(result["success"])
        self.assertEqual(result["username"], "my_business")
        saved = json.loads(self.token_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["ig_user_id"], "ig-123")
        self.assertEqual(saved["access_token"], "long-token")
        self.assertEqual(os.stat(self.token_path).st_mode & 0o777, 0o600)
        self.assertEqual(post.call_args.kwargs["data"]["grant_type"], "authorization_code")
        self.assertNotIn("params", post.call_args.kwargs)
        self.assertEqual(get.call_args_list[0].kwargs["params"]["grant_type"], "ig_exchange_token")

    @patch("app.services.instagram_publisher.config.app")
    @patch("app.services.instagram_publisher.temporary_public_video")
    @patch("app.services.instagram_publisher.requests.get")
    @patch("app.services.instagram_publisher.requests.post")
    def test_upload_keeps_public_url_available_until_reel_is_published(
        self,
        post,
        get,
        public_video,
        config_app,
    ):
        config_app.get.side_effect = self.config.get
        public_video.return_value.__enter__.return_value = (
            "https://random.trycloudflare.com/random.mp4"
        )
        container = MagicMock(ok=True)
        container.json.return_value = {"id": "container-1"}
        published = MagicMock(ok=True)
        published.json.return_value = {"id": "media-1"}
        post.side_effect = [container, published]
        ready = MagicMock(ok=True)
        ready.json.return_value = {"status_code": "FINISHED"}
        permalink = MagicMock(ok=True)
        permalink.json.return_value = {
            "permalink": "https://www.instagram.com/reel/example/"
        }
        get.side_effect = [ready, permalink]
        service = InstagramPublisher()
        token = {
            "access_token": "instagram-token",
            "ig_user_id": "ig-123",
            "expires_at": 99999999999,
        }

        with patch("app.services.instagram_publisher.os.path.isfile", return_value=True):
            with patch.object(service, "_get_valid_token", return_value=token):
                result = service.upload_video("/video.mp4", caption="Caption")

        self.assertTrue(result["success"])
        self.assertEqual(result["provider"], "instagram_direct")
        self.assertEqual(result["post_url"], "https://www.instagram.com/reel/example/")
        create_data = post.call_args_list[0].kwargs["data"]
        self.assertEqual(create_data["media_type"], "REELS")
        self.assertEqual(
            create_data["video_url"],
            "https://random.trycloudflare.com/random.mp4",
        )
        self.assertEqual(create_data["is_ai_generated"], "true")
        self.assertNotIn("upload_type", create_data)
        self.assertEqual(
            post.call_args_list[0].kwargs["headers"]["Authorization"],
            "Bearer instagram-token",
        )
        public_video.return_value.__exit__.assert_called_once()

    @patch("app.services.instagram_publisher.config.app")
    @patch("app.services.instagram_publisher.requests.get")
    def test_expiring_token_is_refreshed(self, get, config_app):
        config_app.get.side_effect = self.config.get
        response = MagicMock(ok=True)
        response.json.return_value = {
            "access_token": "refreshed-token",
            "expires_in": 5000,
        }
        get.return_value = response
        service = InstagramPublisher()
        token = {
            "access_token": "old-token",
            "ig_user_id": "ig-123",
            "issued_at": 1,
            "expires_at": 1,
        }

        with patch.object(service, "_load_token", return_value=token):
            with patch.object(service, "_save_token") as save:
                refreshed = service._get_valid_token()

        self.assertEqual(refreshed["access_token"], "refreshed-token")
        self.assertEqual(
            get.call_args.kwargs["params"]["grant_type"], "ig_refresh_token"
        )
        save.assert_called_once()

    @patch("app.services.instagram_publisher.config.app")
    def test_processing_error_prevents_publish(self, config_app):
        config_app.get.side_effect = self.config.get
        service = InstagramPublisher()
        response = MagicMock(ok=True)
        response.json.return_value = {
            "status_code": "ERROR",
            "status": "Unsupported video",
        }

        with patch("app.services.instagram_publisher.requests.get", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "Unsupported video"):
                service._wait_until_ready("container", "token")

    def test_meta_error_does_not_include_request_url_or_credentials(self):
        response = MagicMock(ok=False)
        response.status_code = 400
        response.json.return_value = {
            "error": {
                "message": "Invalid verification code format.",
                "code": 100,
                "error_subcode": 36008,
            }
        }
        response.url = "https://example.test/?client_secret=must-not-leak"

        with self.assertRaises(RuntimeError) as raised:
            InstagramPublisher._raise_meta_error(response, "Token exchange")

        error = str(raised.exception)
        self.assertIn("Invalid verification code format", error)
        self.assertIn("subcode 36008", error)
        self.assertNotIn("must-not-leak", error)


if __name__ == "__main__":
    unittest.main()
