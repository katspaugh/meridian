"""Collect workstream artifacts from successful tool calls and saved Claude turns."""
import json
import re
import shlex
import subprocess
from urllib.parse import urlsplit
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PR = re.compile(r'https?://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)\b', re.I)
LINEAR = re.compile(r'https?://linear\.app/(?:(?P<team>[\w-]+)/)?issue/(?P<id>[a-z][a-z0-9]*-\d+)\b', re.I)
ISSUE = re.compile(r'\b[A-Z][A-Z0-9]{0,15}-[1-9]\d*\b')
REPO = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+')
MUTATIONS = {'create', 'edit', 'merge', 'close', 'reopen', 'ready', 'review', 'comment'}
PASSED = {'SUCCESS', 'NEUTRAL', 'SKIPPED'}


GITHUB_ITEM = re.compile(r'https?://github\.com/([\w.-]+)/([\w.-]+)/(pull|issues)/([1-9]\d*)(?:[/?#].*)?', re.I)


def github_item(value):
    match = GITHUB_ITEM.fullmatch(value.strip())
    if not match:
        return None
    owner, repo, kind, number = match.groups()
    if owner in ('.', '..') or repo in ('.', '..'):
        return None
    return f'https://github.com/{owner}/{repo}/{kind.lower()}/{number}'


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
        item = github_item(text)
        if item:
            _, _, _, owner, repo, kind, number = item.split('/')
            if kind == 'issues':
                save(db, key, 'github-issue', f'{owner}/{repo}#{number}', item)
            return
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


def command_output(args, cwd, timeout=4):
    try:
        proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return proc.stdout.strip() if proc.returncode == 0 else ''
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return ''


def repository(folder, remote_name='origin'):
    remote = command_output(['git', 'remote', 'get-url', '--', remote_name], folder)
    match = re.fullmatch(r'(?:git@github\.com:|(?:https?|git|ssh)://(?:git@)?github\.com(?::\d+)?/)([\w.-]+/[\w.-]+?)(?:\.git)?/?', remote, re.I)
    return match[1] if match else ''


def summarize(pull):
    """Reduce `gh pr view --json` output to what one row of the Changes list shows."""
    checks = pull.get('statusCheckRollup') or []
    failures = []
    finished = failed = 0
    for check in checks:
        if check.get('__typename') == 'StatusContext':
            result = check.get('state')
            done = result not in ('PENDING', 'EXPECTED')
        else:
            result = check.get('conclusion')
            done = check.get('status') == 'COMPLETED'
        finished += done
        if done and result not in PASSED:
            failed += 1
            target = check.get('detailsUrl') or check.get('targetUrl') or ''
            failures.append(dict(name=check.get('name') or check.get('context') or 'Unnamed check',
                                 url=target if urlsplit(target).scheme in ('https', 'http') else None))
    state = pull.get('state')
    count = lambda value: value if type(value) is int else None
    summary = dict(title=str(pull.get('title') or ''), state='DRAFT' if pull.get('isDraft') and state == 'OPEN' else state,
                additions=count(pull.get('additions')), deletions=count(pull.get('deletions')),
                checks=len(checks), finished=finished, failed=failed)
    if failures:
        summary['failures'] = failures
    if pull.get('headRefOid'):
        summary['head'] = pull['headRefOid']
    if state == 'OPEN':
        attention = []
        if pull.get('reviewDecision') == 'CHANGES_REQUESTED':
            attention.append('Changes requested')
        if pull.get('mergeable') == 'CONFLICTING':
            attention.append('Merge conflict')
        if attention:
            summary['attention'] = attention
    return summary


def pull_details(urls):
    """Look up each PR on GitHub. PRs gh cannot read are omitted and stay plain links."""
    urls = [url for url in dict.fromkeys(urls) if PR.fullmatch(url)]

    def fetch(url):
        fields = 'title,state,isDraft,additions,deletions,statusCheckRollup,headRefOid,reviewRequests,reviewDecision,mergeable'
        try:
            pull = json.loads(command_output(['gh', 'pr', 'view', url, '--json', fields], '/', timeout=15))
            row = summarize(pull)
            reviewers = [r['login'].casefold() for r in pull.get('reviewRequests', []) if r.get('login')]
            return row, reviewers if pull.get('state') == 'OPEN' else []
        except (ValueError, AttributeError, TypeError):
            return None

    if not urls:
        return {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        found = {url: value for url, value in zip(urls, pool.map(fetch, urls)) if value}
    if any(reviewers for _, reviewers in found.values()):
        viewer = command_output(['gh', 'api', '--hostname', 'github.com', 'user', '--jq', '.login'], '/', timeout=10).casefold()
        for row, reviewers in found.values():
            if viewer in reviewers:
                row.setdefault('attention', []).insert(0, 'Review requested from you')
    return {url: row for url, (row, _) in found.items()}


def review_threads(url):
    match = PR.fullmatch(url)
    if not match:
        raise ValueError("Choose a GitHub pull request.")
    owner, repo, number = match.groups()
    query = """query($owner:String!, $repo:String!, $number:Int!, $endCursor:String) {
      repository(owner:$owner,name:$repo) { pullRequest(number:$number) {
        reviewThreads(first:100,after:$endCursor) {
          pageInfo { hasNextPage endCursor }
          nodes { isResolved isOutdated path line originalLine comments(first:1) {
            nodes { body url author { login } }
          } }
        }
      } }
    }"""
    output = command_output(['gh', 'api', '--hostname', 'github.com', 'graphql', '--paginate', '--slurp',
                             '-f', 'query=' + query, '-f', 'owner=' + owner, '-f', 'repo=' + repo,
                             '-F', 'number=' + number], '/', timeout=30)
    if not output:
        raise ValueError('Could not load review threads. Check GitHub CLI access and retry.')
    threads = []
    for page in json.loads(output):
        for thread in page['data']['repository']['pullRequest']['reviewThreads']['nodes']:
            comments = thread['comments']['nodes']
            if thread['isResolved'] or not comments:
                continue
            comment = comments[0]
            threads.append(dict(url=comment['url'], body=comment['body'], author=(comment['author'] or {}).get('login'),
                                path=thread['path'], line=thread['line'] or thread['originalLine'],
                                outdated=thread['isOutdated']))
    return threads


def check_context(pr, name, target, head):
    match = PR.fullmatch(pr)
    if not match:
        raise ValueError('Choose a GitHub pull request.')
    repo = '/'.join(match.groups()[:2])
    context = [f'Pull request: {pr}', f'Check: {name}', f'PR head: {head}', f'Check URL: {target or pr + "/checks"}']
    job = re.fullmatch(r'https://github\.com/' + re.escape(repo) + r'/(?:actions/runs/\d+/job|runs)/(\d+)(?:[?#].*)?', target, re.I)
    if job:
        job_id = job[1]
        metadata = command_output(['gh', 'api', '--hostname', 'github.com', f'repos/{repo}/actions/jobs/{job_id}'], '/', timeout=15)
        if metadata:
            data = json.loads(metadata)
            context.append('Job commit: ' + str(data.get('head_sha') or 'unknown'))
            context.extend('Failed step: ' + step['name'] for step in data.get('steps', [])
                           if step.get('conclusion') in ('failure', 'timed_out', 'cancelled'))
        log = command_output(['gh', 'run', 'view', '--repo', 'github.com/' + repo, '--job', job_id, '--log-failed'], '/', timeout=40)
        context.append('Failed-step log (last 12000 characters):\n' + log[-12000:] if log else 'Failed-step log unavailable.')
    return '\n'.join(context)


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
