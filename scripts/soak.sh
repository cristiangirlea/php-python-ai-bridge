#!/bin/sh
# Informational soak: the ONNX worker under steady load for SOAK_SECONDS, restarted once halfway. Never a gate.
# Usage: sh scripts/soak.sh OUT_DIR   (BRIDGE_TOKEN set; models and wheels already fetched into .cache)
# Writes OUT_DIR/soak.jsonl (one line per window), OUT_DIR/stats.csv (memory, CPU, PIDs each minute), OUT_DIR/events.log.
# Exits non-zero when the driver or the restart failed.
set -eu
export MSYS_NO_PATHCONV=1
out=${1:?usage: sh scripts/soak.sh OUT_DIR}
seconds=${SOAK_SECONDS:-28800}
case $seconds in
    '' | *[!0-9]* | 0*) echo "SOAK_SECONDS must be a whole number of seconds above zero" >&2; exit 2 ;;
esac
mkdir -p "$out"
out=$(cd "$out" && pwd)
cd "$(dirname "$0")/.."

sampler=
driver=
restart_failed=0
compose() {
    docker compose -p bridge-soak -f docker/compose.yaml --profile model "$@"
}
log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >> "$out/events.log"; }

cleanup() {
    status=$?
    trap - EXIT
    [ -z "$sampler" ] || kill "$sampler" 2>/dev/null || true
    [ -z "$driver" ] || kill "$driver" 2>/dev/null || true
    log "worker state: $(docker inspect -f 'restarts={{.RestartCount}} oom_killed={{.State.OOMKilled}} status={{.State.Status}}' bridge-soak-model-worker-1 2>&1)"
    compose down -v --remove-orphans >/dev/null 2>&1 || true
    log "down"
    exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

compose down -v --remove-orphans >/dev/null 2>&1 || true
rm -f "$out/driver.exit"
log "start seconds=$seconds"
compose up -d --wait model-worker
log "worker ready"

# Memory, CPU and PIDs of every container of the project, each minute, against the compose caps.
echo "at,container,mem_usage,mem_percent,cpu_percent,pids" > "$out/stats.csv"
(
    while :; do
        ids=$(docker ps -q --filter label=com.docker.compose.project=bridge-soak 2>/dev/null || true)
        # docker stats with no IDs would sample every container on the host.
        if [ -n "$ids" ]; then
            docker stats --no-stream --format "{{.Name}},{{.MemUsage}},{{.MemPerc}},{{.CPUPerc}},{{.PIDs}}" $ids 2>/dev/null \
                | sed "s|^|$(date -u +%Y-%m-%dT%H:%M:%SZ),|" >> "$out/stats.csv" || true
        else
            echo "$(date -u +%Y-%m-%dT%H:%M:%SZ),none,,,," >> "$out/stats.csv"
        fi
        sleep 60
    done
) &
sampler=$!

# The exit file, not kill -0, tells that the driver ended: an unreaped child still answers kill -0.
(
    set +e
    compose run --rm --no-deps -T -e SOAK_SECONDS="$seconds" -e SOAK_CONCURRENCY="${SOAK_CONCURRENCY:-4}" \
        -e SOAK_WINDOW_SECONDS="${SOAK_WINDOW_SECONDS:-600}" model-load python scripts/soak.py \
        > "$out/soak.jsonl" 2>> "$out/events.log"
    echo $? > "$out/driver.exit"
) &
driver=$!

# One restart halfway: the drain, a fresh start, and the load carrying on against it.
halfway=$(($(date +%s) + seconds / 2))
while [ "$(date +%s)" -lt "$halfway" ] && [ ! -e "$out/driver.exit" ]; do
    sleep 1
done
if [ ! -e "$out/driver.exit" ]; then
    log "restart"
    if compose restart model-worker >> "$out/events.log" 2>&1; then log "restarted"; else log "restart failed"; restart_failed=1; fi
fi

wait "$driver" || true
driver=
driver_status=$(cat "$out/driver.exit" 2>/dev/null || echo 1)
rm -f "$out/driver.exit"
if [ "$driver_status" = 0 ]; then log "driver finished"; else log "driver exited $driver_status"; exit "$driver_status"; fi
[ "$restart_failed" = 0 ] || exit 1
