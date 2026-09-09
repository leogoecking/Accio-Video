import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.oauth_state import OAuthStateStore


class TestOAuthStateStore(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temp_dir.name) / "instagram-oauth-state.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def _path_patch(self):
        state_path = self.state_path
        return patch.object(
            OAuthStateStore,
            "path",
            new_callable=lambda: property(lambda _self: state_path),
        )

    def test_issued_state_can_be_consumed_once(self):
        store = OAuthStateStore("instagram")

        with self._path_patch():
            state = store.issue()
            self.assertTrue(self.state_path.is_file())
            self.assertEqual(os.stat(self.state_path).st_mode & 0o777, 0o600)
            self.assertTrue(store.consume(state))
            self.assertFalse(store.consume(state))
            self.assertFalse(self.state_path.exists())

    def test_mismatched_state_does_not_invalidate_pending_callback(self):
        store = OAuthStateStore("youtube")

        with self._path_patch():
            state = store.issue()
            self.assertFalse(store.consume("different-state"))
            self.assertTrue(store.consume(state))

    def test_expired_state_is_rejected_and_removed(self):
        store = OAuthStateStore("instagram")
        self.state_path.write_text(
            json.dumps(
                {
                    "state": "expired-state",
                    "created_at": time.time() - store.MAX_AGE_SECONDS - 1,
                }
            ),
            encoding="utf-8",
        )

        with self._path_patch():
            self.assertFalse(store.consume("expired-state"))
            self.assertFalse(self.state_path.exists())


if __name__ == "__main__":
    unittest.main()
