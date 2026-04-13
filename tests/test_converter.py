import json
from unittest.mock import patch

import pytest

from src.converter import convert_request, convert_response
from src.models import AnthropicRequest, AnthropicTool


# === 요청 변환 테스트 ===


def _make_request(**kwargs) -> AnthropicRequest:
    defaults = {"model": "claude-sonnet-4-20250514", "max_tokens": 1024, "messages": []}
    defaults.update(kwargs)
    return AnthropicRequest(**defaults)


class TestBasicTextMessage:
    def test_simple_string_content(self):
        req = _make_request(
            messages=[{"role": "user", "content": "Hello"}]
        )
        result = convert_request(req)
        assert result["messages"] == [{"role": "user", "content": "Hello"}]

    def test_model_replaced(self):
        req = _make_request(
            messages=[{"role": "user", "content": "Hi"}]
        )
        with patch("src.converter.settings") as mock_settings:
            mock_settings.model_name = "test-model"
            result = convert_request(req)
        assert result["model"] == "test-model"

    def test_max_tokens_mapped(self):
        req = _make_request(
            messages=[{"role": "user", "content": "Hi"}]
        )
        result = convert_request(req)
        assert result["max_completion_tokens"] == 1024
        assert "max_tokens" not in result


class TestSystemMessage:
    def test_string_system(self):
        req = _make_request(
            system="You are helpful.",
            messages=[{"role": "user", "content": "Hi"}],
        )
        result = convert_request(req)
        assert result["messages"][0] == {"role": "system", "content": "You are helpful."}

    def test_array_system(self):
        req = _make_request(
            system=[{"type": "text", "text": "Rule 1."}, {"type": "text", "text": "Rule 2."}],
            messages=[{"role": "user", "content": "Hi"}],
        )
        result = convert_request(req)
        assert result["messages"][0] == {"role": "system", "content": "Rule 1.\nRule 2."}


class TestToolUseBlock:
    def test_assistant_tool_use_to_tool_calls(self):
        req = _make_request(
            messages=[
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Reading file."},
                        {
                            "type": "tool_use",
                            "id": "toolu_01",
                            "name": "Read",
                            "input": {"file_path": "/src/main.py"},
                        },
                    ],
                }
            ]
        )
        result = convert_request(req)
        msg = result["messages"][0]
        assert msg["role"] == "assistant"
        assert msg["content"] == "Reading file."
        assert len(msg["tool_calls"]) == 1
        assert msg["tool_calls"][0]["id"] == "toolu_01"
        assert msg["tool_calls"][0]["function"]["name"] == "Read"
        assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {
            "file_path": "/src/main.py"
        }


class TestToolResult:
    def test_tool_result_to_tool_message(self):
        req = _make_request(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_01",
                            "content": "file contents here",
                        }
                    ],
                }
            ]
        )
        result = convert_request(req)
        assert result["messages"] == [
            {"role": "tool", "tool_call_id": "toolu_01", "content": "file contents here"}
        ]


class TestImageConversion:
    def test_base64_image_to_data_uri(self):
        req = _make_request(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What is this?"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "abc123",
                            },
                        },
                    ],
                }
            ]
        )
        result = convert_request(req)
        content = result["messages"][0]["content"]
        assert content[0] == {"type": "text", "text": "What is this?"}
        assert content[1]["type"] == "image_url"
        assert content[1]["image_url"]["url"] == "data:image/png;base64,abc123"


class TestToolsDefinition:
    def test_input_schema_to_parameters(self):
        req = _make_request(
            messages=[{"role": "user", "content": "Hi"}],
            tools=[
                AnthropicTool(
                    name="Read",
                    description="Reads a file",
                    input_schema={
                        "type": "object",
                        "properties": {"file_path": {"type": "string"}},
                        "required": ["file_path"],
                    },
                )
            ],
        )
        result = convert_request(req)
        tool = result["tools"][0]
        assert tool["type"] == "function"
        assert tool["function"]["name"] == "Read"
        assert tool["function"]["description"] == "Reads a file"
        assert tool["function"]["parameters"]["type"] == "object"


class TestToolChoice:
    def test_auto(self):
        req = _make_request(
            messages=[{"role": "user", "content": "Hi"}],
            tool_choice={"type": "auto"},
        )
        result = convert_request(req)
        assert result["tool_choice"] == "auto"

    def test_any(self):
        req = _make_request(
            messages=[{"role": "user", "content": "Hi"}],
            tool_choice={"type": "any"},
        )
        result = convert_request(req)
        assert result["tool_choice"] == "required"

    def test_specific_tool(self):
        req = _make_request(
            messages=[{"role": "user", "content": "Hi"}],
            tool_choice={"type": "tool", "name": "Read"},
        )
        result = convert_request(req)
        assert result["tool_choice"] == {
            "type": "function",
            "function": {"name": "Read"},
        }


# === 응답 변환 테스트 ===


class TestResponseConversion:
    def test_text_response(self):
        openai_resp = {
            "id": "chatcmpl-abc",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "Hello!", "tool_calls": None},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        result = convert_response(openai_resp, "claude-sonnet-4-20250514")
        assert result["type"] == "message"
        assert result["model"] == "claude-sonnet-4-20250514"
        assert result["content"] == [{"type": "text", "text": "Hello!"}]
        assert result["stop_reason"] == "end_turn"
        assert result["usage"]["input_tokens"] == 10
        assert result["usage"]["output_tokens"] == 5

    def test_tool_calls_response(self):
        openai_resp = {
            "id": "chatcmpl-abc",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Reading file.",
                        "tool_calls": [
                            {
                                "id": "call_abc",
                                "type": "function",
                                "function": {
                                    "name": "Read",
                                    "arguments": '{"file_path":"/src/main.py"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        }
        result = convert_response(openai_resp, "claude-sonnet-4-20250514")
        assert result["stop_reason"] == "tool_use"
        assert result["content"][0] == {"type": "text", "text": "Reading file."}
        assert result["content"][1]["type"] == "tool_use"
        assert result["content"][1]["name"] == "Read"
        assert result["content"][1]["input"] == {"file_path": "/src/main.py"}


class TestStopReasonMapping:
    @pytest.mark.parametrize(
        "finish_reason,expected",
        [
            ("stop", "end_turn"),
            ("length", "max_tokens"),
            ("tool_calls", "tool_use"),
            ("content_filter", "end_turn"),
            (None, "end_turn"),
        ],
    )
    def test_mapping(self, finish_reason, expected):
        openai_resp = {
            "id": "chatcmpl-abc",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "Hi"},
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        result = convert_response(openai_resp, "test-model")
        assert result["stop_reason"] == expected


class TestMixedUserMessage:
    def test_text_and_tool_result_split(self):
        req = _make_request(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_01", "content": "file contents"},
                        {"type": "tool_result", "tool_use_id": "toolu_02", "content": "search results"},
                        {"type": "text", "text": "Analyze these results"},
                    ],
                }
            ]
        )
        result = convert_request(req)
        assert len(result["messages"]) == 3
        assert result["messages"][0] == {
            "role": "tool",
            "tool_call_id": "toolu_01",
            "content": "file contents",
        }
        assert result["messages"][1] == {
            "role": "tool",
            "tool_call_id": "toolu_02",
            "content": "search results",
        }
        assert result["messages"][2] == {
            "role": "user",
            "content": "Analyze these results",
        }


class TestContentNullResponse:
    def test_null_content_with_tool_calls(self):
        openai_resp = {
            "id": "chatcmpl-abc",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_abc",
                                "type": "function",
                                "function": {"name": "Read", "arguments": '{"file_path":"/a.py"}'},
                            },
                            {
                                "id": "call_def",
                                "type": "function",
                                "function": {"name": "Grep", "arguments": '{"pattern":"TODO"}'},
                            },
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 10},
        }
        result = convert_response(openai_resp, "test-model")
        assert len(result["content"]) == 2
        assert all(c["type"] == "tool_use" for c in result["content"])
        assert result["content"][0]["name"] == "Read"
        assert result["content"][1]["name"] == "Grep"


class TestMultipleToolCallsResponse:
    def test_multiple_tool_calls(self):
        openai_resp = {
            "id": "chatcmpl-abc",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "I'll read both files.",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "Read", "arguments": '{"path":"a.py"}'},
                            },
                            {
                                "id": "call_2",
                                "type": "function",
                                "function": {"name": "Read", "arguments": '{"path":"b.py"}'},
                            },
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 15},
        }
        result = convert_response(openai_resp, "test-model")
        assert len(result["content"]) == 3
        assert result["content"][0]["type"] == "text"
        assert result["content"][1]["type"] == "tool_use"
        assert result["content"][2]["type"] == "tool_use"


class TestArrayContentToolResult:
    def test_image_content_extracts_text_only(self):
        req = _make_request(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_01",
                            "content": [
                                {"type": "text", "text": "screenshot captured"},
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "image/png",
                                        "data": "abc123",
                                    },
                                },
                            ],
                        }
                    ],
                }
            ]
        )
        result = convert_request(req)
        assert result["messages"][0] == {
            "role": "tool",
            "tool_call_id": "toolu_01",
            "content": "screenshot captured",
        }


class TestComplexConversation:
    def test_full_conversation_flow(self):
        """텍스트, tool_use, tool_result가 섞인 전체 대화 흐름 테스트."""
        req = _make_request(
            system="You are a coding assistant.",
            messages=[
                {"role": "user", "content": "Read main.py"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "I'll read the file."},
                        {
                            "type": "tool_use",
                            "id": "toolu_01",
                            "name": "Read",
                            "input": {"file_path": "/src/main.py"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_01",
                            "content": "print('hello')",
                        },
                    ],
                },
                {"role": "assistant", "content": "The file contains a print statement."},
            ],
        )
        result = convert_request(req)
        msgs = result["messages"]
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"
        assert msgs[2]["role"] == "assistant"
        assert "tool_calls" in msgs[2]
        assert msgs[3]["role"] == "tool"
        assert msgs[4]["role"] == "assistant"
        assert msgs[4]["content"] == "The file contains a print statement."
