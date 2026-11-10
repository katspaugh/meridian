"""Store one Superpowers run per workstream and feed it from agent events.

Hooks run concurrently with each other and with list, so every
read-modify-write of a run goes through update, which holds the database
write lock from the read until the commit.
"""
import json
import os
from pathlib import Path
import re

import pipeline
import workstream_log as trace

DOCUMENTS = (
    ("spec", re.compile(r"/docs/superpowers/specs/[^/]+\.md$")),
    ("plan", re.compile(r"/docs/superpowers/plans/[^/]+\.md$")),
)
WORKSPACE_SUFFIX = re.compile(r"/\.superpowers/sdd/[^/]+$")


def initialize(db):
    db.execute("CREATE TABLE IF NOT EXISTS pipeline_runs (stream TEXT PRIMARY KEY, run TEXT NOT NULL)")
    db.execute("""CREATE TABLE IF NOT EXISTS pipeline_docs
               (stream TEXT, path TEXT, kind TEXT, title TEXT, mtime REAL, PRIMARY KEY(stream,path))""")
    db.execute("CREATE TABLE IF NOT EXISTS pipeline_scans (stream TEXT PRIMARY KEY, signature TEXT NOT NULL)")


def owner(db, key):
    row = db.execute("SELECT stream FROM workstream_terminals WHERE id=?", (key,)).fetchone()
    return row[0] if row else key


def _is_run(run):
    if not isinstance(run, dict) or not isinstance(run.get("tasks"), list):
        return False
    phases = run.get("phases")
    return isinstance(phases, dict) and all(
        isinstance(phases.get(phase), dict) and isinstance(phases[phase].get("status"), str)
        for phase in pipeline.PHASES)


def load(db, stream):
    """A run that cannot be decoded (hand-edited, or from a later version) counts as no run."""
    row = db.execute("SELECT run FROM pipeline_runs WHERE stream=?", (stream,)).fetchone()
    if row is None:
        return None
    try:
        run = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    return run if _is_run(run) else None


def update(db, stream, change):
    # An open Python sqlite3 transaction has already written, so it holds the write lock.
    if not db.in_transaction:
        db.execute("BEGIN IMMEDIATE")
    run = change(load(db, stream))
    if run is None:
        db.execute("DELETE FROM pipeline_runs WHERE stream=?", (stream,))
    else:
        db.execute("INSERT OR REPLACE INTO pipeline_runs VALUES (?,?)", (stream, json.dumps(run)))
    db.commit()
    return run


def reset(db, stream):
    db.execute("DELETE FROM pipeline_runs WHERE stream=?", (stream,))
    db.commit()


def _read(path):
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def record_document(db, stream, kind, path, now, created=True):
    """created false lists the document but never lets it start a run."""
    try:
        text = Path(path).read_text(encoding="utf-8")
        mtime = os.stat(path).st_mtime
    except (OSError, UnicodeError):
        return
    heading = next((line[2:].strip() for line in text.split("\n") if line.startswith("# ")), "")
    title = heading or pipeline.slug_name(path)
    is_new = created and db.execute("SELECT 1 FROM pipeline_docs WHERE stream=? AND path=?",
                                    (stream, path)).fetchone() is None
    db.execute("INSERT OR REPLACE INTO pipeline_docs VALUES (?,?,?,?,?)", (stream, path, kind, title, mtime))
    parsed = pipeline.parse_plan_tasks(text) if kind == "plan" else []
    update(db, stream, lambda run: pipeline.document_event(run, kind, path, parsed, is_new, now))


def _response_text(response):
    if isinstance(response, dict) and isinstance(response.get("stdout"), str):
        return response["stdout"]
    if isinstance(response, str):
        return response
    return json.dumps(response)


def tool_event(db, key, name, args, response, now):
    stream = owner(db, key)
    lower = name.lower()
    if name == "Skill":
        skill = args.get("skill") or ""
        update(db, stream, lambda run: pipeline.skill_event(run, skill, now))
    elif name in ("Write", "Edit"):
        path = args.get("file_path") or ""
        kind = next((kind for kind, pattern in DOCUMENTS if pattern.search(path)), None)
        if kind is None:
            return
        # An Edit, or a Write over an existing file, never starts a run.
        overwrote = isinstance(response, dict) and response.get("type") not in (None, "create")
        record_document(db, stream, kind, path, now, created=name == "Write" and not overwrote)
    elif name == "Bash":
        command_event(db, key, args.get("command") or "", _response_text(response), now)
    elif "github" in lower and "create_pull_request" in lower:
        match = pipeline.PR_URL.search(_response_text(response))
        if match:
            update(db, stream, lambda run: pipeline.pull_request_created(run, match.group(0), now))


def _workspace_plan(workspace):
    marker = _read(Path(workspace) / "plan-path")
    marker = marker.strip() if marker else ""
    if not marker:
        return None
    if os.path.isabs(marker):
        return marker
    return os.path.join(WORKSPACE_SUFFIX.sub("", workspace), marker)


def command_event(db, key, command, output, now):
    stream = owner(db, key)
    workspace = pipeline.workspace_path(command, output)
    if workspace is None:
        update(db, stream, lambda run: pipeline.command_event(run, command, output, now))
        return
    plan_path = _workspace_plan(workspace)
    text = _read(plan_path) if plan_path else None
    parsed = pipeline.parse_plan_tasks(text) if text is not None else []
    update(db, stream, lambda run: pipeline.attach_workspace(run, workspace, plan_path, parsed, now))


def guarded(db, default, function, *args, **kwargs):
    """The caller must have no uncommitted writes of its own on db: they are rolled back."""
    try:
        return function(*args, **kwargs)
    except Exception as error:
        db.rollback()
        trace.record(f"pipeline failed type={type(error).__name__}")
        return default
