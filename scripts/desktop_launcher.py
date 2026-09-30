"""Windows desktop process owner. Trusted developer script, never an Agent tool.

Named kernel objects identify live ownership; persisted PIDs are diagnostic only.
Owned jobs contain the backend/client trees, never unrelated apps.
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'agent'))
from forgepilot.build_process import ProcessJob, build_environment
from forgepilot.security import Redactor
from forgepilot.errors import ForgePilotError
from forgepilot.llm import OpenAICompatibleClient

PYTHON = ROOT / 'agent.venv/Scripts/python.exe'
SCRIPT = Path(__file__).resolve()
CLIENT_START_TIMEOUT = 15
SHUTDOWN_TIMEOUT = 25


class Kernel:
    def __init__(self, config: Path):
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'CreateMutexW': ([ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
            'OpenMutexW': ([wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
            'ReleaseMutex': ([wintypes.HANDLE], wintypes.BOOL),
            'CreateEventW': ([ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
            'OpenEventW': ([wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
            'SetEvent': ([wintypes.HANDLE], wintypes.BOOL),
            'WaitForSingleObject': ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            'CloseHandle': ([wintypes.HANDLE], wintypes.BOOL),
            'OpenJobObjectW': ([wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
            'GetCurrentProcess': ([], wintypes.HANDLE),
            'AssignProcessToJobObject': ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (args, result) in signatures.items():
            fn = getattr(self.api, name)
            fn.argtypes, fn.restype = args, result
        identity = hashlib.sha256(str(config.resolve()).casefold().encode()).hexdigest()[:24]
        self.name = 'Local\\ForgePilotDesktop-' + identity

    def running(self):
        handle = self.api.OpenMutexW(0x100001, False, self.name)
        if not handle:
            return False
        try:
            result = self.api.WaitForSingleObject(handle, 0)
            if result in (0, 0x80):
                self.api.ReleaseMutex(handle)
                return False
            if result != 0x102:
                raise OSError('mutex_wait')
            return True
        finally:
            self.api.CloseHandle(handle)

    def event(self):
        handle = self.api.OpenEventW(0x100002, False, self.name + '-stop')
        if not handle:
            raise OSError('stop_event_unavailable')
        return handle

    def signaled(self, handle):
        return self.api.WaitForSingleObject(handle, 0) == 0

    def join(self, name):
        handle = self.api.OpenJobObjectW(1, False, name)
        try:
            if not handle or not self.api.AssignProcessToJobObject(handle, self.api.GetCurrentProcess()):
                raise OSError('job_assignment_failed')
        finally:
            if handle:
                self.api.CloseHandle(handle)


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


def read_config(path):
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    if set(data) != {'base_url', 'model', 'port', 'server_args'}:
        raise ValueError('config_fields')
    if not all(isinstance(data[n], str) and data[n].strip() for n in ('base_url', 'model')):
        raise ValueError('model_config')
    if type(data['port']) is not int or not 1 <= data['port'] <= 65535:
        raise ValueError('port_config')
    args = data['server_args']
    if not isinstance(args, list) or not all(isinstance(a, str) and '\x00' not in a for a in args):
        raise ValueError('server_args')
    # Host/port and model identity have one authoritative configuration source.
    if any(a.split('=')[0] in {'--host', '--port', '--base-url', '--model'} for a in args):
        raise ValueError('reserved_server_arg')
    return data


async def check_model(config):
    """One bounded request; no repository, history database or running service needed."""
    key = os.environ.get('FORGEPILOT_API_KEY', '')
    redactor = Redactor((key,))
    try:
        data = read_config(config)
        if not key:
            raise ForgePilotError('llm_config', '未配置 API Key，请运行 Configure-ForgePilot.cmd。')
        async with OpenAICompatibleClient(data['base_url'], key, data['model'], timeout=30) as llm:
            reply = await llm.chat([{'role': 'user', 'content': 'Reply with only OK.'}])
            if reply.message.tool_calls or not (reply.message.content or '').strip():
                raise ForgePilotError('llm_protocol', '模型未返回有效的文本回答，请核对模型与接口兼容性。')
    except ForgePilotError as exc:
        print(redactor.text(str(exc)), file=sys.stderr)
        return 1
    except Exception:
        # Malformed local configuration must not echo arbitrary values either.
        print('模型连接检查未完成，请检查本机 config.json 和运行环境。', file=sys.stderr)
        return 1
    print('模型连接检查通过：当前地址、模型与 API Key 已成功返回文本回答。')
    return 0


class RedactedLog:
    """Buffer complete lines, including fragmented traceback writes, before redaction."""
    def __init__(self, stream, key):
        self.stream, self.redactor, self.pending = stream, Redactor((key,)), ''

    def write(self, value):
        self.pending += value
        while '\n' in self.pending:
            line, self.pending = self.pending.split('\n', 1)
            self.stream.write(self.redactor.text(line) + '\n')
        self.stream.flush()
        return len(value)

    def flush(self):
        self.stream.flush()

    def finish(self):
        if self.pending:
            self.stream.write(self.redactor.text(self.pending))
            self.pending = ''
        self.stream.flush()

    def isatty(self):
        return False


def backend(config, job, ready, log):
    kernel = Kernel(config)
    kernel.join(job)  # Before loading the service or spawning any of its tools.
    event = kernel.event()
    data = read_config(config)
    os.environ['FORGEPILOT_BASE_URL'] = data['base_url']
    os.environ['FORGEPILOT_MODEL'] = data['model']
    with log.open('w', encoding='utf-8') as output:
        writer = RedactedLog(output, os.environ.get('FORGEPILOT_API_KEY', ''))
        previous = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = writer
        try:
            from forgepilot.server import main as serve
            return serve(['--host', '127.0.0.1', '--port', str(data['port']), *data['server_args']],
                         stop_requested=lambda: kernel.signaled(event),
                         on_ready=lambda: ready.touch())
        finally:
            writer.finish()
            sys.stdout, sys.stderr = previous
            kernel.api.CloseHandle(event)


def owned_client(job, log, port):
    worker = ROOT / 'agent/forgepilot/_build_worker.py'
    with log.open('wb') as output:
        proc = subprocess.Popen([str(PYTHON), '-I', str(worker), job.name], stdin=subprocess.PIPE,
                                stdout=output, stderr=output, env=build_environment(),
                                creationflags=subprocess.CREATE_NO_WINDOW)
    payload = {'command': [str(ROOT / 'client/build/Release/ForgePilotClient.exe'),
                           '--server', f'http://127.0.0.1:{port}'], 'cwd': str(ROOT)}
    proc.stdin.write((json.dumps(payload) + '\n').encode())
    proc.stdin.close()
    return proc


def client_window(job):
    """Only a visible window belonging to our live Job proves that Qt opened.

    Venv redirectors and workers can remain alive while CreateProcess is still
    blocked/failing. Their liveness is not a client readiness signal.
    """
    class ProcessList(ctypes.Structure):
        _fields_ = [('assigned', wintypes.DWORD), ('count', wintypes.DWORD),
                    ('pids', ctypes.c_size_t * 64)]

    query = job.api.QueryInformationJobObject
    query.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    query.restype = wintypes.BOOL
    processes = ProcessList()
    if not query(job.handle, 3, ctypes.byref(processes), ctypes.sizeof(processes), None):
        raise OSError('client_job_query_failed')
    pids = set(processes.pids[:processes.count])
    api = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    api.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    api.EnumWindows.restype = wintypes.BOOL
    api.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    api.IsWindowVisible.argtypes = [wintypes.HWND]
    found = []

    @callback_type
    def visit(window, _):
        pid = wintypes.DWORD()
        api.GetWindowThreadProcessId(window, ctypes.byref(pid))
        if pid.value in pids and api.IsWindowVisible(window):
            found.append(pid.value)
        return True

    if not api.EnumWindows(visit, 0):
        raise OSError('client_window_query_failed')
    return found[0] if found else None


def request_stop(kernel, event):
    if not kernel.api.SetEvent(event):
        raise OSError('stop_event_failed')


def supervise(config, result):
    kernel = Kernel(config)
    mutex = kernel.api.CreateMutexW(None, False, kernel.name)
    if not mutex:
        raise OSError('mutex_create')
    if kernel.api.WaitForSingleObject(mutex, 0) not in (0, 0x80):
        kernel.api.CloseHandle(mutex)
        write_json(result, {'status': 'already_running'})
        return 0
    event = None
    job = None
    client_job = None
    child = None
    client = None
    reported = False
    state = {'status': 'starting'}
    state_file = config.parent / 'runtime.json'
    try:
        event = kernel.api.CreateEventW(None, True, False, kernel.name + '-stop')
        if not event:
            raise OSError('event_create')
        write_json(state_file, state)
        data = read_config(config)
        if not os.environ.get('FORGEPILOT_API_KEY'):
            raise ValueError('missing_api_key')
        if not (ROOT / 'client/build/Release/ForgePilotClient.exe').is_file():
            raise ValueError('client_not_built')
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind(('127.0.0.1', data['port']))
        run = ROOT / 'logs/desktop' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid4().hex[:8])
        run.mkdir(parents=True)
        state.update(log_directory=str(run), port=data['port'], supervisor_pid=os.getpid())
        write_json(state_file, state)
        job = ProcessJob()
        ready = run / 'backend.ready'
        child = subprocess.Popen([str(PYTHON), '-I', str(SCRIPT), 'backend', '--config', str(config),
                                  '--job', job.name, '--ready', str(ready), '--log', str(run / 'backend.log')],
                                 cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        state['backend_pid'] = child.pid
        write_json(state_file, state)
        os.environ.pop('FORGEPILOT_API_KEY', None)
        deadline = time.monotonic() + 30
        while not ready.exists():
            if child.poll() is not None:
                raise RuntimeError('backend_start_failed')
            if kernel.signaled(event):
                raise RuntimeError('start_cancelled')
            if time.monotonic() > deadline:
                raise RuntimeError('backend_start_timeout')
            time.sleep(0.1)
        if kernel.signaled(event):
            raise RuntimeError('start_cancelled')
        # Separate jobs also work when Python's venv redirectors already have distinct
        # parent job hierarchies. A nonempty job cannot cross those hierarchies.
        client_job = ProcessJob()
        client = owned_client(client_job, run / 'client.log', data['port'])
        state['client_worker_pid'] = client.pid
        write_json(state_file, state)
        deadline = time.monotonic() + CLIENT_START_TIMEOUT
        while True:
            if kernel.signaled(event):
                raise RuntimeError('start_cancelled')
            if child.poll() is not None:
                raise RuntimeError('backend_exited')
            if client.poll() is not None:
                state['client_exit'] = client.returncode
                raise RuntimeError('client_start_failed')
            client_pid = client_window(client_job)
            if client_pid is not None:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError('client_window_timeout')
            time.sleep(0.1)
        state.update(status='running', client_pid=client_pid, client_status='running')
        write_json(state_file, state)
        write_json(result, state)
        reported = True
        while not kernel.signaled(event):
            if child.poll() is not None:
                raise RuntimeError('backend_exited')
            if client.poll() is not None and state['client_status'] == 'running':
                state.update(client_exit=client.returncode, client_status='closed')
                if client.returncode != 0:
                    raise RuntimeError('client_exited')
                write_json(state_file, state)  # Closing the UI still leaves ongoing tasks running.
            time.sleep(0.1)
        state['status'] = 'stopping'
        write_json(state_file, state)
    except Exception as exc:
        safe_errors = {'missing_api_key', 'client_not_built', 'backend_start_failed', 'start_cancelled',
                       'backend_start_timeout', 'client_start_failed', 'client_window_timeout', 'client_exited', 'backend_exited',
                       'config_fields', 'model_config', 'port_config', 'server_args', 'reserved_server_arg'}
        reason = str(exc) if str(exc) in safe_errors else ('port_in_use' if isinstance(exc, OSError) and
                 getattr(exc, 'winerror', None) == 10048 else type(exc).__name__)
        state.update(status='failed', error=reason)
    finally:
        try:
            # Failed/cancelled startup must drain the backend too: it may already
            # have opened SQLite even though no usable client window appeared.
            if child:
                if child.poll() is None:
                    request_stop(kernel, event)
                try:
                    child.wait(timeout=SHUTDOWN_TIMEOUT)
                    state.update(shutdown='graceful' if child.returncode == 0 else 'backend_failed',
                                 backend_exit=child.returncode)
                except subprocess.TimeoutExpired:
                    state.update(shutdown='forced_after_25_seconds')
        finally:
            try:
                if client_job:
                    client_job.close()
                if job:
                    job.close()  # Kills only this launch's descendants.
                # Wait for both venv redirectors as well as their owned children.
                for proc in (client, child):
                    if proc:
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            proc.kill()  # Live Popen handle, never a PID read from disk.
                            proc.wait(timeout=5)
                if state['status'] == 'stopping':
                    state['status'] = 'stopped'
                if client:
                    state['client_status'] = 'closed'
                write_json(state_file, state)
                if not reported:
                    write_json(result, state)  # Failure is reported only after cleanup.
            finally:
                if event:
                    kernel.api.CloseHandle(event)
                kernel.api.ReleaseMutex(mutex)
                kernel.api.CloseHandle(mutex)
    return 1 if state['status'] == 'failed' else 0


def stop(config):
    kernel = Kernel(config)
    if not kernel.running():
        print('No managed ForgePilot instance is running. Other processes were not touched.')
        return 0
    # The owner may still be creating its event immediately after acquiring the mutex.
    deadline = time.monotonic() + 3
    while True:
        try:
            event = kernel.event()
            break
        except OSError:
            if not kernel.running():
                return 0
            if time.monotonic() >= deadline:
                print('Stop event unavailable; no process was killed.')
                return 1
            time.sleep(0.1)
    try:
        request_stop(kernel, event)
    finally:
        kernel.api.CloseHandle(event)
    deadline = time.monotonic() + SHUTDOWN_TIMEOUT + 15
    while kernel.running():
        if time.monotonic() >= deadline:
            print('Shutdown timed out. Inspect runtime.json; unrelated processes were not touched.')
            return 1
        time.sleep(0.1)
    state_file = config.parent / 'runtime.json'
    if state_file.exists():
        print(json.dumps(json.loads(state_file.read_text(encoding='utf-8')), ensure_ascii=True, indent=2))
    return 0


def start(config):
    if Kernel(config).running():
        print('ForgePilot is already running; no duplicate processes started.')
        return 0
    config.parent.mkdir(parents=True, exist_ok=True)
    result = config.parent / ('start-' + uuid4().hex + '.json')
    owner = subprocess.Popen([str(PYTHON), '-I', str(SCRIPT), 'supervise', '--config', str(config), '--result', str(result)],
                     cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS)
    deadline = time.monotonic() + 30 + CLIENT_START_TIMEOUT + SHUTDOWN_TIMEOUT + 15
    while not result.exists():
        if owner.poll() is not None and not result.exists():
            print('Launcher owner exited before startup completed. Check runtime.json and Windows application control logs.')
            return 1
        if time.monotonic() >= deadline:
            stop(config)
            print('Startup timed out. Inspect logs/desktop.')
            return 1
        time.sleep(0.1)
    data = json.loads(result.read_text(encoding='utf-8'))
    result.unlink()  # Ephemeral handshake only; actual run logs and runtime.json are retained.
    print(json.dumps(data, ensure_ascii=True, indent=2))
    if data.get('error') in {'client_start_failed', 'client_window_timeout'}:
        print('Client window did not open. See client.log for the Windows error or Qt diagnostic; '
              'also check Windows Code Integrity/App Control events. No security policy was changed.')
    return 0 if data['status'] in {'running', 'already_running'} else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start', 'stop', 'status', 'supervise', 'backend', 'check'))
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--result', type=Path)
    parser.add_argument('--job')
    parser.add_argument('--ready', type=Path)
    parser.add_argument('--log', type=Path)
    args = parser.parse_args()
    config = args.config.resolve()
    if args.action == 'check':
        return asyncio.run(check_model(config))
    if args.action == 'backend':
        return backend(config, args.job, args.ready, args.log)
    if args.action == 'supervise':
        return supervise(config, args.result)
    if args.action == 'status':
        return 0 if Kernel(config).running() else 3
    return start(config) if args.action == 'start' else stop(config)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        print('Launcher configuration or OS operation failed. Check local config and logs/desktop.', file=sys.stderr)
        raise SystemExit(1)
