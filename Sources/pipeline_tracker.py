"""Store one Superpowers run per workstream and feed it from agent events.

Hooks run concurrently with each other and with list, so every
read-modify-write of a run goes through update, which holds the database
write lock from the read until the commit.
"""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess

import pipeline
import workstream_log as trace

DOCUMENTS = (
    ("spec", re.compile(r"/docs/superpowers/specs/[^/]+\.md$")),
    ("plan", re.compile(r"/docs/superpowers/plans/[^/]+\.md$")),
)
DOCUMENT_FOLDERS = (("spec", "docs/superpowers/specs"), ("plan", "docs/superpowers/plans"))
WORKSPACE_SUFFIX = re.compile(r"/\.superpowers/sdd/[^/]+$")
PATH_FIELDS = ("specPath", "planPath", "workspace")
GIT_RETRY_SECONDS = 60
ISO_TIME = re.compile(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})?$")


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
    if not _is_run(run):
        return None
    # A path field that is not text counts as absent, so it cannot break every refresh.
    for field in PATH_FIELDS:
        if field in run and not isinstance(run[field], str):
            del run[field]
    return run


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
    # One file has one row, whichever spelling (a symlinked folder) reached it.
    path = os.path.realpath(path)
    try:
        text = Path(path).read_text(encoding="utf-8")
        mtime = os.stat(path).st_mtime
    except (OSError, UnicodeError):
        return
    heading = next((line[2:].strip() for line in text.split("\n") if line.startswith("# ")), "")
    title = heading or pipeline.slug_name(path)
    # Lock before the lookup, so two creating writes of one path cannot both see it as new.
    if not db.in_transaction:
        db.execute("BEGIN IMMEDIATE")
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


def _text_argument(args, field):
    """The field's text, "" when it is absent or null, None when it is not text."""
    value = args.get(field)
    if value is None:
        return ""
    return value if isinstance(value, str) else None


def tool_event(db, key, name, args, response, now):
    if not isinstance(name, str) or not isinstance(args, dict):
        return
    stream = owner(db, key)
    lower = name.lower()
    if name == "Skill":
        skill = _text_argument(args, "skill")
        if skill is not None:
            update(db, stream, lambda run: pipeline.skill_event(run, skill, now))
    elif name in ("Write", "Edit"):
        path = _text_argument(args, "file_path")
        kind = next((kind for kind, pattern in DOCUMENTS if pattern.search(path)), None) if path else None
        if kind is None:
            return
        # An Edit, or a Write over an existing file, never starts a run.
        overwrote = isinstance(response, dict) and response.get("type") not in (None, "create")
        record_document(db, stream, kind, path, now, created=name == "Write" and not overwrote)
    elif name == "Bash":
        command = _text_argument(args, "command")
        if command is not None:
            command_event(db, key, command, _response_text(response), now)
    elif "github" in lower and "create_pull_request" in lower:
        match = pipeline.PR_URL.search(_response_text(response))
        if match:
            update(db, stream, lambda run: pipeline.pull_request_created(run, match.group(0), now))


def _workspace_plan(workspace):
    marker = _read(Path(workspace) / "plan-path")
    marker = marker.strip() if marker else ""
    if not marker:
        return None
    # Real, like a recorded document's path, so the run's plan compares equal to it.
    return os.path.realpath(os.path.join(WORKSPACE_SUFFIX.sub("", workspace), marker))


def command_event(db, key, command, output, now):
    stream = owner(db, key)
    workspace = pipeline.workspace_path(command, output)
    if workspace is None:
        # Only a workspace can start a run, so other commands take no write lock without one.
        if load(db, stream) is not None:
            update(db, stream, lambda run: pipeline.command_event(run, command, output, now))
        return
    plan_path = _workspace_plan(workspace)
    text = _read(plan_path) if plan_path else None
    parsed = pipeline.parse_plan_tasks(text) if text is not None else []
    update(db, stream, lambda run: pipeline.attach_workspace(run, workspace, plan_path, parsed, now))


def timestamp(text):
    """An ISO time such as 2026-11-10T05:41:25Z as epoch seconds, 0.0 when it cannot be parsed."""
    match = ISO_TIME.match(text.strip()) if isinstance(text, str) else None
    if match is None:
        return 0.0
    zone = match.group(8) or "Z"
    try:
        offset = timedelta(0)
        if zone != "Z":
            digits = zone[1:].replace(":", "")
            offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
            offset = offset if zone[0] == "+" else -offset
        moment = datetime(*(int(part) for part in match.groups()[:6]), tzinfo=timezone(offset))
        return moment.timestamp() + float("0." + (match.group(7) or "0"))
    except (ValueError, OverflowError):
        return 0.0


def _listing(root):
    """{real path: (kind, stat)} for the regular Markdown files in the two document folders of a real root."""
    found = {}
    for kind, folder in DOCUMENT_FOLDERS:
        try:
            with os.scandir(os.path.join(root, folder)) as entries:
                for entry in entries:
                    try:
                        if entry.name.endswith(".md") and entry.is_file():
                            path = os.path.realpath(entry.path) if entry.is_symlink() else entry.path
                            found[path] = kind, entry.stat()
                    except OSError:
                        continue
        except OSError:
            continue
    return found


def _untracked(root, candidates, own_worktree):
    """The candidates Git does not track (ignored ones included), or None when Git cannot answer."""
    try:
        result = subprocess.run(["git", "-C", root, "ls-files", "-z", "--others", "--",
                                 *(folder for _, folder in DOCUMENT_FOLDERS)],
                                capture_output=True, encoding="utf-8", errors="replace", timeout=4,
                                env=dict(os.environ, LC_ALL="C"))
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        # A folder used directly that is not a repository has nothing tracked.
        if not own_worktree and "not a git repository" in result.stderr:
            return set(candidates)
        return None
    named = {os.path.realpath(os.path.join(root, line)) for line in result.stdout.split("\0") if line}
    return {path for path in candidates if path in named}


def _git_failed_at(stored):
    """When the stored scan record is a Git failure, its time; otherwise None."""
    try:
        record = json.loads(stored)
    except (TypeError, ValueError):
        return None
    failed_at = record.get("gitFailedAt") if isinstance(record, dict) else None
    return failed_at if isinstance(failed_at, (int, float)) else None


def _scan(db, stream, root, created, own_worktree, now):
    row = db.execute("SELECT signature FROM pipeline_scans WHERE stream=?", (stream,)).fetchone()
    stored = row[0] if row else "[]"
    failed_at = _git_failed_at(stored)
    # Git failed lately: wait a minute before asking again, so a broken checkout costs no subprocess per refresh.
    if failed_at is not None and failed_at <= now < failed_at + GIT_RETRY_SECONDS:
        return
    root = os.path.realpath(root)
    listing = _listing(root)
    signature = json.dumps(sorted([path, stat.st_mtime_ns] for path, (_, stat) in listing.items()))
    if signature == stored:
        return
    known = dict(db.execute("SELECT path,mtime FROM pipeline_docs WHERE stream=?", (stream,)))
    candidates = [path for path in listing if path not in known]
    adopted = _untracked(root, candidates, own_worktree) if candidates else set()
    if adopted is not None and not own_worktree:
        adopted = {path for path in adopted if listing[path][1].st_mtime > created}
    changed = {path for path in listing if path in known and listing[path][1].st_mtime != known[path]}
    for path in sorted((adopted or set()) | changed, key=lambda path: listing[path][1].st_mtime):
        record_document(db, stream, listing[path][0], path, now)
    # A Git failure is recorded instead of the signature, so the scan is retried after a minute.
    record = signature if adopted is not None else json.dumps({"gitFailedAt": now})
    db.execute("INSERT OR REPLACE INTO pipeline_scans VALUES (?,?)", (stream, record))
    db.commit()


def _poll_ledger(db, stream, root, now):
    run = load(db, stream)          # path fields that are not text were dropped
    if run is None or pipeline.is_finished(run):
        return
    identity = run.get("launchedAt"), run.get("planPath")
    workspace = run.get("workspace")
    if not workspace:
        if not run.get("planPath"):
            return
        workspace = os.path.join(root, ".superpowers", "sdd", re.sub(r"\.md$", "", os.path.basename(run["planPath"])))
        if not os.path.isfile(os.path.join(workspace, "plan-path")):
            return
    ledger = os.path.join(workspace, "progress.md")
    try:
        mtime = os.stat(ledger).st_mtime
    except OSError:
        return
    if mtime == run.get("ledgerMtime"):
        return
    text = _read(ledger)
    if text is None:
        return

    def change(run):
        # A hook may have started another run since the load: this ledger is not its own.
        if run is None or pipeline.is_finished(run) or (run.get("launchedAt"), run.get("planPath")) != identity:
            return run
        pipeline.advance_to(run, "execute", now)
        pipeline.apply_ledger(run, text, now)
        run["workspace"] = workspace
        run["ledgerMtime"] = mtime
        return run
    update(db, stream, change)


def _documents(db, stream):
    rows = db.execute("SELECT path,kind,title FROM pipeline_docs WHERE stream=? ORDER BY mtime DESC,path",
                      (stream,)).fetchall()
    gone = {path for path, _, _ in rows if not os.path.exists(path)}
    if gone:
        db.executemany("DELETE FROM pipeline_docs WHERE stream=? AND path=?", [(stream, path) for path in gone])
        db.commit()
    return [{"kind": kind, "title": title, "path": path} for path, kind, title in rows if path not in gone]


def snapshot(db, stream, root, *, created, own_worktree, closed, waiting, active_agents, now):
    """The rendered run (or None) and the documents, newest first.

    A refresh in which no file changed only reads: list runs every three
    seconds for every workstream while hooks contend for the write lock.
    """
    if not closed:
        _scan(db, stream, root, created, own_worktree, now)
        _poll_ledger(db, stream, root, now)
    documents = _documents(db, stream)
    run = load(db, stream)
    view = pipeline.render(run, now, waiting, active_agents, frozen=closed) if run is not None else None
    return view, documents


def guarded(db, default, function, *args, **kwargs):
    """The caller must have no uncommitted writes of its own on db: they are rolled back."""
    try:
        return function(*args, **kwargs)
    except Exception as error:
        db.rollback()
        # Only names: never paths or payload text.
        name = getattr(function, "__name__", type(function).__name__)
        trace.error(f"pipeline failed type={type(error).__name__} in={name}")
        return default
