"""Public demo connections stay on the server-configured HTTP backend."""

from __future__ import annotations

import os
from pathlib import Path
import unittest
from unittest.mock import patch

try:
    from apps.buyer_ui.app import _http_client
    from streamlit.testing.v1 import AppTest
except ImportError:
    AppTest = None


APP = Path(__file__).resolve().parents[2] / "apps/buyer_ui/app.py"
API_URL = "http://127.0.0.1:18000"


@unittest.skipUnless(AppTest is not None, "Streamlit is needed for hosted configuration tests")
class HostedConnectionTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {
            "BUYER_LOCK_CONNECTION": "true", "BUYER_API_URL": API_URL,
            "BUYER_API_TOKEN": "", "BUYER_UI_MODE": "mock",
            "BUYER_SNAPSHOT_ID": "", "BUYER_RUN_ID": "", "BUYER_MOCK_QUALITY": "ready",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_locked_client_rejects_another_destination_without_token(self):
        for url in ("http://169.254.169.254", API_URL + ".example.test", "https://example.test"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                _http_client(url)
        self.assertEqual(_http_client(API_URL + "/").base_url, API_URL + "/v1")

    def test_local_configuration_keeps_editable_destination(self):
        with patch.dict(os.environ, {"BUYER_LOCK_CONNECTION": "false"}):
            self.assertEqual(_http_client("http://localhost:19000").base_url,
                             "http://localhost:19000/v1")

    def test_token_still_restricts_destination_when_public_lock_is_off(self):
        with patch.dict(os.environ, {"BUYER_LOCK_CONNECTION": "false", "BUYER_API_TOKEN": "test-only"}):
            with self.assertRaises(ValueError):
                _http_client("https://example.test")

    def test_hosted_controls_use_http_and_are_disabled(self):
        app = AppTest.from_file(str(APP), default_timeout=15).run()
        self.assertFalse(list(app.exception))
        self.assertTrue(app.selectbox(key="connection_mode").disabled)
        self.assertEqual(app.selectbox(key="connection_mode").value, "http")
        self.assertTrue(app.text_input(key="connection_url").disabled)
        self.assertEqual(app.session_state["client_mode"], "http")
        self.assertEqual(app.session_state["client"].base_url, API_URL + "/v1")
        self.assertFalse(any("Демо: имитация API" in str(item.value) for item in app.caption))

    def test_modified_widget_state_cannot_redirect_or_switch_to_mock(self):
        app = AppTest.from_file(str(APP), default_timeout=15).run()
        app.session_state["connection_mode"] = "mock"
        app.session_state["connection_url"] = "http://169.254.169.254"
        app.run()
        self.assertFalse(list(app.exception))
        self.assertEqual(app.session_state["client_mode"], "http")
        self.assertEqual(app.session_state["client"].base_url, API_URL + "/v1")

    def test_local_mock_remains_available(self):
        with patch.dict(os.environ, {"BUYER_LOCK_CONNECTION": "false"}):
            app = AppTest.from_file(str(APP), default_timeout=15).run()
        self.assertFalse(list(app.exception))
        self.assertFalse(app.selectbox(key="connection_mode").disabled)
        self.assertEqual(app.session_state["client_mode"], "mock")
        self.assertTrue(any("Демо: имитация API" in str(item.value) for item in app.caption))
