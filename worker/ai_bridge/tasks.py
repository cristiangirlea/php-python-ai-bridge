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
# MISC (nationalities, events, products) is deliberately not offered: it is the noisiest class and rarely personal data.
REDACT_ENTITIES = ("PER", "ORG", "LOC")
REDACT_DEFAULTS = {"entities": ["PER"], "min_score": 0.85}
# Label order of dslim/bert-base-NER; the smoke test fails loudly if a re-pinned model changes it.
NER_LABELS = ("O", "B-MISC", "I-MISC", "B-PER", "I-PER", "B-ORG", "I-ORG", "B-LOC", "I-LOC")


class InvalidInput(ValueError):
    pass


def _text(value: object, name: str) -> int:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_FIELD_CHARACTERS:
        raise InvalidInput(f"{name} must contain 1-{MAX_FIELD_CHARACTERS} characters")
    try:
        # JSON escapes can produce lone surrogates that no tokenizer or encoder accepts.
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidInput(f"{name} must be valid Unicode text") from None
    return len(value)


def _strings(values: object, plural: str, singular: str, limit: int, extra: int = 0) -> None:
    if not isinstance(values, list) or not 1 <= len(values) <= limit:
        raise InvalidInput(f"{plural} must contain 1-{limit} strings")
    if extra + sum(_text(value, f"each {singular}") for value in values) > MAX_TOTAL_CHARACTERS:
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
    if task == "redact":
        if "text" not in payload or set(payload) - {"text", "entities", "min_score"}:
            raise InvalidInput("redact requires text, with optional entities and min_score")
        _text(payload["text"], "text")
        entities = payload.get("entities", REDACT_DEFAULTS["entities"])
        if (not isinstance(entities, list)
                or any(not isinstance(entity, str) or entity not in REDACT_ENTITIES for entity in entities)
                or len(set(entities)) != len(entities)):
            raise InvalidInput("entities must be distinct labels among PER, ORG and LOC")
        min_score = payload.get("min_score", REDACT_DEFAULTS["min_score"])
        if type(min_score) not in (int, float) or not math.isfinite(min_score) or not 0 <= min_score <= 1:
            raise InvalidInput("min_score must be a number between 0 and 1")
        return payload
    if task != "rerank":
        raise InvalidInput("unknown task")
    if not {"query", "documents"} <= set(payload) or set(payload) - {"query", "documents", "top_k"}:
        raise InvalidInput("rerank requires query and documents, with an optional top_k")
    query, documents = payload["query"], payload["documents"]
    _strings(documents, "documents", "document", MAX_DOCUMENTS, extra=_text(query, "query"))
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
    if task == "redact":
        return _redact(payload, backend, model_dir, progress)

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
        import numpy as np

        tokenizer, session, names = _onnx(Path(model_dir) / "rerank", 512)
        scores = []
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


def _onnx(directory: Path, max_length: int, stride: int = 0):
    # Optional dependencies are installed separately; no runtime downloads.
    import onnxruntime as ort
    from tokenizers import Tokenizer

    tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
    tokenizer.enable_truncation(max_length=max_length, stride=stride)
    tokenizer.enable_padding()
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    session = ort.InferenceSession(
        str(directory / "model.onnx"), options, providers=["CPUExecutionProvider"]
    )
    return tokenizer, session, {item.name for item in session.get_inputs()}


def _signed_counts(words: list) -> list:
    # sha256 keeps the demo stable across processes; hash() is salted per process.
    vector = [0.0] * EMBED_DIMENSIONS
    for word in words:
        digest = hashlib.sha256(word.encode()).digest()
        vector[int.from_bytes(digest[:4], "big") % EMBED_DIMENSIONS] += 1.0 if digest[4] & 1 else -1.0
    return vector


def _hashing_vector(text: str) -> list:
    vector = _signed_counts(re.findall(r"\w+", text.casefold()) or [text.strip()])
    if not any(vector):
        # Opposite signs in a shared bucket can cancel exactly; a single token never does.
        vector = _signed_counts([text.strip()])
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector]


def _embed(texts: list, backend: str, model_dir: str, progress) -> dict:
    total = len(texts)
    progress(0, total)
    if backend == "onnx":
        import numpy as np

        # The model was trained on 256 word pieces; longer input is truncated, not rejected.
        tokenizer, session, names = _onnx(Path(model_dir) / "embed", 256)
        encoded = tokenizer.encode_batch(texts)
        inputs = {
            "input_ids": np.array([item.ids for item in encoded], dtype=np.int64),
            "attention_mask": np.array([item.attention_mask for item in encoded], dtype=np.int64),
            "token_type_ids": np.array([item.type_ids for item in encoded], dtype=np.int64),
        }
        outputs = session.run(None, {key: value for key, value in inputs.items() if key in names})
        hidden = _output(session, outputs, "last_hidden_state")
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


def _output(session, outputs: list, name: str):
    # Take an output by name; position is only a fallback for exports without names.
    names = [item.name for item in session.get_outputs()]
    return outputs[names.index(name)] if name in names else outputs[0]


def _luhn(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2:
            value = value * 2 - 9 if value > 4 else value * 2
        total += value
    return total % 10 == 0


def _card_range(text: str, groups: list, start: int):
    # Longest range first, then leftmost, so the most conservative valid mask wins.
    ranges = []
    for first in range(start, len(groups)):
        for last in range(first, len(groups)):
            digits = "".join(text[begin:end] for begin, end in groups[first:last + 1])
            if len(digits) > 19:
                break
            if len(digits) >= 13:
                ranges.append((-len(digits), first, last, digits))
    for _, first, last, digits in sorted(ranges):
        if _luhn(digits):
            return first, last
    return None


def _cards(text: str):
    # Cards are written in separated groups, so only whole-group ranges are tried: a card beside
    # an expiry is still found, while a random digit run gains no Luhn-valid sub-window.
    for run in re.finditer(r"(?<!\d)\d(?:[ -]?\d){12,}(?!\d)", text):
        groups = [(run.start() + group.start(), run.start() + group.end()) for group in re.finditer(r"\d+", run.group())]
        cursor = 0
        while cursor < len(groups):
            found = _card_range(text, groups, cursor)
            if found is None:
                break
            first, last = found
            yield groups[first][0], groups[last][1]
            cursor = last + 1


def _iban(candidate: str) -> bool:
    compact = candidate.replace(" ", "")
    if not 15 <= len(compact) <= 34:
        return False
    return int("".join(str(int(char, 36)) for char in compact[4:] + compact[:4])) % 97 == 1


IBAN_PATTERN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]){11,30}\b")


def _ibans(text: str):
    for match in IBAN_PATTERN.finditer(text):
        candidate = match.group()
        # The greedy match can swallow following upper-case tokens; drop trailing groups until it checks.
        while len(candidate.replace(" ", "")) >= 15:
            if _iban(candidate):
                yield match.start(), match.start() + len(candidate)
                break
            if " " not in candidate:
                break
            candidate = candidate[:candidate.rfind(" ")]


DATE_SHAPES = re.compile(r"\d{4}[-./]\d{2}[-./]\d{2}|\d{2}[-./]\d{2}[-./]\d{2,4}")


def _phone(candidate: str) -> bool:
    digits = sum(char.isdigit() for char in candidate)
    if not 7 <= digits <= 15 or DATE_SHAPES.fullmatch(candidate):
        return False
    # A bare digit run is only taken for a phone number from ten digits; shorter ones need a
    # leading plus or separators, or they are order numbers, amounts and identifiers.
    return digits >= 10 or candidate[0] == "+" or not candidate.isdigit()


def _matches(pattern, accept=None):
    def find(text: str):
        for match in pattern.finditer(text):
            if accept is None or accept(match.group()):
                yield match.start(), match.end()
    return find


# Precedence for overlapping candidates: checksummed and exact rules before patterns. Every
# pattern is anchored on its left so scanning stays linear in the text length.
RULES = (
    ("CARD", _cards, 1.0),
    ("IBAN", _ibans, 1.0),
    ("EMAIL", _matches(re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")), 1.0),
    ("IPV4", _matches(re.compile(r"\b(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}\b")), 1.0),
    ("PHONE", _matches(re.compile(r"(?<!\w)\+?\d[\d ().-]{5,}\d(?!\w)"), _phone), 0.8),
)


def _close(spans: list, current, entities: list, min_score: float) -> None:
    if current is None:
        return
    score = sum(current["scores"]) / len(current["scores"])
    if current["label"] in entities and score >= min_score:
        spans.append({"start": current["start"], "end": current["end"], "label": current["label"],
                      "source": "model:" + current["label"], "score": round(score, 4), "priority": len(RULES)})


def _entities(window, probabilities: list, entities: list, min_score: float) -> list:
    # Each word takes its first word piece's label, the usual BERT aggregation; later pieces only extend it.
    words = []
    for position, word_id in enumerate(window.word_ids):
        if word_id is None:
            continue
        start, end = window.offsets[position]
        if words and words[-1][4] == word_id:
            words[-1][1] = end
            continue
        row = probabilities[position]
        label_id = max(range(len(row)), key=row.__getitem__)
        words.append([start, end, NER_LABELS[label_id], float(row[label_id]), word_id])
    spans, current = [], None
    for start, end, label, score, _ in words:
        prefix, _, kind = label.partition("-")
        if prefix == "B" or (prefix == "I" and (current is None or current["label"] != kind)):
            _close(spans, current, entities, min_score)
            current = {"start": start, "end": end, "label": kind, "scores": [score]}
        elif prefix == "I":
            current["end"] = end
            current["scores"].append(score)
        else:
            _close(spans, current, entities, min_score)
            current = None
    _close(spans, current, entities, min_score)
    return spans


def _ner(text: str, entities: list, min_score: float, model_dir: str, progress) -> list:
    import numpy as np

    # Overlapping 512 piece windows cover long text; offsets stay relative to the whole text.
    tokenizer, session, names = _onnx(Path(model_dir) / "redact", 512, stride=64)
    encoding = tokenizer.encode(text)
    windows = [encoding, *encoding.overflowing]
    progress(0, len(windows))
    candidates = []
    for index, window in enumerate(windows):
        inputs = {
            "input_ids": np.array([window.ids], dtype=np.int64),
            "attention_mask": np.array([window.attention_mask], dtype=np.int64),
            "token_type_ids": np.array([window.type_ids], dtype=np.int64),
        }
        outputs = session.run(None, {key: value for key, value in inputs.items() if key in names})
        logits = _output(session, outputs, "logits")[0]
        shifted = np.exp(logits - logits.max(axis=-1, keepdims=True))
        probabilities = (shifted / shifted.sum(axis=-1, keepdims=True)).tolist()
        candidates.extend(_entities(window, probabilities, entities, min_score))
        progress(index + 1, len(windows))
    return candidates


def _merge(candidates: list) -> list:
    # Leftmost first, then longest, then the stronger source. A candidate overlapping the kept
    # span extends it instead of being dropped, so no part of either stays visible.
    spans = []
    for span in sorted(candidates, key=lambda span: (span["start"], span["start"] - span["end"], span["priority"])):
        if spans and span["start"] < spans[-1]["end"]:
            spans[-1]["end"] = max(spans[-1]["end"], span["end"])
            continue
        spans.append({key: span[key] for key in ("start", "end", "label", "source", "score")})
    return spans


def _redact(payload: dict, backend: str, model_dir: str, progress) -> dict:
    text = payload["text"]
    entities = payload.get("entities", REDACT_DEFAULTS["entities"])
    candidates = []
    for priority, (label, find, score) in enumerate(RULES):
        for start, end in find(text):
            candidates.append({"start": start, "end": end, "label": label, "source": "rule:" + label.lower(),
                               "score": score, "priority": priority})
    # Nothing to ask the model when no entity is wanted, so the model is not even loaded.
    if backend == "onnx" and entities:
        candidates.extend(_ner(text, entities, payload.get("min_score", REDACT_DEFAULTS["min_score"]),
                               model_dir, progress))
        model = "Xenova/bert-base-NER:int8"
    else:
        progress(0, 1)
        model = "rules-only-not-a-model"
    spans = _merge(candidates)
    pieces, cursor = [], 0
    for span in spans:
        pieces.append(text[cursor:span["start"]])
        pieces.append("[" + span["label"] + "]")
        cursor = span["end"]
    pieces.append(text[cursor:])
    if model == "rules-only-not-a-model":
        progress(1, 1)
    return {"model": model, "text": "".join(pieces), "spans": spans}
