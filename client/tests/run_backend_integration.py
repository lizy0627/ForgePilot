"""Real FastAPI + Qt/QML + CMake acceptance; live models require manual approval."""
import asyncio
import argparse
import difflib
import hashlib
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from threading import Event, Thread
import time
from uuid import uuid4

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agent"))

from forgepilot.api import create_app
from forgepilot.errors import ForgePilotError
from forgepilot.llm import AssistantMessage, ChatReply, FunctionCall, ToolCall, OpenAICompatibleClient
from forgepilot.presets import BuildConfig
from forgepilot.process import executable
from forgepilot.task_service import TaskService
from forgepilot.sqlite_store import SqliteEventStore
from forgepilot.chat_service import ChatService


class OfflineChatModel:
    def __init__(self, cleanup_delay=0, captures=None): self.cleanup_delay,self.captures=cleanup_delay,captures
    async def chat(self,messages,tools=None):
        if self.captures is not None: self.captures.append(dict(messages=deepcopy(messages),tools=[t.model_dump() for t in tools] if tools else None))
        prompt=next(m['content'] for m in reversed(messages) if m['role']=='user')
        await asyncio.sleep(30 if '慢回答 fixture' in prompt else 1.2)
        if 'fixture chat failure' in prompt:
            raise ForgePilotError('llm_http','离线问答失败样例')
        if prompt=='只读检查 main.cpp fixture':
            if messages[-1]['role']!='tool':
                return ChatReply(message=AssistantMessage(role='assistant',tool_calls=[ToolCall(id='readonly-main',type='function',function=FunctionCall(name='read_file',arguments='{"path":"main.cpp"}'))]),usage=None)
            assert 'return 1' in messages[-1]['content']
            return ChatReply(message=AssistantMessage(role='assistant',content='只读文件已检查：main.cpp 仍返回 1，未修改或构建。'),usage=None)
        first=next(m['content'] for m in messages if m['role']=='user')
        return ChatReply(message=AssistantMessage(role='assistant',content='离线问答样例：记得本对话的第一条消息：'+first),usage=None)

    async def aclose(self): await asyncio.sleep(self.cleanup_delay)


class OfflineModel:
    def __init__(self):
        self.step = 0

    async def chat(self, messages, tools=None):
        await asyncio.sleep(0.15)  # Allow the real Qt UI to observe asynchronous transitions.
        if any(m.get("role") == "user" and "fixture failure" in str(m.get("content")) for m in messages):
            raise ForgePilotError("llm_http", "HTTP 401. Offline credential-error fixture")
        self.step += 1
        if self.step == 1:
            return ChatReply(message=AssistantMessage(role="assistant", content="main.cpp 返回错误退出码；保留原有测试。"), usage=None)
        if self.step == 2:
            plan = dict(summary="修复程序返回值（开发测试：离线模型 fixture）", root_cause="main.cpp 返回 1", files_to_modify=[dict(path="main.cpp", reason="修复生产实现"), dict(path="result.h", reason="新增返回值定义")],
                        implementation_steps=["新增 result.h 并从 main.cpp 引用，保留现有用户注释与测试"], validation_steps=["Configure、Build、CTest"], risks=["构建脚本会在本机运行"])
            return ChatReply(message=AssistantMessage(role="assistant", content=json.dumps(plan, ensure_ascii=False)), usage=None)
        if self.step == 3:
            # R1 requires an explicit read before the first overwrite of an existing file.
            return ChatReply(message=AssistantMessage(role="assistant", tool_calls=[ToolCall(
                id="offline-read", type="function", function=FunctionCall(name="read_file",
                arguments='{"path":"main.cpp"}'))]), usage=None)
        if self.step == 4:
            calls = [ToolCall(id="offline-header", type="function", function=FunctionCall(name="write_file",
                arguments=json.dumps(dict(path="result.h", content="#pragma once\ninline constexpr int result = 0;\n")))),
                ToolCall(id="offline-write", type="function", function=FunctionCall(name="write_file",
                arguments=json.dumps(dict(path="main.cpp", content='#include "result.h"\nint main(){ return result; }\n// user note\n'))))]
            return ChatReply(message=AssistantMessage(role="assistant", tool_calls=calls), usage=None)
        return ChatReply(message=AssistantMessage(role="assistant", content="实现已修复，交由真实构建测试验证。"), usage=None)


class FeedbackModel(OfflineModel):
    """Explicit feedback scenario; preserve OfflineModel's original execution order."""
    def __init__(self, gates):
        super().__init__()
        self.feedback_step = 0
        self.release = Event()
        gates.append(self.release)

    async def chat(self, messages, tools=None):
        evidence = next((m['content'] for m in reversed(messages) if m.get('role') == 'user'
            and str(m.get('content', '')).startswith('Current plan and task evidence (untrusted data):\n')), None)
        if evidence is not None and self.feedback_step < 2:
            self.feedback_step += 1
            if self.feedback_step == 1:
                try:
                    assert await asyncio.to_thread(self.release.wait, 30), 'QML did not release the feedback fixture'
                except asyncio.CancelledError:
                    self.release.set()
                    raise
                return ChatReply(message=AssistantMessage(role='assistant', content='按意见保留公开接口和用户改动，等待重新批准。'), usage=None)
            plan = json.loads(evidence.split('\n', 1)[1])['current_plan']
            plan['summary'] = '根据修改意见调整实现，保留公开接口'
            return ChatReply(message=AssistantMessage(role='assistant', content=json.dumps(plan, ensure_ascii=False)), usage=None)
        return await super().chat(messages, tools)


JOINT_OLD = '    return quantity + unit_price;'
JOINT_NEW = '    return quantity * unit_price;'


def prepare_joint_fixture(repository, output, git):
    """Large CRLF/BOM source, unchanged public header/tests and pre-task user bytes."""
    sources = {
        'CMakeLists.txt': 'cmake_minimum_required(VERSION 3.24)\nproject(LocalEditJoint LANGUAGES CXX)\n'
            'set(CMAKE_CXX_STANDARD 20)\nadd_executable(counter tests/counter_test.cpp main.cpp)\n'
            'target_include_directories(counter PRIVATE .)\nenable_testing()\nadd_test(NAME counter_test COMMAND counter)\n',
        'counter.h': '#pragma once\nint total_price(int quantity, int unit_price);\n',
        'tests/counter_test.cpp': '#include "counter.h"\n#include <cstdio>\nint main() {\n'
            '    if (total_price(3, 7) != 21 || total_price(0, 7) != 0 || total_price(-2, 7) != 0) {\n'
            '        std::fputs("total_price boundary failed\\n", stderr); return 1;\n    }\n    return 0;\n}\n',
    }
    for name, text in sources.items():
        target = repository/name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode('utf-8'))
    body = '#include "counter.h"\r\n// 金额计算：公开接口必须保持不变。\r\n'
    body += 'int total_price(int quantity, int unit_price) {\r\n    if (quantity <= 0) return 0;\r\n'+JOINT_OLD+'\r\n}\r\n'
    body += ''.join(f'// 保留背景说明 {i:04d}：这是非目标中文注释。'+('unchanged-context-'*3)+'\r\n' for i in range(400))
    original = b'\xef\xbb\xbf'+body.encode('utf-8')
    (repository/'main.cpp').write_bytes(original)
    subprocess.run([git, '-C', str(repository), 'add', 'CMakeLists.txt', 'counter.h', 'main.cpp', 'tests/counter_test.cpp'], check=True)
    baseline = original.replace(b'int total_price(', '// 用户原有改动：保留此审阅备注。\r\nint total_price('.encode('utf-8'), 1)
    assert 30*1024 <= len(baseline) <= 60*1024 and len(baseline.decode('utf-8')) > 12000
    (repository/'main.cpp').write_bytes(baseline)
    saved = output/'byte-baseline'
    saved.mkdir()
    for name in (*sources, 'main.cpp'):
        target = saved/name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((repository/name).read_bytes())
    (output/'source-before-user-change.cpp').write_bytes(original)
    return baseline


class JointModel:
    """One explicit scenario; does not alter either older model's reply sequence."""
    def __init__(self, gates, captures):
        self.stage = 0
        self.release = Event()
        gates.append(self.release)
        self.captures = captures

    async def chat(self, messages, tools=None):
        self.captures.append(dict(stage=self.stage, messages=deepcopy(messages),
            tools=[tool.model_dump() for tool in tools] if tools else None))
        stage = self.stage
        self.stage += 1
        if stage == 3:
            try:
                assert await asyncio.to_thread(self.release.wait, 30), 'QML must observe planning before releasing feedback'
            except asyncio.CancelledError:
                self.release.set()
                raise
        if stage in (0, 3, 6):
            message = AssistantMessage(role='assistant', tool_calls=[ToolCall(id=f'joint-read-{stage}', type='function',
                function=FunctionCall(name='read_file', arguments=json.dumps(dict(path='main.cpp', start_line=1, end_line=20))))])
        elif stage in (2, 5):
            assert tools is None
            plan = dict(summary='修复 total_price 的金额计算' if stage == 2 else '保留公开接口，只修改 total_price 函数实现',
                root_cause='数量与单价被相加，正确结果应为乘积', files_to_modify=[dict(path='main.cpp', reason='只修正 total_price 的返回表达式')],
                implementation_steps=['read_file 读取局部上下文；edit_file 精确替换函数内的加法为乘法，保留非目标字节和用户备注'],
                validation_steps=['运行真实 CMake Configure、Build 和 CTest；保护 tests/counter_test.cpp'], risks=['构建在本机执行'])
            message = AssistantMessage(role='assistant', content=json.dumps(plan, ensure_ascii=False))
        elif stage == 7:
            message = AssistantMessage(role='assistant', tool_calls=[ToolCall(id='joint-exact-edit', type='function',
                function=FunctionCall(name='edit_file', arguments=json.dumps(dict(path='main.cpp', edits=[dict(old_text=JOINT_OLD, new_text=JOINT_NEW)]))))])
        else:
            assert stage in (1, 4, 8), 'Unexpected model request in the fixed joint scenario'
            message = AssistantMessage(role='assistant', content='仅修正函数内返回表达式，保护公开接口、用户备注和现有测试。')
        return ChatReply(message=message, usage=None)


def verify_joint(repository, output, store, task, captures, requests):
    baseline = (output/'byte-baseline/main.cpp').read_bytes()
    actual = (repository/'main.cpp').read_bytes()
    old, new = JOINT_OLD.encode(), JOINT_NEW.encode()
    assert baseline.count(old) == 1 and actual == baseline.replace(old, new, 1)
    start = baseline.index(old)
    assert actual[:start] == baseline[:start] and actual[start+len(new):] == baseline[start+len(old):]
    for name in ('CMakeLists.txt', 'counter.h', 'tests/counter_test.cpp'):
        assert (repository/name).read_bytes() == (output/'byte-baseline'/name).read_bytes()
    assert task['state'] == 'completed' and task['plan_version'] == task['approved_plan_version'] == 2
    assert task['build_result']['tests']['total'] >= 1 and task['build_result']['tests']['failed'] == 0
    files = task['task_diff']['files']
    assert len(files) == 1 and files[0]['path'] == 'main.cpp' and not files[0]['created']
    assert files[0]['original_sha256'] == hashlib.sha256(baseline).hexdigest()
    expected_diff = ''.join(difflib.unified_diff(baseline.decode('utf-8').splitlines(True), actual.decode('utf-8').splitlines(True), fromfile='before/main.cpp', tofile='agent/main.cpp'))
    # Public Diff is redacted for display (CR control characters are removed).
    # The independent byte equality above verifies that disk CRLF/BOM survive.
    assert files[0]['diff'] == expected_diff.replace('\r', '') and not task['task_diff']['truncated']
    traces = store.trace_page(task['task_id'], limit=100)['items']
    details = [store.trace_detail(task['task_id'], row['sequence']) for row in traces]
    kinds = [row['kind'] for row in details]
    assert kinds.count('plan_feedback') == 1
    assert [r['data']['plan_version'] for r in details if r['kind']=='plan_created'] == [1, 2]
    approved = next(i for i,r in enumerate(details) if r['kind']=='plan_approved')
    assert not any(r['kind'] in {'file_changed','build_started','test_started','build_command_finished'} for r in details[:approved])
    started = [r['data'] for r in details if r['kind']=='tool_started']
    assert [r['name'] for r in started] == ['read_file','read_file','read_file','edit_file',
                                           'cmake_configure','cmake_build','run_tests']
    assert all(r.get('source') == 'runtime' for r in started[4:])
    assert not any('write_file' == r['name'] for r in started)
    assert [r['status'] for r in requests if r['path'].endswith('/approve')] == [409, 202]
    assert [r['status'] for r in requests if r['path'].endswith('/feedback')] == [202]
    (output/'source-after.cpp').write_bytes(actual)
    (output/'joint.diff').write_bytes(expected_diff.encode('utf-8'))
    (output/'joint-trace.json').write_text(json.dumps(details, ensure_ascii=False, indent=2), 'utf-8')
    (output/'joint-model-requests.json').write_text(json.dumps(captures, ensure_ascii=False, indent=2), 'utf-8')
    (output/'joint-summary.json').write_text(json.dumps(dict(model='JointModel (offline fixed replies)', task_id=task['task_id'],
        source_bytes=len(baseline), plan_versions=[1,2], approved_version=2, original_sha256=files[0]['original_sha256'],
        final_sha256=hashlib.sha256(actual).hexdigest(), non_target_bytes_equal=True, protected_files_equal=True,
        tools=[r['name'] for r in started], tests=task['build_result']['tests']), ensure_ascii=False, indent=2), 'utf-8')


def main():
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manual", action="store_true")
    parser.add_argument("--live-model", action="store_true", help="Only with --manual --scenario joint; use explicit process model configuration, never automated approval")
    parser.add_argument("--scenario", choices=("baseline", "feedback", "joint"), default="baseline", help="Explicit offline task scenario; baseline reply order is unchanged")
    parser.add_argument("--conversations", action="store_true", help="Run phase 2 conversation UI and restart acceptance")
    parser.add_argument("--deletion", action="store_true", help="Run phase 3 deletion UI and restart acceptance using isolated history")
    parser.add_argument("--memory", action="store_true", help="Run phase 4 memory UI and captured request acceptance")
    parser.add_argument("--mcp", action="store_true", help="Run MCP settings against real FastAPI, SQLite and MCP peers")
    parser.add_argument("--mcp-runtime", action="store_true", help="Run MCP settings, chat, sources and history through the production loops")
    parser.add_argument("--mcp-live-model", action="store_true", help="Use explicitly configured FORGEPILOT model credentials with an isolated MCP fixture (not live Tavily)")
    parser.add_argument("--output-root", type=Path, default=root / "logs/client-integration")
    args = parser.parse_args()
    live_settings = {}
    if args.live_model:
        if not args.manual or args.scenario != 'joint' or any((args.conversations,args.deletion,args.memory,args.mcp,args.mcp_runtime,args.mcp_live_model)):
            parser.error('--live-model requires only --manual --scenario joint; automated live approval is forbidden')
        live_settings = {key: os.environ.get('FORGEPILOT_'+key.upper(), '') for key in ('base_url','api_key','model')}
        if not all(live_settings.values()):
            parser.error('Explicit model configuration is required; credentials are never searched or printed')
    if args.mcp_runtime or args.mcp_live_model:
        from run_mcp_runtime_integration import run
        return run(root,args.output_root,live_model=args.mcp_live_model)
    if args.mcp:
        from run_mcp_integration import run
        return run(root,args.output_root)
    if args.memory:
        from run_memory_integration import run
        return run(root,args.output_root)
    output = args.output_root.resolve() / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
    output.mkdir(parents=True, exist_ok=False)
    print(("REAL MODEL MANUAL" if args.live_model else "OFFLINE MODEL")+" integration evidence: " + str(output), flush=True)
    (output/'model-mode.json').write_text(json.dumps(dict(mode='real_manual' if args.live_model else 'offline',
        model=live_settings.get('model', {'joint':'JointModel','feedback':'FeedbackModel'}.get(args.scenario,'OfflineModel')),
        automatic_approval=not args.manual), ensure_ascii=False, indent=2), 'utf-8')
    cmake = executable(os.environ.get("FORGEPILOT_CMAKE"), "cmake")
    rg = executable(os.environ.get("FORGEPILOT_RG"), "rg")
    git = executable(os.environ.get("FORGEPILOT_GIT"), "git")
    # Use the installed VS generator; the wrapper supplies the original x64 toolchain environment.
    config = BuildConfig(cmake, str(Path(cmake).with_name("ctest.exe")), generator="Visual Studio 17 2022", architecture="x64")
    database=output/("ui-history-"+uuid4().hex+".sqlite3")
    store=SqliteEventStore(database)
    chat_captures=[]
    chat_factory=lambda: ChatService(OfflineChatModel(.6 if args.deletion else 0,chat_captures),rg_executable=rg,git_executable=git)
    feedback_gates=[]
    joint_captures=[]
    app = create_app(lambda trace: TaskService(OpenAICompatibleClient(**live_settings,timeout=120) if args.live_model else JointModel(feedback_gates,joint_captures) if args.scenario=='joint' else FeedbackModel(feedback_gates) if args.scenario=='feedback' else OfflineModel(),
        rg_executable=rg, git_executable=git, trace=trace, build_config=config),store=store,chat_factory=chat_factory)
    if args.scenario in ('feedback', 'joint') and not args.live_model:
        @app.post('/_fixture/release-feedback')
        async def release_feedback():
            feedback_gates[-1].set()
            return {'released': True}
    request_counts=[]
    def capture_requests(app,phase):
        @app.middleware('http')
        async def capture(request,call_next):
            response=await call_next(request)
            request_counts.append(dict(phase=phase,method=request.method,path=request.url.path,status=response.status_code))
            return response
    capture_requests(app,'initial')
    binary=root/'client/build/Release'/('ForgePilotDeletionTests.exe' if args.deletion else 'ForgePilotConversationTests.exe' if args.conversations else 'ForgePilotBackendTests.exe')
    manual = args.manual
    with tempfile.TemporaryDirectory(prefix="ForgePilot 开发测试 ") as temporary:
        repository = Path(temporary)
        subprocess.run([git,"init","-q",str(repository)], check=True)
        (repository/"CMakeLists.txt").write_text('cmake_minimum_required(VERSION 3.24)\nproject(QtCommunicationFixture LANGUAGES CXX)\nset(CMAKE_CXX_STANDARD 20)\nadd_executable(counter main.cpp)\nenable_testing()\nadd_test(NAME counter_test COMMAND counter)\n',encoding="utf-8")
        (repository/"main.cpp").write_text("int main(){ return 1; }\n",encoding="utf-8",newline="\n")
        subprocess.run([git,"-C",str(repository),"add","CMakeLists.txt","main.cpp"],check=True)
        # The working tree contains user changes before the task; never commit in this test.
        (repository/"main.cpp").write_text("int main(){ return 1; }\n// user note\n",encoding="utf-8",newline="\n")
        if args.scenario == 'joint':
            prepare_joint_fixture(repository, output, git)
        (repository/"user-only.txt").write_text("用户任务前已有文件，不应计入 Agent Diff。",encoding="utf-8")
        for name in ("build", ".venv", "node_modules"):
            (repository/name).mkdir(); (repository/name/"excluded.txt").write_text("excluded",encoding="utf-8")
        sock = socket.socket(); sock.bind(("127.0.0.1",0)); port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="warning", timeout_graceful_shutdown=2))
        thread = Thread(target=lambda: server.run(sockets=[sock]))
        thread.start()
        try:
            deadline = time.monotonic()+10
            while not server.started:
                if not thread.is_alive() or time.monotonic()>deadline: raise RuntimeError("Backend did not start")
                time.sleep(0.01)
            env = os.environ.copy()
            env.update(FORGEPILOT_INTEGRATION_URL=f"http://127.0.0.1:{port}", FORGEPILOT_INTEGRATION_REPOSITORY=str(repository),
                       FORGEPILOT_INTEGRATION_QML=str(root/"client/Main.qml"), FORGEPILOT_INTEGRATION_OUTPUT=str(output),
                       QT_QUICK_CONTROLS_STYLE="Basic")
            env['FORGEPILOT_INTEGRATION_SCENARIO']=args.scenario
            if manual:
                stop = output/"manual-stop"
                if stop.exists(): raise RuntimeError("Remove the prior manual-stop marker before another manual run")
                (output/"manual-ready.json").write_text(json.dumps(dict(url=env["FORGEPILOT_INTEGRATION_URL"],repository=str(repository),
                    notice="真实模型；每一版方案由人审阅，新方案必须人工批准" if args.live_model else "开发测试：OfflineModel fixture，真实 FastAPI/CMake/CTest"),ensure_ascii=False),encoding="utf-8")
                print("MANUAL DEVELOPMENT TEST ready: " + env["FORGEPILOT_INTEGRATION_URL"], flush=True)
                deadline=time.monotonic()+1200
                while not stop.exists() and time.monotonic()<deadline: time.sleep(0.2)
                records=[]
                for task in app.state.task_host.store.list():
                    events,closed=app.state.task_host.store.read(task["task_id"],0)
                    records.append(dict(task=task,events=[dict(id=e.id,payload=e.payload.model_dump()) for e in events],closed=closed))
                (output/"manual-backend-evidence.json").write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding="utf-8")
                if args.scenario == 'joint':
                    (output/'source-after.cpp').write_bytes((repository/'main.cpp').read_bytes())
                    for task in store.list():
                        rows=store.trace_page(task['task_id'],limit=100)['items']
                        details=[store.trace_detail(task['task_id'],row['sequence']) for row in rows]
                        (output/('manual-trace-'+task['task_id']+'.json')).write_text(json.dumps(details,ensure_ascii=False,indent=2),'utf-8')
                    (output/'manual-result.json').write_text(json.dumps(dict(status='requires_human_evidence_review',
                        task_states=[r['task']['state'] for r in records], automatic_approval=False,
                        note='Exit 0 only closes the manual session; it is not an acceptance pass'),indent=2),'utf-8')
                return 0
            report = output/"qml-test-results.txt"
            run = subprocess.run([str(binary),"-v1","-o",str(report)+",txt"], env=env, capture_output=True, timeout=180)
            detail = report.read_bytes() if report.exists() else b""
            (output/"qml-backend.log").write_bytes(detail+run.stdout+run.stderr)
            print((detail+run.stdout+run.stderr).decode("utf-8",errors="replace"))
            store=app.state.task_host.store
            records=[]
            for task in store.list():
                events,closed=store.read(task["task_id"],0)
                records.append(dict(task=task,events=[dict(id=e.id, payload=e.payload.model_dump()) for e in events],closed=closed))
            (output/"qml-backend-evidence.json").write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding="utf-8")
            if run.returncode: return run.returncode
            expected=['completed'] if args.scenario=='joint' else [] if args.deletion else ['cancelled','cancelled','completed'] if args.conversations else ["cancelled","cancelled","completed","failed","failed"]
            assert sorted(r["task"]["state"] for r in records)==expected
            if args.scenario == 'joint':
                complete=records[0]['task']
                verify_joint(repository, output, store, complete, joint_captures, request_counts)
                print('JOINT: large-file feedback v1/v2, explicit approval, read_file/edit_file, exact bytes, protected tests and real CTest passed')
            else:
                assert (repository/"main.cpp").read_text("utf-8")=='#include "result.h"\nint main(){ return result; }\n// user note\n'
            if args.deletion:
                keep=(output/'deletion-keep-id.txt').read_text('utf-8')
                def deletion_evidence():
                    counts={table:store._db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in
                        ('conversations','conversation_requests','conversation_messages','tasks','task_events','tool_calls')}
                    assert counts==dict(conversations=1,conversation_requests=1,conversation_messages=2,tasks=0,task_events=0,tool_calls=0)
                    assert store._db.execute('PRAGMA foreign_key_check').fetchall()==[]
                    detail=store.conversation_detail(keep)
                    assert detail['title']=='保留的对话'
                    assert not app.state.task_host._tasks and not app.state.task_host._repositories
                    return dict(counts=counts,conversation=detail)
                preserved=deletion_evidence()
                assert (repository/'result.h').read_text('utf-8')=='#pragma once\ninline constexpr int result = 0;\n'
                assert (repository/'user-only.txt').read_text('utf-8')=='用户任务前已有文件，不应计入 Agent Diff。'
                (output/'deletion-evidence.json').write_text(json.dumps(preserved,ensure_ascii=False,indent=2),encoding='utf-8')
                print('REAL QT/QML + FASTAPI: rename, keep, delete non-current/current/running conversations; tasks/events/tools removed; project files retained')
            elif args.scenario != 'joint':
                complete=next(r["task"] for r in records if r["task"]["state"]=="completed")
                assert complete["build_result"]["tests"]["passed"]==1
                files={f["path"]:f for f in complete["task_diff"]["files"]}
                assert set(files)=={"main.cpp","result.h"}
                assert files["result.h"]["created"] and not files["main.cpp"]["created"]
                assert "+// user note" not in files["main.cpp"]["diff"]
                assert " // user note" in files["main.cpp"]["diff"]
                print("REAL QT/QML + FASTAPI: create, list, query, approve, MSVC build/CTest, cancel, reject, failure, terminal display passed")
                if args.scenario == 'feedback':
                    assert complete['plan_version'] == complete['approved_plan_version'] == 2
                    feedback_posts=[r for r in request_counts if r['method']=='POST' and r['path'].endswith('/feedback')]
                    assert len(feedback_posts)==1 and feedback_posts[0]['status']==202
                    timeline=store.trace_page(complete['task_id'],limit=100)['items']
                    details=[store.trace_detail(complete['task_id'],r['sequence']) for r in timeline if r['kind'] in {'plan_feedback','plan_created'}]
                    assert [r['data']['plan_version'] for r in details if r['kind']=='plan_created']==[1,2]
                    assert sum(r['kind']=='plan_feedback' for r in details)==1
                    (output/'feedback-trace-evidence.json').write_text(json.dumps(details,ensure_ascii=False,indent=2),encoding='utf-8')
                    print('FEEDBACK SCENARIO: one POST 202, one feedback trace, full v1/v2 plans, explicit v2 approval, real build/test passed')
            server.should_exit=True; thread.join(timeout=20); sock.close()
            if thread.is_alive(): raise RuntimeError("First backend failed to stop")
            store.close()
            # Start a fresh HTTP/Runtime host over the same disk database. No execution restoration.
            store=SqliteEventStore(database)
            def no_execution(trace): raise AssertionError("Historical view must not construct an Agent")
            app=create_app(no_execution,store=store,chat_factory=chat_factory)
            capture_requests(app,'restart')
            sock=socket.socket(); sock.bind(("127.0.0.1",0))
            env.update(FORGEPILOT_INTEGRATION_URL=f"http://127.0.0.1:{sock.getsockname()[1]}")
            if args.deletion: env['FORGEPILOT_RESTART_DELETION']=keep
            else: env['FORGEPILOT_RESTART_TASK_ID']=complete['task_id']
            if not args.deletion:
                conversation_file='conversation-id.txt' if args.conversations else 'backend-conversation-id.txt'
                env['FORGEPILOT_RESTART_CONVERSATION_ID']=(output/conversation_file).read_text('utf-8')
            server=uvicorn.Server(uvicorn.Config(app,access_log=False,log_level="warning",timeout_graceful_shutdown=2))
            thread=Thread(target=lambda:server.run(sockets=[sock])); thread.start()
            deadline=time.monotonic()+10
            while not server.started:
                if not thread.is_alive() or time.monotonic()>deadline: raise RuntimeError("Restarted backend did not start")
                time.sleep(.01)
            report=output/"qml-restart-results.txt"
            run=subprocess.run([str(binary),"-v1","-o",str(report)+",txt"],env=env,capture_output=True,timeout=60)
            detail=report.read_bytes() if report.exists() else b""
            (output/"qml-restart.log").write_bytes(detail+run.stdout+run.stderr)
            print((detail+run.stdout+run.stderr).decode("utf-8",errors="replace"))
            if run.returncode: return run.returncode
            if args.conversations:
                for capture in chat_captures:
                    assert {tool['name'] for tool in capture['tools'] or []}<={'list_files','read_file','search_code'}
                assert any(m['role']=='tool' and 'return 1' in m['content'] for c in chat_captures for m in c['messages'])
                assert chat_captures[0]['tools'] is None
                (output/'actual-chat-requests.json').write_text(json.dumps(chat_captures,ensure_ascii=False,indent=2),encoding='utf-8')
            if args.deletion:
                assert deletion_evidence()==preserved
                (output/'deletion-restart-evidence.json').write_text(json.dumps(preserved,ensure_ascii=False,indent=2),encoding='utf-8')
                print('RESTART + QT HISTORY: deleted records stay absent; other conversation unchanged; no orphan records')
            else:
                saved=store.get(complete["task_id"])
                assert saved["read_only"] and saved["task_diff"]==complete["task_diff"] and saved["state"]=="completed"
                assert store._db.execute("PRAGMA foreign_key_check").fetchall()==[]
                (output/"ui-restart-evidence.json").write_text(json.dumps(dict(database=str(database),task=saved,tasks=store.list_page()),ensure_ascii=False,indent=2),encoding="utf-8")
                print("RESTART + QT HISTORY: persistent list, completed details, actual diff, trace detail, read-only controls passed")
        finally:
            server.should_exit=True; thread.join(timeout=20); sock.close()
            (output/'http-requests.json').write_text(json.dumps(request_counts,indent=2),encoding='utf-8')
            if thread.is_alive(): raise RuntimeError("Backend failed to stop")
            store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
