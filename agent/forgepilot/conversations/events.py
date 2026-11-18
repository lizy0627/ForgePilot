"""Internal, already-redacted answer events shared by Q&A and replay."""
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal


@dataclass(frozen=True, slots=True)
class ChatAnswerEvent:
    type: Literal['segment_started', 'content', 'segment_discarded', 'segment_committed',
                  'tool_started', 'tool_finished']
    segment_id: str
    text: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    success: bool | None = None


AnswerEventCallback = Callable[[ChatAnswerEvent], Awaitable[None] | None]
