from utils import release_readiness_page as rr

P = "rr0"
rr.page_header("🔎 Overview",
               "Runs all release readiness rules. Fetches only when you click Refresh (read-only).")
ctx = rr.load_context(P)
rr.refresh_all(ctx)
data = rr.get_data(ctx)
if data:
    freeze = rr.freeze_date_input(P, data)
    rr.render_overview(data, freeze)
