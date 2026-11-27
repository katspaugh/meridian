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
        conflicts = patch.object(comments, 'poll_conflicts')
        self.poll_conflicts = conflicts.start()
        self.addCleanup(conflicts.stop)

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

    def test_new_feedback_bypasses_old_nudge_cap_and_clears_only_cap_question(self):
        self.seed(); self.linked(); self.plan()
        with self.db() as db:
            for _ in range(store.MAX_NUDGES): store.add_nudge(db, 't1')
            store.open_ask(db, 'w1', 't1', manager.NUDGE_CAP, [], '')
            store.open_ask(db, 'w1', '', 'Unrelated question', [], '')
        self.follow()
        self.assertEqual(self.done(), 1)
        with self.db() as db: self.assertEqual(store.nudges(db, 't1'), 0)
        self.assertEqual([a['summary'] for a in manager.state(self.bridge)['asks']], ['Unrelated question'])

    def test_batches_pending_comments_in_one_prompt(self):
        first = self.seed(plan=dict(action='follow', message='First fix.')); self.linked()
        second = dict(first, id='comment:2', url=first['url'] + '-second')
        with self.db() as db:
            db.execute('INSERT INTO manager_comments(id,payload,plan) VALUES (?,?,?)',
                       (second['id'], json.dumps(second), json.dumps(dict(action='follow', message='Second fix.'))))
        self.follow()
        self.assertEqual(len(self.typed()), 2)
        self.assertIn(b'First fix.', self.typed()[0])
        self.assertIn(b'Second fix.', self.typed()[0])
        with self.db() as db: self.assertEqual(db.execute('SELECT count(*) FROM manager_comments WHERE done=0').fetchone()[0], 0)

    def test_newest_revision_supersedes_old_pending_feedback(self):
        item = self.seed(plan=dict(action='follow', message='Obsolete')); self.linked()
        with self.db() as db:
            db.execute("UPDATE manager_comments SET id='github:comment:12:old'")
            item['id'] = 'github:comment:12:new'
            db.execute('INSERT INTO manager_comments(id,payload,plan) VALUES (?,?,?)',
                       (item['id'], json.dumps(item), json.dumps(dict(action='follow', message='Current fix.'))))
        self.follow()
        self.assertNotIn(b'Obsolete', self.typed()[0])
        self.assertIn(b'Current fix.', self.typed()[0])
        with self.db() as db: self.assertEqual(db.execute('SELECT count(*) FROM manager_comments WHERE done=0').fetchone()[0], 0)

    def test_uses_idle_sibling_agent(self):
        self.seed(); self.linked(); self.plan()
        self.rows[0]['terminals'][0]['state'] = 'Working'
        self.rows[0]['terminals'].append(self.terminal('t2', 'codex', 'Review'))
        with patch.object(manager, 'tell', return_value=True) as tell: self.follow()
        self.assertEqual(tell.call_args.args[1], 't2')

    def test_saved_plans_deliver_without_waiting_for_unclassified_comments(self):
        item = self.seed(plan=dict(action='follow', message='Saved fix.')); self.linked()
        with self.db() as db:
            db.execute('INSERT INTO manager_comments(id,payload) VALUES (?,?)', ('comment:2', json.dumps(dict(item, id='comment:2'))))
        comments.follow_up(self.bridge, self.rows, manager.transaction, manager.brain, manager.untrusted, manager._act_one, classify=False)
        self.consult.assert_not_called()
        self.assertEqual(self.done(), 1)

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
        self.assertEqual(self.poll_conflicts.call_count, 2)

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

    def test_linear_follow_up_asks_for_a_draft_reply(self):
        url = 'https://linear.app/acme/issue/ABC-1'
        item = dict(id='linear:c1:now', target=url, url=url + '#comment-c1', author='pm', body='Can you add X?')
        with self.db() as db:
            db.execute('INSERT INTO manager_comments(id,payload,plan) VALUES (?,?,?)',
                       (item['id'], json.dumps(item), json.dumps(dict(action='follow', message='Add X.'))))
        self.rows[0]['links'] = [dict(url=url)]
        with patch.object(manager, 'tell', return_value=True) as tell:
            self.follow()
        self.assertIn('draft', tell.call_args.args[2].lower())
        self.assertIn('Comment: ' + item['url'], tell.call_args.args[2])

    def test_github_follow_up_has_no_draft_rule(self):
        self.seed(); self.linked(); self.plan()
        with patch.object(manager, 'tell', return_value=True) as tell:
            self.follow()
        self.assertNotIn('draft', tell.call_args.args[2].lower())

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

    def test_question_replies_use_only_the_workstream_thread_delivery(self):
        issue = dict(url="https://linear.app/team/issue/ENG-1/title", assignee=dict(id="me"))
        comment = dict(id="answer", body="Keep the old behavior", updatedAt="now", url="reply",
                       user=dict(id="colleague"), issue=issue, parent=dict(id="question"))
        another = dict(comment, id="unrelated", parent=dict(id="another-thread"))
        page = dict(viewer=dict(id="me"), comments=dict(nodes=[comment, another], pageInfo=dict(hasNextPage=False)))
        with patch.object(comments, "linear_query", return_value=page):
            result = comments.linear([dict(linearQuestions=[dict(comment="question")])], "since")
        self.assertEqual([r["id"] for r in result], ["linear:unrelated:now"])

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



PR = 'https://github.com/o/r/pull/12'
SHA = 'a' * 40


def node(mergeable='CONFLICTING', repo='o/r', number=12, sha=SHA):
    return dict(number=number, mergeable=mergeable, headRefOid=sha, repository=dict(nameWithOwner=repo))


def page(*nodes, after=None):
    return dict(search=dict(nodes=list(nodes), pageInfo=dict(hasNextPage=after is not None, endCursor=after)))


class ConflictTests(Fixture):
    def setUp(self):
        super().setUp()
        with self.db() as db:
            store.set_setting(db, 'watch_comments', '1')
        self.bridge.open_github_stream = Mock(side_effect=AssertionError('no workstream should be created'))
        # A PR requested on its own, whose mergeability GitHub has not finished computing.
        probe = patch.object(comments, 'gh', return_value=dict(state='open', mergeable=None, head=dict(sha=SHA)))
        self.gh = probe.start()
        self.addCleanup(probe.stop)

    def poll(self, *pages):
        with patch.object(comments, 'graphql', side_effect=list(pages)):
            comments.poll_conflicts(self.bridge, self.rows, manager.transaction)

    def follow(self):
        comments.follow_up(self.bridge, self.rows, manager.transaction, manager.brain, manager.untrusted, manager._act_one)

    def pending(self):
        return [key for key, in self.query('SELECT id FROM manager_comments WHERE done=0 ORDER BY rowid')]

    def test_search_pages_and_keeps_only_valid_conflicts(self):
        pages = [page(node(), node('MERGEABLE', number=13), node('UNKNOWN', number=14), after='next'),
                 page(node(repo='bad repo'), node(sha='nope'), {}, node(repo='x/y', number=7, sha='b' * 40))]
        with patch.object(comments, 'graphql', side_effect=pages) as query:
            items, unknown = comments.conflicts()
        self.assertEqual([i['id'] for i in items], ['github:conflict:o/r#12:' + SHA, 'github:conflict:x/y#7:' + 'b' * 40])
        self.assertEqual(items[0]['target'], PR)
        self.assertEqual(items[0]['url'], PR)
        self.assertEqual(items[0]['plan']['action'], 'follow')
        self.assertIn('force-push', items[0]['plan']['message'])
        self.assertEqual(unknown, {'github:conflict:o/r#14'})
        self.assertIn('author:@me', query.call_args.args[0])
        self.assertEqual(query.call_args.args[1], dict(after='next'))

    def test_conflicts_outside_the_watched_repositories_are_skipped(self):
        with patch.object(comments, 'graphql', side_effect=[page(node(), node(repo='x/y', number=7))]):
            items, _ = comments.conflicts({'x/y'})
        self.assertEqual([i['id'] for i in items], ['github:conflict:x/y#7:' + SHA])
        with patch.object(manager.brain, 'config', return_value=dict(github_repositories=['x/y'])):
            self.poll(page(node()))
        self.assertEqual(self.pending(), [])

    def test_poll_stores_once_and_drops_conflicts_that_cleared(self):
        self.poll(page(node()), page(node()))
        self.assertEqual(self.pending(), ['github:conflict:o/r#12:' + SHA])
        self.poll(page(node('UNKNOWN')))
        self.assertEqual(len(self.pending()), 1, 'unknown mergeability is never treated as resolved')
        self.poll(page(node('MERGEABLE')))
        self.assertEqual(self.pending(), [])

    def test_unknown_prs_are_requested_on_their_own_and_known_results_are_used(self):
        head = 'd' * 40
        answers = {'repos/o/r/pulls/14': dict(state='open', mergeable=False, mergeable_state='dirty', head=dict(sha=head)),
                   'repos/o/r/pulls/15': dict(state='open', mergeable=None, head=dict(sha=SHA))}
        self.gh.side_effect = lambda path: answers[path]
        with patch.object(comments, 'graphql', return_value=page(node('UNKNOWN', number=14), node('UNKNOWN', number=15))):
            items, unknown = comments.conflicts()
        self.assertEqual(sorted(call.args[0] for call in self.gh.call_args_list), sorted(answers))
        self.assertEqual([i['id'] for i in items], ['github:conflict:o/r#14:' + head])
        self.assertEqual(items[0]['url'], 'https://github.com/o/r/pull/14')
        self.assertEqual(unknown, {'github:conflict:o/r#15'})

    def test_a_probe_that_finds_the_pr_mergeable_clears_its_queued_conflict(self):
        self.poll(page(node()))
        self.gh.return_value = dict(state='open', mergeable=True, mergeable_state='clean', head=dict(sha=SHA))
        self.poll(page(node('UNKNOWN')))
        self.assertEqual(self.pending(), [])

    def test_probes_are_capped_per_poll_and_the_rest_stay_unknown(self):
        nodes = [node('UNKNOWN', number=n) for n in range(100, 100 + comments.CONFLICT_PROBES + 5)]
        with patch.object(comments, 'graphql', return_value=page(*nodes)):
            items, unknown = comments.conflicts()
        self.assertEqual(self.gh.call_count, comments.CONFLICT_PROBES)
        self.assertEqual((items, len(unknown)), ([], len(nodes)))

    def test_a_failed_probe_keeps_the_pr_unknown_without_failing_the_poll(self):
        self.poll(page(node()))
        self.gh.side_effect = comments.GitHubFailure('network')
        self.poll(page(node('UNKNOWN')))
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(self.setting('comments_error_conflicts'), '')

    def test_unrelated_comments_survive_conflict_cleanup(self):
        with self.db() as db:
            db.execute("INSERT INTO manager_comments(id,payload) VALUES ('github:comment:1:now','{}')")
        self.poll(page())
        self.assertEqual(self.pending(), ['github:comment:1:now'])

    def test_failure_is_reported_once_without_details_and_listed_once(self):
        with self.db() as db:
            store.set_setting(db, 'comments_error_github', comments.GitHubFailure('auth').args[0])
        self.poll(comments.GitHubFailure('auth'), comments.GitHubFailure('auth'))
        self.assertEqual(self.setting('comments_error_conflicts'), comments.GitHubFailure.MESSAGES['auth'])
        self.assertEqual(len(manager.state(self.bridge)['events']), 1)
        self.assertEqual(manager.state(self.bridge)['commentErrors'], [comments.GitHubFailure.MESSAGES['auth']])
        self.poll(page())
        self.assertEqual(self.setting('comments_error_conflicts'), '')

    def test_consult_failures_are_listed_with_monitoring_errors(self):
        manager.brain.set_consult_error(self.root, "Meridian's Claude reviews hit a usage limit.")
        self.assertEqual(manager.state(self.bridge)['commentErrors'], ["Meridian's Claude reviews hit a usage limit."])
        manager.brain.set_consult_error(self.root, '')
        self.assertEqual(manager.state(self.bridge)['commentErrors'], [])

    def test_linked_conflict_is_delivered_with_its_pr_without_a_model_call(self):
        self.rows[0]['links'] = [dict(url=PR)]
        self.poll(page(node()))
        self.follow()
        self.consult.assert_not_called()
        self.assertIn(b'PR: ' + PR.encode(), self.typed()[0])
        self.assertNotIn(b'Comment:', self.typed()[0])
        self.assertEqual(self.pending(), [])

    def test_planned_conflicts_deliver_behind_a_backlog_that_cannot_be_classified(self):
        self.rows[0]['links'] = [dict(url=PR)]
        with self.db() as db:
            for n in range(25):
                item = dict(id=f'github:comment:{n}:now', target='https://github.com/o/r/pull/99', url='u', author='a', body='b')
                db.execute('INSERT INTO manager_comments(id,payload) VALUES (?,?)', (item['id'], json.dumps(item)))
        self.poll(page(node()))
        self.consult.return_value = None  # Every classification fails, as when the model is out of usage.
        self.follow()
        self.assertIn(b'PR: ' + PR.encode(), self.typed()[0])
        self.assertNotIn('github:conflict:o/r#12:' + SHA, self.pending())
        self.assertEqual(len(self.pending()), 25)

    def test_new_head_replaces_the_pending_conflict(self):
        self.rows[0]['links'] = [dict(url=PR)]
        self.poll(page(node()), page(node(sha='c' * 40)))
        with patch.object(manager, 'tell', return_value=True) as tell:
            self.follow()
        self.assertEqual(tell.call_count, 1)
        self.assertEqual(self.pending(), [])

    def checkout(self, provider='codex'):
        self.rows[0].update(repository='/checkouts/r', sourceFolder='/checkouts/r', provider=provider)
        remotes = patch.object(comments.links, 'command_output', return_value='upstream\norigin')
        repository = patch.object(comments.links, 'repository', side_effect=lambda folder, remote: 'O/R' if remote == 'origin' else 'other/r')
        for p in (remotes, repository):
            p.start()
            self.addCleanup(p.stop)

    def test_unmatched_conflict_opens_and_starts_a_workstream_from_a_known_checkout(self):
        self.checkout()
        def open_stream(url, folder, provider, brief=None):
            created = self.stream(id='w2', terminals=[self.terminal('w2', 'codex', 'Offline', stream='w2', live=False)])
            created['links'] = [dict(url=url)]
            self.rows.append(created)
            return 'w2'
        self.bridge.open_github_stream = Mock(side_effect=open_stream)
        self.poll(page(node()))
        self.follow()
        self.bridge.open_github_stream.assert_called_once_with(PR, '/checkouts/r', 'codex', brief=comments.CONFLICT_MESSAGE)
        self.assertEqual(self.bridge.launched, ['w2'])
        self.assertIn(('w2', 'comments', 'create'), [(e[0], e[2], e[3]) for e in self.events()])
        self.assertEqual(len(self.pending()), 1, 'delivered once the new agent is idle')
        self.assertEqual(manager.state(self.bridge)['asks'], [])

    def test_second_watch_pass_neither_recreates_nor_prompts_the_new_workstream(self):
        self.checkout()
        snapshot = list(self.rows)
        def open_stream(url, folder, provider, brief=None):
            created = self.stream(id='w2', terminals=[self.terminal('w2', 'codex', 'Offline', stream='w2', live=False)])
            created['links'] = [dict(url=url)]
            self.rows.append(created)
            return 'w2'
        self.bridge.open_github_stream = Mock(side_effect=open_stream)
        with patch.object(comments, 'graphql', return_value=page(node())), patch.object(comments, 'github', return_value=[]), \
                patch.object(comments, 'linear', return_value=[]):
            comments.watch(self.bridge, snapshot, manager.transaction, manager.brain, manager.untrusted, manager._act_one)
        self.bridge.open_github_stream.assert_called_once()
        self.assertEqual([e[3] for e in self.events()].count('create'), 1)
        self.assertEqual(self.typed(), [], 'the new agent is still starting')
        self.assertEqual(len(self.pending()), 1)
        self.rows[-1]['terminals'][0].update(live=True, state='Ready')  # The next poll sees the started agent.
        self.set_activity('w2', 'Ready')
        self.follow()
        self.assertIn(b'PR: ' + PR.encode(), self.typed()[0])
        self.assertEqual(self.pending(), [])

    def test_unmatched_conflict_without_a_checkout_asks_for_a_workstream(self):
        self.poll(page(node()))
        self.follow()
        asks = manager.state(self.bridge)['asks']
        self.assertEqual(len(asks), 1)
        self.assertIn('New workstream needed', asks[0]['summary'])
        self.assertEqual(asks[0]['recommendation'], PR)
        self.assertEqual(self.pending(), [])

    def test_failed_creation_stays_pending_for_retry(self):
        self.checkout()
        self.bridge.open_github_stream = Mock(side_effect=ValueError('fetch failed'))
        self.poll(page(node()))
        self.follow()
        self.assertEqual(len(self.pending()), 1)
        self.follow()
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(manager.state(self.bridge)['asks'], [])
        self.assertEqual([e[3] for e in self.events()].count('unavailable'), 1, 'reported once, then retried quietly')
        self.assertNotIn('fetch failed', json.dumps(self.events()))



if __name__ == '__main__':
    unittest.main()
