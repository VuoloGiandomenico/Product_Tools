"""Small pandas helpers shared by pages and utils."""
from __future__ import annotations

import pandas as pd


def blank_if_missing(series: pd.Series) -> pd.Series:
    """Replace NaN/None/NA with "" without dtype warnings.

    ``series.fillna("")`` on a float column (e.g. an empty CSV column) is deprecated in
    pandas 3 and will raise in pandas 4; casting to ``object`` first keeps it valid.
    """
    return series.astype(object).where(series.notna(), "")

