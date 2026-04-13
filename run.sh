#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTAINER_NAME="anthropic-proxy"
IMAGE_NAME="anthropic-proxy"
INTERNAL_PORT=8082

# .env 로드
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    source "$SCRIPT_DIR/.env"
    set +a
else
    echo "[ERROR] .env 파일이 없습니다."
    echo "  cp .env.example .env 로 생성한 뒤 환경에 맞게 수정하세요."
    exit 1
fi

PROXY_PORT="${PROXY_PORT:-8082}"

usage() {
    cat <<EOF
사용법: ./run.sh <command>

Commands:
  build     컨테이너 이미지 빌드
  start     컨테이너 시작 (이미지 없으면 자동 빌드)
  stop      컨테이너 중지 및 제거
  restart   컨테이너 재시작
  logs      컨테이너 로그 출력 (실시간)
  status    컨테이너 상태 및 헬스체크 확인
  help      이 도움말 출력
EOF
}

do_build() {
    echo "[BUILD] 이미지 빌드 시작..."
    local build_args=()

    # 폐쇄망 빌드 인자 자동 적용
    [ -n "${REGISTRY:-}" ] && build_args+=(--build-arg "REGISTRY=$REGISTRY")
    [ -n "${PIP_INDEX_URL:-}" ] && build_args+=(--build-arg "PIP_INDEX_URL=$PIP_INDEX_URL")
    [ -n "${PIP_TRUSTED_HOST:-}" ] && build_args+=(--build-arg "PIP_TRUSTED_HOST=$PIP_TRUSTED_HOST")

    podman build -f "$SCRIPT_DIR/Containerfile" "${build_args[@]}" -t "$IMAGE_NAME" "$SCRIPT_DIR"
    echo "[BUILD] 완료: $IMAGE_NAME"
}

do_start() {
    # 이미 실행 중이면 안내
    if podman ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        echo "[START] $CONTAINER_NAME 이 이미 실행 중입니다."
        return 0
    fi

    # 중지된 컨테이너가 있으면 제거
    if podman ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        podman rm "$CONTAINER_NAME" >/dev/null 2>&1
    fi

    # 이미지 없으면 빌드
    if ! podman image exists "$IMAGE_NAME"; then
        echo "[START] 이미지가 없습니다. 빌드를 먼저 실행합니다."
        do_build
    fi

    echo "[START] 컨테이너 시작 (포트: $PROXY_PORT)..."
    podman run -d \
        --name "$CONTAINER_NAME" \
        --env-file "$SCRIPT_DIR/.env" \
        -p "${PROXY_PORT}:${INTERNAL_PORT}" \
        --restart unless-stopped \
        "$IMAGE_NAME"
    echo "[START] 완료"
}

do_stop() {
    echo "[STOP] 컨테이너 중지..."
    if podman ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        podman stop "$CONTAINER_NAME" 2>/dev/null || true
        podman rm "$CONTAINER_NAME" 2>/dev/null || true
        echo "[STOP] 완료"
    else
        echo "[STOP] 실행 중인 컨테이너가 없습니다."
    fi
}

do_restart() {
    do_stop
    do_start
}

do_logs() {
    if podman ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        podman logs -f "$CONTAINER_NAME"
    else
        echo "[LOGS] 실행 중인 컨테이너가 없습니다."
        exit 1
    fi
}

do_status() {
    echo "=== 컨테이너 상태 ==="
    if podman ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        podman ps --filter "name=^${CONTAINER_NAME}$"
        echo ""
        echo "=== 헬스체크 ==="
        if curl -sf "http://localhost:${PROXY_PORT}/health" 2>/dev/null; then
            echo ""
            echo "[OK] 프록시 서버 정상 동작 중"
        else
            echo "[WARN] 헬스체크 실패 — 서버가 아직 시작 중이거나 문제가 있습니다."
        fi
    else
        echo "$CONTAINER_NAME 이 실행 중이 아닙니다."
    fi
}

# 메인
case "${1:-help}" in
    build)   do_build ;;
    start)   do_start ;;
    stop)    do_stop ;;
    restart) do_restart ;;
    logs)    do_logs ;;
    status)  do_status ;;
    help)    usage ;;
    *)
        echo "[ERROR] 알 수 없는 명령: $1"
        usage
        exit 1
        ;;
esac
