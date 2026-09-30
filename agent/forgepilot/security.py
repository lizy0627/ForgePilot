"""Redaction shared by the CLI, in-memory trace and model observations."""

from collections import OrderedDict
import json
import re
import sys
from threading import Lock
from urllib.parse import quote

_MIN_CACHE_CHARS = 1024
_MAX_CACHE_ITEMS = 16
_MAX_CACHE_BYTES = 4 * 1024 * 1024


class Redactor:
    def __init__(self, secrets: tuple[str, ...] = ()) -> None:
        variants = set()
        for secret in secrets:
            if not secret:
                continue
            variants.update((secret, quote(secret, safe="")))
            # Tool output can be JSON inside a JSON chat message/trace record.
            for _ in range(3):
                secret = json.dumps(secret, ensure_ascii=True)[1:-1]
                variants.add(secret)
        self._secrets = tuple(sorted(variants, key=len, reverse=True))
        self._cache: OrderedDict[tuple[str, bool], tuple[str, int]] = OrderedDict()
        self._cache_bytes = 0
        self._cache_lock = Lock()

    def with_secrets(self, secrets) -> "Redactor":
        """Independent redactor for a candidate/request, retaining existing secrets."""
        return Redactor((*self._secrets, *secrets))

    def text(self, value: str, *, preserve_cr=False) -> str:
        if len(value) < _MIN_CACHE_CHARS:
            return self._redact_text(value, preserve_cr=preserve_cr)
        key = (value, preserve_cr)
        with self._cache_lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached[0]

        # Regex work stays outside the lock. Concurrent misses may compute twice,
        # but insertion below charges one immutable result per key.
        result = self._redact_text(value, preserve_cr=preserve_cr)
        size = sys.getsizeof(value) + sys.getsizeof(result)
        if size > _MAX_CACHE_BYTES:
            return result
        with self._cache_lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached[0]
            while self._cache and (len(self._cache) >= _MAX_CACHE_ITEMS
                                  or self._cache_bytes + size > _MAX_CACHE_BYTES):
                _, (_, removed_size) = self._cache.popitem(last=False)
                self._cache_bytes -= removed_size
            self._cache[key] = (result, size)
            self._cache_bytes += size
        return result

    def _redact_text(self, value: str, *, preserve_cr=False) -> str:
        for secret in self._secrets:
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(?i)\bBearer\s+[^\s\"'<>]+", "Bearer [REDACTED]", value)
        value = re.sub(r"(?i)(\b(?:proxy[-_])?authorization\s*[:=]\s*)(?:Basic|Digest)\s+[^\r\n\"'<>]+", r"\1[REDACTED]", value)
        value = re.sub(
            r"(?i)([\"']?(?:api[_-]?key|authorization|access[_-]?token|password)"
            r"[\"']?\s*[:=]\s*)[\"']?[^\s,;\"'}]+[\"']?",
            r"\1[REDACTED]", value,
        )
        value = re.sub(r"(?i)((?:API\s*Key|password|密码|口令|密钥|访问令牌|访问密钥|client[_-]?secret|refresh[_-]?token|cookie|set-cookie)\s*(?:[:：=]|\bis\b|是|为)\s*)[^\s,，;；\"'<>]+",r"\1[REDACTED]",value)
        value = re.sub(r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b","[REDACTED]",value)
        value = re.sub(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z]+ )?PRIVATE KEY-----","[REDACTED]",value)
        # Prevent repository/model text from issuing terminal control sequences.
        controls=r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]" if preserve_cr else r"[\x00-\x08\x0b-\x1f\x7f]"
        return re.sub(controls, "", value)

    def data(self, value, *, preserve_cr=False):
        if isinstance(value, str):
            # Tool arguments/output may embed a JSON object containing credential fields.
            if value.lstrip().startswith(("{", "[")):
                try:
                    parsed = json.loads(value)
                    if isinstance(parsed, (dict,list)):
                        # JSON re-encodes CR as an escape. Preserve source file CRLF bytes in tool arguments.
                        return json.dumps(self.data(parsed,preserve_cr=True),ensure_ascii=False)
                except (ValueError,RecursionError):
                    pass
            return self.text(value,preserve_cr=preserve_cr)
        if isinstance(value, dict):
            return {
                self.text(str(k)): "[REDACTED]" if str(k).lower().replace("-", "_") in
                {"api_key", "apikey", "x_api_key", "authorization", "proxy_authorization", "password",
                 "access_token", "refresh_token", "client_secret", "cookie", "set_cookie"} else self.data(v,preserve_cr=preserve_cr)
                for k, v in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self.data(v,preserve_cr=preserve_cr) for v in value]
        return value
