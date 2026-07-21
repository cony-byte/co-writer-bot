# -*- coding: utf-8 -*-
"""Offline transport contract test for native OpenRouter function calling."""
from __future__ import annotations

import json
from unittest.mock import patch

from bot import config
from bot import openrouter_image as oi


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps({
            "choices": [{"message": {"tool_calls": [{
                "type": "function",
                "function": {"name": "respond_with_answer",
                             "arguments": "{\"text\":\"ok\"}"},
            }]}}],
        }).encode()


def test_required_tool_call_payload_supports_compound_calls():
    captured = {}

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data.decode())
        captured["timeout"] = timeout
        return _Response()

    previous = config.OPENROUTER_API_KEY
    config.OPENROUTER_API_KEY = "test-key"
    try:
        with patch("urllib.request.urlopen", fake_urlopen):
            message = oi.tool_chat(
                "system", "hello",
                [{"type": "function", "function": {
                    "name": "respond_with_answer",
                    "description": "answer",
                    "parameters": {"type": "object"},
                }}],
                timeout=7,
            )
    finally:
        config.OPENROUTER_API_KEY = previous

    assert captured["payload"]["tool_choice"] == "required"
    assert captured["payload"]["parallel_tool_calls"] is True
    assert captured["payload"]["tools"][0]["function"]["name"] == "respond_with_answer"
    assert captured["timeout"] == 7
    assert message["tool_calls"][0]["function"]["name"] == "respond_with_answer"


if __name__ == "__main__":
    test_required_tool_call_payload_supports_compound_calls()
    print("1 OpenRouter tool-call transport test passed")
