"""Tests for utils/connection_checks.py (no network: requests.get is mocked)."""
import unittest
from unittest.mock import MagicMock, patch

import requests

from utils import connection_checks as cc

BEARER = "Personal Access Token (Bearer PAT)"
BASIC = "Jira Cloud/Server Basic (Email/User + Token)"


def _resp(status, payload=None):
    r = MagicMock(status_code=status)
    r.json.return_value = payload or {}
    return r


class JiraCheckTests(unittest.TestCase):
    def test_empty_config_is_not_called(self):
        with patch.object(cc.requests, "get") as get:
            self.assertEqual(cc.check_jira_connection("", "t", BEARER, ""), (False, "Server URL or Token empty"))
            get.assert_not_called()

    def test_success_uses_bearer_and_strips_prefix(self):
        with patch.object(cc.requests, "get", return_value=_resp(200, {"displayName": "Ana"})) as get:
            ok, msg = cc.check_jira_connection("https://jira.x/", "Bearer abc ", BEARER, "")
        self.assertTrue(ok)
        self.assertEqual(msg, "Connected as `Ana`")
        url = get.call_args.args[0]
        self.assertEqual(url, "https://jira.x/rest/api/2/myself")
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer abc")
        self.assertIsNone(get.call_args.kwargs["auth"])
        self.assertIn("timeout", get.call_args.kwargs)

    def test_basic_auth(self):
        with patch.object(cc.requests, "get", return_value=_resp(200, {})) as get:
            cc.check_jira_connection("https://jira.x", "tok", BASIC, " me@x.com ")
        self.assertEqual(get.call_args.kwargs["auth"], ("me@x.com", "tok"))
        self.assertNotIn("Authorization", get.call_args.kwargs["headers"])

    def test_http_error(self):
        with patch.object(cc.requests, "get", return_value=_resp(401)):
            self.assertEqual(cc.check_jira_connection("https://j", "t", BEARER, ""),
                             (False, "Auth Failed (HTTP 401)"))

    def test_network_error(self):
        with patch.object(cc.requests, "get", side_effect=requests.ConnectionError("boom")):
            ok, msg = cc.check_jira_connection("https://j", "t", BEARER, "")
        self.assertFalse(ok)
        self.assertIn("Connection Failed", msg)

    def test_non_json_body(self):
        r = _resp(200)
        r.json.side_effect = ValueError("not json")
        with patch.object(cc.requests, "get", return_value=r):
            self.assertEqual(cc.check_jira_connection("https://j", "t", BEARER, ""),
                             (True, "Connected as `User`"))


class ConfluenceCheckTests(unittest.TestCase):
    def test_cloud_url_gets_wiki_suffix(self):
        self.assertEqual(cc.confluence_base_url("https://acme.atlassian.net/"), "https://acme.atlassian.net/wiki")
        self.assertEqual(cc.confluence_base_url("https://acme.atlassian.net/wiki"), "https://acme.atlassian.net/wiki")
        self.assertEqual(cc.confluence_base_url("https://conf.corp/confluence/"), "https://conf.corp/confluence")

    def test_success(self):
        with patch.object(cc.requests, "get", return_value=_resp(200)) as get:
            self.assertEqual(cc.check_confluence_connection("https://acme.atlassian.net", "t", BEARER, ""),
                             (True, "Connected successfully"))
        self.assertEqual(get.call_args.args[0], "https://acme.atlassian.net/wiki/rest/api/content")
        self.assertEqual(get.call_args.kwargs["params"], {"limit": 1})

    def test_http_error(self):
        with patch.object(cc.requests, "get", return_value=_resp(403)):
            self.assertEqual(cc.check_confluence_connection("https://c", "t", BEARER, ""),
                             (False, "Auth Failed (HTTP 403)"))


if __name__ == "__main__":
    unittest.main()

