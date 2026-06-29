import asyncio
import json

import httpx

from services.report_api.providers.base import LLMRequest
from services.report_api.providers.deepseek import DeepSeekProvider


def test_deepseek_provider_uses_v4_json_and_thinking_contract() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.deepseek.com/chat/completions"
        assert request.headers["authorization"] == "Bearer test-key"
        payload = json.loads(request.content)
        assert payload["model"] == "deepseek-v4-pro"
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["thinking"] == {"type": "enabled"}
        assert payload["reasoning_effort"] == "high"
        assert "temperature" not in payload
        return httpx.Response(
            200,
            headers={"x-request-id": "req-123"},
            json={
                "id": "chat-123",
                "model": "deepseek-v4-pro",
                "choices": [{"message": {"content": '{"title":"test"}'}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 4},
            },
        )

    provider = DeepSeekProvider(
        api_key="test-key",
        base_url="https://api.deepseek.com",
        timeout_seconds=10,
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(
        provider.generate_json(
            LLMRequest(
                purpose="test",
                model="deepseek-v4-pro",
                messages=[{"role": "user", "content": "Return json."}],
                thinking_enabled=True,
                max_output_tokens=100,
            )
        )
    )

    assert result.output == {"title": "test"}
    assert result.input_tokens == 12
    assert result.output_tokens == 4
    assert result.request_id == "req-123"
