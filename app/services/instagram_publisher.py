"""Direct Instagram Reels publishing through Instagram Login."""

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
from app.services.public_media import temporary_public_video
from app.utils import utils


class InstagramPublisher:
    AUTH_URL = "https://www.instagram.com/oauth/authorize"
    TOKEN_URL = "https://api.instagram.com/oauth/access_token"
    GRAPH_HOST = "https://graph.instagram.com"
    REQUIRED_SCOPES = (
        "instagram_business_basic",
        "instagram_business_content_publish",
    )
    TERMINAL_FAILURE_STATUSES = frozenset({"ERROR", "EXPIRED"})
    STATUS_POLL_INTERVAL_SECONDS = 5
    STATUS_POLL_ATTEMPTS = 60

    def _setting(self, name: str, default: Any = "") -> Any:
        return config.app.get(f"instagram_direct_{name}", default)

    @property
    def enabled(self) -> bool:
        return bool(self._setting("enabled", False))

    @property
    def app_id(self) -> str:
        return str(self._setting("app_id", "") or "").strip()

    @property
    def app_secret(self) -> str:
        return str(self._setting("app_secret", "") or "").strip()

    @property
    def redirect_uri(self) -> str:
        return str(
            self._setting("redirect_uri", "http://localhost:8501/")
            or "http://localhost:8501/"
        ).strip()

    @property
    def api_version(self) -> str:
        value = str(self._setting("api_version", "v26.0") or "v26.0").strip()
        return value if value.startswith("v") else f"v{value}"

    @property
    def share_to_feed(self) -> bool:
        return bool(self._setting("share_to_feed", True))

    @property
    def token_path(self) -> Path:
        credentials_dir = Path(utils.storage_dir("credentials", create=True)).resolve()
        configured_name = str(self._setting("token_file", "instagram-token.json") or "")
        filename = Path(configured_name).name
        if filename in {"", ".", ".."}:
            filename = "instagram-token.json"
        return credentials_dir / filename

    def has_client_credentials(self) -> bool:
        return bool(self.app_id and self.app_secret and self.redirect_uri)

    def is_authorized(self) -> bool:
        token = self._load_token()
        return bool(token.get("access_token") and token.get("ig_user_id"))

    def is_configured(self) -> bool:
        return self.enabled and self.has_client_credentials() and self.is_authorized()

    def begin_authorization(self) -> str:
        return OAuthStateStore("instagram").issue()

    def consume_authorization_state(self, state: str) -> bool:
        return OAuthStateStore("instagram").consume(state)

    @staticmethod
    def new_state() -> str:
        return secrets.token_urlsafe(32)

    def build_authorization_url(self, state: str) -> str:
        if not self.has_client_credentials():
            raise ValueError("Instagram Login application credentials are missing")
        if not state:
            raise ValueError("OAuth state is required")
        query = urlencode(
            {
                "client_id": self.app_id,
                "redirect_uri": self.redirect_uri,
                "response_type": "code",
                "scope": ",".join(self.REQUIRED_SCOPES),
                "force_reauth": "true",
                "enable_fb_login": "false",
                "state": state,
            }
        )
        return f"{self.AUTH_URL}?{query}"

    def exchange_code(self, code: str) -> dict:
        if not self.has_client_credentials():
            raise ValueError("Instagram Login application credentials are missing")
        if not str(code or "").strip():
            raise ValueError("Instagram OAuth authorization code is missing")

        try:
            short_response = requests.post(
                self.TOKEN_URL,
                data={
                    "client_id": self.app_id,
                    "client_secret": self.app_secret,
                    "grant_type": "authorization_code",
                    "redirect_uri": self.redirect_uri,
                    "code": str(code).strip(),
                },
                timeout=30,
            )
        except requests.RequestException:
            raise RuntimeError("Instagram authorization code exchange failed") from None
        self._raise_meta_error(short_response, "Instagram authorization code exchange")
        short_payload = short_response.json()
        if isinstance(short_payload.get("data"), list) and short_payload["data"]:
            short_payload = short_payload["data"][0]
        short_token = str(short_payload.get("access_token") or "")
        ig_user_id = str(short_payload.get("user_id") or "")
        permissions = short_payload.get("permissions") or []
        if not short_token or not ig_user_id:
            raise RuntimeError("Instagram did not return an access token and user ID")

        long_token, expires_in = self._exchange_long_lived_token(short_token)
        profile = self._get_profile(ig_user_id, long_token)
        now = time.time()
        token = {
            "access_token": long_token,
            "ig_user_id": str(profile.get("user_id") or profile.get("id") or ig_user_id),
            "username": str(profile.get("username") or ""),
            "account_type": str(profile.get("account_type") or ""),
            "permissions": permissions,
            "issued_at": now,
            "expires_at": now + max(0, expires_in),
        }
        self._save_token(token)
        return {
            "success": True,
            "authorized": True,
            "username": token["username"],
            "ig_user_id": token["ig_user_id"],
        }

    def disconnect(self) -> None:
        try:
            self.token_path.unlink(missing_ok=True)
        except OSError as exc:
            raise RuntimeError(f"failed to remove local Instagram token: {exc}") from exc

    def account_summary(self) -> dict:
        token = self._load_token()
        return {
            "authorized": bool(token.get("access_token") and token.get("ig_user_id")),
            "username": str(token.get("username") or ""),
            "account_type": str(token.get("account_type") or ""),
            "ig_user_id": str(token.get("ig_user_id") or ""),
        }

    def upload_video(self, video_path: str, *, caption: str) -> dict:
        if not self.enabled:
            return self._failure("Direct Instagram publishing is disabled")
        if not self.has_client_credentials():
            return self._failure("Instagram Login application credentials are missing")
        if not os.path.isfile(video_path):
            return self._failure(f"Video file not found: {video_path}")

        try:
            token = self._get_valid_token()
            access_token = str(token["access_token"])
            ig_user_id = str(token["ig_user_id"])
            with temporary_public_video(video_path) as video_url:
                container_response = requests.post(
                    f"{self.GRAPH_HOST}/{self.api_version}/{ig_user_id}/media",
                    data={
                        "media_type": "REELS",
                        "video_url": video_url,
                        "caption": str(caption or "")[:2200],
                        "share_to_feed": str(self.share_to_feed).lower(),
                        "is_ai_generated": "true",
                    },
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=30,
                )
                self._raise_meta_error(container_response, "Instagram container creation")
                container_id = str(container_response.json().get("id") or "").strip()
                if not container_id:
                    return self._failure("Instagram did not return a media container ID")

                status_payload = self._wait_until_ready(container_id, access_token)
                publish_response = requests.post(
                    f"{self.GRAPH_HOST}/{self.api_version}/{ig_user_id}/media_publish",
                    data={"creation_id": container_id},
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=60,
                )
                self._raise_meta_error(publish_response, "Instagram Reel publishing")
                media_id = str(publish_response.json().get("id") or "").strip()
                if not media_id:
                    return self._failure("Instagram published the Reel without a media ID")
                permalink = self._get_permalink(media_id, access_token)

            logger.success(f"Instagram Reel published: media_id={media_id}")
            return {
                "success": True,
                "platform": "instagram",
                "provider": "instagram_direct",
                "status": "published",
                "container_id": container_id,
                "media_id": media_id,
                "post_url": permalink,
                "processing_status": status_payload.get("status_code", "FINISHED"),
            }
        except (requests.RequestException, OSError, ValueError, RuntimeError) as exc:
            logger.error(f"direct Instagram upload failed: {exc}")
            return self._failure(str(exc))

    def _exchange_long_lived_token(self, short_token: str) -> tuple[str, int]:
        try:
            response = requests.get(
                f"{self.GRAPH_HOST}/access_token",
                params={
                    "grant_type": "ig_exchange_token",
                    "client_secret": self.app_secret,
                    "access_token": short_token,
                },
                timeout=30,
            )
        except requests.RequestException:
            raise RuntimeError("Instagram long-lived token exchange failed") from None
        self._raise_meta_error(response, "Instagram long-lived token exchange")
        payload = response.json()
        access_token = str(payload.get("access_token") or short_token)
        try:
            expires_in = int(payload.get("expires_in", 60 * 24 * 60 * 60))
        except (TypeError, ValueError):
            expires_in = 60 * 24 * 60 * 60
        return access_token, expires_in

    def _get_profile(self, ig_user_id: str, access_token: str) -> dict:
        response = requests.get(
            f"{self.GRAPH_HOST}/{self.api_version}/{ig_user_id}",
            params={"fields": "user_id,username,account_type"},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        )
        self._raise_meta_error(response, "Instagram profile lookup")
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    def _get_valid_token(self) -> dict:
        token = self._load_token()
        if not token.get("access_token") or not token.get("ig_user_id"):
            raise RuntimeError("Instagram account is not authorized")
        expires_at = float(token.get("expires_at") or 0)
        if not expires_at or expires_at > time.time() + 7 * 24 * 60 * 60:
            return token

        issued_at = float(token.get("issued_at") or 0)
        if issued_at and issued_at > time.time() - 24 * 60 * 60:
            return token
        try:
            response = requests.get(
                f"{self.GRAPH_HOST}/refresh_access_token",
                params={
                    "grant_type": "ig_refresh_token",
                    "access_token": str(token["access_token"]),
                },
                timeout=30,
            )
        except requests.RequestException:
            raise RuntimeError("Instagram access token refresh failed") from None
        self._raise_meta_error(response, "Instagram access token refresh")
        payload = response.json()
        refreshed = dict(token)
        refreshed["access_token"] = str(
            payload.get("access_token") or token["access_token"]
        )
        try:
            expires_in = int(payload.get("expires_in", 60 * 24 * 60 * 60))
        except (TypeError, ValueError):
            expires_in = 60 * 24 * 60 * 60
        refreshed["issued_at"] = time.time()
        refreshed["expires_at"] = time.time() + max(0, expires_in)
        self._save_token(refreshed)
        return refreshed

    def _wait_until_ready(self, container_id: str, access_token: str) -> dict:
        last_status = "IN_PROGRESS"
        for attempt in range(self.STATUS_POLL_ATTEMPTS):
            response = requests.get(
                f"{self.GRAPH_HOST}/{self.api_version}/{container_id}",
                params={"fields": "status_code,status"},
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=30,
            )
            self._raise_meta_error(response, "Instagram media status check")
            payload = response.json()
            last_status = str(payload.get("status_code") or "IN_PROGRESS").upper()
            if last_status in {"FINISHED", "PUBLISHED"}:
                return payload
            if last_status in self.TERMINAL_FAILURE_STATUSES:
                detail = payload.get("status") or last_status
                raise RuntimeError(f"Instagram media processing failed: {detail}")
            if attempt + 1 < self.STATUS_POLL_ATTEMPTS:
                time.sleep(self.STATUS_POLL_INTERVAL_SECONDS)
        raise RuntimeError(f"Instagram media processing timed out: {last_status}")

    def _get_permalink(self, media_id: str, access_token: str) -> str:
        try:
            response = requests.get(
                f"{self.GRAPH_HOST}/{self.api_version}/{media_id}",
                params={"fields": "permalink"},
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=30,
            )
            self._raise_meta_error(response, "Instagram permalink lookup")
            return str(response.json().get("permalink") or "")
        except (requests.RequestException, RuntimeError) as exc:
            logger.warning(f"failed to retrieve Instagram permalink: {exc}")
            return ""

    def _load_token(self) -> dict:
        token_path = self.token_path
        if not token_path.is_file():
            return {}
        try:
            payload = json.loads(token_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            logger.warning(f"failed to read Instagram OAuth token: {exc}")
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save_token(self, token: dict) -> None:
        token_path = self.token_path
        token_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=".instagram-token-",
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
    def _raise_meta_error(response: requests.Response, operation: str) -> None:
        if response.ok:
            return
        message = "request rejected"
        code = None
        subcode = None
        try:
            payload = response.json()
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            message = str(error.get("message") or message)
            code = error.get("code")
            subcode = error.get("error_subcode")
        except (ValueError, TypeError):
            pass
        details = [f"HTTP {response.status_code}"]
        if code is not None:
            details.append(f"code {code}")
        if subcode is not None:
            details.append(f"subcode {subcode}")
        raise RuntimeError(f"{operation} failed ({', '.join(details)}): {message}")

    @staticmethod
    def _failure(error: str) -> dict:
        return {
            "success": False,
            "platform": "instagram",
            "provider": "instagram_direct",
            "status": "failed",
            "error": error,
        }


instagram_publisher = InstagramPublisher()
