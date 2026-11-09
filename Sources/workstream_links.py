"""Collect workstream artifacts from successful tool calls and saved Claude turns."""
import json
import re
import shlex
import subprocess
from pathlib import Path

PR = re.compile(r'https?://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)\b', re.I)
LINEAR = re.compile(r'https?://linear\.app/(?:(?P<team>[\w-]+)/)?issue/(?P<id>[a-z][a-z0-9]*-\d+)\b', re.I)
ISSUE = re.compile(r'\b[A-Z][A-Z0-9]{0,15}-[1-9]\d*\b')
REPO = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+')
MUTATIONS = {'create', 'edit', 'merge', 'close', 'reopen', 'ready', 'review', 'comment'}


def initialize(db):
    db.execute("CREATE TABLE IF NOT EXISTS links (stream TEXT, identity TEXT, kind TEXT, label TEXT, url TEXT, PRIMARY KEY(stream,identity))")
    db.execute("CREATE TABLE IF NOT EXISTS link_index (stream TEXT, path TEXT, offset INTEGER, pending TEXT, PRIMARY KEY(stream,path))")


def save(db, key, kind, label, url):
    db.execute("""INSERT INTO links VALUES (?,?,?,?,?) ON CONFLICT(stream,identity)
                  DO UPDATE SET url=excluded.url WHERE links.url != excluded.url AND links.url LIKE 'https://linear.app/issue/%'""",
               (key, (kind + ':' + label).lower(), kind, label, url))


def collect(db, key, text, *, pulls=True, issues=True):
    if pulls:
        for owner, repo, number in PR.findall(text):
            save(db, key, 'pr', f'{owner}/{repo}#{number}', f'https://github.com/{owner}/{repo}/pull/{number}')
    if issues:
        for match in LINEAR.finditer(text):
            label = match['id'].upper()
            save(db, key, 'linear', label, match.group().split('?')[0])
        for label in ISSUE.findall(text):
            if not label.startswith(('UTF-', 'ISO-', 'SHA-', 'CVE-', 'RFC-')):
                save(db, key, 'linear', label, 'https://linear.app/issue/' + label)


def output_text(value):
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def command_output(args, cwd):
    try:
        proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=4)
        return proc.stdout.strip() if proc.returncode == 0 else ''
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return ''


def repository(folder):
    remote = command_output(['git', 'remote', 'get-url', 'origin'], folder)
    match = re.search(r'github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?$', remote, re.I)
    return match[1] if match else ''


def tool_result(db, key, name, args, result, folder, live=False):
    text = output_text(result)
    lower = name.lower()
    if 'linear' in lower and any(t in lower for t in ('get_issue', 'save_issue', 'create_issue', 'update_issue', 'create_comment', 'list_comments')):
        collect(db, key, output_text(args.get('id', args.get('issueId', ''))) + ' ' + text, pulls=False)
        return
    if 'github' in lower and any(t in lower for t in ('create_pull_request', 'update_pull_request', 'merge_pull_request', 'review_write', 'add_comment_to_pending_review')):
        number = args.get('pullNumber') or args.get('pull_number')
        repo = f"{args.get('owner','')}/{args.get('repo','')}"
        if number and str(number).isdigit() and REPO.fullmatch(repo):
            collect(db, key, f'https://github.com/{repo}/pull/{number}', issues=False)
        collect(db, key, text, issues=False)
        return
    if name != 'Bash':
        return
    cmd = args.get('command', '')
    # gh api is also commonly used to edit PRs, reviews, and the base branch.
    api = re.search(r'\bgh\s+api\s+([^;&|\n]+)', cmd)
    if api and re.search(r'(?:-X\s*|--method[= ])(?:PATCH|POST|PUT|DELETE)\b|(?:^|\s)(?:-[fF]|--(?:raw-)?field)\s', api[1]):
        target = re.search(r"""(?:^|[\s'"])/?repos/([\w.-]+)/([\w.-]+)/pulls(?:/(\d+))?""", api[1])
        if target:
            if target[3]:
                collect(db, key, f'https://github.com/{target[1]}/{target[2]}/pull/{target[3]}', issues=False)
            collect(db, key, text, issues=False)
    match = re.search(r'(?:^|[;&|\n]\s*|\$\()\s*gh\s+pr\s+(\w+)\b([^\n;&|]*)', cmd)
    push = re.search(r'(?:^|[;&|\n]\s*)\s*git\s+push\b', cmd)
    if not ((match and match[1] in MUTATIONS) or push):
        return
    collect(db, key, text, issues=False)
    if match and match[1] in MUTATIONS:
        try:
            words = shlex.split(match[2])
        except ValueError:
            words = match[2].split()
        target = words[0] if words and not words[0].startswith('-') else ''
        if PR.fullmatch(target):
            collect(db, key, target, issues=False)
            return
        repo = repository(folder)
        for opt in ('--repo', '-R'):
            if opt in words and words.index(opt) + 1 < len(words):
                repo = words[words.index(opt) + 1]
        if target.isdigit() and REPO.fullmatch(repo):
            collect(db, key, f'https://github.com/{repo}/pull/{target}', issues=False)
            return
    else:
        branch = re.search(r'\s->\s+([^\s]+)', text.replace('\\n', '\n'))
        target = branch[1] if branch else ''
        repo = repository(folder)
    if live and not PR.search(text) and folder:
        view = ['gh', 'pr', 'view']
        if target and not target.startswith('-'):
            view += [target]
        if repo and target and REPO.fullmatch(repo):
            view += ['--repo', repo]
        collect(db, key, command_output(view + ['--json', 'url', '--jq', '.url'], folder), issues=False)


def backfill(db, key, path, folder):
    if not path:
        return
    try:
        f = Path(path).open('rb')
    except OSError:
        return
    with f:
        old = db.execute('SELECT offset,pending FROM link_index WHERE stream=? AND path=?', (key,path)).fetchone()
        f.seek(0, 2)
        size = f.tell()
        offset, pending = old if old and old[0] <= size else (0, '{}')
        if size == offset:
            return
        pending = json.loads(pending)
        f.seek(offset)
        for line in f:
            if not line.endswith(b'\n'):
                break
            offset += len(line)
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            blocks = row.get('message', {}).get('content', [])
            if not isinstance(blocks, list):
                continue
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                typ = b.get('type')
                if typ == 'tool_use':
                    name, args = b.get('name',''), b.get('input',{})
                    if ('linear' in name.lower() or 'github' in name.lower() or
                        (name == 'Bash' and re.search(r'\b(?:gh\s+(?:pr|api)|git\s+push)\b', args.get('command','')))):
                        pending[b['id']] = (name,args,row.get('cwd') or folder)
                elif typ == 'tool_result':
                    call = pending.pop(b.get('tool_use_id'), None)
                    if call and not b.get('is_error'):
                        tool_result(db,key,call[0],call[1],b.get('content',''),call[2])
                elif typ == 'text' and row.get('type') == 'assistant':
                    # Recover linked handoffs from sessions predating the hooks.
                    collect(db,key,b.get('text',''),pulls=False)
        db.execute('INSERT OR REPLACE INTO link_index VALUES (?,?,?,?)', (key,path,offset,json.dumps(pending)))
