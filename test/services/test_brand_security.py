from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.controllers.v1 import video as controller
from app.models.exception import HttpException
from app.models.schema import VideoParams
from app.services import task, video
from app.utils import file_security, utils


@pytest.fixture
def brand_files(tmp_path, monkeypatch):
    monkeypatch.setattr(utils, "root_dir", lambda: str(tmp_path))
    brand = tmp_path / "storage" / "brand"
    brand.mkdir(parents=True)
    asset = brand / "logo.png"
    asset.write_bytes(b"uploaded")
    outside = tmp_path / "private.png"
    outside.write_bytes(b"private")
    return brand, asset, outside


@pytest.mark.parametrize("field", ["watermark_path", "intro_path", "outro_path"])
def test_brand_paths_accept_upload_and_relative_paths(brand_files, field):
    _, asset, _ = brand_files
    for path in (str(asset), "logo.png", "storage/brand/logo.png"):
        params = VideoParams(**{field: path})
        assert file_security.resolve_brand_paths(params) == {field: str(asset)}


@pytest.mark.parametrize("field", ["watermark_path", "intro_path", "outro_path"])
def test_brand_paths_reject_escape_and_missing_files(brand_files, field):
    _, _, outside = brand_files
    for path in (str(outside), "../../private.png", "missing.png"):
        with pytest.raises(ValueError, match="existing uploaded file"):
            file_security.resolve_brand_paths(VideoParams(**{field: path}))


def test_brand_paths_reject_symlink_escape(brand_files):
    brand, _, outside = brand_files
    link = brand / "link.png"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(ValueError, match="existing uploaded file"):
        file_security.resolve_brand_paths(VideoParams(watermark_path=str(link)))


def test_api_rejects_brand_escape_before_scheduling(brand_files):
    _, _, outside = brand_files
    with patch.object(controller.task_manager, "add_task") as schedule:
        with pytest.raises(HttpException) as error:
            controller.create_task(
                SimpleNamespace(headers={"x-task-id": "brand-test"}),
                VideoParams(intro_path=str(outside)),
                stop_at="video",
            )
    assert error.value.status_code == 400
    schedule.assert_not_called()


def test_pipeline_rejects_brand_escape_before_provider_calls(brand_files):
    _, _, outside = brand_files
    with patch.object(task, "generate_script") as generate, patch.object(task.sm, "state"):
        result = task.start("brand-test", VideoParams(outro_path=str(outside)))
    generate.assert_not_called()
    assert result["failed_stage"] == "preflight"


def test_renderer_rejects_brand_escape_before_opening_media(brand_files):
    _, _, outside = brand_files
    with patch.object(video, "_open_video_clip_quietly") as open_video:
        with pytest.raises(ValueError, match="existing uploaded file"):
            video.generate_video(
                "source.mp4", "voice.mp3", "", "result.mp4",
                VideoParams(watermark_path=str(outside)),
            )
    open_video.assert_not_called()
