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

## 사전 요구사항

### 컨테이너 배포 (권장)

| 항목 | 설명 |
|------|------|
| **Podman** | 컨테이너 런타임 (`run.sh` 및 이미지 빌드에 사용) |
| **podman-compose** | `compose.yaml`로 실행할 경우 필요 (선택) |
| **bash** | `run.sh` 실행에 필요 |
| **curl** | `run.sh status` 헬스체크에 사용 (선택) |

> 컨테이너 내부에서 Python 3.12 및 모든 패키지가 자동 설치되므로, 호스트에 Python을 직접 설치할 필요가 없습니다.

### 로컬 개발 (컨테이너 없이 직접 실행)

| 항목 | 설명 |
|------|------|
| **Python 3.12+** | 런타임 |
| **pip** | 패키지 설치 |

Python 패키지 의존성 (`pyproject.toml`에 정의):

| 패키지 | 용도 |
|--------|------|
| `fastapi>=0.115.0` | HTTP API 프레임워크 |
| `uvicorn>=0.34.0` | ASGI 서버 |
| `pydantic>=2.0.0` | 요청/응답 모델 검증 |
| `pydantic-settings>=2.0.0` | `.env` 기반 설정 관리 |
| `httpx>=0.25.0` | 비동기 HTTP 클라이언트 (vLLM 백엔드 통신) |
| `python-dotenv>=1.0.0` | `.env` 파일 로드 |

로컬 설치 및 실행:

```bash
pip install .
uvicorn src.main:app --host 0.0.0.0 --port 8082
```

### 테스트

```bash
pip install pytest
pytest
```

### 백엔드 요구사항

- OpenAI Chat Completions API 호환 서버 (예: [vLLM](https://docs.vllm.ai/))가 별도로 실행 중이어야 합니다.

## 빠른 시작

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

운영 스크립트(`run.sh`)를 사용하면 빌드부터 실행까지 간편하게 관리할 수 있습니다:

```bash
chmod +x run.sh

./run.sh build     # 이미지 빌드
./run.sh start     # 컨테이너 시작 (이미지 없으면 자동 빌드)
./run.sh stop      # 컨테이너 중지 및 제거
./run.sh restart   # 재시작
./run.sh logs      # 로그 실시간 출력
./run.sh status    # 상태 및 헬스체크 확인
```

> `.env`에 `REGISTRY`, `PIP_INDEX_URL`, `PIP_TRUSTED_HOST`를 설정하면 폐쇄망 빌드가 자동으로 적용됩니다.

또는 podman-compose를 직접 사용할 수도 있습니다:

```bash
podman-compose up -d --build
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
