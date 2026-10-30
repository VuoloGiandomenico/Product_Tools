import os
import json
import tempfile
import pandas as pd
from utils.df_helpers import blank_if_missing
import logging

logger = logging.getLogger(__name__)

DATA_DIR = "data"


def _atomic_write(path, write_fn):
    """Write a file atomically: write to a temp file in the same dir, then os.replace().

    Readers (e.g. other collaborators' auto-refresh) never observe a half-written file.
    """
    directory = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp_", suffix=os.path.splitext(path)[1])
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            write_fn(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise

def get_last_mtime(files):
    """Returns the maximum modification time among a list of filenames inside DATA_DIR."""
    mtimes = []
    for f in files:
        path = os.path.join(DATA_DIR, f)
        if os.path.exists(path):
            mtimes.append(os.path.getmtime(path))
    return max(mtimes) if mtimes else 0

def save_df_to_csv(df, filename):
    """Saves a dataframe to a CSV file in the data directory. If df is None or empty, removes the file."""
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, filename)
    if df is None or df.empty:
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                logger.warning("Could not remove empty workbook file %s", path, exc_info=True)
    else:
        _atomic_write(path, lambda f: df.to_csv(f, index=False))

def load_df_from_csv(filename):
    """Loads a dataframe from a CSV file in the data directory, returning None if not found or empty."""
    path = os.path.join(DATA_DIR, filename)
    if os.path.exists(path):
        try:
            df = pd.read_csv(path)
            if df.empty:
                return None
            
            # Coerce column types to prevent Streamlit editing type conflicts (e.g. empty column parsed as float)
            string_cols = ["Key", "Type", "Summary", "Epic", "Status", "Fix Version", "Labels", "Assignee", "Outlook"]
            for col in string_cols:
                if col in df.columns:
                    df[col] = blank_if_missing(df[col]).astype(str)

            bool_cols = ["Select", "Demo", "Sprint Review", "Release Notes"]
            for col in bool_cols:
                if col in df.columns:
                    df[col] = df[col].fillna(False).astype(bool)

            return df
        except Exception:
            logger.warning("Could not load workbook file %s", path, exc_info=True)
            return None
    return None

def save_custom_tables_to_json(custom_tables, filename):
    """Serializes and saves a list of custom tables to a JSON file in the data directory."""
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, filename)
    logger.debug(f"save_custom_tables_to_json called with {len(custom_tables) if custom_tables else 0} tables")
    if custom_tables is None:
        custom_tables = []
    
    serialized = []
    for item in custom_tables:
        df_val = item.get("df")
        df_records = []
        if isinstance(df_val, pd.DataFrame):
            df_clean = df_val.copy()
            # Replace pd.NA, NaT and float('nan') with empty strings (cast first: pandas 4
            # refuses to fill a float column with a string).
            for col in df_clean.columns:
                df_clean[col] = df_clean[col].astype(object).where(df_clean[col].notna(), "")
            df_records = df_clean.to_dict(orient="records")
        elif isinstance(df_val, list):
            df_records = df_val
            
        serialized_item = {k: v for k, v in item.items() if k != "df"}
        serialized_item["df"] = df_records
        serialized.append(serialized_item)

    _atomic_write(path, lambda f: json.dump(serialized, f, indent=2, ensure_ascii=False))

def load_custom_tables_from_json(filename):
    """Loads and deserializes custom tables from a JSON file in the data directory."""
    path = os.path.join(DATA_DIR, filename)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            deserialized = []
            for item in data:
                df_data = item.get("df", [])
                deserialized_item = {k: v for k, v in item.items() if k != "df"}
                deserialized_item["df"] = pd.DataFrame(df_data) if df_data else pd.DataFrame()
                deserialized.append(deserialized_item)
            return deserialized
        except Exception:
            logger.warning("Could not load custom tables file %s", path, exc_info=True)
            return None
    return None

def enable_auto_save(save_callback):
    """
    Monkey-patches st.rerun to automatically call a save function before rerunning.
    This guarantees that workbook state changes are synced to disk before the UI reloads.
    """
    import streamlit as st
    
    if hasattr(st, "_original_rerun"):
        st.rerun = st._original_rerun
        delattr(st, "_original_rerun")

    st._original_rerun = st.rerun
    def custom_rerun(*args, **kwargs):
        if st.session_state.get("_skip_save_once"):
            st.session_state._skip_save_once = False
        else:
            try:
                save_callback()
            except Exception as e:
                logger.exception("Workbook auto-save failed")
        st._original_rerun(*args, **kwargs)
    st.rerun = custom_rerun

def setup_autorefresh(sync_interval_ms, last_sync_key, watched_files, load_callback):
    """
    Sets up an autorefresh loop on the current page, and triggers a load_callback
    if any of the watched files have a modification time newer than the last sync.
    """
    import streamlit as st
    from streamlit_autorefresh import st_autorefresh
    
    if last_sync_key not in st.session_state:
        st.session_state[last_sync_key] = 0
        try:
            load_callback()
        except Exception:
            pass
            
    st_autorefresh(interval=sync_interval_ms, limit=None, key=f"{last_sync_key}_autorefresh")
    
    disk_mtime = get_last_mtime(watched_files)
    if disk_mtime > st.session_state[last_sync_key]:
        load_callback()
        st.session_state[last_sync_key] = disk_mtime
        st.session_state._skip_save_once = True
        st.rerun()
