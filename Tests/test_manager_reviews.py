"""Review-request watching and repository scoping use a fake gh and bridge; nothing reaches GitHub."""
import json
from unittest.mock import Mock, patch
import unittest

from test_manager_actions import Fixture
import manager
import manager_comments as comments
import manager_reviews as reviews
import manager_store as store

URL = 'https://github.com/via-pt/via-monorepo/pull/7'
CONFIG = dict(github_repositories=['via-pt/via-monorepo'],
              review_requests={'via-pt/via-monorepo': '/checkouts/via-monorepo', 'other/repo': '/checkouts/other'})


class GitHub:
    """Answers the gh paths the review watcher reads."""
    def __init__(self):
        self.requested = []
        self.reviews = []
        self.state = 'open'
        self.paths = []
        self.association = 'MEMBER'
        self.head = 'via-pt/via-monorepo'

    def __call__(self, path, paginate=False):
        self.paths.append(path)
        if path == 'user':
            return dict(login='katspaugh')
        if path.startswith('search/issues?'):
            return dict(items=[dict(html_url=url, title='Add payments', author_association=self.association)
                               for url in self.requested])
        if path.endswith('/reviews?per_page=100'):
            return self.reviews
        if path.startswith('repos/'):
            return dict(state=self.state, head=dict(repo=dict(full_name=self.head)))
        raise AssertionError(path)


class ScopeTests(Fixture):
    def test_repositories_reads_configured_slugs(self):
        with patch.object(manager.brain, 'config', return_value=dict(github_repositories=[' Via-PT/via-monorepo ', 3])):
            self.assertEqual(comments.repositories(self.root), {'via-pt/via-monorepo'})
        with patch.object(manager.brain, 'config', return_value={}):
            self.assertIsNone(comments.repositories(self.root))

    def test_github_ignores_links_and_notifications_outside_scope(self):
        paths = []
        def gh(path, paginate=False):
            paths.append(path)
            if path == 'user': return dict(login='me')
            if path.startswith('notifications'):
                return [dict(subject=dict(url='https://api.github.com/repos/other/repo/pulls/1')),
                        dict(subject=dict(url='https://api.github.com/repos/Via-PT/via-monorepo/issues/2'))]
            return []
        rows = [dict(links=[dict(url='https://github.com/katspaugh/meridian/pull/3'), dict(url=URL)])]
        with patch.object(comments, 'gh', side_effect=gh):
            comments.github(rows, '2026-11-25T00:00:00Z', {'via-pt/via-monorepo'})
        read = {p.split('/issues/')[0].split('/pulls/')[0] for p in paths if p.startswith('repos/')}
        self.assertEqual(read, {'repos/via-pt/via-monorepo', 'repos/Via-PT/via-monorepo'})

    def test_poll_passes_the_configured_scope(self):
        with patch.object(manager.brain, 'config', return_value=CONFIG), \
                patch.object(comments, 'github', return_value=[]) as github, patch.object(comments, 'linear', return_value=[]):
            comments.poll(self.bridge, self.rows, manager.transaction)
        self.assertEqual(github.call_args.args[2], {'via-pt/via-monorepo'})


class ReviewTests(Fixture):
    def setUp(self):
        super().setUp()
        self.github = GitHub()
        for target, value in ((comments, 'gh'), ):
            p = patch.object(target, value, side_effect=self.github)
            p.start()
            self.addCleanup(p.stop)
        config = patch.object(manager.brain, 'config', return_value=CONFIG)
        config.start()
        self.addCleanup(config.stop)
        self.bridge.open_github_stream = Mock(return_value='w9')
        self.bridge.bd = Mock(return_value='')

    def poll(self):
        reviews.poll(self.bridge, self.rows, manager.transaction)

    def request(self):
        with self.db() as db:
            return db.execute('SELECT stream,active,owned,closed,requested FROM review_requests WHERE url=?', (URL,)).fetchone()

    def started(self):
        self.poll()  # Baseline.
        self.github.requested = [URL]
        self.poll()
        self.rows.append(self.stream('w9', 'Review', [self.terminal('w9', 'claude', 'Review', stream='w9')]))
        self.rows[-1]['links'] = [dict(url=URL)]

    def test_only_configured_repositories_in_scope_are_searched(self):
        self.poll()
        searches = [p for p in self.github.paths if p.startswith('search/')]
        self.assertEqual(len(searches), 1)
        self.assertIn('repo%3Avia-pt%2Fvia-monorepo', searches[0])
        self.assertIn('user-review-requested%3Akatspaugh', searches[0])

    def test_unconfigured_watcher_reads_nothing(self):
        with patch.object(manager.brain, 'config', return_value={}):
            self.poll()
        self.assertEqual(self.github.paths, [])

    def test_first_poll_records_existing_requests_without_starting_work(self):
        self.github.requested = [URL]
        self.poll()
        self.bridge.open_github_stream.assert_not_called()
        self.assertEqual(self.request()[:4], ('', 1, 0, 1))
        self.poll()
        self.bridge.open_github_stream.assert_not_called()

    def test_new_request_starts_one_review_workstream_with_its_prompt(self):
        self.poll()
        self.github.requested = [URL]
        self.poll()
        self.poll()
        self.bridge.open_github_stream.assert_called_once_with(URL, '/checkouts/via-monorepo', 'claude')
        self.assertEqual(self.bridge.launched, ['w9'])
        self.assertEqual(self.request()[:4], ('w9', 1, 1, 0))
        with self.db() as db:
            prompt = reviews.initial_prompt(db, 'w9')
        self.assertIn(URL, prompt)
        self.assertIn('COMMENT', prompt)
        self.assertIn('Never approve', prompt)
        self.assertNotIn('Add payments', prompt)
        with self.db() as db:
            self.assertTrue(reviews.review_stream(db, 'w9'))
            self.assertFalse(reviews.review_stream(db, 'w1'))

    def test_forks_and_outside_authors_get_a_question_instead_of_an_agent(self):
        for association, head in (('CONTRIBUTOR', 'via-pt/via-monorepo'), ('MEMBER', 'someone/via-monorepo')):
            with self.subTest(association=association, head=head):
                with self.db() as db:
                    db.execute('DELETE FROM review_requests')
                    db.execute('DELETE FROM manager_asks')
                self.github.requested, self.github.association, self.github.head = [], association, head
                self.poll()
                self.github.requested = [URL]
                self.poll()
                self.poll()
                self.bridge.open_github_stream.assert_not_called()
                asks = self.query("SELECT summary,recommendation FROM manager_asks WHERE status='open'")
                self.assertEqual(len(asks), 1)
                self.assertIn('fork or an outside author', asks[0][0])
                self.assertEqual(asks[0][1], URL)

    def test_later_untrusted_request_keeps_the_review_checkout_isolated(self):
        self.started()
        self.github.requested = []
        self.poll()
        self.github.requested, self.github.association = [URL], 'CONTRIBUTOR'
        self.poll()
        self.assertEqual(self.request()[:4], ('w9', 1, 1, 1))
        with self.db() as db:
            self.assertTrue(reviews.review_stream(db, 'w9'))
            db.execute('DELETE FROM review_requests')
            self.assertTrue(reviews.review_stream(db, 'w9'))

    def test_failed_start_is_retried(self):
        self.poll()
        self.github.requested = [URL]
        self.bridge.open_github_stream.side_effect = [ValueError('no checkout'), 'w9']
        self.poll()
        self.assertIn('could not be started', self.setting('comments_error_reviews'))
        self.poll()
        self.assertEqual(self.bridge.open_github_stream.call_count, 2)
        self.assertEqual(self.setting('comments_error_reviews'), '')

    def test_closes_its_workstream_after_my_review_lands(self):
        self.started()
        self.poll()
        self.bridge.bd.assert_not_called()
        requested = self.request()[4]
        self.github.reviews = [dict(user=dict(login='katspaugh'), state='PENDING', submitted_at=None),
                               dict(user=dict(login='someone'), state='COMMENTED', submitted_at='2099-01-01T00:00:00Z')]
        self.poll()
        self.bridge.bd.assert_not_called()
        self.github.reviews.append(dict(user=dict(login='KatsPaugh'), state='COMMENTED', submitted_at=requested))
        self.github.requested = []
        self.rows[-1]['state'] = 'Working'
        self.poll()
        self.bridge.bd.assert_not_called()
        self.rows[-1]['state'] = 'Review'
        self.poll()
        self.bridge.bd.assert_called_once_with('close', 'w9', '--force', '--reason', 'Review submitted on GitHub.')
        self.assertEqual(self.request()[1:4], (0, 1, 1))
        self.poll()
        self.bridge.bd.assert_called_once()

    def test_reviews_before_the_request_do_not_close(self):
        self.started()
        self.github.reviews = [dict(user=dict(login='katspaugh'), state='COMMENTED', submitted_at='2000-01-01T00:00:00Z')]
        self.poll()
        self.bridge.bd.assert_not_called()

    def test_closed_pull_request_closes_its_workstream(self):
        self.started()
        self.github.state = 'closed'
        self.poll()
        self.bridge.bd.assert_called_once_with('close', 'w9', '--force', '--reason',
                                               'The pull request was closed before a review was submitted.')

    def test_repeat_request_reuses_the_review_workstream_through_the_inbox(self):
        self.started()
        self.github.requested = []
        self.rows[-1]['state'] = 'Done'
        self.poll()
        self.assertEqual(self.request()[3], 1)  # Closed by the user.
        self.github.requested = [URL]
        self.poll()
        self.assertEqual(self.bridge.open_github_stream.call_count, 1)
        self.assertEqual(self.request()[:4], ('w9', 1, 1, 0))
        with self.db() as db:
            key, payload, plan = db.execute('SELECT id,payload,plan FROM manager_comments').fetchone()
        self.assertTrue(key.startswith('github:review-request:via-pt/via-monorepo#7:'))
        self.assertEqual(json.loads(plan)['stream'], 'w9')
        self.assertIn('refs/pull/7/head', json.loads(plan)['message'])
        self.bridge.bd.assert_not_called()  # The pre-reopen snapshot must not close it again.

    def test_does_not_close_a_workstream_the_user_opened(self):
        self.poll()
        self.rows[0]['links'] = [dict(url=URL)]
        self.github.requested = [URL]
        self.poll()
        self.bridge.open_github_stream.assert_not_called()
        self.assertEqual(self.request()[:4], ('w1', 1, 0, 0))
        self.rows[0]['state'] = 'Review'
        self.github.state = 'closed'
        self.poll()
        self.bridge.bd.assert_not_called()

    def test_other_pull_request_comments_never_reach_the_review_workstream(self):
        self.started()
        comment = dict(id='github:comment:5:now', target=URL, url=URL + '#c5', author='author', body='Thanks, fixed.')
        with self.db() as db:
            db.execute('INSERT INTO manager_comments (id,payload) VALUES (?,?)', (comment['id'], json.dumps(comment)))
        self.rows[-1]['state'] = 'Done'
        comments.follow_up(self.bridge, self.rows, manager.transaction, manager.brain, manager.untrusted, manager._act_one)
        self.consult.assert_not_called()
        self.bridge.bd.assert_not_called()
        self.assertEqual(self.typed(), [])
        with self.db() as db:
            self.assertEqual(db.execute('SELECT done FROM manager_comments').fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM manager_asks WHERE status='open'").fetchone()[0], 0)

    def test_errors_surface_in_manager_state(self):
        self.github.requested = None  # Makes the search answer malformed.
        with patch.object(comments, 'gh', side_effect=comments.GitHubFailure('rate-limit')):
            self.poll()
        self.assertIn('rate limit', manager.state(self.bridge)['commentErrors'][0])


if __name__ == '__main__':
    unittest.main()
