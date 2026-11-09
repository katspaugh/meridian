"""Workstream-scoped Claude subagent discovery and transcript display."""
import json
from pathlib import Path


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS agent_sessions
               (stream TEXT, session TEXT, transcript TEXT, PRIMARY KEY(stream,session))""")
    db.execute("""CREATE TABLE IF NOT EXISTS agents
               (stream TEXT, session TEXT, agent TEXT, role TEXT, state TEXT, path TEXT, result TEXT,
                PRIMARY KEY(stream,session,agent))""")


def record(db, key, sid, event):
    agent = event.get('agent_id')
    if not agent:
        return
    name = event['hook_event_name']
    states = {'SubagentStart': 'Working', 'SubagentStop': 'Finished', 'PermissionRequest': 'Needs you',
              'PreToolUse': 'Needs you', 'PostToolUse': 'Working', 'PostToolUseFailure': 'Working'}
    if name not in states:
        return
    db.execute("""INSERT INTO agents VALUES (?,?,?,?,?,?,?) ON CONFLICT(stream,session,agent)
        DO UPDATE SET role=coalesce(excluded.role,agents.role),
        state=CASE WHEN agents.state='Finished' AND excluded.state!='Finished' AND ?!='SubagentStart'
                   THEN agents.state ELSE excluded.state END,
        path=coalesce(excluded.path,agents.path),result=coalesce(excluded.result,agents.result)""",
        (key,sid,agent,event.get('agent_type'),states[name],event.get('agent_transcript_path'),
         event.get('last_assistant_message'),name))


def discover(db, key, sid, transcript, live):
    if transcript:
        db.execute('INSERT OR IGNORE INTO agent_sessions VALUES (?,?,?)',(key,sid,transcript))
        db.execute('UPDATE agent_sessions SET transcript=? WHERE stream=? AND session=? AND transcript!=?',
                   (transcript,key,sid,transcript))
    records = {}
    for session, agent, role, state, path in db.execute(
            'SELECT session,agent,role,state,path FROM agents WHERE stream=? ORDER BY rowid', (key,)):
        records[session,agent] = dict(role=role, state=state, path=path)
    for session, parent in db.execute('SELECT session,transcript FROM agent_sessions WHERE stream=?',(key,)):
        directory = Path(parent).with_suffix('') / 'subagents'
        for file in directory.glob('agent-*'):
            if not (file.name.endswith('.meta.json') or file.suffix == '.jsonl'):
                continue
            agent = file.name[6:].split('.')[0]
            item = records.setdefault((session,agent), {})
            if file.suffix == '.jsonl':
                item['path'] = str(file)
                continue
            try:
                meta = json.loads(file.read_text())
            except (OSError, ValueError):
                continue
            item['title'] = meta.get('description')
            item['role'] = meta.get('agentType') or item.get('role')
            if meta.get('finished') is True:
                item['state'] = 'Finished'
            elif meta.get('finished') is False:
                item.setdefault('state','Working')
    for (session,agent),item in records.items():
        item['id'] = session + '/' + agent
        item['role'] = item.get('role') or 'Subagent'
        item['title'] = item.get('title') or (item['role'] + ' · ' + agent[-6:])
        state = item.get('state', 'Recorded')
        item['state'] = 'Offline' if state in ('Needs you','Working') and (session != sid or not live) else state
        try:
            item['revision'] = str(Path(item['path']).stat().st_mtime_ns)
        except (OSError, TypeError, KeyError):
            item['revision'] = ''
    return list(records.values())


def messages(db, key, identity):
    row = db.execute('SELECT session,transcript FROM activity WHERE id=?',(key,)).fetchone()
    if not row:
        return []
    agents = discover(db,key,*row,live=True)
    item = next((agent for agent in agents if agent['id'] == identity), None)
    if not item:
        return []
    entries = []
    try:
        file = Path(item.get('path') or '').open()
    except OSError:
        file = None
    if file:
        with file:
            for line in file:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                role = row.get('type')
                if role not in ('user','assistant') or row.get('isMeta'):
                    continue
                content = row.get('message',{}).get('content',[])
                if isinstance(content,str):
                    content = [dict(type='text',text=content)]
                for block in content:
                    if block.get('type') == 'text' and block.get('text','').strip():
                        entries.append(dict(label='Assignment' if role=='user' else 'Response',text=block['text']))
                    elif block.get('type') == 'tool_use':
                        entries.append(dict(label='Tool',text=block.get('name','Tool')))
    session,agent = identity.split('/',1)
    result = db.execute('SELECT result FROM agents WHERE stream=? AND session=? AND agent=?',
                        (key,session,agent)).fetchone()
    if result and result[0] and not any(e['text']==result[0] for e in entries):
        entries.append(dict(label='Response',text=result[0]))
    return entries
