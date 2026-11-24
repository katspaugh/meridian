"""Switch the existing Claude TUI through its person-initiated mod command."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

import workstream_log as trace

CHORD = "ctrl+x ctrl+y"
COMMAND = "command:meridian-view"
LEGACY_COMMAND = "command:muxteroid-view"


def existing_view_alias():
    # Register the old name only for installations with that saved keybinding.
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    try:
        contents = (config / "keybindings.json").read_text()
    except OSError:
        return None
    return LEGACY_COMMAND.removeprefix("command:") if LEGACY_COMMAND in contents else None


def ensure_shortcut():
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    path = config / "keybindings.json"
    config.mkdir(parents=True, exist_ok=True)
    block = {"context": "Chat", "bindings": {CHORD: COMMAND}}
    # Never rewrite someone's JSONC file, comments, or existing shortcuts.
    try:
        with path.open("x") as file:
            json.dump({"bindings": [block]}, file, indent=2)
            file.write("\n")
        return True
    except FileExistsError:
        pass
    try:
        blocks = json.loads(path.read_text())["bindings"]
        bindings = {}
        for item in blocks:
            if item["context"] in ("Chat", "Global"):
                bindings.update(item["bindings"])
        if bindings.get(CHORD) in (COMMAND, LEGACY_COMMAND):
            return False
    except (ValueError, KeyError, TypeError):
        pass
    raise ValueError(f'Add "{CHORD}": "{COMMAND}" to the Chat bindings in {path}, '
                     'or type /meridian-view in Claude to open the selection.')


def _read_view(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def _showing_agent(directory, session):
    request = _read_view(directory / "request.json")
    if request.get("session") != session:
        return False
    reply = _read_view(directory / "response.json")
    if request.get("token") and reply.get("token") == request["token"] and not reply.get("error"):
        return bool(request.get("agent"))
    # A denied or unanswered switch leaves the previously confirmed view in place.
    return request.get("wasShowingAgent", False)


def switch(db, root, socket_path, key, identity):
    row = db.execute("SELECT session FROM activity WHERE id=?", (key,)).fetchone()
    if not row or not socket_path.is_socket():
        raise ValueError("Start the workstream terminal first.")
    session = row[0]
    directory = root / "views" / session
    if not directory.is_dir():
        raise ValueError("This Claude session predates sidebar switching. Use /tasks in its terminal, "
                         "or exit Claude and start the terminal again to enable sidebar switching.")
    agent = None
    if identity:
        parent, _, agent = identity.partition("/")
        if parent != session or not agent:
            raise ValueError("This agent belongs to an earlier Claude session.")
    token = str(uuid.uuid4())
    with (directory / "switch.lock").open("a") as lock:
        trace.record("waiting for switch.lock")
        fcntl.flock(lock, fcntl.LOCK_EX)
        trace.record("acquired switch.lock")
        request = directory / "request.json"
        tmp = request.with_suffix(".tmp")
        was_showing_agent = _showing_agent(directory, session)
        tmp.write_text(json.dumps(dict(session=session, agent=agent, token=token,
                                       wasShowingAgent=was_showing_agent)))
        tmp.replace(request)
        # Opening a workstream normally just focuses its terminal. The Chat shortcut
        # is only needed to enter an agent or return from one we actually opened.
        if agent is None and not was_showing_agent:
            return "shown"
        if ensure_shortcut():
            # Claude hot-reloads keybindings asynchronously.
            time.sleep(1)
        trace.record("sending Claude switch shortcut")
        subprocess.run(["dtach", "-p", str(socket_path)], input=b"\x18\x19", check=True)
        trace.record("waiting for Claude switch reply")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            reply = _read_view(directory / "response.json")
            if reply.get("token") == token:
                if reply.get("error"):
                    if agent and reply["error"] in ("no agent under that id", "that agent has ended", "that agent is not one the tasks list shows"):
                        return "archive"
                    raise ValueError(reply["error"] + ". Use /tasks to browse Claude’s session history.")
                return "shown"
            time.sleep(0.08)
        raise ValueError("Claude did not confirm the switch. Close any open dialog and try again, "
                         "or run /meridian-view in the terminal.")
