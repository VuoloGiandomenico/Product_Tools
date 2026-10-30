"""Stage 4: nav order (AC3) and AppTests for pages 0, 1, 8 (fixtures only, no network, no writes)."""
import ast
import copy
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests import test_release_readiness_pages as base  # noqa: E402
from utils.navigation import RELEASE_PLAN_PAGES  # noqa: E402

NEW_FILES = {"rr0": "0_Overview.py", "rr1": "1_Scope.py", "rr8": "8_Notifications.py"}


class NavOrderTests(unittest.TestCase):
    def test_order_0_to_8_and_page7_path(self):
        self.assertEqual([p[0] for p in RELEASE_PLAN_PAGES], list(range(9)))
        titles = [p[2] for p in RELEASE_PLAN_PAGES]
        self.assertTrue(all(t and t.strip() == t for t in titles), titles)
        self.assertEqual(len(titles), len(set(titles)))
        self.assertEqual(RELEASE_PLAN_PAGES[0][2], "Overview")
        self.assertEqual(RELEASE_PLAN_PAGES[7][1], "tools/7_Release_Plan.py")
        self.assertEqual(RELEASE_PLAN_PAGES[7][2], "Post Actions")
        self.assertIsNone(RELEASE_PLAN_PAGES[7][3])  # url_path unchanged (default)
        for _, path, _, _ in RELEASE_PLAN_PAGES:
            self.assertTrue((ROOT / path).is_file(), path)
        urls = [p[3] for p in RELEASE_PLAN_PAGES if p[3]]
        self.assertEqual(len(urls), len(set(urls)))

    def test_app_uses_menu_and_keeps_ai_agent_hidden(self):
        src = (ROOT / "app.py").read_text(encoding="utf-8")
        ast.parse(src)
        self.assertRegex(src, r'"[^"]*Release Plan": release_plan_nav_pages')
        self.assertIn('a[href$="AI_Agent"]', src)
        self.assertIn("rr_overview_page", src)
        for f in ("1_Quarterly_Planner", "2_Sprint_Review", "3_Release_Notes", "4_Sprint_KPIs",
                  "5_AI_Issue_Creator", "6_AI_Agent"):
            self.assertIn(f, src)


class RoutedCase(base.PagesCase):
    """PagesCase where the not-relevant JQL returns only RP-4 and the release JQL the rest."""

    def setUp(self):
        p = patch.dict(base.PAGE_FILES, NEW_FILES)
        p.start()
        self.addCleanup(p.stop)

    def run_page(self, prefix, version="", issues=None, **kw):
        full = issues if issues is not None else base.load("release_readiness/issues.json")

        def route(jql):
            rel = "Not release relevant" in jql
            return {"issues": [i for i in full["issues"] if (i["key"] == "RP-4") == rel]}

        return super().run_page(prefix, version, issues=route, **kw)


class OverviewTests(RoutedCase):
    def test_nothing_fetched_before_click(self):
        for prefix in NEW_FILES:
            with self.subTest(prefix):
                at = self.run_page(prefix, "35.0.0", click=False)
                self.assertEqual(self.jqls, [])
                self.assertEqual(self.conf_calls, 0)
                self.assertEqual([b.key for b in at.button], [f"{prefix}_refresh"])
                self.assert_no_writes()

    def test_overview_semaphore_counters_per_owner_critical_first(self):
        at = self.run_page("rr0", "35.0.0")
        self.assertEqual(len(self.jqls), 2)
        self.assertTrue(any("Not release relevant" in j for j, _ in self.jqls))
        self.assertTrue(any("updated >=" in j for j, _ in self.jqls))
        self.assertIn("rr0_data", at.session_state)
        self.assertNotIn("rr8_data", at.session_state)
        self.assertEqual([m.label for m in at.metric], ["ERROR", "WARN", "INFO"])
        self.assertTrue(any("Release readiness: " in s.value for s in at.subheader))
        self.assertEqual(list(at.dataframe[0].value.columns)[:1], ["Issue"])  # critical table first
        owners = at.dataframe[1].value
        self.assertEqual(list(owners.columns), ["Owner", "ERROR", "WARN", "INFO"])
        self.assertIn("Unassigned", list(owners["Owner"]))
        self.assertIn("Ann", list(owners["Owner"]))
        # each finding counted once: metric totals == owner totals
        for col, m in zip(("ERROR", "WARN", "INFO"), at.metric):
            self.assertEqual(int(m.value), int(owners[col].sum()))
        self.assertTrue(at.expander)
        self.assertTrue(all(not e.proto.expanded for e in at.expander))
        self.assertTrue(any(c.value.startswith("Checked at ") for c in at.caption))
        self.assert_no_writes()

    def test_overview_freeze_manual_input_when_missing(self):
        at = self.run_page("rr0", "35.0.0")
        self.assertEqual(at.date_input[0].key, "rr0_freeze_manual")

    def test_overview_sandbox(self):
        page = base.load("release_plan/confluence_page_999_v22.json")
        at = self.run_page("rr0", "999.0.0", env=base.SANDBOX_ENV, page=page)
        self.assertTrue(any("SANDBOX" in w.value for w in at.warning))
        self.assertIn("DIGITAL:HUB 35.0.0", self.jqls[0][0])


class ScopeTests(RoutedCase):
    def test_scope_counts_and_misclassified(self):
        at = self.run_page("rr1", "35.0.0")
        vals = {m.label: int(m.value) for m in at.metric}
        self.assertEqual(vals["In scope"], 4)
        self.assertEqual(vals["Excluded: Sub-tasks"], 1)
        self.assertEqual(vals["Excluded: out of scope"], 0)
        findings = [df.value for df in at.dataframe if "Severity" in df.value.columns]
        self.assertEqual(len(findings), 1)
        self.assertEqual(list(findings[0]["Issue"]), ["RP-4"])
        self.assertEqual(list(findings[0]["Severity"]), ["WARN"])
        self.assertIn("rr1_data", at.session_state)
        self.assert_no_writes()


class NotificationsTests(RoutedCase):
    def test_independent_refresh_and_texts(self):
        at = self.run_page("rr8", "35.0.0")
        self.assertIn("rr8_data", at.session_state)
        self.assertNotIn("rr0_data", at.session_state)
        self.assertEqual(len(self.jqls), 2)
        codes = [c.value for c in at.code]
        self.assertTrue(codes)
        self.assertIn("RP-4", "\n".join(codes))  # scope WARN text
        self.assert_no_writes()

    def test_unassigned_text_mentions(self):
        issues = base.load("release_readiness/issues.json")
        for i in issues["issues"]:
            if i["key"] == "RP-6":
                i["fields"]["assignee"] = None  # open + unassigned -> WARN
        at = self.run_page("rr8", "35.0.0", issues=copy.deepcopy(issues))
        un = [c.value for c in at.code if "no assignee" in c.value]
        self.assertEqual(len(un), 1)
        for m in ("[~UGN44UN]", "[~FC0KEWC]", "[~APMKRYI]"):
            self.assertIn(m, un[0])

    def test_no_write_controls(self):
        at = self.run_page("rr8", "35.0.0")
        self.assertEqual([b.key for b in at.button], ["rr8_refresh"])
        src = (ROOT / "tools/release_plan/8_Notifications.py").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"update_confluence|requests\.(post|put|delete)|add_comment|\.post\(", src))
        self.assert_no_writes()


if __name__ == "__main__":
    unittest.main()
