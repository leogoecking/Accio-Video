import builtins
import gc
import os
import subprocess
import sys
import threading
import time
import weakref
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.config import config
from app.services import video
from app.services.utils import render_budget
from app.utils import utils


@pytest.fixture(autouse=True)
def isolated_render_slots(tmp_path, monkeypatch):
    monkeypatch.setattr(render_budget.utils, "storage_dir", lambda *args, **kwargs: str(tmp_path / "locks"))


@pytest.fixture
def clips(tmp_path, monkeypatch):
    monkeypatch.setitem(config.app, "video_codec", "libx264")
    monkeypatch.setitem(config.app, "video_concat_stream_copy", True)
    result = []
    for index, color in enumerate(("red", "blue")):
        clip = tmp_path / f"clip-{index}.mp4"
        subprocess.run(
            [utils.get_ffmpeg_binary(), "-v", "error", "-y", "-f", "lavfi", "-i",
             f"color=c={color}:s=240x320:r=30:d=1", "-an", "-c:v", "libx264",
             "-pix_fmt", "yuv420p", str(clip)],
            check=True, capture_output=True, timeout=15,
        )
        result.append(str(clip))
    return result


def frame_hashes(file_path):
    result = subprocess.run(
        [utils.get_ffmpeg_binary(), "-v", "error", "-i", str(file_path),
         "-map", "0:v:0", "-f", "framemd5", "-"],
        capture_output=True, text=True, check=True, timeout=15,
    )
    return [line.split(",")[-1].strip() for line in result.stdout.splitlines()
            if line and not line.startswith("#")]


def audio_prefix(file_path, seconds=0.5):
    return subprocess.run(
        [utils.get_ffmpeg_binary(), "-v", "error", "-i", str(file_path),
         "-map", "0:a:0", "-t", str(seconds), "-f", "s16le", "-acodec",
         "pcm_s16le", "-"],
        capture_output=True, check=True, timeout=15,
    ).stdout


@pytest.fixture
def stitch_materials(tmp_path):
    def make(name, color, duration, *, audio=True, sample_rate=44100, channels=2):
        output = tmp_path / f"{name}.mp4"
        command = [
            utils.get_ffmpeg_binary(), "-v", "error", "-y", "-f", "lavfi", "-i",
            f"color=c={color}:s=240x320:r=30:d={duration}",
        ]
        if audio:
            command.extend([
                "-f", "lavfi", "-i",
                f"sine=frequency=440:sample_rate={sample_rate}:duration={duration}",
            ])
        command.extend([
            "-map", "0:v:0", "-c:v", "libx264", "-preset", "medium",
            "-threads", "2", "-pix_fmt", "yuv420p",
        ])
        if audio:
            command.extend([
                "-map", "1:a:0", "-c:a", "aac", "-ar", str(sample_rate),
                "-ac", str(channels),
            ])
        command.append(str(output))
        subprocess.run(command, check=True, capture_output=True, timeout=30)
        return str(output)

    return make


@pytest.mark.parametrize("with_intro", [False, True])
def test_stitch_copies_main_video_with_normalized_brand_clips(
    stitch_materials, tmp_path, with_intro
):
    main = stitch_materials("main", "red", 2)
    outro = stitch_materials(
        "outro", "blue", 1, sample_rate=48000, channels=1
    )
    intro = stitch_materials("intro", "green", 1, audio=False) if with_intro else None
    output = tmp_path / "stitched.mp4"
    if video._probe_stitch_media(main) is None:
        pytest.skip("optional ffprobe is unavailable")

    with patch.object(video, "VideoFileClip", side_effect=AssertionError("MoviePy fallback")):
        assert video.stitch_intro_outro(
            main, str(output), intro_path=intro, outro_path=outro,
            target_width=240, target_height=320, fps=30,
        )

    main_info = video._probe_stitch_media(main)
    result_info = video._probe_stitch_media(str(output))
    assert result_info["frames"] == 90 + (30 if with_intro else 0)
    assert result_info["audio_signature"] == main_info["audio_signature"]
    assert float(result_info["video"]["start_time"]) == pytest.approx(0, abs=1 / 30)
    offset = 30 if with_intro else 0
    assert frame_hashes(output)[offset:offset + 60] == frame_hashes(main)
    if not with_intro:
        assert audio_prefix(output) == audio_prefix(main)


def test_stitch_keeps_moviepy_fallback_when_copy_is_incompatible(stitch_materials, tmp_path):
    main = stitch_materials("main", "red", 1)
    outro = stitch_materials("outro", "blue", 1)
    output = tmp_path / "stitched.mp4"

    with patch.object(video, "_stitch_intro_outro_stream_copy", return_value=False), patch.object(
        video, "_get_effective_video_codec", return_value="libx264"
    ):
        assert video.stitch_intro_outro(
            main, str(output), outro_path=outro,
            target_width=240, target_height=320, fps=30,
        )

    assert output.is_file()
    assert video._probe_stitch_media(str(output))["frames"] == 60


def test_vaapi_stitch_normalizes_brand_clip_with_matching_encoder(stitch_materials, tmp_path):
    main = stitch_materials("main", "red", 1)
    outro = stitch_materials("outro", "blue", 1)
    probe = video._probe_stitch_media
    main_info = probe(main)
    main_info["video"]["tags"] = {"encoder": "Lavc h264_vaapi"}
    media = {main: main_info, outro: probe(outro)}

    with patch.object(video, "_probe_stitch_media", side_effect=media.get), patch.object(
        video, "_get_vaapi_device", return_value="/dev/dri/renderD128"
    ), patch.object(video.subprocess, "run", return_value=SimpleNamespace(returncode=1, stderr="")) as run:
        assert not video._stitch_intro_outro_stream_copy(
            main, str(tmp_path / "output.mp4"), None, outro, 240, 320, 30
        )

    command = run.call_args.args[0]
    assert command[command.index("-c:v") + 1] == "h264_vaapi"
    assert command[command.index("-vaapi_device") + 1] == "/dev/dri/renderD128"


def test_generate_video_uses_vaapi_final_composition_when_available(tmp_path):
    params = video.VideoParams(video_subject="test", bgm_type="", subtitle_enabled=False)
    output = tmp_path / "final.mp4"
    with patch.object(video, "_render_final_with_vaapi", return_value=True) as render, patch.object(
        video, "_open_video_clip_quietly", side_effect=AssertionError("MoviePy should not run")
    ):
        assert video.generate_video("combined.mp4", "voice.mp3", "", str(output), params)
    assert render.call_args.args[:4] == ("combined.mp4", "voice.mp3", "", str(output))


def test_subtitle_overlay_releases_each_caption_after_saving(tmp_path):
    references = []

    def caption(item):
        start, end = item[0]
        clip = (video.ColorClip((20, 20), color=(255, 255, 255))
                .with_start(start).with_end(end))
        references.append(weakref.ref(clip))
        return clip

    for index in range(3):
        video._save_subtitle_overlay_image(
            ((0, 1), "test"), str(tmp_path / f"cue-{index}.png"),
            32, 32, caption,
        )
        gc.collect()
        assert all(reference() is None for reference in references)


def test_vaapi_failure_log_classifies_error_without_exposing_stderr(
    stitch_materials, tmp_path, monkeypatch
):
    monkeypatch.setitem(config.app, "video_vaapi_final_composition", True)
    source = stitch_materials("source", "blue", 1)
    source_info = video._probe_stitch_media(source)
    error = "Stream specifier matches no streams; access_token=private-value"
    with patch.object(video, "_get_effective_video_codec", return_value="h264_vaapi"), patch.object(
        video, "_get_vaapi_device", return_value="/dev/dri/renderD128"
    ), patch.object(video, "_probe_stitch_media", return_value=source_info), patch.object(
        video.subprocess, "run", return_value=SimpleNamespace(returncode=1, stderr=error)
    ), patch.object(video.logger, "warning") as warning:
        assert not video._render_final_with_vaapi(
            source, source, "", str(tmp_path / "final.mp4"),
            video.VideoParams(video_subject="test", bgm_type=""),
            240, 320, None, None,
        )

    message = warning.call_args.args[0]
    assert "ffmpeg_exit_code=1" in message
    assert "reason=missing_media_stream" in message
    assert "private-value" not in message


def test_vaapi_final_composition_preserves_subtitle_timing_and_audio(
    stitch_materials, tmp_path, monkeypatch
):
    monkeypatch.setitem(config.app, "video_codec", "h264_vaapi")
    monkeypatch.setitem(config.app, "video_vaapi_final_composition", True)
    if video._get_effective_video_codec() != "h264_vaapi":
        pytest.skip("VAAPI render device is unavailable")
    source = stitch_materials("source", "blue", 2)
    subtitles = tmp_path / "subtitles.srt"
    subtitles.write_text("1\n00:00:00,500 --> 00:00:01,500\nTest\n", encoding="utf-8")
    output = tmp_path / "final.mp4"
    params = video.VideoParams(video_subject="test", bgm_type="")

    def cue(item):
        start, end = item[0]
        return (video.ColorClip((20, 20), color=(255, 255, 255))
                .with_position((10, 10)).with_start(start).with_end(end))

    assert video._render_final_with_vaapi(
        source, source, str(subtitles), str(output), params, 240, 320,
        cue, lambda _: video.ColorClip((1, 1), color=(0, 0, 0)),
    )
    info = video._probe_stitch_media(str(output))
    assert info["frames"] == 60
    assert info["audio"]["sample_rate"] == "44100"
    assert info["audio"]["channels"] == 2

    def pixel_at(seconds):
        result = subprocess.run(
            [utils.get_ffmpeg_binary(), "-v", "error", "-ss", str(seconds),
             "-i", str(output), "-frames:v", "1", "-f", "rawvideo",
             "-pix_fmt", "rgb24", "-"],
            capture_output=True, check=True, timeout=15,
        )
        offset = (15 * 240 + 15) * 3
        return result.stdout[offset:offset + 3]

    assert pixel_at(0.2)[0] < 100
    assert pixel_at(1.0)[0] > 200
    assert pixel_at(1.8)[0] < 100


def test_stream_copy_preserves_every_frame_and_clip_order(clips, tmp_path):
    if video._probe_concat_video(clips[0]) is None:
        pytest.skip("optional ffprobe is unavailable")
    output = tmp_path / "combined.mp4"
    codec = video.concat_video_clips_with_ffmpeg(clips, str(output), fps=30)
    assert codec == "copy"
    assert frame_hashes(output) == frame_hashes(clips[0]) + frame_hashes(clips[1])
    assert video._probe_concat_video(str(output))["duration"] == pytest.approx(2, abs=1 / 30)
    assert not list(tmp_path.glob("ffmpeg-concat-*.txt"))


def test_incompatible_streams_do_not_attempt_copy(clips, tmp_path):
    metadata = {
        clips[0]: {"signature": ("first encoder parameters",), "fps": Fraction(30)},
        clips[1]: {"signature": ("different encoder parameters",), "fps": Fraction(30)},
    }
    with patch.object(video, "_probe_concat_video", side_effect=metadata.get), patch.object(
        video.subprocess, "run", wraps=subprocess.run
    ) as run:
        assert video.concat_video_clips_with_ffmpeg(clips, str(tmp_path / "combined.mp4"), fps=30) == "libx264"
    assert all(call.args[0][call.args[0].index("-c:v") + 1] != "copy" for call in run.call_args_list)


def test_missing_optional_probe_retains_encoding(clips, tmp_path):
    with patch.object(video, "_probe_concat_video", return_value=None):
        assert video.concat_video_clips_with_ffmpeg(clips, str(tmp_path / "combined.mp4")) == "libx264"


def test_real_parallel_clip_renders_match_serial_frames(clips, tmp_path, monkeypatch):
    items = [video.SubClippedVideoClip(clip, start_time=0, end_time=1) for clip in clips]
    directories = []
    for workers in (1, 2):
        monkeypatch.setitem(config.app, "video_render_workers", workers)
        directory = tmp_path / f"workers-{workers}"
        directory.mkdir()
        assert video._prepare_native_clips(
            items, str(directory), 2, 1, 1, "fadein", 240, 320, 2, 30,
        ) == {0: True, 1: True}
        directories.append(directory)
    for index in (1, 2):
        assert frame_hashes(directories[0] / f"temp-clip-{index}.mp4") == frame_hashes(
            directories[1] / f"temp-clip-{index}.mp4"
        )


def test_stream_copy_duration_failure_reencodes_to_exact_duration(clips, tmp_path):
    probe = video._probe_concat_video

    def inaccurate_output(file_path):
        info = probe(file_path)
        if info and Path(file_path).name == "combined.mp4":
            return {**info, "duration": 99}
        return info

    if probe(clips[0]) is None:
        pytest.skip("optional ffprobe is unavailable")
    output = tmp_path / "combined.mp4"
    with patch.object(video, "_probe_concat_video", side_effect=inaccurate_output):
        assert video.concat_video_clips_with_ffmpeg(clips, str(output), max_duration=1.5, fps=30) == "libx264"
    assert probe(str(output))["duration"] == pytest.approx(1.5, abs=1 / 30)


def test_native_parallelism_is_bounded_and_results_keep_timeline_order(tmp_path, monkeypatch):
    monkeypatch.setitem(config.app, "video_render_workers", 2)
    items = []
    for index in range(6):
        source = tmp_path / f"source-{index}.mp4"
        source.touch()
        items.append(video.SubClippedVideoClip(str(source), start_time=0, end_time=1))
    state = {"active": 0, "peak": 0, "finished": 0}
    lock = threading.Lock()
    two_started = threading.Event()

    def render(**kwargs):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            if state["active"] == 2:
                two_started.set()
        assert two_started.wait(2), "expected two simultaneous FFmpeg renders"
        time.sleep(0.01)
        Path(kwargs["output_path"]).write_bytes(b"rendered clip")
        with lock:
            state["active"] -= 1
            state["finished"] += 1
        return True

    with patch.object(video, "_get_effective_video_codec", return_value="libx264"), patch.object(
        video.ffmpeg_video, "render_subclip_with_ffmpeg", side_effect=render
    ), patch.object(video.os, "cpu_count", return_value=12):
        result = video._prepare_native_clips(items, str(tmp_path), 3, 1, 1, None, 240, 320, 2, 30)
    assert list(result) == [0, 1, 2]
    assert all(result.values())
    assert state == {"active": 0, "peak": 2, "finished": 3}
    assert len(list(tmp_path.glob("temp-clip-*.mp4"))) == 3


def test_native_failure_retains_serial_fallback_and_waits_for_workers(tmp_path, monkeypatch):
    monkeypatch.setitem(config.app, "video_render_workers", 2)
    source = tmp_path / "source.mp4"
    source.touch()
    items = [video.SubClippedVideoClip(str(source), start_time=0, end_time=1)]
    with patch.object(video, "_get_effective_video_codec", return_value="libx264"), patch.object(
        video.ffmpeg_video, "render_subclip_with_ffmpeg", side_effect=RuntimeError("failed native render")
    ):
        assert video._prepare_native_clips(items, str(tmp_path), 1, 1, 1, None, 240, 320, 2, 30) == {0: False}
    assert not any(thread.name.startswith("video-clip") for thread in threading.enumerate())


def test_hardware_encoding_keeps_incremental_fallback(tmp_path):
    source = tmp_path / "source.mp4"
    source.touch()
    items = [video.SubClippedVideoClip(str(source), start_time=0, end_time=1)]
    with patch.object(video, "_get_effective_video_codec", return_value="h264_nvenc"), patch.object(
        video.ffmpeg_video, "render_subclip_with_ffmpeg"
    ) as render:
        assert video._prepare_native_clips(items, str(tmp_path), 1, 1, 1, None, 240, 320, 2, 30) == {}
        render.assert_not_called()


def test_native_budget_is_shared_between_simultaneous_videos(tmp_path, monkeypatch):
    monkeypatch.setitem(config.app, "video_render_workers", 2)
    source = tmp_path / "source.mp4"
    source.touch()
    items = [video.SubClippedVideoClip(str(source), start_time=0, end_time=1)] * 2
    state = {"active": 0, "peak": 0, "finished": 0}
    lock = threading.Lock()
    two_started = threading.Event()

    def render(**kwargs):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            if state["active"] == 2:
                two_started.set()
        assert two_started.wait(3)
        time.sleep(0.03)
        Path(kwargs["output_path"]).write_bytes(b"clip")
        with lock:
            state["active"] -= 1
            state["finished"] += 1
        return True

    def prepare(index):
        directory = tmp_path / str(index)
        directory.mkdir()
        return video._prepare_native_clips(items, str(directory), 2, 1, 1, None, 240, 320, 2, 30)

    with patch.object(video, "_get_effective_video_codec", return_value="libx264"), patch.object(
        video.ffmpeg_video, "render_subclip_with_ffmpeg", side_effect=render
    ), patch.object(video.os, "cpu_count", return_value=12), ThreadPoolExecutor(2) as pool:
        assert list(pool.map(prepare, (1, 2))) == [{0: True, 1: True}] * 2
    assert state == {"active": 0, "peak": 2, "finished": 4}


def test_interrupted_executor_joins_workers_and_removes_outputs(tmp_path):
    source = tmp_path / "source.mp4"
    source.touch()
    items = [video.SubClippedVideoClip(str(source), start_time=0, end_time=1)] * 2

    class InterruptedPool(ThreadPoolExecutor):
        submissions = 0

        def submit(self, *args, **kwargs):
            self.submissions += 1
            if self.submissions == 2:
                raise RuntimeError("executor interrupted")
            future = super().submit(*args, **kwargs)
            future.result(timeout=3)
            return future

    def render(**kwargs):
        Path(kwargs["output_path"]).write_bytes(b"clip")
        return True

    with patch.object(video, "ThreadPoolExecutor", InterruptedPool), patch.object(
        video, "_get_effective_video_codec", return_value="libx264"
    ), patch.object(video.ffmpeg_video, "render_subclip_with_ffmpeg", side_effect=render):
        with pytest.raises(RuntimeError, match="executor interrupted"):
            video._prepare_native_clips(items, str(tmp_path), 2, 1, 1, None, 240, 320, 2, 30)
    assert not list(tmp_path.glob("temp-clip-*.mp4"))
    assert source.exists()
    assert not any(thread.name.startswith("video-clip") for thread in threading.enumerate())


@pytest.mark.parametrize("stage", ["preparation", "assembly"])
def test_combine_cleans_all_reserved_outputs_on_interruption(tmp_path, stage):
    source = tmp_path / "source.mp4"
    source.touch()
    output = tmp_path / "combined.mp4"
    temporary = tmp_path / "temp-clip-1.mp4"

    def prepare(*args):
        temporary.write_bytes(b"clip")
        if stage == "preparation":
            raise RuntimeError("interrupted preparation")
        return {0: True}

    with patch.object(video, "AudioFileClip", return_value=SimpleNamespace(duration=0.5)), patch.object(
        video, "_open_video_clip_quietly", return_value=SimpleNamespace(duration=1, size=(640, 480))
    ), patch.object(video, "close_clip"), patch.object(video, "_prepare_native_clips", side_effect=prepare), patch.object(
        video, "concat_video_clips_with_ffmpeg", side_effect=RuntimeError("interrupted assembly")
    ):
        with pytest.raises(RuntimeError, match=f"interrupted {stage}"):
            video.combine_videos(str(output), [str(source)], "audio.mp3")
    assert not temporary.exists()
    assert source.exists()


def test_render_slot_is_shared_with_another_process_and_released_on_failure(tmp_path):
    code = (
        "import sys\n"
        "from app.services.utils import render_budget\n"
        "render_budget.utils.storage_dir = lambda *a, **kw: sys.argv[1]\n"
        "print('ready', flush=True)\n"
        "with render_budget.native_render_slot(1):\n"
        "    print('acquired', flush=True)\n"
    )
    child = None
    try:
        with pytest.raises(RuntimeError, match="render failed"):
            with render_budget.native_render_slot(1):
                child = subprocess.Popen(
                    [sys.executable, "-c", code, str(tmp_path / "locks")],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                assert child.stdout.readline().strip() == "ready"
                with pytest.raises(subprocess.TimeoutExpired):
                    child.communicate(timeout=0.25)
                raise RuntimeError("render failed")
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == 0, stderr
        assert "acquired" in stdout
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.communicate(timeout=5)


@pytest.mark.skipif(os.name == "nt", reason="Unix flock supports read-only descriptors")
def test_render_slot_uses_read_only_file_created_by_another_user(tmp_path, monkeypatch):
    directory = tmp_path / "locks"
    directory.mkdir()
    (directory / "slot-1.lock").touch()
    real_open = builtins.open

    def restricted_open(path, mode="r", *args, **kwargs):
        if str(path).endswith("slot-1.lock") and mode == "a+b":
            raise PermissionError(path)
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(render_budget, "open", restricted_open, raising=False)
    with render_budget.native_render_slot(1):
        with render_budget.native_render_slot(2):
            pass


@pytest.mark.parametrize("configured,cpus,threads,codec,expected", [
    (2, 12, 2, "libx264", 2),
    (99, 12, 2, "libx264", 4),
    (4, 2, 2, "libx264", 1),
    (0, 12, 2, "libx264", 1),
    (2, 12, 2, "h264_nvenc", 1),
])
def test_render_worker_budget(configured, cpus, threads, codec, expected, monkeypatch):
    monkeypatch.setitem(config.app, "video_render_workers", configured)
    with patch.object(video.os, "cpu_count", return_value=cpus):
        assert video._get_video_render_workers(threads, codec) == expected
