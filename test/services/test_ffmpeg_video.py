import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from PIL import Image

from app.services.utils import ffmpeg_video
from app.utils import utils


class TestFFmpegVideoUtils(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg_bin = utils.get_ffmpeg_binary()
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.test_dir = cls.temp_dir.name

        # Create a test video (3s, 640x360, 30fps)
        cls.test_video = os.path.join(cls.test_dir, "test_video.mp4")
        subprocess.run(
            [
                cls.ffmpeg_bin,
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=duration=3:size=640x360:rate=30",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                cls.test_video,
            ],
            check=True,
            capture_output=True,
        )

        # Create a test audio file (voice: 3s)
        cls.test_voice = os.path.join(cls.test_dir, "voice.mp3")
        subprocess.run(
            [
                cls.ffmpeg_bin,
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=3",
                "-c:a",
                "libmp3lame",
                cls.test_voice,
            ],
            check=True,
            capture_output=True,
        )

        # Create a test BGM file (bgm: 2s)
        cls.test_bgm = os.path.join(cls.test_dir, "bgm.mp3")
        subprocess.run(
            [
                cls.ffmpeg_bin,
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=220:duration=2",
                "-c:a",
                "libmp3lame",
                cls.test_bgm,
            ],
            check=True,
            capture_output=True,
        )

        # Create a test image (500x700)
        cls.test_img = os.path.join(cls.test_dir, "image.png")
        img = Image.new("RGB", (500, 700), color=(80, 120, 180))
        img.save(cls.test_img)

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

    def test_probe_media_fast(self):
        # Missing file
        self.assertEqual(ffmpeg_video.probe_media_fast("non_existent.mp4"), (None, None, None))

        # Video probe
        dur, w, h = ffmpeg_video.probe_media_fast(self.test_video)
        self.assertIsNotNone(dur)
        self.assertAlmostEqual(dur, 3.0, delta=0.2)
        self.assertEqual(w, 640)
        self.assertEqual(h, 360)

        # Audio probe
        dur_a, w_a, h_a = ffmpeg_video.probe_media_fast(self.test_voice)
        self.assertIsNotNone(dur_a)
        self.assertAlmostEqual(dur_a, 3.0, delta=0.2)
        self.assertIsNone(w_a)
        self.assertIsNone(h_a)

    def test_build_subclip_filtergraph(self):
        # Normal
        fg, is_complex = ffmpeg_video.build_subclip_filtergraph(1080, 1920, 3.0)
        self.assertFalse(is_complex)
        self.assertIn("scale=1080:1920", fg)
        self.assertIn("pad=1080:1920", fg)
        self.assertIn("fps=30", fg)

        # Speed scaling
        fg_speed, _ = ffmpeg_video.build_subclip_filtergraph(1080, 1920, 3.0, clip_speed=1.5)
        self.assertIn("setpts=PTS/1.5000", fg_speed)

        # Transitions
        fg_fade, _ = ffmpeg_video.build_subclip_filtergraph(1080, 1920, 3.0, transition_mode="FadeIn")
        self.assertIn("fade=t=in:st=0:d=1", fg_fade)

        fg_zoom, _ = ffmpeg_video.build_subclip_filtergraph(1080, 1920, 3.0, transition_mode="ZoomIn")
        self.assertIn("zoompan=", fg_zoom)

        # SlideIn complex
        fg_slide, is_complex_slide = ffmpeg_video.build_subclip_filtergraph(
            1080, 1920, 3.0, transition_mode="SlideIn", shuffle_side="right"
        )
        self.assertTrue(is_complex_slide)
        self.assertIn("overlay=", fg_slide)

    def test_render_subclip_with_ffmpeg(self):
        out_clip = os.path.join(self.test_dir, "out_subclip.mp4")
        success = ffmpeg_video.render_subclip_with_ffmpeg(
            source_path=self.test_video,
            start_time=0.5,
            source_duration=1.5,
            output_path=out_clip,
            target_width=1080,
            target_height=1920,
            clip_speed=1.0,
            transition_mode="FadeIn",
        )
        self.assertTrue(success)
        self.assertTrue(os.path.exists(out_clip))
        dur, w, h = ffmpeg_video.probe_media_fast(out_clip)
        self.assertEqual(w, 1080)
        self.assertEqual(h, 1920)
        self.assertAlmostEqual(dur, 1.5, delta=0.2)

    def test_render_image_to_video_with_ffmpeg(self):
        out_img_vid = os.path.join(self.test_dir, "out_image_zoom.mp4")
        success = ffmpeg_video.render_image_to_video_with_ffmpeg(
            image_path=self.test_img,
            output_path=out_img_vid,
            duration=2.0,
            width=1080,
            height=1920,
        )
        self.assertTrue(success)
        self.assertTrue(os.path.exists(out_img_vid))
        dur, w, h = ffmpeg_video.probe_media_fast(out_img_vid)
        self.assertEqual(w, 1080)
        self.assertEqual(h, 1920)
        self.assertAlmostEqual(dur, 2.0, delta=0.2)

    def test_mix_audio_with_ffmpeg(self):
        # Mix voice and BGM with ducking
        out_audio = os.path.join(self.test_dir, "out_mixed.m4a")
        success = ffmpeg_video.mix_audio_with_ffmpeg(
            voice_path=self.test_voice,
            bgm_path=self.test_bgm,
            output_path=out_audio,
            voice_volume=1.0,
            bgm_volume=0.2,
            bgm_fade_out=1.0,
            loop_bgm=True,
            total_duration=3.0,
            enable_ducking=True,
        )
        self.assertTrue(success)
        self.assertTrue(os.path.exists(out_audio))
        dur, _, _ = ffmpeg_video.probe_media_fast(out_audio)
        self.assertAlmostEqual(dur, 3.0, delta=0.2)

    def test_mix_audio_with_ffmpeg_voice_only(self):
        out_voice_only = os.path.join(self.test_dir, "out_voice_only.m4a")
        success = ffmpeg_video.mix_audio_with_ffmpeg(
            voice_path=self.test_voice,
            bgm_path="",
            output_path=out_voice_only,
            voice_volume=0.8,
            bgm_volume=0.0,
        )
        self.assertTrue(success)
        self.assertTrue(os.path.exists(out_voice_only))


if __name__ == "__main__":
    unittest.main()
