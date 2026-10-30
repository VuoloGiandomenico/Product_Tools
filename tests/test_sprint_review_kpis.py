"""Tests for the Sprint Review KPI block helpers in utils/kpi_helpers.py."""
import os
import unittest
from unittest.mock import patch

from utils import kpi_helpers as kh

BASE_ENV = {k: v for k, v in os.environ.items()
            if k not in ("DEFAULT_INCLUDED_SPRINT_KPIS", "DEFAULT_INCLUDE_SPRINT_KPIS")}


class CatalogTests(unittest.TestCase):
    def test_catalog_is_ordered_and_unique(self):
        with patch.object(kh, "configured_quality_kpis", return_value=[]):
            catalog = kh.sprint_review_kpi_catalog()
        self.assertEqual(catalog[:3], ["Committed", "Added", "Delivered"])
        self.assertEqual(len(catalog), len(set(catalog)))
        self.assertIn("Avg Cycle Time (Days)", catalog)

    def test_quality_kpis_are_appended_without_duplicates(self):
        with patch.object(kh, "configured_quality_kpis", return_value=["Code Smells", "Committed", ""]):
            catalog = kh.sprint_review_kpi_catalog()
        self.assertEqual(catalog[-1], "Code Smells")
        self.assertEqual(catalog.count("Committed"), 1)
        self.assertNotIn("", catalog)


class DefaultSelectionTests(unittest.TestCase):
    def _defaults(self, env):
        with patch.dict(os.environ, {**BASE_ENV, **env}, clear=True), \
                patch.object(kh, "configured_quality_kpis", return_value=[]):
            return kh.default_sprint_review_kpis()

    def test_builtin_defaults_are_valid_catalog_entries(self):
        defaults = self._defaults({})
        self.assertEqual(defaults, ["Committed", "Delivered", "Open Bugs (Sev A+B)",
                                    "Critical Bugs (Sev A)", "Avg Cycle Time (Days)"])

    def test_env_override_keeps_order_and_drops_unknown(self):
        defaults = self._defaults({"DEFAULT_INCLUDED_SPRINT_KPIS": " Releases , Unknown KPI,Delivery % "})
        self.assertEqual(defaults, ["Releases", "Delivery %"])

    def test_legacy_env_name_is_supported(self):
        self.assertEqual(self._defaults({"DEFAULT_INCLUDE_SPRINT_KPIS": "Releases"}), ["Releases"])

    def test_env_with_only_unknown_names_falls_back_to_builtin(self):
        self.assertEqual(self._defaults({"DEFAULT_INCLUDED_SPRINT_KPIS": "nope, nada"})[0], "Committed")


class SummaryDfTests(unittest.TestCase):
    METRICS = {
        "items": {"Committed": 12, "Added": 2, "Delivered": 10},
        "total_sp": 40, "achieved_sp": 34, "sprint_pct": "85%", "releases": 1,
        "open_bugs": 3, "crit_bugs": 1, "resolved_bugs": 5, "cycle_time": 4.2,
        "quality": {"Code Smells": 17},
    }

    def test_empty_selection_returns_empty_two_column_df(self):
        df = kh.sprint_kpi_summary_df([], self.METRICS)
        self.assertTrue(df.empty)
        self.assertEqual(list(df.columns), ["KPI", "Value"])

    def test_values_are_mapped_from_each_metric_source(self):
        selected = ["Delivered", "Committed SP (at Start)", "Delivered SP (Total)", "Delivery %", "Releases",
                    "Open Bugs (Sev A+B)", "Critical Bugs (Sev A)", "Resolved Bugs",
                    "Avg Cycle Time (Days)", "Code Smells"]
        df = kh.sprint_kpi_summary_df(selected, self.METRICS)
        self.assertEqual(df["KPI"].tolist(), selected)
        self.assertEqual(df["Value"].tolist(), [10, 40, 34, "85%", 1, 3, 1, 5, 4.2, 17])

    def test_missing_metrics_yield_blank_values(self):
        df = kh.sprint_kpi_summary_df(["Committed", "Releases", "Unknown"], None)
        self.assertEqual(df["Value"].tolist(), ["", "", ""])


if __name__ == "__main__":
    unittest.main()

