"""Provider-neutral embeddings with one bounded OpenAI-compatible adapter."""
from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
import json
import math
from typing import Literal, Protocol, runtime_checkable

import httpx

from ..errors import ForgePilotError
from ..security import Redactor
from .config import RagRuntimeAvailability, RagRuntimeSettings


Embedding = list[float]


@runtime_checkable
class EmbeddingProvider(Protocol):
    async def embed_documents(self, texts: Sequence[str]) -> list[Embedding]: ...
    async def embed_query(self, text: str) -> Embedding: ...


@dataclass(frozen=True, slots=True)
class EmbeddingProviderAvailability:
    """An optional adapter result which is safe for disabled startup paths."""

    status: Literal["available", "unavailable"]
    provider: EmbeddingProvider | None = None
    reason: Literal[
        "disabled", "not_configured", "invalid_configuration", "initialization_failed",
    ] | None = None

    @property
    def available(self) -> bool:
        return self.status == "available"


class OpenAICompatibleEmbeddingProvider:
    """A bounded client for the provider-neutral OpenAI ``/embeddings`` API."""

    _RESPONSE_LIMIT = 32 * 1_048_576
    _TEXT_LIMIT = 1_048_576

    def __init__(self, base_url: str, api_key: str, model: str, *, batch_size: int = 16,
                 timeout: float = 60.0,
                 transport: httpx.AsyncBaseTransport | None = None,
                 limits: httpx.Limits | None = None) -> None:
        if (not all(isinstance(value, str) and value.strip()
                    for value in (base_url, api_key, model))
                or len(model) > 256 or len(api_key) > 4_096
                or any(ord(character) < 32 or ord(character) == 127
                       for character in api_key)):
            raise ForgePilotError("rag_embedding_config",
                                  "Embedding base URL, credential and model are required")
        try:
            url = httpx.URL(base_url)
        except (httpx.InvalidURL, ValueError):
            raise ForgePilotError("rag_embedding_config",
                                  "Embedding base URL is not a valid API root URL") from None
        if (url.scheme not in ("http", "https") or not url.host or url.userinfo
                or url.query or url.fragment):
            raise ForgePilotError("rag_embedding_config",
                                  "Use an http(s) embedding API root without credentials/query/fragment")
        if type(batch_size) is not int or not 1 <= batch_size <= 256:
            raise ForgePilotError("rag_embedding_config", "Embedding batch size must be between 1 and 256")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 600:
            raise ForgePilotError("rag_embedding_config", "Embedding timeout must be in (0, 600] seconds")

        self._url = str(url).rstrip("/") + "/embeddings"
        self._api_key = api_key
        self.model = model
        self.batch_size = batch_size
        self.timeout = float(timeout)
        self.dimension: int | None = None
        self.redactor = Redactor((api_key,))
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=self.timeout,
            limits=limits or httpx.Limits(
                max_connections=10, max_keepalive_connections=5, keepalive_expiry=30.0),
            follow_redirects=False,
            trust_env=False,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    @classmethod
    def _validate_text(cls, text: object) -> str:
        if not isinstance(text, str) or not text or len(text) > cls._TEXT_LIMIT or "\0" in text:
            raise ForgePilotError("rag_embedding_input",
                                  "Embedding input must be bounded nonempty text")
        return text

    @staticmethod
    def _http_error(status: int) -> ForgePilotError:
        reason = ("Embedding authentication failed" if status == 401 else
                  "Embedding access was denied" if status == 403 else
                  "Embedding rate limit reached" if status == 429 else
                  "Redirects are disabled" if 300 <= status < 400 else
                  "Check the embedding endpoint, model and service availability")
        return ForgePilotError("rag_embedding_http", f"HTTP {status}. {reason}")

    def _validate_response(self, raw: bytearray, expected: int) -> list[Embedding]:
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise ValueError("Invalid embeddings response")
        items = data["data"]
        if len(items) != expected:
            raise ValueError("Unexpected embedding count")
        ordered: list[Embedding | None] = [None] * expected
        for item in items:
            if not isinstance(item, dict) or type(item.get("index")) is not int:
                raise ValueError("Invalid embedding item")
            index = item["index"]
            vector = item.get("embedding")
            if (not 0 <= index < expected or ordered[index] is not None
                    or not isinstance(vector, list) or not vector):
                raise ValueError("Invalid embedding item")
            checked: Embedding = []
            for value in vector:
                if type(value) not in (int, float) or not math.isfinite(value):
                    raise ValueError("Invalid embedding value")
                checked.append(float(value))
            if self.dimension is None:
                self.dimension = len(checked)
            if len(checked) != self.dimension:
                raise ForgePilotError(
                    "rag_embedding_dimension",
                    "Embedding service returned inconsistent dimensions; rebuild the RAG index",
                )
            ordered[index] = checked
        if any(vector is None for vector in ordered):
            raise ValueError("Missing embedding item")
        return [vector for vector in ordered if vector is not None]

    async def _embed_batch(self, texts: list[str]) -> list[Embedding]:
        payload = {"model": self.model, "input": texts}
        try:
            async with asyncio.timeout(self.timeout):
                async with self._client.stream(
                    "POST", self._url, json=payload,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                ) as response:
                    if not 200 <= response.status_code < 300:
                        raise self._http_error(response.status_code)
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > self._RESPONSE_LIMIT:
                            raise ForgePilotError("rag_embedding_response_limit",
                                                  "Embedding response exceeds 32 MiB")
                        body.extend(chunk)
            return self._validate_response(body, len(texts))
        except (TimeoutError, httpx.TimeoutException):
            raise ForgePilotError("rag_embedding_timeout", "Embedding request timed out") from None
        except ForgePilotError:
            raise
        except httpx.HTTPError:
            raise ForgePilotError("rag_embedding_network",
                                  "Embedding transport failed; check service and network") from None
        except (UnicodeError, json.JSONDecodeError, ValueError, TypeError, KeyError,
                AttributeError, RecursionError):
            raise ForgePilotError("rag_embedding_protocol",
                                  "Invalid embeddings response structure") from None
        except Exception:
            # Custom test/provider transports must not expose request bodies, URLs or secrets.
            raise ForgePilotError("rag_embedding_network",
                                  "Embedding transport failed; check service and network") from None

    async def embed_documents(self, texts: Sequence[str]) -> list[Embedding]:
        if isinstance(texts, (str, bytes)) or not isinstance(texts, Sequence):
            raise ForgePilotError("rag_embedding_input", "Embedding documents must be a sequence of text")
        checked = [self._validate_text(text) for text in texts]
        result: list[Embedding] = []
        for offset in range(0, len(checked), self.batch_size):
            result.extend(await self._embed_batch(checked[offset:offset + self.batch_size]))
        return result

    async def embed_query(self, text: str) -> Embedding:
        return (await self._embed_batch([self._validate_text(text)]))[0]


def embedding_provider_from_runtime(
        runtime: RagRuntimeAvailability | RagRuntimeSettings | None, *, timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None) -> EmbeddingProviderAvailability:
    """Construct lazily; disabled/unconfigured RAG remains an explicit inert state."""
    if isinstance(runtime, RagRuntimeAvailability):
        if not runtime.settings:
            return EmbeddingProviderAvailability(
                "unavailable", reason=runtime.reason or "not_configured")
        runtime = runtime.settings
    if runtime is None:
        return EmbeddingProviderAvailability("unavailable", reason="disabled")
    config = runtime.config
    if not config.enabled or not config.embedding_base_url or not config.embedding_model:
        return EmbeddingProviderAvailability("unavailable", reason="not_configured")
    try:
        provider = OpenAICompatibleEmbeddingProvider(
            config.embedding_base_url,
            runtime.embedding_api_key,
            config.embedding_model,
            batch_size=config.embedding_batch_size,
            timeout=timeout,
            transport=transport,
        )
    except ForgePilotError:
        return EmbeddingProviderAvailability("unavailable", reason="initialization_failed")
    return EmbeddingProviderAvailability("available", provider=provider)
