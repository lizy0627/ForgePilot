"""The suite refuses accidental provider traffic before opening a socket."""
import httpx
import pytest


@pytest.mark.parametrize("url", [
    "https://provider.invalid/v1/chat/completions",
    "https://provider.invalid/v1/embeddings",
    "http://127.0.0.1:1/v1/chat/completions",
    "https://provider.invalid/api/v3/embeddings/multimodal",
])
def test_sync_model_http_is_disabled(url):
    with httpx.Client(trust_env=False) as client:
        with pytest.raises(pytest.fail.Exception, match="Real model HTTP transport"):
            client.post(url, json={})


@pytest.mark.asyncio
async def test_async_model_http_is_disabled_but_mock_transport_is_available():
    async with httpx.AsyncClient(trust_env=False) as client:
        with pytest.raises(pytest.fail.Exception, match="Real model HTTP transport"):
            await client.post("https://provider.invalid/v1/chat/completions", json={})
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"fake": True}))) as client:
        result = await client.post("https://provider.invalid/v1/chat/completions", json={})
        assert result.json() == {"fake": True}
