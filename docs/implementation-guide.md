# 구현 가이드: Anthropic-to-OpenAI Proxy Server

> 이 문서는 Claude Code가 구현 시 참조할 가이드입니다. 아래 순서대로 구현하세요.

## 1. 구현 순서

```
Step 1: 프로젝트 초기화 (pyproject.toml, .env.example, .gitignore)
Step 2: 설정 모듈 (src/config.py)
Step 3: Pydantic 모델 정의 (src/models.py)
Step 4: 요청/응답 변환 로직 (src/converter.py)
Step 5: 스트리밍 변환 로직 (src/streaming.py)
Step 6: FastAPI 앱 및 라우트 (src/main.py)
Step 7: 컨테이너 구성 (Containerfile, compose.yaml)
Step 8: 테스트 (tests/test_converter.py)
```

---

## 2. Step 1: 프로젝트 초기화

### pyproject.toml

```toml
[project]
name = "anthropic-openai-proxy"
version = "0.1.0"
description = "Proxy server that converts Anthropic API to OpenAI-compatible format"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115.0",
    "uvicorn>=0.34.0",
    "pydantic>=2.0.0",
    "pydantic-settings>=2.0.0",
    "httpx>=0.25.0",
    "python-dotenv>=1.0.0",
]

[build-system]
requires = ["setuptools>=75.0"]
build-backend = "setuptools.backends._legacy:_Backend"
```

### .env.example

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
```

### .gitignore

```
__pycache__/
*.pyc
.env
.venv/
*.egg-info/
```

---

## 3. Step 2: 설정 모듈 — `src/config.py`

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


settings = Settings()
```

- `.env` 파일에서 자동 로드
- 각 필드에 기본값 설정
- `settings` 싱글톤 객체로 export

---

## 4. Step 3: Pydantic 모델 — `src/models.py`

Anthropic API 요청/응답 스키마를 정의한다. 모든 필드가 필수는 아니므로 `Optional`을 적절히 사용한다.

### 구현할 모델 목록

**요청 관련:**
- `AnthropicRequest`: `/v1/messages` 요청 본문
  - `model`: str
  - `max_tokens`: int
  - `messages`: list[AnthropicMessage]
  - `system`: Optional[str | list] — string 또는 content block 배열
  - `stream`: Optional[bool] = False
  - `temperature`: Optional[float]
  - `top_p`: Optional[float]
  - `top_k`: Optional[int]
  - `stop_sequences`: Optional[list[str]]
  - `tools`: Optional[list[AnthropicTool]]
  - `tool_choice`: Optional[dict]
  - `metadata`: Optional[dict]

- `AnthropicMessage`: 메시지 객체
  - `role`: str
  - `content`: str | list[ContentBlock]

- `ContentBlock`: union type
  - `TextBlock`: `{"type": "text", "text": "..."}`
  - `ImageBlock`: `{"type": "image", "source": {"type": "base64", "media_type": "...", "data": "..."}}`
  - `ToolUseBlock`: `{"type": "tool_use", "id": "...", "name": "...", "input": {}}`
  - `ToolResultBlock`: `{"type": "tool_result", "tool_use_id": "...", "content": ...}` — content는 string 또는 content block 배열 (이미지 포함 가능)

- `AnthropicTool`: 도구 정의
  - `name`: str
  - `description`: Optional[str]
  - `input_schema`: dict

**응답 관련:**
- `AnthropicResponse`: `/v1/messages` 응답 본문
  - `id`: str
  - `type`: "message"
  - `role`: "assistant"
  - `model`: str
  - `content`: list[ContentBlock]
  - `stop_reason`: str
  - `stop_sequence`: Optional[str]
  - `usage`: UsageInfo

- `UsageInfo`:
  - `input_tokens`: int
  - `output_tokens`: int
  - `cache_creation_input_tokens`: int = 0
  - `cache_read_input_tokens`: int = 0

> **참고**: Pydantic 모델은 검증 및 직렬화에 사용한다. 변환 로직 자체는 `converter.py`에 구현한다.

---

## 5. Step 4: 변환 로직 — `src/converter.py`

두 개의 핵심 함수를 구현한다.

### 5.1 `convert_request(anthropic_req: AnthropicRequest) -> dict`

Anthropic 요청 → OpenAI 요청 dict 변환.

**처리 순서:**

1. **system 메시지 처리**
   - `anthropic_req.system`이 string이면 그대로 사용
   - list이면 text type 블록들의 text를 합침
   - `{"role": "system", "content": text}`를 messages 맨 앞에 삽입

2. **messages 변환** — 각 메시지를 순회하며:
   - `content`가 string이면 그대로 `{"role": role, "content": content}`
   - `content`가 list이면 각 블록을 검사:
     - `text` 블록 → OpenAI text 형식
     - `image` 블록 → `{"type": "image_url", "image_url": {"url": "data:{media_type};base64,{data}"}}`
     - `tool_use` 블록 → assistant 메시지의 `tool_calls`에 추가 (여러 개일 수 있음)
     - `tool_result` 블록 → 별도 `{"role": "tool", "tool_call_id": id, "content": content}` 메시지로 분리
       - `tool_result`의 content가 배열(이미지 포함)인 경우: text 블록들의 text만 추출하여 문자열로 결합
   - **혼합 메시지 처리** (하나의 user 메시지에 text + tool_result가 같이 있는 경우):
     - 블록 순서를 유지하며 각각 별도 메시지로 분리
     - text 블록 → `{"role": "user", "content": text}`
     - tool_result 블록 → `{"role": "tool", "tool_call_id": id, "content": content}`

3. **tools 변환**
   - `input_schema` → `parameters`로 키 변경
   - `{"type": "function", "function": {...}}` 형태로 래핑

4. **tool_choice 변환**
   - `auto` → `"auto"`, `any` → `"required"`, `tool` → `{"type": "function", "function": {"name": name}}`

5. **나머지 필드 매핑**
   - `model` → `settings.model_name`으로 교체
   - `max_tokens` → `max_completion_tokens`
   - `stop_sequences` → `stop`
   - `temperature`, `top_p` → 그대로
   - `top_k` → 무시

**반환**: OpenAI Chat Completions API 요청 dict

### 5.2 `convert_response(openai_resp: dict, original_model: str) -> dict`

OpenAI 응답 → Anthropic 응답 dict 변환.

**처리 순서:**

1. `choices[0].message`에서 content와 tool_calls 추출
2. **content 블록 생성**:
   - `message.content`가 있고 null이 아니면 `{"type": "text", "text": content}` 추가
   - `message.content`가 null이면 text 블록을 생성하지 않음
   - `message.tool_calls`가 있으면 각각을 `{"type": "tool_use", "id": id, "name": name, "input": json.loads(arguments)}` 추가 (다중 tool_calls 지원)
3. **stop_reason 변환**: finish_reason을 매핑
4. **usage 변환**: `prompt_tokens` → `input_tokens`, `completion_tokens` → `output_tokens`
5. **id 생성**: `"msg_"` 접두어 추가
6. **model**: 원본 요청의 model 값 사용 (Claude Code가 기대하는 모델명 유지)

**반환**: Anthropic Messages API 응답 dict

---

## 6. Step 5: 스트리밍 변환 — `src/streaming.py`

### 6.1 `stream_response(openai_stream, original_model: str) -> AsyncGenerator`

OpenAI SSE 스트림을 Anthropic SSE 이벤트로 변환하는 async generator.

**상태 관리:**
```python
block_index = 0              # 현재 content block 인덱스 (텍스트, tool 모두 포함)
tool_calls = {}              # 진행 중인 tool_call 데이터 누적 {tool_index: {id, name, arguments}}
text_block_started = False   # 텍스트 블록이 시작되었는지
first_chunk = True           # 첫 청크 여부
output_tokens = 0            # 누적 출력 토큰 수
```

**처리 흐름:**

1. **첫 청크 수신 시:**
   - `message_start` 이벤트 발행 (id, model, `input_tokens: 0`으로 설정 — 정확한 값은 이 시점에 알 수 없음)
   - `ping` 이벤트 발행
   - 이후 첫 delta 내용에 따라 텍스트 블록 또는 tool_use 블록을 시작

2. **텍스트 청크 수신 시** (`delta.content`가 있는 경우):
   - 텍스트 블록이 아직 시작되지 않았으면 `content_block_start` 발행 (type: text, index: block_index)
   - `content_block_delta` 이벤트 발행 (type: text_delta)

3. **tool_calls 청크 수신 시** (`delta.tool_calls`가 있는 경우):
   - 해당 tool_call이 처음 등장하면 (id와 name이 포함된 청크):
     - 진행 중인 텍스트 블록이 있으면 `content_block_stop` 발행하고 block_index 증가
     - 이전 tool_call 블록이 열려있으면 `content_block_stop` 발행하고 block_index 증가
     - `content_block_start` 발행 (type: tool_use, id, name, index: block_index)
   - arguments 청크가 있으면:
     - `content_block_delta` 발행 (type: input_json_delta)

4. **스트림 종료 시** (`[DONE]` 또는 `finish_reason` 수신):
   - 마지막 활성 블록의 `content_block_stop` 발행
   - `message_delta` 발행 (stop_reason, `output_tokens`는 vLLM 마지막 청크의 usage에서 추출, 없으면 0)
   - `message_stop` 발행

5. **스트리밍 중 에러 발생 시** (백엔드 연결 끊김, 파싱 에러 등):
   - 스트리밍 시작 전이면: 일반 에러 응답(502) 반환
   - 스트리밍 도중이면: 열린 블록을 `content_block_stop`으로 닫고, `message_delta`(stop_reason: "end_turn") + `message_stop`으로 정상 종료 시퀀스를 보낸 뒤 스트림 종료. 에러 상세는 로그에 기록

**OpenAI 백엔드에 스트리밍 요청 시 `stream_options` 추가:**
```python
# 요청에 추가하여 마지막 청크에 usage 정보를 받는다
openai_request["stream_options"] = {"include_usage": True}
```

### 6.2 SSE 이벤트 포맷 헬퍼

```python
def format_sse(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
```

---

## 7. Step 6: FastAPI 앱 — `src/main.py`

### 7.1 앱 구성

```python
import logging
import time
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="Anthropic-to-OpenAI Proxy")
```

### 7.2 로깅 미들웨어

모든 요청에 대해 메서드, 경로, 상태 코드, 처리 시간을 로깅한다.

### 7.3 라우트

**`POST /v1/messages`**:
1. 요청 본문을 `AnthropicRequest`로 파싱 (`x-api-key`, `anthropic-version` 등의 헤더는 무시)
2. `convert_request()`로 OpenAI 형식 변환
3. `stream` 여부에 따라 분기:
   - **비스트리밍**: `httpx.AsyncClient`로 백엔드에 POST → 백엔드 응답이 에러(4xx/5xx)이면 Anthropic 에러 형식으로 변환하여 반환 → 성공이면 `convert_response()`로 응답 변환 → JSONResponse 반환
   - **스트리밍**: 요청에 `stream_options: {"include_usage": true}` 추가 → `httpx.AsyncClient`로 백엔드에 stream POST → `stream_response()`로 SSE 변환 → StreamingResponse 반환
4. 에러 발생 시 Anthropic 에러 형식으로 반환 (백엔드 에러 시 상태 코드와 응답 본문을 에러 메시지에 포함)

**`GET /health`**:
- `{"status": "ok"}` 반환

### 7.4 httpx 클라이언트

```python
# 앱 시작 시 생성, 종료 시 close
@app.on_event("startup")
async def startup():
    app.state.client = httpx.AsyncClient(
        base_url=settings.openai_base_url,
        headers={"Authorization": f"Bearer {settings.openai_api_key}"},
        timeout=httpx.Timeout(300.0, connect=10.0)  # 생성 응답은 오래 걸릴 수 있음
    )

@app.on_event("shutdown")
async def shutdown():
    await app.state.client.aclose()
```

> **주의**: httpx 클라이언트는 앱 수명 주기에 맞춰 관리한다. 요청마다 생성하지 않는다.

---

## 8. Step 7: 컨테이너 구성

### Containerfile

`docs/technical-design.md` 4.1절 참조. 그대로 구현한다.

### compose.yaml

`docs/technical-design.md` 4.2절 참조. 그대로 구현한다.

---

## 9. Step 8: 테스트

### 9.1 단위 테스트 (`tests/test_converter.py`)

다음 케이스를 테스트한다:

1. **기본 텍스트 메시지 변환** — 단순 문자열 content
2. **system 메시지 변환** — string 형식, array 형식
3. **tool_use 블록 변환** — assistant 메시지의 tool_use → tool_calls
4. **tool_result 변환** — user 메시지의 tool_result → tool role 메시지 분리
5. **이미지 변환** — base64 이미지 → data URI
6. **tools 정의 변환** — input_schema → parameters
7. **tool_choice 변환** — auto, any, tool
8. **응답 변환** — 텍스트 응답, tool_calls 응답
9. **stop_reason 매핑** — stop, length, tool_calls
10. **복합 메시지** — 텍스트 + tool_use + tool_result가 섞인 대화
11. **혼합 user 메시지** — text + tool_result가 같은 user 메시지에 있는 경우 분리
12. **content null 응답** — content가 null이고 tool_calls만 있는 OpenAI 응답
13. **다중 tool_calls 응답** — 여러 tool_calls가 있는 응답 변환
14. **배열 content tool_result** — tool_result의 content가 이미지 포함 배열인 경우 텍스트만 추출

### 9.2 수동 통합 테스트

프록시 서버를 띄운 후 curl로 테스트:

```bash
# 비스트리밍 테스트
curl -X POST http://localhost:8082/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: test" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "claude-sonnet-4-20250514",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "Hello"}]
  }'

# 스트리밍 테스트
curl -X POST http://localhost:8082/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: test" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "claude-sonnet-4-20250514",
    "max_tokens": 1024,
    "stream": true,
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```

---

## 10. Claude Code 연결 방법

구현 및 배포 완료 후, Claude Code에서 사용하려면:

```bash
# 환경 변수 설정
export ANTHROPIC_BASE_URL=http://<프록시서버IP>:8082

# Claude Code 실행
claude
```

또는 Claude Code 설정 파일에서 `ANTHROPIC_BASE_URL`을 지정한다.
