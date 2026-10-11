"""Start or stop this local client without a shell or changes to system policy."""
from pathlib import Path
import argparse
import ctypes
import json
import msvcrt
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from win_lifecycle import start_background

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / '.runtime'
URL = 'http://127.0.0.1:8741/'


def status():
    try:
        with urllib.request.urlopen(URL + 'api/status', timeout=2) as response:
            return json.load(response)
    except (urllib.error.URLError, OSError):
        return None


def owned_status():
    snapshot = status()
    if snapshot and snapshot.get('appId') != 'codex-dragon-client':
        raise RuntimeError('本地端口正被其他应用使用，未启动或关闭任何应用。')
    return snapshot


def stop():
    if not owned_status():
        return
    request = urllib.request.Request(URL + 'api/shutdown', data=b'{}', method='POST',
        headers={'Content-Type': 'application/json', 'Origin': URL.rstrip('/')})
    with urllib.request.urlopen(request, timeout=5) as response:
        json.load(response)


def reconnect():
    request = urllib.request.Request(URL + 'api/reconnect', data=b'{}', method='POST',
        headers={'Content-Type': 'application/json', 'Origin': URL.rstrip('/')})
    with urllib.request.urlopen(request, timeout=35) as response:
        result = json.load(response)
    if result.get('error'):
        raise RuntimeError(result['error'].get('message') or '本地连接暂未恢复，请稍后重试。')


def wait_ready(child=None):
    deadline = time.monotonic() + 45
    snapshot = None
    while time.monotonic() < deadline:
        snapshot = owned_status()
        if snapshot and snapshot.get('connected'):
            return snapshot
        if child is not None and child.poll() is not None:
            raise RuntimeError('龙娘工坊启动失败，请查看安装目录中的运行记录。')
        time.sleep(.3)
    if snapshot and snapshot.get('error'):
        raise RuntimeError(snapshot['error'])
    raise RuntimeError('龙娘工坊尚未连接成功。当前没有打开新窗口，请稍后再试启动入口。')


def start(no_window=False):
    RUNTIME.mkdir(exist_ok=True)
    with (RUNTIME / 'launch.lock').open('a+b') as lock:
        lock.seek(0)
        lock.write(b'0')
        lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return
        try:
            snapshot = owned_status()
            if not snapshot:
                python = Path(sys.executable).with_name('python.exe')
                log = (RUNTIME / 'bridge.log').open('ab')
                try:
                    child = start_background([str(python), str(ROOT / 'server.py'), '--port', '8741',
                        '--cwd', str(ROOT.parent)], cwd=ROOT, stdin=subprocess.DEVNULL,
                        stdout=log, stderr=log)
                finally:
                    log.close()
                (RUNTIME / 'server-process.json').write_text(json.dumps({'pid': child.pid,
                    'script': str(ROOT / 'server.py'), 'startedAt': time.time()}), encoding='utf-8')
                snapshot = wait_ready(child)
            elif not snapshot.get('connected'):
                reconnect()
                snapshot = wait_ready()
            if not no_window:
                candidates = [Path(os.environ.get('PROGRAMFILES(X86)', r'C:\Program Files (x86)')) / 'Microsoft/Edge/Application/msedge.exe',
                    Path(os.environ.get('PROGRAMFILES', r'C:\Program Files')) / 'Microsoft/Edge/Application/msedge.exe']
                edge = next((p for p in candidates if p.is_file()), None)
                if edge:
                    subprocess.Popen([str(edge), '--app=' + URL, '--window-size=1440,900',
                        '--no-first-run', '--user-data-dir=' + str(RUNTIME / 'edge-profile')],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    os.startfile(URL)
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--no-window', action='store_true')
    parser.add_argument('--stop', action='store_true')
    args = parser.parse_args()
    try:
        stop() if args.stop else start(args.no_window)
    except Exception as exc:
        if args.no_window:
            print(str(exc), file=sys.stderr)
        else:
            ctypes.windll.user32.MessageBoxW(None, str(exc), 'Codex 龙娘工坊', 0x10)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
