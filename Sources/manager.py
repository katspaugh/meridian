"""The workstream manager: reviews what Claude agents stop on and turns its verdicts into hook output."""
import collections
import contextlib
import hashlib
import json
import re
import subprocess
import time
import unicodedata
import uuid

import manager_brain as brain
import manager_store as store
import workstream_log

Outcome = collections.namedtuple("Outcome", "output state detail")
NOTHING = Outcome(None, None, None)
REVIEWED_TOOLS = {"AskUserQuestion": "question", "ExitPlanMode": "plan"}
TRIGGERS = {"PermissionRequest": "permission", "PermissionDenied": "denied", "Stop": "stop"}
APPROVED = "Approved by the workstream manager."
CONTINUING = "Claude is continuing after auto mode blocked an action."
TAIL_BYTES = 512 * 1024
RETRY = {"hookSpecificOutput": {"hookEventName": "PermissionDenied", "retry": True}}
STUCK = ("The manager sent this agent back to work three times in a row and it stopped again. "
         "Have a look at where it is stuck.")
REVIEWING = {"permission": "The manager is reviewing a permission request for {tool}.",
             "denied": "The manager is reviewing a permission request for {tool}.",
             "question": "The manager is reading Claude’s question.",
             "plan": "The manager is reviewing Claude’s plan.",
             "stop": "The manager is reviewing where this work stands."}
SITUATIONS = {
    "permission": "Claude Code is showing a permission prompt for {tool}. Return `allow` to let it run once, "
                  "`deny` with a `message` telling the agent what to do instead, or `ask` to hand it to the boss.",
    "denied": "Auto mode's classifier blocked {tool}. Return `approve` to let the agent retry this exact action once, "
              "`uphold` to keep the block, or `ask` to hand it to the boss.",
    "question": "Claude is asking the boss a question. Return `answer` with `answers`, an object keyed by the exact "
                "text of each question, answering every one of them, or `ask` to hand it to the boss.",
    "plan": "Claude is presenting a plan for approval. Return `approve`, `revise` with a `message` saying what to "
            "change, or `ask` to hand it to the boss.",
    "stop": "Claude has ended its turn. Return `stop` to let it stop, `continue` with a `message` telling it what to "
            "do next, or `ask` to hand it to the boss.",
}
FIELDS = ("`summary` is one line for the boss's activity log. `message` is what the agent is told. "
          "With `ask`, the `ask` object is what the boss sees: the decision, the real options, and your recommendation "
          "if you have one. List any product decision you take under `decisions`.")
ACTIONS = ("`tell` types `message` into a terminal as a prompt; it works only on Claude and Codex terminals that are "
           "idle, Review or Ready. `restart` starts an Offline terminal again; leave its `message` empty.")
SWEEP = ("# Trigger: sweep\nYou are looking over every workstream for what no hook reports: a Codex terminal that "
         "stopped or asked something, an idle agent whose blocker has since cleared, a stopped terminal with ready "
         "work. Return `briefing`, a short Markdown summary for the boss across all workstreams, conclusion first: "
         "what is left, what is blocked, what needs the boss. Use `actions` to keep work moving. " + ACTIONS +
         " Put anything only the boss can decide under `asks`, naming its workstream and terminal. "
         "`summary` is one line for the boss's activity log.")
CHAT = ("# Trigger: chat\nThe boss is writing to you. Answer in `reply`, conclusion first. When the boss asks you to "
        "tell an agent something or to restart a terminal, put it under `actions`. " + ACTIONS +
        " Add any preference or standing instruction the boss states to `remember`.")
SWEEP_PAUSED = "You are paused: return no actions."
CHAT_ACTION = "The boss asked for this in chat."
UNREACHABLE = "The manager could not be reached. Check that Claude Code is signed in, then try again."
NUDGE_CAP = ("The manager nudged this agent three times and it is waiting again. "
             "Have a look at where it is stuck.")
# Only an idle agent: a dialog can be on screen while the state still says Working, and Enter would accept it.
TELLABLE = ("Review", "Ready")
SWEEP_INTERVAL = 120
# What follows an approved block, so the log shows whether the hook's approval got the action through.
AFTER_APPROVAL = {"retry-allowed": "Let the approved retry of {tool} through before auto mode's check.",
                  "prompt-allowed": "Answered the permission prompt for the approved {tool} action.",
                  "blocked-again": "Auto mode blocked the approved {tool} action again; told Claude it may retry.",
                  "ran": "The approved {tool} action ran.",
                  "failed": "The approved {tool} action ran and failed."}


@contextlib.contextmanager
def transaction(bridge):
    db = bridge.database()
    try:
        with db:
            yield db
    finally:
        db.close()


def pre_tool(decision, **fields):
    return {"hookSpecificOutput": dict({"hookEventName": "PreToolUse", "permissionDecision": decision}, **fields)}


def permission(decision):
    return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}}


def after_approval(db, stream, key, verdict, tool):
    store.record_event(db, stream, key, "denied", verdict, AFTER_APPROVAL[verdict].format(tool=tool), {"message": ""})


def review(bridge, key, sid, event):
    name = event.get("hook_event_name")
    if name == "UserPromptSubmit":
        with transaction(bridge) as db:
            store.note_user_prompt(db, key, event.get("prompt", ""))
        return NOTHING
    tool, tool_input = event.get("tool_name") or "", event.get("tool_input") or {}
    subagent = bool(event.get("agent_id"))
    if name in ("PostToolUse", "PostToolUseFailure"):
        # Approvals are one-off: once the approved action has run, it needs a fresh review.
        with transaction(bridge) as db:
            if store.revoke(db, key, tool, tool_input):
                after_approval(db, bridge.terminal_owner(key), key,
                               "ran" if name == "PostToolUse" else "failed", tool)
            if not subagent:
                store.resolve_asks(db, terminal=key)  # The agent has moved on.
        return NOTHING
    trigger = REVIEWED_TOOLS.get(tool) if name == "PreToolUse" else TRIGGERS.get(name)
    if name == "PreToolUse" and trigger is None:
        with transaction(bridge) as db:
            allowed = not store.paused(db) and store.approved(db, key, tool, tool_input)
            if allowed:
                after_approval(db, bridge.terminal_owner(key), key, "retry-allowed", tool)
        return Outcome(pre_tool("allow", permissionDecisionReason=APPROVED), None, None) if allowed else NOTHING
    # Claude Code reports subagent turns as SubagentStop; a subagent Stop is never reviewed.
    # Questions and plans are reviewed at PreToolUse, so their permission prompts are left alone.
    if trigger is None or subagent and trigger == "stop" or trigger == "permission" and tool in REVIEWED_TOOLS:
        return NOTHING
    stream = bridge.terminal_owner(key)
    with transaction(bridge) as db:
        if store.paused(db):
            return NOTHING
        if trigger == "permission" and store.approved(db, key, tool, tool_input):
            after_approval(db, stream, key, "prompt-allowed", tool)
            return status(Outcome(permission({"behavior": "allow"}), "Working", APPROVED), subagent)
        if trigger == "denied" and store.approved(db, key, tool, tool_input):
            after_approval(db, stream, key, "blocked-again", tool)
            return Outcome(RETRY, None, None)
        # The boss chose to have the manager review permission prompts only in auto mode.
        if trigger == "permission" and event.get("permission_mode") != "auto":
            return NOTHING
        if trigger == "stop" and store.nudges(db, key) >= store.MAX_NUDGES:
            store.open_ask(db, stream, key, STUCK, [], "")
            return Outcome(None, "Needs you", STUCK)
        if not store.allow_consult(db, key):
            return NOTHING
        if not subagent:
            db.execute("UPDATE activity SET state=?,detail=? WHERE id=? AND session=?",
                       ("Working", REVIEWING[trigger].format(tool=tool), key, sid))
    # Only the snapshot is taken under the bridge lock; the consult runs without it.
    with bridge.bridge_lock():
        rows = bridge.streams()
    row = next((r for r in rows if r.get("id") == stream), {})
    # The interim line has been written; a denial with no decision must still clear it.
    fallback = status(Outcome(None, "Working", CONTINUING), subagent) if trigger == "denied" else NOTHING
    workstream_log.begin("manager-review", key)
    reply = brain.consult(bridge.ROOT, trigger, _context(bridge, key, event, trigger, stream, rows),
                          investigate=trigger == "stop", timeout=270 if trigger == "stop" else 150,
                          folders=[row["folder"]] if row.get("folder") else [])
    if not isinstance(reply, dict) or reply.get("verdict") not in brain.VERDICTS[trigger]:
        return fallback
    verdict, summary, message = reply["verdict"], str(reply.get("summary") or ""), str(reply.get("message") or "")
    decisions = reply.get("decisions")
    decisions = [d for d in decisions if isinstance(d, dict)] if isinstance(decisions, list) else []
    with transaction(bridge) as db:
        if store.paused(db):
            return fallback  # Paused while the consult ran.
        outcome = verdict_outcome(db, key, stream, trigger, verdict, summary, message, reply, tool, tool_input)
        if outcome is None:
            return fallback
        if trigger != "stop" or verdict != "stop" or decisions:
            store.record_event(db, stream, key, trigger, verdict, summary, {"message": message, "decisions": decisions})
    return status(fallback if outcome == NOTHING else outcome, subagent)


def remember(bridge, reply):
    items = reply.get("remember")
    if isinstance(items, list) and items:
        try:
            brain.remember(bridge.ROOT, items)
        except Exception:
            pass  # The reply is already stored; a lost note must not discard it.


def status(outcome, subagent):
    return outcome._replace(state=None, detail=None) if subagent else outcome


def verdict_outcome(db, key, stream, trigger, verdict, summary, message, reply, tool, tool_input):
    """The Outcome for a verdict, or None when the reply cannot be acted on."""
    if verdict == "ask":
        ask = reply.get("ask") if isinstance(reply.get("ask"), dict) else {}
        text = str(ask.get("summary") or "").strip() or summary.strip()
        if not text:
            return None
        options = ask.get("options")
        options = [str(o) for o in options if str(o).strip()] if isinstance(options, list) else []
        recommendation = str(ask.get("recommendation") or "")
        store.open_ask(db, stream, key, text, options, recommendation)
        if options:
            text += "\n\nOptions:\n" + "\n".join("- " + o for o in options)
        if recommendation:
            text += "\n\nRecommended: " + recommendation
        return Outcome(None, "Needs you", text)
    output = None
    if (trigger, verdict) == ("permission", "allow"):
        output = permission({"behavior": "allow"})
    elif (trigger, verdict) == ("permission", "deny"):
        if not message.strip():
            return None
        output = permission({"behavior": "deny", "message": message, "interrupt": False})
    elif (trigger, verdict) == ("denied", "approve"):
        store.approve(db, key, tool, tool_input)
        output = RETRY
    elif trigger in ("question", "plan") and not isinstance(tool_input, dict):
        return None
    elif (trigger, verdict) == ("question", "answer"):
        questions, answers = tool_input.get("questions"), reply.get("answers")
        if not (isinstance(questions, list) and questions and isinstance(answers, dict) and all(
                isinstance(q, dict) and isinstance(q.get("question"), str)
                and isinstance(answers.get(q["question"]), str) and answers[q["question"]].strip()
                for q in questions)):
            return None
        output = pre_tool("allow", updatedInput=dict(tool_input, answers=answers))
    elif (trigger, verdict) == ("plan", "approve"):
        output = pre_tool("allow", updatedInput=tool_input)
    elif (trigger, verdict) in (("plan", "revise"), ("stop", "continue")):
        if not message.strip():
            return None
        if verdict == "continue":
            store.add_nudge(db, key)
            output = {"decision": "block", "reason": message}
        else:
            output = pre_tool("deny", permissionDecisionReason=message)
    if output is None:
        return NOTHING
    return Outcome(output, "Working", summary)


def untrusted(text):
    # Agent text must not be able to close the block it sits in.
    return "<untrusted>\n" + re.sub(r"<(/?)\s*untrusted", r"‹\1untrusted", text, flags=re.I).strip("\n") \
        + "\n</untrusted>"


def context(bridge, key, event, trigger):
    stream = bridge.terminal_owner(key)
    with bridge.bridge_lock():
        rows = bridge.streams()
    return _context(bridge, key, event, trigger, stream, rows)


def _context(bridge, key, event, trigger, stream, rows):
    row = next((r for r in rows if r.get("id") == stream), {})
    tool = event.get("tool_name") or ""
    parts = [f"# Trigger: {trigger}\n" + SITUATIONS[trigger].format(tool=tool) + "\n"
             + f"You may return: {', '.join(brain.VERDICTS[trigger])}.\n" + FIELDS]
    workstream = f"# Workstream\nID: {stream}"
    if not row:
        workstream += "\nThis terminal's workstream was not found."
    lines = [f"Folder: {row.get('folder', '')}"] + _stream_lines(row)
    parts.append(workstream + "\nFolder, title, links and tasks, as written by agents:\n" + untrusted("\n".join(lines)))
    terminals = [f"- {t.get('id', '')}{' (this terminal)' if t.get('id') == key else ''} · {t.get('title', '')} · "
                 f"{t.get('provider', '')} · {t.get('state', '')}" for t in row.get("terminals") or []]
    parts.append(f"# Terminals\nThis terminal: {key}\n" + ("\n".join(terminals) or "(none)"))
    others = [f"{r.get('title', '')} · {r.get('state', '')}" for r in rows if r.get("id") != stream]
    parts.append("# Other workstreams\n" + (untrusted("; ".join(others)) if others else "(none)"))
    with transaction(bridge) as db:
        age = store.user_prompt_age(db, key)
        nudges = store.nudges(db, key)
        events = db.execute("""SELECT trigger,verdict,summary FROM manager_events WHERE stream=?
                               ORDER BY time DESC, id DESC LIMIT 5""", (stream,)).fetchall()
    boss = "No prompt from the boss recorded." if age is None else f"The boss last prompted this agent {_ago(age)}."
    parts.append("# Who prompted last\n" + boss)
    parts.append(f"# Nudges\nYou have sent this terminal back to work {nudges} time{'' if nudges == 1 else 's'} "
                 "since the boss last prompted it.")
    parts.append("# Your recent actions in this workstream\n"
                 + ("\n".join(f"- {t} → {v}: {s}" for t, v, s in events) or "(none)"))
    request = [f"{field}: {value if isinstance(value, str) else json.dumps(value)}"
               for field in ("tool_name", "tool_input", "reason", "last_assistant_message", "background_tasks",
                             "session_crons")
               for value in [event.get(field)] if value not in (None, "", {}, [])]
    tail = conversation_tail(event.get("transcript_path"))
    parts.append("# The request and the recent conversation\n" + untrusted(
        "Request:\n" + ("\n".join(request) or "(no details)") + "\n\nRecent conversation:\n" + (tail or "(unavailable)")))
    return "\n\n".join(parts) + "\n"


def _stream_lines(row):
    lines = [f"Title: {row.get('title', '')}"]
    lines += [f"Link: {link.get('kind', '')} {link.get('label', '')} {link.get('url', '')}" for link in row.get("links") or []]
    for task in row.get("tasks") or []:
        lines.append(f"{task.get('id', '')} [{task.get('status', '')}] {task.get('title', '')}")
        if task.get("status") == "blocked":
            lines.append(f"  Blocked because: {task.get('blockedReason') or 'not given'}")
            lines.append(f"  To unblock: {task.get('unblockAction') or 'not given'}")
    return lines


def _ago(age):
    minutes = int(age // 60)
    return f"{minutes} minute{'' if minutes == 1 else 's'} ago"


def overview(bridge, rows):
    with transaction(bridge) as db:
        prompted = dict(db.execute("SELECT terminal, user_prompt_time FROM manager_terminals"))
    now = time.time()
    parts = []
    for row in rows:
        if row.get("state") == "Done":
            continue
        terminals, details = [], []
        for t in row.get("terminals") or []:
            line = f"{t.get('id', '')} · {t.get('provider', '')} · {t.get('state', '')}"
            when = prompted.get(t.get("id"))
            terminals.append(f"- {line} · " + ("No prompt from the boss recorded." if when is None
                                               else f"The boss last prompted it {_ago(now - when)}."))
            details.append(f"- {line} · {str(t.get('detail') or '')[:600]}")
        lines = _stream_lines(row) + [f"Folder: {row.get('folder', '')}", "Terminals:"] + (details or ["(none)"])
        parts.append(f"## Workstream {row.get('id', '')} · {row.get('state', '')}\nTerminals:\n"
                     + ("\n".join(terminals) or "(none)")
                     + "\nTitle, folder, links, tasks and terminal details, as written by agents:\n"
                     + untrusted("\n".join(lines)))
    return "# Workstreams\n" + ("\n\n".join(parts) or "(none)") + "\n"


def _digest(value):
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


def _pairs(items, first, second):
    return sorted([str(item.get(first) or ""), str(item.get(second) or "")] for item in items or [])


def _hooks_cannot_see(terminal):
    provider, state = terminal.get("provider"), terminal.get("state")
    return provider == "codex" and state in ("Review", "Needs you") or state == "Offline" and provider != "shell"


def signature(rows):
    picked = []
    for row in rows:
        waiting = [t for t in row.get("terminals") or [] if _hooks_cannot_see(t)]
        if row.get("state") != "Done" and waiting and any(task.get("status") != "closed" for task in row.get("tasks") or []):
            picked.append([str(row.get("id") or ""), _pairs(waiting, "id", "state"), _pairs(row.get("tasks"), "id", "status")])
    return _digest(sorted(picked)) if picked else ""


def overall_signature(rows):
    return _digest(sorted([str(row.get("id") or ""), str(row.get("state") or ""), _pairs(row.get("tasks"), "id", "status")]
                          for row in rows))


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def state(bridge):
    with bridge.bridge_lock():
        rows = bridge.streams()
    due = signature(rows)
    with transaction(bridge) as db:
        result = store.state(db)
        swept, sweep_time, briefed = (store.setting(db, key) for key in
                                      ("sweep_signature", "sweep_time", "briefing_signature"))
        if not due and swept:
            # Nothing is waiting now, so the same wait coming back later is new and due again.
            store.set_setting(db, "sweep_signature", "")
    result["sweepDue"] = bool(not result["paused"] and due and due != swept
                              and time.time() - _number(sweep_time) >= SWEEP_INTERVAL)
    result["briefingStale"] = briefed != overall_signature(rows)
    return result


def clean(text):
    # Escape sequences could close the bracketed paste, and a carriage return would submit early.
    return "".join(c for c in text if c in "\n\t" or unicodedata.category(c) != "Cc")


def tell(bridge, terminal, text):
    text = clean(str(text))
    if not text.strip():
        return False  # Enter alone would submit whatever is already in the prompt.
    with bridge.bridge_lock():
        live = terminal in bridge.terminals()
    if not live:
        return False
    with transaction(bridge) as db:
        row = db.execute("""SELECT t.provider, a.state FROM workstream_terminals t JOIN activity a ON a.id=t.id
                            WHERE t.id=?""", (terminal,)).fetchone()
        if not row or row[0] not in ("claude", "codex") or row[1] not in TELLABLE:
            return False
        store.note_tell(db, terminal, text)
    path = str(bridge.TERMINALS / terminal)
    try:
        subprocess.run(["dtach", "-p", path], input=b"\x1b[200~" + text.encode() + b"\x1b[201~", check=True,
                       timeout=10)
        time.sleep(0.3)
        subprocess.run(["dtach", "-p", path], input=b"\r", check=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def restart(bridge, terminal):
    try:
        with bridge.bridge_lock():
            if terminal in bridge.terminals():
                return False
            bridge.launch(terminal)
    except ValueError:
        return False
    return True


def _snapshot(bridge):
    with bridge.bridge_lock():
        rows = bridge.streams()
    known = {t.get("id"): row.get("id") for row in rows if row.get("state") != "Done"
             for t in row.get("terminals") or [] if isinstance(t.get("id"), str)}
    folders = list(dict.fromkeys(row["folder"] for row in rows if row.get("folder")))
    return rows, known, folders


def _act(bridge, reply, known, trigger, summary, exclude=None, paused=False):
    """Applies each valid action, with no lock or transaction held, and logs the ones that took effect.
    Returns the ones that did not, as (action, terminal, reason)."""
    missed, restarted = [], set()
    for item in reply.get("actions") if isinstance(reply.get("actions"), list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("terminal"), str) \
                or item.get("action") not in ("tell", "restart"):
            continue
        terminal, action = item["terminal"], item["action"]
        if terminal not in known:
            reason = "unknown terminal"
        elif paused:
            reason = "manager is paused"
        elif action == "tell" and terminal in restarted:
            reason = "terminal is still starting"
        else:
            try:
                reason = _act_one(bridge, item, known[terminal], trigger, summary, exclude)
            except Exception as exc:  # One action that fails must not cost the others.
                workstream_log.record(f"manager action={action} failed type={type(exc).__name__}")
                reason = type(exc).__name__
        if reason:
            missed.append((action, terminal, reason))
        elif action == "restart":
            restarted.add(terminal)
    return missed


def _act_one(bridge, item, stream, trigger, summary, exclude):
    """Carries out one action; returns why it was not carried out, or None."""
    terminal, action, message = item["terminal"], item["action"], str(item.get("message") or "")
    # Sweep actions count towards the loop guard; what the boss asked for in chat does not.
    guarded = trigger == "sweep"
    if guarded:
        with transaction(bridge) as db:
            if store.nudges(db, terminal) >= store.MAX_NUDGES:
                if not db.execute("SELECT 1 FROM manager_asks WHERE terminal=? AND status='open'",
                                  (terminal,)).fetchone():
                    store.open_ask(db, stream, terminal, NUDGE_CAP, [], "")
                return "nudge limit reached"
            # Read now, not before the consult: the boss may have switched terminals while it ran.
            if action == "tell" and terminal in (exclude, store.setting(db, "focus")):
                return "terminal is on screen"
    if action == "tell" and not tell(bridge, terminal, message):
        return "terminal is busy or showing a dialog"
    if action == "restart" and not restart(bridge, terminal):
        return "could not restart"
    with transaction(bridge) as db:
        if guarded:
            store.add_nudge(db, terminal)
        store.record_event(db, stream, terminal, trigger, action, summary, {"message": message})
    return None


def not_carried_out(missed, rows):
    titles = {t.get("id"): t.get("title") for row in rows if row.get("state") != "Done"
              for t in row.get("terminals") or []}
    return "Not carried out: " + "; ".join(f"{action} {titles.get(terminal) or terminal} ({reason})"
                                           for action, terminal, reason in missed)


def sweep(bridge, exclude=None):
    rows, known, folders = _snapshot(bridge)
    due, overall = signature(rows), overall_signature(rows)
    mark_swept(bridge, due)  # Before the consult too, so no second sweep starts while this one runs.
    with transaction(bridge) as db:
        paused = store.paused(db)
    try:
        reply = brain.consult(bridge.ROOT, "sweep", SWEEP + ("\n" + SWEEP_PAUSED if paused else "") + "\n\n"
                              + overview(bridge, rows), investigate=True, timeout=270, folders=folders)
    finally:
        # A failed sweep still waits its interval, so a broken consult is not retried every few seconds.
        mark_swept(bridge, due)
    if not isinstance(reply, dict) or not isinstance(reply.get("briefing"), str) or not reply["briefing"].strip():
        return
    streams = {row.get("id"): {t.get("id") for t in row.get("terminals") or []} for row in rows
               if row.get("state") != "Done"}
    with transaction(bridge) as db:
        paused = store.paused(db)
        store.set_setting(db, "briefing", reply["briefing"])
        store.set_setting(db, "briefing_time", str(time.time()))
        store.set_setting(db, "briefing_signature", overall)
        for ask in reply.get("asks") if isinstance(reply.get("asks"), list) else []:
            if not isinstance(ask, dict) or not isinstance(ask.get("stream"), str) or ask["stream"] not in streams:
                continue
            text = str(ask.get("summary") or "").strip()
            if not text:
                continue
            terminal = ask.get("terminal") if isinstance(ask.get("terminal"), str) \
                and ask["terminal"] in streams[ask["stream"]] else ""
            options = ask.get("options")
            options = [str(o) for o in options if str(o).strip()] if isinstance(options, list) else []
            store.open_ask(db, ask["stream"], terminal, text, options, str(ask.get("recommendation") or ""))
    missed = _act(bridge, reply, known, "sweep", str(reply.get("summary") or ""), exclude, paused)
    if missed:
        with transaction(bridge) as db:
            store.set_setting(db, "briefing", reply["briefing"].rstrip() + "\n\n" + not_carried_out(missed, rows))


def mark_swept(bridge, due):
    with transaction(bridge) as db:
        store.set_setting(db, "sweep_signature", due)
        store.set_setting(db, "sweep_time", str(time.time()))


def chat(bridge, text):
    with transaction(bridge) as db:
        store.add_chat(db, "user", text)
        session = store.setting(db, "chat_session")
    answer = None
    try:
        rows, known, folders = _snapshot(bridge)
        context = CHAT + "\n\n" + overview(bridge, rows) + "\n# The boss says\n" + text + "\n"

        def ask(session, resume):
            return brain.consult(bridge.ROOT, "chat", context, investigate=True, timeout=270, session=session,
                                 resume=resume, folders=folders)
        reply = ask(session, True) if session else None
        if reply is None:
            # A session that cannot be resumed is replaced; a fresh one that fails is not retried.
            session = str(uuid.uuid4())
            reply = ask(session, False)
        if isinstance(reply, dict) and isinstance(reply.get("reply"), str) and reply["reply"].strip():
            answer = reply["reply"]
    except Exception:
        pass
    with transaction(bridge) as db:
        if answer is None:
            store.add_chat(db, "error", UNREACHABLE)
            return
        store.set_setting(db, "chat_session", session)
        message = store.add_chat(db, "manager", answer)
    missed = _act(bridge, reply, known, "chat", CHAT_ACTION)
    if missed:
        with transaction(bridge) as db:
            db.execute("UPDATE manager_chat SET text=? WHERE id=?",
                       (answer.rstrip() + "\n\n" + not_carried_out(missed, rows), message))
    remember(bridge, reply)


def conversation_tail(path, limit=12000):
    if not path:
        return ""
    try:
        with open(path, "rb") as f:
            size = f.seek(0, 2)
            f.seek(max(0, size - TAIL_BYTES))
            # Split on newlines only: transcripts can hold U+2028 and other characters splitlines() breaks on.
            lines = f.read().decode("utf-8", errors="replace").split("\n")
    except OSError:
        return ""
    if size > TAIL_BYTES:
        lines = lines[1:]  # The first line was cut by the seek.
    entries = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("isMeta") or row.get("type") not in ("user", "assistant"):
            continue
        speaker = "User" if row["type"] == "user" else "Agent"
        content = (row.get("message") or {}).get("content") if isinstance(row.get("message"), dict) else None
        if isinstance(content, str):
            entries.append(f"{speaker}: {content}")
            continue
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                entries.append(f"{speaker}: {block['text']}")
            elif block.get("type") == "tool_use":
                entries.append(f"Tool: {block.get('name', '')} {json.dumps(block.get('input'))[:300]}")
    return "\n".join(entries)[-limit:]
