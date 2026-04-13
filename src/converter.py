import json
import logging
from typing import Any

from .config import settings
from .models import AnthropicRequest

logger = logging.getLogger(__name__)

FINISH_REASON_MAP: dict[str, str] = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "end_turn",
}


def convert_request(anthropic_req: AnthropicRequest) -> dict[str, Any]:
    """Anthropic Messages API 요청을 OpenAI Chat Completions API 요청으로 변환."""
    openai_messages: list[dict[str, Any]] = []

    # 1. system 메시지 처리
    if anthropic_req.system is not None:
        if isinstance(anthropic_req.system, str):
            system_text = anthropic_req.system
        else:
            # list of content blocks — text type들의 text를 합침
            system_text = "\n".join(
                block["text"]
                for block in anthropic_req.system
                if block.get("type") == "text"
            )
        if system_text:
            openai_messages.append({"role": "system", "content": system_text})

    # 2. messages 변환
    for msg in anthropic_req.messages:
        if isinstance(msg.content, str):
            openai_messages.append({"role": msg.role, "content": msg.content})
            continue

        # content가 list인 경우
        if msg.role == "assistant":
            _convert_assistant_message(msg.content, openai_messages)
        elif msg.role == "user":
            _convert_user_message(msg.content, openai_messages)
        else:
            openai_messages.append({"role": msg.role, "content": msg.content})

    # 3. OpenAI 요청 구성
    openai_req: dict[str, Any] = {
        "model": settings.model_name,
        "messages": openai_messages,
    }

    # max_tokens → max_completion_tokens
    openai_req["max_completion_tokens"] = anthropic_req.max_tokens

    # stream
    if anthropic_req.stream:
        openai_req["stream"] = True

    # optional fields
    if anthropic_req.temperature is not None:
        openai_req["temperature"] = anthropic_req.temperature
    if anthropic_req.top_p is not None:
        openai_req["top_p"] = anthropic_req.top_p
    if anthropic_req.stop_sequences is not None:
        openai_req["stop"] = anthropic_req.stop_sequences

    # 4. tools 변환
    if anthropic_req.tools:
        openai_req["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    **({"description": tool.description} if tool.description else {}),
                    "parameters": tool.input_schema,
                },
            }
            for tool in anthropic_req.tools
        ]

    # 5. tool_choice 변환
    if anthropic_req.tool_choice is not None:
        tc_type = anthropic_req.tool_choice.get("type")
        if tc_type == "auto":
            openai_req["tool_choice"] = "auto"
        elif tc_type == "any":
            openai_req["tool_choice"] = "required"
        elif tc_type == "tool":
            openai_req["tool_choice"] = {
                "type": "function",
                "function": {"name": anthropic_req.tool_choice["name"]},
            }

    return openai_req


def _convert_assistant_message(
    content: list[dict[str, Any]], openai_messages: list[dict[str, Any]]
) -> None:
    """assistant 메시지의 content 블록들을 OpenAI 형식으로 변환."""
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []

    for block in content:
        if block.get("type") == "text":
            text_parts.append(block["text"])
        elif block.get("type") == "tool_use":
            tool_calls.append(
                {
                    "id": block["id"],
                    "type": "function",
                    "function": {
                        "name": block["name"],
                        "arguments": json.dumps(block["input"]),
                    },
                }
            )

    msg: dict[str, Any] = {"role": "assistant"}
    msg["content"] = "\n".join(text_parts) if text_parts else None
    if tool_calls:
        msg["tool_calls"] = tool_calls
    openai_messages.append(msg)


def _convert_user_message(
    content: list[dict[str, Any]], openai_messages: list[dict[str, Any]]
) -> None:
    """user 메시지의 content 블록들을 OpenAI 형식으로 변환. 혼합 메시지는 분리."""
    # tool_result가 포함되어 있는지 확인
    has_tool_result = any(b.get("type") == "tool_result" for b in content)

    if has_tool_result:
        # 혼합 메시지 — 블록 순서를 유지하며 각각 별도 메시지로 분리
        for block in content:
            if block.get("type") == "tool_result":
                tool_content = _extract_tool_result_content(block.get("content", ""))
                openai_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": block["tool_use_id"],
                        "content": tool_content,
                    }
                )
            elif block.get("type") == "text":
                openai_messages.append({"role": "user", "content": block["text"]})
            elif block.get("type") == "image":
                logger.warning("Image block in mixed message (with tool_result) is not supported and will be dropped")
    else:
        # 일반 user 메시지 (text, image 등)
        openai_content: list[dict[str, Any]] = []
        for block in content:
            if block.get("type") == "text":
                openai_content.append({"type": "text", "text": block["text"]})
            elif block.get("type") == "image":
                source = block["source"]
                data_uri = f"data:{source['media_type']};base64,{source['data']}"
                openai_content.append(
                    {"type": "image_url", "image_url": {"url": data_uri}}
                )

        # 단일 텍스트만 있으면 string으로 단순화
        if len(openai_content) == 1 and openai_content[0].get("type") == "text":
            openai_messages.append(
                {"role": "user", "content": openai_content[0]["text"]}
            )
        else:
            openai_messages.append({"role": "user", "content": openai_content})


def _extract_tool_result_content(content: Any) -> str:
    """tool_result의 content를 문자열로 변환. 배열이면 텍스트만 추출."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [
            block["text"] for block in content if block.get("type") == "text"
        ]
        return "\n".join(texts)
    return str(content) if content else ""


def convert_response(openai_resp: dict[str, Any], original_model: str) -> dict[str, Any]:
    """OpenAI Chat Completions 응답을 Anthropic Messages API 응답으로 변환."""
    choice = openai_resp["choices"][0]
    message = choice["message"]

    # content 블록 생성
    content_blocks: list[dict[str, Any]] = []

    if message.get("content"):
        content_blocks.append({"type": "text", "text": message["content"]})

    if message.get("tool_calls"):
        for tc in message["tool_calls"]:
            try:
                input_obj = json.loads(tc["function"]["arguments"])
            except (json.JSONDecodeError, TypeError):
                logger.warning("Failed to parse tool arguments: %s", tc["function"].get("arguments"))
                input_obj = {}
            content_blocks.append(
                {
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["function"]["name"],
                    "input": input_obj,
                }
            )

    # stop_reason 매핑
    finish_reason = choice.get("finish_reason", "stop")
    stop_reason = FINISH_REASON_MAP.get(finish_reason, "end_turn")

    # usage 변환
    usage = openai_resp.get("usage", {})

    return {
        "id": f"msg_{openai_resp.get('id', 'unknown')}",
        "type": "message",
        "role": "assistant",
        "model": original_model,
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }
