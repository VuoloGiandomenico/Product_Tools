"""Pure logic for the Release Plan "Post-actions" feature.

No network and no Streamlit here: everything is testable with fixtures.
The merge into Confluence storage format is an offset-based string splice so
that all untouched content stays byte-for-byte identical.
"""
import html
import os
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional

DEFAULT_FIX_VERSION_LABEL = "DIGITAL:HUB"
DEFAULT_TITLE_TEMPLATE = "Release Plan {version}"
ENV_LABEL = "RELEASE_FIXVERSION_LABEL"
ENV_PARENT = "RELEASE_PLAN_PARENT_PAGE"
ENV_TITLE = "RELEASE_PLAN_TITLE_TEMPLATE"
JIRA_PROJECT = os.getenv("RELEASE_PLAN_JIRA_PROJECT", "RECALLTWO").strip() or "RECALLTWO"
ALLOWED_SPACE_KEY = os.getenv("RELEASE_PLAN_SPACE_KEY", JIRA_PROJECT).strip() or JIRA_PROJECT
FORBIDDEN_PAGE_IDS = frozenset({"8307791929"})  # template page
ENV_SANDBOX = "RELEASE_PLAN_SANDBOX_MAP"
# "Release Plan 999.0.0": the only page writable in sandbox
SANDBOX_WRITE_PAGE_ID = os.getenv("RELEASE_PLAN_SANDBOX_PAGE_ID", "7570725597").strip() or "7570725597"
ISSUE_TYPES = ("Bug", "Improvement", "Story", "Task")
REQUIRED_ENV = ("JIRA_SERVER", "JIRA_API_TOKEN", "CONFLUENCE_SERVER", "CONFLUENCE_API_TOKEN", ENV_PARENT)

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
TAG_RE = re.compile(r"^\s*(?:[*#-]+\s+)?post-action:\s*(.*?)\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class PostAction:
    issue_key: str
    text: str
    author: str
    source: str  # "description" | "comment:<id>"


@dataclass(frozen=True)
class ExistingTask:
    task_id: Optional[int]
    status: str
    issue_key: Optional[str]
    text: str


@dataclass(frozen=True)
class ReleasePlanConfig:
    label: str
    parent_id: str
    title_template: str


@dataclass(frozen=True)
class SandboxConfig:
    """Typed version -> source version whose Jira issues are read (read-only)."""
    mapping: tuple = ()  # tuple of (typed_version, source_version)

    def source_for(self, version: str) -> Optional[str]:
        return dict(self.mapping).get((version or "").strip())


@dataclass
class GuardResult:
    ok: bool
    errors: list = field(default_factory=list)
    page_id: str = ""
    title: str = ""
    found_fix_version: Optional[str] = None


@dataclass
class MergePlan:
    new_actions: list
    skipped: list
    new_storage: str
    new_task_ids: list


# ---------------------------------------------------------------- input helpers
def missing_config(env: Mapping) -> list:
    return [name for name in REQUIRED_ENV if not str(env.get(name, "") or "").strip()]


def validate_version(version: str) -> str:
    v = (version or "").strip()
    if not VERSION_RE.match(v):
        raise ValueError("Version must be numeric X.Y.Z, e.g. 35.0.0")
    return v


def load_release_plan_config(env: Mapping) -> ReleasePlanConfig:
    def get(name):
        return str(env.get(name, "") or "").strip()

    label = get(ENV_LABEL) or DEFAULT_FIX_VERSION_LABEL
    template = get(ENV_TITLE) or DEFAULT_TITLE_TEMPLATE
    parent = get(ENV_PARENT)
    if not parent:
        raise ValueError(f"Missing configuration (set in .env): {ENV_PARENT}")
    if not re.fullmatch(r"\d+", parent):
        raise ValueError(f"{ENV_PARENT} must be a numeric page ID (ID only, not a URL).")
    if "{version}" not in template:
        raise ValueError(f"{ENV_TITLE} must contain the {{version}} placeholder.")
    return ReleasePlanConfig(label, parent, template)


def load_sandbox_config(env: Mapping) -> SandboxConfig:
    """Parse RELEASE_PLAN_SANDBOX_MAP="999.0.0:35.0.0[,a:b]". Empty/unset = no sandbox."""
    raw = str(env.get(ENV_SANDBOX, "") or "").strip()
    pairs = []
    for item in (raw.split(",") if raw else []):
        item = item.strip()
        if not item:
            continue
        typed, sep, source = item.partition(":")
        if not sep:
            raise ValueError(f"{ENV_SANDBOX} entries must look like 999.0.0:35.0.0.")
        try:
            pairs.append((validate_version(typed), validate_version(source)))
        except ValueError:
            raise ValueError(f"{ENV_SANDBOX} entries must look like 999.0.0:35.0.0.")
    return SandboxConfig(tuple(pairs))


def resolve_source_version(version: str, config: SandboxConfig) -> tuple:
    """Return (jira_source_version, is_sandbox)."""
    v = (version or "").strip()
    source = config.source_for(v) if config else None
    if source:
        return source, True
    return v, False


def check_write_allowed(page_id, *, is_sandbox: bool, target: str = "confluence") -> None:
    """Raise PermissionError unless a write to `target` ("confluence"|"jira") is allowed."""
    pid = str(page_id or "")
    if target == "jira":
        if is_sandbox:
            raise PermissionError("Jira writes are blocked in sandbox mode.")
        return
    if target != "confluence":
        raise PermissionError(f"Unknown write target '{target}'.")
    if pid in FORBIDDEN_PAGE_IDS:
        raise PermissionError(f"Template page {pid} can never be written.")
    if is_sandbox and pid != SANDBOX_WRITE_PAGE_ID:
        raise PermissionError(
            f"Sandbox mode: only page {SANDBOX_WRITE_PAGE_ID} can be written, not {pid or '?'}."
        )


def build_fix_version(version: str, label: str = DEFAULT_FIX_VERSION_LABEL) -> str:
    # Label is stored without a trailing space; the separator is added here at format time.
    return f"{label.strip()} {version}"


def build_release_jql(version: str, label: str = DEFAULT_FIX_VERSION_LABEL) -> str:
    return (
        f"project = {JIRA_PROJECT} AND issuetype in ({', '.join(ISSUE_TYPES)}) "
        f'AND fixVersion = "{build_fix_version(version, label)}"'
    )


def fix_version_display(raw_version: str, label: str = DEFAULT_FIX_VERSION_LABEL) -> str:
    try:
        return build_fix_version(validate_version(raw_version), label)
    except ValueError:
        return ""


def build_target_title(template: str, version: str) -> str:
    return template.replace("{version}", version)


def select_target_page(children: list, title: str, parent_id: str) -> dict:
    matches = [c for c in (children or []) if c.get("title") == title]
    if not matches:
        raise ValueError(f"{title} not found under parent page {parent_id}.")
    if len(matches) > 1:
        listing = "; ".join(f"'{c.get('title')}' (ID {c.get('id')})" for c in matches)
        raise ValueError(
            f"Multiple pages match '{title}' under parent page {parent_id}: {listing}. Nothing was written."
        )
    return matches[0]


def is_missing_fix_version_error(message: str) -> bool:
    msg = message or ""
    return "400" in msg and "does not exist for the field 'fixVersion'" in msg


# --------------------------------------------------------------------- parsing
def strip_wiki_markup(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\{code(?::[^}]*)?\}|\{noformat\}", "", text)
    text = re.sub(r"\{\{(.*?)\}\}", r"\1", text)
    text = re.sub(r"\[([^\]|\n]+)\|[^\]\n]*\]", r"\1", text)
    text = re.sub(r"\[([^\]\n]+)\]", r"\1", text)
    out = []
    for line in text.split("\n"):
        line = re.sub(r"^\s*[*#-]+\s+", "", line)
        for ch in ("*", "_", "+", "-"):
            e = re.escape(ch)
            line = re.sub(rf"(?<!\w){e}(\S(?:.*?\S)?){e}(?!\w)", r"\1", line)
        out.append(line)
    return "\n".join(out)


def parse_post_actions(text: str) -> list:
    result = []
    for line in strip_wiki_markup(text).split("\n"):
        m = TAG_RE.match(line)
        if m and m.group(1).strip():
            result.append(m.group(1).strip())
    return result


def _display_name(user) -> str:
    if isinstance(user, dict):
        return user.get("displayName") or user.get("name") or "Unknown"
    return "Unknown"


def extract_actions_from_issue(issue: dict) -> list:
    key = issue.get("key", "")
    fields = issue.get("fields") or {}
    actions = []
    reporter = _display_name(fields.get("reporter"))
    for t in parse_post_actions(fields.get("description") or ""):
        actions.append(PostAction(key, t, reporter, "description"))
    comments = (fields.get("comment") or {}).get("comments") or []
    for c in comments:
        author = _display_name(c.get("author"))
        for t in parse_post_actions(c.get("body") or ""):
            actions.append(PostAction(key, t, author, f"comment:{c.get('id', '')}"))
    return actions


def extract_actions(issues: list) -> list:
    out = []
    for issue in issues:
        out.extend(extract_actions_from_issue(issue))
    return out


def normalise_text(text: str) -> str:
    return " ".join((text or "").split()).casefold()


# -------------------------------------------------------------- storage scanning
def assert_well_formed(storage: str) -> None:
    """Validation only (works on a throwaway copy)."""
    copy = re.sub(r"&(?!amp;|lt;|gt;|quot;|apos;|#)\w+;", "&amp;", storage)
    wrapped = f'<root xmlns:ac="urn:ac" xmlns:ri="urn:ri" xmlns:at="urn:at">{copy}</root>'
    try:
        ET.fromstring(wrapped)
    except ET.ParseError as e:
        raise ValueError(f"Page storage is not well-formed XML: {e}")


def _plain(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def _normalise_heading(text: str) -> str:
    """Casefolded heading title without leading emoji/symbols and numbering ("3.3 \u2699\ufe0f Post-actions")."""
    t = text or ""
    prev = None
    while prev != t:
        prev = t
        t = re.sub(r"^[\d.]+(?=\s|$)\s*", "", t.lstrip())
        i = 0
        while i < len(t) and not t[i].isalnum() and unicodedata.category(t[i])[0] in ("S", "M", "C", "P", "Z"):
            i += 1
        t = t[i:]
    return " ".join(t.split()).casefold()


def find_post_actions_section(storage: str) -> tuple:
    """Return (heading_start, heading_end, section_end)."""
    headings = list(re.finditer(r"<h([1-6])\b[^>]*>(.*?)</h\1>", storage, re.S))
    matches = []
    for i, m in enumerate(headings):
        title = _normalise_heading(_plain(m.group(2)))
        if title in ("post-actions", "post actions"):
            matches.append(i)
    if not matches:
        raise ValueError("Post-actions section not found on the page. Nothing was written.")
    if len(matches) > 1:
        raise ValueError("More than one Post-actions heading found on the page. Nothing was written.")
    idx = matches[0]
    h = headings[idx]
    level = int(h.group(1))
    section_end = len(storage)
    for nxt in headings[idx + 1:]:
        if int(nxt.group(1)) <= level:
            section_end = nxt.start()
            break
    return h.start(), h.end(), section_end


def find_all_tasks(storage: str) -> list:
    """Return [(start, end, ExistingTask)] for every <ac:task> on the page."""
    token = re.compile(r"<ac:task(?=[\s>])[^>]*>|</ac:task>")
    result = []
    depth = 0
    start = None
    for m in token.finditer(storage):
        if m.group(0).startswith("</"):
            if depth > 0:
                depth -= 1
                if depth == 0:
                    result.append((start, m.end(), _parse_task(storage[start:m.end()])))
        else:
            if depth == 0:
                start = m.start()
            depth += 1
    return result


def _parse_task(xml: str) -> ExistingTask:
    mid = re.search(r"<ac:task-id>\s*(\d+)\s*</ac:task-id>", xml)
    mst = re.search(r"<ac:task-status>\s*(.*?)\s*</ac:task-status>", xml, re.S)
    mbody = re.search(r"<ac:task-body>(.*)</ac:task-body>", xml, re.S)
    body = mbody.group(1) if mbody else ""
    mkey = re.search(r'<a\s[^>]*href="[^"]*/browse/([A-Z][A-Z0-9_]+-\d+)"', body)
    key = mkey.group(1) if mkey else None
    text = _plain(body)
    # text is kept whole (incl. "KEY (author): " prefix); see _already_present.
    return ExistingTask(int(mid.group(1)) if mid else None, mst.group(1) if mst else "", key, text)


def find_task_list_in_section(storage: str, start: int, end: int):
    """Span (open_start, close_start) of the first top-level <ac:task-list> in [start, end)."""
    token = re.compile(r"<ac:task-list(?=[\s>])[^>]*>|</ac:task-list>")
    depth = 0
    open_start = None
    for m in token.finditer(storage, start, end):
        if m.group(0).startswith("</"):
            if depth > 0:
                depth -= 1
                if depth == 0:
                    return open_start, m.start()
        else:
            if depth == 0:
                open_start = m.start()
            depth += 1
    return None


def extract_release_list_fix_versions(storage: str) -> list:
    versions = []
    for macro in re.finditer(
        r'<ac:structured-macro\b[^>]*ac:name="jira"[^>]*>(.*?)</ac:structured-macro>', storage, re.S
    ):
        for p in re.finditer(
            r'<ac:parameter\b[^>]*ac:name="jqlQuery"[^>]*>(.*?)</ac:parameter>', macro.group(1), re.S
        ):
            jql = html.unescape(re.sub(r"<[^>]+>", "", p.group(1)))
            for fm in re.finditer(r'fixVersion\s*=\s*(?:"([^"]+)"|\'([^\']+)\'|(\S+))', jql):
                versions.append((fm.group(1) or fm.group(2) or fm.group(3)).strip())
    return versions


# ------------------------------------------------------------------------ guard
def check_target_guard(
    page: dict, version: str, label: str = DEFAULT_FIX_VERSION_LABEL, *, parent_id: str,
    accepted_versions=(),
) -> GuardResult:
    parent_id = str(parent_id)
    page = page or {}
    pid = str(page.get("id", "") or "")
    if page.get("error"):
        return GuardResult(False, [f"Page {pid or '?'} not found or not accessible: {page['error']}"], pid, "")
    title = page.get("title", "")
    errors = []
    if pid in FORBIDDEN_PAGE_IDS:
        errors.append(f"Template page {pid} can never be written.")
    space = page.get("space_key")
    if space != ALLOWED_SPACE_KEY:
        errors.append(f"Page is in space {space}, expected {ALLOWED_SPACE_KEY}.")
    ancestors = [str(a) for a in (page.get("ancestor_ids") or [])]
    if pid == parent_id:
        errors.append("Target is the parent page itself.")
    elif str(page.get("parent_id") or "") != parent_id:
        if parent_id in ancestors:
            errors.append(f"Page is a grandchild (not a direct child) of {parent_id}.")
        else:
            errors.append(f"Page is not under {parent_id}.")
    expected = build_fix_version(version, label)
    found = extract_release_list_fix_versions(page.get("storage", "") or "")
    found_str = None
    if not found:
        errors.append(f"Release List Jira macro/JQL not found; expected fixVersion '{expected}'.")
    else:
        found_str = ", ".join(found)
        accepted = {expected} | {build_fix_version(v, label) for v in (accepted_versions or ())}
        if not accepted & {f.strip() for f in found}:
            errors.append(f"fixVersion mismatch: expected '{expected}', found '{found_str}'.")
    return GuardResult(not errors, errors, pid, title, found_str)


# ------------------------------------------------------------------- rendering
def _esc(s: str) -> str:
    return html.escape(s, quote=True)


def render_task(task_id: int, action: PostAction, jira_server: str) -> str:
    base = _esc((jira_server or "").rstrip("/"))
    key = _esc(action.issue_key)
    return (
        f"<ac:task><ac:task-id>{task_id}</ac:task-id><ac:task-status>incomplete</ac:task-status>"
        f'<ac:task-body><a href="{base}/browse/{key}">{key}</a> '
        f"({_esc(action.author)}): {_esc(action.text)}</ac:task-body></ac:task>"
    )


def render_new_task_list(tasks_xml: str) -> str:
    return f'<ul><li style="list-style-type: none;"><ac:task-list>{tasks_xml}</ac:task-list></li></ul>'


# ----------------------------------------------------------------------- merge
def _already_present(existing: list, action: PostAction) -> bool:
    """Author-independent match: existing text is "KEY (<author>): <text>".

    Author may contain ')' or '):', so instead of stripping the prefix we require
    it to start with "KEY (" and end with "): <action text>".
    """
    key = normalise_text(action.issue_key)
    tail = "): " + normalise_text(action.text)
    for k, full in existing:
        if k == action.issue_key and full.startswith(key + " (") and full.endswith(tail):
            return True
    return False


def plan_merge(storage: str, actions: list, jira_server: str) -> MergePlan:
    assert_well_formed(storage)
    h_start, h_end, s_end = find_post_actions_section(storage)
    tasks = find_all_tasks(storage)
    max_id = max((t.task_id for _, _, t in tasks if t.task_id is not None), default=0)
    existing = [
        (t.issue_key, normalise_text(t.text))
        for s, e, t in tasks
        if t.issue_key and h_end <= s < s_end
    ]
    seen = set()
    new, skipped = [], []
    for a in actions:
        ident = (a.issue_key, normalise_text(a.text))
        if ident in seen or _already_present(existing, a):
            skipped.append(a)
        else:
            seen.add(ident)
            new.append(a)
    if not new:
        return MergePlan([], skipped, storage, [])
    ids = list(range(max_id + 1, max_id + 1 + len(new)))
    tasks_xml = "".join(render_task(i, a, jira_server) for i, a in zip(ids, new))
    tl = find_task_list_in_section(storage, h_end, s_end)
    if tl:
        offset, insert = tl[1], tasks_xml
    else:
        offset, insert = h_end, render_new_task_list(tasks_xml)
    new_storage = storage[:offset] + insert + storage[offset:]
    assert_well_formed(new_storage)
    return MergePlan(new, skipped, new_storage, ids)


# ------------------------------------------------------- preview / publish flow
def build_preview(page: dict, issues: list, version: str, label: str, jira_server: str, parent_id: str) -> dict:
    """Never writes. Returns {"guard", "plan", "error"}."""
    guard = check_target_guard(page, version, label, parent_id=parent_id)
    if not guard.ok:
        return {"guard": guard, "plan": None, "error": None}
    try:
        plan = plan_merge(page.get("storage", ""), extract_actions(issues), jira_server)
    except ValueError as e:
        return {"guard": guard, "plan": None, "error": str(e)}
    return {"guard": guard, "plan": plan, "error": None}


def publish(
    get_page: Callable[[], dict],
    update_page: Callable[[str, str, int, str], dict],
    actions: list,
    version: str,
    label: str,
    jira_server: str,
    parent_id: str,
    is_sandbox: Optional[bool] = None,
) -> dict:
    """Re-fetch, re-guard, re-plan, then (only if needed) write once.

    update_page(page_id, title, new_version, storage) -> {"ok": True} | {"error": str}
    """
    source = None
    if is_sandbox is None:  # auto-sandbox: typed version is a key of the env map
        source, is_sandbox = resolve_source_version(version, load_sandbox_config(os.environ))
    elif is_sandbox:
        source, _ = resolve_source_version(version, load_sandbox_config(os.environ))
    accepted = (source,) if source and source != version else ()
    page = get_page()
    guard = check_target_guard(page, version, label, parent_id=parent_id, accepted_versions=accepted)
    if not guard.ok:
        return {"ok": False, "added": 0, "skipped": 0, "error": " ".join(guard.errors)}
    try:
        plan = plan_merge(page.get("storage", ""), actions, jira_server)
    except ValueError as e:
        return {"ok": False, "added": 0, "skipped": 0, "error": str(e)}
    if not plan.new_actions:
        return {"ok": True, "added": 0, "skipped": len(plan.skipped), "error": None}
    try:
        check_write_allowed(guard.page_id, is_sandbox=bool(is_sandbox), target="confluence")
    except PermissionError as e:
        return {"ok": False, "added": 0, "skipped": len(plan.skipped), "error": str(e)}
    res = update_page(guard.page_id, page.get("title", ""), int(page.get("version", 0)) + 1, plan.new_storage)
    if not res or res.get("error") or not res.get("ok"):
        return {"ok": False, "added": 0, "skipped": len(plan.skipped),
                "error": (res or {}).get("error", "Unknown error while updating the page")}
    return {"ok": True, "added": len(plan.new_actions), "skipped": len(plan.skipped), "error": None}
