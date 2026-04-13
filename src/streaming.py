import json
import logging
from typing import Any, AsyncGenerator

from .converter import FINISH_REASON_MAP

logger = logging.getLogger(__name__)


def format_sse(event_type: str, data: dict[str, Any]) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


async def stream_response(
    openai_stream: AsyncGenerator[str, None],
    original_model: str,
    message_id: str,
) -> AsyncGenerator[str, None]:
    """OpenAI SSE 스트림을 Anthropic SSE 이벤트로 변환하는 async generator."""

    block_index = 0
    text_block_started = False
    first_chunk = True
    ping_sent = False
    output_tokens = 0
    # tool_calls 상태: {tool_index: {"id": ..., "name": ..., "arguments": ...}}
    tool_calls: dict[int, dict[str, Any]] = {}
    active_tool_index: int | None = None
    # finish_reason 수신 시 즉시 종료하지 않고 usage trailing 청크를 기다림
    pending_stop_reason: str | None = None

    try:
        async for line in openai_stream:
            line = line.strip()
            if not line:
                continue
            if line.startswith("data: "):
                data_str = line[6:]
            else:
                continue

            if data_str == "[DONE]":
                break

            try:
                chunk = json.loads(data_str)
            except json.JSONDecodeError:
                logger.warning("Failed to parse SSE chunk: %s", data_str)
                continue

            # usage 정보 추출 (trailing 청크에서 올 수 있음)
            if chunk.get("usage"):
                output_tokens = chunk["usage"].get("completion_tokens", 0)

            choices = chunk.get("choices", [])
            if not choices:
                # usage-only trailing 청크일 수 있음 — 계속 진행
                continue

            choice = choices[0]
            delta = choice.get("delta", {})
            finish_reason = choice.get("finish_reason")

            # 첫 청크: message_start 발행
            if first_chunk:
                first_chunk = False
                yield format_sse("message_start", {
                    "type": "message_start",
                    "message": {
                        "id": message_id,
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "model": original_model,
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0,
                        },
                    },
                })

            # 텍스트 delta 처리
            content = delta.get("content")
            if content is not None:
                if not text_block_started:
                    text_block_started = True
                    yield format_sse("content_block_start", {
                        "type": "content_block_start",
                        "index": block_index,
                        "content_block": {"type": "text", "text": ""},
                    })
                    if not ping_sent:
                        ping_sent = True
                        yield format_sse("ping", {"type": "ping"})
                yield format_sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": block_index,
                    "delta": {"type": "text_delta", "text": content},
                })

            # tool_calls delta 처리
            tc_deltas = delta.get("tool_calls")
            if tc_deltas:
                for tc_delta in tc_deltas:
                    tc_index = tc_delta.get("index", 0)

                    # 새 tool_call 시작 (id와 function.name이 포함됨)
                    if tc_delta.get("id") or (tc_delta.get("function") and tc_delta["function"].get("name")):
                        # 이전 텍스트 블록이 열려있으면 닫기
                        if text_block_started and active_tool_index is None:
                            yield format_sse("content_block_stop", {
                                "type": "content_block_stop",
                                "index": block_index,
                            })
                            block_index += 1
                            text_block_started = False

                        # 이전 tool 블록이 열려있으면 닫기
                        if active_tool_index is not None and active_tool_index != tc_index:
                            yield format_sse("content_block_stop", {
                                "type": "content_block_stop",
                                "index": block_index,
                            })
                            block_index += 1

                        tc_id = tc_delta.get("id", "")
                        tc_name = tc_delta.get("function", {}).get("name", "")
                        tool_calls[tc_index] = {
                            "id": tc_id,
                            "name": tc_name,
                            "arguments": "",
                        }
                        active_tool_index = tc_index

                        yield format_sse("content_block_start", {
                            "type": "content_block_start",
                            "index": block_index,
                            "content_block": {
                                "type": "tool_use",
                                "id": tc_id,
                                "name": tc_name,
                            },
                        })
                        if not ping_sent:
                            ping_sent = True
                            yield format_sse("ping", {"type": "ping"})

                    # arguments 청크
                    args_chunk = tc_delta.get("function", {}).get("arguments", "")
                    if args_chunk:
                        if tc_index in tool_calls:
                            tool_calls[tc_index]["arguments"] += args_chunk
                        yield format_sse("content_block_delta", {
                            "type": "content_block_delta",
                            "index": block_index,
                            "delta": {
                                "type": "input_json_delta",
                                "partial_json": args_chunk,
                            },
                        })

            # finish_reason 수신 — 바로 종료하지 않고 플래그만 설정
            if finish_reason is not None:
                pending_stop_reason = FINISH_REASON_MAP.get(finish_reason, "end_turn")
                # 마지막 활성 블록 닫기
                if text_block_started or active_tool_index is not None:
                    yield format_sse("content_block_stop", {
                        "type": "content_block_stop",
                        "index": block_index,
                    })
                    text_block_started = False
                    active_tool_index = None

        # 루프 종료 ([DONE] 또는 스트림 끝) — 최종 이벤트 발행
        stop_reason = pending_stop_reason or "end_turn"

        # finish_reason 없이 블록이 열려있으면 닫기
        if text_block_started or active_tool_index is not None:
            yield format_sse("content_block_stop", {
                "type": "content_block_stop",
                "index": block_index,
            })

        yield format_sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": output_tokens},
        })
        yield format_sse("message_stop", {"type": "message_stop"})

    except Exception as e:
        logger.error("Streaming error: %s", e, exc_info=True)
        # 스트리밍 도중 에러 — 정상 종료 시퀀스 발행
        if not first_chunk:
            if text_block_started or active_tool_index is not None:
                yield format_sse("content_block_stop", {
                    "type": "content_block_stop",
                    "index": block_index,
                })
            yield format_sse("message_delta", {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 0},
            })
            yield format_sse("message_stop", {"type": "message_stop"})
        else:
            raise
