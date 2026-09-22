"""One MCP tool per allowlisted task. The tool list is a literal in this file: nothing is read from
the worker, and the test-only tasks cannot appear whatever BRIDGE_TEST_TASKS says."""

import json
import math
import re
from pathlib import Path
from typing import Annotated, Literal, TypedDict

import anyio
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .protocol import Bridge, BridgeError

TOOL_NAMES = ("bridge_health", "bridge_rerank", "bridge_embed_similarity", "bridge_redact")
# Past this many documents the agent is paying output tokens to narrow text it already holds;
# a file under the root costs it nothing to reference.
BY_VALUE_DOCUMENTS = 64
BY_REFERENCE_DOCUMENTS = 512
MAX_TEXTS = 32
MAX_TEXT_CHARACTERS = 8192
MAX_TOTAL_CHARACTERS = 200000
MAX_FILE_BYTES = 4 * 1024 * 1024
SNIPPET_CHARACTERS = 160
PAIRS = 10

# What each backend really is: the name the worker reports, then what it does, in plain words.
BACKENDS = {
    "lexical": {
        "rerank": ("lexical-demo-not-a-model", "deterministic word overlap between the query and each document. "
                   "This is NOT a model and knows nothing about meaning; it exists to exercise the pipeline."),
        "embed": ("hashing-bow-not-a-model", "a signed hash of word tokens, NOT a model; similarity reflects "
                  "shared words only."),
        "redact": ("rules-only-not-a-model", "checksum and pattern rules only (card numbers, IBANs, emails, IPv4 "
                   "addresses, phone numbers). No model runs, so names, organisations and places are not found."),
    },
    "onnx": {
        "rerank": ("cross-encoder/ms-marco-TinyBERT-L2-v2", "through ONNX Runtime on one CPU thread, a two-layer "
                   "cross-encoder. Its relevance judgement is well below your own on text you can already read."),
        "embed": ("sentence-transformers/all-MiniLM-L6-v2", "through ONNX Runtime on CPU; English, truncated at "
                  "256 word pieces; unit-length mean-pooled vectors."),
        "redact": ("rules + Xenova/bert-base-NER:int8", "checksum and pattern rules plus dslim/bert-base-NER (the "
                   "int8 ONNX conversion published as Xenova/bert-base-NER) for PER, ORG and LOC. Trained on "
                   "English news; recall on other text is lower and unmeasured."),
    },
}

INSTRUCTIONS = """Tools for a private PHP-Python AI bridge worker. Every call submits a job over the bridge's HTTP
protocol, polls it to completion and returns the finished result; the worker runs each job in its own process.

Economics: a tool's input is your output. Do not paste hundreds of documents into bridge_rerank; put them in a
file under the configured root and pass documents_path. bridge_embed_similarity never returns raw vectors.
bridge_redact protects whatever you forward next, not this conversation: the text is already here.

The worker for this server is {backend!r}. Backends: rerank = {rerank} embed = {embed} redact = {redact}"""


class Health(TypedDict):
    backend: str
    protocol: int
    models: dict[str, str]


class RerankHit(TypedDict):
    index: int
    score: float
    snippet: str


class Rerank(TypedDict):
    model: str
    considered: int
    results: list[RerankHit]


class Pair(TypedDict):
    a: int
    b: int
    similarity: float


class Similarity(TypedDict):
    model: str
    matrix: list[list[float]]
    pairs: list[Pair]


class Span(TypedDict):
    start: int
    end: int
    label: str
    source: str
    score: float


class Redaction(TypedDict):
    model: str
    text: str
    spans: list[Span]


def _explain(error: BridgeError) -> str:
    hints = {
        "wait_timeout": "The job did not finish within the tool's deadline and was cancelled; try fewer documents.",
        "capacity_exceeded": "The worker is at capacity (HTTP 429); retry shortly.",
        "invalid_request": "The worker rejected the request as out of contract (HTTP 400).",
        "unauthorized": "The worker did not accept BRIDGE_TOKEN (HTTP 401).",
        "transport_error": "Could not reach the worker at BRIDGE_URL; is it running on the same network?",
        "deadline_exceeded": "The worker's own deadline expired before the job finished.",
    }
    if error.status == 401:
        return hints["unauthorized"]
    if error.status is not None and error.status >= 500:
        return f"The worker, or a proxy in front of it, returned HTTP {error.status}; retry shortly."
    return hints.get(error.code, f"The worker reported {error.code}; internal details are not exposed.")


def _number(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _name(value) -> bool:
    return isinstance(value, str) and value != ""


def _rankings(result: dict, count: int, top_k: int) -> list:
    # The same rules as RerankResult in the PHP client: exact count, unique indexes, stable descending order.
    rankings = result.get("rankings")
    if not _name(result.get("model")) or not isinstance(rankings, list) or len(rankings) != min(top_k, count):
        raise ToolError("The worker returned an invalid ranking.")
    seen, previous = set(), None
    for item in rankings:
        if (not isinstance(item, dict) or type(item.get("index")) is not int or not 0 <= item["index"] < count
                or item["index"] in seen or not _number(item.get("score"))
                or (previous is not None and (item["score"] > previous[1]
                                              or (item["score"] == previous[1] and item["index"] < previous[0])))):
            raise ToolError("The worker returned an invalid ranking.")
        seen.add(item["index"])
        previous = (item["index"], item["score"])
    return rankings


def _vectors(result: dict, count: int) -> list:
    # The same rules as EmbedResult: one vector of the declared dimension per text, finite, unit length.
    vectors, dimensions = result.get("vectors"), result.get("dimensions")
    if (not _name(result.get("model")) or type(dimensions) is not int or dimensions < 1
            or not isinstance(vectors, list) or len(vectors) != count):
        raise ToolError("The worker returned invalid embeddings.")
    for vector in vectors:
        if (not isinstance(vector, list) or len(vector) != dimensions or not all(_number(value) for value in vector)
                or abs(math.sqrt(sum(value * value for value in vector)) - 1.0) > 1e-3):
            raise ToolError("The worker returned invalid embeddings.")
    return vectors


def _spans(result: dict, text: str) -> list:
    # The same rules as RedactResult: spans sorted, disjoint and inside the original text.
    spans = result.get("spans")
    if (not _name(result.get("model")) or not isinstance(result.get("text"), str) or not result["text"]
            or not isinstance(spans, list)):
        raise ToolError("The worker returned an invalid redaction.")
    cursor = 0
    for span in spans:
        if (not isinstance(span, dict) or type(span.get("start")) is not int or type(span.get("end")) is not int
                or span["start"] < cursor or span["start"] >= span["end"] or span["end"] > len(text)
                or not isinstance(span.get("label"), str) or not span["label"]
                or not isinstance(span.get("source"), str) or not span["source"]
                or not _number(span.get("score")) or not 0 <= span["score"] <= 1):
            raise ToolError("The worker returned an invalid redaction.")
        cursor = span["end"]
    return spans


def _check_texts(texts: list, singular: str, limit: int, extra: int = 0) -> None:
    if not 1 <= len(texts) <= limit:
        raise ToolError(f"Give between 1 and {limit} {singular}s.")
    total = extra
    for index, text in enumerate(texts):
        if not isinstance(text, str) or not text.strip():
            raise ToolError(f"{singular} {index} is empty.")
        if len(text) > MAX_TEXT_CHARACTERS:
            raise ToolError(f"{singular} {index} has {len(text)} characters; the limit is {MAX_TEXT_CHARACTERS}.")
        total += len(text)
    if total > MAX_TOTAL_CHARACTERS:
        raise ToolError(f"The {singular}s total {total} characters; the limit is {MAX_TOTAL_CHARACTERS}.")


def _documents_from(root: Path | None, path_text: str) -> list[str]:
    if root is None:
        raise ToolError("documents_path needs a configured root: start the server with BRIDGE_MCP_ROOT set to the "
                        "directory whose files may be read.")
    candidate = Path(path_text)
    try:
        resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    except (OSError, ValueError) as error:
        raise ToolError(f"Cannot resolve {path_text!r}: {type(error).__name__}.") from None
    if not resolved.is_relative_to(root):
        raise ToolError(f"documents_path must stay inside the configured root ({root}).")
    try:
        if not resolved.is_file():
            raise ToolError(f"No file at {path_text!r} under the configured root.")
        if resolved.stat().st_size > MAX_FILE_BYTES:
            raise ToolError(f"{path_text!r} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MiB.")
        text = resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ToolError(f"{path_text!r} is not UTF-8 text.") from None
    except OSError as error:
        raise ToolError(f"Cannot read {path_text!r}: {type(error).__name__}.") from None
    if text.lstrip().startswith("["):
        try:
            documents = json.loads(text)
        except ValueError:
            raise ToolError(f"{path_text!r} starts with '[' but is not a JSON array.") from None
        if not isinstance(documents, list) or not all(isinstance(item, str) for item in documents):
            raise ToolError(f"{path_text!r} must be a JSON array of strings.")
    else:
        documents = [line.strip() for line in text.splitlines() if line.strip()]
    if not documents:
        raise ToolError(f"{path_text!r} contains no documents.")
    if len(documents) > BY_REFERENCE_DOCUMENTS:
        raise ToolError(f"{path_text!r} holds {len(documents)} documents; the worker takes at most "
                        f"{BY_REFERENCE_DOCUMENTS}. Split the file.")
    return documents


def _snippet(text: str) -> str:
    collapsed = re.sub(r"\s+", " ", text).strip()
    return collapsed if len(collapsed) <= SNIPPET_CHARACTERS else collapsed[:SNIPPET_CHARACTERS - 1] + "…"


def build_server(bridge: Bridge, backend: str, root: Path | None, timeout_ms: int) -> MCPServer:
    if backend not in BACKENDS:
        raise ValueError(f"unknown worker backend {backend!r}")
    if type(timeout_ms) is not int or not 100 <= timeout_ms <= 300000:
        raise ValueError("timeout must be between 100 and 300000 milliseconds")
    if root is not None:
        root = root.resolve()
        if not root.is_dir():
            raise ValueError(f"BRIDGE_MCP_ROOT is not a directory: {root}")
    described = {task: f"{name}: {explanation}" for task, (name, explanation) in BACKENDS[backend].items()}
    names = {task: name for task, (name, _) in BACKENDS[backend].items()}
    seconds = timeout_ms / 1000
    timing = ("well under a second" if backend == "lexical"
              else "one to ten seconds, as the model is loaded afresh for every call")
    mcp = MCPServer("bridge_mcp", title="PHP-Python AI bridge", version="0.1",
                    instructions=INSTRUCTIONS.format(backend=backend, **described))

    async def run(task: str, payload: dict, ctx: Context) -> dict:
        def on_progress(completed: int, total: int) -> None:
            # The SDK sends nothing when the host gave no progress token, so this is unconditional.
            anyio.from_thread.run(ctx.report_progress, completed, total, f"{completed}/{total}")

        try:
            return await anyio.to_thread.run_sync(lambda: bridge.run(task, payload, seconds, on_progress, 0.1))
        except BridgeError as error:
            raise ToolError(_explain(error)) from None

    @mcp.tool(
        name="bridge_health", title="Bridge worker health",
        description="Report whether the bridge worker answers and which backend it runs, so you know whether the "
                    "other tools are backed by real models or by the deterministic demos. Instant.",
        annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False),
    )
    async def bridge_health() -> Health:
        try:
            health = await anyio.to_thread.run_sync(bridge.health)
        except BridgeError as error:
            raise ToolError(_explain(error)) from None
        return {"backend": health["backend"], "protocol": health["protocol"], "models": dict(names)}

    @mcp.tool(
        name="bridge_rerank", title="Rerank candidate documents",
        description=f"Rank candidate documents against a query and return only the best top_k with a short snippet "
                    f"of each. Give the documents by value only when there are a few ({BY_VALUE_DOCUMENTS} at most); "
                    f"for a real candidate set write them to a file under the configured root and pass "
                    f"documents_path (a JSON array of strings, or one document per line, up to "
                    f"{BY_REFERENCE_DOCUMENTS}). If you have already read the documents, rank them yourself: this "
                    f"tool earns its place on text you have not read. Takes {timing}. Backend: {described['rerank']}",
        annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False),
    )
    async def bridge_rerank(
        query: Annotated[str, Field(description="What the documents are ranked against.", min_length=1,
                                    max_length=MAX_TEXT_CHARACTERS)],
        ctx: Context,
        documents: Annotated[list[str] | None, Field(
            description=f"Documents by value, at most {BY_VALUE_DOCUMENTS}; prefer documents_path.",
            max_length=BY_VALUE_DOCUMENTS)] = None,
        documents_path: Annotated[str | None, Field(
            description="A file under BRIDGE_MCP_ROOT: a JSON array of strings, or one document per line.")] = None,
        top_k: Annotated[int, Field(description="How many of the best documents to return.", ge=1,
                                    le=BY_REFERENCE_DOCUMENTS)] = 10,
    ) -> Rerank:
        if not query.strip():
            raise ToolError("query is empty.")
        if (documents is None) == (documents_path is None):
            raise ToolError("Give exactly one of documents or documents_path.")
        chosen = documents if documents is not None else _documents_from(root, documents_path)
        _check_texts(chosen, "document", BY_VALUE_DOCUMENTS if documents is not None else BY_REFERENCE_DOCUMENTS,
                     extra=len(query))
        result = await run("rerank", {"query": query, "documents": chosen, "top_k": min(top_k, len(chosen))}, ctx)
        rankings = _rankings(result, len(chosen), top_k)
        return {"model": result["model"], "considered": len(chosen),
                "results": [{"index": item["index"], "score": item["score"], "snippet": _snippet(chosen[item["index"]])}
                            for item in rankings]}

    @mcp.tool(
        name="bridge_embed_similarity", title="Semantic similarity between texts",
        description=f"Embed 2 to {MAX_TEXTS} texts and return their cosine similarity matrix and the most similar "
                    f"pairs, for grouping or deduplicating short texts. Vectors themselves are never returned: build "
                    f"an index with the HTTP protocol from a script instead. Takes {timing}. "
                    f"Backend: {described['embed']}",
        annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False),
    )
    async def bridge_embed_similarity(
        texts: Annotated[list[str], Field(description="The texts to compare.", min_length=2, max_length=MAX_TEXTS)],
        ctx: Context,
    ) -> Similarity:
        _check_texts(texts, "text", MAX_TEXTS)
        result = await run("embed", {"texts": texts}, ctx)
        vectors = _vectors(result, len(texts))
        matrix = [[round(sum(x * y for x, y in zip(a, b)), 4) for b in vectors] for a in vectors]
        pairs = sorted(({"a": a, "b": b, "similarity": matrix[a][b]}
                        for a in range(len(texts)) for b in range(a + 1, len(texts))),
                       key=lambda pair: (-pair["similarity"], pair["a"], pair["b"]))[:PAIRS]
        return {"model": result["model"], "matrix": matrix, "pairs": pairs}

    @mcp.tool(
        name="bridge_redact", title="Mask personal data in a text",
        description=f"Replace personal data in one text with [LABEL] placeholders and list every span found with "
                    f"the rule or model that found it. This protects whatever you send the text to next; the text "
                    f"itself is already in this conversation. Assistive, not a compliance control. Takes {timing}. "
                    f"Backend: {described['redact']}",
        annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False),
    )
    async def bridge_redact(
        text: Annotated[str, Field(description="The text to mask.", min_length=1, max_length=MAX_TEXT_CHARACTERS)],
        ctx: Context,
        entities: Annotated[list[Literal["PER", "ORG", "LOC"]], Field(
            description="Entity kinds the model pass masks; rules always run.", max_length=3)] = ("PER",),
        min_score: Annotated[float, Field(description="Minimum model confidence to mask.", ge=0, le=1)] = 0.85,
    ) -> Redaction:
        if not text.strip():
            raise ToolError("text is empty.")
        if len(set(entities)) != len(entities):
            raise ToolError("entities must be distinct.")
        result = await run("redact", {"text": text, "entities": list(entities), "min_score": min_score}, ctx)
        return {"model": result["model"], "text": result["text"], "spans": _spans(result, text)}

    return mcp
