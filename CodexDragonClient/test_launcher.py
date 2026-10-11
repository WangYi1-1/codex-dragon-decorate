"""Lifecycle and launcher regression checks; the live test owns every process it closes."""
import ctypes
from ctypes import wintypes
import base64
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import launcher
import win_lifecycle as lifecycle


class FakeKernel:
    def __init__(self, in_job=False, flags=0, check_ok=True, query_ok=True):
        self.in_job = in_job
        self.flags = flags
        self.check_ok = check_ok
        self.query_ok = query_ok
        self.queries = 0

    def GetCurrentProcess(self):
        return -1

    def IsProcessInJob(self, handle, job, output):
        ctypes.cast(output, ctypes.POINTER(wintypes.BOOL)).contents.value = self.in_job
        return self.check_ok

    def QueryInformationJobObject(self, job, info_class, output, size, returned):
        self.queries += 1
        ctypes.cast(output, ctypes.POINTER(lifecycle.JobBasicLimits)).contents.LimitFlags = self.flags
        return self.query_ok


class LifecycleTests(unittest.TestCase):
    def flags(self, kernel):
        with patch.object(lifecycle, '_kernel32', return_value=kernel):
            return lifecycle.background_creation_flags()

    def test_normal_desktop_launch_without_job(self):
        kernel = FakeKernel()
        self.assertEqual(self.flags(kernel), lifecycle.CREATE_NO_WINDOW)
        self.assertEqual(kernel.queries, 0)

    def test_explicit_breakaway_when_host_allows_it(self):
        self.assertEqual(self.flags(FakeKernel(True, 0x2800)),
                         lifecycle.CREATE_NO_WINDOW | lifecycle.CREATE_BREAKAWAY_FROM_JOB)

    def test_silent_breakaway_uses_job_supported_behavior(self):
        self.assertEqual(self.flags(FakeKernel(True, 0x3000)), lifecycle.CREATE_NO_WINDOW)

    def test_explicit_breakaway_preferred_when_both_are_allowed(self):
        self.assertEqual(self.flags(FakeKernel(True, 0x3800)),
                         lifecycle.CREATE_NO_WINDOW | lifecycle.CREATE_BREAKAWAY_FROM_JOB)

    def test_prohibited_breakaway_never_starts_a_child(self):
        with patch.object(lifecycle, '_kernel32', return_value=FakeKernel(True, 0x2000)), \
             patch.object(lifecycle.subprocess, 'Popen') as popen:
            with self.assertRaisesRegex(lifecycle.LifecycleError, '不允许后台独立运行'):
                lifecycle.start_background(['fixture'])
            popen.assert_not_called()

    def test_failed_job_membership_query_is_explicit_failure(self):
        with self.assertRaisesRegex(lifecycle.LifecycleError, '无法确认'):
            self.flags(FakeKernel(check_ok=False))

    def test_failed_limit_query_is_explicit_failure(self):
        with self.assertRaisesRegex(lifecycle.LifecycleError, '无法读取'):
            self.flags(FakeKernel(True, query_ok=False))

    def test_spawn_uses_flags_and_preserves_streams_without_a_shell(self):
        child = Mock()
        with patch.object(lifecycle, 'background_creation_flags', return_value=0x09000000), \
             patch.object(lifecycle.subprocess, 'Popen', return_value=child) as popen, \
             patch.object(lifecycle, '_verify_independent') as verify:
            result = lifecycle.start_background(['fixture', 'server.py'], cwd='fixture-dir',
                                                stdin=subprocess.DEVNULL, stdout='log', stderr='log')
        self.assertIs(result, child)
        popen.assert_called_once_with(['fixture', 'server.py'], creationflags=0x09000000,
                                      cwd='fixture-dir', stdin=subprocess.DEVNULL, stdout='log', stderr='log')
        verify.assert_called_once_with(child)

    def test_creation_failure_never_retries_with_weaker_flags(self):
        with patch.object(lifecycle, 'background_creation_flags', return_value=0x09000000), \
             patch.object(lifecycle.subprocess, 'Popen', side_effect=OSError(5, 'denied')) as popen:
            with self.assertRaisesRegex(lifecycle.LifecycleError, '独立后台未能启动'):
                lifecycle.start_background(['fixture'])
        self.assertEqual(popen.call_count, 1)

    def test_still_nested_job_is_rejected_and_only_own_child_stopped(self):
        child = Mock(_handle=7)
        child.poll.return_value = None
        with patch.object(lifecycle, 'background_creation_flags', return_value=0x09000000), \
             patch.object(lifecycle.subprocess, 'Popen', return_value=child), \
             patch.object(lifecycle, '_kernel32', return_value=FakeKernel(True)):
            with self.assertRaisesRegex(lifecycle.LifecycleError, '后台仍受'):
                lifecycle.start_background(['fixture'])
        child.terminate.assert_called_once_with()
        child.wait.assert_called_once_with(timeout=5)
        child.kill.assert_not_called()

    def test_independence_check_failure_does_not_leak_child(self):
        child = Mock()
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired('fixture', 5), 0]
        with patch.object(lifecycle, 'background_creation_flags', return_value=0x09000000), \
             patch.object(lifecycle.subprocess, 'Popen', return_value=child), \
             patch.object(lifecycle, '_verify_independent', side_effect=lifecycle.LifecycleError('check failed')):
            with self.assertRaisesRegex(lifecycle.LifecycleError, 'check failed'):
                lifecycle.start_background(['fixture'])
        child.terminate.assert_called_once_with()
        child.kill.assert_called_once_with()

    def test_nested_job_fallback_stops_only_direct_child_before_broker(self):
        child, broker = Mock(), Mock()
        child.poll.return_value = None
        events = []
        child.terminate.side_effect = lambda: events.append('stop-direct')
        with patch.object(lifecycle, 'background_creation_flags', return_value=0x09000000), \
             patch.object(lifecycle.subprocess, 'Popen', return_value=child), \
             patch.object(lifecycle, '_verify_independent', side_effect=lifecycle.LifecycleError('nested')), \
             patch.object(lifecycle, '_start_broker_background', side_effect=lambda *a, **k: (events.append('broker'), broker)[1]) as start:
            self.assertIs(lifecycle.start_background(['owned-command']), broker)
        self.assertEqual(events, ['stop-direct', 'broker'])
        start.assert_called_once()
        child.wait.assert_called_once_with(timeout=5)


class FakeBrokerKernel(FakeKernel):
    def __init__(self, image, in_job=False):
        super().__init__(in_job=in_job)
        self.image, self.stopped, self.closed = str(image), [], []

    def OpenProcess(self, permissions, inherit, pid):
        return 77

    def QueryFullProcessImageNameW(self, handle, flags, buffer, size):
        buffer.value = self.image
        return True

    def WaitForSingleObject(self, handle, duration):
        return 0 if self.stopped else 258

    def GetExitCodeProcess(self, handle, output):
        ctypes.cast(output, ctypes.POINTER(wintypes.DWORD)).contents.value = 1
        return True

    def TerminateProcess(self, handle, code):
        self.stopped.append(handle)
        return True

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return True


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'client'
        self.root.mkdir()
        (self.root / '.runtime').mkdir()
        (self.root / 'server.py').write_text('# owned fixture', encoding='utf-8')
        self.python = Path(sys.executable).with_name('python.exe').resolve()
        self.command = [str(self.python), str(self.root / 'server.py'), '--port', '8741', '--cwd', str(self.root.parent)]
        self.log = open(self.root / '.runtime/bridge.log', 'ab')
        self.addCleanup(self.log.close)
        self.options = dict(cwd=self.root, stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log)
        self.nonce = 'b' * 32
        self.kernel = FakeBrokerKernel(self.python)
        self.addCleanup(patch.stopall)
        patch.object(lifecycle, 'ROOT', self.root).start()
        patch.object(lifecycle.uuid, 'uuid4', return_value=Mock(hex=self.nonce)).start()
        patch.object(lifecycle, '_kernel32', return_value=self.kernel).start()

    def reply(self, path, nonce, pid, process=None):
        return {'nonce': nonce, 'pid': pid, 'script': str(self.root / 'server.py')}

    def test_owned_broker_requires_correct_python_script_port_streams_and_paths(self):
        self.assertEqual(lifecycle._broker_options(self.command, self.options)['log'], str(self.log.name))
        for command, options in (
            ([str(self.python), '-c', 'arbitrary code'], self.options),
            (self.command[:2] + ['--port', '1234', '--cwd', str(self.root.parent)], self.options),
            ([str(self.python), str(self.root.parent / 'other.py')] + self.command[2:], self.options),
            (self.command, {**self.options, 'env': {'PRIVATE': 'value'}}),
            (self.command, {**self.options, 'stdin': subprocess.PIPE}),
            (self.command, {**self.options, 'stderr': subprocess.PIPE}),
            (self.command, {**self.options, 'cwd': self.root.parent}),
        ):
            with self.subTest(command=command, options=options), self.assertRaises(lifecycle.LifecycleError):
                lifecycle._broker_options(command, options)

    def test_successful_broker_verifies_before_go_then_removes_handshake_files(self):
        checks = []
        def verify(process):
            self.assertEqual(process.pid, 123)
            self.assertFalse((self.root / '.runtime' / ('spawn-' + self.nonce) / 'go.json').exists())
            checks.append('independent')
        def read(path, *args):
            checks.append(path.name)
            if path.name == 'started.json':
                grant = json.loads((path.parent / 'go.json').read_text(encoding='utf-8'))
                self.assertEqual(grant, {'nonce': self.nonce, 'pid': 123})
            return self.reply(path, *args)
        with patch.object(lifecycle, '_wmi_create', return_value=123) as spawn, \
             patch.object(lifecycle, '_read_handshake', side_effect=read), \
             patch.object(lifecycle, '_verify_independent', side_effect=verify):
            process = lifecycle._start_broker_background(self.command, lifecycle.LifecycleError('direct failed'), **self.options)
        self.assertEqual(checks, ['ready.json', 'independent', 'started.json'])
        self.assertEqual(spawn.call_args.args[0][:3], [str(self.python), str(self.root / 'win_lifecycle.py'), '--wmi-child'])
        self.assertEqual(process.pid, 123)
        self.assertIsNone(process.poll())
        self.assertEqual(self.kernel.stopped, [])
        self.assertFalse((self.root / '.runtime' / ('spawn-' + self.nonce)).exists())

    def test_still_nested_broker_is_stopped_without_starting_server(self):
        self.kernel.in_job = True
        with patch.object(lifecycle, '_wmi_create', return_value=123), \
             patch.object(lifecycle, '_read_handshake', side_effect=self.reply) as read:
            with self.assertRaisesRegex(lifecycle.LifecycleError, '后台仍受'):
                lifecycle._start_broker_background(self.command, lifecycle.LifecycleError('direct failed'), **self.options)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(self.kernel.stopped, [77])
        self.assertFalse((self.root / '.runtime' / ('spawn-' + self.nonce)).exists())

    def test_busy_handshake_cleanup_does_not_override_successful_spawn(self):
        with patch.object(lifecycle, '_wmi_create', return_value=123), \
             patch.object(lifecycle, '_read_handshake', side_effect=self.reply), \
             patch.object(Path, 'unlink', side_effect=PermissionError('Windows file busy')) as unlink, \
             patch.object(Path, 'rmdir', side_effect=OSError('Windows directory busy')) as rmdir, \
             patch.object(lifecycle.time, 'sleep') as sleep:
            process = lifecycle._start_broker_background(self.command, lifecycle.LifecycleError('direct failed'), **self.options)
        self.assertEqual(process.pid, 123)
        self.assertIsNone(process.poll())
        self.assertEqual(self.kernel.stopped, [])
        self.assertEqual(unlink.call_count, 12)
        self.assertEqual(rmdir.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_busy_handshake_cleanup_preserves_original_verification_error(self):
        with patch.object(lifecycle, '_wmi_create', return_value=123), \
             patch.object(lifecycle, '_read_handshake', side_effect=self.reply), \
             patch.object(lifecycle, '_verify_independent', side_effect=lifecycle.LifecycleError('original verification failure')), \
             patch.object(Path, 'unlink', side_effect=OSError('cleanup failure')), \
             patch.object(Path, 'rmdir', side_effect=PermissionError('directory busy')), \
             patch.object(lifecycle.time, 'sleep'):
            with self.assertRaisesRegex(lifecycle.LifecycleError, 'original verification failure'):
                lifecycle._start_broker_background(self.command, lifecycle.LifecycleError('direct failed'), **self.options)
        self.assertEqual(self.kernel.stopped, [77])

    def test_handshake_cleanup_refuses_outside_or_unrecognized_directory(self):
        with patch.object(Path, 'unlink') as unlink, patch.object(Path, 'rmdir') as rmdir:
            lifecycle._cleanup_handshake(self.root.parent / ('spawn-' + self.nonce))
            lifecycle._cleanup_handshake(self.root / '.runtime' / 'other-files')
        unlink.assert_not_called()
        rmdir.assert_not_called()

    def test_handshake_cleanup_preserves_unrecognized_files(self):
        directory = self.root / '.runtime' / ('spawn-' + self.nonce)
        directory.mkdir()
        unknown = directory / 'user-note.txt'
        unknown.write_text('keep me', encoding='utf-8')
        (directory / 'config.json').write_text('{}', encoding='utf-8')
        with patch.object(lifecycle.time, 'sleep'):
            lifecycle._cleanup_handshake(directory)
        self.assertEqual(unknown.read_text(encoding='utf-8'), 'keep me')
        self.assertFalse((directory / 'config.json').exists())

    def test_wrong_image_is_not_killed_or_given_go_permission(self):
        self.kernel.image = str(self.root / 'foreign.exe')
        with patch.object(lifecycle, '_wmi_create', return_value=123), \
             patch.object(lifecycle, '_read_handshake', side_effect=self.reply) as read:
            with self.assertRaisesRegex(lifecycle.LifecycleError, '程序路径不匹配'):
                lifecycle._start_broker_background(self.command, lifecycle.LifecycleError('direct failed'), **self.options)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(self.kernel.stopped, [])
        self.assertIn(77, self.kernel.closed)

    def test_handshake_rejects_mismatched_nonce_or_pid(self):
        path = self.root / 'handshake.json'
        for reply in ({'nonce': 'foreign', 'pid': 123}, {'nonce': self.nonce, 'pid': 124}, {'nonce': self.nonce, 'pid': 123, 'error': 'blocked'}):
            path.write_text(json.dumps(reply), encoding='utf-8')
            with self.subTest(reply=reply), self.assertRaisesRegex(lifecycle.LifecycleError, '身份校验失败'):
                lifecycle._read_handshake(path, self.nonce, 123)

    def test_wmi_invocation_is_hidden_encoded_data_and_not_a_shell(self):
        reply = Mock(returncode=0, stdout='{"returnValue":0,"pid":123}')
        with patch.object(lifecycle.subprocess, 'run', return_value=reply) as run:
            self.assertEqual(lifecycle._wmi_create(self.command, self.root), 123)
        args, options = run.call_args.args[0], run.call_args.kwargs
        self.assertIn('-EncodedCommand', args)
        self.assertEqual(options['creationflags'], lifecycle.CREATE_NO_WINDOW)
        self.assertEqual(options['encoding'], 'utf-8')
        self.assertEqual(options['errors'], 'replace')
        self.assertNotIn('shell', options)
        script = base64.b64decode(args[-1]).decode('utf-16le')
        self.assertIn('$s.ShowWindow=0', script)
        self.assertIn('$s.CreateFlags=150994944', script)
        self.assertIn("([wmiclass]'Win32_Process').Create", script)
        self.assertNotIn('server.py', script)

    def test_non_utf8_powershell_diagnostic_does_not_break_ascii_pid_json(self):
        run_process = subprocess.run
        def emit_native_diagnostic(command, **options):
            # A real pipe reader receives CP936-like bytes while the parent
            # uses UTF-8 mode; no WMI process or client service is created.
            options.setdefault('encoding', 'utf-8')
            fixture = 'import os;os.write(2,bytes([0xd5,0xb9]));os.write(1,b\'{"returnValue":0,"pid":123}\')'
            return run_process([sys.executable, '-c', fixture], **options)
        with patch.object(lifecycle.subprocess, 'run', side_effect=emit_native_diagnostic):
            self.assertEqual(lifecycle._wmi_create(self.command, self.root), 123)

    def test_poll_and_wait_fail_if_exact_handle_cannot_be_queried(self):
        process = lifecycle.BrokerProcess(123, 77, self.kernel)
        with patch.object(self.kernel, 'WaitForSingleObject', return_value=0xFFFFFFFF):
            with self.assertRaises(lifecycle.LifecycleError):
                process.poll()
            with self.assertRaises(lifecycle.LifecycleError):
                process.wait(1)


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'client'
        self.root.mkdir()
        self.addCleanup(patch.stopall)
        patch.object(launcher, 'ROOT', self.root).start()
        patch.object(launcher, 'RUNTIME', self.root / '.runtime').start()

    def test_foreign_port_is_rejected(self):
        with patch.object(launcher, 'status', return_value={'appId': 'other'}):
            with self.assertRaisesRegex(RuntimeError, '其他应用'):
                launcher.owned_status()

    def test_existing_connected_service_is_reused(self):
        with patch.object(launcher, 'owned_status', return_value={'connected': True}), \
             patch.object(launcher, 'start_background') as spawn, patch.object(launcher, 'reconnect') as reconnect:
            launcher.start(no_window=True)
        spawn.assert_not_called()
        reconnect.assert_not_called()

    def test_fresh_server_uses_independent_helper_and_no_window(self):
        child = Mock(pid=123)
        with patch.object(launcher, 'owned_status', return_value=None), \
             patch.object(launcher, 'start_background', return_value=child) as spawn, \
             patch.object(launcher, 'wait_ready', return_value={'connected': True}) as wait, \
             patch.object(launcher.subprocess, 'Popen') as popen:
            launcher.start(no_window=True)
        wait.assert_called_once_with(child)
        self.assertEqual(spawn.call_args.args[0][1], str(self.root / 'server.py'))
        self.assertNotIn('creationflags', spawn.call_args.kwargs)
        self.assertEqual(spawn.call_args.kwargs['stdin'], subprocess.DEVNULL)
        self.assertEqual(json.loads((self.root / '.runtime/server-process.json').read_text())['pid'], 123)
        popen.assert_not_called()

    def test_failed_independent_spawn_never_records_pid_or_opens_window(self):
        with patch.object(launcher, 'owned_status', return_value=None), \
             patch.object(launcher, 'start_background', side_effect=lifecycle.LifecycleError('independence failed')), \
             patch.object(launcher.subprocess, 'Popen') as popen:
            with self.assertRaisesRegex(lifecycle.LifecycleError, 'independence failed'):
                launcher.start()
        self.assertFalse((self.root / '.runtime/server-process.json').exists())
        popen.assert_not_called()

    def test_disconnected_owned_service_is_reconnected_before_window(self):
        with patch.object(launcher, 'owned_status', return_value={'connected': False}), \
             patch.object(launcher, 'reconnect') as reconnect, \
             patch.object(launcher, 'wait_ready', return_value={'connected': True}) as wait, \
             patch.object(launcher, 'start_background') as spawn:
            launcher.start(no_window=True)
        reconnect.assert_called_once_with()
        wait.assert_called_once_with()
        spawn.assert_not_called()

    def test_reconnect_failure_does_not_open_browser(self):
        with patch.object(launcher, 'owned_status', return_value={'connected': False}), \
             patch.object(launcher, 'reconnect'), \
             patch.object(launcher, 'wait_ready', side_effect=RuntimeError('backend not ready')), \
             patch.object(launcher.subprocess, 'Popen') as popen:
            with self.assertRaisesRegex(RuntimeError, 'backend not ready'):
                launcher.start()
        popen.assert_not_called()

    def test_reconnect_posts_same_origin_empty_json(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(launcher.urllib.request, 'urlopen', return_value=response) as urlopen, \
             patch.object(launcher.json, 'load', return_value={'result': {'connected': False}}):
            launcher.reconnect()
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, launcher.URL + 'api/reconnect')
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(request.data, b'{}')
        self.assertEqual(request.get_header('Origin'), launcher.URL.rstrip('/'))
        self.assertEqual(request.get_header('Content-type'), 'application/json')

    def test_reconnect_server_error_is_explicit(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(launcher.urllib.request, 'urlopen', return_value=response), \
             patch.object(launcher.json, 'load', return_value={'error': {'message': 'restart failed'}}):
            with self.assertRaisesRegex(RuntimeError, 'restart failed'):
                launcher.reconnect()

    def test_wait_allows_initial_error_until_it_recovers(self):
        with patch.object(launcher.time, 'monotonic', side_effect=[0, 1, 2]), \
             patch.object(launcher.time, 'sleep'), \
             patch.object(launcher, 'owned_status', side_effect=[{'connected': False, 'error': 'old'}, {'connected': True}]):
            self.assertTrue(launcher.wait_ready()['connected'])

    def test_wait_deadline_error_is_not_ready(self):
        with patch.object(launcher.time, 'monotonic', side_effect=[0, 1, 46]), \
             patch.object(launcher.time, 'sleep'), \
             patch.object(launcher, 'owned_status', return_value={'connected': False, 'error': 'still offline'}):
            with self.assertRaisesRegex(RuntimeError, 'still offline'):
                launcher.wait_ready()

    def test_wait_missing_service_is_not_ready(self):
        with patch.object(launcher.time, 'monotonic', side_effect=[0, 46]):
            with self.assertRaisesRegex(RuntimeError, '尚未连接成功'):
                launcher.wait_ready()

    def test_wait_child_exit_is_explicit(self):
        child = Mock()
        child.poll.return_value = 1
        with patch.object(launcher.time, 'monotonic', side_effect=[0, 1]), \
             patch.object(launcher, 'owned_status', return_value=None):
            with self.assertRaisesRegex(RuntimeError, '启动失败'):
                launcher.wait_ready(child)


@unittest.skipUnless(os.name == 'nt', 'Windows lifecycle integration')
class LiveJobTests(unittest.TestCase):
    def test_own_job_close_kills_control_but_keeps_independent_child(self):
        kernel = lifecycle._kernel32()
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ('ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
                         'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', lifecycle.JobBasicLimits), ('IoInfo', IoCounters),
                        ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                        ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]

        job = kernel.CreateJobObjectW(None, None)
        self.assertTrue(job)
        controller = None
        handles = []
        try:
            limits = ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = 0x2000 | 0x0800
            self.assertTrue(kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
            code = (
                'import json,subprocess,sys,time;from win_lifecycle import start_background;'
                'sys.stdin.readline();'
                'command=[sys.executable,"-c","import time;time.sleep(30)"];'
                'options=dict(stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);'
                'control=subprocess.Popen(command,creationflags=subprocess.CREATE_NO_WINDOW,**options);'
                'independent=start_background(command,**options);'
                'print(json.dumps({"control":control.pid,"independent":independent.pid}),flush=True);'
                'time.sleep(30)'
            )
            try:
                controller = lifecycle.start_background([sys.executable, '-c', code],
                    cwd=Path(__file__).parent, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, close_fds=True)
            except lifecycle.LifecycleError as exc:
                if '后台仍受' in str(exc) or '不允许后台独立运行' in str(exc):
                    self.skipTest('This test host cannot release its nested Windows Job; run this integration test from a desktop terminal.')
                raise
            self.assertTrue(kernel.AssignProcessToJobObject(job, wintypes.HANDLE(int(controller._handle))))
            controller.stdin.write('start\n')
            controller.stdin.flush()
            lines = queue.Queue()
            threading.Thread(target=lambda: lines.put(controller.stdout.readline()), daemon=True).start()
            payload = json.loads(lines.get(timeout=8))
            for key in ('control', 'independent'):
                handle = kernel.OpenProcess(0x100000 | 0x1000 | 0x1, False, payload[key])
                self.assertTrue(handle)
                handles.append(handle)
            self.assertTrue(lifecycle._in_job(kernel, handles[0]))
            self.assertFalse(lifecycle._in_job(kernel, handles[1]))
            self.assertTrue(kernel.CloseHandle(job))
            job = None
            self.assertEqual(kernel.WaitForSingleObject(handles[0], 5000), 0)
            self.assertEqual(kernel.WaitForSingleObject(handles[1], 250), 258)
            self.assertIsNotNone(controller.wait(timeout=5))
        finally:
            if job:
                kernel.CloseHandle(job)
            for handle in handles:
                if kernel.WaitForSingleObject(handle, 0) == 258:
                    kernel.TerminateProcess(handle, 0)
                    kernel.WaitForSingleObject(handle, 5000)
                kernel.CloseHandle(handle)
            if controller is not None:
                if controller.poll() is None:
                    controller.terminate()
                controller.wait(timeout=5)
                for stream in (controller.stdin, controller.stdout, controller.stderr):
                    stream.close()


if __name__ == '__main__':
    unittest.main()
