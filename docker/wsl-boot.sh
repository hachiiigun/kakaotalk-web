#!/usr/bin/env bash
set -u

PROJECT_DIR="/mnt/c/tmp/kakaotalk-web"
LOG_DIR="${PROJECT_DIR}/.harness/logs"
LOG_FILE="${LOG_DIR}/wsl-boot.log"
LOCK_FILE="/run/wsl-kakaotalk-keepalive.lock"
WAIT_TIMEOUT=60

mkdir -p "${LOG_DIR}"

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "${LOG_FILE}"
}

log "wsl-boot.sh started (pid=$$)"

elapsed=0
while ! docker info >/dev/null 2>&1; do
    if [ "${elapsed}" -ge "${WAIT_TIMEOUT}" ]; then
        log "ERROR: docker daemon not ready after ${WAIT_TIMEOUT}s"
        exit 1
    fi
    sleep 2
    elapsed=$((elapsed + 2))
done
log "docker daemon ready after ${elapsed}s"

cd "${PROJECT_DIR}" || {
    log "ERROR: failed to cd to ${PROJECT_DIR}"
    exit 1
}

if compose_out=$(docker compose up -d 2>&1); then
    log "docker compose up -d succeeded"
    if [ -n "${compose_out}" ]; then
        printf '%s\n' "${compose_out}" | sed 's/^/  /' >> "${LOG_FILE}"
    fi
else
    rc=$?
    log "ERROR: docker compose up -d failed (rc=${rc})"
    if [ -n "${compose_out}" ]; then
        printf '%s\n' "${compose_out}" | sed 's/^/  /' >> "${LOG_FILE}"
    fi
fi

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    log "keep-alive already running; exiting duplicate launcher"
    exit 0
fi

log "keep-alive session active (pid=$$)"
exec sleep infinity
