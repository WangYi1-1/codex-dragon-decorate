"""Create only this client's background process with an independent Windows lifetime."""
import ctypes
from ctypes import wintypes
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000
ROOT = Path(__file__).resolve().parent


class LifecycleError(RuntimeError):
    pass


class JobBasicLimits(ctypes.Structure):
    _fields_ = [
        ('PerProcessUserTimeLimit', ctypes.c_int64),
        ('PerJobUserTimeLimit', ctypes.c_int64),
        ('LimitFlags', wintypes.DWORD),
        ('MinimumWorkingSetSize', ctypes.c_size_t),
        ('MaximumWorkingSetSize', ctypes.c_size_t),
        ('ActiveProcessLimit', wintypes.DWORD),
        ('Affinity', ctypes.c_size_t),
        ('PriorityClass', wintypes.DWORD),
        ('SchedulingClass', wintypes.DWORD),
    ]


def _kernel32():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    kernel.IsProcessInJob.restype = wintypes.BOOL
    kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                               wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryInformationJobObject.restype = wintypes.BOOL
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


def _in_job(kernel, handle):
    value = wintypes.BOOL()
    if not kernel.IsProcessInJob(handle, None, ctypes.byref(value)):
        raise LifecycleError('无法确认后台的独立运行状态，未继续启动。请从桌面启动入口重试。')
    return bool(value.value)


def _current_job():
    kernel = _kernel32()
    if not _in_job(kernel, kernel.GetCurrentProcess()):
        return False, 0
    limits = JobBasicLimits()
    if not kernel.QueryInformationJobObject(None, 2, ctypes.byref(limits), ctypes.sizeof(limits), None):
        raise LifecycleError('无法读取启动环境的运行限制，未启动依赖原窗口的后台。请从桌面启动入口重试。')
    return True, limits.LimitFlags


def background_creation_flags():
    if os.name != 'nt':
        raise LifecycleError('这个启动入口目前需要 Windows。')
    in_job, limits = _current_job()
    if not in_job:
        return CREATE_NO_WINDOW
    if limits & JOB_OBJECT_LIMIT_BREAKAWAY_OK:
        return CREATE_NO_WINDOW | CREATE_BREAKAWAY_FROM_JOB
    if limits & JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK:
        return CREATE_NO_WINDOW
    raise LifecycleError('当前启动环境不允许后台独立运行。请双击桌面“Codex 龙娘工坊”启动；未启动会随原窗口退出的后台。')


def _verify_independent(process):
    # Popen's Windows process handle refers to this exact newly created process.
    if _in_job(_kernel32(), wintypes.HANDLE(int(process._handle))):
        raise LifecycleError('后台仍受启动窗口的运行限制，已停止本次启动。请从桌面启动入口重试。')


def _stop_owned(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


class BrokerProcess:
    """A held handle to the exact nonce-confirmed child, never a PID-only kill."""
    def __init__(self, pid, handle, kernel):
        self.pid, self._handle, self.kernel = pid, handle, kernel

    def poll(self):
        result = self.kernel.WaitForSingleObject(self._handle, 0)
        if result == 258:
            return None
        if result != 0:
            raise LifecycleError('无法读取本次后台的运行状态。')
        code = wintypes.DWORD()
        if not self.kernel.GetExitCodeProcess(self._handle, ctypes.byref(code)):
            raise LifecycleError('无法读取本次后台的运行状态。')
        return code.value

    def wait(self, timeout=None):
        duration = 0xFFFFFFFF if timeout is None else max(0, int(timeout * 1000))
        result = self.kernel.WaitForSingleObject(self._handle, duration)
        if result == 258:
            raise subprocess.TimeoutExpired('CodexDragonClient background', timeout)
        if result != 0:
            raise LifecycleError('无法等待本次后台的退出状态。')
        return self.poll()

    def terminate(self):
        if not self.kernel.TerminateProcess(self._handle, 1):
            raise LifecycleError('无法停止本次启动的后台。')

    kill = terminate

    def __del__(self):
        if self._handle:
            self.kernel.CloseHandle(self._handle)
            self._handle = None


def _broker_options(command, kwargs):
    # The service-owned WMI fallback is deliberately not a general shell/API.
    expected_python = Path(sys.executable).with_name('python.exe').resolve()
    if (not isinstance(command, (list, tuple)) or len(command) != 6
            or Path(command[0]).resolve() != expected_python
            or Path(command[1]).resolve() != ROOT / 'server.py'
            or list(command[2:5]) != ['--port', '8741', '--cwd']
            or Path(command[5]).resolve() != ROOT.parent
            or set(kwargs) != {'cwd', 'stdin', 'stdout', 'stderr'}
            or Path(kwargs['cwd']).resolve() != ROOT
            or kwargs['stdin'] != subprocess.DEVNULL
            or kwargs['stdout'] is not kwargs['stderr']):
        raise LifecycleError('这个启动请求不能使用独立后台代理。')
    output = Path(getattr(kwargs['stdout'], 'name', '')).resolve()
    if output != ROOT / '.runtime' / 'bridge.log':
        raise LifecycleError('独立后台代理只允许本项目的运行记录。')
    return {'command': list(command), 'cwd': str(ROOT), 'log': str(output)}


def _wmi_create(command, cwd):
    data = base64.b64encode(json.dumps({'commandLine': subprocess.list2cmdline(command), 'cwd': str(cwd)}).encode()).decode()
    # Encoded JSON is data, not interpolated PowerShell or a shell command.
    script = (
        "$ErrorActionPreference='Stop';"
        "$d=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + data + "'))|ConvertFrom-Json;"
        "$s=([wmiclass]'Win32_ProcessStartup').CreateInstance();$s.ShowWindow=0;$s.CreateFlags=150994944;"
        "$r=([wmiclass]'Win32_Process').Create($d.commandLine,$d.cwd,$s);"
        "@{returnValue=[int]$r.ReturnValue;pid=[int]$r.ProcessId}|ConvertTo-Json -Compress"
    )
    powershell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    result = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-EncodedCommand',
        base64.b64encode(script.encode('utf-16le')).decode()], stdin=subprocess.DEVNULL,
        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=20,
        creationflags=CREATE_NO_WINDOW)
    reply = json.loads(result.stdout) if not result.returncode else {}
    if reply.get('returnValue') != 0 or not isinstance(reply.get('pid'), int) or reply['pid'] <= 0:
        raise LifecycleError('Windows 独立后台代理未能启动，请从桌面启动入口重试。')
    return reply['pid']


def _read_handshake(path, nonce, pid, process=None):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            reply = json.loads(path.read_text(encoding='utf-8'))
        except (FileNotFoundError, json.JSONDecodeError):
            reply = None
        if reply is not None:
            if reply.get('nonce') != nonce or reply.get('pid') != pid or reply.get('error'):
                raise LifecycleError('独立后台代理身份校验失败，未启动会话服务。')
            return reply
        if process is not None and process.poll() is not None:
            break
        time.sleep(.05)
    raise LifecycleError('独立后台代理握手超时，未启动会话服务。')


def _cleanup_handshake(directory):
    """Only known transient files; Windows busy files never mask launch results."""
    try:
        target = directory.resolve()
        if target.parent != (ROOT / '.runtime').resolve() or not _is_spawn_directory(target.name):
            return
    except (OSError, RuntimeError):
        return
    for attempt in range(3):
        for name in ('config.json', 'ready.json', 'go.json', 'started.json'):
            try:
                (directory / name).unlink(missing_ok=True)
            except OSError:
                pass
        try:
            directory.rmdir()
            return
        except OSError:
            if attempt < 2:
                time.sleep(.05)


def _start_broker_background(command, original_error, **kwargs):
    try:
        options = _broker_options(command, kwargs)
    except LifecycleError:
        raise original_error
    nonce = uuid.uuid4().hex
    directory = ROOT / '.runtime' / ('spawn-' + nonce)
    directory.mkdir()
    config, ready, go, started = (directory / name for name in ('config.json', 'ready.json', 'go.json', 'started.json'))
    options['nonce'] = nonce
    process = None
    handle = None
    kernel = _kernel32()
    try:
        config.write_text(json.dumps(options), encoding='utf-8')
        pid = _wmi_create([command[0], str(ROOT / 'win_lifecycle.py'), '--wmi-child', str(config)], ROOT)
        reply = _read_handshake(ready, nonce, pid)
        if reply.get('script') != str(ROOT / 'server.py'):
            raise LifecycleError('独立后台代理路径校验失败。')
        handle = kernel.OpenProcess(0x100000 | 0x1000 | 0x1, False, pid)
        if not handle:
            raise LifecycleError('无法验证本次独立后台，未启动会话服务。')
        buffer, size = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
        if (not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size))
                or Path(buffer.value).resolve() != Path(command[0]).resolve()):
            raise LifecycleError('独立后台代理程序路径不匹配。')
        process = BrokerProcess(pid, handle, kernel)
        handle = None
        _verify_independent(process)
        go.write_text(json.dumps({'nonce': nonce, 'pid': pid}), encoding='utf-8')
        _read_handshake(started, nonce, pid, process)
        return process
    except Exception as exc:
        if process is not None:
            _stop_owned(process)
        if isinstance(exc, LifecycleError):
            raise
        raise LifecycleError('独立后台代理启动失败，请从桌面启动入口重试。') from exc
    finally:
        if handle:
            kernel.CloseHandle(handle)
        _cleanup_handshake(directory)


def start_background(command, **kwargs):
    try:
        flags = background_creation_flags()
    except LifecycleError as exc:
        return _start_broker_background(command, exc, **kwargs)
    try:
        process = subprocess.Popen(command, creationflags=flags, **kwargs)
    except OSError as exc:
        error = LifecycleError('独立后台未能启动。请从桌面“Codex 龙娘工坊”重试。')
        return _start_broker_background(command, error, **kwargs)
    try:
        _verify_independent(process)
    except LifecycleError as exc:
        _stop_owned(process)
        return _start_broker_background(command, exc, **kwargs)
    except Exception:
        _stop_owned(process)
        raise
    return process


def _broker_child(config):
    """WMI-owned bootstrap waits for verified-parent permission before starting."""
    import runpy
    config = Path(config).resolve()
    if config.name != 'config.json' or config.parent.parent != ROOT / '.runtime' or not _is_spawn_directory(config.parent.name):
        raise LifecycleError('Invalid background bootstrap path')
    options = json.loads(config.read_text(encoding='utf-8'))
    nonce, pid = options['nonce'], os.getpid()
    if Path(options['log']).resolve() != ROOT / '.runtime' / 'bridge.log':
        raise LifecycleError('Invalid background bootstrap log')
    # Validate the same narrowly scoped target even if the temporary file changed.
    with open(options['log'], 'ab') as log:
        validated = _broker_options(options['command'], {'cwd': options['cwd'], 'stdin': subprocess.DEVNULL, 'stdout': log, 'stderr': log})
        if config.parent.name != 'spawn-' + nonce or _in_job(_kernel32(), _kernel32().GetCurrentProcess()):
            return
        (config.parent / 'ready.json').write_text(json.dumps({'nonce': nonce, 'pid': pid, 'script': validated['command'][1]}), encoding='utf-8')
        _read_handshake(config.parent / 'go.json', nonce, pid)
        (config.parent / 'started.json').write_text(json.dumps({'nonce': nonce, 'pid': pid}), encoding='utf-8')
        with open(os.devnull, 'rb') as incoming:
            os.dup2(incoming.fileno(), 0)
            os.dup2(log.fileno(), 1)
            os.dup2(log.fileno(), 2)
            # WMI has no console/std handles; bind Python's streams as well as
            # CRT descriptors before the actual service writes diagnostics.
            sys.stdin = open(0, 'r', encoding='utf-8', closefd=False)
            sys.stdout = open(1, 'w', encoding='utf-8', errors='replace', buffering=1, closefd=False)
            sys.stderr = open(2, 'w', encoding='utf-8', errors='replace', buffering=1, closefd=False)
            os.chdir(validated['cwd'])
            sys.argv = validated['command'][1:]
            runpy.run_path(sys.argv[0], run_name='__main__')


def _is_spawn_directory(name):
    return name.startswith('spawn-') and len(name) == 38 and all(c in '0123456789abcdef' for c in name[6:])


if __name__ == '__main__' and len(sys.argv) == 3 and sys.argv[1] == '--wmi-child':
    _broker_child(sys.argv[2])
