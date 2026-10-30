"""Regression test: the Sprint KPI toggle must live in the Ingestion step and persist across tabs."""
from pathlib import Path

from streamlit.testing.v1 import AppTest

PAGE = str(Path(__file__).resolve().parents[1] / "tools" / "2_Sprint_Review.py")


def _kpi_checkbox(at):
    return [c for c in at.checkbox if c.key == "include_sprint_kpis_widget"]


def test_kpi_toggle_is_in_ingestion_and_survives_tab_switch():
    at = AppTest.from_file(PAGE, default_timeout=60)
    at.run()
    assert not at.exception

    checkbox = _kpi_checkbox(at)
    assert checkbox, "Sprint KPI toggle missing from the Ingestion step"

    checkbox[0].check().run()
    assert at.session_state["include_sprint_kpis"] is True
    assert any(m.key == "sprint_kpi_selection_widget" for m in at.multiselect)

    at.session_state["active_tab"] = "💾 Exporter"
    at.run()
    at.session_state["active_tab"] = "🔌 Ingestion"
    at.run()

    assert at.session_state["include_sprint_kpis"] is True
    assert _kpi_checkbox(at)[0].value is True
    assert not at.exception


def test_kpi_table_reflects_metrics_when_section_already_enabled():
    """Section already enabled (previous run / env default) and metrics computed later:
    the table must show without touching the KPI selector."""
    at = AppTest.from_file(PAGE, default_timeout=60)
    at.session_state["include_sprint_kpis"] = True
    at.run()
    at.session_state["current_sprint_metrics"] = {"items": {"Committed": 12, "Added": 2, "Delivered": 10}}
    at.run()

    kpi_df = at.session_state["current_sprint_kpi_df"]
    assert not kpi_df.empty
    assert "Committed" in set(kpi_df["KPI"])
    assert len(at.dataframe) >= 1
    assert not at.exception
