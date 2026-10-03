from pathlib import Path
import shutil
import subprocess

import pytest


TRAINING=Path(__file__).resolve().parents[1]


def test_pure_deletion_ui_state_transitions():
    node=shutil.which('node')
    if not node: pytest.skip('Node.js is unavailable for the pure frontend state test')
    script=r"""
const assert=require('assert');
const ui=require('./deletion_ui.js');
const pending={deletion_id:'1',run_id:'a',logical_status:'deleted',cleanup_status:'pending'};
const cleaned={...pending,cleanup_status:'cleaned'};
const manual={...pending,cleanup_status:'manual_intervention_required'};
assert.strictEqual(ui.shouldPoll(pending),true);
assert.strictEqual(ui.shouldPoll(cleaned),false);
assert.strictEqual(ui.shouldPoll(manual),false);
assert.strictEqual(ui.nextRunId([{id:'b'},{id:'c'}],'a',1),'c');
assert.strictEqual(ui.nextRunId([],'a',0),null);
assert.strictEqual(ui.recoverForRun([pending],'a'),pending);
assert.match(ui.statusText(pending),/后台清理/);
assert.match(ui.statusText(manual),/人工检查/);
assert.match(ui.statusText(cleaned),/清理完成/);
"""
    result=subprocess.run([node,'-e',script],cwd=TRAINING,text=True,capture_output=True,timeout=10)
    assert result.returncode==0,result.stderr


def test_frontend_uses_delete_contract_and_safe_status_elements():
    javascript=(TRAINING/'studio.js').read_text(encoding='utf-8')
    html=(TRAINING/'studio.html').read_text(encoding='utf-8')
    assert "undefined,'DELETE'" in javascript
    assert '/delete`' not in javascript
    assert "DeletionUI.recoverForRun" in javascript
    assert "element.textContent=DeletionUI.statusText" in javascript
    assert '模型会立即从训练记录中隐藏' in html
    assert '可能继续在后台清理' in html
    assert '资料会保留' in html
    assert '派生模型拥有独立权重' in html
