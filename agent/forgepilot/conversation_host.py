"""Serialize admission on the existing worker loop; responses are durable and idempotent."""
import asyncio
import sqlite3
from .errors import ForgePilotError


class ConversationHost:
    def __init__(self, tasks, chat_factory):
        self.tasks, self.store, self.chat_factory=tasks,tasks.store,chat_factory
        self._answers={}
        self._deletions={}
        tasks.extra_active=lambda:len(self._answers)

    async def submit(self,cid,rid,mode,prompt):
        return await self.tasks._submit(self._submit(cid,rid,mode,prompt))

    async def _submit(self,cid,rid,mode,prompt):
        if cid in self._deletions:
            raise ForgePilotError('conversation_deleting','对话正在删除，不能再发送请求。')
        retry=self.store.request_retry(cid,rid,mode,prompt)
        if retry: return retry
        if self.tasks._stopping:
            raise ForgePilotError('server_stopping','服务正在关闭。')
        if len(self.tasks._tasks)+len(self._answers)>=self.tasks.max_tasks:
            raise ForgePilotError('task_capacity','活动请求数量已达上限，请等待当前请求完成。')
        if mode=='chat' and self.chat_factory is None:
            raise ForgePilotError('llm_config','问答模型尚未配置。')
        _,notice=self.store.conversation_context(cid)
        request=self.store.begin_conversation_request(cid,rid,mode,prompt,notice)
        if mode=='task':
            try:
                await self.tasks._create(request['repository_path'],prompt,request_id=rid,
                    prepare_context=lambda:self._prepare_task_context(request),background=lambda:self.store.memory_background(cid,request['repository_path']))
            except Exception as exc:
                if isinstance(exc, ForgePilotError) and exc.code == 'storage_unavailable':
                    self.store.mark_request_unavailable(rid)
                    raise
                error=self.store.redactor.text(str(exc)) if isinstance(exc,ForgePilotError) else 'task_internal'
                try:
                    self.store.finish_conversation_request(rid,'failed','执行任务未能启动。\n'+error,error)
                except (sqlite3.Error, ForgePilotError):
                    self.store.mark_request_unavailable(rid)
                    raise ForgePilotError('storage_unavailable','Request persistence failed; durable status is unavailable') from None
            return self.store.conversation_request(cid,rid)
        started=asyncio.Event()
        snapshots=() if self.tasks.mcp_disabled else self.tasks.mcp_service.snapshots('chat')
        self._answers[rid]=asyncio.create_task(self._answer(request,started,snapshots))
        await started.wait()
        return self.store.conversation_request(cid,rid)

    async def _answer(self,request,started,snapshots):
        rid=request['request_id']; service=None
        status,content,error='failed','回答失败，请检查后端。',None
        started.set()
        try:
            service=self.chat_factory()
            if hasattr(service,'configure_mcp'):
                service.configure_mcp(snapshots,connection_factory=self.tasks.mcp_service.connection_factory,
                    on_tool_event=lambda record:self.store.record_mcp_call(request['conversation_id'],rid,record))
            history,notice=await self._prepare_context(request,service)
            content=await service.answer(history,request['prompt'],request['repository_path'],notice,
                background=lambda:self.store.memory_background(request['conversation_id'],request['repository_path']))
            status='completed'
        except asyncio.CancelledError:
            status,content='cancelled','已停止回答，可以继续提问。'
        except ForgePilotError as exc:
            redactor=getattr(service,'redactor',self.store.redactor)
            error=redactor.text(str(exc)); content='回答未完成。\n'+error
        except Exception:
            error='chat_internal'; content='回答发生内部错误，请稍后重试。'
        finally:
            try:
                if service:
                    # A stop arriving after the model reply must not interrupt resource cleanup.
                    cleanup=asyncio.create_task(service.close())
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        await asyncio.shield(cleanup)
            finally:
                try:
                    redactor=getattr(service,'redactor',self.store.redactor)
                    self.store.finish_conversation_request(rid,status,redactor.text(content),redactor.text(error) if error else None)
                except (sqlite3.Error, ForgePilotError):
                    self.store.mark_request_unavailable(rid)
                finally:
                    self._answers.pop(rid,None)

    async def _prepare_context(self,request,service):
        cid,rid=request['conversation_id'],request['request_id']; warning=''
        # At most two bounded compactions per user request; no unbounded catch-up loop.
        for _ in range(2):
            plan=self.store.summary_plan(cid)
            if not plan: break
            try:
                content=await service.summarize(plan)
                if cid in self._deletions or asyncio.current_task().cancelling(): raise asyncio.CancelledError()
                if not self.store.commit_summary(plan,content,rid): break
            except asyncio.CancelledError: raise
            except Exception:
                warning='摘要生成未完成，本轮使用已有摘要和可容纳的近期原文；全部原始消息仍保留。'
                break
        history,notice=self.store.conversation_context(cid)
        notice=' '.join(v for v in (warning,notice) if v)
        self.store.set_context_notice(rid,notice)
        return history,notice

    async def _prepare_task_context(self,request):
        # This runs inside TaskService's cancellable execution, so task IDs and Stop stay available.
        if not self.chat_factory or not self.store.summary_plan(request['conversation_id']):
            return self.store.conversation_context(request['conversation_id'])[0]
        service=self.chat_factory()
        try:
            return (await self._prepare_context(request,service))[0]
        finally:
            cleanup=asyncio.create_task(service.close())
            try: await asyncio.shield(cleanup)
            except asyncio.CancelledError: await asyncio.shield(cleanup); raise

    async def save_memory(self,values,mid=None):
        async def save():
            source=self.store.memory_source(mid) if mid else values.get('source_conversation_id')
            if source in self._deletions: raise ForgePilotError('conversation_deleting','来源对话正在删除，不能保存记忆。')
            return self.store.save_memory(values,mid)
        return await self.tasks._submit(save())

    async def edit_summary(self,cid,revision,**changes):
        async def edit():
            if cid in self._deletions: raise ForgePilotError('conversation_deleting','对话正在删除，不能修改摘要。')
            return self.store.edit_summary(cid,revision,**changes)
        return await self.tasks._submit(edit())

    async def stop(self,cid,rid):
        return await self.tasks._submit(self._stop(cid,rid))

    async def _stop(self,cid,rid):
        request=self.store.conversation_request(cid,rid)
        if request['mode']!='chat':
            raise ForgePilotError('invalid_state','执行任务请使用“停止任务”，不能通过“停止回答”取消。')
        task=self._answers.get(rid)
        if task and not task.done():
            if not task.cancelling(): task.cancel()
            await asyncio.shield(task)
        return self.store.conversation_request(cid,rid)

    async def close(self):
        async def stop_all():
            self.tasks._stopping=True
            await asyncio.gather(*list(self._deletions.values()),return_exceptions=True)
            pending=list(self._answers.values())
            for task in pending:
                if not task.cancelling(): task.cancel()
            await asyncio.gather(*pending,return_exceptions=True)
        await self.tasks._submit(stop_all())

    async def delete(self,cid,stop_active=False):
        return await self.tasks._submit(self._delete(cid,stop_active))

    async def _delete(self,cid,stop_active):
        existing=self._deletions.get(cid)
        if existing is not None:
            return await asyncio.shield(existing)
        exists,requests=self.store.conversation_deletion_info(cid)
        if not exists:
            return {'conversation_id':cid,'deleted':True}
        if any(r['status']=='running' or r['request_id'] in self._answers or r['task_id'] in self.tasks._tasks for r in requests) and not stop_active:
            raise ForgePilotError('conversation_busy','本对话仍在运行，请确认“停止任务并删除”。')
        for r in requests:
            entry=self.tasks._tasks.get(r['task_id'])
            if entry: entry.cancel_requested=True
        operation=asyncio.create_task(self._delete_owned(cid,requests))
        self._deletions[cid]=operation
        try:
            return await asyncio.shield(operation)
        finally:
            self._deletions.pop(cid,None)

    async def _delete_owned(self,cid,requests):
        for r in requests:
            if r['task_id']:
                await self.tasks.stop_for_deletion(r['task_id'])
            answer=self._answers.get(r['request_id'])
            if answer is not None:
                if not answer.done() and not answer.cancelling(): answer.cancel()
                await asyncio.shield(answer)
        return self.store.delete_conversation(cid)
