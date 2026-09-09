"""Short-lived, file-backed OAuth state validation for browser callbacks."""

from __future__ import annotations

import hmac
import json
import os
import secrets
import tempfile
import time
from pathlib import Path

from loguru import logger

from app.utils import utils


class OAuthStateStore:
    """Persist one pending OAuth state so callbacks can open in another tab."""

    MAX_AGE_SECONDS = 15 * 60

    def __init__(self, provider: str):
        normalized = "".join(
            character
            for character in str(provider or "").lower()
            if character.isalnum() or character in {"-", "_"}
        )
        if not normalized:
            raise ValueError("OAuth provider name is required")
        self.provider = normalized

    @property
    def path(self) -> Path:
        credentials_dir = Path(utils.storage_dir("credentials", create=True)).resolve()
        return credentials_dir / f"{self.provider}-oauth-state.json"

    def issue(self, extra: dict | None = None) -> str:
        state = secrets.token_urlsafe(32)
        payload = dict(extra or {})
        payload.update({"state": state, "created_at": time.time()})
        self._save(payload)
        return state

    def consume(self, candidate: str) -> bool:
        return bool(self.consume_payload(candidate))

    def peek(self, candidate: str) -> dict:
        """Return a valid pending payload without consuming it."""
        return self._validated_payload(candidate)

    def consume_payload(self, candidate: str) -> dict:
        """Consume and return a valid pending payload."""
        payload = self._validated_payload(candidate)
        if payload:
            self.clear()
        return payload

    def _validated_payload(self, candidate: str) -> dict:
        value = str(candidate or "").strip()
        if not value:
            return {}

        payload = self._load()
        expected = str(payload.get("state") or "")
        try:
            created_at = float(payload.get("created_at") or 0)
        except (TypeError, ValueError):
            created_at = 0

        if not expected or created_at <= 0:
            return {}
        if time.time() - created_at > self.MAX_AGE_SECONDS:
            self.clear()
            return {}
        if not hmac.compare_digest(value, expected):
            return {}
        return payload

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(f"failed to clear {self.provider} OAuth state: {exc}")

    def _load(self) -> dict:
        if not self.path.is_file():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            logger.warning(f"failed to read {self.provider} OAuth state: {exc}")
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save(self, payload: dict) -> None:
        state_path = self.path
        state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.provider}-oauth-state-",
            suffix=".tmp",
            dir=str(state_path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as state_file:
                json.dump(payload, state_file)
                state_file.flush()
                os.fsync(state_file.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, state_path)
            os.chmod(state_path, 0o600)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
