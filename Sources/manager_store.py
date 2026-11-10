"""SQLite storage and guards for the workstream manager."""
import hashlib
import json
import time
from typing import Optional

APPROVAL_TTL = 600
CONSULTS_PER_HOUR = 30
TELL_WINDOW = 120
MAX_NUDGES = 3


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS manager_events (id INTEGER PRIMARY KEY AUTOINCREMENT, time REAL,
               stream TEXT, terminal TEXT, trigger TEXT, verdict TEXT, summary TEXT, detail TEXT, seen INTEGER DEFAULT 0)""")
    db.execute("""CREATE TABLE IF NOT EXISTS manager_asks (id INTEGER PRIMARY KEY AUTOINCREMENT, time REAL,
               stream TEXT, terminal TEXT, summary TEXT, options TEXT, recommendation TEXT, status TEXT DEFAULT 'open')""")
    db.execute("""CREATE TABLE IF NOT EXISTS manager_approvals
               (terminal TEXT, fingerprint TEXT, expires REAL, PRIMARY KEY(terminal, fingerprint))""")
    db.execute("CREATE TABLE IF NOT EXISTS manager_consults (time REAL, terminal TEXT)")
    db.execute("""CREATE TABLE IF NOT EXISTS manager_terminals (terminal TEXT PRIMARY KEY, nudges INTEGER DEFAULT 0,
               user_prompt_time REAL, tell_text TEXT, tell_time REAL)""")
    db.execute("CREATE TABLE IF NOT EXISTS manager_chat (id INTEGER PRIMARY KEY AUTOINCREMENT, time REAL, role TEXT, text TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS manager_settings (key TEXT PRIMARY KEY, value TEXT)")
    db.execute("""CREATE TABLE IF NOT EXISTS manager_messages (id INTEGER PRIMARY KEY AUTOINCREMENT,
        stream TEXT, kind TEXT, time REAL, revision INTEGER DEFAULT 1, hidden INTEGER DEFAULT 0,
        seen INTEGER DEFAULT 0, body TEXT, UNIQUE(stream,kind))""")
    db.execute("""CREATE TABLE IF NOT EXISTS manager_prompts (id INTEGER PRIMARY KEY AUTOINCREMENT,
        project TEXT, terminal TEXT, time REAL, text TEXT)""")
    db.execute("CREATE TABLE IF NOT EXISTS manager_ask_reads (id INTEGER PRIMARY KEY)")
    db.execute("""CREATE TABLE IF NOT EXISTS manager_tools
        (terminal TEXT PRIMARY KEY, session TEXT, phrase TEXT, started REAL)""")



def clock(now):
    return time.time() if now is None else now


def setting(db, key, default=None) -> Optional[str]:
    row = db.execute("SELECT value FROM manager_settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(db, key, value):
    db.execute("INSERT OR REPLACE INTO manager_settings VALUES (?,?)", (key, value))


def paused(db):
    # A fresh manager does nothing until the boss switches it on once.
    return setting(db, "paused") != "0"


def record_event(db, stream, terminal, trigger, verdict, summary, detail=None, now=None):
    return db.execute("""INSERT INTO manager_events (time,stream,terminal,trigger,verdict,summary,detail)
        VALUES (?,?,?,?,?,?,?)""", (clock(now), stream, terminal, trigger, verdict, summary,
                                     json.dumps(detail or {}))).lastrowid


def open_ask(db, stream, terminal, summary, options, recommendation, now=None):
    if terminal:
        resolve_asks(db, terminal=terminal)
    return db.execute("""INSERT INTO manager_asks (time,stream,terminal,summary,options,recommendation)
        VALUES (?,?,?,?,?,?)""", (clock(now), stream, terminal, summary, json.dumps(list(options or [])),
                                 recommendation)).lastrowid


def resolve_asks(db, terminal=None, ask_id=None):
    query, args = "UPDATE manager_asks SET status='resolved' WHERE status='open'", []
    if terminal is not None:
        query, args = query + " AND terminal=?", args + [terminal]
    if ask_id is not None:
        query, args = query + " AND id=?", args + [ask_id]
        # Dismissing an ask answers it, so the loop guard starts again for its terminal.
        row = db.execute("SELECT terminal FROM manager_asks WHERE id=? AND status='open'", (ask_id,)).fetchone()
        if row and row[0]:
            db.execute("UPDATE manager_terminals SET nudges=0 WHERE terminal=?", (row[0],))
    db.execute(query, args)


def fingerprint(tool_name, tool_input):
    if tool_name == "Bash" and isinstance(tool_input, dict):
        # The model rewords the description when it retries the same command.
        tool_input = {k: v for k, v in tool_input.items() if k != "description"}
    return hashlib.sha256(json.dumps([tool_name, tool_input], sort_keys=True).encode()).hexdigest()


def approve(db, terminal, tool_name, tool_input, now=None):
    db.execute("INSERT OR REPLACE INTO manager_approvals VALUES (?,?,?)",
               (terminal, fingerprint(tool_name, tool_input), clock(now) + APPROVAL_TTL))


def approved(db, terminal, tool_name, tool_input, now=None):
    return db.execute("SELECT 1 FROM manager_approvals WHERE terminal=? AND fingerprint=? AND expires>?",
                      (terminal, fingerprint(tool_name, tool_input), clock(now))).fetchone() is not None


def revoke(db, terminal, tool_name, tool_input):
    return db.execute("DELETE FROM manager_approvals WHERE terminal=? AND fingerprint=?",
                      (terminal, fingerprint(tool_name, tool_input))).rowcount > 0


def allow_consult(db, terminal, now=None):
    now = clock(now)
    db.execute("DELETE FROM manager_consults WHERE time<=?", (now - 3600,))
    count, = db.execute("SELECT count(*) FROM manager_consults WHERE terminal=?", (terminal,)).fetchone()
    if count >= CONSULTS_PER_HOUR:
        return False
    db.execute("INSERT INTO manager_consults VALUES (?,?)", (now, terminal))
    return True


def terminal_row(db, terminal):
    db.execute("INSERT OR IGNORE INTO manager_terminals (terminal) VALUES (?)", (terminal,))


def note_tell(db, terminal, text, now=None):
    terminal_row(db, terminal)
    db.execute("UPDATE manager_terminals SET tell_text=?, tell_time=? WHERE terminal=?",
               (text.strip(), clock(now), terminal))


def note_user_prompt(db, terminal, prompt, now=None):
    now = clock(now)
    row = db.execute("SELECT tell_text, tell_time FROM manager_terminals WHERE terminal=?", (terminal,)).fetchone()
    if row and row[0] is not None and row[1] is not None and (prompt or "").strip() == row[0] \
            and now - row[1] < TELL_WINDOW:
        return False
    terminal_row(db, terminal)
    db.execute("UPDATE manager_terminals SET user_prompt_time=?, nudges=0 WHERE terminal=?", (now, terminal))
    resolve_asks(db, terminal=terminal)
    return True


def nudges(db, terminal):
    row = db.execute("SELECT nudges FROM manager_terminals WHERE terminal=?", (terminal,)).fetchone()
    return row[0] if row else 0


def add_nudge(db, terminal):
    terminal_row(db, terminal)
    db.execute("UPDATE manager_terminals SET nudges=nudges+1 WHERE terminal=?", (terminal,))
    return nudges(db, terminal)


def user_prompt_age(db, terminal, now=None) -> Optional[float]:
    row = db.execute("SELECT user_prompt_time FROM manager_terminals WHERE terminal=?", (terminal,)).fetchone()
    return None if not row or row[0] is None else clock(now) - row[0]


def mark_seen(db):
    db.execute("UPDATE manager_events SET seen=1 WHERE seen=0")


def add_chat(db, role, text, now=None):
    return db.execute("INSERT INTO manager_chat (time,role,text) VALUES (?,?,?)", (clock(now), role, text)).lastrowid


def loads(text, default):
    try:
        value = json.loads(text or "")
    except ValueError:
        return default
    return value if isinstance(value, type(default)) else default


def text(value):
    return "" if value is None else str(value)


def state(db, limit=200):
    # The app decodes every string field as non-optional, so None and stray JSON types become "" or [].
    asks = [dict(id=i, time=t, stream=text(s), terminal=text(term), summary=text(summary),
                 options=[text(o) for o in loads(options, [])], recommendation=text(recommendation))
            for i, t, s, term, summary, options, recommendation in db.execute(
                """SELECT id,time,stream,terminal,summary,options,recommendation FROM manager_asks
                   WHERE status='open' ORDER BY time, id""")]
    events = []
    for i, t, s, term, trigger, verdict, summary, detail, seen in db.execute(
            """SELECT id,time,stream,terminal,trigger,verdict,summary,detail,seen FROM manager_events
               ORDER BY time DESC, id DESC LIMIT ?""", (limit,)):
        detail = loads(detail, {})
        decisions = detail.get("decisions")
        decisions = [dict(decision=text(d.get("decision")), why=text(d.get("why")), undo=text(d.get("undo")))
                     for d in (decisions if isinstance(decisions, list) else []) if isinstance(d, dict)]
        events.append(dict(id=i, time=t, stream=text(s), terminal=text(term), trigger=text(trigger),
                           verdict=text(verdict), summary=text(summary), seen=bool(seen),
                           message=text(detail.get("message")), decisions=decisions))
    chat = [dict(id=i, time=t, role=text(role), text=text(body)) for i, t, role, body in db.execute(
        "SELECT * FROM (SELECT id,time,role,text FROM manager_chat ORDER BY time DESC, id DESC LIMIT ?) ORDER BY time, id",
        (limit,))]
    briefing_time = setting(db, "briefing_time")
    try:
        briefing_time = float(briefing_time) if briefing_time is not None else None
    except ValueError:
        briefing_time = None
    unseen, = db.execute("SELECT count(*) FROM manager_events WHERE seen=0").fetchone()
    return dict(paused=paused(db), briefing=setting(db, "briefing") or "", briefingTime=briefing_time,
                asks=asks, events=events, chat=chat, unseen=unseen, messages=messages(db),
                unread=sum(1 for m in messages(db) if not m['seen'] and m['kind'] != 'status') +
                db.execute("SELECT count(*) FROM manager_asks WHERE status='open' AND id NOT IN "
                           "(SELECT id FROM manager_ask_reads)").fetchone()[0])


def put_message(db, item, now=None):
    """Later survives rewording. Only an explicit new subject can reopen it."""
    old = db.execute("SELECT id,revision,hidden,body FROM manager_messages WHERE stream=? AND kind=?",
                     (item['stream'], item['kind'])).fetchone()
    body = json.dumps(item, sort_keys=True)
    if old and old[3] == body and (not old[2] or not item.get('newSubject', False)):
        return old[0]
    if old and old[2] and not item.get('newSubject', False):
        return old[0]
    if old:
        db.execute("UPDATE manager_messages SET time=?,revision=revision+1,hidden=0,seen=0,body=? WHERE id=?",
                   (clock(now), body, old[0]))
        return old[0]
    return db.execute("INSERT INTO manager_messages (stream,kind,time,body) VALUES (?,?,?,?)",
                      (item['stream'], item['kind'], clock(now), body)).lastrowid


def messages(db, hidden=False):
    return [dict(loads(body, {}), id=i, revision=r, time=t, seen=bool(seen))
            for i, r, t, seen, body in db.execute(
                'SELECT id,revision,time,seen,body FROM manager_messages WHERE hidden=? ORDER BY time DESC,id DESC',
                (int(hidden),))]


def record_prompt(db, project, terminal, prompt, now=None):
    if not project or not isinstance(prompt, str) or not prompt.strip():
        return
    db.execute('INSERT INTO manager_prompts (project,terminal,time,text) VALUES (?,?,?,?)',
               (project, terminal, clock(now), prompt[:500]))
    db.execute('DELETE FROM manager_prompts WHERE project=? AND id NOT IN '
               '(SELECT id FROM manager_prompts WHERE project=? ORDER BY id DESC LIMIT 30)', (project, project))


def project_key(row):
    from pathlib import Path
    path = row.get('repository') or row.get('sourceFolder') or row.get('folder')
    return str(Path(path).resolve()) if path else ''


def rules_path(root, project):
    from pathlib import Path
    import re
    name = re.sub(r'[^a-zA-Z0-9_.-]', '-', Path(project).name) or 'project'
    return Path(root) / 'manager' / 'projects' / (name + '-' + hashlib.sha256(project.encode()).hexdigest()[:12] + '.md')


def project_rules(root, project):
    if not project:
        return ''
    try:
        return rules_path(root, project).read_text(encoding='utf-8')[:8000]
    except OSError:
        return ''


def save_rule(root, project, rule):
    path = rules_path(root, project)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as file:
        file.write('\n## Standing rule\n\n' + rule.strip() + '\n')


def tool_phrase(event):
    from pathlib import Path
    tool, data = event.get('tool_name', ''), event.get('tool_input') or {}
    if not isinstance(data, dict):
        data = {}
    name = Path(str(data.get('file_path') or data.get('path') or '')).name
    phrase = {'Read': 'Reading ' + (name or 'files'), 'Edit': 'Editing ' + (name or 'files'),
              'Write': 'Writing ' + (name or 'files'), 'Grep': 'Searching code', 'Glob': 'Finding files',
              'WebSearch': 'Searching the web', 'WebFetch': 'Reading a web page',
              'Agent': 'Delegating: ' + str(data.get('description') or 'a task')}.get(tool, 'Using ' + tool)
    if tool == 'Bash':
        phrase = str(data.get('description') or 'Running a command')
    if event.get('agent_id'):
        phrase = 'subagent: ' + phrase
    return ' '.join(phrase.split())[:100]
