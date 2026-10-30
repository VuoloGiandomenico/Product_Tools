"""Tests for utils/persistence.py (shared workbook files)."""
import os
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from utils import persistence


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.object(persistence, "DATA_DIR", self._tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _files(self):
        return sorted(os.listdir(self._tmp.name))

    def test_csv_round_trip_coerces_types(self):
        df = pd.DataFrame({"Key": ["A-1", None], "Select": [True, None], "Summary": ["x", "y"]})
        persistence.save_df_to_csv(df, "wb.csv")
        loaded = persistence.load_df_from_csv("wb.csv")
        self.assertEqual(loaded["Key"].tolist(), ["A-1", ""])
        self.assertEqual(loaded["Select"].tolist(), [True, False])
        self.assertEqual(self._files(), ["wb.csv"])  # no temp files left behind

    def test_empty_df_removes_file(self):
        persistence.save_df_to_csv(pd.DataFrame({"Key": ["A-1"]}), "wb.csv")
        persistence.save_df_to_csv(pd.DataFrame(), "wb.csv")
        self.assertEqual(self._files(), [])
        self.assertIsNone(persistence.load_df_from_csv("wb.csv"))

    def test_failed_write_keeps_previous_file_and_no_temp(self):
        persistence.save_df_to_csv(pd.DataFrame({"Key": ["OLD"]}), "wb.csv")
        bad = pd.DataFrame({"Key": ["NEW"]})
        with patch.object(pd.DataFrame, "to_csv", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                persistence.save_df_to_csv(bad, "wb.csv")
        self.assertEqual(self._files(), ["wb.csv"])
        self.assertEqual(persistence.load_df_from_csv("wb.csv")["Key"].tolist(), ["OLD"])

    def test_corrupt_csv_returns_none_and_logs(self):
        with open(os.path.join(self._tmp.name, "bad.csv"), "wb") as f:
            f.write(b"\x00\xff\xfe broken")
        with patch.object(pd, "read_csv", side_effect=ValueError("corrupt")):
            with self.assertLogs("utils.persistence", level="WARNING"):
                self.assertIsNone(persistence.load_df_from_csv("bad.csv"))

    def test_custom_tables_round_trip(self):
        tables = [{"title": "T", "position": "After Demo Table",
                   "df": pd.DataFrame({"Key": ["A-1"], "Value": [float("nan")]})}]
        persistence.save_custom_tables_to_json(tables, "ct.json")
        loaded = persistence.load_custom_tables_from_json("ct.json")
        self.assertEqual(loaded[0]["title"], "T")
        self.assertEqual(loaded[0]["df"].to_dict(orient="records"), [{"Key": "A-1", "Value": ""}])
        self.assertEqual(self._files(), ["ct.json"])

    def test_corrupt_json_returns_none_and_logs(self):
        with open(os.path.join(self._tmp.name, "ct.json"), "w", encoding="utf-8") as f:
            f.write("{not json")
        with self.assertLogs("utils.persistence", level="WARNING"):
            self.assertIsNone(persistence.load_custom_tables_from_json("ct.json"))

    def test_empty_string_column_loads_as_blank_strings(self):
        # An all-empty column is parsed by pandas as float64 NaN: must load as "" without warnings.
        with open(os.path.join(self._tmp.name, "wb.csv"), "w", encoding="utf-8") as f:
            f.write("Key,Labels,Select\nA-1,,True\nA-2,,\n")
        loaded = persistence.load_df_from_csv("wb.csv")
        self.assertEqual(loaded["Labels"].tolist(), ["", ""])
        self.assertEqual(loaded["Select"].tolist(), [True, False])

    def test_get_last_mtime(self):
        self.assertEqual(persistence.get_last_mtime(["missing.csv"]), 0)
        persistence.save_df_to_csv(pd.DataFrame({"Key": ["A"]}), "wb.csv")
        self.assertGreater(persistence.get_last_mtime(["wb.csv", "missing.csv"]), 0)


if __name__ == "__main__":
    unittest.main()

