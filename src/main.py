import logging
import time
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .config import settings
from .converter import convert_request, convert_response
from .models import AnthropicRequest
from .streaming import stream_response

# 로깅 설정
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.client = httpx.AsyncClient(
        base_url=settings.openai_base_url,
        headers={"Authorization": f"Bearer {settings.openai_api_key}"},
        timeout=httpx.Timeout(300.0, connect=10.0),
    )
    logger.info(
        "Proxy started — backend: %s, model: %s",
        settings.openai_base_url,
        settings.model_name,
    )
    yield
    await app.state.client.aclose()


app = FastAPI(title="Anthropic-to-OpenAI Proxy", lifespan=lifespan)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    elapsed = time.time() - start
    logger.info(
        "%s %s → %d (%.3fs)",
        request.method,
        request.url.path,
        response.status_code,
        elapsed,
    )
    return response


def _error_response(status_code: int, error_type: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "type": "error",
            "error": {"type": error_type, "message": message},
        },
    )


@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    return _error_response(404, "not_found_error", f"Not found: {request.url.path}")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/v1/messages")
async def messages(request: Request):
    # 요청 파싱
    try:
        body = await request.json()
        anthropic_req = AnthropicRequest(**body)
    except Exception as e:
        logger.warning("Invalid request: %s", e)
        return _error_response(400, "invalid_request_error", str(e))

    original_model = anthropic_req.model

    # Anthropic → OpenAI 변환
    try:
        openai_req = convert_request(anthropic_req)
    except Exception as e:
        logger.error("Request conversion error: %s", e, exc_info=True)
        return _error_response(500, "api_error", f"Request conversion failed: {e}")

    client: httpx.AsyncClient = app.state.client
    message_id = f"msg_{uuid.uuid4().hex[:24]}"

    if anthropic_req.stream:
        # 스트리밍
        openai_req["stream_options"] = {"include_usage": True}
        try:
            backend_resp = await client.send(
                client.build_request(
                    "POST",
                    "/chat/completions",
                    json=openai_req,
                    headers={"Content-Type": "application/json"},
                ),
                stream=True,
            )
        except httpx.ConnectError as e:
            logger.error("Backend connection failed: %s", e)
            return _error_response(502, "api_error", f"Backend connection failed: {e}")
        except httpx.TimeoutException as e:
            logger.error("Backend timeout: %s", e)
            return _error_response(504, "api_error", f"Backend timeout: {e}")

        if backend_resp.status_code != 200:
            body_text = await backend_resp.aread()
            await backend_resp.aclose()
            logger.error(
                "Backend error: %d %s", backend_resp.status_code, body_text.decode()
            )
            return _error_response(
                502,
                "api_error",
                f"Backend returned {backend_resp.status_code}: {body_text.decode()}",
            )

        async def sse_generator():
            try:
                async for chunk in stream_response(
                    backend_resp.aiter_lines(), original_model, message_id
                ):
                    yield chunk
            finally:
                await backend_resp.aclose()

        return StreamingResponse(
            sse_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
    else:
        # 비스트리밍
        try:
            backend_resp = await client.post(
                "/chat/completions",
                json=openai_req,
                headers={"Content-Type": "application/json"},
            )
        except httpx.ConnectError as e:
            logger.error("Backend connection failed: %s", e)
            return _error_response(502, "api_error", f"Backend connection failed: {e}")
        except httpx.TimeoutException as e:
            logger.error("Backend timeout: %s", e)
            return _error_response(504, "api_error", f"Backend timeout: {e}")

        if backend_resp.status_code != 200:
            logger.error(
                "Backend error: %d %s", backend_resp.status_code, backend_resp.text
            )
            return _error_response(
                502,
                "api_error",
                f"Backend returned {backend_resp.status_code}: {backend_resp.text}",
            )

        try:
            openai_resp = backend_resp.json()
            anthropic_resp = convert_response(openai_resp, original_model)
            anthropic_resp["id"] = message_id
            return JSONResponse(content=anthropic_resp)
        except Exception as e:
            logger.error("Response conversion error: %s", e, exc_info=True)
            return _error_response(
                500, "api_error", f"Response conversion failed: {e}"
            )
