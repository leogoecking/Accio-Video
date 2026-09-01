import os
import re
import subprocess
from typing import Tuple

from loguru import logger

from app.utils import utils


def probe_media_fast(file_path: str) -> Tuple[float | None, int | None, int | None]:
    """
    快速探测媒体文件的时长与画面宽高，避免加载 MoviePy VideoFileClip/AudioFileClip 带来的性能和内存开销。
    
    返回 (duration_in_seconds, width, height)。对于纯音频文件，width 和 height 为 None。
    """
    if not file_path or not os.path.exists(file_path):
        return None, None, None

    ffmpeg_bin = utils.get_ffmpeg_binary()
    try:
        res = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-i", file_path],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except Exception as exc:
        logger.debug(f"fast probe failed for {file_path}: {exc}")
        return None, None, None

    duration = None
    width = None
    height = None

    # 解析时长: Duration: 00:00:05.12, start: ...
    dur_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", res.stderr)
    if dur_match:
        h, m, s = dur_match.groups()
        duration = int(h) * 3600 + int(m) * 60 + float(s)

    # 解析视频分辨率: Stream #0:0... Video: ..., 1080x1920, ...
    for line in res.stderr.splitlines():
        if "Stream #" in line and "Video:" in line:
            dim_match = re.search(r"\b([1-9]\d{1,4})x([1-9]\d{1,4})\b", line)
            if dim_match:
                width, height = int(dim_match.group(1)), int(dim_match.group(2))
                break

    return duration, width, height


def build_subclip_filtergraph(
    target_width: int,
    target_height: int,
    duration: float,
    clip_speed: float = 1.0,
    transition_mode: str | None = None,
    shuffle_side: str | None = None,
    fps: int = 30,
) -> Tuple[str, bool]:
    """
    构建片段切片、变速、画面缩放/黑边居中及转场特效的 FFmpeg filtergraph。

    返回 (filter_string, is_complex_filter)。
    is_complex_filter 为 True 时需要通过 -filter_complex 调用并映射输出标签。
    """
    duration = max(0.001, float(duration))
    target_width = int(target_width)
    target_height = int(target_height)
    total_frames = max(1, int(round(fps * duration)))
    trans_dur = min(1.0, duration)

    # 1. 速度调整 filter
    speed_filter = ""
    if abs(clip_speed - 1.0) > 1e-4 and clip_speed > 0:
        speed_filter = f"setpts=PTS/{clip_speed:.4f},"

    # 2. 保持比例缩放并在目标分辨率中居中黑边填充
    scale_pad_filter = (
        f"scale={target_width}:{target_height}:force_original_aspect_ratio=decrease,"
        f"pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2:color=black"
    )

    transition_value = str(transition_mode or "").strip().lower()

    # 处理 SlideIn / SlideOut (使用 -filter_complex 与黑底叠加)
    if transition_value in ("slidein", "slide_in", "slideout", "slide_out"):
        side = shuffle_side or "left"
        if side not in ("left", "right", "top", "bottom"):
            side = "left"

        if "in" in transition_value:
            # trans_dur 内滑入画面
            if side == "left":
                overlay_pos = f"x='if(lte(t,{trans_dur:.3f}),-w+w*(t/{trans_dur:.3f}),0)':y=0"
            elif side == "right":
                overlay_pos = f"x='if(lte(t,{trans_dur:.3f}),w-w*(t/{trans_dur:.3f}),0)':y=0"
            elif side == "top":
                overlay_pos = f"x=0:y='if(lte(t,{trans_dur:.3f}),-h+h*(t/{trans_dur:.3f}),0)'"
            else:  # bottom
                overlay_pos = f"x=0:y='if(lte(t,{trans_dur:.3f}),h-h*(t/{trans_dur:.3f}),0)'"
        else:
            # 片段末尾 trans_dur 滑出画面
            st = max(0.0, duration - trans_dur)
            if side == "left":
                overlay_pos = f"x='if(gte(t,{st:.3f}),-w*((t-{st:.3f})/{trans_dur:.3f}),0)':y=0"
            elif side == "right":
                overlay_pos = f"x='if(gte(t,{st:.3f}),w*((t-{st:.3f})/{trans_dur:.3f}),0)':y=0"
            elif side == "top":
                overlay_pos = f"x=0:y='if(gte(t,{st:.3f}),-h*((t-{st:.3f})/{trans_dur:.3f}),0)'"
            else:  # bottom
                overlay_pos = f"x=0:y='if(gte(t,{st:.3f}),h*((t-{st:.3f})/{trans_dur:.3f}),0)'"

        complex_filter = (
            f"[0:v]{speed_filter}{scale_pad_filter}[scaled];"
            f"color=c=black:s={target_width}x{target_height}:d={duration:.3f}[bg];"
            f"[bg][scaled]overlay={overlay_pos},fps={fps},format=yuv420p[out]"
        )
        return complex_filter, True

    # 单流滤镜链 (Simple Video Filter)
    vf_parts = []
    if speed_filter:
        vf_parts.append(speed_filter.rstrip(","))
    vf_parts.append(scale_pad_filter)

    if transition_value in ("fadein", "fade_in"):
        vf_parts.append(f"fade=t=in:st=0:d={trans_dur:.3f}")
    elif transition_value in ("fadeout", "fade_out"):
        st = max(0.0, duration - trans_dur)
        vf_parts.append(f"fade=t=out:st={st:.3f}:d={trans_dur:.3f}")
    elif transition_value in ("zoomin", "zoom_in"):
        vf_parts.append(
            f"zoompan=z='min(1.0+0.2*(on/{total_frames}),1.2)':d=1:"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={target_width}x{target_height}:fps={fps}"
        )
    elif transition_value in ("zoomout", "zoom_out"):
        vf_parts.append(
            f"zoompan=z='max(1.2-0.2*(on/{total_frames}),1.0)':d=1:"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={target_width}x{target_height}:fps={fps}"
        )

    vf_parts.append(f"fps={fps}")
    vf_parts.append("format=yuv420p")
    return ",".join(vf_parts), False


def render_subclip_with_ffmpeg(
    source_path: str,
    start_time: float,
    source_duration: float,
    output_path: str,
    target_width: int,
    target_height: int,
    clip_speed: float = 1.0,
    transition_mode: str | None = None,
    shuffle_side: str | None = None,
    effective_duration: float | None = None,
    codec: str = "libx264",
    threads: int = 2,
    fps: int = 30,
) -> bool:
    """
    使用纯 FFmpeg 滤镜图直接截取、缩放、变速并编码视频片段。
    
    避免了在 Python 层逐帧解码与拷贝像素，带来 3x-5x 的速度提升与极低的内存占用。
    """
    ffmpeg_bin = utils.get_ffmpeg_binary()
    calc_duration = (
        effective_duration
        if effective_duration is not None
        else (source_duration / max(0.01, clip_speed))
    )

    filter_str, is_complex = build_subclip_filtergraph(
        target_width=target_width,
        target_height=target_height,
        duration=calc_duration,
        clip_speed=clip_speed,
        transition_mode=transition_mode,
        shuffle_side=shuffle_side,
        fps=fps,
    )

    cmd = [
        ffmpeg_bin,
        "-y",
        "-ss",
        f"{start_time:.3f}",
        "-t",
        f"{source_duration:.3f}",
        "-i",
        source_path,
    ]

    if is_complex:
        cmd.extend(["-filter_complex", filter_str, "-map", "[out]"])
    else:
        cmd.extend(["-vf", filter_str])

    cmd.extend(
        [
            "-c:v",
            codec,
            "-threads",
            str(threads or 2),
            "-pix_fmt",
            "yuv420p",
            output_path,
        ]
    )

    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0:
            return True
        logger.warning(
            f"ffmpeg subclip render returned {res.returncode}: {(res.stderr or '').strip()}"
        )
        return False
    except Exception as exc:
        logger.warning(f"ffmpeg subclip render failed with exception: {exc}")
        return False


def render_image_to_video_with_ffmpeg(
    image_path: str,
    output_path: str,
    duration: float = 3.0,
    width: int | None = None,
    height: int | None = None,
    codec: str = "libx264",
    threads: int = 2,
    fps: int = 30,
) -> bool:
    """
    使用 FFmpeg zoompan 滤镜将静态图片转换为带有 Ken Burns 动态缩放效果的短视频。
    """
    if not os.path.exists(image_path):
        return False

    ffmpeg_bin = utils.get_ffmpeg_binary()
    duration = max(0.1, float(duration))
    total_frames = max(1, int(round(fps * duration)))

    # 确保宽高为偶数以适配 yuv420p
    out_w = (width // 2 * 2) if width else 1080
    out_h = (height // 2 * 2) if height else 1920

    vf = (
        f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
        f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"zoompan=z='min(1.0+0.2*(on/{total_frames}),1.2)':d=1:"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={out_w}x{out_h}:fps={fps},"
        f"format=yuv420p"
    )

    cmd = [
        ffmpeg_bin,
        "-y",
        "-loop",
        "1",
        "-i",
        image_path,
        "-vf",
        vf,
        "-t",
        f"{duration:.3f}",
        "-c:v",
        codec,
        "-threads",
        str(threads or 2),
        "-pix_fmt",
        "yuv420p",
        output_path,
    ]

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        return res.returncode == 0
    except Exception as exc:
        logger.warning(f"ffmpeg image-to-video render failed: {exc}")
        return False


def mix_audio_with_ffmpeg(
    voice_path: str,
    bgm_path: str,
    output_path: str,
    voice_volume: float = 1.0,
    bgm_volume: float = 0.2,
    bgm_fade_out: float = 3.0,
    loop_bgm: bool = True,
    total_duration: float | None = None,
    enable_ducking: bool = True,
    audio_fps: int = 44100,
    audio_bitrate: str = "192k",
) -> bool:
    """
    使用 FFmpeg 滤镜图直接混合旁白与背景音乐，支持：
    1. 旁白与 BGM 独立音量缩放
    2. BGM 自动循环铺满与淡出
    3. 智能 Auto-Ducking (Sidechain Compression)：在旁白说话时自动降低 BGM 音量，间隙平滑恢复。
    """
    if not os.path.exists(voice_path):
        return False

    ffmpeg_bin = utils.get_ffmpeg_binary()
    voice_vol = max(0.0, float(voice_volume))
    bgm_vol = max(0.0, float(bgm_volume))

    if total_duration is None or total_duration <= 0:
        dur, _, _ = probe_media_fast(voice_path)
        total_duration = dur or 10.0

    # 如果没有 BGM 或 BGM 音量为 0，直接单音轨标准化输出
    if not bgm_path or not os.path.exists(bgm_path) or bgm_vol <= 0:
        cmd = [
            ffmpeg_bin,
            "-y",
            "-i",
            voice_path,
            "-filter:a",
            f"volume={voice_vol:.3f}",
            "-c:a",
            "aac",
            "-b:a",
            audio_bitrate,
            "-ar",
            str(audio_fps),
            output_path,
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            return res.returncode == 0
        except Exception as exc:
            logger.warning(f"ffmpeg voice-only export failed: {exc}")
            return False

    # 计算淡出起始时间
    fade_st = max(0.0, total_duration - bgm_fade_out)
    bgm_fade_filter = f"volume={bgm_vol:.3f},afade=t=out:st={fade_st:.3f}:d={bgm_fade_out:.3f}"

    if enable_ducking:
        # 使用 sidechaincompress 动态压制 BGM
        filter_complex = (
            f"[0:a]volume={voice_vol:.3f},asplit=2[v_main][v_side];"
            f"[1:a]{bgm_fade_filter}[bgm_raw];"
            f"[bgm_raw][v_side]sidechaincompress=threshold=0.08:ratio=4:attack=20:release=300[bgm_ducked];"
            f"[v_main][bgm_ducked]amix=inputs=2:duration=first:dropout_transition=2[aout]"
        )
    else:
        filter_complex = (
            f"[0:a]volume={voice_vol:.3f}[v_main];"
            f"[1:a]{bgm_fade_filter}[bgm_raw];"
            f"[v_main][bgm_raw]amix=inputs=2:duration=first:dropout_transition=2[aout]"
        )

    cmd = [ffmpeg_bin, "-y", "-i", voice_path]

    if loop_bgm:
        cmd.extend(["-stream_loop", "-1"])
    cmd.extend(["-i", bgm_path])

    cmd.extend(
        [
            "-filter_complex",
            filter_complex,
            "-map",
            "[aout]",
            "-c:a",
            "aac",
            "-b:a",
            audio_bitrate,
            "-ar",
            str(audio_fps),
            output_path,
        ]
    )

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode == 0:
            return True
        logger.warning(
            f"ffmpeg audio mix returned {res.returncode}: {(res.stderr or '').strip()}"
        )
        return False
    except Exception as exc:
        logger.warning(f"ffmpeg audio mix failed: {exc}")
        return False
