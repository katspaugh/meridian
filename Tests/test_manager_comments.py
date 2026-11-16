"""Comment intake and delivery use fake providers and terminals; no external writes."""
import json
import subprocess
from unittest.mock import Mock, patch
import unittest

from test_manager_actions import Fixture
import manager
import manager_comments as comments
import manager_store as store


class CommentTests(Fixture):
    def setUp(self):
        super().setUp()
        with self.db() as db:
            store.set_setting(db, 'watch_comments', '1')

    def seed(self, target='https://github.com/o/r/pull/12', plan=None):
        item = dict(id='comment:1', target=target, url=target + '#comment-1', author='reviewer', body='Fix the empty state.')
        with self.db() as db:
            db.execute('INSERT INTO manager_comments(id,payload,plan) VALUES (?,?,?)',
                       (item['id'], json.dumps(item), json.dumps(plan) if plan else None))
        return item

    def follow(self):
        comments.follow_up(self.bridge, self.rows, manager.transaction, manager.brain, manager.untrusted, manager._act_one)

    def done(self):
        with self.db() as db:
            return db.execute('SELECT done FROM manager_comments').fetchone()[0]

    def plan(self, action='follow'):
        self.consult.return_value = dict(plans=[dict(id='comment:1', action=action, message='Fix the empty state.')])

    def linked(self):
        self.rows[0]['links'] = [dict(url='https://github.com/o/r/pull/12')]

    def test_routes_and_deduplicates_successful_delivery(self):
        self.seed(); self.linked(); self.plan()
        self.follow(); self.follow()
        self.assertEqual(self.done(), 1)
        self.assertEqual(len(self.typed()), 2)
        self.consult.assert_called_once()
        self.assertIn(b'#comment-1', self.typed()[0])

    def test_busy_terminal_retains_plan_for_retry(self):
        self.seed(); self.linked(); self.plan()
        with patch.object(manager, 'tell', return_value=False):
            self.follow()
        self.assertEqual(self.done(), 0)
        self.follow()
        self.assertEqual(self.done(), 1)
        self.consult.assert_called_once()

    def test_unmatched_comment_asks_for_new_workstream_once(self):
        self.seed(); self.plan()
        self.follow(); self.follow()
        asks = manager.state(self.bridge)['asks']
        self.assertEqual(len(asks), 1)
        self.assertIn('New workstream needed', asks[0]['summary'])
        self.assertLess(len(asks[0]['summary']), 190)
        self.assertTrue(asks[0]['recommendation'].startswith('https://'))
        self.run.assert_not_called()

    def test_multiple_matches_choose_an_agent_without_asking(self):
        self.seed(); self.plan(); self.linked()
        other = self.stream(id='w2'); other['links'] = self.rows[0]['links']
        self.rows.append(other)
        self.follow()
        self.assertEqual(manager.state(self.bridge)['asks'], [])
        self.assertEqual(self.done(), 1)
        self.assertEqual(len(self.typed()), 2)

    def test_prefers_active_stream_and_keeps_choice_for_retries(self):
        self.seed(); self.plan(); self.linked()
        self.rows[0]['state'] = 'Done'
        other = self.stream(id='w2', terminals=[self.terminal('t2', 'codex', 'Review', stream='w2')])
        other['links'] = self.rows[0]['links']
        self.rows.append(other)
        with patch.object(manager, 'tell', return_value=False) as tell:
            self.follow()
            self.rows[0]['state'] = 'Working'
            self.follow()
        self.assertEqual([call.args[1] for call in tell.call_args_list], ['t2', 't2'])
        self.assertEqual(manager.state(self.bridge)['asks'], [])

    def test_existing_choice_questions_are_requeued_once(self):
        item = self.seed(plan=dict(action='follow', message='Fix it.'))
        self.linked()
        with self.db() as db:
            db.execute('UPDATE manager_comments SET done=1')
            store.open_ask(db, '', '', 'Choose the workstream for this comment; more than one has the same link: '
                           + 'Fix it.\n\nComment: ' + item['url'], [], 'Choose one of: w1, w2')
            store.open_ask(db, '', '', 'Unrelated owner decision', [], '')
        self.follow(); self.follow()
        self.assertEqual(self.done(), 1)
        self.assertEqual(len(self.typed()), 2)
        self.assertEqual([ask['summary'] for ask in manager.state(self.bridge)['asks']], ['Unrelated owner decision'])
        self.consult.assert_not_called()

    def test_dismissed_choice_question_is_not_replayed(self):
        item = self.seed(plan=dict(action='follow', message='Fix it.'))
        with self.db() as db:
            db.execute('UPDATE manager_comments SET done=1')
            ask = store.open_ask(db, '', '', 'Choose the workstream for this comment; more than one has the same link: '
                                 + 'Fix it.\n\nComment: ' + item['url'], [], '')
            store.resolve_asks(db, ask_id=ask)
        self.follow()
        self.run.assert_not_called()

    def test_reopens_completed_stream_before_delivery(self):
        self.seed(); self.plan(); self.linked()
        self.rows[0]['state'] = 'Done'
        self.bridge.bd = Mock()
        self.follow()
        self.bridge.bd.assert_called_once_with('reopen', 'w1')
        self.assertEqual(self.done(), 1)

    def test_failed_reopen_is_retryable(self):
        self.seed(); self.plan(); self.linked()
        self.rows[0]['state'] = 'Done'
        self.bridge.bd = Mock(side_effect=ValueError('unavailable'))
        self.follow()
        self.assertEqual(self.done(), 0)
        self.run.assert_not_called()

    def test_restart_keeps_comment_until_prompt_delivered(self):
        self.seed(); self.plan(); self.linked()
        self.rows[0]['terminals'][0]['state'] = 'Offline'
        self.rows[0]['terminals'][0]['live'] = False
        with patch.object(manager, 'restart', return_value=True) as restart:
            self.follow()
        restart.assert_called_once()
        self.assertEqual(self.done(), 0)
        self.rows[0]['terminals'][0]['state'] = 'Review'
        self.rows[0]['terminals'][0]['live'] = True
        self.follow()
        self.assertEqual(self.done(), 1)

    def test_paused_or_focused_manager_does_not_deliver(self):
        self.seed(); self.plan(); self.linked()
        with self.db() as db:
            store.set_setting(db, 'paused', '1')
        self.follow()
        self.assertEqual(self.done(), 0)
        with self.db() as db:
            store.set_setting(db, 'paused', '0')
            store.set_setting(db, 'focus', 't1')
        self.follow()
        self.assertEqual(self.done(), 0)
        self.run.assert_not_called()

    def test_ignore_and_malformed_decisions(self):
        self.seed(); self.plan()
        self.consult.return_value = dict(plans=[dict(id='comment:1', action='follow', message='')])
        self.follow()
        self.assertEqual(self.done(), 0)
        self.plan('ignore'); self.follow()
        self.assertEqual(self.done(), 1)
        self.run.assert_not_called()

    def test_poll_persists_and_deduplicates_across_restarts(self):
        item = dict(id='new:1', target='https://linear.app/team/issue/ENG-1/title', body='Fix it', url='url', author='A')
        with patch.object(comments, 'github', return_value=[item]), patch.object(comments, 'linear', return_value=[]):
            comments.poll(self.bridge, self.rows, manager.transaction)
            comments.poll(self.bridge, self.rows, manager.transaction)
        with self.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM manager_comments').fetchone()[0], 1)
            self.assertIsNotNone(store.setting(db, 'comments_since_github'))

    def test_provider_failure_keeps_checkpoint_and_reports_once(self):
        with self.db() as db:
            store.set_setting(db, 'comments_since_github', '2026-11-16T00:00:00Z')
        with patch.object(comments, 'github', side_effect=ValueError('secret')), patch.object(comments, 'linear', return_value=[]):
            comments.poll(self.bridge, self.rows, manager.transaction)
            comments.poll(self.bridge, self.rows, manager.transaction)
        with self.db() as db:
            self.assertEqual(store.setting(db, 'comments_since_github'), '2026-11-16T00:00:00Z')
        events = manager.state(self.bridge)['events']
        self.assertEqual(len(events), 1)
        self.assertNotIn('secret', str(events))

    def test_partial_fetch_saves_available_comments_without_advancing_checkpoint(self):
        item = dict(id='new:1', target='url', body='Fix it', url='url', author='A')
        with self.db() as db:
            store.set_setting(db, 'comments_since_github', '2026-11-16T00:00:00Z')
        with patch.object(comments, 'github', side_effect=comments.PartialFetch([item])), patch.object(comments, 'linear', return_value=[]):
            comments.poll(self.bridge, self.rows, manager.transaction)
        with self.db() as db:
            self.assertEqual(store.setting(db, 'comments_since_github'), '2026-11-16T00:00:00Z')
            self.assertEqual(db.execute('SELECT id FROM manager_comments').fetchone()[0], 'new:1')

    def test_offline_review_terminal_is_restarted(self):
        self.seed(); self.plan(); self.linked()
        self.rows[0]['terminals'][0]['live'] = False
        with patch.object(manager, 'restart', return_value=True) as restart:
            self.follow()
        restart.assert_called_once()
        self.assertEqual(self.done(), 0)

    def test_quiet_workstreams_still_trigger_comment_polling(self):
        with patch.object(manager.brain, 'config', return_value={}):
            self.assertTrue(manager.state(self.bridge)['sweepDue'])
            manager.mark_swept(self.bridge, '')
            self.assertFalse(manager.state(self.bridge)['sweepDue'])


class ProviderTests(unittest.TestCase):
    def test_github_errors_distinguish_auth_access_network_and_rate_limits(self):
        for status, kind in ((401, 'auth'), (403, 'access'), (404, 'not-found'), (429, 'rate-limit'), (502, 'network')):
            result = Mock(returncode=1, stdout='', stderr=f'secret diagnostic (HTTP {status})')
            with patch.object(comments.subprocess, 'run', return_value=result):
                with self.assertRaises(comments.GitHubFailure) as raised:
                    comments.gh('user')
            self.assertEqual(raised.exception.kind, kind)
            self.assertNotIn('secret', str(raised.exception))
        with patch.object(comments.subprocess, 'run', side_effect=FileNotFoundError()):
            with self.assertRaises(comments.GitHubFailure) as raised:
                comments.gh('user')
        self.assertEqual(raised.exception.kind, 'missing-cli')
        with patch.object(comments.subprocess, 'run', side_effect=subprocess.TimeoutExpired('gh', 60)):
            with self.assertRaises(comments.GitHubFailure) as raised:
                comments.gh('user')
        self.assertEqual(raised.exception.kind, 'network')

    def test_partial_resource_failure_is_not_reported_as_login_failure(self):
        failure = comments.PartialFetch([], [('pull/12', 'not-found'), ('pull/12', 'not-found')])
        message = comments.github_error(failure)
        self.assertIn('GitHub is connected', message)
        self.assertIn('1 source', message)
        self.assertNotIn('auth', message)
        self.assertNotIn('login', message)
        self.assertNotIn('auth', comments.github_error(comments.PartialFetch([], [('pull/12', 'network')])))

    def test_linear_prefers_environment_without_reading_keychain(self):
        with patch.dict(comments.os.environ, {'LINEAR_API_KEY': 'test-env-key'}), patch.object(comments.subprocess, 'run') as run:
            self.assertEqual(comments.linear_token(), 'test-env-key')
        run.assert_not_called()

    def test_linear_reads_keychain_when_environment_missing(self):
        with patch.dict(comments.os.environ, {}, clear=True), patch.object(comments.subprocess, 'run',
                return_value=Mock(returncode=0, stdout='test-keychain-key\n')) as run:
            self.assertEqual(comments.linear_token(), 'test-keychain-key')
        self.assertIn('local.meridian.linear', run.call_args.args[0])
        self.assertTrue(run.call_args.kwargs['capture_output'])

    def test_keychain_failure_or_timeout_returns_no_credential(self):
        with patch.dict(comments.os.environ, {}, clear=True):
            with patch.object(comments.subprocess, 'run', return_value=Mock(returncode=44, stdout='')):
                self.assertEqual(comments.linear_token(), '')
            with patch.object(comments.subprocess, 'run', side_effect=subprocess.TimeoutExpired('security', 10)):
                self.assertEqual(comments.linear_token(), '')

    def test_github_fetches_issue_inline_and_review_comments_and_unmatched_notifications(self):
        paths = []
        def gh(path, paginate=False):
            paths.append(path)
            if path == 'user': return dict(login='me')
            if path.startswith('notifications'):
                return [dict(subject=dict(url='https://api.github.com/repos/o/r/pulls/12'))]
            return [dict(id=1, body='Please fix', updated_at='2026-11-16T12:00:00Z', user=dict(login='reviewer')),
                    dict(id=2, body='My reply', updated_at='2026-11-16T12:00:00Z', user=dict(login='me'))]
        with patch.object(comments, 'gh', side_effect=gh):
            result = comments.github([], '2026-11-16T00:00:00Z')
        self.assertEqual(len(result), 3)
        self.assertEqual(len({r['id'] for r in result}), 3)
        self.assertTrue(all(r['target'] == 'https://github.com/o/r/pull/12' for r in result))
        self.assertTrue(all('/comments?' in p or '/reviews?' in p for p in paths[2:]))

    def test_linear_pages_and_filters_to_linked_or_personal_comments(self):
        def item(id, assignee='other', author='other'):
            return dict(id=id, body='Fix it', updatedAt='now', url='comment-url', user=dict(id=author, name=author),
                        issue=dict(url='https://linear.app/team/issue/ENG-1/title', assignee=dict(id=assignee)))
        pages = [dict(viewer=dict(id='me'), comments=dict(nodes=[item('1'), item('2', 'me'), item('3','me','me')],
                                                           pageInfo=dict(hasNextPage=True, endCursor='next'))),
                 dict(viewer=dict(id='me'), comments=dict(nodes=[item('4', 'me')], pageInfo=dict(hasNextPage=False)))]
        with patch.object(comments, 'linear_query', side_effect=pages) as query:
            result = comments.linear([], 'since')
        self.assertEqual([r['id'] for r in result], ['linear:2:now', 'linear:4:now'])
        self.assertEqual(query.call_args.args[1]['after'], 'next')

    def test_linear_identity_ignores_workspace_slug_and_issue_title(self):
        self.assertEqual(comments.identity('https://linear.app/issue/ENG-1'),
                         comments.identity('https://linear.app/team/issue/eng-1/some-title'))


if __name__ == '__main__':
    unittest.main()
