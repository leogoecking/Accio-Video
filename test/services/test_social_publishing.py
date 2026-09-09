import unittest
from unittest.mock import PropertyMock, patch

from app.services import social_publishing


class TestSocialPublishing(unittest.TestCase):
    @patch("app.services.social_publishing.config.app")
    def test_configured_platforms_normalizes_and_deduplicates(self, config_app):
        config_app.get.return_value = ["YouTube", "instagram", "youtube", "unknown"]

        self.assertEqual(
            social_publishing.configured_platforms(),
            ["youtube", "instagram"],
        )

    @patch.object(
        type(social_publishing.youtube_publisher),
        "enabled",
        new_callable=PropertyMock,
        return_value=True,
    )
    @patch.object(social_publishing.youtube_publisher, "upload_video")
    def test_direct_youtube_bypasses_upload_post(self, youtube_upload, _enabled):
        youtube_upload.return_value = {"success": True, "video_id": "video-1"}
        metadata = {
            "title": "Title",
            "caption": "Description",
            "hashtags": ["#shorts"],
        }

        with patch.object(social_publishing.upload_post, "cross_post_video") as fallback:
            result = social_publishing.publish_video(
                platform="youtube",
                video_path="video.mp4",
                metadata=metadata,
                youtube_privacy_status="private",
            )

        self.assertTrue(result["success"])
        youtube_upload.assert_called_once_with(
            "video.mp4",
            title="Title",
            description="Description",
            tags=["#shorts"],
            privacy_status="private",
            contains_synthetic_media=True,
        )
        fallback.assert_not_called()

    @patch.object(
        type(social_publishing.instagram_publisher),
        "enabled",
        new_callable=PropertyMock,
        return_value=False,
    )
    @patch.object(social_publishing.upload_post, "cross_post_video")
    def test_disabled_direct_provider_uses_upload_post(self, fallback, _enabled):
        fallback.return_value = {"success": True, "request_id": "request-1"}

        result = social_publishing.publish_video(
            platform="instagram",
            video_path="video.mp4",
            metadata={"title": "T", "caption": "C", "hashtags": []},
            youtube_privacy_status="private",
        )

        self.assertEqual(result["platform"], "instagram")
        self.assertEqual(result["provider"], "upload_post")
        self.assertEqual(result["status"], "accepted")
        fallback.assert_called_once_with(
            video_path="video.mp4",
            title="C",
            platforms=["instagram"],
            youtube_extra=None,
        )

    @patch.object(
        type(social_publishing.instagram_publisher),
        "enabled",
        new_callable=PropertyMock,
        return_value=True,
    )
    @patch.object(social_publishing.instagram_publisher, "upload_video")
    def test_direct_instagram_bypasses_upload_post(self, instagram_upload, _enabled):
        instagram_upload.return_value = {"success": True, "media_id": "media-1"}

        with patch.object(social_publishing.upload_post, "cross_post_video") as fallback:
            result = social_publishing.publish_video(
                platform="instagram",
                video_path="video.mp4",
                metadata={"title": "T", "caption": "Caption", "hashtags": []},
                youtube_privacy_status="private",
            )

        self.assertTrue(result["success"])
        instagram_upload.assert_called_once_with("video.mp4", caption="Caption")
        fallback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
