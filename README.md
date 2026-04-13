# Anthropic-to-OpenAI Proxy Server

Anthropic Messages API 요청을 OpenAI Chat Completions API 호환 형식으로 변환하는 프록시 서버입니다.

Claude Code에서 `ANTHROPIC_BASE_URL`을 지정하여 로컬 vLLM 서버에 연결할 수 있습니다.

## 아키텍처

```
┌─────────────┐       ┌──────────────────┐       ┌─────────────────┐
│ Claude Code  │──────>│   Proxy Server   │──────>│  vLLM Server    │
│ (Anthropic   │ HTTP  │  (FastAPI)       │ HTTP  │  (OpenAI compat)│
│  API format) │<──────│                  │<──────│                 │
└─────────────┘       │  - 요청 변환      │       └─────────────────┘
                      │  - 응답 변환      │
                      │  - 스트리밍 처리   │
                      └──────────────────┘
```

## 주요 기능

- **메시지 변환** — Anthropic ↔ OpenAI 요청/응답 양방향 변환
- **SSE 스트리밍** — OpenAI 스트리밍 청크를 Anthropic 이벤트 시퀀스로 실시간 변환
- **Tool Use ↔ Function Calling** — 도구 정의, 호출, 결과 양방향 변환
- **멀티모달** — base64 이미지를 data URI로 변환
- **에러 핸들링** — Anthropic API 에러 형식으로 일관된 에러 응답
- **폐쇄망 지원** — 사설 레지스트리/pip 미러 주소 변경 가능

## 빠른 시작

### 요구사항

- Python 3.12+
- Podman (또는 podman-compose)

### 1. 설정

```bash
cp .env.example .env
```

`.env` 파일을 열어 환경에 맞게 수정합니다:

```env
# vLLM 서버 주소
OPENAI_BASE_URL=http://192.168.1.100:8000/v1

# vLLM API 키 (미사용 시 그대로)
OPENAI_API_KEY=sk-placeholder

# vLLM에 배포된 모델명
MODEL_NAME=openai/glm-4.7

# 프록시 서버 포트
PROXY_PORT=8082
```

### 2. 빌드 및 실행

```bash
# podman-compose 사용
podman-compose up -d --build

# 또는 직접 빌드/실행
podman build -f Containerfile -t anthropic-proxy .
podman run -d --name anthropic-proxy --env-file .env -p ${PROXY_PORT:-8082}:8082 anthropic-proxy
```

#### 폐쇄망 빌드

외부 레지스트리에 접근할 수 없는 환경에서는 빌드 인자를 지정합니다:

```bash
podman build \
  --build-arg REGISTRY=my-registry.internal:5000 \
  --build-arg PIP_INDEX_URL=https://pip-mirror.internal/simple \
  --build-arg PIP_TRUSTED_HOST=pip-mirror.internal \
  -t anthropic-proxy .
```

### 3. 동작 확인

```bash
# 헬스체크
curl http://localhost:8082/health

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
```

## Claude Code 연결

```bash
export ANTHROPIC_BASE_URL=http://<프록시서버IP>:8082
claude
```

또는 Claude Code 설정에서 `ANTHROPIC_BASE_URL`을 지정합니다.

## 설정 항목

| 변수 | 설명 | 기본값 |
|------|------|--------|
| `OPENAI_BASE_URL` | vLLM 서버 주소 | `http://localhost:8000/v1` |
| `OPENAI_API_KEY` | vLLM API 키 | `sk-placeholder` |
| `MODEL_NAME` | 사용할 모델명 | `openai/glm-4.7` |
| `PROXY_PORT` | 프록시 서버 포트 | `8082` |
| `LOG_LEVEL` | 로그 레벨 (DEBUG/INFO/WARNING/ERROR) | `INFO` |

## 라이선스

MIT
