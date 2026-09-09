"""Direct YouTube video publishing through the YouTube Data API v3."""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests
from loguru import logger

from app.config import config
from app.services.oauth_state import OAuthStateStore
from app.utils import utils


class YouTubePublisher:
    AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    TOKEN_URL = "https://oauth2.googleapis.com/token"
    REVOKE_URL = "https://oauth2.googleapis.com/revoke"
    UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
    VIDEO_URL = "https://www.youtube.com/watch?v={video_id}"
    UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
    VALID_PRIVACY_STATUSES = frozenset({"public", "unlisted", "private"})

    def _setting(self, name: str, default: Any = "") -> Any:
        return config.app.get(f"youtube_direct_{name}", default)

    @property
    def enabled(self) -> bool:
        return bool(self._setting("enabled", False))

    @property
    def client_id(self) -> str:
        return str(self._setting("client_id", "") or "").strip()

    @property
    def client_secret(self) -> str:
        return str(self._setting("client_secret", "") or "").strip()

    @property
    def redirect_uri(self) -> str:
        return str(
            self._setting("redirect_uri", "http://localhost:8501")
            or "http://localhost:8501"
        ).strip()

    @property
    def privacy_status(self) -> str:
        value = str(self._setting("privacy_status", "private") or "private").lower()
        return value if value in self.VALID_PRIVACY_STATUSES else "private"

    @property
    def category_id(self) -> str:
        value = str(self._setting("category_id", "22") or "22").strip()
        return value if value.isdigit() else "22"

    @property
    def token_path(self) -> Path:
        credentials_dir = Path(utils.storage_dir("credentials", create=True)).resolve()
        configured_name = str(self._setting("token_file", "youtube-token.json") or "")
        # Token files always stay inside the ignored credentials directory. A custom
        # value may choose a filename but cannot escape through separators or '..'.
        filename = Path(configured_name).name
        if filename in {"", ".", ".."}:
            filename = "youtube-token.json"
        return credentials_dir / filename

    def has_client_credentials(self) -> bool:
        return bool(self.client_id and self.client_secret and self.redirect_uri)

    def is_authorized(self) -> bool:
        token = self._load_token()
        return bool(token.get("refresh_token") or token.get("access_token"))

    def is_configured(self) -> bool:
        return self.enabled and self.has_client_credentials() and self.is_authorized()

    def begin_authorization(self) -> str:
        return OAuthStateStore("youtube").issue()

    def consume_authorization_state(self, state: str) -> bool:
        return OAuthStateStore("youtube").consume(state)

    @staticmethod
    def new_state() -> str:
        return secrets.token_urlsafe(32)

    def build_authorization_url(self, state: str) -> str:
        if not self.has_client_credentials():
            raise ValueError("YouTube OAuth client credentials are missing")
        if not state:
            raise ValueError("OAuth state is required")

        query = urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "response_type": "code",
                "scope": self.UPLOAD_SCOPE,
                "access_type": "offline",
                "include_granted_scopes": "true",
                "prompt": "consent",
                "state": state,
            }
        )
        return f"{self.AUTH_URL}?{query}"

    def exchange_code(self, code: str) -> dict:
        if not self.has_client_credentials():
            raise ValueError("YouTube OAuth client credentials are missing")
        if not str(code or "").strip():
            raise ValueError("YouTube OAuth authorization code is missing")

        response = requests.post(
            self.TOKEN_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code": str(code).strip(),
                "grant_type": "authorization_code",
                "redirect_uri": self.redirect_uri,
            },
            timeout=30,
        )
        response.raise_for_status()
        token = response.json()
        if not token.get("access_token"):
            raise RuntimeError("Google did not return a YouTube access token")
        self._save_token(self._normalize_token(token))
        return {"success": True, "authorized": True}

    def disconnect(self) -> None:
        token = self._load_token()
        revoke_token = token.get("refresh_token") or token.get("access_token")
        if revoke_token:
            try:
                response = requests.post(
                    self.REVOKE_URL,
                    data={"token": revoke_token},
                    timeout=15,
                )
                response.raise_for_status()
            except requests.RequestException as exc:
                logger.warning(self._request_failure("YouTube token revocation", exc))
        try:
            self.token_path.unlink(missing_ok=True)
        except OSError as exc:
            raise RuntimeError(f"failed to remove local YouTube token: {exc}") from exc

    def upload_video(
        self,
        video_path: str,
        *,
        title: str,
        description: str = "",
        tags: list[str] | None = None,
        privacy_status: str | None = None,
        contains_synthetic_media: bool = True,
    ) -> dict:
        if not self.enabled:
            return self._failure("Direct YouTube publishing is disabled")
        if not self.has_client_credentials():
            return self._failure("YouTube OAuth client credentials are missing")
        if not os.path.isfile(video_path):
            return self._failure(f"Video file not found: {video_path}")

        privacy = str(privacy_status or self.privacy_status).lower()
        if privacy not in self.VALID_PRIVACY_STATUSES:
            return self._failure(f"Invalid YouTube privacy status: {privacy}")

        try:
            access_token = self._get_access_token()
            file_size = os.path.getsize(video_path)
            mime_type = "video/mp4"
            metadata = {
                "snippet": {
                    "title": (str(title or "Video").strip() or "Video")[:100],
                    "description": str(description or "")[:5000],
                    "tags": self._normalize_tags(tags),
                    "categoryId": self.category_id,
                },
                "status": {
                    "privacyStatus": privacy,
                    "selfDeclaredMadeForKids": False,
                    "containsSyntheticMedia": bool(contains_synthetic_media),
                },
            }
            session_response = requests.post(
                self.UPLOAD_URL,
                params={"uploadType": "resumable", "part": "snippet,status"},
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json; charset=UTF-8",
                    "X-Upload-Content-Length": str(file_size),
                    "X-Upload-Content-Type": mime_type,
                },
                json=metadata,
                timeout=30,
            )
            session_response.raise_for_status()
            upload_url = session_response.headers.get("Location")
            if not upload_url:
                return self._failure("YouTube did not return a resumable upload URL")

            with open(video_path, "rb") as video_file:
                upload_response = requests.put(
                    upload_url,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": mime_type,
                        "Content-Length": str(file_size),
                    },
                    data=video_file,
                    timeout=600,
                )
            upload_response.raise_for_status()
            payload = upload_response.json()
            video_id = str(payload.get("id") or "").strip()
            if not video_id:
                return self._failure("YouTube accepted the upload without a video ID")

            actual_privacy = (
                payload.get("status", {}).get("privacyStatus")
                if isinstance(payload.get("status"), dict)
                else None
            ) or privacy
            logger.success(f"YouTube upload completed: video_id={video_id}")
            return {
                "success": True,
                "platform": "youtube",
                "provider": "youtube_direct",
                "status": "uploaded",
                "video_id": video_id,
                "post_url": self.VIDEO_URL.format(video_id=video_id),
                "privacy_status": actual_privacy,
            }
        except requests.RequestException as exc:
            message = self._request_failure("YouTube upload", exc)
            logger.error(message)
            return self._failure(message)
        except (OSError, ValueError, RuntimeError) as exc:
            logger.error(f"direct YouTube upload failed: {exc}")
            return self._failure(str(exc))

    def _get_access_token(self) -> str:
        token = self._load_token()
        access_token = str(token.get("access_token") or "")
        expires_at = float(token.get("expires_at") or 0)
        if access_token and expires_at > time.time() + 60:
            return access_token

        refresh_token = str(token.get("refresh_token") or "")
        if not refresh_token:
            raise RuntimeError("YouTube account is not authorized")
        response = requests.post(
            self.TOKEN_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=30,
        )
        response.raise_for_status()
        refreshed = response.json()
        if not refreshed.get("access_token"):
            raise RuntimeError("Google did not refresh the YouTube access token")
        refreshed["refresh_token"] = refresh_token
        normalized = self._normalize_token(refreshed)
        self._save_token(normalized)
        return str(normalized["access_token"])

    def _load_token(self) -> dict:
        token_path = self.token_path
        if not token_path.is_file():
            return {}
        try:
            payload = json.loads(token_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            logger.warning(f"failed to read YouTube OAuth token: {exc}")
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save_token(self, token: dict) -> None:
        token_path = self.token_path
        token_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=".youtube-token-",
            suffix=".tmp",
            dir=str(token_path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as token_file:
                json.dump(token, token_file, ensure_ascii=False)
                token_file.flush()
                os.fsync(token_file.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, token_path)
            os.chmod(token_path, 0o600)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    @staticmethod
    def _normalize_token(token: dict) -> dict:
        normalized = {
            key: value
            for key, value in token.items()
            if key in {"access_token", "refresh_token", "scope", "token_type"}
        }
        try:
            expires_in = max(0, int(token.get("expires_in", 3600)))
        except (TypeError, ValueError):
            expires_in = 3600
        normalized["expires_at"] = time.time() + expires_in
        return normalized

    @staticmethod
    def _normalize_tags(tags: list[str] | None) -> list[str]:
        normalized = []
        for tag in tags or []:
            value = str(tag or "").strip().lstrip("#")
            if value and value not in normalized:
                normalized.append(value[:30])
        return normalized[:30]

    @staticmethod
    def _request_failure(operation: str, error: requests.RequestException) -> str:
        """Describe request failures without exposing request URLs or credentials."""
        response = getattr(error, "response", None)
        status_code = getattr(response, "status_code", None)
        if status_code is not None:
            return f"{operation} failed (HTTP {status_code})"
        return f"{operation} failed ({type(error).__name__})"

    @staticmethod
    def _failure(error: str) -> dict:
        return {
            "success": False,
            "platform": "youtube",
            "provider": "youtube_direct",
            "status": "failed",
            "error": error,
        }


youtube_publisher = YouTubePublisher()
