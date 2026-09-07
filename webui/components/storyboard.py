"""
WebUI Storyboard & Scene Draft Review Component

Enables intermediate draft review, scene-by-scene preview, text tweaking,
and granular media replacement before the final video compilation.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict, dataclass, field
from typing import Any

from loguru import logger

from app.utils import utils


@dataclass
class StoryboardScene:
    """Represents a single narrative scene in a draft."""
    scene_index: int
    text: str
    duration: float = 4.0
    start_time: float = 0.0
    end_time: float = 0.0
    material_path: str = ""
    material_provider: str = ""
    search_term: str = ""
    thumbnail_path: str = ""
    custom_notes: str = ""
    audio_clip_path: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StoryboardScene:
        return cls(
            scene_index=data.get("scene_index", 0),
            text=data.get("text", ""),
            duration=float(data.get("duration", 4.0)),
            start_time=float(data.get("start_time", 0.0)),
            end_time=float(data.get("end_time", 0.0)),
            material_path=data.get("material_path", ""),
            material_provider=data.get("material_provider", ""),
            search_term=data.get("search_term", ""),
            thumbnail_path=data.get("thumbnail_path", ""),
            custom_notes=data.get("custom_notes", ""),
            audio_clip_path=data.get("audio_clip_path", ""),
            extra=data.get("extra", {}),
        )


@dataclass
class StoryboardDraft:
    """Represents the complete pre-render draft for a video task."""
    task_id: str
    video_subject: str
    total_duration: float
    scenes: list[StoryboardScene] = field(default_factory=list)
    status: str = "draft_ready"  # draft_ready, rendering, completed
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "video_subject": self.video_subject,
            "total_duration": self.total_duration,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "scenes": [s.to_dict() for s in self.scenes],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StoryboardDraft:
        scenes = [
            StoryboardScene.from_dict(s)
            for s in data.get("scenes", [])
            if isinstance(s, dict)
        ]
        return cls(
            task_id=data.get("task_id", ""),
            video_subject=data.get("video_subject", ""),
            total_duration=float(data.get("total_duration", 0.0)),
            scenes=scenes,
            status=data.get("status", "draft_ready"),
            created_at=float(data.get("created_at", 0.0)),
            updated_at=float(data.get("updated_at", 0.0)),
        )


def generate_scene_thumbnail(
    material_path: str,
    output_path: str,
    time_offset: float = 0.5,
) -> str | None:
    """
    Extract a clean thumbnail frame from a video or copy/resize a static image.
    Uses FFmpeg to grab a frame at time_offset.
    """
    if not material_path or not os.path.exists(material_path):
        return None

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    ext = os.path.splitext(material_path)[1].lower()

    if ext in (".jpg", ".jpeg", ".png", ".webp"):
        return material_path

    ffmpeg_bin = utils.get_ffmpeg_binary()
    cmd = [
        ffmpeg_bin,
        "-y",
        "-ss", str(max(0.0, time_offset)),
        "-i", material_path,
        "-vframes", "1",
        "-q:v", "2",
        output_path,
    ]

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=10)
        if res.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            return output_path
        logger.debug(f"thumbnail generation fallback at 0.0s for {material_path}")
        cmd[3] = "0.0"
        res2 = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=10)
        if res2.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            return output_path
    except Exception as exc:
        logger.warning(f"failed to generate thumbnail for {material_path}: {exc}")

    return None


def parse_srt_time_to_seconds(time_str: str) -> float:
    """Parse SRT timestamp '00:00:04,975' to seconds float."""
    try:
        parts = time_str.strip().split(":")
        h = int(parts[0])
        m = int(parts[1])
        s_parts = parts[2].replace(".", ",").split(",")
        s = int(s_parts[0])
        ms = int(s_parts[1]) if len(s_parts) > 1 else 0
        return h * 3600 + m * 60 + s + ms / 1000.0
    except Exception:
        return 0.0


def parse_subtitles_to_scenes(
    subtitle_path: str,
    script_lines: list[str],
    total_audio_duration: float,
) -> list[dict[str, Any]]:
    """
    Maps script paragraphs to subtitle time spans to produce exact, continuous
    scene timestamps aligned with the narration audio.
    """
    import re
    from app.services.subtitle import file_to_subtitles

    raw_subs = file_to_subtitles(subtitle_path)
    if not raw_subs:
        dur_per_scene = (
            (total_audio_duration / len(script_lines))
            if (total_audio_duration and script_lines)
            else 4.0
        )
        return [
            {
                "text": line,
                "start": round(i * dur_per_scene, 2),
                "end": round((i + 1) * dur_per_scene, 2),
                "duration": round(dur_per_scene, 2),
            }
            for i, line in enumerate(script_lines)
        ]

    parsed_subs = []
    for _idx, times, text in raw_subs:
        if " --> " in times:
            start_str, end_str = times.split(" --> ")
            parsed_subs.append({
                "start": parse_srt_time_to_seconds(start_str),
                "end": parse_srt_time_to_seconds(end_str),
                "text": text,
            })

    if not parsed_subs:
        dur_per_scene = (
            (total_audio_duration / len(script_lines))
            if (total_audio_duration and script_lines)
            else 4.0
        )
        return [
            {
                "text": line,
                "start": round(i * dur_per_scene, 2),
                "end": round((i + 1) * dur_per_scene, 2),
                "duration": round(dur_per_scene, 2),
            }
            for i, line in enumerate(script_lines)
        ]

    scenes = []
    sub_idx = 0
    prev_end = 0.0

    for p_idx, p in enumerate(script_lines):
        p_clean = re.sub(r"[^\w\s]", "", p.lower())
        p_words = p_clean.split()
        matched = []

        while sub_idx < len(parsed_subs):
            sub_clean = re.sub(r"[^\w\s]", "", parsed_subs[sub_idx]["text"].lower())
            sub_words = sub_clean.split()
            if any(w in p_words for w in sub_words):
                matched.append(parsed_subs[sub_idx])
                sub_idx += 1
                if sub_words and sub_words[-1] in p_words[-2:]:
                    break
            else:
                if not matched:
                    matched.append(parsed_subs[sub_idx])
                    sub_idx += 1
                break

        if p_idx == len(script_lines) - 1:
            end_time = (
                total_audio_duration
                if total_audio_duration > prev_end
                else (matched[-1]["end"] if matched else prev_end + 3.0)
            )
        elif matched and sub_idx < len(parsed_subs):
            end_time = (matched[-1]["end"] + parsed_subs[sub_idx]["start"]) / 2.0
        elif matched:
            end_time = matched[-1]["end"]
        else:
            end_time = prev_end + 3.5

        dur = max(0.5, round(end_time - prev_end, 2))
        scenes.append({
            "text": p,
            "start": round(prev_end, 2),
            "end": round(prev_end + dur, 2),
            "duration": dur,
        })
        prev_end = prev_end + dur

    return scenes


def generate_scene_image_prompt(scene_text: str, context_term: str = "") -> str:
    """
    Uses LLM to convert a narration sentence into a concise, photorealistic
    visual prompt in English for image generation.
    """
    clean_text = scene_text.strip()
    if not clean_text:
        return context_term or "cinematic video scene"

    try:
        from app.services import llm

        prompt = (
            f"Convert this video narration sentence into a single concise, photorealistic English visual prompt "
            f"(under 25 words) for an image generator (like Midjourney or Flux). Describe a concrete physical visual scene:\n"
            f"Narration: \"{clean_text}\"\n"
            f"Context: \"{context_term.strip()}\"\n"
            f"Rules:\n"
            f"- Output ONLY the English visual description (Subject, Environment, Lighting, Style).\n"
            f"- No preamble, no quotes, no conversational intro."
        )
        response = llm._generate_response(prompt)
        if response and not response.startswith("Error:"):
            cleaned = response.strip().strip('"\'')
            if len(cleaned) > 5:
                return cleaned
    except Exception as exc:
        logger.warning(f"failed to generate scene image prompt via LLM: {exc}")

    return context_term or clean_text[:60]


def create_storyboard_draft(
    task_id: str,
    video_subject: str,
    script_lines: list[str],
    video_paths: list[str],
    audio_duration: float = 0.0,
    material_sources: list[dict[str, Any]] | None = None,
    subtitle_path: str | None = None,
) -> StoryboardDraft:
    """
    Assemble a new StoryboardDraft from raw script lines and matched video paths,
    aligning scene durations accurately with subtitle timestamps.
    """
    import time

    now = time.time()
    sources = material_sources or []
    scenes: list[StoryboardScene] = []

    clean_lines = [line.strip() for line in script_lines if line.strip()]
    if not clean_lines:
        clean_lines = [video_subject or "Scene 1"]

    if subtitle_path and os.path.isfile(subtitle_path):
        timed_scenes = parse_subtitles_to_scenes(
            subtitle_path, clean_lines, audio_duration
        )
    else:
        dur_per_scene = (
            (audio_duration / len(clean_lines))
            if (audio_duration and clean_lines)
            else 4.0
        )
        timed_scenes = [
            {
                "text": line,
                "start": round(i * dur_per_scene, 2),
                "end": round((i + 1) * dur_per_scene, 2),
                "duration": round(dur_per_scene, 2),
            }
            for i, line in enumerate(clean_lines)
        ]

    task_dir = utils.task_dir(task_id) if task_id else ""
    thumbs_dir = os.path.join(task_dir, "thumbnails") if task_dir else ""

    for idx, s_info in enumerate(timed_scenes):
        text = s_info["text"]
        dur = s_info["duration"]
        start_t = s_info.get("start", 0.0)
        end_t = s_info.get("end", 0.0)

        vid = video_paths[idx % len(video_paths)] if video_paths else ""
        source_meta = sources[idx % len(sources)] if sources else {}
        provider = (
            source_meta.get("provider", "stock")
            if isinstance(source_meta, dict)
            else "stock"
        )
        term = (
            source_meta.get("search_term", "")
            if isinstance(source_meta, dict)
            else ""
        )

        thumb_path = ""
        if vid and thumbs_dir:
            target_thumb = os.path.join(thumbs_dir, f"scene-{idx + 1}.jpg")
            thumb_path = generate_scene_thumbnail(vid, target_thumb) or ""

        scene = StoryboardScene(
            scene_index=idx + 1,
            text=text,
            duration=round(dur, 2),
            start_time=round(start_t, 2),
            end_time=round(end_t, 2),
            material_path=vid,
            material_provider=provider,
            search_term=term,
            thumbnail_path=thumb_path,
        )
        scenes.append(scene)

    return StoryboardDraft(
        task_id=task_id,
        video_subject=video_subject,
        total_duration=round(audio_duration, 2),
        scenes=scenes,
        status="draft_ready",
        created_at=now,
        updated_at=now,
    )


def save_storyboard_draft(task_id: str, draft: StoryboardDraft | dict[str, Any]) -> str:
    """Save the storyboard draft to the task directory as draft.json."""
    data = draft.to_dict() if isinstance(draft, StoryboardDraft) else draft
    task_dir = utils.task_dir(task_id)
    os.makedirs(task_dir, exist_ok=True)
    draft_file = os.path.join(task_dir, "draft.json")
    with open(draft_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"storyboard draft saved for task {task_id}: {draft_file}")
    return draft_file


def load_storyboard_draft(task_id: str) -> StoryboardDraft | None:
    """Load an existing storyboard draft for task_id, or None if not found."""
    task_dir = utils.task_dir(task_id)
    draft_file = os.path.join(task_dir, "draft.json")
    if not os.path.isfile(draft_file):
        return None
    try:
        with open(draft_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        return StoryboardDraft.from_dict(data)
    except Exception as exc:
        logger.warning(f"failed to read draft file {draft_file}: {exc}")
        return None


def update_scene_material(
    draft: StoryboardDraft,
    scene_index: int,
    new_material_path: str,
    new_provider: str = "",
    new_search_term: str = "",
) -> bool:
    """Replace the media associated with a specific scene in the draft."""
    for scene in draft.scenes:
        if scene.scene_index == scene_index:
            scene.material_path = new_material_path
            if new_provider:
                scene.material_provider = new_provider
            if new_search_term:
                scene.search_term = new_search_term
            task_dir = utils.task_dir(draft.task_id) if draft.task_id else ""
            if task_dir and new_material_path:
                target_thumb = os.path.join(task_dir, "thumbnails", f"scene-{scene_index}.jpg")
                scene.thumbnail_path = generate_scene_thumbnail(new_material_path, target_thumb) or ""
            import time
            draft.updated_at = time.time()
            return True
    return False


def update_scene_text(
    draft: StoryboardDraft,
    scene_index: int,
    new_text: str,
) -> bool:
    """Update the script/subtitle text of a specific scene."""
    for scene in draft.scenes:
        if scene.scene_index == scene_index:
            scene.text = new_text.strip()
            import time
            draft.updated_at = time.time()
            return True
    return False
