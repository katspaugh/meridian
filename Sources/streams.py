"""Private command bridge used by the Mac app and per-session Claude hooks."""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import socket
import stat
import sqlite3
import struct
import subprocess
import termios
import time
import sys
import uuid
from urllib.parse import unquote, urlsplit

# The bridge runs inside the signed app bundle.
sys.dont_write_bytecode = True
import workstream_log as trace
import workstream_links as links
import subagents
import agent_navigation
import codex_session
import pipeline_tracker as tracker
import manager
import manager_store

ROOT = Path(os.getenv("MUXTEROID_HOME", "~/Library/Application Support/Muxteroid")).expanduser().resolve()
ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
os.environ["PATH"] += os.pathsep + os.pathsep.join(map(str, [Path.home()/".local/bin", "/opt/homebrew/bin", "/usr/local/bin"]))
SOCKET = "muxteroid-" + hashlib.sha256(str(ROOT).encode()).hexdigest()[:12]
LEGACY_TMUX = ["tmux", "-L", SOCKET]
TERMINALS = Path("/tmp") / f"{SOCKET}-{os.getuid()}"


def run(args, **kwargs):
    # Full arguments can include prompts, paths and plugin settings.
    operation = " ".join(args[:2])
    trace.record(f"{operation} begin")
    p = subprocess.run(args, capture_output=True, text=True, **kwargs)
    trace.record(f"{operation} exited status={p.returncode}")
    if p.returncode:
        raise ValueError(p.stderr.strip() or p.stdout.strip() or f"{args[0]} exited {p.returncode}")
    return p.stdout.strip()


def bd(*args):
    env = dict(os.environ, HOME=str(ROOT), BEADS_DIR=str(ROOT/".beads"))
    if not (ROOT/".beads/metadata.json").exists():
        run(["bd", "init", "--quiet", "--stealth", "--prefix", "mx"], cwd=ROOT, env=env)
    return run(["bd", *args], cwd=ROOT, env=env)


def database():
    trace.record("activity database open")
    db = sqlite3.connect(ROOT/"activity.sqlite", timeout=10)
    db.execute("CREATE TABLE IF NOT EXISTS activity (id TEXT PRIMARY KEY, session TEXT, state TEXT, detail TEXT, transcript TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS workstream_terminals (id TEXT PRIMARY KEY, stream TEXT NOT NULL, provider TEXT NOT NULL, title TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS closed_terminals (id TEXT PRIMARY KEY)")
    db.execute("CREATE TABLE IF NOT EXISTS codex_children (stream TEXT, id TEXT, title TEXT, role TEXT, state TEXT, PRIMARY KEY(stream,id))")
    links.initialize(db)
    subagents.initialize(db)
    tracker.initialize(db)
    manager_store.initialize(db)
    trace.record("activity database ready")
    return db


@contextlib.contextmanager
def bridge_lock():
    with (ROOT/"bridge.lock").open("a") as lock:
        trace.record("waiting for bridge.lock")
        fcntl.flock(lock, fcntl.LOCK_EX)
        trace.record("acquired bridge.lock")
        yield


PROVIDERS = {"claude": "Claude", "codex": "Codex", "shell": "Terminal"}


def workstream_folder(folder):
    """Allocate one checkout for the stream, independently of its agent provider."""
    metadata = {"folder": str(folder), "sourceFolder": str(folder)}
    probe = subprocess.run(["git", "-C", str(folder), "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, env=dict(os.environ, LC_ALL="C"))
    if probe.returncode:
        if "not a git repository" in probe.stderr:
            return metadata
        raise ValueError(probe.stderr.strip() or "Cannot inspect the selected Git repository.")
    repository = Path(probe.stdout.strip()).resolve()
    relative = folder.relative_to(repository)
    try:
        commit = run(["git", "-C", str(repository), "rev-parse", "--verify", "HEAD^{commit}"])
    except ValueError as error:
        raise ValueError("The repository needs an initial commit before creating a workstream.") from error
    if relative != Path('.'):
        try:
            kind = run(["git", "-C", str(repository), "cat-file", "-t", f"{commit}:{relative.as_posix()}"])
        except ValueError:
            kind = None
        if kind != "tree":
            raise ValueError("The selected subfolder is not committed. Commit it first or choose the repository root.")
    name = uuid.uuid4().hex[:12]
    branch = "muxteroid/" + name
    checkout = ROOT / "worktrees" / name
    checkout.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "-C", str(repository), "worktree", "add", "-b", branch, str(checkout), commit])
    metadata.update(folder=str(checkout / relative), worktreeRoot=str(checkout),
                    worktreeBranch=branch, repository=str(repository))
    return metadata


def initial_stream_title(folder, reference):
    """Name the stream from its reference or original folder, before checkout allocation."""
    reference = reference.strip()
    if reference:
        if match := links.PR.match(reference):
            return f'{match[2]}#{match[3]}'
        if match := links.LINEAR.match(reference):
            return match['id'].upper()
        try:
            url = urlsplit(reference)
            if url.scheme in ('http', 'https') and url.hostname:
                return unquote(url.path.rstrip('/').rsplit('/', 1)[-1]) or url.hostname
        except ValueError:
            pass
        return reference
    return folder.name or str(folder)


def create_stream(title, folder, reference, provider="claude"):
    if provider not in ("claude", "codex"):
        raise ValueError("Unknown agent.")
    folder = Path(folder).expanduser().resolve()
    if not folder.is_dir():
        raise ValueError("Choose an existing folder.")
    metadata = workstream_folder(folder)
    metadata["provider"] = provider
    try:
        return bd("create", "--title", title.strip() or initial_stream_title(folder, reference), "-t", "task",
                  "--metadata", json.dumps(metadata), "--silent",
                  *(["--external-ref", reference.strip()] if reference.strip() else []))
    except Exception as error:
        if metadata.get("worktreeRoot"):
            try:
                # Never force removal: preserve files if something changed during creation.
                run(["git", "-C", str(folder), "worktree", "remove", metadata["worktreeRoot"]])
                run(["git", "-C", str(folder), "branch", "-d", metadata["worktreeBranch"]])
            except ValueError as cleanup:
                raise ValueError(f"{error}\nWorktree cleanup needs attention: {metadata['worktreeRoot']}\n{cleanup}") from error
        raise


def register_original(db, issue):
    """Keep the original socket/activity ID so running pre-upgrade sessions survive."""
    provider = issue["metadata"].get("provider", "claude")
    db.execute("INSERT OR IGNORE INTO workstream_terminals VALUES (?,?,?,?)",
               (issue["id"], issue["id"], provider, PROVIDERS[provider]))


def terminal_owner(key):
    with database() as db:
        row = db.execute("SELECT stream FROM workstream_terminals WHERE id=?", (key,)).fetchone()
    return row[0] if row else key


def terminal_issue(key, allow_closed=False):
    parent = terminal_owner(key)
    issue = json.loads(bd("show", parent, "--json"))[0]
    if issue.get("parent") or not (issue.get("metadata") or {}).get("folder"):
        raise ValueError("Not a workstream.")
    with database() as db:
        register_original(db, issue)
        row = db.execute("SELECT provider FROM workstream_terminals WHERE id=? AND stream=?", (key, parent)).fetchone()
        if not allow_closed and db.execute("SELECT 1 FROM closed_terminals WHERE id=?", (key,)).fetchone():
            raise ValueError("This terminal is closed.")
    if not row:
        raise ValueError("Unknown terminal.")
    return issue, row[0]


def add_terminal(parent, provider):
    if provider not in PROVIDERS:
        raise ValueError("Choose a plain terminal, Claude, or Codex.")
    issue, _ = terminal_issue(parent, allow_closed=True)
    if issue["id"] != parent or issue["status"] == "closed":
        raise ValueError("Reopen this workstream before adding a terminal.")
    key = "term-" + uuid.uuid4().hex[:16]
    with database() as db:
        count = db.execute("SELECT count(*) FROM workstream_terminals WHERE stream=? AND provider=?", (parent, provider)).fetchone()[0]
        title = PROVIDERS[provider] + (f" {count + 1}" if count else "")
        db.execute("INSERT INTO workstream_terminals VALUES (?,?,?,?)", (key, parent, provider, title))
    try:
        launch(key)
    except Exception:
        # Keep a successfully started process discoverable even if launch reporting failed.
        if key not in terminals():
            with database() as db:
                db.execute("DELETE FROM workstream_terminals WHERE id=?", (key,))
                db.execute("DELETE FROM activity WHERE id=?", (key,))
        raise
    return key


def mark_terminal_closed(key):
    with database() as db:
        db.execute("INSERT OR IGNORE INTO closed_terminals VALUES (?)", (key,))


def use_shell(key):
    with database() as db:
        if db.execute("SELECT 1 FROM closed_terminals WHERE id=?", (key,)).fetchone():
            return False
        db.execute("UPDATE workstream_terminals SET provider='shell',title='Terminal' WHERE id=?", (key,))
        # Invalidates late agent hooks without deleting saved transcripts or links.
        db.execute("UPDATE activity SET session=NULL,state='Ready',detail='Shell is ready.' WHERE id=?", (key,))
    return True


def run_terminal(key, command):
    """The PTY outlives its agent; the shell's exit ends the terminal itself."""
    _, provider = terminal_issue(key)
    def stop(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGHUP, stop)
    signal.signal(signal.SIGTERM, stop)
    # Let the foreground program interpret Ctrl+C without interrupting this supervisor.
    signal.signal(signal.SIGINT, lambda signum, frame: None)
    tty = termios.tcgetattr(0) if os.isatty(0) else None
    try:
        subprocess.run(command, check=False)
        if provider != 'shell' and use_shell(key):
            if tty is not None:
                termios.tcsetattr(0, termios.TCSANOW, tty)
                print('\033[?1049l\033[?1000l\033[?1002l\033[?1003l\033[?1006l\033[?2004l\033[0m', end='', flush=True)
            subprocess.run([os.environ.get('SHELL') or '/bin/zsh', '-l'], check=False)
    finally:
        mark_terminal_closed(key)


def close_terminal(key):
    terminal_issue(key, allow_closed=True)
    if key in terminals():
        path = str(TERMINALS / key)
        # The socket identifies the owning dtach daemon, never an agent by its name.
        result = subprocess.run(['/usr/sbin/lsof', '-t', '-a', '-U', '-c', 'dtach', path],
                                capture_output=True, text=True)
        pids = [int(p) for p in result.stdout.split() if p.isdigit()]
        if not pids:
            raise ValueError('Cannot find the terminal process. Try closing it again.')
        mark_terminal_closed(key)
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    elif key in legacy_sessions():
        run(LEGACY_TMUX + ['kill-session', '-t', '=' + key])
        mark_terminal_closed(key)
    else:
        mark_terminal_closed(key)


def terminal_exited(key):
    """Handle sessions launched before the agent-to-shell supervisor was installed."""
    with database() as db:
        if db.execute("SELECT 1 FROM closed_terminals WHERE id=?", (key,)).fetchone():
            return
    if key in terminals() or key in legacy_sessions():
        return
    issue, provider = terminal_issue(key)
    if provider == 'shell' or issue['status'] == 'closed':
        mark_terminal_closed(key)
    elif use_shell(key):
        launch(key)


def agent_context(key, event):
    key = terminal_owner(key)
    # Always provide an explicit tracker path. Already-running sessions predate the
    # workstream environment below, and a working directory can have its own database.
    cli = "BEADS_DIR=" + shlex.quote(str(ROOT/".beads")) + " " + shlex.quote(shutil.which("bd") or "bd")
    link_command = shlex.join([sys.executable, str(Path(__file__).resolve()), "link", key])
    guide = f"""Use Beads proactively as your durable task manager for Muxteroid workstream {key}.
Run your Beads commands with this prefix: {cli}
Inspect the parent with bd show {key}, then bd children {key} --json before planning or resuming.
Maintain the parent title as a short semantic summary of the current goal. As soon as the goal is clear, use bd update {key} --title "Concise task name". Use the conversation, relevant findings, and linked issue details to describe the concrete outcome in roughly 3–8 words (for example, "Recover interrupted photo uploads"). Avoid generic placeholders, paths, ticket IDs, and copied prompt fragments.
Reconsider the title when a clarification or investigation reveals a more accurate goal. Update it in that same turn, including when the change becomes clear midway through the work. Keep an accurate title through routine progress. Honor any title the user explicitly asks to keep.
For implementation work, reuse matching children or create actionable steps with bd create --parent {key} -t task --title "step".
Keep all internal tasks directly under {key}. Use bd ready --parent {key} to choose unblocked work.
Other terminals may share this workstream, folder, worktree, and task list. Check current task ownership before editing; do not overwrite another terminal's changes. Keep the parent title about the shared goal, not just your current assignment.
Set a child in_progress when starting (bd update CHILD -s in_progress).
Record discoveries as children and link dependencies with bd dep add BLOCKED PREREQUISITE.
When marking a task blocked, always record both the specific missing condition and the concrete next action in the same command: bd update CHILD -s blocked --set-metadata blocked_reason="What prevents progress" --set-metadata unblock_action="Exactly what the user should do, where to do it, and how to tell the agent it is ready" --append-notes "Reason and next action". The app displays these fields under Why it’s blocked and To unblock. Use plain language, not internal IDs or vague requests to resolve the blocker. If another task or external event must finish, name it and say whether the user needs to act or just wait. For example: blocked_reason="No workstream has two different live providers to verify the overlapping icons" and unblock_action="In this workstream, use + beside the terminal tabs to add Claude alongside Codex, then tell the agent to verify the sidebar".
Refresh both fields whenever the blocker changes. When resuming, clear them with bd update CHILD -s in_progress --unset-metadata blocked_reason --unset-metadata unblock_action. Do not mark a task blocked merely because an optional check is pending if you can still make progress.
Close finished children with bd close CHILD --reason "outcome and verification". Before handing back, reconcile the remaining tasks and leave enough notes to resume.
When you open or change a PR (including pushing commits), or work with a Linear issue, register its canonical URL with the workstream by running {link_command} URL. Resolve the PR URL with gh pr view when needed.
The user owns completion of the parent. Simple questions can be answered directly.
In these examples, replace bd with the command prefix above."""
    if event == "SessionStart":
        # Include current state after startup, resume and compaction.
        children = json.loads(bd("list", "--parent", key, "--all", "--limit", "0", "--json")) or []
        current = [f"{c['id']} [{c['status']}] {c['title'][:140]}" for c in children if c["status"] != "closed"]
        guide += "\nOpen child tasks at session start:\n" + ("\n".join(current[:40]) or "(none)")
    return guide


def interrupted(signum, frame):
    raise RuntimeError(f"Interrupted by signal {signum}.")


def hook(key, sid):
    event = json.load(sys.stdin)
    if event.get("session_id") != sid:
        return
    # A hook killed mid-review must still replace the "manager is reviewing" line; unwinding
    # through subprocess.run also kills the consult.
    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, interrupted)
    # First, with no connection open here: a review may consult the manager for minutes.
    try:
        outcome = manager.review(sys.modules[__name__], key, sid, event)
    except Exception as error:
        trace.record(f"manager review failed type={type(error).__name__}")
        outcome = manager.NOTHING
    name = event["hook_event_name"]
    tool = event.get("tool_name", "")
    # PreToolUse fires for every tool; only questions and plans wait for the user.
    waits = name != "PreToolUse" or tool in manager.REVIEWED_TOOLS
    if name == "PostToolUse":
        with database() as db:
            links.tool_result(db, key, tool, event.get("tool_input", {}),
                              event.get("tool_response", ""), event.get("cwd"), live=True)
    if event.get("agent_id"):
        if waits and outcome.output is None:
            with database() as db:
                subagents.record(db, key, sid, event)
    elif outcome.state or waits:
        state, detail = (outcome.state, outcome.detail or "") if outcome.state else {
            "SessionStart": ("Ready", "Claude is ready for a prompt."),
            "UserPromptSubmit": ("Working", "Claude is working on your request."),
            "PermissionRequest": ("Needs you", f"Permission requested for {tool}."),
            "PermissionDenied": ("Working", manager.CONTINUING),
            "PreToolUse": ("Needs you", f"Claude is waiting for {tool}."),
            "Notification": ("Needs you", event.get("message", "Permission requested.")),
            "PostToolUse": ("Working", "Claude is continuing."),
            "PostToolUseFailure": ("Working", "Claude is continuing after a tool error."),
            "Stop": ("Review", event.get("last_assistant_message") or "Claude finished its turn."),
            "StopFailure": ("Needs you", event.get("error_details") or "Claude stopped with an error."),
        }[name]
        with database() as db:
            # A permission prompt's notification follows it within seconds, while the manager may still settle it.
            reviewing = name == "Notification" and str((db.execute(
                "SELECT detail FROM activity WHERE id=? AND session=?", (key, sid)).fetchone() or [""])[0] or ""
            ).startswith("The manager is ")
            if not reviewing:
                db.execute("UPDATE activity SET state=?,detail=?,transcript=COALESCE(?,transcript) WHERE id=? AND session=?",
                           (state, str(detail) if name == "Stop" else str(detail)[:2000], event.get("transcript_path"),
                            key, sid))
        if name in ("SessionStart", "UserPromptSubmit"):
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": name, "additionalContext": agent_context(key, name)
            }}))
    if outcome.output is not None:
        print(json.dumps(outcome.output))
    if name == "PostToolUse" and not event.get("agent_id"):
        # Its own connection, after the state update, so a wait on the run's
        # write lock never delays the state.
        with database() as db:
            tracker.guarded(db, None, tracker.tool_event, db, key, tool,
                            event.get("tool_input") or {}, event.get("tool_response"), time.time())


def settings(key, sid):
    command = shlex.join([sys.executable, str(Path(__file__).resolve()), "hook", key, sid])
    hooks = {}
    # Hooks the manager may review wait for its consult.
    timeouts = {"PermissionRequest": 180, "PermissionDenied": 180, "PreToolUse": 180, "Stop": 300}
    for event in ("SessionStart", "UserPromptSubmit", "PermissionRequest", "PermissionDenied", "PreToolUse",
                  "Notification", "PostToolUse", "PostToolUseFailure", "Stop", "StopFailure",
                  "SubagentStart", "SubagentStop"):
        item = {"hooks": [{"type": "command", "command": command}]}
        if event in timeouts:
            item["hooks"][0]["timeout"] = timeouts[event]
        if event == "Notification":
            item["matcher"] = "permission_prompt|elicitation_dialog"
        hooks[event] = [item]
    return json.dumps({"hooks": hooks})


def legacy_sessions():
    # Only contact tmux when an earlier release left a running server.
    base = Path(os.getenv("TMUX_TMPDIR") or "/tmp") / f"tmux-{os.getuid()}"
    if not (base / SOCKET).is_socket() or not shutil.which("tmux"):
        return set()
    p = subprocess.run(LEGACY_TMUX + ["list-sessions", "-F", "#{session_name}"], capture_output=True, text=True)
    if p.returncode and not any(s in p.stderr for s in ("no server", "No such file", "no sessions", "Connection refused")):
        raise ValueError(p.stderr.strip())
    return set(p.stdout.splitlines())


def terminal_directory():
    TERMINALS.mkdir(mode=0o700, exist_ok=True)
    info = TERMINALS.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError(f"Invalid private terminal directory: {TERMINALS}")
    return TERMINALS


def terminals():
    trace.record("discovering terminal sockets")
    live = set()
    for path in terminal_directory().iterdir():
        if path.name.endswith(".sock") or not path.is_socket():
            continue
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(1)
            try:
                probe.connect(str(path))
            except FileNotFoundError:
                continue
            except ConnectionRefusedError:
                # A killed daemon leaves its socket behind. Launch and list
                # are serialized by bridge.lock, so a replacement is safe.
                path.unlink(missing_ok=True)
                continue
        live.add(path.name)
    return live


def streams():
    issues = json.loads(bd("list", "--all", "--limit", "0", "--json")) or []
    attached = terminals()
    live = attached | legacy_sessions()
    with database() as db:
        runtime = {}
        agents = {}
        parents = {i['id']: i for i in issues if not i.get('parent') and (i.get('metadata') or {}).get('folder')}
        for issue in parents.values():
            register_original(db, issue)
        registry = list(db.execute("SELECT id,stream,provider,title FROM workstream_terminals ORDER BY rowid"))
        closed = {row[0] for row in db.execute("SELECT id FROM closed_terminals")}
        owners = {key: parent for key, parent, _, _ in registry}
        providers = {key: provider for key, _, provider, _ in registry}
        folders = {key: parents[parent]['metadata']['folder'] for key, parent, _, _ in registry if parent in parents}
        sessions = {}
        for key, sid, state, detail, transcript in db.execute("SELECT id,session,state,detail,transcript FROM activity"):
            sessions[key] = sid
            runtime[key] = state, detail
            if providers.get(key) == "shell":
                continue
            if providers.get(key) == "codex":
                agents[key] = [dict(id=i,title=t,role=r,state=("Offline" if key not in live and st in ("Working","Needs you") else st),revision="")
                               for i,t,r,st in db.execute("SELECT id,title,role,state FROM codex_children WHERE stream=?", (key,))]
                continue
            agents[key] = subagents.discover(db, key, sid, transcript, key in live)
            links.backfill(db, key, transcript, folders.get(key))
        for issue in parents.values():
            links.collect(db, issue['id'], issue.get('external_ref') or '')
        artifacts = {}
        for key, kind, label, url in db.execute("SELECT stream,kind,label,url FROM links ORDER BY kind DESC,rowid"):
            items = artifacts.setdefault(owners.get(key, key), [])
            if not any(item['url'] == url for item in items):
                items.append(dict(kind=kind, label=label, url=url))
    children = {}
    for issue in issues:
        if issue.get("parent"):
            children.setdefault(issue["parent"], []).append(
                {**{k: issue[k] for k in ("id", "title", "status")}, "notes": issue.get("notes") or "",
                 "blockedReason": (issue.get("metadata") or {}).get("blocked_reason"),
                 "unblockAction": (issue.get("metadata") or {}).get("unblock_action")}
            )
    rows = []
    for issue in issues:
        if issue.get("parent") or not (issue.get("metadata") or {}).get("folder"):
            continue
        key = issue["id"]
        entries = []
        for tid, parent, provider, title in registry:
            if parent != key or tid in closed:
                continue
            state, detail = runtime.get(tid, ("Queued", "Open the terminal to start."))
            if tid in runtime and tid not in live and (state != "Review" or provider == "shell"):
                state, detail = "Offline", "Start this terminal to continue."
            sid = sessions.get(tid)
            entries.append(dict(id=tid, title=title, provider=provider, state=state, detail=detail,
                                live=tid in live, terminalSocket=str(TERMINALS / tid) if tid in attached else None,
                                session=sid, nativeView=bool(sid and (ROOT/"views"/sid).is_dir()),
                                agents=[dict(a, terminalID=tid) for a in agents.get(tid, [])
                                        if a['state'] in ('Working', 'Needs you')]))
        # An idle shell does not mask finished agent work. A working agent does.
        priority = {"Needs you": 0, "Working": 1, "Review": 2, "Ready": 3, "Queued": 4, "Offline": 5}
        representative = min(entries, key=lambda t: priority.get(t['state'], 6)) if entries else dict(state='Ready', detail='Add a terminal to continue this workstream.')
        state, detail = representative['state'], representative['detail']
        meta = issue["metadata"]
        stream_agents = [a for t in entries for a in t['agents']]
        pipeline_view, documents = tracker.guarded(
            db, (None, []), tracker.snapshot, db, key, meta.get("worktreeRoot") or meta["folder"],
            created=tracker.timestamp(issue.get("created_at") or ""), own_worktree=bool(meta.get("worktreeRoot")),
            closed=issue["status"] == "closed", waiting=state in ("Needs you", "Review"),
            active_agents=sum(1 for a in stream_agents if a.get("state") in ("Working", "Needs you")), now=time.time())
        if issue["status"] == "closed":
            state = "Done"
        rows.append(dict(id=key, terminalSocket=str(TERMINALS / key) if key in attached else None, provider=providers[key], title=issue["title"], created=issue.get("created_at", ""), folder=(issue.get("metadata") or {}).get("folder", ""),
                         sourceFolder=issue["metadata"].get("sourceFolder"), worktreeBranch=issue["metadata"].get("worktreeBranch"),
                         reference=issue.get("external_ref") or "", state=state, detail=detail, live=any(t['live'] for t in entries), tasks=children.get(key, []), links=artifacts.get(key, []),
                         agents=stream_agents, terminals=entries, pipeline=pipeline_view, documents=documents,
                         session=sessions.get(key), nativeView=bool(sessions.get(key) and (ROOT/"views"/sessions[key]).is_dir())))
    return rows


def launch(key):
    issue, provider = terminal_issue(key)
    if issue["status"] == "closed":
        raise ValueError("Reopen this workstream first.")
    if key not in terminals() and key not in legacy_sessions():
        folder = Path(issue["metadata"]["folder"])
        if not folder.is_dir():
            raise ValueError("Check the workstream folder.")
        if not shutil.which("dtach"):
            raise ValueError("Install dtach with brew install dtach to start the terminal.")
        env = dict(os.environ, TERM="xterm-256color", TERM_PROGRAM="ghostty", COLORTERM="truecolor",
                   BEADS_DIR=str(ROOT/".beads"), MUXTEROID_HOME=str(ROOT), MUXTEROID_ISSUE=issue['id'], MUXTEROID_TERMINAL=key)
        for name in ("TMUX", "TMUX_PANE", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
            env.pop(name, None)
        if provider == "shell":
            shell = os.environ.get("SHELL") or "/bin/zsh"
            with database() as db:
                db.execute("INSERT OR REPLACE INTO activity VALUES (?,NULL,'Ready','Shell is ready.',NULL)", (key,))
            run(["dtach", "-n", str(TERMINALS/key), "-r", "winch", sys.executable, str(Path(__file__).resolve()), "terminal-run", key, shell, "-l"],
                cwd=folder, env=env, stdin=subprocess.DEVNULL)
            return
        if provider == "codex":
            if not shutil.which("codex") or not folder.is_dir():
                raise ValueError("Install Codex CLI and check the workstream folder.")
            with database() as db:
                db.execute("INSERT OR IGNORE INTO activity VALUES (?,NULL,'Ready','Opening Codex.',NULL)", (key,))
            run(["dtach","-n",str(TERMINALS/key),"-r","winch",sys.executable,str(Path(__file__).resolve()),"terminal-run",key,
                 sys.executable,str(Path(__file__).resolve()),"codex-run",key],
                cwd=folder,env=env,stdin=subprocess.DEVNULL)
            return
        claude = shutil.which("claude")
        if not folder.is_dir() or not claude:
            raise ValueError("Check the workstream folder and Claude Code installation.")
        with database() as db:
            old = db.execute("SELECT session,transcript FROM activity WHERE id=?", (key,)).fetchone()
            resume = old and old[1] and Path(old[1]).is_file()
            sid = old[0] if resume else str(uuid.uuid4())
            db.execute("INSERT OR REPLACE INTO activity VALUES (?,?,?,?,?)",
                       (key, sid, "Ready", "Opening Claude Code.", old[1] if resume else None))
        args = [claude, "--resume" if resume else "--session-id", sid, "--settings", settings(key, sid)]
        view_dir = ROOT / "views" / sid
        view_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        env["MUXTEROID_VIEW_DIR"] = str(view_dir)
        # Claude generates type definitions in the plugin directory at runtime.
        # Keep that writable directory outside the signed application bundle.
        source = Path(__file__).with_name("agent-view")
        plugin = view_dir / "plugin"
        for relative in (".claude-plugin/plugin.json", "hooks/hooks.json", "hooks/register.js"):
            target = plugin / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / relative, target)
        args.extend(["--plugin-dir", str(plugin)])
        env.pop("TMUX", None)
        env.pop("TMUX_PANE", None)
        run(["dtach", "-n", str(TERMINALS / key), "-r", "winch", sys.executable, str(Path(__file__).resolve()), "terminal-run", key, *args],
            cwd=folder, env=env, stdin=subprocess.DEVNULL)



def attach_terminal(path):
    # Ink skips layout when the size is unchanged. A fresh Ghostty surface is
    # blank even if it happens to have the previous client's dimensions.
    if os.fork() == 0:
        try:
            time.sleep(0.25)
            size = fcntl.ioctl(0, termios.TIOCGWINSZ, bytes(8))
            rows, cols, x, y = struct.unpack("HHHH", size)
            if rows and cols > 1:
                temporary = struct.pack("HHHH", rows, cols - 1, x, y)
                fcntl.ioctl(0, termios.TIOCSWINSZ, temporary)
                time.sleep(0.15)
                if fcntl.ioctl(0, termios.TIOCGWINSZ, bytes(8)) == temporary:
                    fcntl.ioctl(0, termios.TIOCSWINSZ, size)
        finally:
            os._exit(0)
    trace.record("handing terminal to dtach")
    os.execvp("dtach", ["dtach", "-a", str(path), "-E", "-z", "-r", "winch"])


def main(action, *args):
    if action == "usage":
        import usage_limits
        print(json.dumps(usage_limits.report(ROOT)))
        return
    if action == "hook":
        return hook(*args)
    # The manager's actions take bridge.lock only around their snapshot: a sweep or chat consults for minutes.
    if action == "manager-state":
        print(json.dumps(manager.state(sys.modules[__name__])))
        return
    if action == "manager-sweep":
        return manager.sweep(sys.modules[__name__], (args[0] if args else None) or None)
    if action == "manager-chat":
        text, = args
        return manager.chat(sys.modules[__name__], text)
    if action in ("manager-pause", "manager-dismiss", "manager-seen", "manager-focus"):
        with database() as db:
            if action == "manager-focus":
                # The terminal the boss has on screen, or "" when none is or the app is in the background.
                manager_store.set_setting(db, "focus", args[0] if args else "")
            elif action == "manager-pause":
                value, = args
                if value not in ("0", "1"):
                    raise ValueError("Use manager-pause 0 or 1.")
                manager_store.set_setting(db, "paused", value)
            elif action == "manager-dismiss":
                ask, = args
                manager_store.resolve_asks(db, ask_id=int(ask))
            else:
                manager_store.mark_seen(db)
        return
    if action == "terminal-run":
        return run_terminal(args[0], list(args[1:]))
    if action == "codex-run":
        key, = args
        with database() as db:
            codex_session.run(key,db,terminal_directory(),ROOT/".beads",agent_context(key,"SessionStart"))
        return
    if action == "codex-child":
        key, child = args
        with database() as db:
            if not db.execute("SELECT 1 FROM codex_children WHERE stream=? AND id=?", (key,child)).fetchone():
                raise ValueError("Unknown Codex subagent.")
        path = codex_session.endpoint(terminal_directory(),key)
        if not path.is_socket():
            raise ValueError("Start the workstream to open this subagent.")
        trace.record("handing terminal to Codex child")
        os.execvp("codex", ["codex","--remote","unix://"+str(path),"resume",child])
    if action == "view-agent":
        key, identity = args
        if Path(key).name != key:
            raise ValueError("Invalid workstream ID.")
        with database() as db:
            print(agent_navigation.switch(db, ROOT, terminal_directory() / key, key, identity))
        return
    if action == "agent-archive":
        with database() as db:
            entries = subagents.messages(db, *args)
        import tempfile
        with tempfile.TemporaryFile(mode="w+") as transcript:
            transcript.write("Saved subagent transcript · snapshot\n\n")
            for entry in entries:
                clean = "".join(c for c in entry["text"] if c in "\n\t" or c.isprintable())
                transcript.write(entry["label"] + "\n" + clean + "\n\n")
            if not entries:
                transcript.write("No messages were recorded.")
            transcript.seek(0)
            trace.record("opening transcript pager")
            subprocess.run(["/usr/bin/less", "-+F"], stdin=transcript, env=dict(os.environ, LESSSECURE="1"))
        return
    if action == "pulls":
        # Network lookups stay outside bridge.lock so they never delay the list refresh.
        print(json.dumps(links.pull_details(args)))
        return
    if action == "link":
        key, url = args
        if not (links.PR.search(url) or links.LINEAR.search(url)):
            raise ValueError("Use a GitHub PR or Linear issue URL.")
        with database() as db:
            links.collect(db, key, url)
        return
    if action == "pipeline-reset":
        stream, = args
        with database() as db:
            tracker.reset(db, stream)
        return
    if action == "pipeline-poll":
        # gh may take seconds per pull request, so the poll never holds bridge.lock.
        with database() as db:
            tracker.poll_pull_requests(db, args, time.time())
        return
    if action == "attach":
        key, = args
        if Path(key).name != key:
            raise ValueError("Invalid workstream ID.")
        path = terminal_directory() / key
        if path.is_socket():
            attach_terminal(path)
        if key in legacy_sessions():
            trace.record("handing terminal to tmux")
            os.execvp("tmux", LEGACY_TMUX + ["attach-session", "-t", "=" + key])
        raise ValueError("The terminal exited. Start it again to resume the workstream.")
    with bridge_lock():
        if action == "list":
            print(json.dumps(streams()))
        elif action == "create":
            print(create_stream(*args))
        elif action == "open":
            launch(*args)
        elif action == "add-terminal":
            print(add_terminal(*args))
        elif action == "close-terminal":
            close_terminal(*args)
        elif action == "terminal-exited":
            terminal_exited(*args)
        elif action in ("close", "reopen"):
            bd(action, *args)
        else:
            raise ValueError("Unknown action")


if __name__ == "__main__":
    try:
        action = sys.argv[1]
        if action in ("open", "attach", "codex-run", "codex-child", "agent-archive", "view-agent"):
            trace.begin(action, sys.argv[2])
        elif action in ("manager-sweep", "manager-chat"):
            trace.begin(action, "-")
        elif action == "create" or (action == "list" and os.getenv("MUXTEROID_TRACE_REFRESH") == "1"):
            trace.begin(action, "-")
        main(*sys.argv[1:])
        trace.record("finished")
    except Exception as exc:
        trace.record(f"failed type={type(exc).__name__}")
        sys.exit(str(exc))
