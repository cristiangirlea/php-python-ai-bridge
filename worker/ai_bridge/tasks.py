"""Explicit task allowlist. Requests never select imports or executable code."""

import math
import os
import re
import time
from pathlib import Path


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
    if set(payload) != {"query", "documents"}:
        raise InvalidInput("rerank requires only query and documents")
    query, documents = payload["query"], payload["documents"]
    if not isinstance(query, str) or not query.strip() or len(query) > 8192:
        raise InvalidInput("query must contain 1-8192 characters")
    if not isinstance(documents, list) or not 1 <= len(documents) <= 32:
        raise InvalidInput("documents must contain 1-32 strings")
    if any(not isinstance(doc, str) or not doc.strip() or len(doc) > 8192 for doc in documents):
        raise InvalidInput("each document must contain 1-8192 characters")
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
    progress(0, len(documents))
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
            progress(i + 1, len(documents))
        model = "cross-encoder/ms-marco-TinyBERT-L2-v2"
    else:
        words = set(re.findall(r"\w+", query.casefold()))
        scores = []
        for i, document in enumerate(documents):
            other = set(re.findall(r"\w+", document.casefold()))
            scores.append(len(words & other) / max(1, len(words)))
            progress(i + 1, len(documents))
        model = "lexical-demo-not-a-model"
    if any(not math.isfinite(score) for score in scores):
        raise ValueError("model returned a non-finite score")
    rankings = [{"index": i, "score": score} for i, score in enumerate(scores)]
    rankings.sort(key=lambda item: (-item["score"], item["index"]))
    return {"rankings": rankings, "model": model}
