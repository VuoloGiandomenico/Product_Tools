import streamlit as st

from utils import release_checks as rc
from utils import release_readiness_page as rr

P = "rr8"
rr.page_header("📨 Notifications",
               "Copy-ready texts to paste into Jira comments or chat. Nothing is posted from here. Runs only on Refresh.")
ctx = rr.load_context(P)
rr.refresh_all(ctx)
data = rr.get_data(ctx)
if data:
    freeze = rr.freeze_date_input(P, data)
    results = rc.run_all(data["issues"], freeze, rr.date.today())
    texts = rc.notification_texts(results)
    st.caption(f"Checked at {data['checked_at']}")
    if not texts:
        st.success("Nothing to notify: no ERROR or WARN findings.")
    for rule, text in texts.items():
        st.markdown(f"#### {rule}")
        st.code(text, language="text")
