"""Audience checks at the registered handler; all data and context are synthetic."""
import argparse
import contextvars
import importlib.util
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

PLUGIN = Path(__file__).resolve().parents[1] / 'memory-rewind'
NAME = 'memory_rewind'
if NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(NAME, PLUGIN / '__init__.py',
                                                 submodule_search_locations=[str(PLUGIN)])
    package = importlib.util.module_from_spec(spec)
    sys.modules[NAME] = package
    spec.loader.exec_module(package)
import memory_rewind as package
from memory_rewind import chat, cli

class AudienceTests(unittest.TestCase):
    def test_group_and_unknown_are_refused_before_history_access(self):
        for platform, chat_type in (('telegram', 'group'), ('telegram', 'channel'),
                                    ('telegram', 'thread'), ('telegram', ''),
                                    ('', ''), ('', 'dm'), ('telegram', 'unknown'),
                                    ('slack', 'dm'), ('matrix', 'dm'), ('discord', 'dm')):
            with self.subTest(platform=platform, chat_type=chat_type):
                with self.context(platform, chat_type), patch.object(
                    package, 'resolve_home', side_effect=AssertionError('history accessed')
                ), patch.object(chat, 'render_history') as render:
                    result = chat.handle_slash('SYNTHETIC-version', Mock())
                    self.assertEqual(result, chat._PRIVATE_HISTORY_NOTICE)
                    render.assert_not_called()

    def test_missing_context_api_is_refused_before_history_access(self):
        with patch.dict(sys.modules, {'gateway.session_context': None}), patch.object(
            package, 'resolve_home', side_effect=AssertionError('history accessed')
        ):
            self.assertEqual(chat.handle_slash('', Mock()), chat._CONTEXT_API_NOTICE)

    def test_old_api_is_refused_before_history_access(self):
        module = types.ModuleType('gateway.session_context')
        module.get_session_env = lambda key, default='': 'telegram' if key.endswith('PLATFORM') else 'dm'
        with patch.dict(sys.modules, {'gateway.session_context': module}), patch.object(
            package, 'resolve_home', side_effect=AssertionError('history accessed')):
            self.assertEqual(chat.handle_slash('', Mock()), chat._CONTEXT_API_NOTICE)

    def test_actual_native_api_refuses_unbound_stale_dm_and_allows_bound_telegram(self):
        root = os.environ.get('RM72_NATIVE_SOURCE')
        if not root:
            self.skipTest('set RM72_NATIVE_SOURCE to the matching native candidate checkout')
        spec = importlib.util.spec_from_file_location('gateway.session_context', Path(root) / 'gateway/session_context.py')
        context = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(context)
        with patch.dict(sys.modules, {'gateway.session_context': context}), patch.dict(
            os.environ, {'HERMES_SESSION_PLATFORM': 'telegram', 'HERMES_SESSION_CHAT_TYPE': 'dm'}):
            def check():
                with patch.object(package, 'resolve_home', side_effect=AssertionError('history accessed')):
                    self.assertEqual(chat.handle_slash('', Mock()), chat._PRIVATE_HISTORY_NOTICE)
                    context.set_session_vars(platform='', chat_type='')
                    self.assertEqual(chat.handle_slash('', Mock()), chat._PRIVATE_HISTORY_NOTICE)
                    # Native Slack's mpim path reports dm; the candidate must still deny it.
                    context.set_session_vars(platform='slack', chat_type='dm')
                    self.assertEqual(chat.handle_slash('', Mock()), chat._PRIVATE_HISTORY_NOTICE)
                context.set_session_vars(platform='telegram', chat_type='dm')
                with patch.object(package, 'resolve_home', return_value=Path('/SYNTHETIC-home')), patch.object(
                    package, 'resolve_data_dir', return_value=Path('/SYNTHETIC-data')), patch.object(
                    package, 'read_options', return_value='SYNTHETIC-options'), patch.object(
                    chat, 'render_history', return_value='SYNTHETIC history') as render:
                    self.assertEqual(chat.handle_slash('', Mock()), 'SYNTHETIC history')
                    render.assert_called_once()
            contextvars.Context().run(check)

    def test_dm_keeps_exact_history_delegate(self):
        with self.context('telegram', 'dm'), patch.object(package, 'resolve_home',
            return_value=Path('/SYNTHETIC-home')), patch.object(package, 'resolve_data_dir',
            return_value=Path('/SYNTHETIC-data')), patch.object(package, 'read_options',
            return_value='SYNTHETIC-options'), patch.object(chat, 'render_history',
            return_value='SYNTHETIC history') as render:
            self.assertEqual(chat.handle_slash('SYNTHETIC-version memory', Mock()),
                             'SYNTHETIC history')
            render.assert_called_once_with('SYNTHETIC-version memory',
                Path('/SYNTHETIC-home'), Path('/SYNTHETIC-data'), 'SYNTHETIC-options')

    def test_registration_preserves_hooks_and_host_cli(self):
        ctx = Mock()
        package.register(ctx)
        self.assertEqual([call.args[0] for call in ctx.register_hook.call_args_list],
            ['on_session_start', 'pre_llm_call', 'post_tool_call', 'on_session_end'])
        self.assertEqual(ctx.register_command.call_args.args[0], 'memory-history')
        registration = ctx.register_cli_command.call_args.kwargs
        self.assertEqual(registration['name'], 'memory-rewind')
        self.assertIs(registration['setup_fn'], cli.build_parser)
        parser = argparse.ArgumentParser()
        registration['setup_fn'](parser)
        for argv in (['log', 'memory'], ['show', 'SYNTHETIC-rev', 'memory'],
                     ['diff', 'SYNTHETIC-rev', 'memory'],
                     ['restore', 'SYNTHETIC-rev', 'memory', '--dry-run']):
            self.assertEqual(parser.parse_args(argv).mrw_command, argv[0])
        with patch.object(package, 'run_cli', create=True):
            # The registration closes over cli.run_cli, so prove it is unmodified by
            # identity through its closure rather than executing a real host command.
            self.assertIn(cli.run_cli, [c.cell_contents for c in
                                       registration['handler_fn'].__closure__])

    @staticmethod
    def context(platform, chat_type):
        module = types.ModuleType('gateway.session_context')
        values = {'HERMES_SESSION_PLATFORM': platform, 'HERMES_SESSION_CHAT_TYPE': chat_type}
        def get_session_env(key, default='', *, allow_env_fallback=True):
            if allow_env_fallback:
                raise AssertionError('plugin must disable environment fallback')
            return values.get(key, default)
        module.get_session_env = get_session_env
        return patch.dict(sys.modules, {'gateway.session_context': module})

if __name__ == '__main__':
    unittest.main()
