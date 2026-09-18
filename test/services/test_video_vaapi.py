"""AMD VAAPI command wiring and safe CPU fallback."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.config import config
from app.services import video
from app.services.utils import ffmpeg_video


@pytest.fixture(autouse=True)
def vaapi_config(monkeypatch):
    monkeypatch.setitem(config.app, "video_codec", "h264_vaapi")
    monkeypatch.setitem(config.app, "video_vaapi_device", "/dev/dri/renderD128")
    monkeypatch.setitem(config.app, "video_vaapi_qp", 18)
    video._runtime_disabled_video_codecs.clear()
    yield
    video._runtime_disabled_video_codecs.clear()


def test_vaapi_selection_requires_render_node():
    with patch.object(video, "_ffmpeg_encoder_exists", return_value=True), patch.object(
        video.os.path, "exists", side_effect=lambda path: path == "/dev/dri/renderD128"
    ):
        assert video._get_effective_video_codec() == "h264_vaapi"
    with patch.object(video, "_ffmpeg_encoder_exists", return_value=True), patch.object(
        video.os.path, "exists", return_value=False
    ):
        assert video._get_effective_video_codec() == "libx264"
    config.app["video_vaapi_device"] = "/tmp/not-a-render-node"
    with patch.object(video, "_ffmpeg_encoder_exists", return_value=True):
        assert video._get_effective_video_codec() == "libx264"


@pytest.mark.parametrize("configured,expected", [(18, 18), ("16", 16), (0, 1), (99, 51), ("bad", 18)])
def test_vaapi_quality_is_bounded(configured, expected):
    config.app["video_vaapi_qp"] = configured
    assert video._get_vaapi_qp() == expected


@pytest.mark.parametrize("transition,complex_filter", [(None, False), ("slidein", True), ("zoomin", False)])
def test_vaapi_clip_command_uploads_frames(transition, complex_filter):
    with patch.object(ffmpeg_video.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
        assert ffmpeg_video.render_subclip_with_ffmpeg(
            "source.mp4", 0, 1, "output.mp4", 240, 320,
            transition_mode=transition, codec="h264_vaapi", fps=30,
            vaapi_device="/dev/dri/renderD128", vaapi_qp=18,
        )
    command = run.call_args.args[0]
    assert command[command.index("-vaapi_device") + 1] == "/dev/dri/renderD128"
    assert command[command.index("-qp") + 1] == "18"
    assert command[command.index("-c:v") + 1] == "h264_vaapi"
    assert "-pix_fmt" not in command
    graph = command[command.index("-filter_complex" if complex_filter else "-vf") + 1]
    assert "format=nv12,hwupload" in graph
    if complex_filter:
        assert "[software]format=nv12,hwupload[out]" in graph


def test_vaapi_image_command_uploads_frames(tmp_path):
    image = tmp_path / "image.png"
    image.touch()
    with patch.object(ffmpeg_video.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
        assert ffmpeg_video.render_image_to_video_with_ffmpeg(
            str(image), str(tmp_path / "output.mp4"), codec="h264_vaapi",
            vaapi_device="/dev/dri/renderD128", vaapi_qp=16,
        )
    command = run.call_args.args[0]
    assert command[command.index("-vaapi_device") + 1] == "/dev/dri/renderD128"
    assert command[command.index("-qp") + 1] == "16"
    assert "format=nv12,hwupload" in command[command.index("-vf") + 1]


def test_native_clip_gpu_failure_retries_cpu_without_disabling_on_double_failure(tmp_path):
    source = tmp_path / "source.mp4"
    source.touch()
    target = tmp_path / "output.mp4"
    kwargs = dict(source_path=str(source), start_time=0, source_duration=1,
                  output_path=str(target), target_width=240, target_height=320,
                  codec="h264_vaapi", threads=2, fps=30)
    with patch.object(video, "_get_vaapi_device", return_value="/dev/dri/renderD128"), patch.object(
        video, "native_render_slot"
    ) as slot, patch.object(video.ffmpeg_video, "render_subclip_with_ffmpeg", side_effect=[False, True]) as render:
        assert video._render_native_clip(**kwargs)
    assert [call.kwargs["codec"] for call in render.call_args_list] == ["h264_vaapi", "libx264"]
    assert "h264_vaapi" in video._runtime_disabled_video_codecs
    assert slot.called

    video._runtime_disabled_video_codecs.clear()
    with patch.object(video, "_get_vaapi_device", return_value="/dev/dri/renderD128"), patch.object(
        video, "native_render_slot"
    ), patch.object(video.ffmpeg_video, "render_subclip_with_ffmpeg", side_effect=[False, False]):
        assert not video._render_native_clip(**kwargs)
    assert "h264_vaapi" not in video._runtime_disabled_video_codecs


def test_moviepy_final_composition_uses_cpu_and_preserves_vaapi():
    class Clip:
        def __init__(self):
            self.codecs = []

        def write_videofile(self, output_file, codec, **kwargs):
            self.codecs.append(codec)

    clip = Clip()
    with patch.object(video, "_get_effective_video_codec", return_value="h264_vaapi"):
        assert video._write_videofile_with_codec_fallback(clip, "final.mp4", "h264_vaapi") == "libx264"
    assert clip.codecs == ["libx264"]
    assert not video._runtime_disabled_video_codecs


def test_vaapi_concat_command_and_cpu_fallback(tmp_path):
    source = tmp_path / "clip.mp4"
    source.touch()
    codecs = []

    def encode(command, **kwargs):
        codec = command[command.index("-c:v") + 1]
        codecs.append(codec)
        if codec == "h264_vaapi":
            assert command[command.index("-vaapi_device") + 1] == "/dev/dri/renderD128"
            assert command[command.index("-vf") + 1] == "format=nv12,hwupload"
            assert command[command.index("-qp") + 1] == "18"
            assert "-pix_fmt" not in command
            return SimpleNamespace(returncode=1, stderr="GPU failure", stdout="")
        assert command[command.index("-pix_fmt") + 1] == "yuv420p"
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    with patch.object(video, "_get_effective_video_codec", return_value="h264_vaapi"), patch.object(
        video, "_get_vaapi_device", return_value="/dev/dri/renderD128"
    ), patch.object(video, "_can_stream_copy_concat", return_value=False), patch.object(
        video.subprocess, "run", side_effect=encode
    ):
        assert video.concat_video_clips_with_ffmpeg(
            [str(source)], str(tmp_path / "joined.mp4"), fps=30
        ) == "libx264"
    assert codecs == ["h264_vaapi", "libx264"]
    assert "h264_vaapi" in video._runtime_disabled_video_codecs
