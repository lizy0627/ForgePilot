"""Independent, loopback-only training workbench. Never loads Agent configuration."""
from __future__ import annotations
import argparse
from contextlib import asynccontextmanager
import ctypes
from ctypes import wintypes
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from uuid import uuid4
import webbrowser
import zipfile

from fastapi import FastAPI,Request,HTTPException
from fastapi.responses import HTMLResponse,FileResponse,JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
import uvicorn
from studio_data import parse_files,demo_files
from studio_io import read_json,write_json
from deletion import (atomic_move_within_storage,delete_transaction,read_receipt,read_transaction,
    read_lineage_snapshot,receipt_run_ids,safe_error,target_locations,transaction_ids,
    validate_target,write_lineage_snapshot,write_receipt,write_transaction)

ROOT=Path(__file__).resolve().parents[1]
DEFAULT_STORAGE=ROOT/'logs/model-training/studio'
ACTIVE={'preparing','running','pausing'}
CLEANUP_MAX_ATTEMPTS=3
CLEANUP_RETRY_DELAYS=(1.0,5.0)
CLEANUP_CLOSE_TIMEOUT=2.0


def now(): return datetime.now(timezone.utc).isoformat()


class DeletionLocked(ValueError):
    pass


class ProcessOwner:
    """Windows Job kills only this studio's workers if its server is closed."""
    def __init__(self,memory_limit=None):
        self.handle=None
        if os.name=='nt':
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            kernel.CreateJobObjectW.argtypes=[ctypes.c_void_p,wintypes.LPCWSTR]; kernel.CreateJobObjectW.restype=wintypes.HANDLE
            kernel.SetInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
            kernel.AssignProcessToJobObject.argtypes=[wintypes.HANDLE,wintypes.HANDLE]
            kernel.CloseHandle.argtypes=[wintypes.HANDLE]
            # JOBOBJECT_EXTENDED_LIMIT_INFORMATION, layout valid on Windows x64.
            class Basic(ctypes.Structure):
                _fields_=[('per_process',ctypes.c_int64),('per_job',ctypes.c_int64),('flags',wintypes.DWORD),
                    ('min_ws',ctypes.c_size_t),('max_ws',ctypes.c_size_t),('active',wintypes.DWORD),
                    ('affinity',ctypes.c_size_t),('priority',wintypes.DWORD),('scheduling',wintypes.DWORD)]
            class Info(ctypes.Structure):
                _fields_=[('basic',Basic),('io',ctypes.c_uint64*6),('process_memory',ctypes.c_size_t),
                    ('job_memory',ctypes.c_size_t),('peak_process',ctypes.c_size_t),('peak_job',ctypes.c_size_t)]
            info=Info(); info.basic.flags=0x2000
            if memory_limit: info.basic.flags|=0x100; info.process_memory=memory_limit
            self.handle=kernel.CreateJobObjectW(None,None); self.kernel=kernel
            if not self.handle or not kernel.SetInformationJobObject(self.handle,9,ctypes.byref(info),ctypes.sizeof(info)):
                raise OSError('无法创建独立训练进程容器。')
    def attach(self,process):
        if self.handle and not self.kernel.AssignProcessToJobObject(self.handle,wintypes.HANDLE(process._handle)):
            process.kill(); process.wait(); raise OSError('无法隔离训练进程。')
    def resume(self,process):
        """Assign the suspended launcher before it can create a Python child."""
        if os.name!='nt': return
        kernel=self.kernel
        class ThreadEntry(ctypes.Structure):
            _fields_=[('size',wintypes.DWORD),('usage',wintypes.DWORD),('thread',wintypes.DWORD),
                ('process',wintypes.DWORD),('base_priority',wintypes.LONG),('delta_priority',wintypes.LONG),('flags',wintypes.DWORD)]
        kernel.CreateToolhelp32Snapshot.argtypes=[wintypes.DWORD,wintypes.DWORD]; kernel.CreateToolhelp32Snapshot.restype=wintypes.HANDLE
        kernel.Thread32First.argtypes=[wintypes.HANDLE,ctypes.POINTER(ThreadEntry)]
        kernel.Thread32Next.argtypes=[wintypes.HANDLE,ctypes.POINTER(ThreadEntry)]
        kernel.OpenThread.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]; kernel.OpenThread.restype=wintypes.HANDLE
        kernel.ResumeThread.argtypes=[wintypes.HANDLE]; kernel.ResumeThread.restype=wintypes.DWORD
        snapshot=kernel.CreateToolhelp32Snapshot(0x4,0)
        if snapshot==ctypes.c_void_p(-1).value: raise OSError('无法检查训练进程。')
        try:
            entry=ThreadEntry(); entry.size=ctypes.sizeof(entry)
            found=kernel.Thread32First(snapshot,ctypes.byref(entry))
            while found:
                if entry.process==process.pid:
                    thread=kernel.OpenThread(0x0002,False,entry.thread)
                    if not thread: raise OSError('无法启动训练线程。')
                    try:
                        if kernel.ResumeThread(thread)==0xffffffff: raise OSError('无法恢复训练线程。')
                    finally: kernel.CloseHandle(thread)
                    return
                found=kernel.Thread32Next(snapshot,ctypes.byref(entry))
            raise OSError('没有找到训练线程。')
        finally: kernel.CloseHandle(snapshot)
    def close(self):
        if self.handle: self.kernel.CloseHandle(self.handle); self.handle=None
    def terminate(self):
        if self.handle:
            self.kernel.TerminateJobObject.argtypes=[wintypes.HANDLE,wintypes.UINT]
            self.kernel.TerminateJobObject(self.handle,1)


class Studio:
    def __init__(self,storage):
        self.root=Path(storage).resolve(); self.root.mkdir(parents=True,exist_ok=True)
        for folder in ('datasets','runs','requests','imports','deletion-journal','trash'):
            (self.root/folder).mkdir(exist_ok=True)
        (self.root/'deletion-journal/receipts').mkdir(exist_ok=True)
        self.lock=threading.RLock(); self.process=None; self.active_id=None; self.inference=None; self.inference_id=None
        self.cleanup_tasks_lock=threading.Lock(); self.cleanup_tasks={}; self.cleanup_stop=threading.Event()
        self.deletion_fault=lambda _point:None
        self.owner=ProcessOwner(); self.csrf=secrets.token_urlsafe(32); self.instance=uuid4().hex
        self.import_owner=ProcessOwner(memory_limit=768*1024*1024); self.import_process=None
        key_path=self.root/'api-key.json'
        if not key_path.exists(): write_json(key_path,{'key':secrets.token_urlsafe(32)})
        self.api_key=read_json(key_path)['key']; self.shutdown_callback=lambda:None

    def path(self,kind,identity):
        if not isinstance(identity,str) or not re.fullmatch('[a-f0-9]{16}',identity): raise ValueError('记录编号无效。')
        path=self.root/kind/identity
        if not path.resolve().is_relative_to(self.root/kind): raise ValueError('记录路径无效。')
        return path

    def reconcile(self):
        if self.process and self.process.poll() is not None:
            path=self.path('runs',self.active_id)/'state.json'; state=read_json(path)
            if state['status'] in ACTIVE:
                state.update(status='failed',message='训练进程意外退出。已保存的进度仍保留，可以尝试继续。'); write_json(path,state)
            self.process=None; self.active_id=None

    def recover(self):
        self.recover_deletions()
        for path in (self.root/'imports').glob('*/state.json'):
            state=read_json(path)
            if state['status']=='processing':
                state.update(status='failed',message='上次资料处理已中断，请重新选择文件。'); write_json(path,state)
        for path in (self.root/'runs').glob('*/state.json'):
            state=read_json(path)
            if state['status'] in ACTIVE:
                state.update(status='interrupted',message='上次工作室已关闭，训练没有自动恢复。可以手动继续。'); write_json(path,state)

    def snapshot(self):
        with self.lock:
            self.reconcile(); datasets=[]; runs=[]; states={}; corrupt_run_ids=set()
            for path in (self.root/'datasets').glob('*/data.json'):
                item=read_json(path); datasets.append({k:v for k,v in item.items() if k!='documents'})
            for path in (self.root/'runs').glob('*/state.json'):
                try:
                    if self._dependent_is_logically_deleted(path.parent.name): continue
                except (OSError,ValueError,json.JSONDecodeError): pass
                try: item=self._validated_run_state(read_json(path),path.parent.name)
                except (OSError,ValueError,json.JSONDecodeError,TypeError,KeyError):
                    corrupt_run_ids.add(path.parent.name); continue
                states[item['id']]=(path,item)
            for identity,(path,item) in states.items():
                item=dict(item); artifact=path.parent/'model/model.safetensors'
                item['has_model']=artifact.exists(); item['can_resume']=(path.parent/'checkpoint.pt').exists() and item['step']<item['total_steps']
                if item['id']==self.active_id:
                    control=read_json(path.parent/'control.json').get('action')
                    if control in ('pause','stop'): item['pending_action']=control
                base_run=item.get('base_run')
                if base_run:
                    item.update(base_run_deleted=False,base_run_snapshot=None,deleted_at=None,base_run_status='present')
                    if base_run not in states:
                        item.update(base_run_deleted=None,base_run_status='unknown')
                        if base_run not in corrupt_run_ids:
                            try: lineage=read_lineage_snapshot(path.parent/'lineage.json')
                            except (OSError,ValueError,json.JSONDecodeError): pass
                            else:
                                if lineage['base_run']==base_run:
                                    item.update(base_run_deleted=True,base_run_snapshot=lineage['base_run_snapshot'],
                                        deleted_at=lineage['deleted_at'],base_run_status='deleted')
                else: item['base_run_status']='none'
                runs.append(item)
            return {'datasets':sorted(datasets,key=lambda x:x['created'],reverse=True),
                'runs':sorted(runs,key=lambda x:x['created'],reverse=True),'active_run':self.active_id,
                'inference_busy':self.inference is not None,'storage':str(self.root),
                'deletions':self.deletion_summaries()}

    def add_dataset(self,payload):
        dataset=parse_files(payload.get('files')); identity=uuid4().hex[:16]
        dataset.update(id=identity,name=str(payload.get('name') or payload['files'][0]['name'])[:100],created=now(),demo=bool(payload.get('_demo',False)))
        path=self.path('datasets',identity); path.mkdir(); write_json(path/'data.json',dataset)
        return {k:v for k,v in dataset.items() if k!='documents'}

    @staticmethod
    def preview(item,offset=0):
        offset=max(0,min(offset,max(0,item['count']-1)))
        return {**{k:v for k,v in item.items() if k!='documents'},'offset':offset,
            'samples':item['documents'][offset:offset+5],
            'split_counts':{k:len(v) for k,v in __import__('studio_data').split_documents(item['documents']).items()}}

    def start_import(self,value):
        if not isinstance(value.get('files'),list) or not 1<=len(value['files'])<=32: raise ValueError('一次请选择 1–32 个文件。')
        with self.lock:
            if self.import_process and self.import_process.poll() is None: raise ValueError('正在处理上一批资料，请稍等。')
            identity=uuid4().hex[:16]; directory=self.path('imports',identity); directory.mkdir()
            write_json(directory/'input.json',value)
            state={'id':identity,'status':'processing','progress':0,'message':'正在检查和提取资料文字。'}
            write_json(directory/'state.json',state)
            process=self.spawn(['import',str(directory)],directory/'worker.log',owner=self.import_owner)
            self.import_process=process
            def supervise():
                try: process.wait(timeout=90)
                except subprocess.TimeoutExpired:
                    with self.lock:
                        if self.import_process is process: self.import_owner.terminate()
                    process.wait(timeout=10)
                result=read_json(directory/'state.json')
                if result['status']=='processing':
                    result.update(status='failed',message='资料处理超时或资源不足，请拆分文件或转存为文本。')
                    write_json(directory/'state.json',result)
            threading.Thread(target=supervise,daemon=True).start()
            return state

    def save_import(self,identity):
        with self.lock:
            directory=self.path('imports',identity)
            if read_json(directory/'state.json')['status']!='ready': raise ValueError('请等待资料处理完成。')
            dataset=read_json(directory/'result.json'); dataset.update(id=identity,created=now(),demo=False)
            target=self.path('datasets',identity); target.mkdir(exist_ok=True)
            if not (target/'data.json').exists(): write_json(target/'data.json',dataset)
            return {k:v for k,v in dataset.items() if k!='documents'}

    def spawn(self,args,log,owner=None):
        owner=owner or self.owner
        flags=(subprocess.CREATE_NO_WINDOW|subprocess.BELOW_NORMAL_PRIORITY_CLASS|0x00000004) if os.name=='nt' else 0
        env=os.environ.copy()
        # No external-model or MCP credentials are needed by training workers.
        for name in list(env):
            if name.startswith(('FORGEPILOT_API','OPENAI_API','ANTHROPIC_API')): env.pop(name,None)
        env['PYTHONIOENCODING']='utf-8'; env['OMP_NUM_THREADS']='2'; env['TOKENIZERS_PARALLELISM']='false'
        with log.open('ab') as stream:
            process=subprocess.Popen([sys.executable,str(Path(__file__).with_name('studio_worker.py')),*args],
                cwd=Path(__file__).parent,env=env,stdout=stream,stderr=stream,creationflags=flags)
        try: owner.attach(process); owner.resume(process)
        except Exception:
            if process.poll() is None: process.kill(); process.wait()
            raise
        return process

    def start(self,payload):
        with self.lock:
            self.reconcile()
            if self.process or self.inference: raise ValueError('显卡正在训练或试用，请先等待或暂停。')
            dataset_path=self.path('datasets',payload.get('dataset_id'))/'data.json'
            if not dataset_path.exists(): raise ValueError('请先选择资料。')
            dataset=read_json(dataset_path); preset=payload.get('preset','demo')
            if preset not in ('demo','local','custom'): raise ValueError('请选择有效的模型设置。')
            demo=preset=='demo'
            steps=int(payload.get('steps',40 if demo else 1000)); lr=float(payload.get('learning_rate',0.0003))
            if not 5<=steps<=10000 or not 0.00001<=lr<=0.003: raise ValueError('训练步数应为 5–10000，学习率应为 0.00001–0.003。')
            width=int(payload.get('width',128 if demo else 384)); layers=int(payload.get('layers',4 if demo else 8))
            context=int(payload.get('context',128 if demo else 512)); heads=width//64
            if width not in (128,256,384,512,768) or not 2<=layers<=16 or context not in (128,256,512,1024):
                raise ValueError('模型结构参数不在本机支持范围内。')
            architecture={'layers':layers,'width':width,'heads':heads,'kv_heads':1 if width==128 else (4 if width==768 else 2),
                'intermediate':int(math_ceil_multiple(width*8/3,128)),'context':context}
            training_mode=payload.get('training_mode','fresh')
            if training_mode not in ('fresh','continue'): raise ValueError('请选择从零训练或继续学习。')
            request={'mode':'sft' if dataset['kind']=='qa' else ('continue' if training_mode=='continue' else 'pretrain'),'steps':steps,'learning_rate':lr,
                'architecture':architecture,'vocab_size':2048 if demo else 32768,'accumulation':1 if demo else 4,
                'gentle':payload.get('gentle',True) is not False,'preset':preset}
            if request['mode'] in ('sft','continue'):
                base_id=payload.get('base_run'); base=self.path('runs',base_id)/'model'
                if not all((base/name).exists() for name in ('model.safetensors','config.json','tokenizer.json','training.json')): raise ValueError('请先选择一个已保存的基础模型。')
                base_meta=read_json(base/'training.json')
                if request['mode']=='continue' and base_meta['mode']=='sft': raise ValueError('文字续训请选择基础模型。已有问答模型请使用问答资料继续学习。')
                # Immutable snapshot: later resuming the parent cannot change this run.
                request.update(base_model=str(base),base_run=base_id)
            identity=uuid4().hex[:16]; directory=self.path('runs',identity); directory.mkdir()
            if request.get('base_model'):
                shutil.copytree(request['base_model'],directory/'base_model')
                request['base_model']=str(directory/'base_model')
            write_json(directory/'request.json',request); shutil.copyfile(dataset_path,directory/'dataset.json')
            state={'id':identity,'name':str(payload.get('name') or ('问答训练' if dataset['kind']=='qa' else '我的第一个模型'))[:80],
                'created':now(),'dataset_id':dataset['id'],'dataset_name':dataset['name'],'demo':dataset.get('demo',False),
                'status':'preparing','message':'正在启动独立训练进程。','step':0,'total_steps':steps,'metrics':[],
                'mode':request['mode'],'gentle':request['gentle'],'base_run':request.get('base_run'),
                'phase':'prepare','events':[{'time':now(),'message':'任务已创建，正在准备资料。'}]}
            write_json(directory/'state.json',state); write_json(directory/'control.json',{'action':'run'})
            try: self.process=self.spawn(['train',str(directory)],directory/'worker.log'); self.active_id=identity
            except Exception:
                state.update(status='failed',message='训练进程启动失败，请查看环境检查。'); write_json(directory/'state.json',state); raise
            return state

    def control(self,identity,action):
        with self.lock:
            self.reconcile(); directory=self.path('runs',identity); state=read_json(directory/'state.json')
            if action in ('pause','stop'):
                if self.active_id!=identity: raise ValueError('这项训练当前没有运行。')
                write_json(directory/'control.json',{'action':action}); return {'ok':True}
            if action=='resume':
                if self.process or self.inference: raise ValueError('请先等待其他训练或试用结束。')
                if not (directory/'checkpoint.pt').exists() or state['step']>=state['total_steps']: raise ValueError('没有可继续的训练进度。')
                write_json(directory/'control.json',{'action':'run'}); state.update(status='preparing',message='正在恢复保存的训练进度。')
                write_json(directory/'state.json',state)
                self.process=self.spawn(['train',str(directory)],directory/'worker.log'); self.active_id=identity
                return {'ok':True}
            raise ValueError('无效操作。')

    @staticmethod
    def _validated_run_state(value,expected_id):
        if (not isinstance(expected_id,str) or not re.fullmatch('[a-f0-9]{16}',expected_id)
                or not isinstance(value,dict) or value.get('id')!=expected_id
                or not isinstance(value.get('status'),str) or not value['status']):
            raise ValueError('训练记录状态无效。')
        dataset_id=value.get('dataset_id')
        if not isinstance(dataset_id,str) or not re.fullmatch('[a-f0-9]{16}',dataset_id):
            raise ValueError('训练记录资料编号无效。')
        base_run=value.get('base_run')
        if base_run is not None and (not isinstance(base_run,str) or not re.fullmatch('[a-f0-9]{16}',base_run)):
            raise ValueError('训练记录依赖编号无效。')
        return value

    def _matching_deletion(self,run_id):
        try: return True,read_receipt(self.root,run_id)
        except FileNotFoundError: pass
        journals=[]
        for transaction_id in transaction_ids(self.root):
            transaction=read_transaction(self.root,transaction_id)
            if transaction['transaction_id']!=transaction_id:
                raise ValueError('删除事务日志编号不匹配。')
            if transaction['run_id']==run_id: journals.append(transaction)
        if len(journals)>1: raise ValueError('同一训练记录存在多个删除事务，需要人工处理。')
        return (False,journals[0]) if journals else (False,None)

    def plan_run_deletion(self,identity):
        """Build a read-only deletion plan while holding the Studio lock."""
        with self.lock:
            self.reconcile()
            self.path('runs',identity)
            if self.active_id==identity: raise DeletionLocked('请先暂停或停止这项训练，再删除模型。')
            if self.inference and self.inference_id in (None,identity):
                raise DeletionLocked('模型正在试用，请等待本次生成完成后再删除。')
            receipt,existing=self._matching_deletion(identity)
            if existing is not None:
                return {'existing':existing,'receipt':receipt}
            directory=self.path('runs',identity)
            state=self._validated_run_state(read_json(directory/'state.json'),identity)
            dataset_id=state['dataset_id']; dataset_shared=False; uncertain=False; dependent=[]
            for path in (self.root/'runs').glob('*/state.json'):
                if path.parent==directory: continue
                other_id=path.parent.name
                try:
                    if self._dependent_is_logically_deleted(other_id): continue
                    other=self._validated_run_state(read_json(path),other_id)
                except (OSError,ValueError,json.JSONDecodeError,TypeError,KeyError):
                    uncertain=True; continue
                if other['dataset_id']==dataset_id: dataset_shared=True
                if other.get('base_run')==identity: dependent.append(other_id)
            dataset_shared=dataset_shared or uncertain
            targets=[{'kind':'run','id':identity}]
            if not dataset_shared:
                targets.append({'kind':'dataset','id':dataset_id})
                if self.path('imports',dataset_id).exists(): targets.append({'kind':'import','id':dataset_id})
            for request_dir in (self.root/'requests').iterdir():
                if not request_dir.is_dir(): continue
                try: request=read_json(request_dir/'input.json')
                except (OSError,json.JSONDecodeError): continue
                if request.get('model_id')==identity:
                    target={'kind':'request','id':request_dir.name}; validate_target(target); targets.append(target)
            return {'run_id':identity,'dataset_id':dataset_id,'dataset_shared':dataset_shared,
                'dependent_run_ids':sorted(dependent),'targets':targets}

    def prepare_deletion(self,plan):
        with self.lock:
            if plan.get('existing') is not None: return plan['existing']
            for _ in range(10):
                transaction_id=uuid4().hex; trash=self.root/'trash'/transaction_id
                try: trash.mkdir()
                except FileExistsError: continue
                break
            else: raise OSError('无法创建唯一删除事务目录。')
            transaction={'version':1,'transaction_id':transaction_id,'run_id':plan['run_id'],
                'dataset_id':plan['dataset_id'],'status':'prepared','created_at':now(),'committed_at':None,
                'dataset_shared':plan['dataset_shared'],'dependent_run_ids':plan['dependent_run_ids'],
                'targets':plan['targets'],'moved':[],'cleanup_attempts':0,'last_error':None}
            write_transaction(self.root,transaction)
            self.deletion_fault('after_prepared')
            return transaction

    def _mark_deletion_manual(self,transaction,message):
        transaction={**transaction,'status':'manual_intervention_required','last_error':message}
        write_transaction(self.root,transaction)
        return transaction

    def _move_deletion_target(self,transaction,target,fault_point=None):
        source,destination=target_locations(self.root,transaction['transaction_id'],target)
        source_exists=os.path.lexists(source); destination_exists=os.path.lexists(destination)
        if source_exists and destination_exists:
            return self._mark_deletion_manual(transaction,'source and trash target both exist')
        if not source_exists and not destination_exists:
            return self._mark_deletion_manual(transaction,'source and trash target are both missing')
        if source_exists:
            destination.parent.mkdir(parents=True,exist_ok=True)
            atomic_move_within_storage(self.root,source,destination)
            if fault_point: self.deletion_fault(fault_point)
        if target not in transaction['moved']:
            transaction={**transaction,'moved':[*transaction['moved'],target],'last_error':None}
            write_transaction(self.root,transaction)
        return transaction

    def _dependent_is_logically_deleted(self,identity):
        receipt,transaction=self._matching_deletion(identity)
        if transaction is None: return False
        if receipt or transaction['status'] in ('committed','staged','cleanup_pending','cleaned'):
            return True
        if transaction['status']=='prepared':
            source,destination=target_locations(self.root,transaction['transaction_id'],
                {'kind':'run','id':identity})
            return not os.path.lexists(source) and os.path.lexists(destination)
        return False

    def _prepare_dependent_lineage(self,transaction):
        """Durably snapshot known live child lineage before the parent commit point."""
        run_target={'kind':'run','id':transaction['run_id']}
        source,destination=target_locations(self.root,transaction['transaction_id'],run_target)
        source_exists=os.path.lexists(source); destination_exists=os.path.lexists(destination)
        if source_exists==destination_exists: return transaction,None
        parent_directory=source if source_exists else destination
        parent=self._validated_run_state(read_json(parent_directory/'state.json'),transaction['run_id'])
        parent_name=str(parent.get('name') or transaction['run_id'])[:80]
        live_dependents=[]
        for identity in transaction['dependent_run_ids']:
            directory=self.path('runs',identity)
            if not directory.exists():
                if self._dependent_is_logically_deleted(identity): continue
                raise ValueError('派生模型状态不确定，父模型删除未提交。')
            child=self._validated_run_state(read_json(directory/'state.json'),identity)
            if child.get('base_run')!=transaction['run_id']:
                raise ValueError('派生模型谱系不一致，父模型删除未提交。')
            live_dependents.append(identity)
        if live_dependents!=transaction['dependent_run_ids']:
            transaction={**transaction,'dependent_run_ids':live_dependents}
            write_transaction(self.root,transaction)
        if not source_exists:
            deleted_at=None
            for identity in live_dependents:
                try: lineage=read_lineage_snapshot(self.path('runs',identity)/'lineage.json')
                except (OSError,ValueError,json.JSONDecodeError):
                    return self._mark_deletion_manual(transaction,'dependent lineage snapshot is missing'),None
                if (lineage['transaction_id']!=transaction['transaction_id']
                        or lineage['base_run']!=transaction['run_id']):
                    return self._mark_deletion_manual(transaction,'dependent lineage snapshot does not match'),None
                if deleted_at is not None and lineage['deleted_at']!=deleted_at:
                    return self._mark_deletion_manual(transaction,'dependent lineage timestamps do not match'),None
                deleted_at=lineage['deleted_at']
            return transaction,deleted_at or now()
        deleted_at=now()
        lineage={'version':1,'transaction_id':transaction['transaction_id'],
            'base_run':transaction['run_id'],'base_run_deleted':True,
            'base_run_snapshot':{'name':parent_name},'deleted_at':deleted_at}
        for identity in live_dependents:
            write_lineage_snapshot(self.path('runs',identity)/'lineage.json',lineage)
        for identity in live_dependents:
            if read_lineage_snapshot(self.path('runs',identity)/'lineage.json')!=lineage:
                raise OSError('谱系快照校验失败。')
        self.deletion_fault('after_lineage_snapshots')
        return transaction,deleted_at

    def commit_deletion(self,transaction):
        with self.lock:
            if transaction['status']!='prepared': return transaction
            run_target={'kind':'run','id':transaction['run_id']}
            try:
                transaction,deleted_at=self._prepare_dependent_lineage(transaction)
                if transaction['status']=='manual_intervention_required': return transaction
                transaction=self._move_deletion_target(transaction,run_target,'after_run_move')
            except Exception as error:
                transaction={**transaction,'last_error':safe_error(error)}
                write_transaction(self.root,transaction); raise
            if transaction['status']=='manual_intervention_required': return transaction
            transaction={**transaction,'status':'committed','committed_at':deleted_at or now(),'last_error':None}
            write_transaction(self.root,transaction)
            self.deletion_fault('after_committed')
            return transaction

    def stage_deletion_targets(self,transaction):
        with self.lock:
            if transaction['status']!='committed': return transaction
            first_request=True
            for target in transaction['targets']:
                if target['kind']=='run': continue
                point={'dataset':'after_dataset_move','import':'after_import_move'}.get(target['kind'])
                if target['kind']=='request' and first_request:
                    point='after_first_request_move'; first_request=False
                try: transaction=self._move_deletion_target(transaction,target,point)
                except Exception as error:
                    transaction={**transaction,'last_error':safe_error(error)}
                    write_transaction(self.root,transaction); return transaction
                if transaction['status']=='manual_intervention_required': return transaction
            transaction={**transaction,'status':'staged','last_error':None}
            write_transaction(self.root,transaction)
            self.deletion_fault('after_staged')
            return transaction

    @staticmethod
    def _receipt_from_transaction(transaction):
        return {'version':1,'transaction_id':transaction['transaction_id'],'run_id':transaction['run_id'],
            'status':'cleaned','dataset_shared':transaction['dataset_shared'],
            'deleted_dataset':any(target['kind']=='dataset' for target in transaction['moved']),
            'deleted_requests':sum(target['kind']=='request' for target in transaction['moved']),
            'dependent_runs':len(transaction['dependent_run_ids']),
            'committed_at':transaction['committed_at'],'cleaned_at':now()}

    def _finalize_cleaned(self,transaction):
        transaction={**transaction,'status':'cleaned','last_error':None}
        write_transaction(self.root,transaction)
        write_receipt(self.root,self._receipt_from_transaction(transaction))
        delete_transaction(self.root,transaction['transaction_id'])

    def _cleanup_deletion_once(self,transaction_id):
        """Perform one physical attempt; rmtree always runs without Studio.lock."""
        with self.lock:
            try: transaction=read_transaction(self.root,transaction_id)
            except FileNotFoundError:
                return True
            if transaction['status']=='cleaned':
                self._finalize_cleaned(transaction); return True
            if transaction['status'] not in ('staged','cleanup_pending'): return True
            transaction={**transaction,'status':'cleanup_pending',
                'cleanup_attempts':transaction['cleanup_attempts']+1,'last_error':None}
            write_transaction(self.root,transaction)
        try: shutil.rmtree(self.root/'trash'/transaction_id)
        except FileNotFoundError: pass
        except OSError as error:
            with self.lock:
                transaction=read_transaction(self.root,transaction_id)
                transaction={**transaction,'status':'cleanup_pending','last_error':safe_error(error)}
                write_transaction(self.root,transaction)
            return False
        with self.lock:
            transaction=read_transaction(self.root,transaction_id)
            self._finalize_cleaned(transaction)
        return True

    def _cleanup_worker(self,transaction_id,auto_retry):
        task_attempts=0
        try:
            while True:
                task_attempts+=1
                try: complete=self._cleanup_deletion_once(transaction_id)
                except (OSError,ValueError,json.JSONDecodeError): complete=False
                if complete or not auto_retry or self.cleanup_stop.is_set(): return
                with self.lock:
                    try: transaction=read_transaction(self.root,transaction_id)
                    except (OSError,ValueError,json.JSONDecodeError): return
                    attempts=transaction['cleanup_attempts']
                if attempts>=CLEANUP_MAX_ATTEMPTS or task_attempts>=CLEANUP_MAX_ATTEMPTS: return
                delay=CLEANUP_RETRY_DELAYS[min(task_attempts-1,len(CLEANUP_RETRY_DELAYS)-1)]
                if self.cleanup_stop.wait(delay): return
        finally:
            with self.cleanup_tasks_lock:
                if self.cleanup_tasks.get(transaction_id) is threading.current_thread():
                    self.cleanup_tasks.pop(transaction_id,None)

    def _schedule_cleanup(self,transaction_id,auto_retry=True):
        with self.cleanup_tasks_lock:
            current=self.cleanup_tasks.get(transaction_id)
            if current and current.is_alive(): return False
            if self.cleanup_stop.is_set(): return False
            thread=threading.Thread(target=self._cleanup_worker,args=(transaction_id,auto_retry),
                name='training-cleanup-'+transaction_id[:8],daemon=True)
            self.cleanup_tasks[transaction_id]=thread; thread.start(); return True

    def retry_deletion_cleanup(self,transaction_id):
        """Explicitly request one cleanup attempt, including after automatic retries are exhausted."""
        with self.lock:
            transaction=read_transaction(self.root,transaction_id)
            if transaction['status'] not in ('staged','cleanup_pending','cleaned'):
                raise ValueError('删除事务尚未完成转移，不能清理。')
        return self._schedule_cleanup(transaction_id,auto_retry=False)

    def wait_for_deletion_cleanup(self,transaction_id,timeout=5):
        with self.cleanup_tasks_lock: thread=self.cleanup_tasks.get(transaction_id)
        if thread: thread.join(timeout=max(0,timeout))
        return not thread or not thread.is_alive()

    def recover_deletions(self):
        cleanup=[]
        with self.lock:
            try: identities=transaction_ids(self.root)
            except ValueError: identities=[]
            for transaction_id in identities:
                try: transaction=read_transaction(self.root,transaction_id)
                except (OSError,ValueError,json.JSONDecodeError):
                    continue
                if transaction['status']=='manual_intervention_required': continue
                try:
                    if transaction['status']=='prepared': transaction=self.commit_deletion(transaction)
                    if transaction['status']=='committed': transaction=self.stage_deletion_targets(transaction)
                    if transaction['status'] in ('staged','cleanup_pending'): cleanup.append(transaction_id)
                    if transaction['status']=='cleaned': self._finalize_cleaned(transaction)
                except (OSError,ValueError,json.JSONDecodeError):
                    continue
        for transaction_id in cleanup: self._schedule_cleanup(transaction_id,auto_retry=False)

    def deletion_status(self,transaction_id):
        with self.lock:
            receipt=None
            for run_id in receipt_run_ids(self.root):
                candidate=read_receipt(self.root,run_id)
                if candidate['transaction_id']==transaction_id:
                    receipt=candidate; break
            if receipt is not None:
                return {'ok':True,'transaction_id':transaction_id,'status':'cleaned',
                    'deleted_run':receipt['run_id'],'deleted_dataset':receipt['deleted_dataset'],
                    'dataset_shared':receipt['dataset_shared'],'deleted_requests':receipt['deleted_requests'],
                    'dependent_runs':receipt['dependent_runs'],'cleanup_pending':False,'last_error':None}
            transaction=read_transaction(self.root,transaction_id)
            requests=sum(target['kind']=='request' for target in transaction['moved'])
            dataset=any(target['kind']=='dataset' for target in transaction['moved'])
            return {'ok':transaction['status'] not in ('manual_intervention_required',),
                'transaction_id':transaction_id,'status':transaction['status'],
                'deleted_run':transaction['run_id'],'deleted_dataset':dataset,
                'dataset_shared':transaction['dataset_shared'],'deleted_requests':requests,
                'dependent_runs':len(transaction['dependent_run_ids']),
                'logical_deleted':any(target['kind']=='run' for target in transaction['moved']),
                'cleanup_pending':transaction['status']=='cleanup_pending',
                'last_error':transaction['last_error']}

    @staticmethod
    def _public_deletion_status(value):
        cleaned=value['status']=='cleaned'
        logical_deleted=cleaned or value.get('logical_deleted',False)
        cleanup_status=('cleaned' if cleaned else
            'manual_intervention_required' if value['status']=='manual_intervention_required' else 'pending')
        return {'deletion_id':value['transaction_id'],'run_id':value['deleted_run'],
            'logical_status':'deleted' if logical_deleted else 'not_deleted',
            'cleanup_status':cleanup_status,'dataset_shared':value['dataset_shared'],
            'deleted_dataset':value['deleted_dataset'],'deleted_requests':value['deleted_requests'],
            'dependent_runs':value['dependent_runs']}

    def deletion_api_status(self,transaction_id):
        if not isinstance(transaction_id,str) or not re.fullmatch('[a-f0-9]{32}',transaction_id):
            raise ValueError('删除事务编号无效。')
        return self._public_deletion_status(self.deletion_status(transaction_id))

    def deletion_summaries(self):
        values={}
        try: identities=transaction_ids(self.root)
        except ValueError: identities=[]
        for transaction_id in identities:
            try: values[transaction_id]=self.deletion_api_status(transaction_id)
            except (OSError,ValueError,json.JSONDecodeError): continue
        try: run_ids=receipt_run_ids(self.root)
        except ValueError: run_ids=[]
        for run_id in run_ids:
            try:
                receipt=read_receipt(self.root,run_id)
                values[receipt['transaction_id']]=self.deletion_api_status(receipt['transaction_id'])
            except (OSError,ValueError,json.JSONDecodeError): continue
        return list(values.values())

    def delete_run(self,identity):
        with self.lock:
            plan=self.plan_run_deletion(identity)
            if plan.get('receipt'): return self.deletion_status(plan['existing']['transaction_id'])
            transaction=self.prepare_deletion(plan)
            transaction=self.commit_deletion(transaction)
            transaction=self.stage_deletion_targets(transaction)
            transaction_id=transaction['transaction_id']; should_cleanup=transaction['status'] in ('staged','cleanup_pending')
        if should_cleanup: self._schedule_cleanup(transaction_id)
        return self.deletion_status(transaction_id)

    def generate(self,identity,payload):
        with self.lock:
            self.reconcile()
            if self.process or self.inference: raise ValueError('训练暂停并释放显卡后才能试用模型。')
            model=self.path('runs',identity)/'model'
            if not (model/'model.safetensors').exists(): raise ValueError('模型尚未保存，请先完成或暂停训练。')
            messages=payload.get('messages')
            if not isinstance(messages,list) or not 1<=len(messages)<=16: raise ValueError('请输入要试用的内容。')
            for message in messages:
                if (not isinstance(message,dict) or message.get('role') not in ('system','user','assistant')
                        or not isinstance(message.get('content'),str) or len(message['content'])>8000): raise ValueError('消息格式或长度无效。')
            maximum=payload.get('max_tokens',80)
            if type(maximum)!=int or not 1<=maximum<=256: raise ValueError('输出长度应为 1–256。')
            request_id=uuid4().hex; folder=self.root/'requests'/request_id; folder.mkdir()
            write_json(folder/'input.json',{'model_id':identity,'messages':messages,'max_tokens':maximum})
            process=self.spawn(['generate',str(model),'--request',str(folder/'input.json'),'--result',str(folder/'output.json')],folder/'worker.log')
            self.inference=process; self.inference_id=identity
        try:
            try: process.wait(timeout=180)
            except subprocess.TimeoutExpired: process.kill(); process.wait(); raise ValueError('试用超时，进程已停止。请缩短输入再试。') from None
            if not (folder/'output.json').exists(): raise ValueError('模型试用进程未返回结果，请检查环境或训练记录。')
            result=read_json(folder/'output.json')
            if 'error' in result: raise ValueError(result['error'])
            return result
        finally:
            with self.lock: self.inference=None; self.inference_id=None

    def close(self):
        self.cleanup_stop.set()
        deadline=time.monotonic()+CLEANUP_CLOSE_TIMEOUT
        with self.cleanup_tasks_lock: cleanup_threads=list(self.cleanup_tasks.values())
        for thread in cleanup_threads:
            remaining=deadline-time.monotonic()
            if remaining<=0: break
            thread.join(remaining)
        with self.lock:
            if self.process and self.process.poll() is None:
                write_json(self.path('runs',self.active_id)/'control.json',{'action':'pause'})
                try: self.process.wait(timeout=12)
                except subprocess.TimeoutExpired: self.process.kill(); self.process.wait()
                self.reconcile()
            if self.inference and self.inference.poll() is None: self.inference.kill(); self.inference.wait()
            self.import_owner.close()
            self.owner.close()


def math_ceil_multiple(value,multiple):
    import math
    return math.ceil(value/multiple)*multiple


async def payload(request):
    body=bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body)>32*1024*1024: raise HTTPException(413,'一次导入最多 32 MB，请分批整理。')
    try:
        value=json.loads(body)
        if not isinstance(value,dict): raise ValueError()
        return value
    except (ValueError,UnicodeError): raise HTTPException(400,'请求内容不是有效 JSON。') from None


def create_app(studio):
    @asynccontextmanager
    async def lifespan(app):
        yield
        studio.close()
    app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None)
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=['127.0.0.1','localhost'])
    @app.middleware('http')
    async def local_boundary(request,call_next):
        if request.url.path.startswith('/api/') and request.method!='GET':
            if not secrets.compare_digest(request.headers.get('x-training-token',''),studio.csrf):
                return JSONResponse({'detail':'页面已失效，请刷新训练工作室。'},status_code=403)
            origin=request.headers.get('origin')
            if origin and origin!=str(request.base_url).rstrip('/'):
                return JSONResponse({'detail':'只接受当前工作室页面的操作。'},status_code=403)
        response=await call_next(request)
        response.headers['Cache-Control']='no-store'; response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; connect-src 'self'"
        return response
    @app.exception_handler(ValueError)
    async def invalid(request,exc): return JSONResponse({'detail':str(exc)},status_code=400)
    @app.exception_handler(FileNotFoundError)
    async def missing(request,exc): return JSONResponse({'detail':'这条记录不存在。'},status_code=404)
    @app.get('/',response_class=HTMLResponse)
    def index(): return Path(__file__).with_name('studio.html').read_text(encoding='utf-8').replace('__SESSION_TOKEN__',studio.csrf)
    @app.get('/studio.js')
    def javascript(): return FileResponse(Path(__file__).with_name('studio.js'),media_type='text/javascript')
    @app.get('/deletion_ui.js')
    def deletion_javascript(): return FileResponse(Path(__file__).with_name('deletion_ui.js'),media_type='text/javascript')
    @app.get('/health')
    def health(): return {'application':'forgepilot-training-studio','instance':studio.instance}
    @app.get('/api/state')
    def state(): return studio.snapshot()
    @app.get('/api/environment')
    def environment():
        reports=list((ROOT/'logs/training-environment').glob('check-*.json'))
        latest=read_json(max(reports,key=lambda p:p.stat().st_mtime)) if reports else {}
        return {'python':sys.version.split()[0],'gpu':latest.get('stack',{}).get('device','未检查'),
            'verified':latest.get('passed',False),'bf16':latest.get('stack',{}).get('bf16_supported',False)}
    @app.post('/api/datasets')
    async def dataset(request:Request): return studio.add_dataset(await payload(request))
    @app.get('/api/datasets/{identity}')
    def dataset_preview(identity:str,offset:int=0): return studio.preview(read_json(studio.path('datasets',identity)/'data.json'),offset)
    @app.post('/api/imports')
    async def import_files(request:Request): return studio.start_import(await payload(request))
    @app.get('/api/imports/{identity}')
    def import_status(identity:str,offset:int=0):
        directory=studio.path('imports',identity); state=read_json(directory/'state.json')
        if state['status']=='ready': state['preview']=studio.preview(read_json(directory/'result.json'),offset)
        return state
    @app.post('/api/imports/{identity}/save')
    def import_save(identity:str): return studio.save_import(identity)
    @app.post('/api/demo')
    async def demo(request:Request):
        value=await payload(request); kind=value.get('kind','text')
        return studio.add_dataset({'name':'C++ 入门体验问答' if kind=='qa' else 'C++ 入门体验资料','files':demo_files(kind),'_demo':True})
    @app.get('/api/template')
    def template(): return JSONResponse({'question':'Qt 的信号和槽是什么？','answer':'信号用于通知事件发生，槽用于接收并处理事件。'})
    @app.post('/api/runs')
    async def run(request:Request): return studio.start(await payload(request))
    @app.post('/api/runs/{identity}/pause')
    def pause_run(identity:str): return studio.control(identity,'pause')
    @app.post('/api/runs/{identity}/stop')
    def stop_run(identity:str): return studio.control(identity,'stop')
    @app.post('/api/runs/{identity}/resume')
    def resume_run(identity:str): return studio.control(identity,'resume')
    def deletion_response(value):
        status=200 if value['cleanup_status']=='cleaned' else 202
        if value['logical_status']=='not_deleted':
            return JSONResponse({**value,'detail':'删除未提交，模型仍然保留；该事务需要人工检查。'},status_code=409)
        return JSONResponse(value,status_code=status)
    @app.delete('/api/runs/{identity}')
    def delete_run(identity:str):
        try:
            result=studio.delete_run(identity)
            return deletion_response(studio.deletion_api_status(result['transaction_id']))
        except DeletionLocked as error:
            raise HTTPException(423,str(error)) from None
        except ValueError:
            raise HTTPException(409,'删除前检查发现状态冲突，训练记录保持不变。') from None
        except OSError:
            raise HTTPException(503,'本机存储暂时无法完成删除准备，请稍后重试。') from None
    @app.get('/api/deletions/{transaction_id}')
    def deletion_status(transaction_id:str):
        if not re.fullmatch('[a-f0-9]{32}',transaction_id):
            raise HTTPException(400,'删除事务编号无效。')
        try: return studio.deletion_api_status(transaction_id)
        except FileNotFoundError: raise
        except (ValueError,json.JSONDecodeError):
            raise HTTPException(409,'删除事务状态无法安全读取，需要人工检查。') from None
        except OSError:
            raise HTTPException(503,'本机存储暂时无法读取删除状态。') from None
    @app.post('/api/deletions/{transaction_id}/retry')
    def retry_deletion(transaction_id:str):
        if not re.fullmatch('[a-f0-9]{32}',transaction_id):
            raise HTTPException(400,'删除事务编号无效。')
        try: value=studio.deletion_api_status(transaction_id)
        except FileNotFoundError: raise
        except (ValueError,json.JSONDecodeError):
            raise HTTPException(409,'删除事务状态无法安全读取，需要人工检查。') from None
        except OSError:
            raise HTTPException(503,'本机存储暂时无法读取删除状态。') from None
        if value['cleanup_status']=='cleaned': return deletion_response(value)
        if value['cleanup_status']=='manual_intervention_required' or value['logical_status']!='deleted':
            return JSONResponse({**value,'detail':'该删除事务需要人工检查，未自动重试。'},status_code=409)
        studio.retry_deletion_cleanup(transaction_id)
        return deletion_response(studio.deletion_api_status(transaction_id))
    @app.post('/api/generate/{identity}')
    async def generate(identity:str,request:Request):
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(studio.generate,identity,await payload(request))
    @app.get('/api/export/{identity}')
    def export(identity:str):
        with studio.lock:
            studio.reconcile()
            if studio.active_id==identity: raise ValueError('请先暂停或完成训练再导出。')
            directory=studio.path('runs',identity); model=directory/'model'
            required=['model.safetensors','config.json','tokenizer.json','training.json','MODEL_CARD.md']
            if not all((model/name).is_file() for name in required): raise ValueError('模型尚未完整保存。')
            newest=max((model/name).stat().st_mtime_ns for name in required)
            destination=directory/f'forgepilot-model-{identity}-{newest}.zip'
            if not destination.exists():
                with zipfile.ZipFile(destination,'w',zipfile.ZIP_STORED) as archive:
                    for name in required: archive.write(model/name,name)
                    source=model/'studio_model.py'
                    archive.write(source if source.exists() else Path(__file__).with_name('studio_model.py'),'studio_model.py')
            return FileResponse(destination,filename=destination.name,media_type='application/zip')
    @app.get('/api/connection/{identity}')
    def connection(identity:str,request:Request):
        if not secrets.compare_digest(request.headers.get('x-training-token',''),studio.csrf): raise HTTPException(403,'请刷新页面。')
        if not (studio.path('runs',identity)/'model/model.safetensors').exists(): raise ValueError('模型尚未保存。')
        return {'base_url':str(request.base_url).rstrip('/')+'/v1','model':'forgepilot-'+identity,'api_key':studio.api_key,
                'capabilities':'仅文本，未验证工具调用；不会修改现有 ForgePilot 配置。'}
    @app.post('/v1/chat/completions')
    async def completions(request:Request):
        if not secrets.compare_digest(request.headers.get('authorization',''),'Bearer '+studio.api_key): raise HTTPException(401,'Invalid local API key')
        value=await payload(request)
        if value.get('tools'): raise HTTPException(422,'此自研模型尚未验证工具调用，仅支持文本试用。')
        if value.get('stream'): raise HTTPException(422,'仅支持非流式输出。')
        identity=str(value.get('model','')).removeprefix('forgepilot-')
        from starlette.concurrency import run_in_threadpool
        result=await run_in_threadpool(studio.generate,identity,value)
        return {'id':'local-'+uuid4().hex,'object':'chat.completion','model':value['model'],
            'choices':[{'index':0,'message':{'role':'assistant','content':result['text']},'finish_reason':result['finish_reason']}],
            'usage':{'prompt_tokens':result['prompt_tokens'],'completion_tokens':result['completion_tokens'],
                     'total_tokens':result['prompt_tokens']+result['completion_tokens']}}
    @app.post('/api/shutdown')
    def shutdown(): studio.shutdown_callback(); return {'ok':True}
    return app


def lock_storage(root):
    import msvcrt
    handle=(root/'server.lock').open('a+b')
    handle.seek(0); handle.write(b'1'); handle.flush(); handle.seek(0)
    try: msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
    except OSError: handle.close(); return None
    return handle


def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--port',type=int,default=8787)
    parser.add_argument('--storage',type=Path,default=DEFAULT_STORAGE); parser.add_argument('--no-browser',action='store_true')
    parser.add_argument('--stop',action='store_true'); args=parser.parse_args()
    root=args.storage.resolve(); root.mkdir(parents=True,exist_ok=True)
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def existing():
        data=read_json(root/'server.json'); url=f'http://127.0.0.1:{int(data["port"])}'
        with opener.open(url+'/health',timeout=2) as response: health=json.load(response)
        if health!={'application':'forgepilot-training-studio','instance':data['instance']}: raise ValueError('服务标识不匹配。')
        return data,url
    if args.stop:
        try:
            data,url=existing(); request=urllib.request.Request(url+'/api/shutdown',data=b'{}',headers={'X-Training-Token':data['token'],'Content-Type':'application/json'})
            with opener.open(request,timeout=3): pass
            print('训练工作室正在保存并退出。'); return 0
        except (OSError,ValueError): print('没有找到本工作室正在运行的服务。'); return 0
    handle=lock_storage(root)
    if handle is None:
        try:
            _,url=existing()
            if not args.no_browser: webbrowser.open(url)
            print('训练工作室已运行：'+url); return 0
        except (OSError,ValueError): print('工作室正在启动或被另一进程占用，请稍后再试。'); return 1
    studio=None
    try:
        listener=socket.socket(); listener.bind(('127.0.0.1',args.port)); listener.listen(128)
        studio=Studio(root); studio.recover(); app=create_app(studio)
        server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=args.port,log_level='warning',timeout_graceful_shutdown=20))
        studio.shutdown_callback=lambda:setattr(server,'should_exit',True)
        write_json(root/'server.json',{'port':args.port,'instance':studio.instance,'token':studio.csrf})
        if not args.no_browser:
            def open_when_ready():
                for _ in range(100):
                    if server.started: webbrowser.open(f'http://127.0.0.1:{args.port}'); return
                    time.sleep(0.1)
            threading.Thread(target=open_when_ready,daemon=True).start()
        print(f'训练工作室：http://127.0.0.1:{args.port}；关闭浏览器不会停止训练，请用页面中的退出按钮。',flush=True)
        server.run(sockets=[listener]); listener.close(); return 0
    except OSError as exc:
        print(f'无法启动训练工作室（端口可能已被占用）：{exc}'); return 1
    finally:
        if studio: studio.close()
        handle.close()


if __name__=='__main__': raise SystemExit(main())
