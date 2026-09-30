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

ROOT=Path(__file__).resolve().parents[1]
DEFAULT_STORAGE=ROOT/'logs/model-training/studio'
ACTIVE={'preparing','running','pausing'}


def now(): return datetime.now(timezone.utc).isoformat()


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
        for folder in ('datasets','runs','requests','imports'): (self.root/folder).mkdir(exist_ok=True)
        self.lock=threading.RLock(); self.process=None; self.active_id=None; self.inference=None
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
            self.reconcile(); datasets=[]; runs=[]
            for path in (self.root/'datasets').glob('*/data.json'):
                item=read_json(path); datasets.append({k:v for k,v in item.items() if k!='documents'})
            for path in (self.root/'runs').glob('*/state.json'):
                item=read_json(path); artifact=path.parent/'model/model.safetensors'
                item['has_model']=artifact.exists(); item['can_resume']=(path.parent/'checkpoint.pt').exists() and item['step']<item['total_steps']
                if item['id']==self.active_id:
                    control=read_json(path.parent/'control.json').get('action')
                    if control in ('pause','stop'): item['pending_action']=control
                runs.append(item)
            return {'datasets':sorted(datasets,key=lambda x:x['created'],reverse=True),
                'runs':sorted(runs,key=lambda x:x['created'],reverse=True),'active_run':self.active_id,
                'inference_busy':self.inference is not None,'storage':str(self.root)}

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
            write_json(folder/'input.json',{'messages':messages,'max_tokens':maximum})
            process=self.spawn(['generate',str(model),'--request',str(folder/'input.json'),'--result',str(folder/'output.json')],folder/'worker.log')
            self.inference=process
        try:
            try: process.wait(timeout=180)
            except subprocess.TimeoutExpired: process.kill(); process.wait(); raise ValueError('试用超时，进程已停止。请缩短输入再试。') from None
            if not (folder/'output.json').exists(): raise ValueError('模型试用进程未返回结果，请检查环境或训练记录。')
            result=read_json(folder/'output.json')
            if 'error' in result: raise ValueError(result['error'])
            return result
        finally:
            with self.lock: self.inference=None

    def close(self):
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
    @app.post('/api/runs/{identity}/{action}')
    def control(identity:str,action:str): return studio.control(identity,action)
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
