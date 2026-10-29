# Reranker reference scores

`rerank-fp32.json` holds eight fixed question/passage pairs and raw fp32 logits from
`onnx/model.onnx` at each quantized model's exact pinned revision. The file hashes,
revisions and repositories are in the fixture. No reference weights are retained or
added to the model fetcher.

Generated on 2026-10-28 UTC on an AMD Ryzen 9 9950X, Linux amd64 under Docker Desktop,
using the hash-locked ONNX Runtime 1.31.0, tokenizers 0.23.3 and NumPy 2.5.4. A disposable
container with one CPU, 6 GiB memory and no swap downloaded the fp32 files and ran
`scripts/rerank_reference.py`. Both files were available: MiniLM 90,992,115 bytes;
BGE 1,112,459,588 bytes. The production tokenizer loading, truncation (512 tokens),
padding and CPU scoring code is shared. Each query's four passages form one batch.

To regenerate from the repository root, with the wheels already fetched:

```bash
export MSYS_NO_PATHCONV=1
repo=$(pwd -W 2>/dev/null || pwd)
docker run --rm --name bridge-reranker-reference --label com.docker.compose.project=bridge-reranker \
  --memory=6g --memory-swap=6g --cpus=1 \
  -e PYTHONDONTWRITEBYTECODE=1 -e ORT_DISABLE_TELEMETRY=1 \
  -e OPENBLAS_NUM_THREADS=1 -e OMP_NUM_THREADS=1 -e RAYON_NUM_THREADS=1 \
  -v "$repo:/work:ro" -v "$repo/.cache:/cache:ro" -w /work \
  python:3.12-bookworm@sha256:581429e3df12d76e6af4be5ab7d0e7fc2013eb57dc23d2de691411c8efdbb970 \
  sh -ec 'pip install --no-compile --no-index --find-links=/cache/wheels --require-hashes --target /tmp/deps -r requirements-model.lock >&2; PYTHONPATH=/tmp/deps python scripts/rerank_reference.py' \
  > tests/fixtures/rerank-fp32.json
```

The PHP model job compares the pinned int8 files with these scores through the real
worker, asserting the same complete ordering for each query. Absolute raw-logit
tolerances are 0.75 for MiniLM and 1.25 for BGE. These allow quantization and CPU
variation; they do not assert numerically identical inference. Initial local maxima
were 0.49723 and 1.12509 respectively, with identical ordering for both. BGE failed an
initial 0.75 bound; its looser bound explicitly records that larger drift. This tiny
sample cannot establish whether quantization causes its corpus-level quality loss;
that requires a full fp32 quality run.
