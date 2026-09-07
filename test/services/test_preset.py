"""
Unit tests for app/services/preset.py (Named Presets and Brand Kits)
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from app.models.schema import VideoParams
from app.services import preset


class TestPresetService(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.original_storage_dir = preset.utils.storage_dir

        def fake_storage_dir(sub_dir: str = "", create: bool = False):
            d = os.path.join(self.temp_dir, sub_dir)
            if create:
                os.makedirs(d, exist_ok=True)
            return d

        preset.utils.storage_dir = fake_storage_dir

    def tearDown(self):
        preset.utils.storage_dir = self.original_storage_dir
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_validate_preset_name_valid(self):
        self.assertEqual(preset.validate_preset_name("Shorts 9-16"), "Shorts 9-16")
        self.assertEqual(preset.validate_preset_name("Dark_Motivation"), "Dark_Motivation")
        self.assertEqual(preset.validate_preset_name("  Channel-1  "), "Channel-1")

    def test_validate_preset_name_invalid(self):
        for invalid in ("", "   ", "../escape", "name/with/slash", "name\\backslash", "a" * 70, "test*char"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    preset.validate_preset_name(invalid)

    def test_init_default_presets_seeds_defaults(self):
        presets = preset.list_presets()
        self.assertIn("Shorts_TikTok_Reels", presets)
        self.assertIn("YouTube_Landscape", presets)

        loaded = preset.load_preset("Shorts_TikTok_Reels")
        self.assertEqual(loaded["video_aspect"], "9:16")
        self.assertEqual(loaded["subtitle_style"], "karaoke")

    def test_save_and_load_preset_from_videoparams(self):
        params = VideoParams(
            video_subject="Crypto News",
            video_aspect="16:9",
            video_clip_duration=6,
            watermark_path="/path/to/logo.png",
            watermark_position="bottom_right",
            watermark_opacity=0.75,
            watermark_scale=0.2,
            intro_path="/path/to/intro.mp4",
            outro_path="/path/to/outro.mp4",
        )
        saved = preset.save_preset("Crypto_Channel", params)
        self.assertEqual(saved["schema"], "accio-video.settings-preset")

        presets = preset.list_presets()
        self.assertIn("Crypto_Channel", presets)

        loaded = preset.load_preset("Crypto_Channel")
        self.assertEqual(loaded["video_aspect"], "16:9")
        self.assertEqual(loaded["video_clip_duration"], 6)
        self.assertEqual(loaded["watermark_path"], "/path/to/logo.png")
        self.assertEqual(loaded["watermark_position"], "bottom_right")
        self.assertEqual(loaded["watermark_opacity"], 0.75)
        self.assertEqual(loaded["watermark_scale"], 0.2)
        self.assertEqual(loaded["intro_path"], "/path/to/intro.mp4")
        self.assertEqual(loaded["outro_path"], "/path/to/outro.mp4")

    def test_delete_preset(self):
        params = {"video_aspect": "9:16"}
        preset.save_preset("Temporary_Preset", params)
        self.assertIn("Temporary_Preset", preset.list_presets())

        deleted = preset.delete_preset("Temporary_Preset")
        self.assertTrue(deleted)
        self.assertNotIn("Temporary_Preset", preset.list_presets())

        # Deleting non-existent preset returns False
        self.assertFalse(preset.delete_preset("Non_Existent"))

    def test_load_non_existent_preset_raises_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            preset.load_preset("Unknown_Preset")

    def test_save_brand_asset_watermark(self):
        png_content = b"\x89PNG\r\n\x1a\nfake-png-data"
        saved_path = preset.save_brand_asset("watermark", "my_logo.PNG", png_content)
        self.assertTrue(os.path.isfile(saved_path))
        self.assertTrue(saved_path.endswith(".png"))
        self.assertEqual(Path(saved_path).read_bytes(), png_content)

        assets = preset.list_brand_assets("watermark")
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0], saved_path)

    def test_save_brand_asset_video(self):
        video_content = b"fake-video-bytes"
        intro_path = preset.save_brand_asset("intro", "intro_vignette.mp4", video_content)
        outro_path = preset.save_brand_asset("outro", "outro_cta.mov", video_content)

        self.assertTrue(os.path.isfile(intro_path))
        self.assertTrue(os.path.isfile(outro_path))

        all_assets = preset.list_brand_assets()
        self.assertIn(intro_path, all_assets)
        self.assertIn(outro_path, all_assets)

    def test_save_brand_asset_rejects_invalid_extension_or_category(self):
        with self.assertRaises(ValueError):
            preset.save_brand_asset("watermark", "malicious.exe", b"bytes")

        with self.assertRaises(ValueError):
            preset.save_brand_asset("intro", "audio.mp3", b"bytes")

        with self.assertRaises(ValueError):
            preset.save_brand_asset("unknown_category", "logo.png", b"bytes")

    def test_save_brand_asset_non_ascii_filename(self):
        saved = preset.save_brand_asset("watermark", "我的水印.png", b"img-data")
        self.assertTrue(os.path.isfile(saved))
        self.assertTrue(saved.endswith(".png"))
        self.assertIn("watermark_asset_", os.path.basename(saved))


if __name__ == "__main__":
    unittest.main()
