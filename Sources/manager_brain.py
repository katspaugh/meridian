"""Asks Claude, run headless and read-only, for a structured verdict. Any failure yields None."""
import json
import os
import re
import time
import unicodedata
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
# --output writes a file; --web and -w open the boss's browser.
INVESTIGATE_DENIED = ["Bash(*--output*)", "Bash(*--web*)", "Bash(* -w)", "Bash(* -w *)"]
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


MESSAGES = {"type": "array", "items": _object({
    "stream": STRING, "terminal": STRING, "kind": {"type": "string", "enum": ["status", "next", "reminder", "rule"]},
    "text": STRING, "newSubject": {"type": "boolean"}, "rule": STRING, "ruleEvidence": STRINGS,
    "buttons": {"type": "array", "items": _object({"label": STRING, "action": STRING,
        "terminal": STRING, "value": STRING}, ["label", "action", "terminal", "value"])}},
    ["stream", "terminal", "kind", "text", "newSubject", "rule", "buttons"])}


def _manager(root):
    return Path(root) / "manager"


def config(root):
    result = {"model": "fable", "fallback_model": "opus", "effort": "high"}
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
    if trigger == "comment":
        plan = _object({"id": STRING, "action": {"type": "string", "enum": ["ignore", "follow"]}, "message": STRING},
                       ["id", "action", "message"])
        return _object({"plans": {"type": "array", "items": plan}}, ["plans"])
    if trigger == "sweep":
        return _object({"summary": STRING, "briefing": STRING, "actions": ACTIONS, "asks": ASKS, "messages": MESSAGES},
                       ["summary", "briefing", "actions"])
    if trigger == "chat":
        # Notes go into the system prompt, so only the boss's own chat may add to them.
        return _object({"reply": STRING, "actions": ACTIONS, "remember": STRINGS}, ["reply", "actions"])
    properties = {"verdict": {"type": "string", "enum": VERDICTS[trigger]}, "summary": STRING,
                  "message": STRING, "ask": ASK, "decisions": DECISIONS}
    if trigger == "stop":
        properties["messages"] = MESSAGES
    if trigger == "question":
        properties["answers"] = {"type": "object", "additionalProperties": STRING}
    return _object(properties, ["verdict", "summary", "message"])


def _environment(root):
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
           and not k.startswith("CLAUDE_CODE_")}
    env["BEADS_DIR"] = str(Path(root) / ".beads")
    return env


def _command(claude, root, trigger, investigate, session, resume, folders, model=None):
    settings = config(root)
    tools = TOOLS + (INVESTIGATE_TOOLS if investigate else [])
    rules = [t for t in tools if t != "Bash"] + (INVESTIGATE_RULES if investigate else [])
    prompt = ROLE.read_text(encoding="utf-8").rstrip("\n") + "\n\n# Your notes\n" + notes(root)
    # The consult's folder has no project settings, so the boss's own permission rules cannot widen its tools.
    args = [claude, "-p", "--safe-mode", "--setting-sources", "project", "--model", str(model or settings["model"]), "--effort", str(settings["effort"]),
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


STATUS = "consult_status.json"
LIMIT = re.compile(r"hit your .{0,40}limit|usage limit", re.I)
# Claude's own error text is shown to the boss; anything shaped like a credential is not.
SECRET = re.compile(r"\b(?:sk-ant-[\w-]+|sk-[\w-]{16,}|[A-Za-z0-9_\-]{40,})|[?&][^\s]*(?:token|key|secret)=[^\s&]+", re.I)


def _shown(text, limit=240):
    text = "".join(c if unicodedata.category(c)[0] != "C" else " " for c in str(text))
    text = " ".join(SECRET.sub("[redacted]", text).split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def set_consult_error(root, message):
    """The latest consult failure, for the app's monitoring errors. An empty message clears it."""
    try:
        folder = _manager(root)
        folder.mkdir(parents=True, exist_ok=True)
        temporary = folder / (STATUS + ".tmp")
        temporary.write_text(json.dumps({"error": message, "time": time.time()}), encoding="utf-8")
        os.replace(temporary, folder / STATUS)
    except OSError as exc:
        workstream_log.record(f"manager consult status failed type={type(exc).__name__}")


def consult_error(root):
    try:
        value = json.loads((_manager(root) / STATUS).read_text(encoding="utf-8")).get("error")
    except (OSError, ValueError, AttributeError):
        return ""
    return value if isinstance(value, str) else ""


def _attempt(args, context, timeout, cwd, root, required):
    """Returns (output, failure). A failure carries a log kind, the text shown to the boss and whether it is a usage limit."""
    def fail(kind, shown=None, limit=False):
        return None, dict(kind=kind, shown=shown or kind.replace("-", " "), limit=limit)

    try:
        p = subprocess.run(args, input=context, capture_output=True, text=True, encoding="utf-8", timeout=timeout,
                           cwd=str(cwd), env=_environment(root))
    except subprocess.TimeoutExpired:
        return fail("timeout", "it timed out")
    except Exception as exc:
        return fail(f"error type={type(exc).__name__}", "Claude Code could not be started")
    # Claude prints its JSON result even when it exits with an error; stderr is never read.
    try:
        reply = json.loads(p.stdout)
    except (TypeError, ValueError):
        reply = None
    if isinstance(reply, dict) and reply.get("is_error") is True:
        text = reply.get("result") if isinstance(reply.get("result"), str) else ""
        limit = reply.get("api_error") == "usage_limit_reached" or bool(LIMIT.search(text))
        return fail("usage-limit" if limit else "is-error", _shown(text) or "Claude reported an error", limit)
    if p.returncode != 0:
        return fail(f"exit status={p.returncode}", f"exit status {p.returncode}")
    if not isinstance(reply, dict):
        return fail("unparsed", "its reply could not be read")
    output = reply.get("structured_output")
    if not isinstance(output, dict) or any(key not in output for key in required):
        return fail("incomplete", "its reply was incomplete")
    return output, None


def consult(root, trigger, context, investigate=False, timeout=150, session=None, resume=False, folders=()):
    def fail(kind):
        workstream_log.record(f"manager consult trigger={trigger} failed kind={kind}")

    try:
        claude = shutil.which("claude")
        if claude is None:
            set_consult_error(root, "Meridian could not consult Claude: Claude Code is not installed.")
            return fail("missing-claude")
        settings = config(root)
        required = schema(trigger)["required"]
        cwd = _manager(root)
        cwd.mkdir(parents=True, exist_ok=True)
        models = [str(settings["model"])]
        fallback = settings.get("fallback_model")
        if isinstance(fallback, str) and fallback.strip() and fallback.strip() != models[0]:
            models.append(fallback.strip())
        failures = []
        for index, model in enumerate(models):
            args = _command(claude, root, trigger, investigate, session, resume, folders, model)
            output, failure = _attempt(args, context, timeout, cwd, root, required)
            if failure is None:
                if index:
                    workstream_log.record(f"manager consult trigger={trigger} used fallback model")
                set_consult_error(root, "")
                return output
            fail(failure["kind"] + (" model=fallback" if index else ""))
            failures.append(dict(failure, model=_shown(model, 40)))
            if not failure["limit"]:
                break  # Only a usage limit is worth another model; other failures would fail the same way.
    except Exception as exc:
        set_consult_error(root, "Meridian could not consult Claude. Check that Claude Code is signed in.")
        return fail(f"error type={type(exc).__name__}")
    if failures[0]["limit"]:
        detail = " ".join(f"{f['model']}: {f['shown']}" for f in failures)
        set_consult_error(root, "Meridian's Claude reviews hit a usage limit: " + detail
                          + ("" if len(failures) > 1 else " Set fallback_model in manager/config.json to keep working."))
    else:
        set_consult_error(root, "Meridian's Claude reviews are failing (" + failures[-1]["shown"]
                          + "). Check that Claude Code is signed in.")
    return None
