"""Settings never return credentials or put them on process command lines."""
from unittest.mock import Mock, patch
from test_manager_actions import Fixture
import app_settings
import manager
import manager_store as store


class SettingsTests(Fixture):
    def test_preferences_persist_and_override_legacy_config(self):
        app_settings.perform(self.bridge, 'preferences', dict(paused=False, watchComments=True))
        self.assertTrue(manager.watch_comments(self.bridge))
        app_settings.perform(self.bridge, 'preferences', dict(paused=True, watchComments=False))
        self.assertFalse(manager.watch_comments(self.bridge))
        with self.db() as db:
            self.assertTrue(store.paused(db))

    def test_invalid_preferences_do_not_change_state(self):
        with self.assertRaises(ValueError):
            app_settings.perform(self.bridge, 'preferences', dict(paused='false', watchComments=True))
        with self.db() as db:
            self.assertIsNone(store.setting(db, 'watch_comments'))

    def test_state_reports_presence_only(self):
        with patch.object(app_settings, 'keychain', return_value=Mock(returncode=0)), patch.dict(app_settings.os.environ, {}, clear=True):
            result = app_settings.state(self.bridge)
        self.assertEqual(set(result), {'paused', 'watchComments', 'linearStored', 'linearEnvironment'})
        self.assertTrue(result['linearStored'])
        self.assertFalse(result['linearEnvironment'])

    def test_save_validates_then_uses_stdin_not_arguments(self):
        key = 'lin_api_TEST_VALUE_ONLY'
        with patch.object(app_settings.comments, 'linear_query') as query, patch.object(app_settings, 'keychain',
                return_value=Mock(returncode=0, stdout=key)):
            self.run.return_value = Mock(returncode=0)
            result = app_settings.perform(self.bridge, 'save-linear', dict(key=key))
        self.assertTrue(result['ok'])
        self.assertEqual(query.call_args.kwargs['token'], key)
        self.assertNotIn(key, str(self.run.call_args.args))
        self.assertIn(key, self.run.call_args.kwargs['input'])
        self.assertNotIn(key, str(result))

    def test_failed_verification_keeps_existing_key(self):
        with patch.object(app_settings.comments, 'linear_query', side_effect=ValueError('secret')):
            with self.assertRaisesRegex(ValueError, 'could not verify') as raised:
                app_settings.perform(self.bridge, 'save-linear', dict(key='lin_api_TEST_VALUE_ONLY'))
        self.run.assert_not_called()
        self.assertNotIn('secret', str(raised.exception))

    def test_key_rejects_security_interactive_command_injection(self):
        for key in ('', 'lin_api_abc\ndelete-keychain', 'lin_api_abc"', 'lin_api_abc def'):
            with self.assertRaises(ValueError):
                app_settings.perform(self.bridge, 'save-linear', dict(key=key))
        self.run.assert_not_called()

    def test_remove_only_targets_saved_linear_item(self):
        with patch.object(app_settings, 'keychain', return_value=Mock(returncode=0)) as keychain:
            result = app_settings.perform(self.bridge, 'remove-linear')
        self.assertTrue(result['ok'])
        keychain.assert_called_once_with('delete-generic-password', '-s', 'local.meridian.linear', '-a', 'comments')

    def test_connection_errors_are_sanitized(self):
        with patch.object(app_settings.comments, 'gh', side_effect=ValueError('secret response')):
            with self.assertRaisesRegex(ValueError, 'GitHub connection failed') as raised:
                app_settings.perform(self.bridge, 'test-github')
        self.assertNotIn('secret', str(raised.exception))
