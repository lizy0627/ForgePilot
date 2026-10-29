"""Redaction shared by the CLI, in-memory trace and model observations."""

from collections import OrderedDict
import base64
import json
import re
import sys
from threading import Lock
from urllib.parse import quote

_MIN_CACHE_CHARS = 1024
_MAX_CACHE_ITEMS = 16
_MAX_CACHE_BYTES = 4 * 1024 * 1024
_MAX_STREAM_CHARS = 32_768
_REDACTED = "[REDACTED]"

# These fields must never cross a persistence, event, or API boundary. Keeping
# the rule here makes nested tool results and JSON-in-text share one policy.
_SENSITIVE_FIELDS = {
    "api_key", "apikey", "x_api_key", "authorization", "proxy_authorization",
    "password", "access_token", "refresh_token", "client_secret", "cookie",
    "set_cookie", "environment", "env", "env_vars", "environment_variables",
    "dpapi", "dpapi_blob", "dpapi_plaintext", "decrypted_secret",
    "reasoning", "reasoning_content", "hidden_reasoning", "chain_of_thought",
    "hidden_thought", "hidden_thoughts", "thoughts", "hidden_test_source",
    "hidden_test_sources", "oracle_source", "oracle_code", "full_repository",
    "repository_contents", "repository_snapshot", "repository_source",
}
_SENSITIVE_FIELD_KEYS = {re.sub(r"[^a-z0-9]", "", key) for key in _SENSITIVE_FIELDS}


def _sensitive_key(value: str) -> bool:
    key = re.sub(r"[^a-z0-9]", "", value.lower())
    return (key in _SENSITIVE_FIELD_KEYS or "dpapi" in key or key.endswith(("apikey", "secret", "password",
            "credential", "token", "privatekey")) or key.startswith(("hiddenthought",
            "hiddenreasoning", "chainofthought", "oraclesource", "repositorycontents")))


class Redactor:
    def __init__(self, secrets: tuple[str, ...] = ()) -> None:
        variants = set()
        for secret in secrets:
            if not secret:
                continue
            escaped_url = quote(secret, safe="")
            variants.update((secret, escaped_url, quote(escaped_url, safe="")))
            # A known credential can reappear in logs as transport encodings.
            # Avoid treating short ordinary words as encoded credentials.
            if len(secret) >= 6:
                raw = secret.encode("utf-8")
                variants.update((raw.hex(), raw.hex().upper(),
                                 base64.b64encode(raw).decode(),
                                 base64.b64encode(raw).decode().rstrip("="),
                                 base64.urlsafe_b64encode(raw).decode(),
                                 base64.urlsafe_b64encode(raw).decode().rstrip("="),
                                 base64.b64encode(secret.encode("utf-16le")).decode()))
                if secret.isascii():
                    variants.add("".join(f"\\u{ord(char):04x}" for char in secret))
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

    def streaming(self, *, max_chars: int = _MAX_STREAM_CHARS) -> "StreamingRedactor":
        return StreamingRedactor(self, max_chars=max_chars)

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
                self.text(str(k)): "[REDACTED]" if _sensitive_key(str(k))
                else self.data(v,preserve_cr=preserve_cr)
                for k, v in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self.data(v,preserve_cr=preserve_cr) for v in value]
        return value


def _word(character: str) -> bool:
    return bool(character and re.match(r"\w", character))


def _literal(suffix: str, position: int, choices: tuple[str, ...]):
    tail = suffix[position:].casefold()
    matches = [choice for choice in choices if choice.casefold().startswith(tail)]
    if matches:
        return None
    for choice in choices:
        folded = choice.casefold()
        if tail.startswith(folded):
            return position + len(choice)
    return -1


def _space(suffix: str, position: int) -> int:
    while position < len(suffix) and suffix[position].isspace():
        position += 1
    return position


def _token_tail(suffix: str, position: int, allowed) -> bool:
    if position == len(suffix):
        return True
    if not allowed(suffix[position]):
        return False
    while position < len(suffix) and allowed(suffix[position]):
        position += 1
    return position == len(suffix)


def _bearer_open(suffix: str, previous: str) -> bool:
    if _word(previous):
        return False
    position = _literal(suffix, 0, ("Bearer",))
    if position is None:
        return True
    if position < 0 or position == len(suffix) or not suffix[position].isspace():
        return position == len(suffix)
    position = _space(suffix, position)
    return _token_tail(suffix, position, lambda char: not char.isspace() and char not in "\"'<>" )


def _authorization_open(suffix: str, previous: str) -> bool:
    if _word(previous):
        return False
    position = _literal(suffix, 0, ("authorization", "proxy-authorization", "proxy_authorization"))
    if position is None:
        return True
    if position < 0:
        return False
    position = _space(suffix, position)
    if position == len(suffix):
        return True
    if suffix[position] not in ":=":
        return False
    position = _space(suffix, position + 1)
    scheme = _literal(suffix, position, ("Basic", "Digest"))
    if scheme is None:
        return True
    if scheme < 0 or scheme == len(suffix) or not suffix[scheme].isspace():
        return scheme == len(suffix)
    position = _space(suffix, scheme)
    return _token_tail(suffix, position, lambda char: char not in "\r\n\"'<>" )


def _assignment_open(suffix: str, previous: str) -> bool:
    position = 0
    if suffix and suffix[0] in "\"'":
        position = 1
    keyword = _literal(suffix, position, (
        "api_key", "api-key", "apikey", "authorization",
        "access_token", "access-token", "accesstoken", "password",
    ))
    if keyword is None:
        return True
    if keyword < 0:
        return False
    position = keyword
    if position < len(suffix) and suffix[position] in "\"'":
        position += 1
    position = _space(suffix, position)
    if position == len(suffix):
        return True
    if suffix[position] not in ":=":
        return False
    position = _space(suffix, position + 1)
    if position == len(suffix):
        return True
    if suffix[position] in "\"'":
        position += 1
    return _token_tail(suffix, position, lambda char: not char.isspace() and char not in ",;\"'}" )


_FRIENDLY_FIXED = ("password", "密码", "口令", "密钥", "访问令牌", "访问密钥",
                   "client_secret", "client-secret", "clientsecret",
                   "refresh_token", "refresh-token", "refreshtoken", "cookie", "set-cookie")


def _friendly_keyword(suffix: str):
    fixed = _literal(suffix, 0, _FRIENDLY_FIXED)
    if fixed is None or fixed >= 0:
        return fixed
    api = _literal(suffix, 0, ("API",))
    if api is None:
        return None
    if api < 0:
        return -1
    position = _space(suffix, api)
    key = _literal(suffix, position, ("Key",))
    return key


def _friendly_open(suffix: str, previous: str) -> bool:
    position = _friendly_keyword(suffix)
    if position is None:
        return True
    if position < 0:
        return False
    position = _space(suffix, position)
    if position == len(suffix):
        return True
    if suffix[position] in ":：=是为":
        position += 1
    elif suffix[position:position + 2].casefold() == "is":
        if position + 2 == len(suffix):
            return True
        if _word(suffix[position + 2]):
            return False
        position += 2
    elif "is".startswith(suffix[position:].casefold()):
        return True
    else:
        return False
    position = _space(suffix, position)
    return _token_tail(suffix, position, lambda char: not char.isspace() and char not in ",，;；\"'<>" )


def _credential_token_open(suffix: str, previous: str) -> bool:
    if _word(previous):
        return False
    folded = suffix.casefold()
    if "sk-".startswith(folded):
        return True
    if folded.startswith("sk-"):
        return all(char.isascii() and (char.isalnum() or char in "_-") for char in suffix[3:])
    for prefix in ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_"):
        if prefix.startswith(folded):
            return True
        if folded.startswith(prefix):
            return all(char.isascii() and (char.isalnum() or prefix == "github_pat_" and char == "_")
                       for char in suffix[len(prefix):])
    return False


_PEM_BEGIN = re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")
_PEM_BLOCK = re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z]+ )?PRIVATE KEY-----")


def _pem_header_prefix(suffix: str) -> bool:
    base = "-----BEGIN "
    if base.startswith(suffix):
        return True
    if not suffix.startswith(base):
        return False
    tail = suffix[len(base):]
    direct = "PRIVATE KEY-----"
    if direct.startswith(tail):
        return True
    position = 0
    while position < len(tail) and "A" <= tail[position] <= "Z":
        position += 1
    if position == 0 or position == len(tail):
        return position == len(tail)
    if tail[position] != " ":
        return False
    return direct.startswith(tail[position + 1:])


def _pem_open(suffix: str, previous: str) -> bool:
    begin = _PEM_BEGIN.match(suffix)
    if begin:
        return _PEM_BLOCK.match(suffix) is None
    return _pem_header_prefix(suffix)


class _LiteralStage:
    def __init__(self, value: str) -> None:
        self.value = value
        self.buffer = ""

    def feed(self, text: str) -> str:
        self.buffer += text
        output=[]
        position=0
        while True:
            found=self.buffer.find(self.value,position)
            if found<0:
                break
            output.append(self.buffer[position:found]); output.append(_REDACTED)
            position=found+len(self.value)
        remainder=self.buffer[position:]
        keep = 0
        maximum = min(len(remainder), len(self.value) - 1)
        for length in range(1, maximum + 1):
            if self.value.startswith(remainder[-length:]):
                keep = length
        output.append(remainder[:-keep] if keep else remainder)
        self.buffer = remainder[-keep:] if keep else ""
        return ''.join(output)

    def finish(self) -> str:
        result = self.buffer.replace(self.value, _REDACTED)
        self.buffer = ""
        return result


class _RuleStage:
    def __init__(self, pattern, replacement, possible_starts: str, open_match) -> None:
        self.pattern = pattern
        self.replacement = replacement
        self.possible_starts = possible_starts
        self.open_match = open_match
        self.buffer = ""
        self.previous = ""

    def _cut(self) -> int:
        context = self.previous + self.buffer if self.previous else self.buffer
        offset = 1 if self.previous else 0
        spans = [(match.start() - offset, match.end() - offset)
                 for match in self.pattern.finditer(context) if match.start() >= offset]
        for index, character in enumerate(self.buffer):
            if character in self.possible_starts:
                if any(start < index < end for start, end in spans):
                    continue
                previous = self.buffer[index - 1] if index else self.previous
                if self.open_match(self.buffer[index:], previous):
                    return index
        return len(self.buffer)

    def _replace(self, value: str) -> str:
        if not value:
            return ""
        context = self.previous + value if self.previous else value
        result = self.pattern.sub(self.replacement, context)
        if self.previous:
            if not result.startswith(self.previous):
                raise RuntimeError("stream redaction boundary invariant failed")
            result = result[len(self.previous):]
        self.previous = value[-1]
        return result

    def feed(self, text: str) -> str:
        self.buffer += text
        cut = self._cut()
        safe, self.buffer = self.buffer[:cut], self.buffer[cut:]
        return self._replace(safe)

    def finish(self) -> str:
        safe, self.buffer = self.buffer, ""
        return self._replace(safe)


class StreamingRedactor:
    """Incremental equivalent of Redactor.text for bounded assistant answers."""
    def __init__(self, redactor: Redactor, *, max_chars: int = _MAX_STREAM_CHARS) -> None:
        if type(max_chars) is not int or not 0 < max_chars <= _MAX_STREAM_CHARS:
            raise ValueError("stream redaction limit must be between 1 and 32768 characters")
        self._redactor = redactor
        self._max_chars = max_chars
        self._received = 0
        self._finished = False
        self._stages = [_LiteralStage(secret) for secret in redactor._secrets]
        self._stages.extend((
            _RuleStage(re.compile(r"(?i)\bBearer\s+[^\s\"'<>]+"), "Bearer [REDACTED]", "Bb", _bearer_open),
            _RuleStage(re.compile(r"(?i)(\b(?:proxy[-_])?authorization\s*[:=]\s*)(?:Basic|Digest)\s+[^\r\n\"'<>]+"),
                       r"\1[REDACTED]", "AaPp", _authorization_open),
            _RuleStage(re.compile(r"(?i)([\"']?(?:api[_-]?key|authorization|access[_-]?token|password)[\"']?\s*[:=]\s*)[\"']?[^\s,;\"'}]+[\"']?"),
                       r"\1[REDACTED]", "AaPp\"'", _assignment_open),
            _RuleStage(re.compile(r"(?i)((?:API\s*Key|password|密码|口令|密钥|访问令牌|访问密钥|client[_-]?secret|refresh[_-]?token|cookie|set-cookie)\s*(?:[:：=]|\bis\b|是|为)\s*)[^\s,，;；\"'<>]+"),
                       r"\1[REDACTED]", "AaPp密口访CcRrSs", _friendly_open),
            _RuleStage(re.compile(r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
                       _REDACTED, "sSgG", _credential_token_open),
            _RuleStage(_PEM_BLOCK, _REDACTED, "-", _pem_open),
        ))

    def _cascade(self, text: str, start: int = 0) -> str:
        for stage in self._stages[start:]:
            text = stage.feed(text)
            if not text:
                break
        if sum(len(stage.buffer) for stage in self._stages) > self._max_chars:
            raise ValueError("stream redaction buffer exceeds 32768 characters")
        return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)

    def feed(self, text: str) -> list[str]:
        if self._finished:
            raise RuntimeError("stream redactor is already closed")
        if not isinstance(text, str):
            raise TypeError("stream redactor accepts text only")
        self._received += len(text)
        if self._received > self._max_chars:
            raise ValueError("stream redaction input exceeds 32768 characters")
        output = self._cascade(text)
        return [output] if output else []

    def finish(self) -> list[str]:
        if self._finished:
            return []
        outputs: list[str] = []
        for index, stage in enumerate(self._stages):
            output = stage.finish()
            if output:
                output = self._cascade(output, index + 1)
                if output:
                    outputs.append(output)
        self._finished = True
        return outputs

    def discard(self) -> None:
        for stage in self._stages:
            stage.buffer = ""
        self._finished = True
