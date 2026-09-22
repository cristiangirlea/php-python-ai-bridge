"""Explicit task allowlist. Requests never select imports or executable code."""

import math
import os
import re
import time
from pathlib import Path


MAX_DOCUMENTS = 512
MAX_FIELD_CHARACTERS = 8192
# Chosen to leave headroom under the 262144 byte request limit for ASCII payloads.
# Multi-byte text reaches that byte limit first and is rejected by the transport.
MAX_TOTAL_CHARACTERS = 200000


class InvalidInput(ValueError):
    pass


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
    if task != "rerank":
        raise InvalidInput("unknown task")
    if not {"query", "documents"} <= set(payload) or set(payload) - {"query", "documents", "top_k"}:
        raise InvalidInput("rerank requires query and documents, with an optional top_k")
    query, documents = payload["query"], payload["documents"]
    if not isinstance(query, str) or not query.strip() or len(query) > MAX_FIELD_CHARACTERS:
        raise InvalidInput(f"query must contain 1-{MAX_FIELD_CHARACTERS} characters")
    if not isinstance(documents, list) or not 1 <= len(documents) <= MAX_DOCUMENTS:
        raise InvalidInput(f"documents must contain 1-{MAX_DOCUMENTS} strings")
    if any(not isinstance(doc, str) or not doc.strip() or len(doc) > MAX_FIELD_CHARACTERS
           for doc in documents):
        raise InvalidInput(f"each document must contain 1-{MAX_FIELD_CHARACTERS} characters")
    if len(query) + sum(len(doc) for doc in documents) > MAX_TOTAL_CHARACTERS:
        raise InvalidInput(f"query and documents must total at most {MAX_TOTAL_CHARACTERS} characters")
    # type() rejects bool, which int subclasses and would otherwise pass a range check.
    if type(payload.get("top_k", 1)) is not int or not 1 <= payload.get("top_k", 1) <= len(documents):
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

    query, documents = payload["query"], payload["documents"]
    total = len(documents)
    # Bound progress messages so a large set cannot flood the coordinator pipe.
    step = max(1, total // 64)

    def report(completed):
        if completed % step == 0 or completed == total:
            progress(completed, total)

    report(0)
    if backend == "onnx":
        # Optional dependencies are installed separately; no runtime downloads.
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        directory = Path(model_dir)
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
