"""ForgePilot Windows desktop application and process owner.

Named kernel objects identify live ownership; persisted PIDs are diagnostic only.
Owned jobs contain the backend/client trees, never unrelated apps.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import redirect_stdout
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import secrets
from pathlib import Path
import socket
import subprocess
import sys
import time
from uuid import uuid4

FROZEN = bool(getattr(sys, 'frozen', False))
ROOT = Path(sys.executable).resolve().parents[1] if FROZEN else Path(__file__).resolve().parents[1]
if not FROZEN:
    sys.path.insert(0, str(ROOT / 'agent'))
from forgepilot.build_process import ProcessJob, build_environment
from forgepilot.security import Redactor
from forgepilot.errors import ForgePilotError
from forgepilot.models.gateway import ModelGateway
from forgepilot import desktop_configuration

PYTHON = Path(sys.executable) if FROZEN else ROOT / 'agent.venv/Scripts/python.exe'
LEGACY_PYTHON_ENVIRONMENT = ROOT / 'agent/.venv'
SCRIPT = Path(sys.executable).resolve() if FROZEN else Path(__file__).resolve()
CLIENT_START_TIMEOUT = 15
SHUTDOWN_TIMEOUT = 25


def launcher_command(action: str, *arguments: str) -> list[str]:
    prefix = [str(SCRIPT)] if FROZEN else [str(PYTHON), '-I', str(SCRIPT)]
    return [*prefix, action, *arguments]


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
            'ResetEvent': ([wintypes.HANDLE], wintypes.BOOL),
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

    def restart_event(self):
        handle = self.api.OpenEventW(0x100002, False, self.name + '-restart')
        if not handle:
            raise OSError('restart_event_unavailable')
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
    # Windows readers briefly hold a non-share-delete handle.  Runtime status
    # updates must survive that harmless race rather than aborting the owner.
    deadline = time.monotonic() + 2.0
    while True:
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def read_config(path):
    return desktop_configuration.read_config(path)


async def check_model(config, *, transport=None):
    return await desktop_configuration.check_model(config, transport=transport, gateway_factory=ModelGateway)


class RedactedLog:
    """Buffer complete lines, including fragmented traceback writes, before redaction."""
    def __init__(self, stream, key):
        self.stream, self.redactor, self.pending = stream, Redactor((key, os.environ.get('FORGEPILOT_LOCAL_API_TOKEN', ''), os.environ.get('FORGEPILOT_EMBEDDING_API_KEY', ''))), ''

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
    frozen_config = os.environ.get('FORGEPILOT_LAUNCH_CONFIG')
    data = read_config(Path(frozen_config) if frozen_config else config)
    os.environ['FORGEPILOT_BASE_URL'] = data['base_url']
    if data['model']:
        os.environ['FORGEPILOT_MODEL'] = data['model']
    else:
        os.environ.pop('FORGEPILOT_MODEL', None)
    os.environ['FORGEPILOT_CHAT_MODEL'] = data['chat_model']
    os.environ['FORGEPILOT_TASK_MODEL'] = data['task_model']
    os.environ['FORGEPILOT_SUMMARY_MODEL'] = data['summary_model']
    for name, value in (('FORGEPILOT_REVIEW_MODEL', data['review_model']),
                        ('FORGEPILOT_TASK_MODEL_FAMILY', data['task_model_family']),
                        ('FORGEPILOT_REVIEW_MODEL_FAMILY', data['review_model_family'])):
        if value:
            os.environ[name] = value
        else:
            os.environ.pop(name, None)
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


def owned_client(job, log, port, local_token):
    worker = ROOT / 'agent/forgepilot/_build_worker.py'
    with log.open('wb') as output:
        client_env = build_environment()
        client_env.update(desktop_configuration.model_check_environment(os.environ.get('FORGEPILOT_RUNTIME_CONFIG')))
        client_env['FORGEPILOT_LOCAL_API_TOKEN'] = local_token
        command = (launcher_command('worker', '--job', job.name) if FROZEN
                   else [str(PYTHON), '-I', str(worker), job.name])
        proc = subprocess.Popen(command, stdin=subprocess.PIPE,
                                stdout=output, stderr=output, env=client_env,
                                creationflags=subprocess.CREATE_NO_WINDOW)
    command = [str(ROOT / 'client/build/Release/ForgePilotClient.exe'), '--server', f'http://127.0.0.1:{port}']
    if os.environ.get('FORGEPILOT_RUNTIME_CONFIG'):
        command.extend(['--runtime-config', os.environ['FORGEPILOT_RUNTIME_CONFIG']])
    payload = {'command': command, 'cwd': str(ROOT)}
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


def default_config_path() -> Path:
    # Share the per-checkout settings and DPAPI credentials with the Windows GUI.
    identity = hashlib.sha256(str(ROOT).lower().encode('utf-8')).hexdigest()[:16].upper()
    return Path(os.environ['LOCALAPPDATA']) / 'ForgePilot' / 'launcher' / identity / 'config.json'


def supervise(config, result=None):
    while True:
        exit_code = _supervise_once(config, result)
        if exit_code is not None:
            return exit_code
        # A source run keeps the same process (and debugger) across a GUI restart.
        if result is not None:
            return start(config)


def _supervise_once(config, result):
    kernel = Kernel(config)
    mutex = kernel.api.CreateMutexW(None, False, kernel.name)
    if not mutex:
        raise OSError('mutex_create')
    if kernel.api.WaitForSingleObject(mutex, 0) not in (0, 0x80):
        kernel.api.CloseHandle(mutex)
        if result is None:
            return show_managed_window(config)
        write_json(result, {'status': 'already_running'})
        return 0
    event = None
    restart_event = None
    show_event = None
    restart_requested = False
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
        restart_event = kernel.api.CreateEventW(None, True, False, kernel.name + '-restart')
        show_event = kernel.api.CreateEventW(None, True, False, kernel.name + '-show')
        if not restart_event or not show_event:
            raise OSError('event_create')
        write_json(state_file, state)
        with desktop_configuration.ConfigurationLock(config):
            data = read_config(config)
            running_revision = desktop_configuration.configuration_revision(config)
            if data.get('credential_file') or config.with_name('api-key.dpapi').is_file():
                os.environ['FORGEPILOT_API_KEY'] = desktop_configuration.load_saved_key(config)
            embedding_key = os.environ.get('FORGEPILOT_EMBEDDING_API_KEY', '')
            optional_warning = None
            if '--enable-rag' in data['server_args']:
                try:
                    saved_embedding_key = desktop_configuration.load_embedding_key(config)
                    if data.get('embedding_credential_file') and not saved_embedding_key:
                        raise desktop_configuration.ConfigurationError('embedding_credential', 'Saved embedding credential is missing')
                    embedding_key = saved_embedding_key or embedding_key
                except (desktop_configuration.ConfigurationError, OSError):
                    data['server_args'] = [arg for arg in data['server_args'] if arg != '--enable-rag']
                    embedding_key = ''
                    optional_warning = 'embedding_credential_unavailable'

        if not os.environ.get('FORGEPILOT_API_KEY'):
            raise ValueError('missing_api_key')
        # A trusted process owner may supply a fresh token (for example an
        # integration harness); ordinary desktop starts generate one here.
        local_token = os.environ.get('FORGEPILOT_LOCAL_API_TOKEN') or secrets.token_urlsafe(32)
        if not (ROOT / 'client/build/Release/ForgePilotClient.exe').is_file():
            raise ValueError('client_not_built')
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind(('127.0.0.1', data['port']))
        run = ROOT / 'logs/desktop' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid4().hex[:8])
        run.mkdir(parents=True)
        launch_config = run / 'launch-config.json'
        write_json(launch_config, data)
        state.update(log_directory=str(run), port=data['port'], supervisor_pid=os.getpid(), running_revision=running_revision, optional_warning=optional_warning)
        write_json(state_file, state)
        job = ProcessJob()
        ready = run / 'backend.ready'
        child = subprocess.Popen(launcher_command('backend', '--config', str(config),
                                  '--job', job.name, '--ready', str(ready), '--log', str(run / 'backend.log')),
                                 cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
                                 env={**os.environ, 'FORGEPILOT_LOCAL_API_TOKEN': local_token,
                                      'FORGEPILOT_EMBEDDING_API_KEY': embedding_key,
                                      'FORGEPILOT_LAUNCH_CONFIG': str(launch_config)})
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
        os.environ['FORGEPILOT_RUNTIME_CONFIG'] = str(config)

        def launch_window():
            nonlocal client_job, client
            # Separate jobs also work when Python's venv redirectors already have distinct
            # parent job hierarchies. A nonempty job cannot cross those hierarchies.
            if client_job:
                client_job.close()
            client_job = ProcessJob()
            state.pop('client_exit', None)
            state.pop('client_pid', None)
            state.pop('client_error', None)
            client = owned_client(client_job, run / 'client.log', data['port'], local_token)
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

        launch_window()
        if result is None:
            print(f"ForgePilot is running at http://127.0.0.1:{data['port']}", flush=True)
        else:
            write_json(result, state)
        reported = True
        status_due = 0.0
        while not kernel.signaled(event):
            if kernel.signaled(restart_event):
                kernel.api.ResetEvent(restart_event)
                request_id = None
                try:
                    intent = json.loads(config.with_name('restart-request.json').read_text(encoding='utf-8'))
                    request_id = intent['request_id']
                    read_config(config)
                    desktop_configuration.load_saved_key(config)
                    _backend_runtime(data['port'], local_token, prepare=True)
                    state.update(status='restarting', restart_request_id=request_id, restart_accepted=True)
                    restart_requested = True
                    write_json(state_file, state)
                    break
                except Exception as exc:
                    state.update(restart_request_id=request_id, restart_accepted=False,
                        restart_error=exc.code if isinstance(exc, ForgePilotError) else 'restart_unavailable')
                    write_json(state_file, state)
            if time.monotonic() >= status_due:
                try:
                    state['active_count'] = _backend_runtime(data['port'], local_token)['active_count']
                except Exception:
                    state['active_count'] = None
                write_json(state_file, state)
                status_due = time.monotonic() + 2
            if child.poll() is not None:
                raise RuntimeError('backend_exited')
            if state['client_status'] == 'running' and client.poll() is not None:
                state.update(client_exit=client.returncode, client_status='closed')
                if client.returncode != 0:
                    raise RuntimeError('client_exited')
                write_json(state_file, state)  # Closing the UI still leaves ongoing tasks running.
            if kernel.signaled(show_event):
                if state['client_status'] == 'closed':
                    try:
                        launch_window()
                    except (OSError, RuntimeError, ForgePilotError) as exc:
                        if client_job:
                            client_job.close()
                        state.update(client_status='closed', client_error=type(exc).__name__)
                        write_json(state_file, state)
                elif client_window(client_job) is None:
                    # WM_CLOSE can hide the window before the worker reports exit.
                    time.sleep(0.1)
                    continue
                # Acknowledge all pending starts after verifying the window or recording a failure.
                kernel.api.ResetEvent(show_event)
            time.sleep(0.1)
        state['status'] = 'stopping'
        write_json(state_file, state)
    except KeyboardInterrupt:
        state['status'] = 'stopping'
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
                if result is not None and not reported:
                    write_json(result, state)  # Failure is reported only after cleanup.
            finally:
                if show_event:
                    kernel.api.CloseHandle(show_event)
                if restart_event:
                    kernel.api.CloseHandle(restart_event)
                if event:
                    kernel.api.CloseHandle(event)
                kernel.api.ReleaseMutex(mutex)
                kernel.api.CloseHandle(mutex)
    if restart_requested and state['status'] != 'failed':
        if result is not None:
            os.environ['FORGEPILOT_API_KEY'] = desktop_configuration.load_saved_key(config)
        return None
    if result is None and state['status'] == 'failed':
        print(f"ForgePilot failed: {state['error']}. See {state_file}.", file=sys.stderr)
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


def show_managed_window(config):
    kernel = Kernel(config)
    # This may race initial startup. Holding the named event until acknowledgment
    # lets the owner receive the request even if it has only just acquired its mutex.
    event = kernel.api.CreateEventW(None, True, False, kernel.name + '-show')
    if not event:
        raise OSError('event_create')
    try:
        if not kernel.api.SetEvent(event):
            raise OSError('show_event_failed')
        deadline = time.monotonic() + 30 + CLIENT_START_TIMEOUT + 5
        while kernel.signaled(event):
            if not kernel.running() or time.monotonic() >= deadline:
                print('Desktop window request was not acknowledged. Inspect runtime.json.')
                return 1
            time.sleep(0.1)
        state = json.loads(config.with_name('runtime.json').read_text(encoding='utf-8'))
        if state.get('status') == 'running' and state.get('client_status') == 'running':
            print('Connected to the running ForgePilot desktop.')
            return 0
        print('Desktop window did not open. Inspect client.log.')
        return 1
    finally:
        kernel.api.CloseHandle(event)


def _start_managed(config):
    config.parent.mkdir(parents=True, exist_ok=True)
    result = config.parent / ('start-' + uuid4().hex + '.json')
    owner = subprocess.Popen(launcher_command('supervise', '--config', str(config), '--result', str(result)),
                     cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS)
    deadline = time.monotonic() + 30 + CLIENT_START_TIMEOUT + SHUTDOWN_TIMEOUT + 15
    while not result.exists():
        if owner.poll() is not None and not result.exists():
            print(f'Launcher owner exited before startup completed (supervisor_exit={owner.returncode}). '
                  'Check runtime.json and Windows application control logs.')
            return {'status': 'failed', 'error': 'owner_exited'}
        if time.monotonic() >= deadline:
            stop(config)
            print('Startup timed out. Inspect logs/desktop.')
            return {'status': 'failed', 'error': 'start_timeout'}
        time.sleep(0.1)
    data = json.loads(result.read_text(encoding='utf-8'))
    result.unlink()  # Ephemeral handshake only; actual run logs and runtime.json are retained.
    print(json.dumps(data, ensure_ascii=True, indent=2))
    if data.get('error') in {'client_start_failed', 'client_window_timeout'}:
        print('Client window did not open. See client.log for the Windows error or Qt diagnostic; '
              'also check Windows Code Integrity/App Control events. No security policy was changed.')
    if data['status'] != 'already_running':
        return data
    if show_managed_window(config) == 0:
        return {'status': 'running'}
    return {'status': 'failed', 'error': 'client_window_unavailable'}


def start(config):
    if Kernel(config).running():
        return show_managed_window(config)
    return 0 if _start_managed(config)['status'] == 'running' else 1


def runtime_status(config):
    managed = Kernel(config).running()
    if not managed:
        return {'managed': False, 'active_count': 0, 'restarting': False, 'restart_allowed': True,
                'notice': '配置保存后可启动后端。'}
    try:
        state = json.loads(config.with_name('runtime.json').read_text(encoding='utf-8'))
        count = state.get('active_count')
        known = type(count) is int and count >= 0
        return {'managed': True, 'active_count': count if known else None,
                'restarting': state.get('status') in {'stopping', 'restarting'},
                'running_revision': state.get('running_revision'),
                'restart_allowed': known and count == 0 and state.get('status') == 'running',
                'notice': '有活动任务，仅保存；任务结束后再重启生效。' if known and count else
                    '重启前会再次核对活动任务。' if known else '暂时无法核实活动任务；配置可保存，请稍后刷新。'}
    except (OSError, ValueError):
        return {'managed': True, 'active_count': None, 'restart_allowed': False, 'restarting': False,
                'notice': '暂时无法核实活动任务；配置可保存，请稍后刷新。'}


def request_restart(config):
    # The owner is outside client/build jobs; the GUI helper may end when its
    # client's job closes. The owner validates and performs the entire restart.
    kernel = Kernel(config)
    if not kernel.running():
        state = _start_managed(config)
        if state['status'] == 'running':
            return {'ok': True, 'notice': '后端和主窗口已就绪。', 'restart_required': False}
        error = state['error']
        if error == 'port_in_use':
            notice = '本机端口已被占用，请更改端口并保存，再点击“重启并生效”。'
        elif error == 'client_not_built':
            notice = '缺少桌面客户端，请构建客户端或重新解压发布包，再重试。'
        else:
            notice = f'启动失败（{error}）。请查看配置目录的 runtime.json 和 logs/desktop 下的启动日志后重试。'
        return {'ok': False, 'notice': notice, 'error_code': error, 'restart_required': True}
    request_id = uuid4().hex
    write_json(config.with_name('restart-request.json'), {'request_id': request_id})
    event = kernel.restart_event()
    try:
        request_stop(kernel, event)
    finally:
        kernel.api.CloseHandle(event)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            state = json.loads(config.with_name('runtime.json').read_text(encoding='utf-8'))
            if state.get('restart_request_id') == request_id:
                accepted = state.get('restart_accepted') is True
                return {'ok': accepted, 'restart_required': not accepted,
                        'notice': '正在通过启动器重启，新窗口将加载已保存配置。' if accepted else
                            '当前有活动任务或无法安全重启；已保存配置将在任务结束后重启生效。',
                        'error_code': state.get('restart_error')}
        except (OSError, ValueError):
            pass  # The owner's atomic status replacement can race a reader.
        time.sleep(0.1)
    return {'ok': False, 'restart_required': True, 'notice': '尚未确认重启，请刷新当前运行状态；不会自动重复重启。'}


def _backend_runtime(port, token, *, prepare=False):
    import httpx
    path = '/api/v2/runtime/prepare-restart' if prepare else '/api/v2/runtime/status'
    with httpx.Client(timeout=2, trust_env=False) as client:
        response = client.request('POST' if prepare else 'GET', f'http://127.0.0.1:{port}' + path,
            headers={'X-ForgePilot-Token': token}, **({'json': {}} if prepare else {}))
    if response.status_code == 409:
        raise ForgePilotError('runtime_busy', 'Tasks are still active')
    response.raise_for_status()
    result = response.json()
    if type(result.get('active_count')) is not int or type(result.get('restarting')) is not bool:
        raise ValueError('runtime_status')
    return result


def launch_configuration_client(config):
    root = desktop_configuration.application_root()
    command = [str(root / 'client/build/Release/ForgePilotClient.exe'), '--server', 'http://127.0.0.1:1', '--configure',
               '--runtime-config', str(config)]
    if not Path(command[0]).is_file():
        print('缺少 Qt 客户端，请构建客户端或重新解压发布包。', file=sys.stderr)
        return 1
    environment = build_environment()
    environment.update(desktop_configuration.model_check_environment(config))
    if os.environ.get('FORGEPILOT_DISABLE_SKILL_ENTRIES') == '1':
        # First-time setup can launch the backend via the configuration helper.
        environment['FORGEPILOT_DISABLE_SKILL_ENTRIES'] = '1'
    environment['FORGEPILOT_RUNTIME_CONFIG'] = str(config)
    client = subprocess.Popen(command, cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
    print(json.dumps({'status': 'configuration', 'configuration_client_pid': client.pid}))
    return 0


def desktop(config, *, configure=False, foreground=False):
    if configure:
        return launch_configuration_client(config)
    if Kernel(config).running():
        return show_managed_window(config)
    loaded = asyncio.run(desktop_configuration.handle_request(config, {'action': 'load'}, status=lambda: runtime_status(config)))
    if not loaded.get('configured'):
        return launch_configuration_client(config)
    if foreground:
        return supervise(config)
    previous_key = os.environ.get('FORGEPILOT_API_KEY')
    try:
        os.environ['FORGEPILOT_API_KEY'] = desktop_configuration.load_saved_key(config)
        # Local validation only: connection checking is an explicit GUI action.
        if start(config) == 0:
            return 0
    finally:
        if previous_key is None:
            os.environ.pop('FORGEPILOT_API_KEY', None)
        else:
            os.environ['FORGEPILOT_API_KEY'] = previous_key
    return launch_configuration_client(config)


def configuration_helper(config):
    try:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise ValueError('request_limit')
        request = json.loads(raw.decode('utf-8'))
        # The Qt helper's stdout is a single JSON document, including during startup.
        with redirect_stdout(sys.stderr):
            response = asyncio.run(desktop_configuration.handle_request(config, request,
                restart=lambda: request_restart(config), status=lambda: runtime_status(config)))
    except (ValueError, UnicodeError, OSError):
        response = {'ok': False, 'notice': '配置请求无效，请重新打开配置。', 'errors': []}
    sys.stdout.write(json.dumps(response, ensure_ascii=True) + '\n')
    return 0


def run_project():
    """Run the desktop application from this file, including its local backend."""
    return desktop(default_config_path(), foreground=True)


def main():
    if len(sys.argv) == 1:
        return run_project()
    if sys.argv[1] == 'database-copy':
        from forgepilot.database_copy import copy_database
        parser = argparse.ArgumentParser(description="Copy a database into a new rollback directory.")
        parser.add_argument('--source', type=Path, required=True)
        parser.add_argument('--destination', type=Path, required=True)
        args = parser.parse_args(sys.argv[2:])
        print(copy_database(args.source, args.destination))
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == 'config-helper':
        parser = argparse.ArgumentParser()
        parser.add_argument('action'); parser.add_argument('--config', type=Path, required=True)
        args = parser.parse_args()
        return configuration_helper(args.config.resolve())
    if len(sys.argv) > 1 and sys.argv[1] == 'stdio-worker':
        if len(sys.argv) < 4:
            return 125
        from forgepilot._lsp_worker import main as stdio_worker
        sys.argv = [sys.argv[0], *sys.argv[2:]]
        return stdio_worker()
    if LEGACY_PYTHON_ENVIRONMENT.exists():
        print('WARNING: agent/.venv is not supported. Use the root agent.venv; '
              'the old environment was not removed.', file=sys.stderr)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start', 'stop', 'status', 'supervise', 'backend', 'check',
                                           'server', 'smoke', 'worker', 'desktop', 'configure', 'check-saved'))
    parser.add_argument('--config', type=Path)
    parser.add_argument('--result', type=Path)
    parser.add_argument('--job')
    parser.add_argument('--ready', type=Path)
    parser.add_argument('--log', type=Path)
    args, extra = parser.parse_known_args()
    if args.action == 'server':
        from forgepilot.server import main as serve
        return serve(extra)
    if extra:
        parser.error('unrecognized arguments: ' + ' '.join(extra))
    if args.action == 'smoke':
        if not FROZEN:
            sys.path.insert(0, str(ROOT / 'scripts'))
        from release_smoke import main as smoke
        return smoke()
    if args.action == 'worker':
        if not args.job:
            parser.error('worker requires --job')
        from forgepilot._build_worker import main as worker
        sys.argv = [sys.argv[0], args.job]
        return worker()
    if args.config is None:
        parser.error('--config is required')
    config = args.config.resolve()
    if args.action == 'desktop':
        return desktop(config)
    if args.action == 'configure':
        return desktop(config, configure=True)
    if args.action == 'check-saved':
        os.environ['FORGEPILOT_API_KEY'] = desktop_configuration.load_saved_key(config)
        return asyncio.run(check_model(config))
    if args.action == 'check':
        return asyncio.run(check_model(config))
    if args.action == 'backend':
        return backend(config, args.job, args.ready, args.log)
    if args.action == 'supervise':
        return supervise(config, args.result)
    if args.action == 'status':
        return 0 if Kernel(config).running() else 3
    return start(config) if args.action == 'start' else stop(config)


def run():
    try:
        return main()
    except Exception:
        print('Launcher configuration or OS operation failed. Check local config and logs/desktop.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(run())
