"""Manual check of the manager against the real Claude Code: python3 Tests/manager_live.py

Not a unit test, and not run by `unittest discover`: it calls the real `claude` once per trigger, on your own
login, in a temporary MUXTEROID_HOME. Every event is read-only in nature; nothing asks for anything to be
deleted, moved, installed or sent. Approvals and answers are only printed: no agent runs.
"""
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Sources"))
import manager
import manager_store as store

TERMINAL, STREAM, SESSION = "t1", "w1", "live-session"
HEADINGS = ("Unreleased", "Next release")


class Bridge:
    """The shape of the stub bridges in test_manager.py: one workstream, one Claude terminal, one blocked task."""

    def __init__(self, root, folder):
        self.ROOT = root
        self.path = root / "activity.sqlite"
        self.rows = [dict(
            id=STREAM, title="Changelog for the export option", folder=str(folder), state="Working", links=[],
            tasks=[dict(id=STREAM + ".1", title="Write the changelog entry", status="blocked", notes="",
                        blockedReason="The heading for the new section is not decided",
                        unblockAction=f"Tell the agent whether to use '{HEADINGS[0]}' or '{HEADINGS[1]}'")],
            terminals=[dict(id=TERMINAL, title="Claude", provider="claude", state="Working", detail="", live=True)])]
        db = sqlite3.connect(self.path)
        with db:
            db.execute("CREATE TABLE activity (id TEXT PRIMARY KEY, session TEXT, state TEXT, detail TEXT, transcript TEXT)")
            db.execute("INSERT INTO activity VALUES (?,?,'Working','Claude is working.',NULL)", (TERMINAL, SESSION))
            store.initialize(db)
        db.close()

    def database(self):
        return sqlite3.connect(self.path, timeout=10)

    def streams(self):
        return self.rows

    @contextlib.contextmanager
    def bridge_lock(self):
        yield

    def terminal_owner(self, key):
        return STREAM if key == TERMINAL else key


def event(name, folder, **fields):
    return dict(dict(hook_event_name=name, session_id=SESSION, cwd=str(folder), permission_mode="auto"), **fields)


def events(folder):
    status = {"command": "git status", "description": "Show the working tree status"}
    question = {"questions": [{
        "question": "Which heading should the new changelog section use?", "header": "Heading", "multiSelect": False,
        "options": [{"label": HEADINGS[0], "description": "The usual name for changes not yet released."},
                    {"label": HEADINGS[1], "description": "Names the section after the coming release."}]}]}
    plan = ("1. Add a section at the top of CHANGELOG.md for the coming release.\n"
            "2. Under it, add one line describing the new CSV export option.")
    return [
        ("permission", event("PermissionRequest", folder, tool_name="Bash", tool_input=status)),
        ("denied", event("PermissionDenied", folder, tool_name="Bash", tool_input=status,
                         reason="Auto mode did not allow running git in this folder.")),
        ("question", event("PreToolUse", folder, tool_name="AskUserQuestion", tool_input=question)),
        ("plan", event("PreToolUse", folder, tool_name="ExitPlanMode", tool_input={"plan": plan},
                       permission_mode="plan")),
        ("stop", event("Stop", folder, stop_hook_active=False,
                       last_assistant_message=("I have drafted the changelog entry for the CSV export option. "
                                               f"Should the new section be headed '{HEADINGS[0]}' or "
                                               f"'{HEADINGS[1]}'?"))),
    ]


def main():
    with tempfile.TemporaryDirectory() as home:
        root = Path(home).resolve()
        os.environ["MUXTEROID_HOME"] = str(root)
        folder = root / "project"
        folder.mkdir()
        (folder / "CHANGELOG.md").write_text("# Changelog\n\n## 1.2.0\n\n- Added PDF export.\n", encoding="utf-8")
        (folder / "README.md").write_text("# Exporter\n\nExports reports as PDF and CSV.\n", encoding="utf-8")
        bridge = Bridge(root, folder)
        print(f"MUXTEROID_HOME={root}")
        for trigger, item in events(folder):
            start = time.monotonic()
            try:
                outcome = manager.review(bridge, TERMINAL, SESSION, item)
            except Exception as exc:
                outcome = f"raised {type(exc).__name__}: {exc}"
            seconds = time.monotonic() - start
            print(f"\n== {trigger} ({item['hook_event_name']}) · {seconds:.1f} s")
            if isinstance(outcome, manager.Outcome):
                if outcome == manager.NOTHING:
                    print("outcome: nothing (no decision; the hook behaves as without the manager)")
                print("output:", json.dumps(outcome.output, indent=2) if outcome.output is not None else None)
                print("state:", outcome.state, "·", outcome.detail)
            else:
                print("outcome:", outcome)
        with contextlib.closing(bridge.database()) as db:
            print("\n== recorded events")
            for row in db.execute("SELECT trigger, verdict, summary, detail FROM manager_events ORDER BY id"):
                print(" ·", *row)
            print("== open asks")
            for row in db.execute("SELECT summary, options, recommendation FROM manager_asks WHERE status='open'"):
                print(" ·", *row)


if __name__ == "__main__":
    main()
