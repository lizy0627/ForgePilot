import asyncio
import hashlib
import json
import os
import subprocess
import sys

import pytest

from forgepilot.errors import ForgePilotError
from forgepilot.process import executable, run_process
from forgepilot.repository import MAX_FILE_BYTES, MAX_OUTPUT_CHARS, MAX_SEARCH_BYTES, Repository
from forgepilot.tools import ToolRegistry, register_repository_tools


async def registry_for(root, rg_exe, git_exe):
    repo = await Repository.open(str(root), rg_executable=rg_exe, git_executable=git_exe)
    registry = ToolRegistry()
    register_repository_tools(registry, repo)
    return registry


@pytest.mark.asyncio
async def test_read_versions_use_complete_raw_bytes_and_normalized_paths(cpp_repo, rg_exe, git_exe):
    raw = b"\xef\xbb\xbf// first\r\n// second\r\n"
    (cpp_repo / "main.cpp").write_bytes(raw)
    repo = await Repository.open(str(cpp_repo), rg_executable=rg_exe, git_executable=git_exe)
    result = json.loads(await repo.read_file(".\\MAIN.CPP", start_line=2, end_line=2))
    assert result["content"] == "2: // second" and result["limits"] == ["line_range"]
    assert repo._read_versions == {"main.cpp": hashlib.sha256(raw).hexdigest()}
    other = await Repository.open(str(cpp_repo), rg_executable=rg_exe, git_executable=git_exe)
    assert other._read_versions == {}
    # Internal reads used by search and clangd neither create nor refresh baselines.
    changed = b"// changed\n"
    (cpp_repo / "main.cpp").write_bytes(changed)
    assert repo._read("main.cpp") == (changed.decode(), False)
    assert json.loads(await repo.search_code("changed", "main.cpp"))["matches"]
    assert repo._read_versions == {"main.cpp": hashlib.sha256(raw).hexdigest()}
    assert other._read("main.cpp") == (changed.decode(), False)
    await other.search_code("changed", "main.cpp")
    assert other._read_versions == {}
    await repo.read_file("main.cpp")
    assert repo._read_versions == {"main.cpp": hashlib.sha256(changed).hexdigest()}


@pytest.mark.asyncio
async def test_read_byte_limit_invalidates_old_version_but_output_limit_does_not(cpp_repo, rg_exe, git_exe):
    target = cpp_repo / "main.cpp"
    repo = await Repository.open(str(cpp_repo), rg_executable=rg_exe, git_executable=git_exe)
    raw = b"x" * MAX_FILE_BYTES
    target.write_bytes(raw)
    result = json.loads(await repo.read_file("main.cpp"))
    assert result["limits"] == ["output_limit"]
    assert repo._read_versions == {"main.cpp": hashlib.sha256(raw).hexdigest()}
    target.write_bytes(raw + b"x")
    result = json.loads(await repo.read_file(".\\MAIN.CPP"))
    assert "file_byte_limit" in result["limits"]
    assert repo._read_versions == {}


@pytest.mark.asyncio
async def test_listing_reading_and_literal_search(cpp_repo, rg_exe, git_exe, monkeypatch, tmp_path):
    config = tmp_path / "rg-config"
    config.write_text("--glob=!*.cpp\n", encoding="utf-8")
    monkeypatch.setenv("RIPGREP_CONFIG_PATH", str(config))
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "wrong-git-dir"))
    registry = await registry_for(cpp_repo, rg_exe, git_exe)
    listing = await registry.execute("list_files", {})
    assert set(json.loads(listing.output)["files"]) == {"App.cpp", "CMakeLists.txt", "Counter.cpp", "Counter.hpp", "main.cpp", "notes.txt"}
    read = await registry.execute("read_file", {"path": "Counter.cpp", "start_line": 3, "end_line": 5})
    assert json.loads(read.output)["content"] == "3: void Counter::increment() {\n4:     ++value_;\n5: }"
    search = await registry.execute("search_code", {"query": "counter_.increment()"})
    assert search.success
    assert json.loads(search.output)["matches"] == [{"path": "App.cpp", "line": 6, "text": "        counter_.increment();"}]
    no_matches = await registry.execute("search_code", {"query": "--pre=untrusted-command"})
    assert no_matches.success and json.loads(no_matches.output)["matches"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["../outside.txt", "sub/../../outside.txt", "C:\\private.txt", "/outside", "\\\\server\\share", "Counter.cpp:stream", "Counter.cpp.", ".git/config", ".ENV", "build/cache.txt", "secrets.json", "link/x.cpp"])
async def test_denied_paths(cpp_repo, rg_exe, git_exe, path):
    registry = await registry_for(cpp_repo, rg_exe, git_exe)
    result = await registry.execute("read_file", {"path": path})
    assert not result.success
    assert result.error.startswith("repository_")


@pytest.mark.asyncio
async def test_sensitive_generated_and_linked_files_are_never_exposed(cpp_repo, tmp_path, rg_exe, git_exe):
    for rel in [".env", "SECRETS.json", "build/cache.cpp", "agent.venv/lib/x.cpp", ".ssh/key.cpp", "client/CMakeUserPresets.json"]:
        file = cpp_repo / rel
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("DO_NOT_EXPOSE", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "external.cpp").write_text("DO_NOT_EXPOSE", encoding="utf-8")
    link = cpp_repo / "external"
    if os.name == "nt":
        subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(link), str(outside)],
                       check=True, capture_output=True)
    else:
        link.symlink_to(outside, target_is_directory=True)
    registry = await registry_for(cpp_repo, rg_exe, git_exe)
    listed = await registry.execute("list_files", {})
    assert "external" not in listed.output and "cache.cpp" not in listed.output
    searched = await registry.execute("search_code", {"query": "DO_NOT_EXPOSE"})
    assert searched.success and json.loads(searched.output)["matches"] == []
    denied = await registry.execute("read_file", {"path": "external/external.cpp"})
    assert not denied.success and "boundary" in denied.error


@pytest.mark.asyncio
async def test_limits_binary_and_encoding(cpp_repo, rg_exe, git_exe):
    (cpp_repo / "large.cpp").write_bytes(b"match\n" * (MAX_FILE_BYTES // 6 + 2))
    (cpp_repo / "binary.dat").write_bytes(b"\x00binary")
    (cpp_repo / "legacy.cpp").write_bytes(b"\xff\xfeabc")
    registry = await registry_for(cpp_repo, rg_exe, git_exe)
    listing = json.loads((await registry.execute("list_files", {"limit": 1})).output)
    assert len(listing["files"]) == 1 and listing["truncated"]
    read = json.loads((await registry.execute("read_file", {"path": "large.cpp"})).output)
    assert read["truncated"] and "file_byte_limit" in read["limits"]
    matches = json.loads((await registry.execute("search_code", {"query": "match", "max_results": 2})).output)
    assert len(matches["matches"]) == 2 and matches["truncated"]
    assert (await registry.execute("read_file", {"path": "binary.dat"})).error.startswith("repository_binary")
    assert (await registry.execute("read_file", {"path": "legacy.cpp"})).error.startswith("repository_encoding")
    assert (await registry.execute("read_file", {"path": "Counter.cpp", "start_line": 5, "end_line": 2})).error.startswith("invalid_arguments")


@pytest.mark.asyncio
async def test_non_git_and_subdirectory_rejected(tmp_path, cpp_repo, rg_exe, git_exe):
    for path in [tmp_path, cpp_repo / "missing"]:
        with pytest.raises(ForgePilotError, match="repository_"):
            await Repository.open(str(path), rg_executable=rg_exe, git_executable=git_exe)
    sub = cpp_repo / "sub"
    sub.mkdir()
    with pytest.raises(ForgePilotError, match="repository_not_git_root"):
        await Repository.open(str(sub), rg_executable=rg_exe, git_executable=git_exe)


@pytest.mark.asyncio
async def test_subprocess_timeout_cancel_and_output_cap(tmp_path, monkeypatch):
    processes = []
    launched = asyncio.Event()
    original = asyncio.create_subprocess_exec
    async def capture(*args, **kwargs):
        proc = await original(*args, **kwargs)
        processes.append(proc)
        launched.set()
        return proc
    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    command = [sys.executable, "-c", "import time; time.sleep(10)"]
    task = asyncio.create_task(run_process(command, tmp_path))
    await launched.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert processes[-1].returncode is not None
    with pytest.raises(ForgePilotError, match="tool_timeout"):
        await run_process(command, tmp_path, timeout=0.1)
    assert processes[-1].returncode is not None
    output = await run_process([sys.executable, "-c", "print('x' * 100000)"], tmp_path, output_limit=100)
    assert output.truncated and len(output.stdout) == 100


def test_executable_lookup_never_searches_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "." + os.pathsep)
    (tmp_path / "rg.exe").write_text("not an executable", encoding="utf-8")
    with pytest.raises(ForgePilotError, match="tool_missing"):
        executable(None, "rg")
    with pytest.raises(ForgePilotError, match="tool_missing"):
        executable("rg.exe", "rg")


@pytest.mark.asyncio
async def test_read_output_remains_valid_json_after_escaping(cpp_repo, rg_exe, git_exe):
    (cpp_repo / "escaped.cpp").write_text('\\\t"' * 10_000, encoding="utf-8")
    registry = await registry_for(cpp_repo, rg_exe, git_exe)
    result = await registry.execute("read_file", {"path": "escaped.cpp"})
    data = json.loads(result.output)
    assert data["truncated"] and "output_limit" in data["limits"]
    assert len(result.output) <= 16_384


def forbid_search_processes(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('search_code must not create a subprocess')
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', forbidden)
    monkeypatch.setattr(asyncio, 'create_subprocess_shell', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)


@pytest.mark.asyncio
@pytest.mark.parametrize('query', ['counter_.increment()', 'Counter', '++value_', 'no_match'])
async def test_search_cpp_matches_legacy_rg_snapshot_results(cpp_repo, rg_exe, monkeypatch, query):
    repo = Repository(cpp_repo.resolve(), rg_exe)
    expected = []
    files, limited = await repo._files('.')
    assert not limited
    for file in files:
        text, limited = repo._read(file)
        assert not limited
        old = await run_process([rg_exe, '--no-config', '--fixed-strings', '--line-number',
            '--no-heading', '--color', 'never', '--text', '--max-count', '51', '-e', query, '--', '-'],
            repo.root, data=text.encode('utf-8'))
        assert old.returncode in (0, 1) and not old.truncated
        for row in old.stdout.decode('utf-8').splitlines():
            number, _, content = row.partition(':')
            expected.append({'path': file, 'line': int(number), 'text': content})
    forbid_search_processes(monkeypatch)
    result = json.loads(await repo.search_code(query))
    assert result == {'matches': expected, 'truncated': False, 'limits': [], 'skipped_non_utf8_or_binary': 0}
    assert repo._read_versions == {}


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['', '\n'])
async def test_search_literal_unicode_case_symbols_and_lf_lines(tmp_path, monkeypatch, ending):
    rows = ['int 中文值 = 1;', 'Needle needle NEEDLE', r'auto literal = ".*[a-z]+? ^$ \\ --flag";',
            'prefix\u2028中文目标\u2029tail\rsegment', 'last 中文值']
    # CRLF on the first line, LF elsewhere, and an embedded CR are distinct.
    (tmp_path / '中文.cpp').write_bytes((rows[0]+'\r\n'+'\n'.join(rows[1:])+ending).encode('utf-8'))
    repo = Repository(tmp_path.resolve(), 'unused-rg.exe')
    forbid_search_processes(monkeypatch)
    cases = [('中文值', [1, 5]), ('Needle', [2]), ('needle', [2]), ('NEEDLE', [2]), ('nEEdle', []),
             ('.*[a-z]+?', [3]), ('\\', [3]), ('--flag', [3]), ('\u2028中文目标\u2029', [4]), ('segment', [4])]
    for query, numbers in cases:
        result = json.loads(await repo.search_code(query))
        assert result['matches'] == [{'path': '中文.cpp', 'line': n, 'text': rows[n-1]} for n in numbers]
        assert not result['truncated'] and result['limits'] == []


@pytest.mark.asyncio
@pytest.mark.parametrize('query', ['', 'a\r', 'a\n', 'a\0', 'a'*257])
async def test_search_invalid_query_rejected_before_read(tmp_path, monkeypatch, query):
    repo = Repository(tmp_path.resolve(), 'unused-rg.exe')
    registry = ToolRegistry(); register_repository_tools(registry, repo)
    def forbidden(*args): pytest.fail('invalid queries must be rejected before repository access')
    monkeypatch.setattr(repo, '_path', forbidden)
    forbid_search_processes(monkeypatch)
    result = await registry.execute('search_code', {'query': query})
    assert not result.success and result.error.startswith('invalid_arguments:')


@pytest.mark.asyncio
async def test_search_2000_files_only_reads_first_200_sorted(tmp_path, monkeypatch):
    for i in reversed(range(2000)):
        (tmp_path / f'source{i:04}.cpp').write_bytes(b'needle\n' if i in {0, 199, 200, 1999} else b'other\n')
    repo = Repository(tmp_path.resolve(), 'unused-rg.exe')
    read = repo._read; reads = []
    def counted(file):
        reads.append(file)
        return read(file)
    monkeypatch.setattr(repo, '_read', counted)
    forbid_search_processes(monkeypatch)
    result = json.loads(await repo.search_code('needle'))
    assert reads == [f'source{i:04}.cpp' for i in range(200)]
    assert result == {'matches': [{'path': f'source{i:04}.cpp', 'line': 1, 'text': 'needle'} for i in (0, 199)],
                      'truncated': True, 'limits': ['file_scan_limit'], 'skipped_non_utf8_or_binary': 0}


@pytest.mark.asyncio
async def test_search_eight_mib_budget_counts_utf8_bytes(tmp_path, monkeypatch):
    assert MAX_SEARCH_BYTES == 32 * MAX_FILE_BYTES
    for i in range(34):
        prefix = b'needle\n' if i in {31, 32} else b''
        remaining = MAX_FILE_BYTES-len(prefix)
        payload = prefix + '中'.encode('utf-8')*(remaining//3) + b'x'*(remaining%3)
        (tmp_path / f'source{i:02}.cpp').write_bytes(payload)
    repo = Repository(tmp_path.resolve(), 'unused-rg.exe')
    read = repo._read; reads = []
    def counted(file):
        reads.append(file)
        return read(file)
    monkeypatch.setattr(repo, '_read', counted)
    forbid_search_processes(monkeypatch)
    result = json.loads(await repo.search_code('needle'))
    # The next snapshot is read to measure its size, but never searched or counted.
    assert reads == [f'source{i:02}.cpp' for i in range(33)]
    assert result == {'matches': [{'path': 'source31.cpp', 'line': 1, 'text': 'needle'}],
                      'truncated': True, 'limits': ['search_byte_limit'], 'skipped_non_utf8_or_binary': 0}


@pytest.mark.asyncio
async def test_search_byte_truncation_binary_encoding_and_bom(tmp_path, monkeypatch):
    (tmp_path / 'a.dat').write_bytes(b'\0needle')
    (tmp_path / 'b.cpp').write_bytes(b'\xffneedle')
    (tmp_path / 'c.cpp').write_bytes(b'\xef\xbb\xbfneedle\r\n')
    (tmp_path / 'd.cpp').write_bytes(b'needle\n'+b'x'*(MAX_FILE_BYTES-7)+b'\nneedle hidden\n')
    repo = Repository(tmp_path.resolve(), 'unused-rg.exe')
    forbid_search_processes(monkeypatch)
    result = json.loads(await repo.search_code('needle'))
    assert result == {'matches': [{'path': f'{name}.cpp', 'line': 1, 'text': 'needle'} for name in ('c', 'd')],
                      'truncated': True, 'limits': ['file_byte_limit'], 'skipped_non_utf8_or_binary': 2}
    assert repo._read_versions == {}


@pytest.mark.asyncio
@pytest.mark.parametrize('long_line', [False, True])
async def test_search_output_cost_handles_escaping_and_long_lines(tmp_path, monkeypatch, long_line):
    line = 'needle'+'x'*(MAX_FILE_BYTES-6) if long_line else 'needle '+ '\\"\t'*900
    (tmp_path / 'text.cpp').write_bytes((line if long_line else (line+'\n')*3).encode('utf-8'))
    repo = Repository(tmp_path.resolve(), 'unused-rg.exe')
    forbid_search_processes(monkeypatch)
    encoded = await repo.search_code('needle', max_results=100)
    result = json.loads(encoded)
    expected = [] if long_line else [{'path': 'text.cpp', 'line': n, 'text': line} for n in (1, 2)]
    assert result == {'matches': expected, 'truncated': True, 'limits': ['match_or_output_limit'], 'skipped_non_utf8_or_binary': 0}
    assert len(encoded) < 16_384
    assert sum(len(json.dumps(m, ensure_ascii=False))+2 for m in expected) <= MAX_OUTPUT_CHARS


@pytest.mark.asyncio
@pytest.mark.parametrize('count', [1, 2])
async def test_search_exact_result_limit_is_only_truncated_when_more_match(tmp_path, monkeypatch, count):
    (tmp_path / 'text.cpp').write_bytes(b'needle\n'*count)
    repo = Repository(tmp_path.resolve(), 'unused-rg.exe')
    forbid_search_processes(monkeypatch)
    result = json.loads(await repo.search_code('needle', max_results=1))
    assert result['matches'] == [{'path': 'text.cpp', 'line': 1, 'text': 'needle'}]
    assert result['truncated'] is (count > 1)
    assert result['limits'] == (['match_or_output_limit'] if count > 1 else [])


@pytest.mark.asyncio
@pytest.mark.parametrize('single_file', [False, True])
async def test_search_cancellation_between_files_and_within_one_file(tmp_path, monkeypatch, single_file):
    (tmp_path / 'a.cpp').write_bytes(b'x\n'*(4096 if single_file else 1))
    (tmp_path / 'b.cpp').write_bytes(b'needle\n')
    repo = Repository(tmp_path.resolve(), 'unused-rg.exe')
    read = repo._read; reads = []
    def cancel_after_read(file):
        reads.append(file)
        snapshot = read(file)
        asyncio.get_running_loop().call_soon(asyncio.current_task().cancel)
        return snapshot
    monkeypatch.setattr(repo, '_read', cancel_after_read)
    forbid_search_processes(monkeypatch)
    pending = asyncio.create_task(repo.search_code('needle', 'a.cpp' if single_file else '.'))
    with pytest.raises(asyncio.CancelledError): await asyncio.wait_for(pending, 2)
    assert pending.done() and pending.cancelled() and reads == ['a.cpp']


@pytest.mark.asyncio
async def test_search_uses_verified_snapshot_without_reopening_or_recording_read_version(tmp_path, monkeypatch):
    file = tmp_path / 'text.cpp'; file.write_bytes(b'original needle\n')
    repo = Repository(tmp_path.resolve(), 'unused-rg.exe')
    read = repo._read; reads = []
    def changed_after_read(path):
        reads.append(path)
        snapshot = read(path)
        file.write_bytes(b'changed after verified snapshot\n')
        return snapshot
    monkeypatch.setattr(repo, '_read', changed_after_read)
    forbid_search_processes(monkeypatch)
    result = json.loads(await repo.search_code('needle', 'text.cpp'))
    assert result['matches'] == [{'path': 'text.cpp', 'line': 1, 'text': 'original needle'}]
    assert reads == ['text.cpp'] and not repo._read_versions
    assert file.read_bytes() == b'changed after verified snapshot\n'


@pytest.mark.asyncio
async def test_search_rejects_opened_handle_location_mismatch(tmp_path, monkeypatch):
    (tmp_path / 'text.cpp').write_bytes(b'needle\n')
    repo = Repository(tmp_path.resolve(), 'unused-rg.exe')
    monkeypatch.setattr('forgepilot.repository.opened_path', lambda fd: tmp_path.parent / 'outside.cpp')
    forbid_search_processes(monkeypatch)
    with pytest.raises(ForgePilotError, match='repository_boundary: Opened file location changed'):
        await repo.search_code('needle', 'text.cpp')


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['../outside.cpp', 'C:\\private.cpp', 'text.cpp:stream', '.git/config', 'SECRETS.json', 'build/cache.cpp'])
async def test_search_rejects_protected_paths(tmp_path, monkeypatch, path):
    repo = Repository(tmp_path.resolve(), 'unused-rg.exe')
    forbid_search_processes(monkeypatch)
    with pytest.raises(ForgePilotError, match='repository_'):
        await repo.search_code('needle', path)


@pytest.mark.asyncio
async def test_search_keeps_suffix_filter_and_sorted_path_order(tmp_path, monkeypatch):
    for name in ('z.CPP', 'a.hpp', 'other.txt'):
        (tmp_path / name).write_bytes(b'needle\n')
    repo = Repository(tmp_path.resolve(), 'unused-rg.exe')
    forbid_search_processes(monkeypatch)
    result = json.loads(await repo.search_code('needle', file_suffixes={'.cpp', '.hpp'}))
    assert [m['path'] for m in result['matches']] == ['a.hpp', 'z.CPP']
    assert not result['truncated'] and not result['limits']
