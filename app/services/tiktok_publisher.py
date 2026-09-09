"""Direct private TikTok posting through the official Content Posting API."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import requests
from loguru import logger

from app.config import config
from app.services.oauth_state import OAuthStateStore
from app.utils import utils


class TikTokPublisher:
    AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
    TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
    REVOKE_URL = "https://open.tiktokapis.com/v2/oauth/revoke/"
    CREATOR_INFO_URL = (
        "https://open.tiktokapis.com/v2/post/publish/creator_info/query/"
    )
    INIT_POST_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"
    STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"
    REQUIRED_SCOPES = ("user.info.basic", "video.publish")
    TEST_PRIVACY_LEVEL = "SELF_ONLY"
    TERMINAL_STATUSES = frozenset({"PUBLISH_COMPLETE", "FAILED"})
    MIN_CHUNK_SIZE = 5 * 1024 * 1024
    MAX_CHUNK_SIZE = 64 * 1024 * 1024
    MAX_FINAL_CHUNK_SIZE = 128 * 1024 * 1024
    MAX_VIDEO_SIZE = 4 * 1024 * 1024 * 1024
    STATUS_POLL_INTERVAL_SECONDS = 2
    STATUS_POLL_ATTEMPTS = 30

    def __init__(self) -> None:
        self._pending_code_verifier = ""

    def _setting(self, name: str, default: Any = "") -> Any:
        return config.app.get(f"tiktok_direct_{name}", default)

    @property
    def enabled(self) -> bool:
        return bool(self._setting("enabled", False))

    @property
    def client_key(self) -> str:
        return str(self._setting("client_key", "") or "").strip()

    @property
    def client_secret(self) -> str:
        return str(self._setting("client_secret", "") or "").strip()

    @property
    def redirect_uri(self) -> str:
        return str(
            self._setting("redirect_uri", "https://localhost:8501/")
            or "https://localhost:8501/"
        ).strip()

    @property
    def test_mode(self) -> bool:
        return bool(self._setting("test_mode", True))

    @property
    def token_path(self) -> Path:
        credentials_dir = Path(utils.storage_dir("credentials", create=True)).resolve()
        configured_name = str(self._setting("token_file", "tiktok-token.json") or "")
        filename = Path(configured_name).name
        if filename in {"", ".", ".."}:
            filename = "tiktok-token.json"
        return credentials_dir / filename

    def has_client_credentials(self) -> bool:
        return bool(self.client_key and self.client_secret and self.redirect_uri)

    def is_authorized(self) -> bool:
        token = self._load_token()
        return bool(token.get("access_token") and token.get("refresh_token"))

    def is_configured(self) -> bool:
        return self.enabled and self.has_client_credentials() and self.is_authorized()

    def begin_authorization(self) -> str:
        verifier = self._new_code_verifier()
        return OAuthStateStore("tiktok").issue({"code_verifier": verifier})

    def build_authorization_url(self, state: str) -> str:
        if not self.has_client_credentials():
            raise ValueError("TikTok OAuth client credentials are missing")
        self._validate_redirect_uri(self.redirect_uri)
        payload = OAuthStateStore("tiktok").peek(state)
        verifier = str(payload.get("code_verifier") or "")
        if not verifier:
            raise ValueError("TikTok OAuth state or PKCE verifier is missing")
        challenge = hashlib.sha256(verifier.encode("ascii")).hexdigest()
        query = urlencode(
            {
                "client_key": self.client_key,
                "scope": ",".join(self.REQUIRED_SCOPES),
                "response_type": "code",
                "redirect_uri": self.redirect_uri,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{self.AUTH_URL}?{query}"

    def consume_authorization_state(self, state: str) -> bool:
        payload = OAuthStateStore("tiktok").consume_payload(state)
        verifier = str(payload.get("code_verifier") or "")
        self._pending_code_verifier = verifier
        return bool(verifier)

    def exchange_code(self, code: str) -> dict:
        if not self.has_client_credentials():
            raise ValueError("TikTok OAuth client credentials are missing")
        self._validate_redirect_uri(self.redirect_uri)
        verifier = self._pending_code_verifier
        self._pending_code_verifier = ""
        if not verifier:
            raise ValueError("TikTok OAuth PKCE verifier is missing or expired")
        if not str(code or "").strip():
            raise ValueError("TikTok OAuth authorization code is missing")

        try:
            response = requests.post(
                self.TOKEN_URL,
                data={
                    "client_key": self.client_key,
                    "client_secret": self.client_secret,
                    "code": str(code).strip(),
                    "grant_type": "authorization_code",
                    "redirect_uri": self.redirect_uri,
                    "code_verifier": verifier,
                },
                timeout=30,
            )
        except requests.RequestException as exc:
            raise RuntimeError(self._request_failure("TikTok authorization", exc)) from None
        token = self._token_payload(response, "TikTok authorization")
        self._save_token(self._normalize_token(token))
        return {
            "success": True,
            "authorized": True,
            "open_id": str(token.get("open_id") or ""),
        }

    def disconnect(self) -> None:
        token = self._load_token()
        access_token = str(token.get("access_token") or "")
        if access_token and self.has_client_credentials():
            try:
                response = requests.post(
                    self.REVOKE_URL,
                    data={
                        "client_key": self.client_key,
                        "client_secret": self.client_secret,
                        "token": access_token,
                    },
                    timeout=15,
                )
                if not response.ok:
                    logger.warning(
                        f"TikTok token revocation failed (HTTP {response.status_code})"
                    )
            except requests.RequestException as exc:
                logger.warning(self._request_failure("TikTok token revocation", exc))
        try:
            self.token_path.unlink(missing_ok=True)
        except OSError as exc:
            raise RuntimeError(f"failed to remove local TikTok token: {exc}") from exc

    def account_summary(self) -> dict:
        token = self._load_token()
        return {
            "authorized": bool(token.get("access_token") and token.get("refresh_token")),
            "open_id": str(token.get("open_id") or ""),
            "scope": str(token.get("scope") or ""),
        }

    def get_creator_info(self) -> dict:
        access_token = self._get_access_token()
        return self._api_post(
            self.CREATOR_INFO_URL,
            access_token=access_token,
            body={},
            operation="TikTok creator lookup",
        )

    def publish_video(
        self,
        video_path: str,
        *,
        title: str,
        privacy_level: str,
        allow_comment: bool = False,
        allow_duet: bool = False,
        allow_stitch: bool = False,
        brand_content: bool = False,
        brand_organic: bool = False,
        duration_seconds: float | None = None,
    ) -> dict:
        if not self.enabled:
            return self._failure("Direct TikTok publishing is disabled")
        if not self.has_client_credentials():
            return self._failure("TikTok OAuth client credentials are missing")
        if not os.path.isfile(video_path):
            return self._failure(f"Video file not found: {video_path}")

        try:
            file_size = os.path.getsize(video_path)
            self._validate_file_size(file_size)
            access_token = self._get_access_token()
            creator = self._api_post(
                self.CREATOR_INFO_URL,
                access_token=access_token,
                body={},
                operation="TikTok creator lookup",
            )
            privacy = str(privacy_level or "").strip().upper()
            available_privacy = {
                str(value).upper()
                for value in creator.get("privacy_level_options", [])
                if value
            }
            if privacy not in available_privacy:
                raise ValueError("Selected TikTok privacy level is unavailable")
            if self.test_mode and privacy != self.TEST_PRIVACY_LEVEL:
                raise ValueError("TikTok test mode only allows SELF_ONLY posts")
            if brand_content and privacy == self.TEST_PRIVACY_LEVEL:
                raise ValueError("Branded TikTok content cannot use private visibility")

            duration = (
                float(duration_seconds)
                if duration_seconds is not None
                else self._probe_video_duration(video_path)
            )
            max_duration = float(creator.get("max_video_post_duration_sec") or 0)
            if duration <= 0:
                raise ValueError("Could not determine the TikTok video duration")
            if max_duration > 0 and duration > max_duration:
                raise ValueError(
                    f"Video duration exceeds the TikTok account limit of {max_duration:g} seconds"
                )

            chunk_size, chunk_count = self._chunk_plan(file_size)
            post_info = {
                "title": self._truncate_utf16(str(title or ""), 2200),
                "privacy_level": privacy,
                "disable_comment": bool(
                    creator.get("comment_disabled", False) or not allow_comment
                ),
                "disable_duet": bool(
                    creator.get("duet_disabled", False) or not allow_duet
                ),
                "disable_stitch": bool(
                    creator.get("stitch_disabled", False) or not allow_stitch
                ),
                "brand_content_toggle": bool(brand_content),
                "brand_organic_toggle": bool(brand_organic),
                "is_aigc": True,
            }
            initialized = self._api_post(
                self.INIT_POST_URL,
                access_token=access_token,
                body={
                    "post_info": post_info,
                    "source_info": {
                        "source": "FILE_UPLOAD",
                        "video_size": file_size,
                        "chunk_size": chunk_size,
                        "total_chunk_count": chunk_count,
                    },
                },
                operation="TikTok post initialization",
            )
            publish_id = str(initialized.get("publish_id") or "").strip()
            upload_url = str(initialized.get("upload_url") or "").strip()
            if not publish_id or not upload_url:
                raise RuntimeError("TikTok did not return a publish ID and upload URL")

            self._upload_file(video_path, upload_url, file_size, chunk_size, chunk_count)
            status = self._wait_for_status(publish_id, access_token)
            if status.get("status") == "FAILED":
                return self._failure(
                    f"TikTok post failed: {status.get('fail_reason') or 'unknown reason'}",
                    publish_id=publish_id,
                    processing_status="FAILED",
                )

            processing_status = str(status.get("status") or "PROCESSING_UPLOAD")
            logger.success(
                f"TikTok upload accepted: publish_id={publish_id}, status={processing_status}"
            )
            return {
                "success": True,
                "platform": "tiktok",
                "provider": "tiktok_direct",
                "status": (
                    "published" if processing_status == "PUBLISH_COMPLETE" else "processing"
                ),
                "publish_id": publish_id,
                "processing_status": processing_status,
            }
        except requests.RequestException as exc:
            message = self._request_failure("TikTok publishing", exc)
            logger.error(message)
            return self._failure(message)
        except (OSError, ValueError, RuntimeError) as exc:
            logger.error(f"direct TikTok publishing failed: {exc}")
            return self._failure(str(exc))

    def get_post_status(self, publish_id: str) -> dict:
        publish_id = str(publish_id or "").strip()
        if not publish_id:
            raise ValueError("TikTok publish ID is missing")
        return self._fetch_status(publish_id, self._get_access_token())

    def _get_access_token(self) -> str:
        token = self._load_token()
        access_token = str(token.get("access_token") or "")
        expires_at = float(token.get("expires_at") or 0)
        if access_token and expires_at > time.time() + 60:
            return access_token
        refresh_token = str(token.get("refresh_token") or "")
        if not refresh_token:
            raise RuntimeError("TikTok account is not authorized")

        try:
            response = requests.post(
                self.TOKEN_URL,
                data={
                    "client_key": self.client_key,
                    "client_secret": self.client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
                timeout=30,
            )
        except requests.RequestException as exc:
            raise RuntimeError(self._request_failure("TikTok token refresh", exc)) from None
        refreshed = self._token_payload(response, "TikTok token refresh")
        normalized = self._normalize_token(refreshed)
        self._save_token(normalized)
        return str(normalized["access_token"])

    def _api_post(
        self,
        url: str,
        *,
        access_token: str,
        body: dict,
        operation: str,
    ) -> dict:
        try:
            response = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json; charset=UTF-8",
                },
                json=body,
                timeout=30,
            )
        except requests.RequestException as exc:
            raise RuntimeError(self._request_failure(operation, exc)) from None
        return self._api_payload(response, operation)

    def _fetch_status(self, publish_id: str, access_token: str) -> dict:
        return self._api_post(
            self.STATUS_URL,
            access_token=access_token,
            body={"publish_id": publish_id},
            operation="TikTok post status lookup",
        )

    def _wait_for_status(self, publish_id: str, access_token: str) -> dict:
        last_status = {"status": "PROCESSING_UPLOAD"}
        for attempt in range(self.STATUS_POLL_ATTEMPTS):
            last_status = self._fetch_status(publish_id, access_token)
            if str(last_status.get("status") or "") in self.TERMINAL_STATUSES:
                return last_status
            if attempt + 1 < self.STATUS_POLL_ATTEMPTS:
                time.sleep(self.STATUS_POLL_INTERVAL_SECONDS)
        return last_status

    @classmethod
    def _chunk_plan(cls, file_size: int) -> tuple[int, int]:
        if file_size <= 0:
            raise ValueError("TikTok video file is empty")
        if file_size <= cls.MAX_CHUNK_SIZE:
            return file_size, 1
        chunk_size = cls.MAX_CHUNK_SIZE
        chunk_count = max(1, file_size // chunk_size)
        final_size = file_size - (chunk_count - 1) * chunk_size
        if final_size > cls.MAX_FINAL_CHUNK_SIZE:
            chunk_count += 1
        return chunk_size, chunk_count

    def _upload_file(
        self,
        video_path: str,
        upload_url: str,
        file_size: int,
        chunk_size: int,
        chunk_count: int,
    ) -> None:
        parsed = urlparse(upload_url)
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not (
            hostname == "tiktokapis.com" or hostname.endswith(".tiktokapis.com")
        ):
            raise RuntimeError("TikTok returned an invalid upload URL")

        mime_type = mimetypes.guess_type(video_path)[0] or "video/mp4"
        if mime_type not in {"video/mp4", "video/quicktime", "video/webm"}:
            raise ValueError("TikTok supports MP4, MOV, or WebM video files")

        with open(video_path, "rb") as video_file:
            offset = 0
            for index in range(chunk_count):
                remaining = file_size - offset
                current_size = remaining if index == chunk_count - 1 else chunk_size
                chunk = video_file.read(current_size)
                if len(chunk) != current_size:
                    raise OSError("Could not read the complete TikTok video chunk")
                end = offset + current_size - 1
                response = requests.put(
                    upload_url,
                    headers={
                        "Content-Type": mime_type,
                        "Content-Length": str(current_size),
                        "Content-Range": f"bytes {offset}-{end}/{file_size}",
                    },
                    data=chunk,
                    timeout=600,
                )
                expected_status = 201 if index == chunk_count - 1 else 206
                if response.status_code != expected_status:
                    raise RuntimeError(
                        f"TikTok video transfer failed (HTTP {response.status_code})"
                    )
                offset = end + 1

    @staticmethod
    def _probe_video_duration(video_path: str) -> float:
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return 0.0
        try:
            result = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    video_path,
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            return float(result.stdout.strip()) if result.returncode == 0 else 0.0
        except (OSError, subprocess.SubprocessError, ValueError):
            return 0.0

    @staticmethod
    def _new_code_verifier() -> str:
        # token_urlsafe uses only RFC 3986 unreserved characters and stays below 128 chars.
        import secrets

        return secrets.token_urlsafe(64)

    @staticmethod
    def _validate_redirect_uri(redirect_uri: str) -> None:
        parsed = urlparse(str(redirect_uri or ""))
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "TikTok OAuth redirect URI must be an exact HTTPS URL without query or fragment"
            )

    @staticmethod
    def _truncate_utf16(value: str, limit: int) -> str:
        encoded = value.encode("utf-16-le")
        if len(encoded) <= limit * 2:
            return value
        truncated = encoded[: limit * 2]
        while truncated:
            try:
                return truncated.decode("utf-16-le")
            except UnicodeDecodeError:
                truncated = truncated[:-2]
        return ""

    @classmethod
    def _validate_file_size(cls, file_size: int) -> None:
        if file_size <= 0:
            raise ValueError("TikTok video file is empty")
        if file_size > cls.MAX_VIDEO_SIZE:
            raise ValueError("TikTok video exceeds the 4 GB upload limit")

    @staticmethod
    def _token_payload(response: requests.Response, operation: str) -> dict:
        try:
            payload = response.json()
        except ValueError:
            raise RuntimeError(f"{operation} returned an invalid response") from None
        if not response.ok or payload.get("error"):
            code = str(payload.get("error") or f"HTTP {response.status_code}")
            detail = str(payload.get("error_description") or "request rejected")
            raise RuntimeError(f"{operation} failed ({code}): {detail}")
        if not payload.get("access_token") or not payload.get("refresh_token"):
            raise RuntimeError(f"{operation} did not return refreshable tokens")
        return payload

    @staticmethod
    def _api_payload(response: requests.Response, operation: str) -> dict:
        try:
            payload = response.json()
        except ValueError:
            raise RuntimeError(f"{operation} returned an invalid response") from None
        error = payload.get("error") if isinstance(payload, dict) else None
        error = error if isinstance(error, dict) else {}
        code = str(error.get("code") or "")
        if not response.ok or code not in {"", "ok"}:
            safe_code = code or f"HTTP {response.status_code}"
            message = str(error.get("message") or "request rejected")
            raise RuntimeError(f"{operation} failed ({safe_code}): {message}")
        data = payload.get("data") if isinstance(payload, dict) else None
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _normalize_token(token: dict) -> dict:
        now = time.time()
        try:
            expires_in = max(0, int(token.get("expires_in", 86400)))
        except (TypeError, ValueError):
            expires_in = 86400
        try:
            refresh_expires_in = max(
                0, int(token.get("refresh_expires_in", 365 * 24 * 60 * 60))
            )
        except (TypeError, ValueError):
            refresh_expires_in = 365 * 24 * 60 * 60
        return {
            "access_token": str(token.get("access_token") or ""),
            "refresh_token": str(token.get("refresh_token") or ""),
            "open_id": str(token.get("open_id") or ""),
            "scope": str(token.get("scope") or ""),
            "token_type": str(token.get("token_type") or "Bearer"),
            "expires_at": now + expires_in,
            "refresh_expires_at": now + refresh_expires_in,
        }

    def _load_token(self) -> dict:
        if not self.token_path.is_file():
            return {}
        try:
            payload = json.loads(self.token_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            logger.warning(f"failed to read TikTok OAuth token: {exc}")
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save_token(self, token: dict) -> None:
        token_path = self.token_path
        token_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=".tiktok-token-",
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
    def _request_failure(operation: str, error: requests.RequestException) -> str:
        response = getattr(error, "response", None)
        status_code = getattr(response, "status_code", None)
        if status_code is not None:
            return f"{operation} failed (HTTP {status_code})"
        return f"{operation} failed ({type(error).__name__})"

    @staticmethod
    def _failure(error: str, **extra: Any) -> dict:
        return {
            "success": False,
            "platform": "tiktok",
            "provider": "tiktok_direct",
            "status": "failed",
            "error": error,
            **extra,
        }


tiktok_publisher = TikTokPublisher()
