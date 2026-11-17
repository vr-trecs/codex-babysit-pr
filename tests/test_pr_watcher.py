import copy
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import pr_watcher as w

THREAD = '00000000-0000-4000-8000-000000000001'
URL = 'https://github.com/owner/repo/pull/12'


def page(busy=False):
    return {'thread': {'id': THREAD, 'kind': 'codex', 'hostId': 'local', 'cwd': '/bound',
                       'status': {'type': 'active' if busy else 'idle', 'activeFlags': []}},
            'turns': [{'status': 'inProgress' if busy else 'completed', 'items': []}], 'page': {}}


def binding():
    return {'status': 'watching', 'url': URL, 'repo': 'owner/repo', 'number': 12,
            'thread': THREAD, 'viewer': 'me', 'cwd': '/bound', 'branch': 'feature',
            'base': 'develop', 'seen': {}, 'pending': {}, 'outbox': None}


def snap():
    return {'events': {'inline:1': 'hash'}, 'head': 'a' * 40, 'branch': 'feature',
            'base': 'develop', 'head_repo': 'owner/repo', 'base_repo': 'owner/repo'}


class FakeDesktop:
    def __init__(self):
        self.page, self.sends, self.fail = page(), [], False
        self.thread_id = THREAD

    def read(self, cursor=None):
        return copy.deepcopy(self.page)

    def call(self, tool, args):
        self.sends.append(args['prompt'])
        if self.fail:
            raise RuntimeError('uncertain')
        self.page['turns'][0]['items'].append({'type': 'userMessage', 'content': [
            {'type': 'text', 'text': args['prompt']}]})
        return {'threadId': THREAD}


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'watch.json'
        self.b, self.s, self.d = binding(), snap(), FakeDesktop()
        self.now = 100
        self.stack = __import__('contextlib').ExitStack()
        self.stack.enter_context(patch.object(w, 'validate_binding'))
        self.stack.enter_context(patch.object(w.time, 'time', side_effect=lambda: self.now))
        self.stack.enter_context(patch.object(w, 'api', side_effect=self.api))

    def tearDown(self):
        self.stack.close()
        self.tmp.cleanup()

    def api(self, gh, route):
        if route == 'user':
            return {'login': 'me'}
        return {'state': 'open', 'head': {'sha': self.s['head']}}

    def tick(self):
        w.tick(self.b, self.path, {'gh': 'gh'}, desktop_factory=lambda *_: self.d,
               snapshot_fn=lambda *_: copy.deepcopy(self.s))

    def deliver(self):
        self.tick()
        self.now += 11
        self.tick()

    def test_unchanged_feedback_sends_once_across_restart(self):
        self.deliver()
        self.b = json.loads(self.path.read_text())
        self.assertTrue(self.d.sends[0].startswith(
            f"[local-pr-watcher:{self.b['last_delivered_id']}]\n"))
        self.now += 60
        self.tick()
        self.assertEqual(len(self.d.sends), 1)

    def test_busy_chat_coalesces_without_wake(self):
        self.d.page = page(True)
        self.deliver()
        self.s['events']['discussion:2'] = 'hash2'
        self.now += 60
        self.tick()
        self.assertFalse(self.d.sends)
        self.d.page = page()
        self.now += 11
        self.tick()
        self.assertEqual(len(self.d.sends), 1)
        self.assertIn('Changed review/CI/conflict objects: 2', self.d.sends[0])

    def test_edit_is_a_new_event(self):
        self.deliver()
        self.s['events']['inline:1'] = 'edited'
        self.now += 60
        self.deliver()
        self.assertEqual(len(self.d.sends), 2)

    def test_uncertain_send_not_retried(self):
        self.d.fail = True
        with self.assertRaises(RuntimeError):
            self.tick()
        self.b = json.loads(self.path.read_text())
        self.now += 60
        self.tick()
        self.assertEqual(len(self.d.sends), 1)
        self.assertEqual(self.b['status'], 'delivery-uncertain')

    def test_receipt_recovers_accepted_send_after_crash(self):
        self.b['outbox'] = {'id': 'batch', 'events': self.s['events'], 'head': self.s['head']}
        self.d.page['turns'][0]['items'] = [{'type': 'userMessage', 'content': [
            {'type': 'text', 'text': '<codex_delegation>[local-pr-watcher:batch]</codex_delegation>'}]}]
        self.tick()
        self.assertIsNone(self.b['outbox'])
        self.assertFalse(self.d.sends)

    def test_tool_output_cannot_fake_a_delivery_receipt(self):
        self.assertFalse(w.receipt({'turns': [{'items': [{'type': 'commandExecution',
                                                        'text': '[local-pr-watcher:batch]'}]}]},
                                   '[local-pr-watcher:batch]'))

    def test_actual_desktop_delegation_is_receipt(self):
        p = {'turns': [{'items': [{'type': 'functionCallOutput', 'namespace': 'codex_app',
             'name': 'send_message_to_thread', 'output': {'text': '<codex_delegation><source_thread_id>' + THREAD +
             '</source_thread_id><input>[local-pr-watcher:batch] wake</input></codex_delegation>'}}]}]}
        self.assertTrue(w.receipt(p, '[local-pr-watcher:batch]', THREAD))
        self.assertFalse(w.receipt(p, '[local-pr-watcher:batch]', 'another-chat'))
        p['turns'][0]['items'][0]['namespace'] = 'github'
        self.assertFalse(w.receipt(p, '[local-pr-watcher:batch]', THREAD))

    def test_merged_pr_never_reads_or_wakes_chat(self):
        self.s = {'terminal': 'merged'}
        self.d.read = lambda *_: self.fail('desktop read for merged PR')
        self.tick()
        self.assertEqual(self.b['status'], 'merged')
        self.assertFalse(self.d.sends)

    def test_stopped_watch_does_no_network(self):
        self.b['status'] = 'stopped'
        with patch.object(w, 'api', side_effect=AssertionError('network')):
            self.tick()

    def test_account_change_blocks_delivery(self):
        with patch.object(w, 'api', return_value={'login': 'other'}):
            with self.assertRaisesRegex(RuntimeError, 'account changed'):
                self.tick()

    def test_head_changed_before_send_defers(self):
        with patch.object(w, 'api', side_effect=lambda gh, r: {'login': 'me'} if r == 'user' else
                          {'state': 'open', 'head': {'sha': 'new'}}):
            self.tick()
        self.assertFalse(self.d.sends)

    def test_conflict_wakes_same_chat_once_and_new_base_wakes_again(self):
        self.s['events'] = {'conflict:head:base': 'conflicting'}
        def conflict_api(gh, route):
            return {'login': 'me'} if route == 'user' else {'state': 'open', 'head': {'sha': self.s['head']}, 'mergeable': False}
        with patch.object(w, 'api', side_effect=conflict_api):
            self.deliver()
            self.tick()
            self.assertEqual(len(self.d.sends), 1)
            self.assertEqual(self.b['seen'], {'conflict:head:base': 'conflicting'})
            self.s['events'] = {'conflict:head:new-base': 'conflicting'}
            self.tick()
            self.assertEqual(len(self.d.sends), 2)

    def test_conflict_cleared_before_send_does_not_wake(self):
        self.s['events'] = {'conflict:head:base': 'conflicting'}
        self.tick()
        self.assertFalse(self.d.sends)

    def test_worktree_mismatch_blocks_delivery(self):
        with patch.object(w, 'validate_binding', side_effect=RuntimeError('mismatch')):
            with self.assertRaises(RuntimeError):
                self.tick()
        self.assertFalse(self.d.sends)

    def test_state_is_private(self):
        self.tick()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_unknown_or_attention_status_is_not_idle(self):
        for status in [{'type': 'unknown'}, {'type': 'idle', 'activeFlags': ['waitingForInput']}]:
            p = page()
            p['thread']['status'] = status
            self.assertFalse(w.idle(p))

    def test_live_binding_validation_checks_branch_and_remote(self):
        with patch.object(w, 'run', side_effect=['feature\n', 'git@github.com:owner/repo.git\n']):
            # Exercise the real function, outside the state-machine mock.
            REAL_VALIDATE(binding(), page(), snap())
        with patch.object(w, 'run', side_effect=['other\n', 'https://github.com/owner/repo.git\n']):
            with self.assertRaises(RuntimeError):
                REAL_VALIDATE(binding(), page(), snap())


REAL_VALIDATE = w.validate_binding


class SnapshotTests(unittest.TestCase):
    def test_pagination_edits_and_author_filtering(self):
        pr = {'state': 'open', 'head': {'sha': 'a', 'ref': 'feature', 'repo': {'full_name': 'owner/repo'}},
              'base': {'ref': 'develop', 'repo': {'full_name': 'owner/repo'}}}
        comments = [{'id': i, 'body': 'text', 'user': {'login': user, 'type': kind},
                     'author_association': association}
                    for i, user, kind, association in [(1, 'me', 'User', 'OWNER'),
                     (2, 'outsider', 'User', 'NONE'), (3, 'bot', 'Bot', 'NONE'),
                     (4, 'colleague', 'User', 'MEMBER')]]
        with patch.object(w, 'ci_failures', return_value={}), patch.object(w, 'api', side_effect=[pr, comments, [], [], pr]):
            result = w.snapshot('gh', URL, 'me')
        self.assertEqual(set(result['events']), {'inline:3', 'inline:4'})
        self.assertNotIn('text', json.dumps(result))

    def test_unstable_snapshot_fails_closed(self):
        a = {'state': 'open', 'head': {'sha': 'a', 'ref': 'f', 'repo': {'full_name': 'o/r'}},
             'base': {'ref': 'develop', 'repo': {'full_name': 'o/r'}}}
        b = copy.deepcopy(a)
        b['head']['sha'] = 'b'
        with patch.object(w, 'ci_failures', return_value={}), patch.object(w, 'api', side_effect=[a, [], [], [], b]):
            with self.assertRaisesRegex(RuntimeError, 'changed during snapshot'):
                w.snapshot('gh', URL, 'me')

    def test_confirmed_conflicts_only_and_base_scoped_identity(self):
        pr = {'state': 'open', 'head': {'sha': 'head', 'ref': 'f', 'repo': {'full_name': 'o/r'}},
              'base': {'sha': 'base', 'ref': 'develop', 'repo': {'full_name': 'o/r'}}}
        for mergeable, state, expected in [(False, 'dirty', True), (None, 'unknown', False),
                                            (True, 'blocked', False), (True, 'clean', False)]:
            current = dict(pr, mergeable=mergeable, mergeable_state=state)
            with patch.object(w, 'ci_failures', return_value={}), patch.object(w, 'api', side_effect=[current, [], [], [], current]):
                result = w.snapshot('gh', URL, 'me')
            self.assertEqual(result['events'], {'conflict:head:base': 'conflicting'} if expected else {})
        newer = copy.deepcopy(pr)
        newer['base']['sha'] = 'new-base'
        with patch.object(w, 'ci_failures', return_value={}), patch.object(w, 'api', side_effect=[pr, [], [], [], newer]):
            with self.assertRaisesRegex(RuntimeError, 'changed during snapshot'):
                w.snapshot('gh', URL, 'me')

    def test_slurp_flattens_every_page(self):
        with patch.object(w, 'run', return_value='[[{"id":1}],[{"id":101}]]'):
            self.assertEqual(w.api('gh', '/route', True), [{'id': 1}, {'id': 101}])

    def test_exact_url_and_canonical_owner_key(self):
        self.assertEqual(w.key(URL), w.key('https://github.com/OWNER/REPO/pull/12/'))
        for url in [URL + '?x=1', URL + '/comments', 'https://evil.test/owner/repo/pull/12']:
            with self.assertRaises(ValueError):
                w.target(url)


class CiTests(unittest.TestCase):
    def test_current_failures_only_and_all_check_pages(self):
        pages = [{'check_runs': [
            {'id': 1, 'head_sha': 'head', 'status': 'completed', 'conclusion': 'failure'},
            {'id': 2, 'head_sha': 'old', 'status': 'completed', 'conclusion': 'failure'},
            {'id': 3, 'head_sha': 'head', 'status': 'in_progress', 'conclusion': None},
            {'id': 8, 'head_sha': 'head', 'status': 'completed', 'conclusion': 'success'}]},
            {'check_runs': [{'id': 4, 'head_sha': 'head', 'status': 'completed', 'conclusion': 'timed_out'}]}]
        statuses = [{'id': 5, 'context': 'build', 'state': 'success'},
                    {'id': 6, 'context': 'build', 'state': 'failure'},
                    {'id': 7, 'context': 'deploy', 'state': 'error'}]
        with patch.object(w, 'run', return_value=json.dumps(pages)), patch.object(w, 'api', return_value=statuses):
            events = w.ci_failures('gh', 'owner/repo', 'head')
        self.assertEqual(set(events), {'ci:head:check:1', 'ci:head:check:4', 'ci:head:status:7'})

    def test_failure_wakes_once_then_new_run_wakes_again(self):
        case = WatchTests()
        case.setUp()
        try:
            case.s['events'] = {'ci:head:check:1': 'failed'}
            case.deliver()
            case.tick()
            self.assertEqual(len(case.d.sends), 1)
            case.s['events'] = {'ci:head:check:2': 'failed'}
            case.tick()
            self.assertEqual(len(case.d.sends), 2)
            self.assertIn('CI failures', case.d.sends[0])
        finally:
            case.tearDown()


class BindingTests(unittest.TestCase):
    def test_attached_checkout_is_validated_against_branch_and_origin(self):
        p = page()
        p['thread']['cwd'] = '/original'
        p['attached_worktrees'] = ['/bound']
        with patch.object(w, 'run', side_effect=['feature', 'https://github.com/owner/repo.git']):
            w.validate_binding(binding(), p, snap())
        for replies in [['other', 'https://github.com/owner/repo.git'],
                        ['feature', 'https://github.com/other/repo.git']]:
            with patch.object(w, 'run', side_effect=replies):
                with self.assertRaisesRegex(RuntimeError, 'branch or origin'):
                    w.validate_binding(binding(), p, snap())
        p['attached_worktrees'] = []
        with self.assertRaisesRegex(RuntimeError, 'worktree'):
            w.validate_binding(binding(), p, snap())

    def test_read_is_bounded_and_uses_only_local_attached_worktrees(self):
        d = w.Desktop({}, THREAD)
        artifacts = {'artifacts': [
            {'attachmentType': 'linked_worktree', 'payload': {'hostId': 'local', 'root': '/bound'}},
            {'attachmentType': 'linked_worktree', 'payload': {'hostId': 'remote', 'root': '/foreign'}},
            {'attachmentType': 'pull_request', 'payload': {'root': '/not-a-worktree'}}]}
        with patch.object(d, 'call', side_effect=[page(), artifacts]) as call:
            p = d.read()
        self.assertEqual(p['attached_worktrees'], ['/bound'])
        self.assertEqual(call.call_args_list[0].args[1]['turnLimit'], 1)
        self.assertTrue(call.call_args_list[0].args[1]['includeOutputs'])
        self.assertEqual(call.call_args_list[1].args[0], 'list_artifacts')


class BridgeTests(unittest.TestCase):
    def test_read_reconnects_but_write_is_attempted_once(self):
        replies = [json.dumps({'error': 'reset'}), json.dumps({'error': 'reset'}),
                   json.dumps({'result': {'ok': True}})]
        with patch.dict(os.environ, {'CODEX_PR_WATCHER_BROKER': '1'}), \
                patch.object(w, 'socket_paths', return_value=['/private']), \
                patch.object(w.sys, 'stdin', io.StringIO('\n'.join(replies))), \
                contextlib.redirect_stdout(io.StringIO()):
            desktop = w.Desktop({}, THREAD)
            self.assertEqual(desktop.call('read_thread'), {'ok': True})
        with patch.dict(os.environ, {'CODEX_PR_WATCHER_BROKER': '1'}), \
                patch.object(w.sys, 'stdin', io.StringIO('\n'.join(replies))) as stdin, \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'uncertain'):
                desktop.call('send_message_to_thread', {'prompt': 'probe'})
            self.assertEqual(stdin.readline().strip(), replies[1])

    def test_real_bridge_handles_fragmented_frames(self):
        node = os.environ.get('CODEX_MCP_NODE_PATH')
        if not node:
            self.skipTest('desktop signed Node runtime unavailable')
        with tempfile.TemporaryDirectory() as directory:
            pipe = str(Path(directory) / 'desktop.sock')
            server = socket.socket(socket.AF_UNIX)
            server.bind(pipe)
            os.chmod(pipe, 0o600)
            server.listen()
            errors = []

            def host():
                try:
                    with server.accept()[0] as connection:
                        for _ in range(2):
                            def read(n):
                                result = b''
                                while len(result) < n:
                                    result += connection.recv(n - len(result))
                                return result
                            # Match the bundled MCP client's persistent native connection.
                            request = json.loads(read(struct.unpack('<I', read(4))[0]))
                            result = {'tools': [{'name': 'read_thread', 'namespace': 'codex_app'}]} if request['method'] == 'tools/list' else {
                                'success': True, 'contentItems': [{'type': 'inputText', 'text': '{"ok":true}'}]}
                            body = json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}).encode()
                            frame = struct.pack('<I', len(body)) + body
                            for chunk in [frame[:2], frame[2:7], frame[7:]]:
                                connection.sendall(chunk)
                except Exception as error:
                    errors.append(error)

            thread = threading.Thread(target=host, daemon=True)
            thread.start()
            try:
                output = w.run([node, str(w.HERE / 'desktop_bridge.mjs')], data=json.dumps({
                    'pipe': pipe, 'threadId': THREAD, 'tool': 'read_thread', 'args': {}}))
                self.assertEqual(json.loads(output), {'ok': True})
            finally:
                server.close()
            thread.join(2)
            self.assertFalse(errors)

    def test_single_flight_is_real_os_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'lock'
            with w.lock(path):
                with self.assertRaises(BlockingIOError):
                    with w.lock(path, blocking=False):
                        self.fail('second owner entered')


if __name__ == '__main__':
    unittest.main()
