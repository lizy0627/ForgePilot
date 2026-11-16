"""Real Windows launcher checks; dummy credentials, no model requests or user DB."""
import importlib.util
import asyncio
import ctypes
from ctypes import wintypes
import hashlib
import io
import json
import os
import secrets
from pathlib import Path
import socket
import subprocess
import sys
import time
import textwrap
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
import httpx

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/desktop_launcher.py'
spec = importlib.util.spec_from_file_location('desktop_launcher', SCRIPT)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)
pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows desktop lifecycle')
KEY = 'offline-launcher-sentinel-not-a-real-key'


def invoke(action, config, env=None):
    return subprocess.run([sys.executable, '-I', str(SCRIPT), action, '--config', str(config)],
                          env=env, capture_output=True, text=True, timeout=50,
                          creationflags=subprocess.CREATE_NO_WINDOW)


@pytest.fixture
def configured(tmp_path):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    config = tmp_path / 'settings with spaces' / 'config.json'
    config.parent.mkdir()
    data = {'base_url': 'http://127.0.0.1:1/v1', 'model': 'offline-startup-only', 'port': port,
            'server_args': ['--database', str(tmp_path / 'history.sqlite3')]}
    config.write_text(json.dumps(data), encoding='utf-8-sig')
    env = {**os.environ, 'FORGEPILOT_API_KEY': KEY,
           'FORGEPILOT_LOCAL_API_TOKEN': secrets.token_urlsafe(32)}
    try:
        yield config, data, env
    finally:
        result = invoke('stop', config, env)
        assert result.returncode == 0, result.stdout + result.stderr


def runtime(config):
    return json.loads((config.parent / 'runtime.json').read_text(encoding='utf-8'))


def startup_evidence(config, result, env):
    """Bounded test diagnostics; list fixture files without reading config or secrets."""
    redactor = launcher.Redactor((KEY, env['FORGEPILOT_LOCAL_API_TOKEN']))
    return json.dumps({
        'returncode': result.returncode,
        'stdout': redactor.text(result.stdout)[:4096],
        'stderr': redactor.text(result.stderr)[:4096],
        'runtime_exists': (config.parent / 'runtime.json').is_file(),
        'files': sorted(str(path.relative_to(config.parent.parent))
                        for path in config.parent.parent.rglob('*') if path.is_file()),
    }, ensure_ascii=True)


def close_fixture_window(pid):
    api = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    api.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    api.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    api.IsWindowVisible.argtypes = [wintypes.HWND]
    api.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    closed = []
    @callback_type
    def visit(window, _):
        owner = wintypes.DWORD()
        api.GetWindowThreadProcessId(window, ctypes.byref(owner))
        if owner.value == pid and api.IsWindowVisible(window):
            closed.append(bool(api.PostMessageW(window, 0x10, 0, 0)))
        return True
    deadline = time.monotonic() + 10
    while not closed:
        api.EnumWindows(visit, 0)
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert all(closed)



def wait_stopped(config):
    deadline = time.monotonic() + 8
    while launcher.Kernel(config).running() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not launcher.Kernel(config).running()


def test_startup_evidence_redacts_output_without_reading_config(configured):
    config, _, env = configured
    config.write_text('private-config-content', encoding='utf-8')
    (config.parent / 'runtime.tmp').touch()
    result = subprocess.CompletedProcess([], 1, stdout=KEY + '\n' + 'x' * 5000,
                                         stderr=env['FORGEPILOT_LOCAL_API_TOKEN'])
    evidence = startup_evidence(config, result, env)
    details = json.loads(evidence)
    assert details['returncode'] == 1 and not details['runtime_exists']
    assert len(details['stdout']) == 4096 and details['stderr'] == '[REDACTED]'
    assert details['files'] == ['settings with spaces\\config.json', 'settings with spaces\\runtime.tmp']
    assert all(secret not in evidence for secret in
               (KEY, env['FORGEPILOT_LOCAL_API_TOKEN'], 'private-config-content'))


def test_real_start_repeat_stop_restart_preserves_unrelated_process(configured):
    config, data, env = configured
    unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(90)'],
                                 creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        first = invoke('start', config, env)
        assert first.returncode == 0, first.stdout + first.stderr
        before = runtime(config)
        assert env['FORGEPILOT_LOCAL_API_TOKEN'] not in json.dumps(before)
        for name in ('backend.log', 'client.log'):
            assert env['FORGEPILOT_LOCAL_API_TOKEN'] not in (
                Path(before['log_directory']) / name).read_text(encoding='utf-8')
        from test_build_process import alive
        assert before['client_status'] == 'running' and alive(before['client_pid'])
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(f"http://127.0.0.1:{data['port']}/api/v1/tasks?limit=1",
            headers={'X-ForgePilot-Token': env['FORGEPILOT_LOCAL_API_TOKEN']})
        with opener.open(request, timeout=5) as response:
            assert json.load(response)['total'] == 0
        assert invoke('start', config, env).returncode == 0
        assert runtime(config)['backend_pid'] == before['backend_pid']
        assert invoke('stop', config, env).returncode == 0
        assert invoke('stop', config, env).returncode == 0
        state = runtime(config)
        assert all(not alive(before[field]) for field in ('backend_pid', 'client_worker_pid', 'client_pid'))
        assert state['shutdown'] == 'graceful'
        assert state['backend_exit'] == 0
        assert unrelated.poll() is None
        assert not launcher.Kernel(config).running()
        with socket.socket() as sock:
            assert sock.connect_ex(('127.0.0.1', data['port'])) != 0
        # Same SQLite is unlocked/reopened; stale PID metadata is not shutdown authority.
        state['backend_pid'] = unrelated.pid
        launcher.write_json(config.parent / 'runtime.json', state)
        assert invoke('stop', config, env).returncode == 0
        assert unrelated.poll() is None
        assert invoke('start', config, env).returncode == 0
        assert invoke('stop', config, env).returncode == 0
        assert runtime(config)['shutdown'] == 'graceful'
        for path in Path(before['log_directory']).iterdir():
            assert KEY not in path.read_text(encoding='utf-8')
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=5)


def test_port_conflict_does_not_stop_listener(configured):
    config, data, env = configured
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(('127.0.0.1', data['port']))
        listener.listen()
        failed = invoke('start', config, env)
        evidence = startup_evidence(config, failed, env)
        assert failed.returncode == 1, evidence
        assert (config.parent / 'runtime.json').is_file(), evidence
        state = runtime(config)
        assert state['status'] == 'failed' and state['error'] == 'port_in_use', evidence
        assert '"error": "port_in_use"' in failed.stdout, evidence
        assert not {'backend_pid', 'client_worker_pid', 'client_pid'} & state.keys(), evidence
        wait_stopped(config)
        assert not list(config.parent.glob('start-*.json')), evidence
        stopped = invoke('stop', config, env)
        assert stopped.returncode == 0, startup_evidence(config, stopped, env)
        with socket.socket() as probe:
            assert probe.connect_ex(('127.0.0.1', data['port'])) == 0, evidence


def test_bad_backend_configuration_keeps_failure_logs(configured):
    config, data, env = configured
    data['server_args'].append('--unknown-server-option')
    config.write_text(json.dumps(data), encoding='utf-8')
    assert invoke('start', config, env).returncode == 1
    wait_stopped(config)
    state = runtime(config)
    assert state['error'] == 'backend_start_failed'
    log = (Path(state['log_directory']) / 'backend.log').read_text(encoding='utf-8')
    assert log.strip()
    assert KEY not in log


def test_missing_development_tools_do_not_block_chat_startup(configured):
    config,data,env=configured
    for option in ('--cmake','--git','--rg','--clangd'):
        data['server_args'].extend([option,str(config.parent/'does-not-exist.exe')])
    config.write_text(json.dumps(data),encoding='utf-8')
    result=invoke('start',config,env)
    assert result.returncode==0,result.stdout+result.stderr
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(f"http://127.0.0.1:{data['port']}/api/v1/conversations",
        headers={'X-ForgePilot-Token': env['FORGEPILOT_LOCAL_API_TOKEN']})
    with opener.open(request,timeout=5) as response:
        assert json.load(response)['total']==0
    assert invoke('stop',config,env).returncode==0


def test_missing_key_is_explicit(configured):
    config, _, env = configured
    env.pop('FORGEPILOT_API_KEY')
    assert invoke('start', config, env).returncode == 1
    wait_stopped(config)
    assert runtime(config)['error'] == 'missing_api_key'


def test_client_worker_never_receives_api_key(monkeypatch, tmp_path):
    monkeypatch.setenv('FORGEPILOT_API_KEY', KEY)
    captured = {}

    class DummyProcess:
        stdin = io.BytesIO()

    def popen(command, **kwargs):
        captured['command'] = command
        captured.update(kwargs)
        return DummyProcess()

    monkeypatch.setattr(launcher.subprocess, 'Popen', popen)
    job = type('Job', (), {'name': 'test-owned-job'})()
    launcher.owned_client(job, tmp_path / 'client.log', 8765, 'test-local-token')
    assert 'FORGEPILOT_API_KEY' not in captured['env']
    assert KEY not in json.dumps(captured['env'])
    assert captured['env']['FORGEPILOT_LOCAL_API_TOKEN'] == 'test-local-token'
    assert 'test-local-token' not in repr(captured['command'])


def test_fragmented_secret_is_redacted_before_logging():
    stream = io.StringIO()
    writer = launcher.RedactedLog(stream, KEY)
    writer.write('failure: ' + KEY[:14])
    writer.flush()
    assert stream.getvalue() == ''
    writer.write(KEY[14:] + '\n')
    writer.finish()
    assert KEY not in stream.getvalue()
    assert '[REDACTED]' in stream.getvalue()


def test_config_cannot_override_listener(configured):
    config, data, _ = configured
    data['server_args'].extend(['--host=0.0.0.0'])
    config.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(ValueError, match='reserved_server_arg'):
        launcher.read_config(config)


def test_launcher_reads_legacy_partial_complete_and_rejects_invalid_models(tmp_path):
    config = tmp_path / 'config.json'
    base = {'base_url': 'https://provider.invalid/v1', 'port': 8765, 'server_args': []}

    config.write_text(json.dumps({**base, 'model': 'legacy'}), encoding='utf-8')
    assert {name: launcher.read_config(config)[name] for name in
            ('chat_model','task_model','summary_model')} == {
                'chat_model':'legacy','task_model':'legacy','summary_model':'legacy'}

    config.write_text(json.dumps({**base, 'model':'fallback', 'chat_model':'fast'}), encoding='utf-8')
    partial=launcher.read_config(config)
    assert (partial['chat_model'],partial['task_model'],partial['summary_model']) == ('fast','fallback','fast')

    config.write_text(json.dumps({**base, 'chat_model':'chat','task_model':'task',
                                  'summary_model':'summary'}), encoding='utf-8')
    complete=launcher.read_config(config)
    assert (complete['model'],complete['chat_model'],complete['task_model'],complete['summary_model']) == (
        None,'chat','task','summary')

    for invalid in ({**base,'chat_model':'chat'},
                    {**base,'model':'fallback','task_model':'bad\nmodel'},
                    {**base,'model':'fallback','unexpected':'value'}):
        config.write_text(json.dumps(invalid),encoding='utf-8')
        with pytest.raises(ValueError): launcher.read_config(config)


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [200, 401, 403, 429])
async def test_model_check_uses_config_without_history_or_secret_output(configured, monkeypatch, capsys, status):
    config, data, _ = configured
    monkeypatch.setenv('FORGEPILOT_API_KEY', KEY)
    original = config.read_bytes()
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, json={'choices': [{'message': {'role': 'assistant', 'content': KEY},
                                                        'finish_reason': 'stop'}]})

    assert await launcher.check_model(config, transport=httpx.MockTransport(handler)) == (0 if status == 200 else 1)
    out = capsys.readouterr()
    assert KEY not in out.out + out.err
    assert ('检查通过' in out.out) == (status == 200)
    if status != 200:
        assert f'HTTP {status}' in out.err
    assert len(requests) == 1
    assert str(requests[0].url) == data['base_url'] + '/chat/completions'
    assert requests[0].headers['Authorization'] == 'Bearer ' + KEY
    payload = json.loads(requests[0].content)
    assert payload['model'] == data['model'] and 'tools' not in payload
    assert config.read_bytes() == original
    assert not (config.parent / 'runtime.json').exists()
    assert not Path(data['server_args'][1]).exists()


@pytest.mark.asyncio
async def test_model_check_requires_key_and_text_reply(configured, monkeypatch, capsys):
    config, _, _ = configured
    monkeypatch.delenv('FORGEPILOT_API_KEY', raising=False)
    assert await launcher.check_model(config) == 1
    assert '未配置 API Key' in capsys.readouterr().err
    monkeypatch.setenv('FORGEPILOT_API_KEY', KEY)
    body = {'choices': [{'message': {'role': 'assistant', 'tool_calls': [
        {'id': 'unrequested', 'type': 'function', 'function': {'name': 'read_file', 'arguments': '{}'}}]},
        'finish_reason': 'tool_calls'}]}
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    assert await launcher.check_model(config, transport=transport) == 1
    assert 'llm_tool_unknown' in capsys.readouterr().err


@pytest.mark.asyncio
async def test_model_check_validates_each_distinct_model_and_names_failed_purpose(tmp_path,monkeypatch,capsys):
    config=tmp_path/'models.json'
    config.write_text(json.dumps({'base_url':'https://provider.invalid/v1','chat_model':'fast-chat',
        'task_model':'quality-task','summary_model':'cheap-summary','port':8765,'server_args':[]}),encoding='utf-8')
    calls=[]
    def respond(request):
        model = json.loads(request.content)['model']
        calls.append(model)
        if model == 'quality-task':
            return httpx.Response(403, text='private response')
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': 'OK'},
                                                     'finish_reason': 'stop'}]})
    monkeypatch.setenv('FORGEPILOT_API_KEY',KEY)
    assert await launcher.check_model(config, transport=httpx.MockTransport(respond))==1
    output=capsys.readouterr()
    assert calls==['fast-chat','quality-task','cheap-summary']
    assert '任务模型检查失败' in output.err and '聊天模型检查失败' not in output.err
    assert KEY not in output.out+output.err and 'provider.invalid' not in output.out+output.err


@pytest.mark.asyncio
async def test_model_check_names_invalid_configuration_purpose(tmp_path,monkeypatch,capsys):
    config=tmp_path/'invalid-model.json'
    config.write_text(json.dumps({'base_url':'https://must-not-be-printed.invalid/v1','model':'fallback',
        'task_model':'bad\nmodel','port':8765,'server_args':[]}),encoding='utf-8')
    monkeypatch.setenv('FORGEPILOT_API_KEY',KEY)
    assert await launcher.check_model(config)==1
    output=capsys.readouterr().err
    assert '任务模型配置无效' in output
    assert KEY not in output and 'must-not-be-printed.invalid' not in output


@pytest.mark.parametrize('existing_key', [False, True])
def test_gui_saves_offline_and_check_reads_saved_key(tmp_path, existing_key):
    accepted = False
    received = []

    class Endpoint(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.path, self.headers.get('Authorization'),
                             json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            body = json.dumps({'choices': [{'message': {'role': 'assistant', 'content': KEY},
                                           'finish_reason': 'stop'}]}).encode()
            self.send_response(200 if accepted else 401)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Endpoint)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    local = tmp_path / 'local app data'
    identity = hashlib.sha256(str(ROOT).lower().encode('utf-8')).hexdigest()[:16].upper()
    config = local / 'ForgePilot/launcher' / identity / 'config.json'
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({'base_url': f'http://127.0.0.1:{server.server_port}/v1',
                                 'model': 'offline-check', 'port': 8765, 'server_args': []}), encoding='utf-8')
    original_config = config.read_bytes()
    encrypted = config.with_name('api-key.dpapi')
    env = {k: v for k, v in os.environ.items() if k.upper() != 'PSMODULEPATH'}
    env.update(LOCALAPPDATA=str(local), FORGEPILOT_API_KEY=KEY, FORGEPILOT_TEST_KEY_FILE=str(encrypted))

    def powershell(command):
        return subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', command],
                              env=env, capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)

    def entry(action):
        name = 'Configure-ForgePilot.cmd' if action == 'Configure' else 'Check-Model.cmd'
        result = subprocess.run(['cmd.exe', '/d', '/c', str(ROOT / name)], env=env, capture_output=True,
                                timeout=45, stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        assert KEY.encode() not in result.stdout + result.stderr
        return result

    try:
        if existing_key:
            saved = powershell("$s=ConvertTo-SecureString 'previous-test-key' -AsPlainText -Force; "
                               "$s | ConvertFrom-SecureString | Set-Content -Encoding ASCII -LiteralPath $env:FORGEPILOT_TEST_KEY_FILE; $s.Dispose()")
            assert saved.returncode == 0
        original_key = encrypted.read_bytes() if encrypted.exists() else None
        from forgepilot.desktop_configuration import handle_request
        loaded = asyncio.run(handle_request(config, {"action": "load"}))
        fields = {**loaded["fields"], "chat_model": "offline-check", "task_model": "offline-check",
                  "summary_model": "offline-check", "review_model": "offline-review",
                  "task_model_family": "writer", "review_model_family": "reviewer"}
        invalid = asyncio.run(handle_request(config, {"action": "save", "config_revision": loaded["config_revision"],
            "fields": {**fields, "review_model_family": "writer"}, "api_key": KEY}))
        assert not invalid["ok"]
        assert (encrypted.read_bytes() if encrypted.exists() else None) == original_key
        assert config.read_bytes() == original_config
        saved_config = asyncio.run(handle_request(config, {"action": "save", "config_revision": loaded["config_revision"],
            "fields": fields, "api_key": KEY}))
        assert saved_config["ok"] and saved_config["restart_required"]
        assert received == []  # GUI saves do not trigger connection detection.
        data = json.loads(config.read_text(encoding="utf-8"))
        encrypted = config.parent / data["credential_file"]
        env['FORGEPILOT_TEST_KEY_FILE'] = str(encrypted)
        assert KEY not in encrypted.read_text() and KEY not in config.read_text(encoding="utf-8")
        saved = encrypted.read_bytes()
        final_config = config.read_bytes()
        decrypted = powershell("$s=(Get-Content -Raw -LiteralPath $env:FORGEPILOT_TEST_KEY_FILE).Trim() | ConvertTo-SecureString; "
                               "try { if ([Net.NetworkCredential]::new('', $s).Password -cne $env:FORGEPILOT_API_KEY) { exit 2 } } finally { $s.Dispose() }")
        assert decrypted.returncode == 0
        env['FORGEPILOT_API_KEY'] = 'unrelated-inherited-key'
        failure = entry('Check')
        assert failure.returncode != 0
        assert '模型鉴权失败' in (failure.stdout + failure.stderr).decode('utf-8', errors='replace')
        assert len(received) == 2
        assert encrypted.read_bytes() == saved and config.read_bytes() == final_config
        accepted = True
        assert entry('Check').returncode == 0
        assert len(received) == 4
        assert all(path == '/v1/chat/completions' and auth == 'Bearer ' + KEY
                   and body['model'] in {'offline-check', 'offline-review'} for path, auth, body in received)
        assert encrypted.read_bytes() == saved and config.read_bytes() == final_config
        assert not list(config.parent.glob('api-key-*.tmp'))
        assert not (config.parent / 'runtime.json').exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_gui_save_and_explicit_check_use_distinct_purpose_models(tmp_path):
    received=[]
    class Endpoint(BaseHTTPRequestHandler):
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            received.append(body['model'])
            payload=json.dumps({'choices':[{'message':{'role':'assistant','content':'OK'},
                                            'finish_reason':'stop'}]}).encode()
            self.send_response(200); self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(payload))); self.end_headers(); self.wfile.write(payload)
        def log_message(self,*_): pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Endpoint)
    thread=Thread(target=server.serve_forever,daemon=True); thread.start()
    local=tmp_path/'purpose local data'
    identity=hashlib.sha256(str(ROOT).lower().encode('utf-8')).hexdigest()[:16].upper()
    config=local/'ForgePilot/launcher'/identity/'config.json'; config.parent.mkdir(parents=True)
    config.write_text(json.dumps({'base_url':f'http://127.0.0.1:{server.server_port}/v1',
        'model':'legacy','port':8765,'server_args':[]}),encoding='utf-8')
    env={k:v for k,v in os.environ.items() if k.upper()!='PSMODULEPATH'}
    env.update(LOCALAPPDATA=str(local),FORGEPILOT_API_KEY=KEY,
               FORGEPILOT_CHAT_MODEL='fast-chat',FORGEPILOT_TASK_MODEL='quality-task',
               FORGEPILOT_SUMMARY_MODEL='cheap-summary')
    try:
        from forgepilot.desktop_configuration import handle_request
        loaded = asyncio.run(handle_request(config, {"action": "load"}))
        fields = {**loaded["fields"], "chat_model": "fast-chat", "task_model": "quality-task",
                  "summary_model": "cheap-summary", "review_model": "independent-review",
                  "task_model_family": "writer", "review_model_family": "reviewer"}
        saved_result = asyncio.run(handle_request(config, {"action": "save", "fields": fields,
            "api_key": KEY, "config_revision": loaded["config_revision"]}))
        assert saved_result["ok"] and not received
        result=subprocess.run(['cmd.exe','/d','/c',str(ROOT/'Check-Model.cmd')],env=env,
            capture_output=True,timeout=50,stdin=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
        assert result.returncode==0,(result.stdout+result.stderr).decode(errors='replace')
        saved=json.loads(config.read_text(encoding='utf-8-sig'))
        assert [saved[name] for name in ('chat_model','task_model','summary_model')]==[
            'fast-chat','quality-task','cheap-summary']
        assert received==['fast-chat','quality-task','cheap-summary','independent-review']
        assert KEY.encode() not in result.stdout+result.stderr
        assert KEY not in config.read_text(encoding='utf-8-sig')
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)


@pytest.mark.parametrize('bad_backend', [False, True])
def test_cmd_entrypoints_and_dpapi_roundtrip(configured, tmp_path, bad_backend):
    config, data, env = configured
    local = tmp_path / 'local app data 测试 🧪'
    data.update(chat_model=data['model'], task_model=data['model'], summary_model=data['model'],
                review_model='offline-review', task_model_family='writer', review_model_family='reviewer')
    if bad_backend:
        data['server_args'].append('--unknown-server-option')
    identity = hashlib.sha256(str(ROOT).lower().encode('utf-8')).hexdigest()[:16].upper()
    actual = local / 'ForgePilot/launcher' / identity / 'config.json'
    actual.parent.mkdir(parents=True)
    actual.write_text(json.dumps(data), encoding='utf-8')
    encrypted = actual.with_name('api-key.dpapi')
    # Synthetic sentinel only. Test the real Windows account-bound encryption boundary.
    command = "$s=ConvertTo-SecureString $env:FORGEPILOT_API_KEY -AsPlainText -Force; "
    command += "$s | ConvertFrom-SecureString | Set-Content -Encoding ASCII -LiteralPath '"
    command += str(encrypted).replace("'", "''") + "'; $s.Dispose()"
    saved = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', command],
                           env={k: v for k, v in env.items() if k.upper() != 'PSMODULEPATH'},
                           capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
    assert saved.returncode == 0, saved.stderr.decode('utf-8', errors='replace')
    assert KEY not in encrypted.read_text()
    env = {**env, 'LOCALAPPDATA': str(local)}
    env.pop('FORGEPILOT_API_KEY')  # Entry point must decrypt it, not reuse our test environment.

    def entry(action):
        return subprocess.run(['cmd.exe', '/d', '/c', str(ROOT / f'{action}-ForgePilot.cmd')], env=env,
                              cwd=tmp_path, capture_output=True, timeout=55, stdin=subprocess.DEVNULL,
                              creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        started = entry('Start')
        assert started.returncode == 0, started.stdout.decode(errors='replace')
        assert runtime(actual)['status'] == ('failed' if bad_backend else 'running')
        if bad_backend:
            assert b'backend_start_failed' in started.stdout
            assert b'configuration_client_pid' in started.stdout
            import re
            pid = int(re.search(rb'"configuration_client_pid"\s*:\s*(\d+)', started.stdout).group(1))
            close_fixture_window(pid)
        assert KEY.encode() not in started.stdout + started.stderr
        stopped = entry('Stop')
        assert stopped.returncode == 0, stopped.stdout.decode(errors='replace')
        if not bad_backend:
            assert runtime(actual)['shutdown'] == 'graceful'
    finally:
        assert invoke('stop', actual, env).returncode == 0


def fault_launcher(tmp_path, mode):
    """Inject faults at process boundaries; ownership, HTTP and shutdown stay real.

    No production environment switches, executable replacement or user settings.
    The worker still joins the actual Windows Job before spawning its child.
    """
    harness = tmp_path / 'launcher_fault.py'
    harness.write_text(textwrap.dedent('''\
        import json, subprocess, sys, time
        from pathlib import Path
        sys.path.insert(0, SCRIPTS)
        import desktop_launcher as launcher
        launcher.SCRIPT = Path(__file__)
        if MODE in ('delayed_exit', 'no_window', 'cancel'):
            def owned_client(job, log, port, local_token):
                with log.open('wb') as output:
                    process = subprocess.Popen(
                        [str(launcher.PYTHON), '-I', str(launcher.ROOT / 'agent/forgepilot/_build_worker.py'), job.name],
                        stdin=subprocess.PIPE, stdout=output, stderr=output,
                        env=launcher.build_environment(), creationflags=subprocess.CREATE_NO_WINDOW)
                code = 'import time; time.sleep(1.4); raise SystemExit(125)' if MODE == 'delayed_exit' else 'import time; time.sleep(90)'
                command = [str(launcher.PYTHON), '-I', '-c', code]
                process.stdin.write((json.dumps({'command': command, 'cwd': str(log.parent)}) + '\\n').encode())
                process.stdin.close()
                return process
            launcher.owned_client = owned_client
            if MODE == 'no_window':
                launcher.CLIENT_START_TIMEOUT = 0.7
        elif MODE == 'hung_backend':
            def backend(config, job, ready, log):
                launcher.Kernel(config).join(job)
                ready.touch()
                time.sleep(120)
            launcher.backend = backend
        elif MODE == 'owner_exit':
            launcher.supervise = lambda *args: 9
        raise SystemExit(launcher.main())
        ''').replace('SCRIPTS', repr(str(SCRIPT.parent))).replace('MODE', repr(mode)), encoding='utf-8')
    return harness


@pytest.mark.parametrize('mode,error', [('delayed_exit', 'client_start_failed'), ('no_window', 'client_window_timeout')])
def test_start_waits_for_window_and_drains_backend_after_client_failure(configured, tmp_path, mode, error):
    from test_build_process import alive
    import sqlite3

    config, data, env = configured
    harness = fault_launcher(tmp_path, mode)
    result = subprocess.run([sys.executable, '-I', str(harness), 'start', '--config', str(config)],
                            env=env, capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 1
    state = runtime(config)
    assert state['status'] == 'failed' and state['error'] == error
    assert state['shutdown'] == 'graceful' and state['backend_exit'] == 0
    assert not alive(state['backend_pid']) and not alive(state['client_worker_pid'])
    assert KEY.encode() not in result.stdout + result.stderr
    with sqlite3.connect(data['server_args'][1]) as database:
        assert database.execute('PRAGMA quick_check').fetchone() == ('ok',)
    assert not list(config.parent.glob('start-*.json'))


def test_stop_cancels_startup_before_window_and_waits_for_both_workers(configured, tmp_path):
    from test_build_process import alive

    config, _, env = configured
    harness = fault_launcher(tmp_path, 'cancel')
    starting = subprocess.Popen([sys.executable, '-I', str(harness), 'start', '--config', str(config)],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        deadline = time.monotonic() + 15
        while True:
            state_file = config.parent / 'runtime.json'
            if state_file.exists() and 'client_worker_pid' in runtime(config):
                break
            assert starting.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.05)
        assert invoke('stop', config, env).returncode == 0
        output = starting.communicate(timeout=10)
        assert starting.returncode == 1 and KEY.encode() not in b''.join(output)
        state = runtime(config)
        assert state['error'] == 'start_cancelled' and state['shutdown'] == 'graceful'
        assert not alive(state['backend_pid']) and not alive(state['client_worker_pid'])
        assert not launcher.Kernel(config).running()
    finally:
        invoke('stop', config, env)
        starting.wait(timeout=10)


def test_unresponsive_backend_is_force_closed_after_grace_period(configured, tmp_path):
    from test_build_process import alive

    config, _, env = configured
    harness = fault_launcher(tmp_path, 'hung_backend')
    started = subprocess.run([sys.executable, '-I', str(harness), 'start', '--config', str(config)],
                             env=env, capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    assert started.returncode == 0, started.stdout
    before = runtime(config)
    elapsed = time.monotonic()
    stopped = invoke('stop', config, env)
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    assert 25 <= time.monotonic() - elapsed < 40
    assert runtime(config)['shutdown'] == 'forced_after_25_seconds'
    assert all(not alive(before[field]) for field in ('backend_pid', 'client_worker_pid', 'client_pid'))


def test_owner_exiting_before_handshake_fails_promptly(configured, tmp_path):
    config, _, env = configured
    harness = fault_launcher(tmp_path, 'owner_exit')
    started = time.monotonic()
    result = subprocess.run([sys.executable, '-I', str(harness), 'start', '--config', str(config)],
                            env=env, capture_output=True, text=True, timeout=15,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    evidence = startup_evidence(config, result, env)
    assert result.returncode == 1 and 'owner exited' in result.stdout, evidence
    assert 'supervisor_exit=9' in result.stdout, evidence
    assert not (config.parent / 'runtime.json').exists(), evidence
    assert not list(config.parent.glob('start-*.json')), evidence
    assert time.monotonic() - started < 10, evidence
    assert not launcher.Kernel(config).running(), evidence


def test_client_crash_after_start_drains_backend_without_resurrecting_handshake(configured):
    from test_build_process import alive

    config, _, env = configured
    assert invoke('start', config, env).returncode == 0
    before = runtime(config)
    # A handle to the isolated fixture's newly created Qt process, never a user PID.
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = api.OpenProcess(1, False, before['client_pid'])
    assert handle
    try:
        assert api.TerminateProcess(handle, 99)
    finally:
        api.CloseHandle(handle)
    wait_stopped(config)
    state = runtime(config)
    assert state['error'] == 'client_exited' and state['client_exit'] == 99
    assert state['shutdown'] == 'graceful'
    assert all(not alive(before[field]) for field in ('backend_pid', 'client_worker_pid', 'client_pid'))
    assert not list(config.parent.glob('start-*.json'))


def test_normal_window_close_keeps_backend_until_stop(configured):
    config, data, env = configured
    assert invoke('start', config, env).returncode == 0
    pid = runtime(config)['client_pid']
    api = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    api.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    api.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    api.IsWindowVisible.argtypes = [wintypes.HWND]
    api.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    closed = []

    @callback_type
    def visit(window, _):
        owner = wintypes.DWORD()
        api.GetWindowThreadProcessId(window, ctypes.byref(owner))
        if owner.value == pid and api.IsWindowVisible(window):
            closed.append(bool(api.PostMessageW(window, 0x10, 0, 0)))  # WM_CLOSE on fixture window only.
        return True

    assert api.EnumWindows(visit, 0) and closed and all(closed)
    deadline = time.monotonic() + 10
    while runtime(config)['client_status'] != 'closed':
        assert time.monotonic() < deadline
        time.sleep(0.1)
    assert runtime(config)['status'] == 'running' and runtime(config)['client_exit'] == 0
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(f"http://127.0.0.1:{data['port']}/api/v1/tasks?limit=1",
        headers={'X-ForgePilot-Token': env['FORGEPILOT_LOCAL_API_TOKEN']})
    with opener.open(request, timeout=5) as response:
        assert json.load(response)['total'] == 0
    assert invoke('stop', config, env).returncode == 0
    assert runtime(config)['shutdown'] == 'graceful'


@pytest.mark.asyncio
async def test_managed_shutdown_cancels_pending_plan_and_keeps_sqlite(cpp_repo, rg_exe, git_exe, tmp_path):
    from forgepilot.api import create_app
    from forgepilot.server import _serve_managed
    from forgepilot.sqlite_store import SqliteEventStore
    from forgepilot.task_service import TaskService
    from conftest import ScriptedLLM
    from test_api import messages, until

    before = (cpp_repo / 'main.cpp').read_bytes()
    database = tmp_path / 'managed-shutdown.sqlite3'
    store = SqliteEventStore(database)
    app = create_app(lambda trace: TaskService(ScriptedLLM(messages()), rg_executable=rg_exe,
                                               git_executable=git_exe, trace=trace), store=store)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    ready = asyncio.Event()
    stopping = asyncio.Event()
    serving = asyncio.create_task(_serve_managed(app, dict(host='127.0.0.1', port=port, log_level='error',
                                                         access_log=False), stopping.is_set, ready.set))
    try:
        await asyncio.wait_for(ready.wait(), 10)
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}', trust_env=False) as client:
            response = await client.post('/api/v1/tasks', json={'repository_path': str(cpp_repo), 'prompt': 'fix'})
            assert response.status_code == 202
            task_id = response.json()['task_id']
            await until(client, task_id, {'waiting_approval'})
        stopping.set()
        await asyncio.wait_for(serving, 15)
        assert store.get(task_id)['state'] == 'cancelled'
        assert store.get(task_id)['finished_at']
        assert (cpp_repo / 'main.cpp').read_bytes() == before
    finally:
        stopping.set()
        await asyncio.wait_for(serving, 15)
        store.close()
    reopened = SqliteEventStore(database)
    try:
        assert reopened.get(task_id)['state'] == 'cancelled'
    finally:
        reopened.close()
