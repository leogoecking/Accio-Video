import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from webui.components.storyboard import (
    StoryboardDraft,
    StoryboardScene,
    create_storyboard_draft,
    generate_scene_thumbnail,
    load_storyboard_draft,
    save_storyboard_draft,
    update_scene_material,
    update_scene_text,
)


class TestStoryboardComponent(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_storyboard_scene_serialization(self):
        scene = StoryboardScene(
            scene_index=1,
            text="Welcome to the future",
            duration=3.5,
            material_path="/path/to/clip1.mp4",
            material_provider="pexels",
            search_term="futuristic city",
            thumbnail_path="/path/to/thumb1.jpg",
        )
        d = scene.to_dict()
        self.assertEqual(d["scene_index"], 1)
        self.assertEqual(d["text"], "Welcome to the future")
        self.assertEqual(d["duration"], 3.5)

        restored = StoryboardScene.from_dict(d)
        self.assertEqual(restored.scene_index, 1)
        self.assertEqual(restored.text, "Welcome to the future")
        self.assertEqual(restored.material_provider, "pexels")

    def test_create_and_serialize_draft(self):
        script_lines = ["Line 1", "Line 2", "Line 3"]
        video_paths = ["/video/1.mp4", "/video/2.mp4", "/video/3.mp4"]
        sources = [
            {"provider": "pexels", "search_term": "nature"},
            {"provider": "ai_image", "search_term": "cyberpunk"},
            {"provider": "pixabay", "search_term": "sky"},
        ]

        with patch("app.utils.utils.task_dir", return_value=self.temp_dir.name):
            draft = create_storyboard_draft(
                task_id="test_task_123",
                video_subject="Documentary",
                script_lines=script_lines,
                video_paths=video_paths,
                audio_duration=12.0,
                material_sources=sources,
            )

        self.assertEqual(draft.task_id, "test_task_123")
        self.assertEqual(draft.video_subject, "Documentary")
        self.assertEqual(len(draft.scenes), 3)
        self.assertEqual(draft.scenes[0].text, "Line 1")
        self.assertEqual(draft.scenes[0].material_provider, "pexels")
        self.assertEqual(draft.scenes[1].material_provider, "ai_image")
        self.assertEqual(draft.scenes[0].duration, 4.0)

    def test_save_and_load_storyboard_draft(self):
        draft = StoryboardDraft(
            task_id="task_save_load",
            video_subject="History of AI",
            total_duration=10.0,
            scenes=[
                StoryboardScene(
                    scene_index=1,
                    text="In the beginning",
                    duration=5.0,
                    material_path="/vid/1.mp4",
                ),
                StoryboardScene(
                    scene_index=2,
                    text="Then neural nets emerged",
                    duration=5.0,
                    material_path="/vid/2.mp4",
                ),
            ],
        )

        with patch("app.utils.utils.task_dir", return_value=self.temp_dir.name):
            file_path = save_storyboard_draft("task_save_load", draft)
            self.assertTrue(os.path.exists(file_path))

            loaded = load_storyboard_draft("task_save_load")
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.task_id, "task_save_load")
            self.assertEqual(loaded.video_subject, "History of AI")
            self.assertEqual(len(loaded.scenes), 2)
            self.assertEqual(loaded.scenes[1].text, "Then neural nets emerged")

    def test_load_nonexistent_draft_returns_none(self):
        with patch("app.utils.utils.task_dir", return_value=self.temp_dir.name):
            result = load_storyboard_draft("non_existent_task")
            self.assertIsNone(result)

    def test_update_scene_material_and_text(self):
        draft = StoryboardDraft(
            task_id="update_test",
            video_subject="Space",
            total_duration=8.0,
            scenes=[
                StoryboardScene(scene_index=1, text="Rocket launch", material_path="/v/old.mp4"),
                StoryboardScene(scene_index=2, text="Orbit insertion", material_path="/v/old2.mp4"),
            ],
        )

        with patch("app.utils.utils.task_dir", return_value=self.temp_dir.name):
            # Update scene 2 text
            ok_text = update_scene_text(draft, 2, "Re-entry and landing")
            self.assertTrue(ok_text)
            self.assertEqual(draft.scenes[1].text, "Re-entry and landing")

            # Update scene 1 material
            ok_mat = update_scene_material(
                draft,
                scene_index=1,
                new_material_path="/v/new_rocket.mp4",
                new_provider="ai_image",
                new_search_term="falcon 9 launch",
            )
            self.assertTrue(ok_mat)
            self.assertEqual(draft.scenes[0].material_path, "/v/new_rocket.mp4")
            self.assertEqual(draft.scenes[0].material_provider, "ai_image")
            self.assertEqual(draft.scenes[0].search_term, "falcon 9 launch")

            # Attempt update on invalid scene index
            self.assertFalse(update_scene_text(draft, 99, "invalid"))
            self.assertFalse(update_scene_material(draft, 99, "/invalid.mp4"))

    def test_generate_scene_thumbnail_image_direct(self):
        fake_img = os.path.join(self.temp_dir.name, "sample.jpg")
        with open(fake_img, "wb") as f:
            f.write(b"image_bytes")

        out_thumb = os.path.join(self.temp_dir.name, "thumb.jpg")
        res = generate_scene_thumbnail(fake_img, out_thumb)
        self.assertEqual(res, fake_img)

    def test_generate_scene_thumbnail_mock_ffmpeg(self):
        fake_vid = os.path.join(self.temp_dir.name, "sample.mp4")
        with open(fake_vid, "wb") as f:
            f.write(b"video_bytes")

        out_thumb = os.path.join(self.temp_dir.name, "thumb.jpg")

        def fake_run(cmd, **kwargs):
            with open(out_thumb, "wb") as f:
                f.write(b"thumb_frame")
            mock_res = MagicMock()
            mock_res.returncode = 0
            return mock_res

        with patch("subprocess.run", side_effect=fake_run):
            res = generate_scene_thumbnail(fake_vid, out_thumb)
            self.assertEqual(res, out_thumb)
            self.assertTrue(os.path.exists(out_thumb))


if __name__ == "__main__":
    unittest.main()
