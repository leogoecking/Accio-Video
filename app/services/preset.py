"""
Preset and Brand Kit Service
Handles named generation presets and brand asset management.
"""
import json
import os
import re
from pathlib import Path
from uuid import uuid4
from loguru import logger

from app.config import config
from app.models.schema import VideoParams
from app.utils import file_security, utils
from webui.components.settings_transfer import (
    build_settings_preset_payload,
    parse_settings_preset,
)

PRESET_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_\- ]{1,64}$")
SUPPORTED_WATERMARK_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp"})
SUPPORTED_VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".mkv", ".webm"})


def get_preset_dir() -> str:
    return utils.storage_dir("presets", create=True)


def get_brand_dir() -> str:
    return utils.storage_dir("brand", create=True)


def validate_preset_name(name: str) -> str:
    if not name:
        raise ValueError("preset name cannot be empty")
    name = name.strip()
    if not PRESET_NAME_PATTERN.match(name) or ".." in name:
        raise ValueError(
            "preset name must contain only letters, numbers, spaces, hyphens, and underscores (max 64 chars)"
        )
    return name


def list_presets() -> list[str]:
    preset_dir = get_preset_dir()
    init_default_presets()
    names = []
    for file in Path(preset_dir).glob("*.json"):
        names.append(file.stem)
    return sorted(names)


def get_preset_file_path(name: str) -> str:
    safe_name = validate_preset_name(name)
    preset_dir = get_preset_dir()
    file_path = os.path.join(preset_dir, f"{safe_name}.json")
    return file_security.resolve_path_within_directory(
        preset_dir, file_path, require_file=False
    )


def save_preset(name: str, params: dict | VideoParams) -> dict:
    safe_name = validate_preset_name(name)
    if isinstance(params, VideoParams):
        params_dict = params.model_dump(mode="json")
    elif isinstance(params, dict):
        params_dict = params
    else:
        raise TypeError("params must be a dict or VideoParams instance")

    payload = build_settings_preset_payload(
        params=params_dict,
        app_version=getattr(config, "project_version", "1.0.0"),
    )
    file_path = get_preset_file_path(safe_name)
    with open(file_path, "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)
    logger.info(f"saved preset '{safe_name}' to {file_path}")
    return payload


def load_preset(name: str) -> dict:
    file_path = get_preset_file_path(name)
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"preset '{name}' not found")
    raw_bytes = Path(file_path).read_bytes()
    return parse_settings_preset(raw_bytes)


def delete_preset(name: str) -> bool:
    try:
        file_path = get_preset_file_path(name)
    except ValueError:
        return False
    if os.path.isfile(file_path):
        os.remove(file_path)
        logger.info(f"deleted preset '{name}'")
        return True
    return False


def init_default_presets():
    """Initializes default starter presets if storage/presets is empty."""
    preset_dir = get_preset_dir()
    existing = list(Path(preset_dir).glob("*.json"))
    if existing:
        return

    default_shorts = {
        "video_aspect": "9:16",
        "video_clip_duration": 4,
        "subtitle_enabled": True,
        "subtitle_style": "karaoke",
        "karaoke_highlight_color": "#FFDD00",
        "karaoke_max_words": 3,
        "font_size": 70,
        "stroke_width": 2.0,
    }
    default_landscape = {
        "video_aspect": "16:9",
        "video_clip_duration": 5,
        "subtitle_enabled": True,
        "subtitle_style": "classic",
        "font_size": 50,
        "stroke_width": 1.5,
    }
    try:
        save_preset("Shorts_TikTok_Reels", default_shorts)
        save_preset("YouTube_Landscape", default_landscape)
        logger.info("seeded default generation presets")
    except Exception as exc:
        logger.warning(f"failed to seed default presets: {exc}")


def save_brand_asset(category: str, filename: str, content: bytes) -> str:
    category = category.lower().strip()
    if category not in {"watermark", "intro", "outro"}:
        raise ValueError(f"unsupported brand asset category: {category}")

    ext = Path(filename).suffix.lower()
    if category == "watermark" and ext not in SUPPORTED_WATERMARK_EXTENSIONS:
        raise ValueError(
            f"watermark file must be an image ({', '.join(sorted(SUPPORTED_WATERMARK_EXTENSIONS))})"
        )
    if category in {"intro", "outro"} and ext not in SUPPORTED_VIDEO_EXTENSIONS:
        raise ValueError(
            f"{category} file must be a video ({', '.join(sorted(SUPPORTED_VIDEO_EXTENSIONS))})"
        )

    raw_stem = Path(filename).stem
    base_name = re.sub(r"[^a-zA-Z0-9_\-\.]", "_", raw_stem)[:40].strip("._")
    if not base_name:
        base_name = f"asset_{uuid4().hex[:8]}"
    safe_filename = f"{category}_{base_name}_{uuid4().hex}{ext}"
    brand_dir = get_brand_dir()
    target_path = os.path.join(brand_dir, safe_filename)
    target_path = file_security.resolve_path_within_directory(
        brand_dir, target_path, require_file=False
    )
    with open(target_path, "xb") as fp:
        fp.write(content)
    logger.info(f"saved brand asset ({category}) to {target_path}")
    return target_path


def list_brand_assets(category: str | None = None) -> list[str]:
    brand_dir = get_brand_dir()
    pattern = f"{category.lower()}_*" if category else "*"
    files = sorted(Path(brand_dir).glob(pattern))
    return [str(f) for f in files if f.is_file()]
