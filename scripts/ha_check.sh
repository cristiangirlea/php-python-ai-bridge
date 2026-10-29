#!/bin/sh
set -eu

# A dedicated project keeps the destructive crash drill away from another deployment.
compose() {
    docker compose -p bridge-instances -f docker/compose.yaml --profile ha "$@"
}
cleanup() {
    status=$?
    trap - EXIT
    if [ "$status" -ne 0 ]; then
        compose logs --tail 100 || true
    fi
    compose down -v || status=1
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo '+ docker compose -p bridge-instances -f docker/compose.yaml --profile ha config -q'
compose config -q
echo '+ docker compose -p bridge-instances -f docker/compose.yaml --profile ha up -d --wait --wait-timeout 60 worker-a worker-b ha-proxy'
compose up -d --wait --wait-timeout 60 worker-a worker-b ha-proxy
echo '+ docker compose -p bridge-instances -f docker/compose.yaml --profile ha run --rm --no-deps -T ha-check php tests/php/ha.php roundtrip'
compose run --rm --no-deps -T ha-check php tests/php/ha.php roundtrip
for worker in worker-a worker-b; do
    echo "+ check $worker request log for oversized POST forwarding"
    compose logs --no-log-prefix --no-color "$worker" | compose run --rm --no-deps -T ha-check php tests/php/ha.php request-log
done
echo '+ docker compose -p bridge-instances -f docker/compose.yaml --profile ha run --rm --no-deps -T ha-check php tests/php/ha.php prepare'
target=$(compose run --rm --no-deps -T ha-check php tests/php/ha.php prepare)
HA_JOB_ID=${target%% *}
HA_JOB_KEY=${target#* }
export HA_JOB_ID HA_JOB_KEY
echo '+ docker compose -p bridge-instances -f docker/compose.yaml --profile ha kill -s SIGKILL worker-a'
compose kill -s SIGKILL worker-a
echo '+ docker compose -p bridge-instances -f docker/compose.yaml --profile ha start worker-a'
compose start worker-a
echo '+ docker compose -p bridge-instances -f docker/compose.yaml --profile ha run --rm --no-deps -T -e HA_JOB_ID -e HA_JOB_KEY ha-check php tests/php/ha.php recover'
compose run --rm --no-deps -T -e HA_JOB_ID -e HA_JOB_KEY ha-check php tests/php/ha.php recover
echo 'PASS: HA routing and hard-crash recovery end to end'
