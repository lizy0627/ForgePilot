"""Shared checks for private fragments in skill prose."""
import re
from unicodedata import normalize


# Summary fields can themselves contain pasted logs, code or a prompt. Keep
# short prose only. Drop the whole field when it contains a private fragment.
_PRIVATE = re.compile(
    r"[{}`\\$=]|@@|diff --git|(?<!\w)/\S|[A-Za-z]:/|(?:^|\s)(?:---|\+\+\+|#include)\s|"
    r"\b(?:raw[_ -]?prompt|system[_ -]?prompt|hidden[_ -]?tests?|protected[_ -]?tests?|"
    r"oracle|environment[_ -]?variables?|BEGIN .*PRIVATE KEY)\b|原始提示|系统提示|隐藏测试|环境变量|"
    r"(?:^|\s)(?:system|assistant|user)\s*[:：]|"
    r"(?:^|\s)(?:def |#include|import \w|from \w+ import |class \w+[:(]|function \w+\()|"
    r"^\s*(?:#|//|<\w+>|\w+(?:[.:]+\w+)*\([^)]*\)\s*;?\s*$)|"
    r"\b(?:AWS|FORGEPILOT|OPENAI)_[A-Z_]{2,}|\b(?:key|secret|token|credential)\s*[:：]",
    re.IGNORECASE,
)


def contains_private_fragment(value: str) -> bool:
    return _PRIVATE.search(normalize("NFKC", value)) is not None
