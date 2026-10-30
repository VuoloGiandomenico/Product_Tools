"""The optional Sprint KPI block must be rendered in the PDF right before the Outlook section."""
import pandas as pd
import pytest
from pypdf import PdfReader

from utils import pdf_helpers
from utils.sprint_review_helpers import build_sprint_review_pdf


class _FakeSessionState(dict):
    """Minimal stand-in for st.session_state (supports both attribute and key access)."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


@pytest.fixture(autouse=True)
def _session_state(monkeypatch):
    # pdf_helpers' page decorators read branding values straight from st.session_state.
    fake = _FakeSessionState(primary_color="#1E3A8A", project_name="Test")
    monkeypatch.setattr(pdf_helpers.st, "session_state", fake, raising=False)
    yield fake

METRICS = {
    "items": {"Committed": 12, "Added": 2, "Delivered": 10},
    "total_sp": 40, "achieved_sp": 34, "sprint_pct": "85%", "releases": 1,
    "open_bugs": 3, "crit_bugs": 1, "resolved_bugs": 5, "cycle_time": 4.2,
    "quality": {},
}


def _items(prefix, status):
    return pd.DataFrame([{
        "Select": False, "Type": "Story", "Key": f"{prefix}-1", "Summary": f"{prefix} topic",
        "Status": status, "Epic": "Epic A", "Fix Version": "1.0.0", "Labels": "Frontend_Team",
    }])


def _pdf_text(config):
    pdf = build_sprint_review_pdf(_items("OV", "Done"), _items("OT", "To Do"), config)
    reader = PdfReader(pdf)
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _config(include):
    return {
        "include_sprint_kpis": include,
        "sprint_kpi_selection": ["Committed", "Delivered"],
        "current_sprint_metrics": METRICS,
        "custom_tables": [],
        "next_release_df": None,
        "primary_color": "#1E3A8A",
        "project_name": "Test",
        "sprint_number": "1",
    }


def test_kpi_summary_is_rendered_between_overview_and_outlook():
    text = _pdf_text(_config(True))
    overview = text.index("Overview:")
    kpis = text.index("Sprint KPI Summary:")
    outlook = text.index("Outlook:")
    assert overview < kpis < outlook


def test_kpi_summary_is_omitted_when_disabled():
    text = _pdf_text(_config(False))
    assert "Sprint KPI Summary" not in text
    assert "Outlook:" in text

