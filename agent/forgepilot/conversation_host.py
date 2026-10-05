"""Serialize admission on the existing worker loop; responses are durable and idempotent."""
import asyncio
import sqlite3
from time import monotonic
from .errors import ForgePilotError
from .conversation_stream import ConversationStreamBroker
from .stream_metrics import StreamMetrics

STOP_SUFFIX='\n\n[回答已由用户停止]'
FAILURE_SUFFIX='\n\n[回答因错误未完成，请稍后重试]'
MAX_FINAL_ANSWER=32_768


class ConversationHost:
    def __init__(self, tasks, chat_factory, *, stream_retention_seconds=60.0,
                 stream_max_subscribers=8):
        self.tasks, self.store, self.chat_factory=tasks,tasks.store,chat_factory
        self._answers={}
        self._deletions={}
        self._streams={}
        self._stream_releases={}
        self._stream_retention_seconds=stream_retention_seconds
        self._stream_max_subscribers=stream_max_subscribers
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
        metrics=StreamMetrics()
        broker=ConversationStreamBroker(cid,rid,max_subscribers=self._stream_max_subscribers,
                                        metrics=metrics)
        self._streams[rid]=broker
        self._answers[rid]=asyncio.create_task(self._answer(request,started,snapshots))
        await started.wait()
        return self.store.conversation_request(cid,rid)

    async def _answer(self,request,started,snapshots):
        rid=request['request_id']; service=None
        broker=self._streams[rid]
        status,content,error='failed','回答未完成，请稍后重试。','chat_internal'
        error_code='chat_internal'
        started.set()
        try:
            service=self.chat_factory()
            if hasattr(service,'configure_mcp'):
                service.configure_mcp(snapshots,connection_factory=self.tasks.mcp_service.connection_factory,
                    on_tool_event=lambda record:self.store.record_mcp_call(request['conversation_id'],rid,record))
            history,notice=await self._prepare_context(request,service)
            content=await service.answer(history,request['prompt'],request['repository_path'],notice,
                background=lambda:self.store.memory_background(request['conversation_id'],request['repository_path']),
                on_event=broker.publish,diagnostics=broker.metrics)
            status='completed'
        except asyncio.CancelledError:
            status='cancelled'
        except ForgePilotError as exc:
            error_code=exc.code
            error=error_code
        except Exception:
            error=error_code
        finally:
            cleanup_failed=False
            try:
                if service:
                    # A stop arriving after the model reply must not interrupt resource cleanup.
                    cleanup=asyncio.create_task(service.close())
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        await asyncio.shield(cleanup)
                    except Exception:
                        cleanup_failed=True
            finally:
                if broker.stop_requested:
                    status,error,error_code='cancelled',None,'cancelled'
                elif cleanup_failed:
                    status,error,error_code='failed','chat_internal','chat_internal'
                redactor=getattr(service,'redactor',self.store.redactor)
                if status=='completed':
                    content=redactor.text(content)
                    try:
                        broker.validate_completion(content)
                    except ForgePilotError:
                        status,error,error_code='failed','conversation_stream_state','conversation_stream_state'
                if status=='cancelled':
                    content=self._partial_content(broker.draft,STOP_SUFFIX,'已停止回答，可以继续提问。')
                elif status=='failed':
                    content=self._partial_content(broker.draft,FAILURE_SUFFIX,'回答未完成，请稍后重试。')
                content=redactor.text(content)
                persist_started=monotonic()
                try:
                    committed=self.store.finish_conversation_request(rid,status,content,error)
                except (sqlite3.Error, ForgePilotError):
                    broker.metrics.set_error('storage_unavailable')
                    self.store.mark_request_unavailable(rid)
                    try:
                        broker.finish('failed',error_code='storage_unavailable',persisted=False)
                    except ForgePilotError:
                        broker.shutdown()
                else:
                    message_id=committed['message_id'] if committed else None
                    persisted_status=committed['request']['status'] if committed else status
                    try:
                        if persisted_status=='completed':
                            broker.finish('completed',content=content,message_id=message_id)
                        elif persisted_status=='cancelled':
                            broker.finish('cancelled',content=content,message_id=message_id)
                        else:
                            broker.finish('failed',content=content,message_id=message_id,error_code=error_code)
                    except ForgePilotError:
                        broker.metrics.set_error('conversation_stream_state')
                        # The durable result remains authoritative; never emit a
                        # contradictory terminal event after it commits.
                        broker.shutdown()
                finally:
                    broker.metrics.persist_finished(persist_started)
                    if status!='completed' and broker.metrics.snapshot()['stream_error_code']=='none':
                        broker.metrics.set_error(error_code)
                    broker.metrics.emit()
                    self._answers.pop(rid,None)
                    self._schedule_stream_release(rid,broker)

    @staticmethod
    def _partial_content(draft,suffix,empty):
        if not draft:
            return empty
        return draft[:MAX_FINAL_ANSWER-len(suffix)]+suffix

    def _schedule_stream_release(self,rid,broker):
        async def release():
            try:
                await asyncio.sleep(self._stream_retention_seconds)
                if self._streams.get(rid) is broker:
                    self._streams.pop(rid,None)
                    broker.shutdown()
            finally:
                if self._stream_releases.get(rid) is asyncio.current_task():
                    self._stream_releases.pop(rid,None)
        previous=self._stream_releases.pop(rid,None)
        if previous is not None:
            previous.cancel()
        self._stream_releases[rid]=asyncio.create_task(release())

    def conversation_stream(self,cid,rid,last_event_id=None):
        request,message=self.store.conversation_stream_state(cid,rid)
        if request['mode']!='chat':
            raise ForgePilotError('invalid_state','执行任务没有回答流。')
        broker=self._streams.get(rid)
        if broker is None:
            if request['status']=='running':
                raise ForgePilotError('conversation_stream_unavailable','回答流当前不可用。')
            broker=ConversationStreamBroker.released_terminal(cid,rid,request['status'],
                content=message['content'] if message else None,
                message_id=message['message_id'] if message else None)
        return broker.subscribe(last_event_id)

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
            broker=self._streams.get(rid)
            if broker: broker.stop()
            if not task.cancelling(): task.cancel()
            await asyncio.shield(task)
        return self.store.conversation_request(cid,rid)

    async def close(self):
        async def stop_all():
            self.tasks._stopping=True
            await asyncio.gather(*list(self._deletions.values()),return_exceptions=True)
            pending=list(self._answers.values())
            for rid in tuple(self._answers):
                broker=self._streams.get(rid)
                if broker: broker.stop()
            for task in pending:
                if not task.cancelling(): task.cancel()
            await asyncio.gather(*pending,return_exceptions=True)
            releases=list(self._stream_releases.values())
            for task in releases:
                task.cancel()
            await asyncio.gather(*releases,return_exceptions=True)
            self._stream_releases.clear()
            for broker in self._streams.values():
                broker.shutdown()
            self._streams.clear()
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
                broker=self._streams.get(r['request_id'])
                if broker: broker.stop()
                if not answer.done() and not answer.cancelling(): answer.cancel()
                await asyncio.shield(answer)
        result=self.store.delete_conversation(cid)
        for r in requests:
            rid=r['request_id']
            release=self._stream_releases.pop(rid,None)
            if release: release.cancel()
            broker=self._streams.pop(rid,None)
            if broker: broker.shutdown()
        return result
