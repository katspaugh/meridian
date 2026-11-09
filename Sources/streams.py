"""Private command bridge used by the Mac app and per-session Claude hooks."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import stat
import sqlite3
import struct
import subprocess
import termios
import time
import sys
import uuid

# The bridge runs inside the signed app bundle.
sys.dont_write_bytecode = True
import workstream_links as links

ROOT = Path(os.getenv("MUXTEROID_HOME", "~/Library/Application Support/Muxteroid")).expanduser().resolve()
ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
os.environ["PATH"] += os.pathsep + os.pathsep.join(map(str, [Path.home()/".local/bin", "/opt/homebrew/bin", "/usr/local/bin"]))
SOCKET = "muxteroid-" + hashlib.sha256(str(ROOT).encode()).hexdigest()[:12]
LEGACY_TMUX = ["tmux", "-L", SOCKET]
TERMINALS = Path("/tmp") / f"{SOCKET}-{os.getuid()}"


def run(args, **kwargs):
    p = subprocess.run(args, capture_output=True, text=True, **kwargs)
    if p.returncode:
        raise ValueError(p.stderr.strip() or p.stdout.strip() or f"{args[0]} exited {p.returncode}")
    return p.stdout.strip()


def bd(*args):
    env = dict(os.environ, HOME=str(ROOT), BEADS_DIR=str(ROOT/".beads"))
    if not (ROOT/".beads/metadata.json").exists():
        run(["bd", "init", "--quiet", "--stealth", "--prefix", "mx"], cwd=ROOT, env=env)
    return run(["bd", *args], cwd=ROOT, env=env)


def database():
    db = sqlite3.connect(ROOT/"activity.sqlite", timeout=10)
    db.execute("CREATE TABLE IF NOT EXISTS activity (id TEXT PRIMARY KEY, session TEXT, state TEXT, detail TEXT, transcript TEXT)")
    links.initialize(db)
    return db


def agent_context(key, event):
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
Set a child in_progress when starting (bd update CHILD -s in_progress).
Record discoveries as children, link dependencies with bd dep add BLOCKED PREREQUISITE, and record blockers with bd update CHILD -s blocked --append-notes "reason".
Close finished children with bd close CHILD --reason "outcome and verification". Before handing back, reconcile the remaining tasks and leave enough notes to resume.
When you open or change a PR (including pushing commits), or work with a Linear issue, register its canonical URL for the workstream bar with {link_command} URL. Resolve the PR URL with gh pr view when needed.
The user owns completion of the parent. Simple questions can be answered directly.
In these examples, replace bd with the command prefix above."""
    if event == "SessionStart":
        # Include current state after startup, resume and compaction.
        children = json.loads(bd("list", "--parent", key, "--all", "--limit", "0", "--json")) or []
        current = [f"{c['id']} [{c['status']}] {c['title'][:140]}" for c in children if c["status"] != "closed"]
        guide += "\nOpen child tasks at session start:\n" + ("\n".join(current[:40]) or "(none)")
    return guide


def hook(key, sid):
    event = json.load(sys.stdin)
    if event.get("session_id") != sid:
        return
    if event["hook_event_name"] == "PostToolUse":
        with database() as db:
            links.tool_result(db, key, event.get("tool_name", ""), event.get("tool_input", {}),
                              event.get("tool_response", ""), event.get("cwd"), live=True)
    if event.get("agent_id"):
        return
    name = event["hook_event_name"]
    tool = event.get("tool_name", "")
    state, detail = {
        "SessionStart": ("Ready", "Claude is ready for a prompt."),
        "UserPromptSubmit": ("Working", "Claude is working on your request."),
        "PermissionRequest": ("Needs you", f"Permission requested for {tool}."),
        "PreToolUse": ("Needs you", f"Claude is waiting for {tool}."),
        "Notification": ("Needs you", event.get("message", "Permission requested.")),
        "PostToolUse": ("Working", "Claude is continuing."),
        "PostToolUseFailure": ("Working", "Claude is continuing after a tool error."),
        "Stop": ("Review", event.get("last_assistant_message") or "Claude finished its turn."),
        "StopFailure": ("Needs you", event.get("error_details") or "Claude stopped with an error."),
    }[name]
    with database() as db:
        db.execute("UPDATE activity SET state=?,detail=?,transcript=COALESCE(?,transcript) WHERE id=? AND session=?",
                   (state, str(detail) if name == "Stop" else str(detail)[:2000], event.get("transcript_path"), key, sid))

    if name in ("SessionStart", "UserPromptSubmit"):
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": name, "additionalContext": agent_context(key, name)
        }}))


def settings(key, sid):
    command = shlex.join([sys.executable, str(Path(__file__).resolve()), "hook", key, sid])
    hooks = {}
    for event in ("SessionStart", "UserPromptSubmit", "PermissionRequest", "PreToolUse", "Notification",
                  "PostToolUse", "PostToolUseFailure", "Stop", "StopFailure"):
        item = {"hooks": [{"type": "command", "command": command}]}
        if event == "PreToolUse":
            item["matcher"] = "AskUserQuestion|ExitPlanMode"
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
    live = set()
    for path in terminal_directory().iterdir():
        if not path.is_socket():
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
    live = terminals() | legacy_sessions()
    with database() as db:
        runtime = {}
        folders = {i['id']: (i.get('metadata') or {}).get('folder') for i in issues}
        for key, state, detail, transcript in db.execute("SELECT id,state,detail,transcript FROM activity"):
            runtime[key] = state, detail
            links.backfill(db, key, transcript, folders.get(key))
        for issue in issues:
            if folders[issue['id']]:
                links.collect(db, issue['id'], issue.get('external_ref') or '')
        artifacts = {}
        for key, kind, label, url in db.execute("SELECT stream,kind,label,url FROM links ORDER BY kind DESC,rowid"):
            artifacts.setdefault(key, []).append(dict(kind=kind, label=label, url=url))
    children = {}
    for issue in issues:
        if issue.get("parent"):
            children.setdefault(issue["parent"], []).append(
                {k: issue[k] for k in ("id", "title", "status")}
            )
    rows = []
    for issue in issues:
        if issue.get("parent") or not (issue.get("metadata") or {}).get("folder"):
            continue
        key = issue["id"]
        state, detail = runtime.get(key, ("Queued", "Open the terminal to start Claude Code."))
        if issue["status"] == "closed":
            state = "Done"
        elif key in runtime and key not in live and state != "Review":
            state, detail = "Offline", "Open the terminal to continue this stream."
        rows.append(dict(id=key, title=issue["title"], created=issue.get("created_at", ""), folder=(issue.get("metadata") or {}).get("folder", ""),
                         reference=issue.get("external_ref") or "", state=state, detail=detail, live=key in live, tasks=children.get(key, []), links=artifacts.get(key, [])))
    return rows


def launch(key):
    issue = json.loads(bd("show", key, "--json"))[0]
    if issue["status"] == "closed":
        raise ValueError("Reopen this workstream first.")
    if key not in terminals() and key not in legacy_sessions():
        folder = Path(issue["metadata"]["folder"])
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
        if not shutil.which("dtach"):
            raise ValueError("Install dtach with brew install dtach to start the terminal.")
        env = dict(os.environ, TERM="xterm-256color", TERM_PROGRAM="ghostty", COLORTERM="truecolor",
                   BEADS_DIR=str(ROOT/".beads"), MUXTEROID_HOME=str(ROOT), MUXTEROID_ISSUE=key)
        env.pop("TMUX", None)
        env.pop("TMUX_PANE", None)
        run(["dtach", "-n", str(TERMINALS / key), "-r", "winch", *args],
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
    os.execvp("dtach", ["dtach", "-a", str(path), "-E", "-z", "-r", "winch"])


def main(action, *args):
    if action == "hook":
        return hook(*args)
    if action == "link":
        key, url = args
        if not (links.PR.search(url) or links.LINEAR.search(url)):
            raise ValueError("Use a GitHub PR or Linear issue URL.")
        with database() as db:
            links.collect(db, key, url)
        return
    if action == "attach":
        key, = args
        if Path(key).name != key:
            raise ValueError("Invalid workstream ID.")
        path = terminal_directory() / key
        if path.is_socket():
            attach_terminal(path)
        if key in legacy_sessions():
            os.execvp("tmux", LEGACY_TMUX + ["attach-session", "-t", "=" + key])
        raise ValueError("The terminal exited. Start it again to resume Claude.")
    with (ROOT/"bridge.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if action == "list":
            print(json.dumps(streams()))
        elif action == "create":
            title, folder, reference = args
            folder = Path(folder).expanduser().resolve()
            if not folder.is_dir():
                raise ValueError("Choose an existing folder.")
            metadata = {"folder": str(folder)}
            if not title.strip():
                title = "Untitled workstream"
            print(bd("create", "--title", title.strip(), "-t", "task", "--metadata", json.dumps(metadata),
                     "--silent", *(["--external-ref", reference.strip()] if reference.strip() else [])))
        elif action == "open":
            launch(*args)
        elif action in ("close", "reopen"):
            bd(action, *args)
        else:
            raise ValueError("Unknown action")


if __name__ == "__main__":
    try:
        main(*sys.argv[1:])
    except Exception as exc:
        sys.exit(str(exc))
