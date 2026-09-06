"""
WebUI Task History & Task Management Helpers
"""
import os
import re
import json
from collections.abc import Mapping
from datetime import datetime
from loguru import logger

_FINAL_VIDEO_PATTERN = re.compile(
    r"^final-(?P<index>\d+)\.(?P<extension>mp4|mov|mkv|webm)$",
    re.IGNORECASE,
)
_DOWNLOAD_FILENAME_INVALID_PATTERN = re.compile(r"[<>:\"/\\|?*\x00-\x1f]")

VOICE_MODE_TTS = "tts"
VOICE_MODE_UPLOAD = "upload"
VOICE_MODE_NONE = "none"


def format_task_time(timestamp: float | None) -> str:
    if not timestamp:
        return "-"
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


def format_task_subject(subject: str | None, max_length: int = 30) -> str:
    subj = str(subject or "").replace("\n", " ").strip()
    if len(subj) <= max_length:
        return subj or "-"
    return f"{subj[:max_length]}..."


def safe_load_task_script(task_path: str) -> dict:
    script_file = os.path.join(task_path, "script.json")
    if not os.path.isfile(script_file):
        return {}
    try:
        with open(script_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"failed to read task script data: {script_file}, {e}")
        return {}


def find_final_task_video(task_path: str) -> str:
    """Return path of the final output video with lowest index in task_path."""
    try:
        files = os.listdir(task_path)
    except OSError:
        return ""

    candidates = []
    for file_name in files:
        match = _FINAL_VIDEO_PATTERN.fullmatch(file_name)
        if match:
            candidates.append((int(match.group("index")), file_name))

    if not candidates:
        return ""

    _, file_name = min(candidates, key=lambda item: item[0])
    return os.path.join(task_path, file_name)


def build_video_download_name(subject: str | None, index: int, total: int) -> str:
    cleaned = _DOWNLOAD_FILENAME_INVALID_PATTERN.sub("_", str(subject or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip().strip("._")
    base_name = cleaned or "video"
    if total > 1:
        return f"{base_name}-{index}.mp4"
    return f"{base_name}.mp4"


def build_restore_upload_requirements(params: Mapping) -> dict:
    """Track upload requirements from history tasks that cannot be auto-filled by browser."""
    return {
        "local_materials": params.get("video_source") == "local",
        "custom_audio": bool(params.get("custom_audio_file")),
        "original_voice_name": params.get("voice_name") or "",
    }


def get_unmet_restore_upload_requirements(
    requirements: Mapping | None,
    *,
    video_source: str,
    voice_name: str,
    has_local_materials: bool,
    has_custom_audio: bool,
    voice_mode: str | None = None,
) -> set[str]:
    """Return unmet upload requirements for restoring a historical generation task."""
    requirements = requirements or {}
    unmet = set()

    if (
        requirements.get("local_materials")
        and video_source == "local"
        and not has_local_materials
    ):
        unmet.add("local_materials")

    if requirements.get("custom_audio") and not has_custom_audio:
        if voice_mode is not None:
            if voice_mode == VOICE_MODE_UPLOAD:
                unmet.add("custom_audio")
        elif voice_name == requirements.get("original_voice_name", ""):
            unmet.add("custom_audio")

    return unmet
