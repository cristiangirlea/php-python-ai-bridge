#!/bin/sh
# Docker owns fault injection; the standard-library probe owns the HTTP assertions.
set -eu
export MSYS_NO_PATHCONV=1
: "${BRIDGE_TOKEN:?Set BRIDGE_TOKEN to a disposable random token of at least 32 characters}"
cd "$(dirname "$0")/.."

compose() {
    docker compose -p bridge-drills -f docker/compose.yaml --profile drills "$@"
}

probe() {
    service=$1
    shift
    compose exec -T "$service" python scripts/failure_drills.py "$@"
}

wait_stopped() {
    container=$(compose ps -a -q "$1")
    test -n "$container"
    attempts=0
    while [ "$(docker inspect -f '{{.State.Running}}' "$container")" = true ]; do
        attempts=$((attempts + 1))
        if [ "$attempts" -ge 15 ]; then
            echo 'worker did not stop within 15 seconds'
            return 1
        fi
        sleep 1
    done
    test "$(docker inspect -f '{{.State.ExitCode}} {{.State.OOMKilled}}' "$container")" = '0 false'
}

startup() {
    service=$1
    kind=$2
    code=0
    output=$(compose run --rm --no-deps -T "$service" 2>&1) || code=$?
    printf '%s\n' "$output" | compose exec -T drill-client python scripts/failure_drills.py startup --kind "$kind" --exit-code "$code"
}

one() {
    case "$1" in
        child-killed-mid-job)
            compose up -d --no-deps drill-child
            probe drill-child child-prepare
            child=$(probe drill-child child-pid)
            compose exec -T drill-child sh -ec 'kill -9 "$1"' sh "$child"
            probe drill-child child-check
            ;;
        missing-model-directory)
            startup drill-missing-model missing-model
            ;;
        state-directory-full)
            compose up -d --no-deps drill-state
            probe drill-state state-prepare
            probe drill-state fill-state
            probe drill-state state-check
            ;;
        open-token-file)
            startup drill-open-token open-token-file
            ;;
        drain-past-deadline)
            compose up -d --no-deps drill-drain
            probe drill-client drain-prepare --url http://drill-drain:8090
            compose kill -s TERM drill-drain
            probe drill-client poll-stop --url http://drill-drain:8090
            wait_stopped drill-drain
            compose logs --no-color --no-log-prefix drill-drain | compose exec -T drill-client python scripts/failure_drills.py drain-check
            ;;
        connection-flood)
            compose up -d --no-deps drill-flood
            probe drill-client flood --url http://drill-flood:8090
            ;;
        sigterm-under-load)
            compose up -d --no-deps drill-load
            probe drill-client load-prepare --url http://drill-load:8090
            compose kill -s TERM drill-load
            probe drill-client poll-stop --url http://drill-load:8090
            wait_stopped drill-load
            compose logs --no-color --no-log-prefix drill-load | compose exec -T drill-client python scripts/failure_drills.py load-logs
            compose start drill-load
            probe drill-client load-recovered --url http://drill-load:8090
            ;;
        *) exit 2 ;;
    esac
}

if [ "${1:-}" = --one ]; then
    one "$2"
    exit 0
fi

cleanup() {
    compose rm --stop --force -v >/dev/null 2>&1 || true
    docker volume rm bridge-drills_drill-load-state >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 1' INT TERM

compose config -q
# Discard state and stopped containers left by an interrupted run.
cleanup
compose up -d --no-deps drill-client
failed=0
for name in child-killed-mid-job missing-model-directory state-directory-full open-token-file drain-past-deadline connection-flood sigterm-under-load; do
    # A fresh shell preserves errexit inside each drill, even though its result is tested here.
    if output=$(sh scripts/failure_drills.sh --one "$name" 2>&1); then
        printf 'PASS %s\n' "$name"
    else
        reason=$(printf '%s\n' "$output" | tail -n 1)
        printf 'FAIL %s: %s\n' "$name" "${reason:-Docker step failed}"
        failed=1
    fi
done
printf 'SKIP start-crash-loop: no external control makes a spawn child die before readiness; covered by injected unit tests\n'
exit "$failed"
