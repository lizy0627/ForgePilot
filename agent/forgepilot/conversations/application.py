"""Serialize admission on the existing worker loop; responses are durable and idempotent."""
import asyncio
import sqlite3
from time import monotonic
from ..errors import ForgePilotError
from .stream import ConversationStreamBroker
from ..stream_metrics import StreamMetrics
from .service import chat_tool_route,web_mcp_snapshots
from ..repository import RepositoryCache

STOP_SUFFIX='\n\n[回答已由用户停止]'
FAILURE_SUFFIX='\n\n[回答因错误未完成，请稍后重试]'
MAX_FINAL_ANSWER=32_768
SUMMARY_IDLE_SECONDS=0.05


class ConversationHost:
    def __init__(self, tasks, chat_factory, *, summary_factory=None, stream_retention_seconds=60.0,
                 stream_max_subscribers=8, repository_cache=None, runtime=None, capacity=None, mcp_service=None, mcp_disabled=None):
        self.tasks, self.store, self.chat_factory=tasks,tasks.store,chat_factory
        self.runtime = runtime if runtime is not None else tasks.runtime
        self.capacity = capacity if capacity is not None else tasks.capacity
        self.mcp_service = mcp_service if mcp_service is not None else tasks.mcp_service
        self.mcp_disabled = tasks.mcp_disabled if mcp_disabled is None else mcp_disabled
        # Existing embedders that provide one factory keep the original behavior.
        self.summary_factory=summary_factory or chat_factory
        self._answers={}
        self._deletions={}
        self._streams={}
        self._stream_releases={}
        self._summary_tasks={}
        self._summary_slot=asyncio.Semaphore(1)
        self.repository_cache=repository_cache or RepositoryCache()
        self._stream_retention_seconds=stream_retention_seconds
        self._stream_max_subscribers=stream_max_subscribers

    async def submit(self,cid,rid,mode,prompt,tool_mode='auto',use_rag=True):
        request_started=monotonic()
        return await self.runtime.submit(self._submit(cid,rid,mode,prompt,tool_mode,use_rag,request_started))

    async def _submit(self,cid,rid,mode,prompt,tool_mode,use_rag,request_started):
        if cid in self._deletions:
            raise ForgePilotError('conversation_deleting','对话正在删除，不能再发送请求。')
        retry=self.store.request_retry(cid,rid,mode,prompt)
        if retry: return retry
        if self.runtime.stopping:
            raise ForgePilotError('server_stopping','服务正在关闭。')
        self.capacity.check_available('活动请求数量已达上限，请等待当前请求完成。')
        if mode=='chat':
            if self.chat_factory is None:
                raise ForgePilotError('llm_config','问答模型尚未配置。')
            has_repository=bool(self.store.require_conversation(cid)['repository_path'])
            route=chat_tool_route(tool_mode,prompt,has_repository)
            snapshots=self._web_snapshots(route)
        _,notice=self.store.conversation_context(cid)
        request=self.store.begin_conversation_request(cid,rid,mode,prompt,notice)
        try:
            if mode=='task':
                await self.tasks.create_for_conversation(request['repository_path'],prompt,request_id=rid,use_rag=use_rag,
                    prepare_context=lambda:self._prepare_task_context(request),background=lambda:self.store.memory_background(cid,request['repository_path']))
            else:
                # Project association can change on the API thread before admission.
                if has_repository!=bool(request['repository_path']):
                    route=chat_tool_route(tool_mode,prompt,bool(request['repository_path']))
                    snapshots=self._web_snapshots(route)
                started=self._start_answer(request,route,snapshots,request_started,len(prompt))
                await started.wait()
        except Exception as exc:
            if mode=='task':
                if isinstance(exc, ForgePilotError) and exc.code == 'storage_unavailable':
                    self.store.mark_request_unavailable(rid)
                    raise
                error=self.store.redactor.text(str(exc)) if isinstance(exc,ForgePilotError) else 'task_internal'
                content='执行任务未能启动。\n'+error
            else:
                error=exc.code if isinstance(exc,ForgePilotError) else 'chat_internal'
                content='回答未完成，请稍后重试。'
            try:
                self.store.finish_conversation_request(rid,'failed',content,error)
            except (sqlite3.Error, ForgePilotError):
                self.store.mark_request_unavailable(rid)
                raise ForgePilotError('storage_unavailable','Request persistence failed; durable status is unavailable') from None
        return self.store.conversation_request(cid,rid)

    def _web_snapshots(self,route):
        if self.mcp_disabled or not route.web:
            return ()
        return web_mcp_snapshots(self.mcp_service.snapshots('chat'))

    def _start_answer(self,request,route,snapshots,request_started,input_chars):
        cid,rid=request['conversation_id'],request['request_id']
        started=asyncio.Event()
        metrics=StreamMetrics(started=request_started,mode='chat',
            project_tools_enabled=route.project,mcp_enabled=bool(snapshots),
            request_input_chars=input_chars,tool_mode=route.mode)
        broker=ConversationStreamBroker(cid,rid,max_subscribers=self._stream_max_subscribers,
                                        metrics=metrics)
        self.capacity.acquire('chat',rid,message='活动请求数量已达上限，请等待当前请求完成。')
        self._streams[rid]=broker
        answer=self._answer(request,started,snapshots,route.mode)
        try:
            metrics.request_prepared()
            self._answers[rid]=asyncio.create_task(answer)
        except Exception:
            answer.close()
            self._streams.pop(rid)
            self.capacity.release('chat',rid)
            raise
        return started

    async def _answer(self,request,started,snapshots,tool_mode):
        rid=request['request_id']; service=None
        broker=self._streams[rid]
        status,content,error='failed','回答未完成，请稍后重试。','chat_internal'
        error_code='chat_internal'
        started.set()
        try:
            service=self.chat_factory()
            if hasattr(service,'bind_repository_cache'):
                service.bind_repository_cache(self.repository_cache)
            if hasattr(service,'configure_mcp'):
                service.configure_mcp(snapshots,connection_factory=self.mcp_service.connection_factory,
                    on_tool_event=lambda record:self.store.record_mcp_call(request['conversation_id'],rid,record))
            history,notice=await self._prepare_context(request,broker.metrics)
            content=await service.answer(history,request['prompt'],request['repository_path'],notice,
                background=lambda:self.store.memory_background(request['conversation_id'],request['repository_path']),
                on_event=broker.publish,diagnostics=broker.metrics,tool_mode=tool_mode)
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
                committed=None; persisted_status=None
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
                    if status!='completed' and broker.metrics.snapshot()['request_error_code']=='none':
                        broker.metrics.set_error(error_code)
                    broker.metrics.request_finished()
                    self._answers.pop(rid,None)
                    self.capacity.release('chat',rid)
                    summary_queued=(persisted_status=='completed' and committed is not None
                                    and self._queue_summary(request['conversation_id'],broker.metrics))
                    if not summary_queued: broker.metrics.emit()
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

    async def _prepare_context(self,request,metrics=None):
        cid,rid=request['conversation_id'],request['request_id']
        context_started=monotonic()
        try:
            history,notice=self.store.conversation_context(cid)
            self.store.set_context_notice(rid,notice)
            return history,notice
        finally:
            if metrics: metrics.context_finished(context_started)

    def _queue_summary(self,cid,metrics):
        if self.summary_factory is None or self.runtime.stopping or cid in self._deletions:
            metrics.summary_not_started('unavailable')
            return False
        existing=self._summary_tasks.get(cid)
        if existing is not None and not existing.done():
            metrics.summary_not_started('deduplicated')
            return False
        try:
            state=self.store.summary_state(cid)
            if not state['enabled']:
                metrics.summary_not_started('disabled')
                return False
            plan=self.store.summary_plan(cid)
        except Exception:
            # Planning is background-only; storage/planning failures cannot change the durable answer.
            metrics.summary_not_started('unavailable')
            return False
        if plan is None:
            metrics.summary_not_started('not_needed')
            return False
        metrics.queue_summary()
        task=asyncio.create_task(self._run_summary(cid,plan,metrics))
        self._summary_tasks[cid]=task
        return True

    async def _run_summary(self,cid,plan,metrics):
        service=None; summary_started=None; result='cancelled'
        try:
            # Yield to follow-up requests and wait for all foreground model work to become idle.
            await asyncio.sleep(SUMMARY_IDLE_SECONDS)
            async with self._summary_slot:
                while (self._answers or self.tasks.has_active_tasks()) and not self.runtime.stopping and cid not in self._deletions:
                    await asyncio.sleep(SUMMARY_IDLE_SECONDS)
                if self.runtime.stopping or cid in self._deletions or asyncio.current_task().cancelling():
                    raise asyncio.CancelledError()
                metrics.start_summary(); summary_started=monotonic()
                service=self.summary_factory()
                content=await service.summarize(plan)
                if self.runtime.stopping or cid in self._deletions or asyncio.current_task().cancelling():
                    raise asyncio.CancelledError()
                result='committed' if self.store.commit_summary(plan,content) else 'stale'
        except asyncio.CancelledError:
            result='cancelled'
        except Exception:
            # The foreground answer is already durable; summary failures are retryable background work.
            result='failed'
        finally:
            if service is not None:
                cleanup=asyncio.create_task(service.close())
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    await asyncio.shield(cleanup)
                except Exception:
                    if result not in {'committed','stale'}: result='failed'
            if summary_started is None: metrics.summary_not_started(result)
            else: metrics.summary_finished(summary_started,result)
            metrics.emit()
            if self._summary_tasks.get(cid) is asyncio.current_task():
                self._summary_tasks.pop(cid,None)

    async def _cancel_summary(self,cid):
        task=self._summary_tasks.get(cid)
        if task is None or task is asyncio.current_task(): return
        if not task.done() and not task.cancelling(): task.cancel()
        await asyncio.gather(task,return_exceptions=True)

    async def _prepare_task_context(self,request):
        # Task planning also uses only the already committed summary and bounded recent turns.
        return self.store.conversation_context(request['conversation_id'])[0]

    async def save_memory(self,values,mid=None):
        async def save():
            source=self.store.memory_source(mid) if mid else values.get('source_conversation_id')
            if source in self._deletions: raise ForgePilotError('conversation_deleting','来源对话正在删除，不能保存记忆。')
            return self.store.save_memory(values,mid)
        return await self.runtime.submit(save())

    async def edit_summary(self,cid,revision,**changes):
        async def edit():
            if cid in self._deletions: raise ForgePilotError('conversation_deleting','对话正在删除，不能修改摘要。')
            await self._cancel_summary(cid)
            return self.store.edit_summary(cid,revision,**changes)
        return await self.runtime.submit(edit())

    async def stop(self,cid,rid):
        return await self.runtime.submit(self._stop(cid,rid))

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
            self.runtime.stop_accepting()
            await asyncio.gather(*list(self._deletions.values()),return_exceptions=True)
            summaries=list(self._summary_tasks.values())
            for task in summaries:
                if not task.done() and not task.cancelling(): task.cancel()
            await asyncio.gather(*summaries,return_exceptions=True)
            self._summary_tasks.clear()
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
            await self.repository_cache.aclose()
        await self.runtime.drain(stop_all())

    async def delete(self,cid,stop_active=False):
        return await self.runtime.submit(self._delete(cid,stop_active))

    async def _delete(self,cid,stop_active):
        existing=self._deletions.get(cid)
        if existing is not None:
            return await asyncio.shield(existing)
        exists,requests=self.store.conversation_deletion_info(cid)
        if not exists:
            return {'conversation_id':cid,'deleted':True}
        if any(r['status']=='running' or r['request_id'] in self._answers or self.tasks.has_active_task(r['task_id']) for r in requests) and not stop_active:
            raise ForgePilotError('conversation_busy','本对话仍在运行，请确认“停止任务并删除”。')
        for r in requests:
            self.tasks.mark_cancel_for_deletion(r['task_id'])
        operation=asyncio.create_task(self._delete_owned(cid,requests))
        self._deletions[cid]=operation
        try:
            return await asyncio.shield(operation)
        finally:
            self._deletions.pop(cid,None)

    async def _delete_owned(self,cid,requests):
        await self._cancel_summary(cid)
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
