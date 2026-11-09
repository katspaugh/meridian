"""Switch the existing Claude TUI through its person-initiated mod command."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

CHORD = "ctrl+x ctrl+y"
COMMAND = "command:muxteroid-view"


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
        if bindings.get(CHORD) == COMMAND:
            return False
    except (ValueError, KeyError, TypeError):
        pass
    raise ValueError(f'Add "{CHORD}": "{COMMAND}" to the Chat bindings in {path}, '
                     'or type /muxteroid-view in Claude to open the selection.')


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
        fcntl.flock(lock, fcntl.LOCK_EX)
        request = directory / "request.json"
        tmp = request.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(session=session, agent=agent, token=token)))
        tmp.replace(request)
        if ensure_shortcut():
            # Claude hot-reloads keybindings asynchronously.
            time.sleep(1)
        subprocess.run(["dtach", "-p", str(socket_path)], input=b"\x18\x19", check=True)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                reply = json.loads((directory / "response.json").read_text())
            except (FileNotFoundError, ValueError):
                reply = {}
            if reply.get("token") == token:
                if reply.get("error"):
                    if agent and reply["error"] in ("no agent under that id", "that agent has ended", "that agent is not one the tasks list shows"):
                        return "archive"
                    raise ValueError(reply["error"] + ". Use /tasks to browse Claude’s session history.")
                return "shown"
            time.sleep(0.08)
        raise ValueError("Claude did not confirm the switch. Close any open dialog and try again, "
                         "or run /muxteroid-view in the terminal.")
