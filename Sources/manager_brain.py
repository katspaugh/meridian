"""Asks Claude, run headless and read-only, for a structured verdict. Any failure yields None."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import workstream_log

ROLE = Path(__file__).resolve().with_name("manager_role.md")
TOOLS = ["Read", "Grep", "Glob"]
INVESTIGATE_TOOLS = ["WebFetch", "WebSearch", "Bash"]
# No git: a repository's own config can make git run programs, and agents own those repositories.
INVESTIGATE_RULES = [f"Bash({command} *)" for command in (
    "gh pr view", "gh pr list", "gh pr checks", "gh issue view", "gh run view", "gh run list",
    "bd show", "bd list", "bd ready", "bd children")]
# --output writes a file; --web opens the boss's browser.
INVESTIGATE_DENIED = ["Bash(*--output*)", "Bash(*--web*)"]
VERDICTS = {"permission": ["allow", "deny", "ask"], "denied": ["approve", "uphold", "ask"],
            "question": ["answer", "ask"], "plan": ["approve", "revise", "ask"],
            "stop": ["stop", "continue", "ask"]}
STRING = {"type": "string"}
STRINGS = {"type": "array", "items": STRING}


def _object(properties, required):
    return {"type": "object", "properties": properties, "required": required}


ASK = _object({"summary": STRING, "options": STRINGS, "recommendation": STRING},
              ["summary", "options", "recommendation"])
DECISIONS = {"type": "array", "items": _object({"decision": STRING, "why": STRING, "undo": STRING},
                                               ["decision", "why", "undo"])}
ACTIONS = {"type": "array", "items": _object(
    {"terminal": STRING, "action": {"type": "string", "enum": ["tell", "restart"]}, "message": STRING},
    ["terminal", "action", "message"])}
ASKS = {"type": "array", "items": _object(
    {"stream": STRING, "terminal": STRING, "summary": STRING, "options": STRINGS, "recommendation": STRING},
    ["stream", "terminal", "summary", "options", "recommendation"])}


def _manager(root):
    return Path(root) / "manager"


def config(root):
    result = {"model": "fable", "effort": "high"}
    try:
        override = json.loads((_manager(root) / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return result
    if isinstance(override, dict):
        result.update(override)
    return result


def notes(root):
    try:
        return (_manager(root) / "notes.md").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return ""


def remember(root, items):
    lines = [" ".join(str(item).split()) for item in items]
    lines = [f"- {line}\n" for line in lines if line]
    if not lines:
        return
    _manager(root).mkdir(parents=True, exist_ok=True)
    with open(_manager(root) / "notes.md", "a", encoding="utf-8") as f:
        f.write("".join(lines))


def schema(trigger):
    if trigger == "sweep":
        return _object({"summary": STRING, "briefing": STRING, "actions": ACTIONS, "asks": ASKS},
                       ["summary", "briefing", "actions"])
    if trigger == "chat":
        # Notes go into the system prompt, so only the boss's own chat may add to them.
        return _object({"reply": STRING, "actions": ACTIONS, "remember": STRINGS}, ["reply", "actions"])
    properties = {"verdict": {"type": "string", "enum": VERDICTS[trigger]}, "summary": STRING,
                  "message": STRING, "ask": ASK, "decisions": DECISIONS}
    if trigger == "question":
        properties["answers"] = {"type": "object", "additionalProperties": STRING}
    return _object(properties, ["verdict", "summary", "message"])


def _environment(root):
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
           and not k.startswith("CLAUDE_CODE_")}
    env["BEADS_DIR"] = str(Path(root) / ".beads")
    return env


def _command(claude, root, trigger, investigate, session, resume, folders):
    settings = config(root)
    tools = TOOLS + (INVESTIGATE_TOOLS if investigate else [])
    rules = [t for t in tools if t != "Bash"] + (INVESTIGATE_RULES if investigate else [])
    prompt = ROLE.read_text(encoding="utf-8").rstrip("\n") + "\n\n# Your notes\n" + notes(root)
    # The consult's folder has no project settings, so the boss's own permission rules cannot widen its tools.
    args = [claude, "-p", "--safe-mode", "--setting-sources", "project", "--model", str(settings["model"]), "--effort", str(settings["effort"]),
            "--output-format", "json", "--json-schema", json.dumps(schema(trigger)),
            "--system-prompt", prompt, "--tools", ",".join(tools), "--allowedTools", *rules,
            "--permission-mode", "dontAsk"]
    if investigate:
        args += ["--disallowedTools", *INVESTIGATE_DENIED]
    for folder in folders:
        if os.path.isdir(folder):
            args += ["--add-dir", str(folder)]
    if session is None:
        args.append("--no-session-persistence")
    else:
        args += ["--resume" if resume else "--session-id", session]
    return args


def consult(root, trigger, context, investigate=False, timeout=150, session=None, resume=False, folders=()):
    def fail(kind):
        workstream_log.record(f"manager consult trigger={trigger} failed kind={kind}")

    try:
        claude = shutil.which("claude")
        if claude is None:
            return fail("missing-claude")
        args = _command(claude, root, trigger, investigate, session, resume, folders)
        required = schema(trigger)["required"]
        cwd = _manager(root)
        cwd.mkdir(parents=True, exist_ok=True)
        p = subprocess.run(args, input=context, capture_output=True, text=True, encoding="utf-8", timeout=timeout,
                           cwd=str(cwd), env=_environment(root))
    except subprocess.TimeoutExpired:
        return fail("timeout")
    except Exception as exc:
        return fail(f"error type={type(exc).__name__}")
    if p.returncode != 0:
        return fail(f"exit status={p.returncode}")
    try:
        reply = json.loads(p.stdout)
    except (TypeError, ValueError):
        return fail("unparsed")
    if not isinstance(reply, dict):
        return fail("unparsed")
    if reply.get("is_error") is True:
        return fail("is-error")
    output = reply.get("structured_output")
    if not isinstance(output, dict) or any(key not in output for key in required):
        return fail("incomplete")
    return output
