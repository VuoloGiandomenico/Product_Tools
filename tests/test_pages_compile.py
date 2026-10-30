"""Smoke test: every Streamlit page and entry point must at least be valid Python."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = sorted(
    [ROOT / "app.py", ROOT / "api.py"]
    + list((ROOT / "tools").rglob("*.py"))
    + list((ROOT / "utils").glob("*.py"))
)


class PagesCompileTests(unittest.TestCase):
    def test_all_sources_compile(self):
        for path in SOURCES:
            with self.subTest(path=str(path.relative_to(ROOT))):
                compile(path.read_text(encoding="utf-8"), str(path), "exec")


if __name__ == "__main__":
    unittest.main()

