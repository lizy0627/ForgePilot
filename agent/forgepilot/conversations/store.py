"""Conversation persistence. Chat text is context, never serialized execution authority."""
from datetime import datetime, timezone
import json
from uuid import uuid4

from ..errors import ForgePilotError
from ..history_cursor import encode_cursor, decode_cursor
from ..mcp_records import bounded_records

HISTORY_CHARS = 32_000
HISTORY_MESSAGES = 40
CONTEXT_NOTICE = "对话较长，本次只使用最近完整轮次（最多 40 条消息、32000 字符）；更早消息仍保存在历史中。"


def stamp():
    return datetime.now(timezone.utc).isoformat()


class ConversationStore:
    """SQLite integration mixin; composed into the single writer, never a new connection.

    Public hooks keep migrations, task publication and recovery in the owner's
    existing transaction/lock. Private hooks remain for legacy fault injection.
    """

    def create_conversation_schema(self):
        return self._conversation_schema()

    def recover_conversations(self):
        return self._recover_conversations()

    def sync_conversation_task(self, snapshot):
        """Called inside the task publication transaction; does not commit or notify."""
        return self._sync_conversation_task(snapshot)

    def require_conversation(self, cid):
        """Validate existence and durable availability under the caller's read lock."""
        return self._conversation(cid)

    def _conversation_schema(self):
        for sql in (
            """CREATE TABLE conversations(conversation_id TEXT PRIMARY KEY, title TEXT NOT NULL,
                repository_path TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
            "CREATE INDEX conversations_history ON conversations(created_at DESC,conversation_id DESC)",
            """CREATE TABLE conversation_requests(request_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
                mode TEXT NOT NULL CHECK(mode IN ('chat','task')), prompt TEXT NOT NULL,
                repository_path TEXT, status TEXT NOT NULL, task_id TEXT UNIQUE REFERENCES tasks(task_id),
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, error TEXT, context_notice TEXT NOT NULL DEFAULT '')""",
            "CREATE INDEX requests_conversation ON conversation_requests(conversation_id,created_at,request_id)",
            "CREATE UNIQUE INDEX one_active_request ON conversation_requests(conversation_id) WHERE status='running'",
            """CREATE TABLE conversation_messages(message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
                request_id TEXT NOT NULL REFERENCES conversation_requests(request_id),
                role TEXT NOT NULL CHECK(role IN ('user','assistant')), content TEXT NOT NULL,
                created_at TEXT NOT NULL, UNIQUE(request_id,role))""",
            "CREATE INDEX messages_conversation ON conversation_messages(conversation_id,message_id)",
        ):
            self._db.execute(sql)
        # Import only conversational text. Plans, grants, counters and tool messages stay in task tables.
        for row in self._db.execute("SELECT snapshot_json FROM tasks ORDER BY created_at,task_id").fetchall():
            self._import_task(json.loads(row[0]))

    def _import_task(self, s):
        cid, rid = str(uuid4()), str(uuid4())
        self._db.execute("INSERT INTO conversations(conversation_id,title,repository_path,created_at,updated_at) VALUES(?,?,?,?,?)", (cid,
            (s.get('prompt') or '历史任务')[:80], s.get('repository_path'), s['created_at'], s['updated_at']))
        self._db.execute("INSERT INTO conversation_requests(request_id,conversation_id,mode,prompt,repository_path,status,task_id,created_at,updated_at,error,context_notice) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (rid,cid,'task',
            s.get('prompt',''),s.get('repository_path'),'running',s['task_id'],s['created_at'],s['updated_at'],None,''))
        self._insert_message(cid,rid,'user',s.get('prompt',''),s['created_at'])
        if s['state'] in {'completed','failed','cancelled'}:
            self._sync_conversation_task(s)

    def _conversation(self, cid):
        row = self._db.execute("SELECT * FROM conversations WHERE conversation_id=?", (cid,)).fetchone()
        if row is None:
            raise ForgePilotError('unknown_conversation', '对话不存在，请刷新历史记录。')
        self._ensure_conversation_available(cid)
        return dict(row)

    def _ensure_request_available(self, row):
        if row['request_id'] in self._unavailable_requests:
            raise ForgePilotError('storage_unavailable', 'Request persistence failed; durable status is unavailable')
        if row['task_id']:
            self._ensure_task_available(row['task_id'])

    def _ensure_conversation_available(self, cid):
        # Called under the existing store lock, including from write transactions.
        if self._unavailable_requests or self._unavailable_tasks:
            for row in self._db.execute('SELECT request_id,task_id FROM conversation_requests WHERE conversation_id=?', (cid,)):
                self._ensure_request_available(row)

    def create_conversation(self, title='新对话', repository_path=None):
        cid, now = str(uuid4()), stamp()
        with self._transaction():
            self._db.execute("INSERT INTO conversations(conversation_id,title,repository_path,created_at,updated_at) VALUES(?,?,?,?,?)", (cid,self.redactor.text(title),repository_path,now,now))
        return self.conversation_detail(cid)

    def update_conversation(self, cid, changes):
        with self._transaction():
            current=self._conversation(cid)
            if 'repository_path' in changes and self._db.execute(
                "SELECT 1 FROM conversation_requests WHERE conversation_id=? AND status='running'", (cid,)).fetchone():
                raise ForgePilotError('conversation_busy', '当前请求尚未结束，暂时不能更换项目。')
            if 'repository_path' in changes and changes['repository_path']!=current['repository_path']:
                self._db.execute('DELETE FROM conversation_summaries WHERE conversation_id=?',(cid,))
                self._db.execute('UPDATE conversations SET summary_revision=summary_revision+1 WHERE conversation_id=?',(cid,))
            for key in ('title','repository_path'):
                if key in changes:
                    value = self.redactor.text(changes[key]) if isinstance(changes[key],str) else changes[key]
                    self._db.execute(f"UPDATE conversations SET {key}=?,updated_at=? WHERE conversation_id=?", (value,stamp(),cid))
        return self.conversation_detail(cid)

    def conversation_page(self, limit=20, before=None):
        with self._lock:
            params, where = [], ''
            if before:
                boundary=decode_cursor(before)
                if boundary is None:
                    row = self._conversation(before)
                    boundary=[row['created_at'],before]
                where = 'WHERE (created_at,conversation_id)<(?,?)'
                params = boundary
            rows = self._db.execute('SELECT * FROM conversations '+where+' ORDER BY created_at DESC,conversation_id DESC LIMIT ?', (*params,limit+1)).fetchall()
            items = [dict(r) for r in rows[:limit]]
            for item in items:
                self._ensure_conversation_available(item['conversation_id'])
            return dict(items=items,total=self._db.execute('SELECT COUNT(*) FROM conversations').fetchone()[0],
                        next_cursor=encode_cursor(items[-1]['created_at'],items[-1]['conversation_id']) if len(rows)>limit else None)

    def conversation_deletion_info(self, cid):
        with self._lock:
            exists=self._db.execute('SELECT 1 FROM conversations WHERE conversation_id=?',(cid,)).fetchone()
            requests=[dict(row) for row in self._db.execute(
                'SELECT request_id,mode,status,task_id FROM conversation_requests WHERE conversation_id=?',(cid,))]
            return bool(exists),requests

    def delete_conversation(self, cid):
        """Only called after host cleanup. Delete children before parents in one transaction."""
        task_ids=[]
        with self._transaction():
            _,requests=self.conversation_deletion_info(cid)
            for request in requests:
                self._ensure_request_available(request)
            if any(r['status']=='running' for r in requests):
                raise ForgePilotError('conversation_busy','请先停止任务并等待资源清理后再删除。')
            task_ids=[r['task_id'] for r in requests if r['task_id']]
            linked='SELECT task_id FROM conversation_requests WHERE conversation_id=? AND task_id IS NOT NULL'
            if self._db.execute('SELECT 1 FROM tasks WHERE closed=0 AND task_id IN ('+linked+')',(cid,)).fetchone():
                raise ForgePilotError('conversation_busy','执行资源尚未清理完成，请稍后重试。')
            self._db.execute('DELETE FROM memories WHERE source_conversation_id=?',(cid,))
            self._db.execute('DELETE FROM conversation_summaries WHERE conversation_id=?',(cid,))
            self._db.execute('DELETE FROM conversation_messages WHERE conversation_id=?',(cid,))
            self._db.execute('DELETE FROM tool_calls WHERE task_id IN ('+linked+')',(cid,))
            self._db.execute('DELETE FROM task_events WHERE task_id IN ('+linked+')',(cid,))
            self._db.execute('DELETE FROM conversation_requests WHERE conversation_id=?',(cid,))
            self._db.executemany('DELETE FROM tasks WHERE task_id=?',[(tid,) for tid in task_ids])
            self._db.execute('DELETE FROM conversations WHERE conversation_id=?',(cid,))
        # In-memory changes happen only after commit; rollback leaves all records intact.
        with self._lock:
            self._live.difference_update(task_ids)
            for tid in task_ids:
                for loop,event in self._waiters.pop(tid,set()):
                    if not loop.is_closed(): loop.call_soon_threadsafe(event.set)
        return {'conversation_id':cid,'deleted':True}

    def conversation_detail(self, cid, before=None, limit=100):
        with self._lock:
            result = self._conversation(cid)
            params = [cid]
            where = ''
            if before:
                where = ' AND message_id<?'; params.append(before)
            rows = self._db.execute('SELECT * FROM conversation_messages WHERE conversation_id=?'+where+
                ' ORDER BY message_id DESC LIMIT ?', (*params,limit+1)).fetchall()
            result['messages'] = [dict(r) for r in reversed(rows[:limit])]
            result['next_before'] = rows[limit-1]['message_id'] if len(rows)>limit else None
            active = self._db.execute("SELECT * FROM conversation_requests WHERE conversation_id=? AND status='running'", (cid,)).fetchone()
            last = self._db.execute('SELECT * FROM conversation_requests WHERE conversation_id=? ORDER BY created_at DESC,request_id DESC LIMIT 1', (cid,)).fetchone()
            result['active_request'] = self._request_state(active) if active else None
            result['last_request'] = self._request_state(last) if last else None
            result['context_notice'] = last['context_notice'] if last else ''
            return result

    def conversation_request(self, cid, rid):
        with self._lock:
            row = self._db.execute('SELECT * FROM conversation_requests WHERE request_id=? AND conversation_id=?', (rid,cid)).fetchone()
            if row is None:
                raise ForgePilotError('unknown_request', '请求不存在。')
            return self._request_state(row)

    def conversation_stream_state(self, cid, rid):
        """Return durable request and assistant state under one read lock."""
        with self._lock:
            row = self._db.execute('SELECT * FROM conversation_requests WHERE request_id=? AND conversation_id=?',
                                   (rid,cid)).fetchone()
            if row is None:
                raise ForgePilotError('unknown_request', '请求不存在。')
            request = self._request_state(row)
            message = self._db.execute("SELECT message_id,content FROM conversation_messages WHERE request_id=? AND role='assistant'",
                                       (rid,)).fetchone()
            return request,dict(message) if message else None

    def request_retry(self, cid, rid, mode, prompt):
        with self._lock:
            row = self._db.execute('SELECT * FROM conversation_requests WHERE request_id=?', (rid,)).fetchone()
            if row is None:
                return None
            if row['conversation_id']!=cid or row['mode']!=mode or row['prompt']!=self.redactor.text(prompt):
                raise ForgePilotError('request_conflict', '请求标识已被不同内容使用，请刷新对话。')
            return self._request_state(row)

    def _mcp_records(self, row):
        self._ensure_request_available(row)
        if row['mode']=='task' and row['task_id']:
            records={}
            # At most start+finish for each of eight calls. Work uses its existing Trace.
            for event in self._db.execute("SELECT data_json FROM task_events WHERE task_id=? AND kind='mcp_tool_activity' ORDER BY sequence LIMIT 16",(row['task_id'],)):
                record=json.loads(event[0]); records[record['sequence']]=record
            values=list(records.values())
        else:
            # v2 task import occurs before the v5 column is added.
            values=json.loads(row['mcp_calls_json']) if 'mcp_calls_json' in row.keys() else []
        for record in values:
            if record['status']=='running' and row['status']!='running':
                record.update(status='interrupted',error_code='service_interrupted')
        return json.loads(bounded_records(values))

    def _request_state(self, row):
        result=dict(row); result.pop('mcp_calls_json',None)
        records=self._mcp_records(row)
        result['tool_activity'] = ({key:records[-1][key] for key in
            ('sequence','server_name','tool_name','status','duration_ms','error_code')} if records else None)
        return result

    def conversation_mcp_tools(self, cid, rid):
        with self._lock:
            row=self._db.execute('SELECT * FROM conversation_requests WHERE conversation_id=? AND request_id=?',(cid,rid)).fetchone()
            if row is None:
                raise ForgePilotError('unknown_request','请求不存在。')
            return {'conversation_id':cid,'request_id':rid,'items':self._mcp_records(row)}

    def record_mcp_call(self, cid, rid, record):
        # This callback accepts only program-produced records, never model text or an API write.
        with self._transaction():
            row=self._db.execute("SELECT * FROM conversation_requests WHERE conversation_id=? AND request_id=? AND mode='chat' AND status='running'",(cid,rid)).fetchone()
            if row is None: return
            records=json.loads(row['mcp_calls_json'])
            sequence=record['sequence']
            if not 1<=sequence<=8 or sequence>len(records)+1: return
            if sequence<=len(records): records[sequence-1]=self.redactor.data(record)
            else: records.append(self.redactor.data(record))
            self._db.execute('UPDATE conversation_requests SET mcp_calls_json=?,updated_at=? WHERE request_id=?',
                             (bounded_records(records),stamp(),rid))

    def conversation_context(self, cid):
        with self._lock:
            state=self.summary_state(cid); summary=state['summary'] if state['enabled'] else None
            after=summary['through_message_id'] if summary else 0
            # Read a bounded number of complete turns. Never load tool logs into model context.
            turns = self._db.execute("""SELECT r.request_id FROM conversation_requests r JOIN conversation_messages m ON m.request_id=r.request_id
                WHERE r.conversation_id=? AND r.status IN ('completed','failed','cancelled','interrupted') AND m.message_id>? GROUP BY r.request_id
                ORDER BY MAX(m.message_id) DESC LIMIT ?""", (cid,after,HISTORY_MESSAGES//2+1)).fetchall()
            pairs, used = [], 0
            for turn in turns:
                pair = self._db.execute('SELECT role,content FROM conversation_messages WHERE request_id=? AND message_id>? ORDER BY message_id', (turn[0],after)).fetchall()
                size = sum(len(r['content']) for r in pair)
                if len(pairs)>=HISTORY_MESSAGES//2 or used+size>HISTORY_CHARS:
                    break
                pairs.append([dict(r) for r in pair]); used+=size
            count = self._db.execute("SELECT COUNT(DISTINCT r.request_id) FROM conversation_requests r JOIN conversation_messages m ON m.request_id=r.request_id WHERE r.conversation_id=? AND r.status IN ('completed','failed','cancelled','interrupted') AND m.message_id>?", (cid,after)).fetchone()[0]
            notice=('较早内容使用摘要（覆盖至消息 #'+str(after)+'），保留最近轮次原文；原始消息仍可查看。') if summary else ''
            if len(pairs)<count: notice+=' '+CONTEXT_NOTICE
            return [m for pair in reversed(pairs) for m in pair],notice.strip()

    def set_context_notice(self,rid,notice):
        with self._transaction():
            self._db.execute('UPDATE conversation_requests SET context_notice=? WHERE request_id=? AND status=\'running\'',(notice,rid))

    def begin_conversation_request(self, cid, rid, mode, prompt, notice):
        with self._transaction():
            c = self._conversation(cid)
            if self._db.execute("SELECT 1 FROM conversation_requests WHERE conversation_id=? AND status='running'", (cid,)).fetchone():
                raise ForgePilotError('conversation_busy', '本对话已有请求正在处理，请等待完成或停止当前请求。')
            if mode=='task' and not c['repository_path']:
                raise ForgePilotError('project_required', '执行任务前必须关联项目。')
            now = stamp(); prompt=self.redactor.text(prompt)
            self._db.execute('INSERT INTO conversation_requests(request_id,conversation_id,mode,prompt,repository_path,status,task_id,created_at,updated_at,error,context_notice) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (rid,cid,mode,prompt,c['repository_path'],'running',None,now,now,None,notice))
            self._insert_message(cid,rid,'user',prompt,now)
            self._db.execute("UPDATE conversations SET updated_at=?,title=CASE WHEN title='新对话' THEN ? ELSE title END WHERE conversation_id=?", (now,prompt[:80],cid))
        return self.conversation_request(cid,rid)

    def _insert_message(self, cid, rid, role, content, now):
        self._db.execute('INSERT OR IGNORE INTO conversation_messages(conversation_id,request_id,role,content,created_at) VALUES(?,?,?,?,?)',
                         (cid,rid,role,self.redactor.text(content),now))

    def _finish_conversation_request(self, rid, status, content, error=None):
        row = self._db.execute('SELECT * FROM conversation_requests WHERE request_id=?', (rid,)).fetchone()
        if not row:
            return None
        if row['status']!='running':
            message=self._db.execute("SELECT message_id FROM conversation_messages WHERE request_id=? AND role='assistant'",(rid,)).fetchone()
            return {'request':self._request_state(row),'message_id':message['message_id'] if message else None,
                    'changed':False}
        now=stamp()
        changed=self._db.execute("UPDATE conversation_requests SET status=?,error=?,updated_at=? WHERE request_id=? AND status='running'",
                                 (status,error,now,rid)).rowcount
        if changed!=1:
            raise ForgePilotError('request_conflict','回答终态提交发生冲突。')
        inserted=self._db.execute('INSERT INTO conversation_messages(conversation_id,request_id,role,content,created_at) VALUES(?,?,?,?,?)',
                                  (row['conversation_id'],rid,'assistant',self.redactor.text(content),now))
        self._db.execute('UPDATE conversations SET updated_at=? WHERE conversation_id=?', (now,row['conversation_id']))
        updated=self._db.execute('SELECT * FROM conversation_requests WHERE request_id=?',(rid,)).fetchone()
        return {'request':self._request_state(updated),'message_id':inserted.lastrowid,'changed':True}

    def finish_conversation_request(self, rid, status, content, error=None):
        with self._transaction():
            return self._finish_conversation_request(rid,status,self.redactor.text(content),
                                                     self.redactor.text(error) if error else None)

    def _sync_conversation_task(self, snapshot):
        row = self._db.execute('SELECT request_id FROM conversation_requests WHERE task_id=?', (snapshot['task_id'],)).fetchone()
        if not row:
            return
        state = snapshot['state']
        label = {'completed':'任务已完成','failed':'任务失败','cancelled':'任务已停止'}[state]
        # A bounded result is sufficient for follow-ups. Full plans/logs remain in the task view.
        text = snapshot.get('final_result') or snapshot.get('analysis_result') or snapshot.get('error') or ''
        content = label+'（'+snapshot['task_id']+'）\n'+text[:8_000]
        if len(text)>8_000: content+='\n结果过长，完整内容请查看任务详情。'
        self._finish_conversation_request(row[0],state,content,snapshot.get('error'))

    def _recover_conversations(self):
        with self._transaction():
            for row in self._db.execute("SELECT request_id,mcp_calls_json FROM conversation_requests WHERE status='running'").fetchall():
                records=json.loads(row['mcp_calls_json'])
                for record in records:
                    if record['status']=='running':
                        record.update(status='interrupted',error_code='service_interrupted')
                self._db.execute('UPDATE conversation_requests SET mcp_calls_json=? WHERE request_id=?',
                                 (bounded_records(records),row['request_id']))
                self._finish_conversation_request(row[0],'interrupted','上次服务中断，回答未完成。可以重新发送问题；旧请求不会自动重跑。','service_interrupted')
