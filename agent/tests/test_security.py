"""Local cache, concurrency, and real temporary SQLite redaction regressions."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import sys
from threading import Barrier
from urllib.parse import quote

import pytest

from forgepilot.recording import now, project_events
from forgepilot.security import Redactor
from forgepilot.sqlite_store import SqliteEventStore
from forgepilot.trace import TraceEvent
from test_persistence import task


def assert_accounting(redactor):
    with redactor._cache_lock:
        assert len(redactor._cache) <= 16
        sizes = [sys.getsizeof(key[0]) + sys.getsizeof(value[0]) for key, value in redactor._cache.items()]
        assert sizes == [value[1] for value in redactor._cache.values()]
        assert redactor._cache_bytes == sum(sizes) <= 4 * 1024 * 1024


def count_scans(monkeypatch, redactor):
    scans = []
    original = redactor._redact_text
    def counted(value, *, preserve_cr=False):
        scans.append((len(value), preserve_cr))
        return original(value, preserve_cr=preserve_cr)
    monkeypatch.setattr(redactor, '_redact_text', counted)
    return scans


@pytest.mark.parametrize('preserve_cr', [False, True])
def test_cold_hot_text_matches_pre_cache_golden_rules(monkeypatch, preserve_cr):
    padding = 'safe compiler line\n' * 100
    value = (padding+'plain unique-fixture-key\nBearer opaque-bearer\nAuthorization: Basic b64\n'
             'api_key="api-literal"\n密码：中文密钥\nsk-proj-abcdefghijklmnop\n'
             '-----BEGIN PRIVATE KEY-----\nxyz\n-----END PRIVATE KEY-----\nline\r\n\0\t')
    expected = (padding+'plain [REDACTED]\nBearer [REDACTED]\nAuthorization: [REDACTED]\n'
                'api_key=[REDACTED]\n密码：[REDACTED]\n[REDACTED]\n[REDACTED]\nline'
                + ('\r\n' if preserve_cr else '\n') + '\t')
    redactor = Redactor(('unique-fixture-key',)); scans = count_scans(monkeypatch, redactor)
    assert redactor.text(value, preserve_cr=preserve_cr) == expected
    assert redactor.text(value, preserve_cr=preserve_cr) == expected
    assert len(scans) == 1
    assert_accounting(redactor)


@pytest.mark.parametrize('variant', range(5), ids=['raw', 'url', 'json-1', 'json-2', 'json-3'])
def test_encoded_secret_variants_are_redacted_on_cold_and_hot_calls(variant):
    secret = 'fixture"key\\多+?/'
    variants = [secret, quote(secret, safe='')]
    encoded = secret
    for _ in range(3):
        encoded = json.dumps(encoded, ensure_ascii=True)[1:-1]
        variants.append(encoded)
    padding = 'safe\n' * 300
    redactor = Redactor((secret,))
    value = padding + variants[variant]
    for _ in range(2): assert redactor.text(value) == padding+'[REDACTED]'
    assert_accounting(redactor)


def test_instance_keys_and_with_secrets_have_independent_empty_caches():
    padding = 'safe\n' * 300
    value = padding+'private-alpha private-beta'
    left, right = Redactor(('private-alpha',)), Redactor(('private-beta',))
    assert left.text(value) == padding+'[REDACTED] private-beta'
    assert right.text(value) == padding+'private-alpha [REDACTED]'
    child = left.with_secrets(('private-beta',))
    assert not child._cache and child._cache_bytes == 0
    assert child._cache is not left._cache and child._cache_lock is not left._cache_lock
    assert child.text(value) == padding+'[REDACTED] [REDACTED]'
    assert left.text(value) == padding+'[REDACTED] private-beta'
    assert right.text(value) == padding+'private-alpha [REDACTED]'
    for redactor in (left, right, child): assert_accounting(redactor)


def test_preserve_cr_has_separate_cache_entries(monkeypatch):
    value = 'first\r\nsecond\r\n' * 100
    redactor = Redactor(); scans = count_scans(monkeypatch, redactor)
    for _ in range(2):
        assert redactor.text(value) == value.replace('\r', '')
        assert redactor.text(value, preserve_cr=True) == value
    assert scans == [(len(value), False), (len(value), True)]
    assert len(redactor._cache) == 2
    assert_accounting(redactor)


def test_data_still_checks_json_structure_and_sensitive_keys_after_text_warmup():
    redactor = Redactor(('private-known',))
    payload = {'password': {'nested': 'hidden-password'}, 'source': 'line\r\n' * 200,
               'items': [{'Authorization': ['hidden-auth'], 'body': 'x'*1100+'private-known'}]}
    encoded = json.dumps(payload, ensure_ascii=False)
    redactor.text(encoded)  # A warm text cache cannot replace data's structural checks.
    expected = {'password': '[REDACTED]', 'source': payload['source'],
                'items': [{'Authorization': '[REDACTED]', 'body': 'x'*1100+'[REDACTED]'}]}
    for _ in range(2): assert json.loads(redactor.data(encoded)) == expected
    original = deepcopy(payload)
    first = redactor.data(payload)
    assert first['source'] == payload['source'].replace('\r', '') and payload == original
    payload['items'][0]['body'] = 'new '*300+'private-known'
    second = redactor.data(payload)
    assert second['items'][0]['body'] == 'new '*300+'[REDACTED]'
    assert first['items'][0]['body'] == expected['items'][0]['body']
    assert_accounting(redactor)


@pytest.mark.parametrize('length,expected_scans,entries', [(1023, 2, 0), (1024, 1, 1)])
def test_only_large_strings_are_cached(monkeypatch, length, expected_scans, entries):
    redactor = Redactor(); scans = count_scans(monkeypatch, redactor)
    value = 'x' * length
    assert redactor.text(value) == value and redactor.text(value) == value
    assert len(scans) == expected_scans and len(redactor._cache) == entries
    assert_accounting(redactor)


def test_sixteen_entry_lru_hits_refresh_and_evicted_values_recompute(monkeypatch):
    redactor = Redactor(); scans = count_scans(monkeypatch, redactor)
    values = [f'{i:02d} '+'x'*1100 for i in range(17)]
    for value in values[:16]: assert redactor.text(value) == value
    assert len(redactor._cache) == 16 and len(scans) == 16
    assert redactor.text(values[0]) == values[0] and len(scans) == 16
    redactor.text(values[16])
    assert (values[0], False) in redactor._cache and (values[1], False) not in redactor._cache
    assert len(redactor._cache) == 16 and len(scans) == 17
    redactor.text(values[1])
    assert len(scans) == 18 and (values[2], False) not in redactor._cache
    assert_accounting(redactor)


@pytest.mark.parametrize('character', ['x', '资', '😀'], ids=['ascii', 'cjk', 'non-bmp'])
def test_byte_limit_charges_python_string_sizes_and_evicts_before_item_limit(character):
    redactor = Redactor()
    values = [str(i)+character*400_000 for i in range(6)]
    for value in values:
        assert redactor.text(value) == value
        assert_accounting(redactor)
    assert len(redactor._cache) < 6 < 16
    assert (values[-1], False) in redactor._cache and (values[0], False) not in redactor._cache


def test_exact_byte_limit_is_allowed_and_larger_item_bypasses_without_eviction(monkeypatch):
    redactor = Redactor(); scans = count_scans(monkeypatch, redactor)
    value = 'x' * ((4*1024*1024 - 2*(sys.getsizeof('x')-1)) // 2)
    assert redactor.text(value) == value
    assert redactor._cache_bytes == 4*1024*1024
    too_large = value+'x'
    for _ in range(2): assert redactor.text(too_large) == too_large
    assert len(scans) == 3 and len(redactor._cache) == 1
    assert (value, False) in redactor._cache and (too_large, False) not in redactor._cache
    assert_accounting(redactor)


def test_redaction_expansion_is_charged_and_oversized_result_is_not_cached():
    redactor = Redactor(('Z',))
    value = 'Z' * 450_000
    for _ in range(2): assert redactor.text(value) == '[REDACTED]' * len(value)
    assert not redactor._cache and redactor._cache_bytes == 0


def test_concurrent_same_key_misses_charge_only_one_entry_and_scan_outside_lock(monkeypatch):
    redactor = Redactor(('private-known',))
    original = redactor._redact_text
    barrier = Barrier(8)
    value = 'safe\n'*400+'private-known'
    def synchronized(value, *, preserve_cr=False):
        barrier.wait(timeout=5)
        return original(value, preserve_cr=preserve_cr)
    monkeypatch.setattr(redactor, '_redact_text', synchronized)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(redactor.text, value) for _ in range(8)]
        assert all(f.result(timeout=10) == 'safe\n'*400+'[REDACTED]' for f in futures)
    assert len(redactor._cache) == 1
    assert_accounting(redactor)
    # A hot call must not re-enter the barrier/regex computation.
    assert redactor.text(value) == 'safe\n'*400+'[REDACTED]'


@pytest.mark.parametrize('repeats,operations', [(300, 400), (30_000, 80)], ids=['entry-bound', 'byte-bound'])
def test_concurrent_hits_misses_evictions_and_cr_modes_keep_accounting_consistent(repeats, operations):
    redactor = Redactor(('private-known',))
    values = [f'{i:02d} '+('line\r\n'*repeats)+'private-known' for i in range(40)]
    def use(index):
        value, mode = values[index % len(values)], bool(index % 3)
        expected = value.replace('private-known', '[REDACTED]')
        if not mode: expected = expected.replace('\r', '')
        assert redactor.text(value, preserve_cr=mode) == expected
        assert_accounting(redactor)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(use, range(operations)))
    assert len(redactor._cache) == 16 if repeats == 300 else len(redactor._cache) < 16
    assert_accounting(redactor)


def test_hot_cached_large_logs_never_persist_keys_or_cache_in_sqlite(tmp_path):
    secret = 'r6-private-fixture-never-persist'
    redactor = Redactor((secret,))
    raw = 'compiler output\n'*1000 + secret
    safe = raw.replace(secret, '[REDACTED]')
    assert redactor.text(raw) == safe and redactor.text(raw) == safe
    path = tmp_path / 'cached-redaction.sqlite3'
    store = SqliteEventStore(path, redactor=redactor)
    value = task(); tid = value['task_id']
    value['build_result'] = {'commands': [{'stdout': raw}]}
    value['validation_history'] = [value['build_result']]
    try:
        store.create(tid, value)
        traces = [TraceEvent(1, tid, 'tool_started', now(), {'name': 'read_file', 'arguments': json.dumps({'body': raw, 'password': {'nested': secret}})}),
                  TraceEvent(2, tid, 'tool_finished', now(), {'name': 'read_file', 'success': True, 'output': raw, 'error': None, 'duration_ms': 1})]
        store.publish(tid, value, project_events(traces, value), traces=traces)
        saved = store.get(tid)
        assert saved['build_result']['commands'][0]['stdout'] == safe
        assert saved['validation_history'][0]['commands'][0]['stdout'] == safe
        dump = '\n'.join(store._db.iterdump())
        assert secret not in dump and '_cache' not in dump
        assert redactor._cache and (raw, False) in redactor._cache
        for suffix in ('', '-wal', '-shm'):
            file = tmp_path / (path.name + suffix)
            if file.exists():
                data = file.read_bytes()
                assert secret.encode() not in data and secret.encode('utf-16-le') not in data
        assert_accounting(redactor)
    finally:
        store.close()
    assert secret.encode() not in path.read_bytes()
