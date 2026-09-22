"""Explicit task allowlist. Requests never select imports or executable code."""

import hashlib
import math
import os
import re
import time
from pathlib import Path


MAX_DOCUMENTS = 512
# 32 vectors of 384 six-decimal components stay well inside the PHP client's 262144 byte response cap.
MAX_TEXTS = 32
MAX_FIELD_CHARACTERS = 8192
# Chosen to leave headroom under the 262144 byte request limit for ASCII payloads.
# Multi-byte text reaches that byte limit first and is rejected by the transport.
MAX_TOTAL_CHARACTERS = 200000
EMBED_DIMENSIONS = 384


class InvalidInput(ValueError):
    pass


def _strings(values: object, plural: str, singular: str, limit: int, extra: int = 0) -> None:
    if not isinstance(values, list) or not 1 <= len(values) <= limit:
        raise InvalidInput(f"{plural} must contain 1-{limit} strings")
    if any(not isinstance(value, str) or not value.strip() or len(value) > MAX_FIELD_CHARACTERS
           for value in values):
        raise InvalidInput(f"each {singular} must contain 1-{MAX_FIELD_CHARACTERS} characters")
    if extra + sum(len(value) for value in values) > MAX_TOTAL_CHARACTERS:
        raise InvalidInput(f"{plural} must total at most {MAX_TOTAL_CHARACTERS} characters")


def validate(task: str, payload: object, test_tasks: bool = False) -> dict:
    if not isinstance(payload, dict):
        raise InvalidInput("input must be an object")
    if test_tasks and task in {"test.delay", "test.crash", "test.error"}:
        if set(payload) - {"seconds", "value"}:
            raise InvalidInput("unknown input field")
        seconds = payload.get("seconds", 0.05)
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 <= seconds <= 10:
            raise InvalidInput("seconds must be between 0 and 10")
        return payload
    if task == "embed":
        if set(payload) != {"texts"}:
            raise InvalidInput("embed requires only texts")
        _strings(payload["texts"], "texts", "text", MAX_TEXTS)
        return payload
    if task != "rerank":
        raise InvalidInput("unknown task")
    if not {"query", "documents"} <= set(payload) or set(payload) - {"query", "documents", "top_k"}:
        raise InvalidInput("rerank requires query and documents, with an optional top_k")
    query, documents = payload["query"], payload["documents"]
    if not isinstance(query, str) or not query.strip() or len(query) > MAX_FIELD_CHARACTERS:
        raise InvalidInput(f"query must contain 1-{MAX_FIELD_CHARACTERS} characters")
    _strings(documents, "documents", "document", MAX_DOCUMENTS, extra=len(query))
    top_k = payload.get("top_k", len(documents))
    # type() rejects bool, which int subclasses and would otherwise pass a range check.
    if type(top_k) is not int or not 1 <= top_k <= len(documents):
        raise InvalidInput("top_k must be an integer between 1 and the document count")
    return payload


def execute(task: str, payload: dict, backend: str, model_dir: str, progress) -> dict:
    if task == "test.crash":
        os._exit(17)
    if task == "test.error":
        raise RuntimeError("This internal exception must not reach the client")
    if task == "test.delay":
        progress(0, 1)
        time.sleep(payload.get("seconds", 0.05))
        progress(1, 1)
        return {"value": payload.get("value")}
    if task == "embed":
        return _embed(payload["texts"], backend, model_dir, progress)

    query, documents = payload["query"], payload["documents"]
    total = len(documents)
    # Ceiling division bounds progress to at most 66 messages so a large set
    # cannot flood the coordinator pipe.
    step = -(-total // 64)

    def report(completed):
        if completed % step == 0 or completed == total:
            progress(completed, total)

    report(0)
    if backend == "onnx":
        # Optional dependencies are installed separately; no runtime downloads.
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        directory = Path(model_dir) / "rerank"
        tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
        tokenizer.enable_truncation(max_length=512)
        tokenizer.enable_padding()
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        session = ort.InferenceSession(
            str(directory / "model.onnx"), options, providers=["CPUExecutionProvider"]
        )
        scores = []
        names = {item.name for item in session.get_inputs()}
        for i, document in enumerate(documents):
            encoded = tokenizer.encode(query, document)
            inputs = {
                "input_ids": np.array([encoded.ids], dtype=np.int64),
                "attention_mask": np.array([encoded.attention_mask], dtype=np.int64),
                "token_type_ids": np.array([encoded.type_ids], dtype=np.int64),
            }
            output = session.run(None, {key: value for key, value in inputs.items() if key in names})
            scores.append(float(output[0].reshape(-1)[0]))
            report(i + 1)
        model = "cross-encoder/ms-marco-TinyBERT-L2-v2"
    else:
        words = set(re.findall(r"\w+", query.casefold()))
        scores = []
        for i, document in enumerate(documents):
            other = set(re.findall(r"\w+", document.casefold()))
            scores.append(len(words & other) / max(1, len(words)))
            report(i + 1)
        model = "lexical-demo-not-a-model"
    if any(not math.isfinite(score) for score in scores):
        raise ValueError("model returned a non-finite score")
    rankings = [{"index": i, "score": score} for i, score in enumerate(scores)]
    rankings.sort(key=lambda item: (-item["score"], item["index"]))
    # validate() guarantees 1 <= top_k <= len(documents) whenever the key is present.
    return {"rankings": rankings[:payload.get("top_k", total)], "model": model}


def _hashing_vector(text: str) -> list:
    # sha256 keeps the demo stable across processes; hash() is salted per process.
    words = re.findall(r"\w+", text.casefold()) or [text.strip()]
    vector = [0.0] * EMBED_DIMENSIONS
    for word in words:
        digest = hashlib.sha256(word.encode()).digest()
        vector[int.from_bytes(digest[:4], "big") % EMBED_DIMENSIONS] += 1.0 if digest[4] & 1 else -1.0
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector]


def _embed(texts: list, backend: str, model_dir: str, progress) -> dict:
    total = len(texts)
    progress(0, total)
    if backend == "onnx":
        # Optional dependencies are installed separately; no runtime downloads.
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        directory = Path(model_dir) / "embed"
        tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
        # The model was trained on 256 word pieces; longer input is truncated, not rejected.
        tokenizer.enable_truncation(max_length=256)
        tokenizer.enable_padding()
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        session = ort.InferenceSession(
            str(directory / "model.onnx"), options, providers=["CPUExecutionProvider"]
        )
        names = {item.name for item in session.get_inputs()}
        encoded = tokenizer.encode_batch(texts)
        inputs = {
            "input_ids": np.array([item.ids for item in encoded], dtype=np.int64),
            "attention_mask": np.array([item.attention_mask for item in encoded], dtype=np.int64),
            "token_type_ids": np.array([item.type_ids for item in encoded], dtype=np.int64),
        }
        hidden = session.run(None, {key: value for key, value in inputs.items() if key in names})[0]
        # Mean pooling over attended tokens, then unit length, as sentence-transformers does.
        mask = inputs["attention_mask"][:, :, None].astype(hidden.dtype)
        pooled = (hidden * mask).sum(axis=1) / np.clip(mask.sum(axis=1), 1e-9, None)
        vectors = (pooled / np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-12, None)).tolist()
        model = "sentence-transformers/all-MiniLM-L6-v2"
    else:
        vectors = [_hashing_vector(text) for text in texts]
        model = "hashing-bow-not-a-model"
    # Six decimals bound the JSON size; unit vectors keep dot product equal to cosine.
    vectors = [[round(float(value), 6) for value in vector] for vector in vectors]
    if any(len(vector) != EMBED_DIMENSIONS for vector in vectors):
        raise ValueError("model returned the wrong number of dimensions")
    if any(not math.isfinite(value) for vector in vectors for value in vector):
        raise ValueError("model returned a non-finite component")
    progress(total, total)
    return {"model": model, "dimensions": EMBED_DIMENSIONS, "vectors": vectors}
