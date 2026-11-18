"""Linear's issue, comment-thread, and review-attachment API for the desktop bridge."""
import difflib
import hashlib
import json
import re
import shlex
import sys
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener, Request

import manager_store

ISSUE = re.compile(r"[a-z][a-z0-9]*-[1-9]\d*", re.I)
ISSUE_PATH = re.compile(r"/(?:([\w-]+)/)?issue/([a-z][a-z0-9]*-[1-9]\d*)(?:/[^/]*)?/?", re.I)
ISSUE_QUERY = """query($id: String!) { issue(id: $id) {
  id identifier url title description state { name } assignee { name }
  comments(first: 12, orderBy: createdAt) { nodes { body user { name } } }
} }"""
COMMENT = """query($id: String!) { comment(id: $id) { id url issue { id }
  children(first: 1, orderBy: createdAt, filter: {user: {app: {eq: false}}}) { nodes { body user { name } } }
} }"""


def reference(value):
    value = value.strip()
    if ISSUE.fullmatch(value):
        return "", value.upper()
    try:
        url = urlsplit(value)
    except ValueError:
        return None
    match = ISSUE_PATH.fullmatch(url.path)
    if url.scheme == "https" and url.netloc.lower() == "linear.app" and match:
        return (match[1] or "").lower(), match[2].upper()
    return None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class Client:
    def __init__(self, key):
        self.key = key
        self.http = build_opener(NoRedirect())

    def __call__(self, query, **variables):
        body = json.dumps(dict(query=query, variables=variables)).encode()
        request = Request("https://api.linear.app/graphql", data=body,
                          headers={"Authorization": self.key, "Content-Type": "application/json"})
        try:
            with self.http.open(request, timeout=12) as response:
                data = json.load(response)
        except HTTPError as error:
            if error.code in (401, 403):
                raise ValueError("Linear denied access. Check the API key and its team permissions in Settings.") from error
            if error.code == 429:
                raise ValueError("Linear's request limit was reached. Try again later.") from error
            raise ValueError(f"Linear returned HTTP {error.code}. Retry the request.") from error
        except (OSError, URLError):
            raise ValueError("Could not reach Linear. Check your connection and retry.")
        if data.get("errors"):
            raise ValueError(data["errors"][0].get("message") or "Linear rejected the request.")
        return data["data"]


def fetch(api, identity, workspace=""):
    raw = api(ISSUE_QUERY, id=identity)["issue"]
    if not raw:
        raise ValueError("Linear could not find this issue.")
    canonical = reference(raw["url"])
    if not canonical or (workspace and workspace != canonical[0]):
        raise ValueError("This issue belongs to a different Linear workspace.")
    return dict(id=raw["id"], identifier=raw["identifier"], url=raw["url"], title=raw["title"],
                status=raw["state"]["name"], assignee=(raw["assignee"] or {}).get("name") or "Unassigned",
                description=raw["description"] or "", discussion=[
                    f"{(c['user'] or {}).get('name') or 'Linear'}: {c['body'][:2500]}"
                    for c in reversed(raw["comments"]["nodes"])])


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS linear_primary
               (stream TEXT PRIMARY KEY, current TEXT NOT NULL, baseline TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS linear_stopped_questions
               (ask INTEGER PRIMARY KEY, session TEXT, detail TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS linear_handoffs
               (stream TEXT PRIMARY KEY, anchor TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS linear_questions
               (ask INTEGER PRIMARY KEY, comment TEXT NOT NULL, url TEXT, reply TEXT,
                received REAL, session TEXT, pending INTEGER NOT NULL DEFAULT 0)""")


def save(db, stream, issue):
    snapshot = json.dumps(issue)
    db.execute("""INSERT INTO linear_primary VALUES (?,?,?) ON CONFLICT(stream)
               DO UPDATE SET current=excluded.current""", (stream, snapshot, snapshot))


def changes(previous, current):
    result = []
    for key in ("title", "status", "assignee"):
        if previous[key] != current[key]:
            result.append(f"{key.capitalize()}: {previous[key]} → {current[key]}")
    if previous["description"] != current["description"]:
        lines = difflib.unified_diff(previous["description"].splitlines(), current["description"].splitlines(), n=0)
        result.append("Description\n" + "\n".join(line for line in list(lines)[2:] if not line.startswith("@@"))[:3200])
    if previous["discussion"] != current["discussion"]:
        added = [comment for comment in current["discussion"] if comment not in previous["discussion"]]
        result.append("Discussion updated" + ("\n" + "\n".join(added)[:2000] if added else ""))
    return result


def cards(db, requested=None):
    query = "SELECT stream,current,baseline FROM linear_primary"
    rows = db.execute(query) if requested is None else db.execute(query + " WHERE stream=?", (requested,))
    result = {}
    for stream, current, baseline in rows:
        issue = json.loads(current)
        issue["changes"] = changes(json.loads(baseline), issue)
        issue["revision"] = hashlib.sha256(current.encode()).hexdigest()
        if requested is None:
            # The dashboard refreshes all streams frequently. Only the one-agent
            # context request needs the full recent discussion.
            del issue["discussion"]
        result[stream] = issue
    return result


def match(streams, issue):
    workspace, identifier = reference(issue["url"])
    for row in sorted(streams, key=lambda row: row["status"] == "closed"):
        metadata = row.get("metadata") or {}
        if row.get("parent") or not metadata.get("folder"):
            continue
        existing = metadata.get("linearIssueID")
        ref = reference(row.get("external_ref") or "")
        if (existing == issue["id"] or
                (not existing and ref and ref[1] == identifier and ref[0] in ("", workspace))):
            return row
    return None


def initial_prompt(db, stream):
    row = db.execute("SELECT current FROM linear_primary WHERE stream=?", (stream,)).fetchone()
    if row:
        issue = json.loads(row[0])
        return (f"Work on {issue['identifier']} — {issue['title']}.\n{issue['url']}\n\n"
                f"Issue description:\n{issue['description'][:32000]}\n\nRecent discussion:\n" +
                "\n".join(issue["discussion"]))


def question_state(db):
    rows = db.execute("""SELECT a.stream,a.id,q.url,q.reply,a.summary,q.pending,q.comment FROM linear_questions q
                         JOIN manager_asks a ON a.id=q.ask WHERE q.url IS NOT NULL
                         AND (a.status='open' OR q.reply IS NOT NULL) ORDER BY a.id DESC""")
    result = {}
    for stream, ask, url, reply, summary, pending, comment in rows:
        result.setdefault(stream, []).append(dict(ask=ask, comment=comment, url=url, reply=reply, summary=summary, pending=bool(pending)))
    return result


def ask_linear(db, api, stream, ask):
    row = db.execute("""SELECT a.summary,a.options,a.recommendation,p.current FROM manager_asks a
                        JOIN linear_primary p ON p.stream=a.stream
                        WHERE a.id=? AND a.stream=? AND a.status='open'""", (ask, stream)).fetchone()
    if not row:
        raise ValueError("This question is closed or the workstream has no primary issue.")
    summary, options, recommendation, current = row
    issue_id = json.loads(current)["id"]
    db.execute("INSERT OR IGNORE INTO linear_questions(ask,comment) VALUES (?,?)", (ask, str(uuid.uuid4())))
    comment, url = db.execute("SELECT comment,url FROM linear_questions WHERE ask=?", (ask,)).fetchone()
    db.commit()
    if url:
        return
    body = "\n\n".join(filter(None, [summary, "\n".join("- " + opt for opt in json.loads(options)),
                                     "Recommendation: " + recommendation if recommendation else "",
                                     "Reply in this thread to send your answer to Meridian."]))
    try:
        data = api("""mutation($input: CommentCreateInput!) {
            commentCreate(input:$input) { success comment { url } } }""",
                   input=dict(id=comment, issueId=issue_id, body=body))["commentCreate"]
        if not data["success"]:
            raise ValueError("Linear could not post the question.")
        url = data["comment"]["url"]
    except ValueError as original:
        # The mutation may have committed before its response was lost.
        try:
            existing = api(COMMENT, id=comment)["comment"]
            if not existing or existing["issue"]["id"] != issue_id:
                raise ValueError()
            url = existing["url"]
        except ValueError:
            raise original
    db.execute("UPDATE linear_questions SET url=? WHERE ask=?", (url, ask))


def remember_stopped_question(db, terminal, session, detail):
    # A Stop hook leaves an idle prompt, whereas a permission or tool hook can
    # leave a native dialog. Both are displayed as "Needs you" in the dashboard.
    db.execute("""INSERT OR REPLACE INTO linear_stopped_questions
                  SELECT id,?,? FROM manager_asks WHERE terminal=? AND status='open'
                  ORDER BY id DESC LIMIT 1""", (session, detail, terminal))


def poll_reply(db, api, stream, ask):
    row = db.execute("""SELECT q.comment FROM linear_questions q JOIN manager_asks a ON a.id=q.ask
                        WHERE q.ask=? AND a.stream=? AND a.status='open' AND q.reply IS NULL
                        AND q.url IS NOT NULL""", (ask, stream)).fetchone()
    if not row:
        return
    nodes = api(COMMENT, id=row[0])["comment"]["children"]["nodes"]
    if not nodes or not nodes[0].get("user"):
        return
    answer = nodes[0]
    reply = f"{answer['user']['name']}: {answer['body']}"
    # Recheck after the HTTP request. A local dismissal or another poll may have won.
    updated = db.execute("""UPDATE linear_questions SET reply=? WHERE ask=? AND reply IS NULL
                            AND EXISTS(SELECT 1 FROM manager_asks WHERE id=? AND status='open')""",
                         (reply, ask, ask)).rowcount
    if updated:
        terminal, summary = db.execute("SELECT terminal,summary FROM manager_asks WHERE id=?", (ask,)).fetchone()
        manager_store.record_event(db, stream, terminal, "linear", "reply", summary, {"message": reply})
        # The reply is available to the agent and inspector, including after a restart.
        manager_store.resolve_asks(db, ask_id=int(ask))
        db.execute("""UPDATE activity SET state='Review',detail='Linear reply received.'
                      WHERE id=? AND state='Needs you' AND EXISTS (
                      SELECT 1 FROM linear_stopped_questions WHERE ask=?
                      AND session=activity.session AND detail=activity.detail)""", (terminal, ask))
        session = db.execute("SELECT session FROM activity WHERE id=?", (terminal,)).fetchone()
        if session and session[0]:
            db.execute("UPDATE linear_questions SET received=?,session=?,pending=1 WHERE ask=?",
                       (time.time(), session[0], ask))


def deliver(bridge, db, stream, ask):
    # Only wake the same conversation. Replies stay readable in the workstream
    # when the agent has moved on, the user is typing, or a native dialog is up.
    row = db.execute("""SELECT a.terminal,q.session,x.session,x.state,q.received,t.user_prompt_time
        FROM linear_questions q JOIN manager_asks a ON a.id=q.ask
        LEFT JOIN activity x ON x.id=a.terminal LEFT JOIN manager_terminals t ON t.terminal=a.terminal
        WHERE q.ask=? AND a.stream=? AND q.pending=1""", (ask, stream)).fetchone()
    if not row:
        return
    terminal, expected, session, state, received, prompted = row
    if session != expected or (prompted and prompted > received):
        db.execute("UPDATE linear_questions SET pending=0 WHERE ask=?", (ask,))
        return
    if state not in bridge.manager.TELLABLE or manager_store.paused(db) or terminal == manager_store.setting(db, "focus"):
        return
    if not db.execute("UPDATE linear_questions SET pending=0 WHERE ask=? AND pending=1", (ask,)).rowcount:
        return
    db.commit()  # Claim before sending so overlapping refreshes cannot send twice.
    read = shlex.join([sys.executable, bridge.__file__, "linear-context", stream])
    if not bridge.manager.tell(bridge, terminal, "A teammate replied to your Manager question on Linear. "
                               "Read the reply as issue context using " + read):
        db.execute("UPDATE linear_questions SET pending=1 WHERE ask=?", (ask,))


def share(db, api, stream, pr, verified, remaining):
    row = db.execute("""SELECT p.current FROM linear_primary p WHERE p.stream=? AND EXISTS(
        SELECT 1 FROM links l LEFT JOIN workstream_terminals t ON t.id=l.stream
        WHERE COALESCE(t.stream,l.stream)=p.stream AND l.kind='pr' AND l.url=?)""", (stream, pr)).fetchone()
    if not row:
        raise ValueError("Choose a pull request linked to this workstream.")
    if not verified.strip() or not remaining.strip():
        raise ValueError("Describe verification and any remaining work.")
    issue = json.loads(row[0])
    # Keep one attachment across revisions, including when a follow-up PR takes over.
    # The anchor opens the original review; its preview links directly to the current PR.
    db.execute("INSERT OR IGNORE INTO linear_handoffs VALUES (?,?)", (stream, pr + "/files#meridian-review-" + stream))
    url, = db.execute("SELECT anchor FROM linear_handoffs WHERE stream=?", (stream,)).fetchone()
    db.commit()
    data = api("""mutation($input: AttachmentCreateInput!) {
        attachmentCreate(input:$input) { success } }""",
               input=dict(issueId=issue["id"], url=url, title=f"Meridian review · {issue['identifier']}",
                          subtitle=(verified + " · " + remaining)[:2048],
                          metadata=dict(messages=[dict(subject="Current pull request", body=f"[Open pull request]({pr})")],
                                        attributes=[dict(name="Pull request", value=pr),
                                                    dict(name="Verification", value=verified),
                                                    dict(name="Remaining work", value=remaining)])))
    if not data["attachmentCreate"]["success"]:
        raise ValueError("Linear could not save the review handoff.")


def command(bridge, action, args, api):
    if action in ("linear-preview", "linear-start"):
        ref = reference(args[0])
        if not ref:
            raise ValueError("Enter a Linear issue identifier or URL.")
        issue = fetch(api, ref[1], ref[0])
        with bridge.bridge_lock():
            rows = json.loads(bridge.bd("list", "--all", "--limit", "0", "--json")) or []
            # Once hydrated, a legacy bare reference is bound to an immutable issue ID.
            with bridge.database() as db:
                known = {key: json.loads(raw)["id"] for key, raw in db.execute("SELECT stream,current FROM linear_primary")}
            for row in rows:
                if row["id"] in known:
                    row.setdefault("metadata", {})["linearIssueID"] = known[row["id"]]
            found = match(rows, issue)
            if action == "linear-preview":
                return dict(issue=issue, existing=found["id"] if found else None,
                            closed=bool(found and found["status"] == "closed"))
            _, folder, provider, expected, preview_id, was_closed = args
            if (issue["id"] != preview_id or (found["id"] if found else "") != expected
                    or bool(found and found["status"] == "closed") != (was_closed == "1")):
                raise ValueError("The matching workstream changed. Look up the issue again.")
            if found:
                key = found["id"]
                if found["status"] == "closed":
                    bridge.bd("reopen", key)
            else:
                key = bridge.create_stream(issue["title"], folder, issue["url"], provider, issue["id"])
            with bridge.database() as db:
                save(db, key, issue)
        return key

    stream = args[0]
    with bridge.database() as db:
        if action == "linear-pin":
            ref = reference(args[1])
            if not ref:
                raise ValueError("Choose a linked Linear issue.")
            issue = fetch(api, ref[1], ref[0])
            with bridge.bridge_lock():
                parent = json.loads(bridge.bd("show", stream, "--json"))[0]
                if parent.get("external_ref") or parent.get("parent") or not (parent.get("metadata") or {}).get("folder"):
                    raise ValueError("The workstream already has a reference or is unavailable.")
                bridge.bd("update", stream, "--external-ref", issue["url"], "--set-metadata", "linearIssueID=" + issue["id"])
                save(db, stream, issue)
        elif action == "linear-refresh":
            parent = json.loads(bridge.bd("show", stream, "--json"))[0]
            ref = reference(parent.get("external_ref") or "")
            if not ref:
                raise ValueError("This workstream has no primary Linear reference.")
            cached = db.execute("SELECT current FROM linear_primary WHERE stream=?", (stream,)).fetchone()
            identity = json.loads(cached[0])["id"] if cached else (parent.get("metadata") or {}).get("linearIssueID")
            issue = fetch(api, identity or ref[1], "" if identity else ref[0])
            # Ignore an out-of-order refresh when another lookup updated this card.
            db.execute("BEGIN IMMEDIATE")
            newest = db.execute("SELECT current FROM linear_primary WHERE stream=?", (stream,)).fetchone()
            if newest == cached:
                save(db, stream, issue)
        elif action == "linear-seen":
            row = db.execute("SELECT current FROM linear_primary WHERE stream=?", (stream,)).fetchone()
            if row and hashlib.sha256(row[0].encode()).hexdigest() == args[1]:
                db.execute("UPDATE linear_primary SET baseline=current WHERE stream=? AND current=?", (stream, row[0]))
        elif action == "linear-ask":
            ask_linear(db, api, stream, int(args[1]))
        elif action == "linear-poll":
            poll_reply(db, api, stream, int(args[1]))
            db.commit()
            deliver(bridge, db, stream, int(args[1]))
        elif action == "linear-deliver":
            deliver(bridge, db, stream, int(args[1]))
        elif action == "linear-share":
            share(db, api, *args)
        elif action == "linear-context":
            return dict(issue=cards(db, stream).get(stream), questions=question_state(db).get(stream, []))
        else:
            raise ValueError("Unknown Linear action.")
