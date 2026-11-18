"""Settings bridge. Credentials travel over stdin and stay in macOS Keychain."""
import os
import re
import subprocess

import manager
import manager_comments as comments
import manager_store as store

SERVICE = 'local.meridian.linear'
ACCOUNT = 'comments'


def keychain(*args):
    return subprocess.run(['/usr/bin/security', *args], capture_output=True, text=True, timeout=15)


def state(bridge):
    try:
        stored = keychain('find-generic-password', '-s', SERVICE, '-a', ACCOUNT).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        stored = False
    with manager.transaction(bridge) as db:
        paused = store.paused(db)
    return dict(paused=paused, watchComments=manager.watch_comments(bridge), linearStored=stored,
                linearEnvironment=bool(os.environ.get('LINEAR_API_KEY')))


def perform(bridge, action, payload=None):
    payload = payload or {}
    if action == 'state':
        return state(bridge)
    if action == 'preferences':
        if type(payload.get('paused')) is not bool or type(payload.get('watchComments')) is not bool:
            raise ValueError('Settings must be true or false.')
        with manager.transaction(bridge) as db:
            store.set_setting(db, 'paused', '1' if payload['paused'] else '0')
            store.set_setting(db, 'watch_comments', '1' if payload['watchComments'] else '0')
        return dict(ok=True, message='Settings saved.')
    if action == 'save-linear':
        key = payload.get('key', '')
        # Reject command separators, whitespace and quotes before the security interactive parser.
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{10,256}', key):
            raise ValueError('Enter a valid Linear personal API key.')
        try:
            comments.linear_query('{ viewer { id } comments(first: 1) { nodes { id } } }', {}, token=key)
        except Exception:
            raise ValueError('Linear could not verify this key. Check its read access and try again.') from None
        command = f'add-generic-password -U -s {SERVICE} -a {ACCOUNT} -T /usr/bin/security -w {key}\n'
        try:
            result = subprocess.run(['/usr/bin/security', '-i'], input=command, capture_output=True, text=True, timeout=30)
            check = keychain('find-generic-password', '-s', SERVICE, '-a', ACCOUNT, '-w')
            if result.returncode or check.returncode or check.stdout.strip() != key:
                raise ValueError('Keychain did not save the credential.')
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError('Could not access macOS Keychain. Unlock it and try again.') from None
        with manager.transaction(bridge) as db:
            store.set_setting(db, 'comments_error_linear', '')
        return dict(ok=True, message='Linear key verified and saved in Keychain.')
    if action == 'remove-linear':
        try:
            result = keychain('delete-generic-password', '-s', SERVICE, '-a', ACCOUNT)
            if result.returncode not in (0, 44):
                raise ValueError('Could not remove the Linear key from Keychain.')
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError('Could not access macOS Keychain.') from None
        return dict(ok=True, message='Saved Linear key removed.')
    if action in ('test-linear', 'test-github'):
        try:
            if action == 'test-linear':
                comments.linear_query('{ viewer { id } comments(first: 1) { nodes { id } } }', {})
            else:
                comments.gh('user')
                comments.gh('notifications?per_page=1')
        except Exception as exc:
            raise ValueError('Linear connection failed. Check the key and read access.' if action == 'test-linear'
                             else comments.github_error(exc)) from None
        if action == 'test-github':
            with manager.transaction(bridge) as db:
                if store.setting(db, 'comments_error_github') == (
                        'GitHub comment monitoring unavailable. Check gh auth status and repo/notifications access.'):
                    store.set_setting(db, 'comments_error_github', '')
        return dict(ok=True, message='Linear connection verified.' if action == 'test-linear' else 'GitHub connection verified.')
    raise ValueError('Unknown settings action.')
