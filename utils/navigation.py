"""Pure description of the 'Release Plan' menu group (order checked by tests)."""

# (order, file, title, url_path). url_path None = Streamlit default (derived from the file name).
RELEASE_PLAN_PAGES = (
    (0, "tools/release_plan/0_Overview.py", "Overview", "release-plan-overview"),
    (1, "tools/release_plan/1_Scope.py", "Scope", "release-plan-scope"),
    (2, "tools/release_plan/2_Approval.py", "Approval", "release-plan-approval"),
    (3, "tools/release_plan/3_Documentation_Table.py", "Documentation table", "release-plan-documentation-table"),
    (4, "tools/release_plan/4_Critical_Changes.py", "Critical changes", "release-plan-critical-changes"),
    (5, "tools/release_plan/5_PRE_Testing.py", "PRE testing", "release-plan-pre-testing"),
    (6, "tools/release_plan/6_Unassigned.py", "Unassigned", "release-plan-unassigned"),
    (7, "tools/7_Release_Plan.py", "Post Actions", None),
    (8, "tools/release_plan/8_Notifications.py", "Notifications", "release-plan-notifications"),
)
