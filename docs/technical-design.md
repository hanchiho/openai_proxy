# 기술 설계 문서: Anthropic-to-OpenAI Proxy Server

## 1. 시스템 아키텍처

### 1.1 전체 구조

```
┌─────────────┐       ┌──────────────────┐       ┌─────────────────┐
│ Claude Code  │──────>│   Proxy Server   │──────>│  vLLM Server    │
│ (Anthropic   │ HTTP  │  (FastAPI)       │ HTTP  │  (OpenAI compat)│
│  API format) │<──────│                  │<──────│                 │
└─────────────┘       │  - 요청 변환      │       └─────────────────┘
                      │  - 응답 변환      │
                      │  - 스트리밍 처리   │
                      └──────────────────┘
                        Podman Container
```

### 1.2 요청/응답 흐름

```
1. Claude Code → POST /v1/messages (Anthropic 형식)
2. 프록시: 요청 파싱 및 검증
3. 프록시: Anthropic → OpenAI 형식 변환
4. 프록시 → POST {OPENAI_BASE_URL}/chat/completions (OpenAI 형식)
5. vLLM → 응답 (OpenAI 형식)
6. 프록시: OpenAI → Anthropic 형식 변환
7. 프록시 → Claude Code (Anthropic 형식)
```

### 1.3 스트리밍 흐름

```
1. Claude Code → POST /v1/messages (stream: true)
2. 프록시 → POST /chat/completions (stream: true)
3. vLLM → SSE 청크들 (OpenAI 형식)
4. 프록시: 각 청크를 Anthropic SSE 이벤트로 변환
   - 첫 청크 → message_start + content_block_start + ping
   - 텍스트 청크 → content_block_delta (text_delta)
   - tool_calls 청크 → content_block_delta (input_json_delta)
   - 종료 → content_block_stop + message_delta + message_stop
5. Claude Code ← SSE 이벤트 스트림
```

---

## 2. API 매핑 명세

### 2.1 엔드포인트 매핑

| 프록시 엔드포인트 | 백엔드 엔드포인트 | 설명 |
|---|---|---|
| `POST /v1/messages` | `POST {OPENAI_BASE_URL}/chat/completions` | 메시지 생성 |
| `GET /health` | - | 헬스체크 (프록시 자체) |

### 2.1.1 헤더 처리

Claude Code는 다음 헤더를 함께 보낸다. 프록시는 이를 **수신하되 무시**한다 (검증/전달하지 않음):
- `x-api-key`: Anthropic API 키 (프록시에서는 사용하지 않음)
- `anthropic-version`: API 버전 (예: `2023-06-01`)
- `anthropic-beta`: 베타 기능 플래그

백엔드로 전달하는 헤더는 `Authorization: Bearer {OPENAI_API_KEY}`와 `Content-Type: application/json`만 사용한다.

### 2.2 요청 변환: Anthropic → OpenAI

#### 2.2.1 최상위 필드

| Anthropic 필드 | OpenAI 필드 | 변환 규칙 |
|---|---|---|
| `model` | `model` | `.env`의 `MODEL_NAME` 값으로 교체 |
| `max_tokens` | `max_completion_tokens` | 그대로 전달 |
| `stop_sequences` | `stop` | 그대로 전달 (배열) |
| `temperature` | `temperature` | 그대로 전달 |
| `top_p` | `top_p` | 그대로 전달 |
| `top_k` | - | OpenAI 미지원, 무시 |
| `stream` | `stream` | 그대로 전달 |
| `system` | `messages[0]` | system role 메시지로 삽입 |
| `tools` | `tools` | 2.2.4 참조 |
| `tool_choice` | `tool_choice` | 2.2.5 참조 |
| `metadata` | - | 무시 |

#### 2.2.2 메시지 변환

**일반 텍스트 메시지:**
```json
// Anthropic
{
  "role": "user",
  "content": "Hello"
}

// → OpenAI
{
  "role": "user",
  "content": "Hello"
}
```

**복합 콘텐츠 (텍스트 + 이미지):**
```json
// Anthropic
{
  "role": "user",
  "content": [
    {"type": "text", "text": "What is this?"},
    {"type": "image", "source": {
      "type": "base64",
      "media_type": "image/png",
      "data": "<base64_data>"
    }}
  ]
}

// → OpenAI
{
  "role": "user",
  "content": [
    {"type": "text", "text": "What is this?"},
    {"type": "image_url", "image_url": {
      "url": "data:image/png;base64,<base64_data>"
    }}
  ]
}
```

**assistant 메시지에 tool_use 블록이 있는 경우:**
```json
// Anthropic
{
  "role": "assistant",
  "content": [
    {"type": "text", "text": "파일을 읽겠습니다."},
    {"type": "tool_use", "id": "toolu_01", "name": "Read", "input": {"file_path": "/src/main.py"}}
  ]
}

// → OpenAI
{
  "role": "assistant",
  "content": "파일을 읽겠습니다.",
  "tool_calls": [
    {
      "id": "toolu_01",
      "type": "function",
      "function": {
        "name": "Read",
        "arguments": "{\"file_path\":\"/src/main.py\"}"
      }
    }
  ]
}
```

**tool_result 메시지:**
```json
// Anthropic
{
  "role": "user",
  "content": [
    {
      "type": "tool_result",
      "tool_use_id": "toolu_01",
      "content": "file contents here..."
    }
  ]
}

// → OpenAI
{
  "role": "tool",
  "tool_call_id": "toolu_01",
  "content": "file contents here..."
}
```

> **주의**: Anthropic은 `tool_result`를 `user` role 메시지 안에 배열로 보낸다. 하나의 user 메시지에 여러 `tool_result`가 있을 수 있으므로, 각각을 별도의 OpenAI `tool` role 메시지로 분리해야 한다.

**혼합 콘텐츠 메시지 (text + tool_result):**
```json
// Anthropic — 하나의 user 메시지에 text와 tool_result가 혼합
{
  "role": "user",
  "content": [
    {"type": "tool_result", "tool_use_id": "toolu_01", "content": "file contents..."},
    {"type": "tool_result", "tool_use_id": "toolu_02", "content": "search results..."},
    {"type": "text", "text": "위 결과를 분석해줘"}
  ]
}

// → OpenAI — 순서를 유지하며 각각 별도 메시지로 분리
{"role": "tool", "tool_call_id": "toolu_01", "content": "file contents..."},
{"role": "tool", "tool_call_id": "toolu_02", "content": "search results..."},
{"role": "user", "content": "위 결과를 분석해줘"}
```

**tool_result에 배열 content (이미지 포함)가 있는 경우:**
```json
// Anthropic
{
  "type": "tool_result",
  "tool_use_id": "toolu_01",
  "content": [
    {"type": "text", "text": "screenshot captured"},
    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "<base64>"}}
  ]
}

// → OpenAI — 텍스트 부분만 추출 (OpenAI tool role은 문자열 content만 지원)
{"role": "tool", "tool_call_id": "toolu_01", "content": "screenshot captured"}
```

> **참고**: tool_result의 이미지는 OpenAI tool role 메시지에서 지원되지 않으므로, 텍스트 부분만 추출한다. 이미지가 중요한 경우 별도 처리가 필요할 수 있으나, 현재 범위에서는 텍스트만 전달한다.

#### 2.2.3 system 메시지 변환

```json
// Anthropic (string)
{ "system": "You are a helpful assistant." }

// Anthropic (array)
{ "system": [{"type": "text", "text": "You are a helpful assistant."}] }

// → OpenAI
// messages 배열 맨 앞에 삽입:
{ "role": "system", "content": "You are a helpful assistant." }
```

#### 2.2.4 tools 변환

```json
// Anthropic
{
  "tools": [{
    "name": "Read",
    "description": "Reads a file",
    "input_schema": {
      "type": "object",
      "properties": {
        "file_path": {"type": "string"}
      },
      "required": ["file_path"]
    }
  }]
}

// → OpenAI
{
  "tools": [{
    "type": "function",
    "function": {
      "name": "Read",
      "description": "Reads a file",
      "parameters": {
        "type": "object",
        "properties": {
          "file_path": {"type": "string"}
        },
        "required": ["file_path"]
      }
    }
  }]
}
```

#### 2.2.5 tool_choice 변환

| Anthropic | OpenAI | 설명 |
|---|---|---|
| `{"type": "auto"}` | `"auto"` | 모델이 결정 |
| `{"type": "any"}` | `"required"` | 반드시 도구 사용 |
| `{"type": "tool", "name": "X"}` | `{"type": "function", "function": {"name": "X"}}` | 특정 도구 강제 |
| 미지정 | 미지정 | 기본값 사용 |

### 2.3 응답 변환: OpenAI → Anthropic

#### 2.3.1 비스트리밍 응답

```json
// OpenAI 응답
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion",
  "model": "openai/glm-4.7",
  "choices": [{
    "index": 0,
    "message": {
      "role": "assistant",
      "content": "Hello!",
      "tool_calls": null
    },
    "finish_reason": "stop"
  }],
  "usage": {
    "prompt_tokens": 10,
    "completion_tokens": 5,
    "total_tokens": 15
  }
}

// → Anthropic 응답
{
  "id": "msg_chatcmpl-abc123",
  "type": "message",
  "role": "assistant",
  "model": "<원본 요청의 model 값>",
  "content": [
    {"type": "text", "text": "Hello!"}
  ],
  "stop_reason": "end_turn",
  "stop_sequence": null,
  "usage": {
    "input_tokens": 10,
    "output_tokens": 5,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0
  }
}
```

#### 2.3.2 응답에 tool_calls가 있는 경우

```json
// OpenAI 응답
{
  "choices": [{
    "message": {
      "role": "assistant",
      "content": "파일을 읽겠습니다.",
      "tool_calls": [{
        "id": "call_abc",
        "type": "function",
        "function": {
          "name": "Read",
          "arguments": "{\"file_path\":\"/src/main.py\"}"
        }
      }]
    },
    "finish_reason": "tool_calls"
  }]
}

// → Anthropic 응답
{
  "content": [
    {"type": "text", "text": "파일을 읽겠습니다."},
    {
      "type": "tool_use",
      "id": "call_abc",
      "name": "Read",
      "input": {"file_path": "/src/main.py"}
    }
  ],
  "stop_reason": "tool_use"
}
```

#### 2.3.3 content가 null이고 tool_calls만 있는 경우

```json
// OpenAI 응답 — content가 null
{
  "choices": [{
    "message": {
      "role": "assistant",
      "content": null,
      "tool_calls": [
        {"id": "call_abc", "type": "function", "function": {"name": "Read", "arguments": "{\"file_path\":\"/a.py\"}"}},
        {"id": "call_def", "type": "function", "function": {"name": "Grep", "arguments": "{\"pattern\":\"TODO\"}"}}
      ]
    },
    "finish_reason": "tool_calls"
  }]
}

// → Anthropic 응답 — text 블록 없이 tool_use만
{
  "content": [
    {"type": "tool_use", "id": "call_abc", "name": "Read", "input": {"file_path": "/a.py"}},
    {"type": "tool_use", "id": "call_def", "name": "Grep", "input": {"pattern": "TODO"}}
  ],
  "stop_reason": "tool_use"
}
```

#### 2.3.4 finish_reason → stop_reason 매핑

| OpenAI finish_reason | Anthropic stop_reason |
|---|---|
| `stop` | `end_turn` |
| `length` | `max_tokens` |
| `tool_calls` | `tool_use` |
| `content_filter` | `end_turn` |
| `null` / 기타 | `end_turn` |

### 2.4 스트리밍 응답 변환

#### 2.4.1 Anthropic SSE 이벤트 시퀀스

프록시가 생성해야 하는 이벤트 순서:

```
event: message_start
data: {"type":"message_start","message":{"id":"msg_...","type":"message","role":"assistant","content":[],"model":"...","stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":N,"output_tokens":0,"cache_creation_input_tokens":0,"cache_read_input_tokens":0}}}

event: content_block_start
data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}

event: ping
data: {"type":"ping"}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"토큰"}}

... (반복) ...

event: content_block_stop
data: {"type":"content_block_stop","index":0}

event: message_delta
data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":N}}

event: message_stop
data: {"type":"message_stop"}
```

#### 2.4.2 Tool Use 스트리밍

텍스트 블록 이후 tool_calls가 시작되면:

```
event: content_block_stop       (텍스트 블록 종료)
data: {"type":"content_block_stop","index":0}

event: content_block_start      (tool_use 블록 시작)
data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"call_abc","name":"Read"}}

event: content_block_delta      (tool 인자 스트리밍)
data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\"file_path\":"}}

event: content_block_delta
data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"\"/src/main.py\"}"}}

event: content_block_stop       (tool_use 블록 종료)
data: {"type":"content_block_stop","index":1}

event: message_delta
data: {"type":"message_delta","delta":{"stop_reason":"tool_use","stop_sequence":null},"usage":{"output_tokens":N}}

event: message_stop
data: {"type":"message_stop"}
```

#### 2.4.3 텍스트 없이 tool_calls만 있는 경우 (tool-only 스트리밍)

OpenAI가 `delta.content` 없이 바로 `delta.tool_calls`를 보내는 경우:
- 텍스트 블록을 시작하지 않는다
- 바로 tool_use `content_block_start`를 index 0부터 발행한다

```
event: message_start
data: {"type":"message_start","message":{...}}

event: content_block_start
data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"call_abc","name":"Read"}}

event: ping
data: {"type":"ping"}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{...}"}}

event: content_block_stop
data: {"type":"content_block_stop","index":0}

event: message_delta
data: {"type":"message_delta","delta":{"stop_reason":"tool_use","stop_sequence":null},"usage":{"output_tokens":N}}

event: message_stop
data: {"type":"message_stop"}
```

#### 2.4.4 다중 tool_calls 스트리밍

여러 도구가 한 응답에서 호출될 때, 각 tool_call은 순차적으로 블록이 열리고 닫힌다:

```
... (텍스트 블록이 있었다면 content_block_stop) ...

event: content_block_start      (첫 번째 tool, index N)
data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"call_abc","name":"Read"}}

event: content_block_delta
data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{...}"}}

event: content_block_stop
data: {"type":"content_block_stop","index":1}

event: content_block_start      (두 번째 tool, index N+1)
data: {"type":"content_block_start","index":2,"content_block":{"type":"tool_use","id":"call_def","name":"Grep"}}

event: content_block_delta
data: {"type":"content_block_delta","index":2,"delta":{"type":"input_json_delta","partial_json":"{...}"}}

event: content_block_stop
data: {"type":"content_block_stop","index":2}

event: message_delta
data: {"type":"message_delta","delta":{"stop_reason":"tool_use",...},...}

event: message_stop
data: {"type":"message_stop"}
```

> **인덱싱 규칙**: 텍스트 블록이 있었으면 index 0, 이후 tool_use 블록은 1, 2, 3... 순서. 텍스트 블록이 없었으면 tool_use가 0부터 시작.

#### 2.4.5 스트리밍 시 input_tokens 처리

`message_start` 이벤트에 `usage.input_tokens` 값이 필요하다. 이 값의 출처:

1. **스트리밍 요청 시 `stream_options: {"include_usage": true}` 옵션 추가**: OpenAI 호환 API에 이 옵션을 보내면, 마지막 청크에 `usage` 정보가 포함된다. 하지만 `message_start`는 첫 번째 이벤트이므로 이 시점에는 아직 값을 모른다.
2. **해결 방법**: `message_start`에서 `input_tokens`를 0으로 설정하고, 스트림 마지막 `message_delta`의 `usage`에 최종 토큰 수를 포함한다. 또는 비스트리밍 사전 요청 없이, vLLM이 첫 청크에 usage를 포함하면 그 값을 사용한다.
3. **실용적 접근**: `message_start`에서 `input_tokens: 0`으로 보내고, `message_delta`에서 가용한 usage 정보를 포함한다. Claude Code는 스트리밍 중간의 정확한 input_tokens 값에 의존하지 않는다.

---

## 3. 프로젝트 구조

```
proxy_server/
├── docs/
│   ├── requirements.md          # 요구사항 문서
│   ├── technical-design.md      # 기술 설계 문서 (본 문서)
│   └── implementation-guide.md  # 구현 가이드
├── src/
│   ├── __init__.py
│   ├── main.py                  # FastAPI 앱 진입점, 라우트 정의
│   ├── config.py                # 환경 변수 로드 및 설정 관리
│   ├── converter.py             # Anthropic ↔ OpenAI 변환 로직
│   ├── streaming.py             # SSE 스트리밍 변환 처리
│   └── models.py                # Pydantic 모델 정의 (요청/응답 스키마)
├── tests/
│   └── test_converter.py        # 변환 로직 단위 테스트
├── Containerfile                # Podman 빌드 파일
├── compose.yaml                 # podman-compose 설정
├── .env.example                 # 환경 변수 템플릿
├── pyproject.toml               # Python 프로젝트 메타데이터 및 의존성
└── .gitignore
```

### 3.1 모듈 역할

| 모듈 | 역할 |
|---|---|
| `main.py` | FastAPI 앱 생성, `/v1/messages` 및 `/health` 라우트 정의, 로깅 미들웨어 |
| `config.py` | `.env` 로드, 설정값 검증, 설정 객체 export |
| `converter.py` | 요청/응답 변환 함수: `anthropic_to_openai_request()`, `openai_to_anthropic_response()` |
| `streaming.py` | OpenAI SSE 청크 → Anthropic SSE 이벤트 변환 제너레이터 |
| `models.py` | Anthropic 요청/응답 Pydantic 모델, OpenAI 요청/응답 Pydantic 모델 |

---

## 4. 컨테이너 구성

### 4.1 Containerfile

```dockerfile
ARG REGISTRY=docker.io
ARG PYTHON_VERSION=3.12

FROM ${REGISTRY}/library/python:${PYTHON_VERSION}-slim

ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_TRUSTED_HOST=pypi.org

ENV PIP_INDEX_URL=${PIP_INDEX_URL}
ENV PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST}

WORKDIR /app

COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

EXPOSE 8082

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8082"]
```

- `REGISTRY`: 베이스 이미지 레지스트리 주소 (폐쇄망에서 사설 레지스트리로 변경)
- `PIP_INDEX_URL`: pip 미러 주소 (폐쇄망에서 사설 미러로 변경)
- `PIP_TRUSTED_HOST`: pip trusted host

### 4.2 compose.yaml

```yaml
services:
  proxy:
    build:
      context: .
      dockerfile: Containerfile
      args:
        REGISTRY: ${REGISTRY:-docker.io}
        PIP_INDEX_URL: ${PIP_INDEX_URL:-https://pypi.org/simple}
        PIP_TRUSTED_HOST: ${PIP_TRUSTED_HOST:-pypi.org}
    ports:
      - "${PROXY_PORT:-8082}:8082"
    env_file:
      - .env
    restart: unless-stopped
```

### 4.3 빌드 및 실행

```bash
# .env 파일 생성
cp .env.example .env
# .env 수정 후

# 빌드 및 실행 (podman-compose)
podman-compose up -d --build

# 또는 직접 빌드/실행
podman build -t anthropic-proxy .
podman run -d --name anthropic-proxy --env-file .env -p 8082:8082 anthropic-proxy

# 폐쇄망 빌드 (사설 레지스트리 사용)
podman build \
  --build-arg REGISTRY=my-registry.internal:5000 \
  --build-arg PIP_INDEX_URL=https://pip-mirror.internal/simple \
  --build-arg PIP_TRUSTED_HOST=pip-mirror.internal \
  -t anthropic-proxy .
```

---

## 5. 설정 관리

### 5.1 .env.example

```env
# === 백엔드 설정 ===
# 프록시가 요청을 전달할 vLLM 서버 주소 (프록시 서버 자체의 주소가 아님)
# 프록시와 vLLM이 같은 머신이면 localhost, 다른 머신이면 해당 IP로 변경
# 예: http://192.168.1.50:8000/v1
OPENAI_BASE_URL=http://localhost:8000/v1

# vLLM 서버 API 키 (vLLM에서 --api-key 옵션을 사용하는 경우 설정, 미사용 시 그대로 둠)
OPENAI_API_KEY=sk-placeholder

# vLLM에 배포된 모델명 (vLLM 서버의 --served-model-name 또는 모델 경로와 일치해야 함)
MODEL_NAME=openai/glm-4.7

# === 프록시 설정 ===
# 프록시 서버가 수신할 포트 (클라이언트는 이 포트로 접속)
PROXY_PORT=8082

# 로그 레벨 (DEBUG, INFO, WARNING, ERROR)
LOG_LEVEL=INFO

# === 빌드 설정 (폐쇄망용, 필요 시 주석 해제) ===
# 컨테이너 베이스 이미지를 가져올 레지스트리 (외부 접근 불가 시 사설 레지스트리로 변경)
# REGISTRY=my-registry.internal:5000

# Python 패키지를 가져올 pip 미러 주소 (외부 접근 불가 시 사설 미러로 변경)
# PIP_INDEX_URL=https://pip-mirror.internal/simple
# PIP_TRUSTED_HOST=pip-mirror.internal
```

### 5.2 설정 로드 (config.py)

```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    openai_base_url: str = "http://localhost:8000/v1"
    openai_api_key: str = "sk-placeholder"
    model_name: str = "openai/glm-4.7"
    proxy_port: int = 8082
    log_level: str = "INFO"

    class Config:
        env_file = ".env"
```

---

## 6. 에러 핸들링

### 6.1 에러 응답 형식

모든 에러는 Anthropic API 에러 형식으로 반환한다:

```json
{
  "type": "error",
  "error": {
    "type": "api_error",
    "message": "에러 상세 메시지"
  }
}
```

### 6.2 에러 매핑

| 상황 | HTTP 상태 | error.type | 설명 |
|---|---|---|---|
| 잘못된 요청 형식 | 400 | `invalid_request_error` | 요청 파싱 실패 |
| 백엔드 인증 실패 | 401 | `authentication_error` | API 키 오류 |
| 백엔드 연결 실패 | 502 | `api_error` | vLLM 서버 연결 불가 |
| 백엔드 타임아웃 | 504 | `api_error` | vLLM 응답 타임아웃 |
| 백엔드 에러 응답 (4xx/5xx) | 502 | `api_error` | vLLM이 에러 응답을 반환. 에러 메시지에 백엔드 상태 코드와 응답 본문을 포함한다 |
| 스트리밍 중 백엔드 에러/연결 끊김 | - | - | 아래 6.3절 참조 |
| 프록시 내부 오류 | 500 | `api_error` | 변환 중 예외 발생 |
| 지원하지 않는 경로 | 404 | `not_found_error` | 없는 엔드포인트 |

### 6.3 스트리밍 중 에러 처리

스트리밍 응답은 이미 HTTP 200으로 시작된 상태이므로, 도중에 에러가 발생하면 HTTP 상태 코드를 변경할 수 없다. 다음과 같이 처리한다:

**백엔드 연결 실패 (스트리밍 시작 전):**
- 아직 SSE 응답이 시작되지 않은 상태이므로, 일반 에러 응답(502)을 반환한다.

**스트리밍 도중 백엔드 연결 끊김 또는 에러:**
- 열린 content block이 있으면 `content_block_stop` 발행
- 에러 정보를 담은 `message_delta`를 발행: `stop_reason`을 `"end_turn"`으로 설정
- `message_stop` 발행하여 스트림을 정상 종료
- 에러 상세를 로그에 기록

```
event: content_block_stop
data: {"type":"content_block_stop","index":0}

event: message_delta
data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":0}}

event: message_stop
data: {"type":"message_stop"}
```

> **이유**: 스트림을 갑자기 끊으면 클라이언트가 불완전한 응답을 파싱하지 못할 수 있다. 정상적인 종료 시퀀스를 보내야 클라이언트가 안정적으로 처리할 수 있다.
