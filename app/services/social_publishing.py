"""Route social publishing jobs to direct APIs or the Upload-Post fallback."""

from __future__ import annotations

from typing import Any

from app.config import config
from app.services import upload_post
from app.services.instagram_publisher import instagram_publisher
from app.services.youtube_publisher import youtube_publisher


SUPPORTED_PLATFORMS = frozenset({"youtube", "instagram", "tiktok", "facebook"})


def configured_platforms() -> list[str]:
    configured = config.app.get(
        "social_publish_platforms",
        upload_post.upload_post_service.platforms,
    )
    if not isinstance(configured, (list, tuple)):
        return []
    platforms = []
    for raw_platform in configured:
        platform = str(raw_platform or "").strip().lower()
        if platform in SUPPORTED_PLATFORMS and platform not in platforms:
            platforms.append(platform)
    return platforms


def auto_publish_enabled() -> bool:
    return bool(
        config.app.get(
            "social_auto_publish",
            upload_post.upload_post_service.auto_upload,
        )
    )


def publishing_enabled() -> bool:
    if not auto_publish_enabled() or not configured_platforms():
        return False
    if youtube_publisher.enabled or instagram_publisher.enabled:
        return True
    return upload_post.upload_post_service.is_configured()


def youtube_privacy_status() -> str:
    if youtube_publisher.enabled:
        return youtube_publisher.privacy_status
    value = str(upload_post.upload_post_service.youtube_privacy_status or "private")
    return value if value in {"public", "unlisted", "private"} else "private"


def provider_for(platform: str) -> str:
    platform = str(platform or "").strip().lower()
    if platform == "youtube" and youtube_publisher.enabled:
        return "youtube_direct"
    if platform == "instagram" and instagram_publisher.enabled:
        return "instagram_direct"
    return "upload_post"


def publish_video(
    *,
    platform: str,
    video_path: str,
    metadata: dict[str, Any],
    youtube_privacy_status: str,
) -> dict:
    platform = str(platform or "").strip().lower()
    if platform not in SUPPORTED_PLATFORMS:
        return {
            "success": False,
            "platform": platform or "unknown",
            "provider": "none",
            "status": "failed",
            "error": f"Unsupported publishing platform: {platform or 'empty'}",
        }

    if platform == "youtube" and youtube_publisher.enabled:
        return youtube_publisher.upload_video(
            video_path,
            title=metadata.get("title", ""),
            description=metadata.get("caption", ""),
            tags=metadata.get("hashtags", []),
            privacy_status=youtube_privacy_status,
            contains_synthetic_media=True,
        )

    if platform == "instagram" and instagram_publisher.enabled:
        return instagram_publisher.upload_video(
            video_path,
            caption=(metadata.get("caption") or metadata.get("title") or ""),
        )

    result = upload_post.cross_post_video(
        video_path=video_path,
        title=(
            metadata.get("caption")
            or metadata.get("title")
            or "Check out this video! #shorts #viral"
        ),
        platforms=[platform],
        youtube_extra=(
            {
                "youtube_title": metadata.get("title", ""),
                "youtube_description": metadata.get("caption", ""),
                "tags": metadata.get("hashtags", []),
                "privacyStatus": youtube_privacy_status,
                "containsSyntheticMedia": True,
            }
            if platform == "youtube"
            else None
        ),
    )
    if not isinstance(result, dict):
        result = {
            "success": False,
            "error": "Upload-Post returned an invalid response",
        }
    normalized = dict(result)
    normalized.setdefault("platform", platform)
    normalized.setdefault("provider", "upload_post")
    normalized.setdefault("status", "accepted" if normalized.get("success") else "failed")
    return normalized
