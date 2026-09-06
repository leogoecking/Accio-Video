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
            status=data.get("status", "draft_ready"),
            created_at=float(data.get("created_at", 0.0)),
            updated_at=float(data.get("updated_at", 0.0)),
            scenes=scenes,
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


def create_storyboard_draft(
    task_id: str,
    video_subject: str,
    script_lines: list[str],
    video_paths: list[str],
    audio_duration: float = 0.0,
    material_sources: list[dict[str, Any]] | None = None,
) -> StoryboardDraft:
    """
    Assemble a new StoryboardDraft from raw script lines and matched video paths.
    """
    import time

    now = time.time()
    sources = material_sources or []
    scenes: list[StoryboardScene] = []

    total_scenes = max(len(script_lines), len(video_paths), 1)
    dur_per_scene = (audio_duration / total_scenes) if (audio_duration and total_scenes) else 4.0

    task_dir = utils.task_dir(task_id) if task_id else ""
    thumbs_dir = os.path.join(task_dir, "thumbnails") if task_dir else ""

    for idx in range(total_scenes):
        text = script_lines[idx] if idx < len(script_lines) else ""
        vid = video_paths[idx] if idx < len(video_paths) else (video_paths[0] if video_paths else "")
        source_meta = sources[idx] if idx < len(sources) else {}
        provider = source_meta.get("provider", "stock") if isinstance(source_meta, dict) else "stock"
        term = source_meta.get("search_term", "") if isinstance(source_meta, dict) else ""

        thumb_path = ""
        if vid and thumbs_dir:
            target_thumb = os.path.join(thumbs_dir, f"scene-{idx + 1}.jpg")
            thumb_path = generate_scene_thumbnail(vid, target_thumb) or ""

        scene = StoryboardScene(
            scene_index=idx + 1,
            text=text,
            duration=round(dur_per_scene, 2),
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
