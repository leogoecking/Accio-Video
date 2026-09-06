import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect
from app.services import ai_image, material


class TestAiImageService(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_generate_ai_image_empty_prompt(self):
        result = ai_image.generate_ai_image("   ")
        self.assertIsNone(result)

    def test_generate_ai_image_success(self):
        fake_content = b"fake_image_bytes_" * 100
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = fake_content

        with patch("requests.get", return_value=mock_response):
            path = ai_image.generate_ai_image(
                prompt="test futuristic city",
                video_aspect=VideoAspect.portrait,
                save_dir=self.temp_dir.name,
            )

        self.assertIsNotNone(path)
        self.assertTrue(os.path.exists(path))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), fake_content)

    def test_generate_ai_image_reclaims_cache(self):
        fake_content = b"cached_bytes_" * 100
        # pre-create file
        prompt = "reused city"
        aspect = VideoAspect.portrait
        w, h = aspect.to_resolution()
        import hashlib
        hsh = hashlib.md5(f"{prompt}_{w}_{h}_pollinations".encode("utf-8")).hexdigest()
        cached_file = os.path.join(self.temp_dir.name, f"ai-img-{hsh}.jpg")
        with open(cached_file, "wb") as f:
            f.write(fake_content)

        with patch("requests.get") as mock_get:
            path = ai_image.generate_ai_image(
                prompt=prompt,
                video_aspect=aspect,
                save_dir=self.temp_dir.name,
            )
            mock_get.assert_not_called()

        self.assertEqual(path, cached_file)

    def test_render_image_to_ken_burns_clip_invalid_image(self):
        result = ai_image.render_image_to_ken_burns_clip(
            image_path="/non/existent/image.jpg",
            output_path="/tmp/out.mp4",
            duration=4.0,
        )
        self.assertIsNone(result)

    def test_render_image_to_ken_burns_clip_mock_ffmpeg(self):
        img_path = os.path.join(self.temp_dir.name, "img.jpg")
        with open(img_path, "wb") as f:
            f.write(b"content")

        out_path = os.path.join(self.temp_dir.name, "out.mp4")

        def fake_run(cmd, **kwargs):
            with open(out_path, "wb") as f:
                f.write(b"fake_video")
            res = MagicMock()
            res.returncode = 0
            return res

        with patch("subprocess.run", side_effect=fake_run):
            result = ai_image.render_image_to_ken_burns_clip(
                image_path=img_path,
                output_path=out_path,
                duration=3.0,
                video_aspect=VideoAspect.portrait,
                motion="zoom_in",
            )

        self.assertEqual(result, out_path)
        self.assertTrue(os.path.exists(out_path))

    def test_generate_ai_video_clip_creates_material_info(self):
        with (
            patch("app.services.ai_image.generate_ai_image", return_value="/tmp/test.jpg"),
            patch("app.services.ai_image.render_image_to_ken_burns_clip", return_value="/tmp/test_clip.mp4"),
        ):
            item = ai_image.generate_ai_video_clip(
                prompt="alien planet landscape",
                duration=4.0,
                video_aspect=VideoAspect.portrait,
                save_dir=self.temp_dir.name,
            )

        self.assertIsNotNone(item)
        self.assertEqual(item.provider, "ai_image")
        self.assertEqual(item.url, "/tmp/test_clip.mp4")
        self.assertEqual(item.duration, 4.0)
        self.assertEqual(item.source_info["rendition"]["width"], 1080)
        self.assertEqual(item.source_info["rendition"]["height"], 1920)

    def test_download_videos_triggers_ai_fallback_on_zero_stock_results(self):
        """当搜索无库存视频时，自动触发 AI 图像生成与 Ken Burns 动效。"""
        ai_item = MaterialInfo()
        ai_item.provider = "ai_image"
        ai_item.url = "/tmp/ai_generated_clip.mp4"
        ai_item.duration = 5.0
        ai_item.source_info = {"provider": "ai_image", "asset_id": "ai_1"}

        with (
            patch("app.services.material._search_videos_with_cache", return_value=[]),
            patch("app.services.ai_image.generate_ai_video_clip", return_value=ai_item) as mock_ai,
            patch("app.services.material.save_video", return_value="/tmp/ai_generated_clip.mp4"),
            patch("os.path.exists", return_value=True),
            patch("os.path.isfile", return_value=True),
        ):
            config.app["enable_ai_image_fallback"] = True
            config.app["match_materials_to_script"] = False

            results = material.download_videos(
                task_id="test_ai_fallback",
                search_terms=["alien galaxy civilization"],
                source="pexels",
                audio_duration=4.0,
                max_clip_duration=5,
            )

        self.assertEqual(results, ["/tmp/ai_generated_clip.mp4"])
        mock_ai.assert_called_once()

    def test_download_videos_source_ai_image_generates_on_demand(self):
        """当选择 video_source='ai_image' 时，逐段调用 AI 生成并转换为带 Ken Burns 的素材。"""
        ai_item = MaterialInfo()
        ai_item.provider = "ai_image"
        ai_item.url = "/tmp/full_ai_clip.mp4"
        ai_item.duration = 4.0
        ai_item.source_info = {"provider": "ai_image", "asset_id": "ai_100"}

        with (
            patch("app.services.ai_image.generate_ai_video_clip", return_value=ai_item) as mock_ai,
            patch("app.services.material.save_video", return_value="/tmp/full_ai_clip.mp4"),
            patch("os.path.exists", return_value=True),
            patch("os.path.isfile", return_value=True),
        ):
            results = material.download_videos(
                task_id="test_full_ai",
                search_terms=["scene 1", "scene 2"],
                source="ai_image",
                audio_duration=4.0,
                max_clip_duration=4,
            )

        self.assertEqual(results, ["/tmp/full_ai_clip.mp4"])
        self.assertEqual(mock_ai.call_count, 1)

    def test_generate_ai_image_gemini_fallback_to_pollinations(self):
        """当 Gemini 图像接口报错或超额时，自动无缝平滑回退到 Pollinations AI。"""
        fake_content = b"pollinations_fallback_bytes_" * 100
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = fake_content

        with (
            patch("app.services.ai_image._generate_with_gemini", return_value=False),
            patch("requests.get", return_value=mock_response),
        ):
            path = ai_image.generate_ai_image(
                prompt="futuristic neon samurai",
                provider="gemini",
                save_dir=self.temp_dir.name,
            )

        self.assertIsNotNone(path)
        self.assertTrue(os.path.exists(path))

    def test_generate_ai_image_falls_back_to_stock_photo(self):
        """当所有 AI 生成服务均不可用时，自动回退到高清摄影图库保底。"""
        fake_stock = b"stock_photo_bytes_" * 100

        with (
            patch("app.services.ai_image._generate_with_gemini", return_value=False),
            patch("app.services.ai_image._fetch_pollinations_image_bytes", return_value=None),
            patch("app.services.ai_image._fetch_stock_photo_bytes", return_value=fake_stock),
        ):
            path = ai_image.generate_ai_image(
                prompt="steaming cup of coffee",
                provider="gemini",
                save_dir=self.temp_dir.name,
            )
            self.assertIsNotNone(path)
            self.assertTrue(os.path.exists(path))

    def test_download_videos_ai_image_expands_terms_for_long_audio(self):
        """当音频时长较长而关键词较少时，自动扩展视角并交替运镜生成足够的独立片段。"""
        ai_item = MaterialInfo()
        ai_item.provider = "ai_image"
        ai_item.url = "/tmp/clip.mp4"
        ai_item.duration = 4.0
        ai_item.source_info = {"provider": "ai_image"}

        with (
            patch("app.services.ai_image.generate_ai_video_clip", return_value=ai_item) as mock_ai,
            patch("app.services.material.save_video", side_effect=lambda url, **kw: f"/tmp/saved_{mock_ai.call_count}.mp4"),
            patch("os.path.exists", return_value=True),
            patch("os.path.isfile", return_value=True),
        ):
            results = material.download_videos(
                task_id="test_expansion",
                search_terms=["espresso"],
                source="ai_image",
                audio_duration=12.0,
                max_clip_duration=4,
            )

        self.assertEqual(len(results), 3)
        self.assertEqual(mock_ai.call_count, 3)
        # 验证运镜交替：zoom_in, zoom_out, zoom_in
        self.assertEqual(mock_ai.call_args_list[0].kwargs["motion"], "zoom_in")
        self.assertEqual(mock_ai.call_args_list[1].kwargs["motion"], "zoom_out")
        self.assertEqual(mock_ai.call_args_list[2].kwargs["motion"], "zoom_in")

    def test_download_videos_ai_image_handles_empty_search_terms_gracefully(self):
        """当 search_terms 为空或仅有空格时，优雅降级生成默认场景，不抛出 ZeroDivisionError。"""
        ai_item = MaterialInfo()
        ai_item.provider = "ai_image"
        ai_item.url = "/tmp/default_clip.mp4"
        ai_item.duration = 4.0
        ai_item.source_info = {"provider": "ai_image"}

        with (
            patch("app.services.ai_image.generate_ai_video_clip", return_value=ai_item) as mock_ai,
            patch("app.services.material.save_video", return_value="/tmp/default_clip.mp4"),
            patch("os.path.exists", return_value=True),
            patch("os.path.isfile", return_value=True),
        ):
            results = material.download_videos(
                task_id="test_empty_terms",
                search_terms=["   ", ""],
                source="ai_image",
                audio_duration=4.0,
                max_clip_duration=4,
            )

        self.assertEqual(results, ["/tmp/default_clip.mp4"])
        self.assertEqual(mock_ai.call_count, 1)


if __name__ == "__main__":
    unittest.main()


