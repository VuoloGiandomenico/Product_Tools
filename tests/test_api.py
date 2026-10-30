"""Tests for api.py: API-key authentication and error sanitisation (no network)."""
import importlib
import os
import sys
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

# api.py calls load_dotenv(override=True) at import time: never load the real .env in tests.
with patch("dotenv.load_dotenv"):
    sys.modules.pop("api", None)
    api = importlib.import_module("api")

PAYLOAD = {"model_name": "m", "issue_type": "Story", "input_text": "idea"}
URL = "/api/v1/generate-issue"


class ApiAuthTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(api.app)

    def test_missing_header_is_rejected(self):
        with patch.dict(os.environ, {"API_KEY": "k"}, clear=False):
            resp = self.client.post(URL, json=PAYLOAD)
        self.assertIn(resp.status_code, (401, 403))

    def test_wrong_key_is_401(self):
        with patch.dict(os.environ, {"API_KEY": "k"}, clear=False):
            resp = self.client.post(URL, json=PAYLOAD, headers={"X-API-Key": "nope"})
        self.assertEqual(resp.status_code, 401)

    def test_unconfigured_server_key_fails_closed(self):
        env = {k: v for k, v in os.environ.items() if k != "API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            resp = self.client.post(URL, json=PAYLOAD, headers={"X-API-Key": ""})
        self.assertNotEqual(resp.status_code, 200)

    def test_valid_key_reaches_endpoint(self):
        with patch.dict(os.environ, {"API_KEY": "k"}, clear=False), \
                patch.object(api, "generate_issue_with_ollama", return_value="Generated text"):
            resp = self.client.post(URL, json=PAYLOAD, headers={"X-API-Key": "k"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"generated_description": "Generated text"})


class ApiErrorSanitisationTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(api.app)

    def test_internal_error_details_are_not_leaked(self):
        secret_detail = "Exception: token=SUPERSECRET at /Users/someone/.env line 3"
        with patch.dict(os.environ, {"API_KEY": "k"}, clear=False), \
                patch.object(api, "generate_issue_with_ollama", return_value=secret_detail), \
                self.assertLogs("po_tools.api", level="ERROR") as logs:
            resp = self.client.post(URL, json=PAYLOAD, headers={"X-API-Key": "k"})
        self.assertEqual(resp.status_code, 500)
        self.assertNotIn("SUPERSECRET", resp.text)
        self.assertIn("ref", resp.json()["detail"])
        self.assertTrue(any("SUPERSECRET" in line for line in logs.output))  # kept server-side


if __name__ == "__main__":
    unittest.main()

