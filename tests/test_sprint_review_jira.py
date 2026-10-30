"""Tests for utils/sprint_review_jira.py (Sprint Review Jira ingestion, no network)."""
import unittest
from unittest.mock import MagicMock, patch

import requests

from utils import sprint_review_jira as srj


def _resp(status=200, payload=None, text="", json_error=False):
    r = MagicMock(status_code=status, text=text)
    if json_error:
        r.json.side_effect = ValueError("not json")
    else:
        r.json.return_value = payload or {}
    return r


def _issue(key, type_name="Story", status="In Review", epic_cf=None, parent=None, labels=("Backend_Team",),
           fix=("Release 1.2.3",), assignee="Ana"):
    fields = {
        "summary": f"Summary {key}",
        "status": {"name": status},
        "issuetype": {"name": type_name},
        "fixVersions": [{"name": f} for f in fix],
        "labels": list(labels),
        "assignee": {"displayName": assignee} if assignee else None,
    }
    if epic_cf:
        fields["customfield_10014"] = epic_cf
    if parent:
        fields["parent"] = parent
    return {"key": key, "fields": fields}


class IssueTypeSelectionTests(unittest.TestCase):
    def test_inc_all_selects_every_allowed_type(self):
        self.assertEqual(srj.selected_issue_types({"inc_all": True}), sorted(srj.ALLOWED_JIRA_TYPES))

    def test_flags_are_merged_and_deduplicated(self):
        flags = {"inc_task": True, "inc_tech": True, "inc_story": True, "inc_bug": False}
        self.assertEqual(srj.selected_issue_types(flags), ["Epic", "Story", "Sub-task", "Task"])

    def test_nothing_selected(self):
        self.assertEqual(srj.selected_issue_types({}), [])


class JqlTests(unittest.TestCase):
    def test_numeric_sprint_id(self):
        self.assertEqual(srj.build_sprint_jql("123", True, False, ["Bug"]), "sprint = 123 AND issuetype in ('Bug')")

    def test_named_sprint_unresolved(self):
        jql = srj.build_sprint_jql("Sprint 14", True, True, ["Bug", "Story"])
        self.assertEqual(jql, "sprint = 'Sprint 14' AND resolution is empty AND issuetype in ('Bug', 'Story')")

    def test_no_types_matches_nothing(self):
        self.assertTrue(srj.build_sprint_jql("1", True, False, []).endswith("issuetype in ('')"))

    def test_raw_jql_is_passed_through(self):
        self.assertEqual(srj.build_sprint_jql("project = X", False, True, ["Bug"]), "project = X")


class ClassifyIssueTypeTests(unittest.TestCase):
    def test_mapping(self):
        cases = {
            "Technical Sub-task": "Technical Sub-task", "Story": "User Story", "Bug": "Bug",
            "Improvement": "Improvement", "Performance work": "Technical Task", "Sub-task": "Sub-task",
            "Task": "Task", "Epic": "Technical Task",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(srj.classify_issue_type(raw), expected)

    def test_unknown_type_kept_when_all_types_included(self):
        self.assertEqual(srj.classify_issue_type("Epic", include_all_types=True), "Epic")


class IssueToRowTests(unittest.TestCase):
    def test_row_columns_and_values(self):
        row = srj.issue_to_row(_issue("P-1", epic_cf="P-100"), include_raw_status=True)
        self.assertEqual(row["Key"], "P-1")
        self.assertEqual(row["Status"], "In Progress")
        self.assertEqual(row["Raw Status"], "In Review")
        self.assertEqual(row["Epic"], "P-100")
        self.assertEqual(row["Type"], "User Story")
        self.assertEqual(row["Labels"], "Backend_Team")
        self.assertEqual(row["Assignee"], "Ana")
        self.assertEqual((row["Sprint Review"], row["Release Notes"], row["Demo"], row["Outlook"]),
                         (True, True, False, ""))

    def test_parent_epic_with_summary(self):
        parent = {"key": "P-9", "fields": {"summary": "Checkout"}}
        self.assertEqual(srj.issue_to_row(_issue("P-2", parent=parent))["Epic"], "P-9 - Checkout")

    def test_unassigned_and_no_epic(self):
        row = srj.issue_to_row(_issue("P-3", assignee=None))
        self.assertEqual((row["Assignee"], row["Epic"]), ("Unassigned", "-"))
        self.assertNotIn("Raw Status", row)


class FetchTests(unittest.TestCase):
    def test_missing_config_raises(self):
        with self.assertRaises(srj.JiraFetchError):
            srj.fetch_sprint_review_dataset("", "t", "1")

    def test_http_error_raises_with_status(self):
        with patch.object(srj.requests, "get", return_value=_resp(401, text="denied")):
            with self.assertRaises(srj.JiraFetchError) as ctx:
                srj.fetch_sprint_review_dataset("https://j", "t", "1", issue_types=["Bug"])
        self.assertIn("401", ctx.exception.message)
        self.assertIsNone(ctx.exception.body_preview)

    def test_non_json_response_exposes_preview(self):
        with patch.object(srj.requests, "get", return_value=_resp(200, text="<html>SSO</html>", json_error=True)):
            with self.assertRaises(srj.JiraFetchError) as ctx:
                srj.fetch_sprint_review_dataset("https://j", "t", "1")
        self.assertEqual(ctx.exception.body_preview, "<html>SSO</html>")

    def test_network_error_raises(self):
        with patch.object(srj.requests, "get", side_effect=requests.ConnectionError("down")):
            with self.assertRaises(srj.JiraFetchError):
                srj.fetch_sprint_review_dataset("https://j", "t", "1")

    def test_no_issues_returns_empty_dataframe(self):
        with patch.object(srj.requests, "get", return_value=_resp(200, {"issues": []})):
            self.assertTrue(srj.fetch_sprint_review_dataset("https://j", "t", "1").empty)

    def test_success_resolves_epic_summaries(self):
        search = _resp(200, {"issues": [_issue("P-1", epic_cf="P-100"), _issue("P-2")]})
        epics = _resp(200, {"issues": [{"key": "P-100", "fields": {"summary": "Payments"}}]})
        with patch.object(srj.requests, "get", side_effect=[search, epics]) as get:
            df = srj.fetch_sprint_review_dataset("https://j/", "Bearer t", "Sprint 1",
                                                 issue_types=["Story"], include_raw_status=True)
        self.assertEqual(df["Epic"].tolist(), ["P-100 - Payments", "-"])
        first_call = get.call_args_list[0]
        self.assertEqual(first_call.args[0], "https://j/rest/api/2/search")
        self.assertEqual(first_call.kwargs["params"]["jql"], "sprint = 'Sprint 1' AND issuetype in ('Story')")
        self.assertEqual(first_call.kwargs["headers"]["Authorization"], "Bearer t")
        self.assertEqual(get.call_args_list[1].kwargs["params"]["jql"], "key in ('P-100')")

    def test_epic_lookup_failure_keeps_keys(self):
        search = _resp(200, {"issues": [_issue("P-1", epic_cf="P-100")]})
        with patch.object(srj.requests, "get", side_effect=[search, requests.Timeout("slow")]), \
                self.assertLogs("utils.sprint_review_jira", level="WARNING"):
            df = srj.fetch_sprint_review_dataset("https://j", "t", "1", issue_types=["Story"])
        self.assertEqual(df["Epic"].tolist(), ["P-100"])


if __name__ == "__main__":
    unittest.main()

