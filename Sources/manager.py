"""The workstream manager: reviews what Claude agents stop on and turns its verdicts into hook output."""
import collections
import contextlib
import json
import re

import manager_brain as brain
import manager_store as store

Outcome = collections.namedtuple("Outcome", "output state detail")
NOTHING = Outcome(None, None, None)
REVIEWED_TOOLS = {"AskUserQuestion": "question", "ExitPlanMode": "plan"}
TRIGGERS = {"PermissionRequest": "permission", "PermissionDenied": "denied", "Stop": "stop"}
APPROVED = "Approved by the workstream manager."
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


def review(bridge, key, sid, event):
    name = event.get("hook_event_name")
    if name == "UserPromptSubmit":
        with transaction(bridge) as db:
            store.note_user_prompt(db, key, event.get("prompt", ""))
        return NOTHING
    tool, tool_input = event.get("tool_name") or "", event.get("tool_input") or {}
    subagent = bool(event.get("agent_id"))
    trigger = REVIEWED_TOOLS.get(tool) if name == "PreToolUse" else TRIGGERS.get(name)
    if name == "PreToolUse" and trigger is None:
        with transaction(bridge) as db:
            allowed = not store.paused(db) and store.approved(db, key, tool, tool_input)
        return Outcome(pre_tool("allow", permissionDecisionReason=APPROVED), None, None) if allowed else NOTHING
    # Claude Code reports subagent turns as SubagentStop; a subagent Stop is never reviewed.
    if trigger is None or subagent and trigger == "stop":
        return NOTHING
    stream = bridge.terminal_owner(key)
    with transaction(bridge) as db:
        if store.paused(db):
            return NOTHING
        if trigger == "permission" and store.approved(db, key, tool, tool_input):
            return status(Outcome(permission({"behavior": "allow"}), "Working", APPROVED), subagent)
        if trigger == "stop" and store.nudges(db, key) >= store.MAX_NUDGES:
            store.open_ask(db, stream, key, STUCK, [], "")
            return Outcome(None, "Needs you", STUCK)
        if not store.allow_consult(db, key):
            return NOTHING
        if not subagent:
            db.execute("UPDATE activity SET state=?,detail=? WHERE id=? AND session=?",
                       ("Working", REVIEWING[trigger].format(tool=tool), key, sid))
    rows = bridge.streams()
    row = next((r for r in rows if r.get("id") == stream), {})
    reply = brain.consult(bridge.ROOT, trigger, _context(bridge, key, event, trigger, stream, rows),
                          investigate=trigger == "stop", timeout=270 if trigger == "stop" else 150,
                          folders=[row["folder"]] if row.get("folder") else [])
    if not isinstance(reply, dict) or reply.get("verdict") not in brain.VERDICTS[trigger]:
        return NOTHING
    verdict, summary, message = reply["verdict"], str(reply.get("summary") or ""), str(reply.get("message") or "")
    decisions = reply.get("decisions")
    decisions = [d for d in decisions if isinstance(d, dict)] if isinstance(decisions, list) else []
    with transaction(bridge) as db:
        outcome = verdict_outcome(db, key, stream, trigger, verdict, summary, message, reply, tool, tool_input)
        if outcome is None:
            return NOTHING
        if trigger != "stop" or verdict != "stop" or decisions:
            store.record_event(db, stream, key, trigger, verdict, summary, {"message": message, "decisions": decisions})
    remember = reply.get("remember")
    if isinstance(remember, list) and remember:
        brain.remember(bridge.ROOT, remember)
    return status(outcome, subagent)


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
        output = permission({"behavior": "deny", "message": message, "interrupt": False})
    elif (trigger, verdict) == ("denied", "approve"):
        store.approve(db, key, tool, tool_input)
        output = {"hookSpecificOutput": {"hookEventName": "PermissionDenied", "retry": True}}
    elif (trigger, verdict) == ("question", "answer"):
        questions, answers = tool_input.get("questions"), reply.get("answers")
        if not (isinstance(questions, list) and questions and isinstance(answers, dict) and all(
                isinstance(q, dict) and isinstance(answers.get(q.get("question")), str)
                and answers[q["question"]].strip() for q in questions)):
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
    return _context(bridge, key, event, trigger, bridge.terminal_owner(key), bridge.streams())


def _context(bridge, key, event, trigger, stream, rows):
    row = next((r for r in rows if r.get("id") == stream), {})
    tool = event.get("tool_name") or ""
    parts = [f"# Trigger: {trigger}\n" + SITUATIONS[trigger].format(tool=tool) + "\n"
             + f"You may return: {', '.join(brain.VERDICTS[trigger])}.\n" + FIELDS]
    workstream = f"# Workstream\nTitle: {row.get('title', '')}\nID: {stream}\nFolder: {row.get('folder', '')}"
    if not row:
        workstream += "\nThis terminal's workstream was not found."
    lines = [f"Link: {link.get('kind', '')} {link.get('label', '')} {link.get('url', '')}" for link in row.get("links") or []]
    for task in row.get("tasks") or []:
        lines.append(f"{task.get('id', '')} [{task.get('status', '')}] {task.get('title', '')}")
        if task.get("status") == "blocked":
            lines.append(f"  Blocked because: {task.get('blockedReason') or 'not given'}")
            lines.append(f"  To unblock: {task.get('unblockAction') or 'not given'}")
    parts.append(workstream + "\nLinks and tasks, as written by agents:\n" + untrusted("\n".join(lines) or "(none)"))
    terminals = [f"- {t.get('id', '')}{' (this terminal)' if t.get('id') == key else ''} · {t.get('title', '')} · "
                 f"{t.get('provider', '')} · {t.get('state', '')}" for t in row.get("terminals") or []]
    parts.append(f"# Terminals\nThis terminal: {key}\n" + ("\n".join(terminals) or "(none)"))
    others = [f"{r.get('title', '')} · {r.get('state', '')}" for r in rows if r.get("id") != stream]
    parts.append("# Other workstreams\n" + ("; ".join(others) or "(none)"))
    with transaction(bridge) as db:
        age = store.user_prompt_age(db, key)
        nudges = store.nudges(db, key)
        events = db.execute("""SELECT trigger,verdict,summary FROM manager_events WHERE stream=?
                               ORDER BY time DESC, id DESC LIMIT 5""", (stream,)).fetchall()
    boss = "No prompt from the boss recorded." if age is None else \
        f"The boss last prompted this agent {int(age // 60)} minutes ago."
    parts.append("# Who prompted last\n" + boss)
    parts.append(f"# Nudges\nYou have sent this terminal back to work {nudges} time{'' if nudges == 1 else 's'} "
                 "since the boss last prompted it.")
    parts.append("# Your recent actions in this workstream\n"
                 + ("\n".join(f"- {t} → {v}: {s}" for t, v, s in events) or "(none)"))
    request = [f"{field}: {value if isinstance(value, str) else json.dumps(value)}"
               for field in ("tool_name", "tool_input", "reason", "last_assistant_message")
               for value in [event.get(field)] if value not in (None, "", {})]
    tail = conversation_tail(event.get("transcript_path"))
    parts.append("# The request and the recent conversation\n" + untrusted(
        "Request:\n" + ("\n".join(request) or "(no details)") + "\n\nRecent conversation:\n" + (tail or "(unavailable)")))
    return "\n\n".join(parts) + "\n"


def conversation_tail(path, limit=12000):
    if not path:
        return ""
    entries = []
    try:
        with open(path, errors="replace") as f:
            for line in f:
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
    except OSError:
        return ""
    return "\n".join(entries)[-limit:]
