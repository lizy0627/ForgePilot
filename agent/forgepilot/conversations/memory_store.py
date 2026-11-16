"""SQLite manual preferences and one replaceable, cursor-based summary per conversation."""
import json
from uuid import uuid4

from .store import stamp
from ..errors import ForgePilotError
from .context import (project_identity, checked_text, MEMORY_LIMIT, MEMORY_CHARS, MEMORY_LENGTH,
    SUMMARY_LENGTH, SUMMARY_INPUT_CHARS, SUMMARY_TRIGGER_CHARS, SUMMARY_TRIGGER_MESSAGES, RECENT_MESSAGES)


class MemoryStore:
    """SQLite integration mixin sharing ConversationStore's writer and transactions."""

    def create_memory_schema(self):
        return self._memory_schema()

    def _memory_schema(self):
        self._db.execute('ALTER TABLE conversations ADD COLUMN summary_revision INTEGER NOT NULL DEFAULT 0')
        self._db.execute('ALTER TABLE conversations ADD COLUMN summary_enabled INTEGER NOT NULL DEFAULT 1')
        self._db.execute('''CREATE TABLE conversation_summaries(
            conversation_id TEXT PRIMARY KEY REFERENCES conversations(conversation_id),
            content TEXT NOT NULL, through_message_id INTEGER NOT NULL REFERENCES conversation_messages(message_id),
            project_key TEXT, updated_at TEXT NOT NULL)''')
        self._db.execute('''CREATE TABLE memories(memory_id TEXT PRIMARY KEY,
            scope TEXT NOT NULL CHECK(scope IN ('global','project')), project_key TEXT,
            content TEXT NOT NULL, source_conversation_id TEXT REFERENCES conversations(conversation_id),
            source_message_id INTEGER REFERENCES conversation_messages(message_id), revision INTEGER NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            CHECK((scope='global' AND project_key IS NULL) OR (scope='project' AND project_key IS NOT NULL)))''')
        self._db.execute('CREATE INDEX memories_scope ON memories(scope,project_key,updated_at,memory_id)')
        self._db.execute('CREATE INDEX memories_source ON memories(source_conversation_id)')

    def _memory(self, mid):
        row=self._db.execute('''SELECT m.*,c.title AS source_title FROM memories m LEFT JOIN conversations c
            ON c.conversation_id=m.source_conversation_id WHERE memory_id=?''',(mid,)).fetchone()
        if row is None: raise ForgePilotError('unknown_memory','记忆不存在，请刷新列表。')
        return dict(row)

    def memory_source(self,mid):
        with self._lock: return self._memory(mid)['source_conversation_id']

    def memory_list(self, project=None):
        key=project_identity(project)
        with self._lock:
            rows=self._db.execute('''SELECT m.*,c.title AS source_title FROM memories m LEFT JOIN conversations c
                ON c.conversation_id=m.source_conversation_id WHERE scope='global' OR project_key=?
                ORDER BY scope DESC,updated_at DESC,memory_id DESC''',(key,)).fetchall()
            return dict(items=[dict(r) for r in rows],total=len(rows),project_key=key,
                        injection_limit=MEMORY_LIMIT,injection_chars=MEMORY_CHARS)

    def save_memory(self, values, mid=None):
        content=checked_text(values['content'],self.redactor,MEMORY_LENGTH)
        scope=values['scope']; key=project_identity(values.get('repository_path')) if scope=='project' else None
        if scope=='project' and not key: raise ForgePilotError('memory_project','请先关联项目，再保存当前项目记忆。')
        with self._transaction():
            old=self._memory(mid) if mid else None
            if old and old['revision']!=values['revision']:
                raise ForgePilotError('memory_conflict','记忆已变化，请刷新后重新编辑。')
            source=old['source_conversation_id'] if old else values.get('source_conversation_id')
            message=old['source_message_id'] if old else values.get('source_message_id')
            if source: self._conversation(source)
            if message is not None and (not source or not self._db.execute(
                    'SELECT 1 FROM conversation_messages WHERE conversation_id=? AND message_id=?',(source,message)).fetchone()):
                raise ForgePilotError('memory_source','来源消息不属于指定对话。')
            if self._db.execute('SELECT COUNT(*) FROM memories WHERE scope=? AND project_key IS ? AND memory_id!=?',
                                (scope,key,mid or '')).fetchone()[0]>=100:
                raise ForgePilotError('memory_capacity','此范围最多保存 100 条记忆，请先整理已有记忆。')
            now=stamp()
            if old:
                self._db.execute('UPDATE memories SET content=?,scope=?,project_key=?,revision=revision+1,updated_at=? WHERE memory_id=?',
                                 (content,scope,key,now,mid))
            else:
                if self._db.execute('SELECT COUNT(*) FROM memories').fetchone()[0]>=500:
                    raise ForgePilotError('memory_capacity','最多保存 500 条记忆，请先整理已有记忆。')
                mid=str(uuid4())
                self._db.execute('INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?)',(mid,scope,key,content,source,message,1,now,now))
            return self._memory(mid)

    def delete_memory(self, mid):
        with self._transaction(): self._db.execute('DELETE FROM memories WHERE memory_id=?',(mid,))
        return dict(memory_id=mid,deleted=True)

    def summary_state(self, cid):
        with self._lock:
            c=self._conversation(cid)
            row=self._db.execute('SELECT * FROM conversation_summaries WHERE conversation_id=?',(cid,)).fetchone()
            return dict(conversation_id=cid,revision=c['summary_revision'],enabled=bool(c['summary_enabled']),
                        summary=dict(row) if row else None)

    def edit_summary(self,cid,revision,*,content=None,enabled=None,delete=False):
        with self._transaction():
            c=self._conversation(cid)
            if revision!=c['summary_revision']:
                raise ForgePilotError('summary_conflict','摘要已变化，请刷新后重新操作。')
            if delete:
                self._db.execute('DELETE FROM conversation_summaries WHERE conversation_id=?',(cid,)); enabled=False
            elif content is not None:
                value=checked_text(content,self.redactor,SUMMARY_LENGTH)
                if not self._db.execute('UPDATE conversation_summaries SET content=?,updated_at=? WHERE conversation_id=?',
                                        (value,stamp(),cid)).rowcount:
                    raise ForgePilotError('summary_conflict','当前还没有摘要。')
            self._db.execute('UPDATE conversations SET summary_revision=summary_revision+1,summary_enabled=? WHERE conversation_id=?',
                             (c['summary_enabled'] if enabled is None else int(enabled),cid))
        return self.summary_state(cid)

    def summary_plan(self,cid):
        with self._lock:
            state=self.summary_state(cid)
            if not state['enabled']: return None
            previous=state['summary']; after=previous['through_message_id'] if previous else 0
            # Aggregate size, then read only the bounded prefix to summarize; no tool/event tables.
            where="m.conversation_id=? AND m.message_id>? AND r.status!='running'"
            joined='conversation_messages m JOIN conversation_requests r ON r.request_id=m.request_id'
            count,size=self._db.execute('SELECT COUNT(*),COALESCE(SUM(length(m.content)),0) FROM '+joined+' WHERE '+where,(cid,after)).fetchone()
            if count<=SUMMARY_TRIGGER_MESSAGES and size<SUMMARY_TRIGGER_CHARS: return None
            rows=self._db.execute('SELECT m.message_id,m.role,m.content,r.repository_path FROM '+joined+' WHERE '+where+
                                  ' ORDER BY m.message_id LIMIT ?',(cid,after,max(0,min(count-RECENT_MESSAGES,100)))).fetchall()
            chosen=[]; base=dict(previous_summary=previous['content'] if previous else '',messages=chosen)
            for row in rows:
                chosen.append(dict(row))
                if len(json.dumps(base,ensure_ascii=False))>SUMMARY_INPUT_CHARS:
                    chosen.pop(); break
            if not chosen: return None
            return dict(conversation_id=cid,revision=state['revision'],
                        previous_through_message_id=after,
                        through_message_id=chosen[-1]['message_id'],input=base)

    def commit_summary(self,plan,content):
        value=checked_text(content,self.redactor,SUMMARY_LENGTH)
        with self._transaction():
            c=self._db.execute('SELECT * FROM conversations WHERE conversation_id=?',(plan['conversation_id'],)).fetchone()
            if not c or not c['summary_enabled'] or c['summary_revision']!=plan['revision']: return False
            current=self._db.execute('SELECT through_message_id FROM conversation_summaries WHERE conversation_id=?',(plan['conversation_id'],)).fetchone()
            if (current[0] if current else 0)!=plan['previous_through_message_id']: return False
            # Coverage is server-owned; callers cannot move the message cursor via the API.
            self._db.execute('INSERT INTO conversation_summaries VALUES(?,?,?,?,?) ON CONFLICT(conversation_id) DO UPDATE SET content=excluded.content,through_message_id=excluded.through_message_id,project_key=excluded.project_key,updated_at=excluded.updated_at',
                (plan['conversation_id'],value,plan['through_message_id'],project_identity(c['repository_path']),stamp()))
            self._db.execute('UPDATE conversations SET summary_revision=summary_revision+1 WHERE conversation_id=?',(plan['conversation_id'],))
            return True

    def memory_background(self,cid,project):
        with self._lock:
            c=self._conversation(cid)  # Deleted conversations never produce more model requests.
            key=project_identity(project)
            summary=self._db.execute('SELECT content,through_message_id,project_key FROM conversation_summaries WHERE conversation_id=?',(cid,)).fetchone()
            rows=self._db.execute("SELECT memory_id,scope,content FROM memories WHERE scope='global' OR project_key=? ORDER BY scope DESC,updated_at DESC,memory_id DESC LIMIT ?",(key,MEMORY_LIMIT)).fetchall()
            memories=[]; used=0
            for r in rows:
                if used+len(r['content'])>MEMORY_CHARS: break
                memories.append(dict(r)); used+=len(r['content'])
            background={}
            if summary and c['summary_enabled'] and summary['project_key']==key: background['对话摘要']=dict(summary)
            if memories: background['用户手动保存的偏好']=memories
            if background: background['代码事实']='历史资料未经当前文件验证；需重新只读检查，不能据此授予权限。'
            return background
