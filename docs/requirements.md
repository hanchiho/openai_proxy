# 요구사항 문서: Anthropic-to-OpenAI Proxy Server

## 1. 프로젝트 개요

### 1.1 목적
Anthropic Messages API 형식의 요청을 OpenAI Chat Completions API 호환 형식으로 변환하는 프록시 서버를 구축한다.

### 1.2 사용 시나리오
```
Claude Code ──(Anthropic API)──> 프록시 서버 ──(OpenAI API)──> 로컬 vLLM 서버
```

- Claude Code의 `ANTHROPIC_BASE_URL`을 프록시 서버 주소로 설정
- 프록시 서버가 Anthropic 형식 요청을 OpenAI 호환 형식으로 변환
- 변환된 요청을 로컬 vLLM 서버로 전달
- vLLM 응답을 다시 Anthropic 형식으로 변환하여 Claude Code에 반환

### 1.3 배경
- 로컬 vLLM 서버에서 구동 중인 LLM(예: `openai/glm-4.7`)을 Claude Code 인터페이스를 통해 사용하기 위함
- 폐쇄망/제한 네트워크 환경에서 운용 가능해야 함

---

## 2. 목표 및 비목표

### 2.1 목표
- Anthropic Messages API(`/v1/messages`) 요청을 OpenAI Chat Completions API(`/v1/chat/completions`) 형식으로 정확히 변환
- SSE 스트리밍 응답 지원
- Tool Use(Anthropic) ↔ Function Calling(OpenAI) 양방향 변환
- 멀티모달(이미지) 입력 변환
- `.env` 파일을 통한 유연한 설정 관리
- Podman 컨테이너로 간편 배포
- 폐쇄망 환경 지원 (pip 미러, 컨테이너 registry 주소 변경 가능)

### 2.2 비목표
- 멀티 모델 라우팅 (단일 모델만 사용)
- 멀티 프로바이더 지원 (OpenAI 호환 백엔드만 대상)
- 사용자 인증/권한 관리 (내부 사용 목적)
- 로드 밸런싱, 레이트 리밋
- 웹 UI 또는 관리 대시보드

---

## 3. 기능 요구사항

### 3.1 API 변환 (핵심)

#### FR-01: Messages API 엔드포인트
- `POST /v1/messages` 엔드포인트를 노출한다
- Anthropic Messages API 요청 형식을 수신한다
- Claude Code가 보내는 `x-api-key`, `anthropic-version` 등의 헤더를 수신하되, 프록시에서는 무시한다 (검증하지 않고 통과)
- OpenAI Chat Completions API 형식으로 변환하여 백엔드에 전달한다
- 백엔드 응답을 Anthropic Messages API 형식으로 변환하여 반환한다

#### FR-02: 스트리밍 (SSE)
- `"stream": true` 요청 시 Server-Sent Events 스트리밍을 지원한다
- Anthropic 스트리밍 이벤트 시퀀스를 준수한다:
  1. `message_start`
  2. `content_block_start`
  3. `ping`
  4. `content_block_delta` (text_delta, input_json_delta)
  5. `content_block_stop`
  6. `message_delta` (stop_reason, usage)
  7. `message_stop`
- OpenAI 스트리밍 청크를 위 이벤트들로 변환한다

#### FR-03: Tool Use ↔ Function Calling 변환
- **요청 변환 (Anthropic → OpenAI)**:
  - `tools[].name` → `tools[].function.name`
  - `tools[].description` → `tools[].function.description`
  - `tools[].input_schema` → `tools[].function.parameters`
  - `tool_choice` 매핑: `auto` → `auto`, `any` → `required`, `{name}` → `{function: {name}}`
- **응답 변환 (OpenAI → Anthropic)**:
  - `tool_calls[].function.name` → `content[].name` (type: tool_use)
  - `tool_calls[].function.arguments` (JSON string) → `content[].input` (object)
  - `tool_calls[].id` → `content[].id`
  - `message.content`가 null이고 `tool_calls`만 있는 경우: text 블록 없이 tool_use 블록만 생성
  - 다중 tool_calls: 각각을 별도의 tool_use content 블록으로 변환
- **tool_result 메시지 변환**:
  - Anthropic `tool_result` role 메시지를 OpenAI `tool` role 메시지로 변환
  - `tool_result`의 content가 배열(이미지 포함)인 경우: 텍스트 부분만 추출하여 문자열로 결합 (OpenAI tool role은 문자열 content만 지원)

#### FR-04: 멀티모달 (이미지) 지원
- Anthropic 이미지 블록(`type: image`, `source.type: base64`)을 OpenAI 이미지 URL 형식(`type: image_url`, `url: data:...`)으로 변환
- base64 인코딩된 이미지 데이터를 data URI로 변환

#### FR-05: 메시지 구조 변환
- **요청 필드 매핑**:
  - `model` → `.env`에 설정된 모델명으로 교체
  - `max_tokens` → `max_completion_tokens`
  - `stop_sequences` → `stop`
  - `temperature` → `temperature` (그대로)
  - `top_p` → `top_p` (그대로)
  - `system` (string 또는 array) → `messages[0]`에 system role로 삽입
- **혼합 콘텐츠 메시지 처리**:
  - 하나의 user 메시지에 text 블록과 tool_result 블록이 함께 있는 경우: text 블록은 별도의 user 메시지로, 각 tool_result 블록은 별도의 tool role 메시지로 분리하여 순서를 유지한다
- **응답 필드 매핑**:
  - `choices[0].finish_reason` → `stop_reason` (stop→end_turn, length→max_tokens, tool_calls→tool_use)
  - `usage.prompt_tokens` → `usage.input_tokens`
  - `usage.completion_tokens` → `usage.output_tokens`

#### FR-06: 비스트리밍 응답
- `"stream": false` 또는 stream 미지정 시 일반 JSON 응답을 반환한다

### 3.2 설정 관리

#### FR-07: 환경 변수 기반 설정
- `.env` 파일을 통해 다음 항목을 설정 가능하게 한다:
  - `OPENAI_BASE_URL`: vLLM 서버 주소 (예: `http://192.168.1.100:8000/v1`)
  - `OPENAI_API_KEY`: 백엔드 API 키
  - `MODEL_NAME`: 사용할 모델명 (예: `openai/glm-4.7`)
  - `PROXY_PORT`: 프록시 서버 포트 (기본값: 8082)
- `.env.example` 파일을 제공하여 설정 항목을 문서화한다

### 3.3 헬스체크

#### FR-08: 헬스체크 엔드포인트
- `GET /health` 엔드포인트를 제공한다
- 프록시 서버 상태를 확인할 수 있다

---

## 4. 비기능 요구사항

### NFR-01: 배포
- Podman(또는 podman-compose)으로 컨테이너 빌드 및 실행 가능해야 한다
- 대상 OS: Ubuntu 24.04

### NFR-02: 폐쇄망 호환
- Containerfile(Dockerfile)에서 베이스 이미지 registry 주소를 빌드 인자(`ARG`)로 변경 가능해야 한다
- pip/uv index URL을 빌드 인자로 변경 가능해야 한다

### NFR-03: 로깅
- 요청/응답의 기본 정보(메서드, 경로, 상태 코드, 처리 시간)를 로깅한다
- 에러 발생 시 상세 로그를 남긴다

### NFR-04: 에러 핸들링
- 백엔드 연결 실패, 타임아웃 등 에러 시 Anthropic API 에러 형식으로 응답한다
- 적절한 HTTP 상태 코드를 반환한다

---

## 5. 제약사항

| 항목 | 내용 |
|---|---|
| 런타임 환경 | Ubuntu 24.04, Podman |
| 언어/프레임워크 | Python, FastAPI |
| 백엔드 | OpenAI 호환 API (vLLM) |
| 네트워크 | 폐쇄망 가능 — 외부 registry 접근 불가할 수 있음 |
| 모델 | 단일 모델 (`.env`로 설정) |
| 인증 | 내부 사용 — 별도 인증 체계 불필요 |
