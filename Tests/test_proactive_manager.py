"""Proactive messages, action guards, rules and timing, with no model/network calls."""
import json
import time
from unittest.mock import patch
from test_manager_actions import Fixture
import manager
import manager_store as store


class ProactiveTests(Fixture):
    def item(self, **changes):
        return dict(dict(stream='w1', terminal='t1', kind='next', text='Tests pass. Ship it?', newSubject=False,
                         buttons=[dict(label='Later', action='later', terminal='', value='')]), **changes)

    def accept(self, item, trigger='sweep'):
        with self.db() as db:
            manager.accept_messages(db, {'messages': [item]}, self.rows, trigger, 'w1')
            return store.messages(db)

    def messages(self):
        with self.db() as db:
            return store.messages(db)

    def test_migration_is_repeatable_and_keeps_messages(self):
        self.accept(self.item())
        with self.db() as db:
            store.initialize(db)
            store.initialize(db)
            self.assertEqual(len(store.messages(db)), 1)

    def test_later_survives_rewording_and_reopens_only_for_new_subject(self):
        first = self.accept(self.item())[0]
        self.assertTrue(manager.message_action(self.bridge, first['id'], first['revision'], 'later', 0))
        self.assertEqual(self.accept(self.item(text='Everything passed. Release?')), [])
        reopened = self.accept(self.item(text='A different PR is ready.', newSubject=True))[0]
        self.assertEqual(reopened['id'], first['id'])
        self.assertGreater(reopened['revision'], first['revision'])
        self.assertFalse(reopened['seen'])
        self.assertFalse(manager.message_action(self.bridge, first['id'], first['revision'], 'later', 0))

    def test_repeated_identical_message_does_not_reset_read_state(self):
        first = self.accept(self.item())[0]
        manager.message_action(self.bridge, first['id'], first['revision'], 'seen')
        again = self.accept(self.item())[0]
        self.assertTrue(again['seen'])
        self.assertEqual(again['revision'], first['revision'])

    def test_bad_messages_keep_previous_valid_message(self):
        first = self.accept(self.item())
        for item in (None, {}, self.item(stream='ghost'), self.item(kind='other'), self.item(text=7),
                     self.item(terminal='ghost'), self.item(buttons='bad')):
            self.assertEqual(self.accept(item), first)

    def test_unknown_buttons_and_unlinked_urls_are_dropped(self):
        msg = self.accept(self.item(buttons=[dict(label='Erase', action='exec', value='rm -rf /'),
                    dict(label='Open', action='open', value='https://example.com'),
                    dict(label='Tell', action='tell', terminal='ghost', value='go')]))[0]
        self.assertEqual(msg['buttons'], [])

    def test_stop_cannot_publish_reminders_rules_or_other_streams(self):
        for item in (self.item(kind='reminder'), self.item(kind='rule'), self.item(stream='other')):
            self.assertEqual(self.accept(item, 'stop'), [])
        self.assertEqual(len(self.accept(self.item(kind='status'), 'stop')), 1)

    def test_long_message_is_cut_and_logged(self):
        with patch.object(manager.workstream_log, 'record') as log:
            msg = self.accept(self.item(text='a' * 101))[0]
        self.assertEqual(len(msg['text']), 100)
        log.assert_called_once()

    def test_paused_produces_no_messages(self):
        with self.db() as db:
            store.set_setting(db, 'paused', '1')
        self.assertEqual(self.accept(self.item()), [])

    def test_unread_excludes_status_and_read_questions(self):
        self.accept(self.item(kind='status'))
        self.accept(self.item())
        with self.db() as db:
            ask = store.open_ask(db, 'w1', 't1', 'Choose?', [], '')
            self.assertEqual(store.state(db)['unread'], 2)
            db.execute('INSERT INTO manager_ask_reads VALUES (?)', (ask,))
            self.assertEqual(store.state(db)['unread'], 1)

    def test_tell_rechecks_runtime_even_if_snapshot_says_idle(self):
        msg = self.accept(self.item(buttons=[dict(label='Ship', action='tell', terminal='t1', value='Ship it.')]))[0]
        self.set_activity('t1', 'Working')
        self.assertFalse(manager.message_action(self.bridge, msg['id'], msg['revision'], 'tell', 0))
        self.run.assert_not_called()
        self.assertEqual(len(self.messages()), 1)
        self.set_activity('t1', 'Review')
        self.assertTrue(manager.message_action(self.bridge, msg['id'], msg['revision'], 'tell', 0))
        self.assertEqual(self.typed(), [b'\x1b[200~Ship it.\x1b[201~', b'\r'])
        self.assertEqual(self.messages(), [])
        self.assertEqual(self.events()[-1][2:4], ('button', 'tell'))

    def test_rules_require_two_real_prompts_and_save_only_on_click(self):
        project = store.project_key(self.rows[0])
        rule = self.item(kind='rule', rule='Rebuild before finishing.', ruleEvidence=['Rebuild at end', 'Rebuild at end'],
                         buttons=[dict(label='Save', action='save', terminal='', value='')])
        with self.db() as db:
            store.record_prompt(db, project, 't1', 'Rebuild at end')
        self.assertEqual(self.accept(rule), [])
        with self.db() as db:
            store.record_prompt(db, project, 't1', 'Rebuild at end')
        msg = self.accept(rule)[0]
        self.assertEqual(store.project_rules(self.root, project), '')
        self.assertTrue(manager.message_action(self.bridge, msg['id'], msg['revision'], 'save', 0))
        self.assertIn('Rebuild before finishing.', store.project_rules(self.root, project))
        self.assertIn('Rebuild before finishing.', manager.project_context(self.bridge, self.rows))

    def test_prompt_history_is_capped_and_worktrees_share_repository(self):
        first = store.project_key(dict(repository='/repo', folder='/worktree/one'))
        second = store.project_key(dict(repository='/repo', folder='/worktree/two'))
        self.assertEqual(first, second)
        with self.db() as db:
            for i in range(40):
                store.record_prompt(db, first, 't1', str(i) + 'x' * 600)
            rows = db.execute('SELECT text FROM manager_prompts').fetchall()
            self.assertEqual(len(rows), 30)
            self.assertTrue(all(len(r[0]) == 500 for r in rows))
        self.assertNotEqual(store.rules_path(self.root, '/one/repo'), store.rules_path(self.root, '/two/repo'))

    def test_manager_prompts_are_not_used_as_owner_rule_evidence(self):
        with self.db() as db:
            store.note_tell(db, 't1', 'Go.')
        manager.review(self.bridge, 't1', 's', dict(hook_event_name='UserPromptSubmit', prompt='Go.'))
        self.assertEqual(self.query('SELECT * FROM manager_prompts'), [])
        manager.review(self.bridge, 't1', 's', dict(hook_event_name='UserPromptSubmit', prompt='My instruction'))
        self.assertEqual(self.query('SELECT text FROM manager_prompts'), [('My instruction',)])

    def test_reminder_is_once_per_prompt_and_works_when_paused(self):
        self.set_activity('t1', 'Working')
        with self.db() as db:
            store.set_setting(db, 'paused', '1')
            store.note_user_prompt(db, 't1', 'Work', now=time.time() - 301)
        self.assertTrue(manager.planned_reminder(self.bridge, 't1', 's'))
        self.assertFalse(manager.planned_reminder(self.bridge, 't1', 's'))
        manager.review(self.bridge, 't1', 's', dict(hook_event_name='UserPromptSubmit', prompt='More work'))
        self.assertFalse(manager.planned_reminder(self.bridge, 't1', 's'))

    def test_reminder_requires_working_session_and_few_tasks(self):
        with self.db() as db:
            store.note_user_prompt(db, 't1', 'Work', now=time.time() - 301)
        self.assertFalse(manager.planned_reminder(self.bridge, 't1', 's'))
        self.set_activity('t1', 'Working')
        self.assertFalse(manager.planned_reminder(self.bridge, 't1', 'old-session'))
        self.rows[0]['tasks'].append(dict(id='w1.2', status='open'))
        self.assertFalse(manager.planned_reminder(self.bridge, 't1', 's'))

    def test_tool_phrases_use_descriptions_and_basename(self):
        self.assertEqual(store.tool_phrase(dict(tool_name='Bash', tool_input={'description': 'Running tests'})), 'Running tests')
        self.assertEqual(store.tool_phrase(dict(tool_name='Edit', tool_input={'file_path': '/tmp/app.py'})), 'Editing app.py')
        self.assertEqual(store.tool_phrase(dict(tool_name='WebSearch', agent_id='child')), 'subagent: Searching the web')

    def test_pr_transition_is_once_and_later_survives_polling(self):
        url = 'https://github.com/o/r/pull/12'
        self.rows[0]['links'] = [dict(url=url, kind='pr')]
        green = dict(state='OPEN', checks=2, finished=2, failed=0)
        manager.pull_messages(self.bridge, {url: dict(green, finished=1)})
        self.assertEqual(self.messages(), [])
        manager.pull_messages(self.bridge, {url: green})
        msg = self.messages()[0]
        self.assertEqual(msg['text'], 'PR #12 checks passed. Merge it?')
        manager.message_action(self.bridge, msg['id'], msg['revision'], 'later', 2)
        manager.pull_messages(self.bridge, {url: green})
        self.assertEqual(self.messages(), [])
        manager.pull_messages(self.bridge, {url: dict(green, finished=1)})
        manager.pull_messages(self.bridge, {url: green})
        self.assertEqual(len(self.messages()), 1)

    def test_hourly_sweep_does_not_run_while_paused_or_all_done(self):
        with self.db() as db:
            store.set_setting(db, 'sweep_time', str(time.time() - 3601))
        self.assertTrue(manager.state(self.bridge)['sweepDue'])
        self.rows[0]['state'] = 'Done'
        self.assertFalse(manager.state(self.bridge)['sweepDue'])
        self.rows[0]['state'] = 'Working'
        with self.db() as db:
            store.set_setting(db, 'paused', '1')
        self.assertFalse(manager.state(self.bridge)['sweepDue'])

    def test_completed_stream_messages_do_not_leave_an_unread_badge(self):
        self.accept(self.item())
        self.assertEqual(manager.state(self.bridge)['unread'], 1)
        self.rows[0]['state'] = 'Done'
        state = manager.state(self.bridge)
        self.assertEqual(state['messages'], [])
        self.assertEqual(state['unread'], 0)
