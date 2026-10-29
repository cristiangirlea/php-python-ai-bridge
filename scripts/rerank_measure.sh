#!/usr/bin/env bash
# One committed tree and a quiet host keep candidate timings comparable.
set -euo pipefail
cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1
: "${BRIDGE_TOKEN:?Set a disposable BRIDGE_TOKEN}"
git diff --quiet && git diff --cached --quiet || { echo 'Commit the measurement tree first' >&2; exit 1; }
export BENCH_COMMIT="$(git rev-parse HEAD)"
export BENCH_RUNNER="${BENCH_RUNNER:-local Windows 11 Pro, Docker Desktop}"
export BRIDGE_MODEL_RETENTION_SECONDS=15
export RELEVANCE_GATE=0
C=(docker compose -p bridge-reranker -f docker/compose.yaml)
mkdir -p .cache/rerank-measure
trap '"${C[@]}" --profile model down' EXIT
foreign_containers() {
    docker ps --format '{{.Names}}' | sed '/^bridge-reranker-/d' | { grep -Ev "${BENCH_STANDING:-^$}" || true; }
}
quiet() {
    local began=$SECONDS foreign
    while :; do
        # Services that always run on the host are named in BENCH_STANDING (an extended regex) and never wait.
        foreign=$(foreign_containers)
        if [ -z "$foreign" ]; then
            echo "Quiet host after $((SECONDS - began)) seconds" >&2
            return
        fi
        echo "Waiting for quiet host ($((SECONDS - began)) s): $foreign" >&2
        if [ $((SECONDS - began)) -ge 10800 ]; then
            echo 'Quiet host unavailable after three hours; no shared-host timings recorded' >&2
            return 1
        fi
        sleep 60
    done
}
for choice in tinybert minilm-l6 bge-base; do
    export BRIDGE_RERANK_MODEL="$choice"
    for dataset in arguana squad; do
        export RELEVANCE_DATASET="$dataset"
        quiet
        "${C[@]}" up -d --force-recreate model-worker
        "${C[@]}" run --rm --no-deps -T model-quality python scripts/relevance_measure.py \
            | tee ".cache/rerank-measure/$choice-$dataset.md"
        worker=$("${C[@]}" ps -q model-worker)
        peak=$(docker exec "$worker" cat /sys/fs/cgroup/memory.peak)
        swap=$(docker exec "$worker" cat /sys/fs/cgroup/memory.swap.max)
        printf '\nPeak cgroup memory: %s bytes; cgroup swap limit: %s bytes\n' "$peak" "$swap" \
            | tee -a ".cache/rerank-measure/$choice-$dataset.md"
        foreign=$(foreign_containers)
        if [ -n "$foreign" ]; then
            echo "Foreign containers after measurement; timings refused: $foreign" >&2
            exit 1
        fi
        "${C[@]}" --profile model down
    done
done
