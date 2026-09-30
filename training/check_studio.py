"""Opt-in real GPU acceptance; all test data stays in a unique logs directory."""
from datetime import datetime,timezone
import hashlib
import io
from pathlib import Path
import time
from uuid import uuid4
import zipfile

from fastapi.testclient import TestClient
from studio import Studio,create_app,ROOT
from studio_data import demo_files
from studio_io import read_json,write_json


def main():
    root=ROOT/'logs/model-training'/('acceptance-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:6])
    studio=Studio(root); report={'storage':str(root),'passed':False}
    def wait(identity,predicate,timeout=180):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            state=next(r for r in studio.snapshot()['runs'] if r['id']==identity)
            if state['status']=='failed': raise AssertionError(state['message'])
            if predicate(state): return state
            time.sleep(0.1)
        raise TimeoutError(state)
    try:
        data=studio.add_dataset({'name':'GPU 验收资料','files':demo_files()})
        first=studio.start({'dataset_id':data['id'],'steps':20,'context':512})['id']
        wait(first,lambda s:s['step']>=3)
        studio.control(first,'pause')
        paused=wait(first,lambda s:s['status']=='paused' and studio.process is None)
        assert paused['can_resume'] and paused['has_model'] and paused['step']<20
        report['pause']={'step':paused['step'],'worker_exited':True}
        studio.control(first,'resume')
        final=wait(first,lambda s:s['status']=='completed' and studio.process is None)
        assert final['step']==20 and final['tokens']==20*512
        assert final['token_counts']['validation']<final['context']
        report['short_validation_data']='evaluated at its actual length, without duplicating data'
        assert final['metrics'][-1]['validation']<final['metrics'][0]['validation']
        report['pretrain']={k:final[k] for k in ('parameters','step','tokens','train_loss','validation_loss','test_loss','gpu_memory_mib')}
        weights=studio.path('runs',first)/'model/model.safetensors'
        original_hash=hashlib.sha256(weights.read_bytes()).hexdigest()
        continued=studio.start({'dataset_id':data['id'],'steps':5,'base_run':first,'training_mode':'continue'})['id']
        studio.control(continued,'pause')
        zero=wait(continued,lambda s:s['status']=='paused' and studio.process is None)
        assert zero['step']==0
        child=studio.path('runs',continued)
        assert hashlib.sha256((child/'model/model.safetensors').read_bytes()).hexdigest()==original_hash
        assert hashlib.sha256((child/'model/tokenizer.json').read_bytes()).hexdigest()==hashlib.sha256((weights.parent/'tokenizer.json').read_bytes()).hexdigest()
        studio.control(continued,'resume')
        learned=wait(continued,lambda s:s['status']=='completed' and studio.process is None)
        assert hashlib.sha256((child/'model/model.safetensors').read_bytes()).hexdigest()!=original_hash
        assert hashlib.sha256(weights.read_bytes()).hexdigest()==original_hash
        assert learned['phase']=='complete' and learned['events']
        report['continue_learning']={'starts_from_saved_weights':True,'preserves_tokenizer':True,'parent_unchanged':True,'steps':learned['step'],'visible_events':len(learned['events'])}
        qa=studio.add_dataset({'name':'GPU 验收问答','files':demo_files('qa')})
        second=studio.start({'dataset_id':qa['id'],'steps':10,'base_run':first})['id']
        qa_final=wait(second,lambda s:s['status']=='completed' and studio.process is None)
        assert qa_final['mode']=='sft' and hashlib.sha256(weights.read_bytes()).hexdigest()==original_hash
        report['sft']={k:qa_final[k] for k in ('parameters','step','tokens','train_loss','validation_loss','test_loss')}
        with TestClient(create_app(studio),base_url='http://127.0.0.1') as client:
            result=client.post('/v1/chat/completions',headers={'Authorization':'Bearer '+studio.api_key},
                json={'model':'forgepilot-'+second,'messages':[{'role':'user','content':'如何用 C++ 返回整数加 1 的结果？'}],'max_tokens':16})
            assert result.status_code==200,result.text
            completion=result.json(); assert 'content' in completion['choices'][0]['message']
            report['inference']={'http_status':200,'usage':completion['usage'],'finish_reason':completion['choices'][0]['finish_reason']}
            assert studio.inference is None
            exported=client.get('/api/export/'+second); assert exported.status_code==200
            with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
                assert hashlib.sha256(archive.read('model.safetensors')).digest()==hashlib.sha256((studio.path('runs',second)/'model/model.safetensors').read_bytes()).digest()
                report['export']={'files':archive.namelist(),'bytes':len(exported.content)}
            third=studio.start({'dataset_id':data['id'],'steps':20})['id']
            wait(third,lambda s:s['step']>=2)
            studio.control(third,'stop')
            stop=wait(third,lambda s:s['status']=='stopped' and studio.process is None)
            report['stop']={'step':stop['step'],'worker_exited':True}
            fourth=studio.start({'dataset_id':data['id'],'steps':20})['id']
            wait(fourth,lambda s:s['step']>=2)
            studio.close()
            closed=read_json(studio.path('runs',fourth)/'state.json')
            assert closed['status']=='paused' and studio.process is None
            report['shutdown']={'status':closed['status'],'worker_exited':True}
        # A venv launcher creates another Python process on Windows. Verify the
        # actual GPU worker also belongs to the job and exits on abrupt closure.
        abrupt=Studio(root/'abrupt-exit')
        try:
            import ctypes
            from ctypes import wintypes
            data=abrupt.add_dataset({'files':demo_files()})
            identity=abrupt.start({'dataset_id':data['id'],'steps':100})['id']
            deadline=time.monotonic()+180
            while time.monotonic()<deadline:
                state=abrupt.snapshot()['runs'][0]
                if state['status']=='failed': raise AssertionError(state['message'])
                if state['step']>=2: break
                time.sleep(0.1)
            assert state['step']>=2
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]; kernel.OpenProcess.restype=wintypes.HANDLE
            kernel.IsProcessInJob.argtypes=[wintypes.HANDLE,wintypes.HANDLE,ctypes.POINTER(wintypes.BOOL)]
            kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]; kernel.CloseHandle.argtypes=[wintypes.HANDLE]
            worker=kernel.OpenProcess(0x100000|0x1000,False,state['worker_pid']); assert worker
            try:
                member=wintypes.BOOL()
                assert kernel.IsProcessInJob(worker,abrupt.owner.handle,ctypes.byref(member)) and member.value
                abrupt.owner.close()
                assert kernel.WaitForSingleObject(worker,10000)==0
                abrupt.process.wait(timeout=10)
            finally: kernel.CloseHandle(worker)
            report['abrupt_exit']={'actual_gpu_worker_in_job':True,'worker_exited':True}
        finally: abrupt.close()
        report['passed']=True
    finally:
        studio.close(); write_json(root/'acceptance.json',report)
        print(root/'acceptance.json',flush=True)
    print('GPU train / pause / resume / SFT / real HTTP inference / export / stop / shutdown passed.',flush=True)


if __name__=='__main__': main()
