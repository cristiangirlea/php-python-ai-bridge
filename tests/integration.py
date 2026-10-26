"""Real HTTP -> FrankenPHP worker -> PHP client -> Python process acceptance test."""

import concurrent.futures
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE = os.environ.get("FRANKENPHP_URL", "http://frankenphp:8080")
WORKER = os.environ.get("WORKER_URL", "http://worker:8090")
TOKENS_WORKER = os.environ.get("TOKENS_WORKER_URL", "http://tokens-worker:8090")
# BRIDGE_OWNER_CAPACITY and BRIDGE_CAPACITY of tokens-worker in docker/compose.yaml.
OWNER_CAPACITY, CAPACITY = 8, 12


def request(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = Request(BASE + path, data=data, headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=5) as response:
        return response.status, json.load(response), int(response.headers["X-Demo-Worker-Requests"])


def metrics(token=None, worker=WORKER):
    """The worker's counters as {(name, labels): value}; the format itself is checked by the unit tests."""
    headers = {"Authorization": "Bearer " + (token if token is not None else os.environ["BRIDGE_TOKEN"])}
    with urlopen(Request(worker + "/metrics", headers=headers), timeout=5) as response:
        assert response.headers["Content-Type"] == "text/plain; version=0.0.4; charset=utf-8"
        text = response.read().decode("utf-8")
    samples = {}
    for line in text.splitlines():
        if not line.startswith("#"):
            series, value = line.rsplit(" ", 1)
            samples[series] = float(value)
    return text, samples


def call(token, method, path, body=None):
    """Status and decoded body of one request to the two-token worker, an error status included."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    try:
        with urlopen(Request(TOKENS_WORKER + path, data=data, headers=headers, method=method), timeout=5) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


def owners():
    """Two tokens on one worker: a job is the submitting token's, and another token reads it as unknown."""
    app, other = os.environ["BRIDGE_APP_TOKEN"], os.environ["BRIDGE_OTHER_TOKEN"]
    deadline = time.monotonic() + 30
    while True:
        try:
            if call(app, "GET", "/healthz")[0] == 200:
                break
        except (URLError, OSError):
            pass
        assert time.monotonic() < deadline, "the two-token worker did not become ready"
        time.sleep(0.1)
    # The PHP integration ran on this worker first, so the counters are compared before and after.
    _, counted = metrics(other, TOKENS_WORKER)
    rerank = {"task": "rerank", "input": {"query": "needle token", "documents": ["other words", "needle token"]}}
    status, job = call(app, "POST", "/v1/jobs", rerank)
    assert status == 202, status
    unknown = call(other, "GET", "/v1/jobs/" + "a" * 32)
    assert unknown[0] == 404, unknown
    assert call(other, "GET", "/v1/jobs/" + job["id"]) == unknown
    assert call(other, "POST", "/v1/jobs/" + job["id"] + "/cancel", {}) == unknown
    assert call(app, "GET", "/v1/jobs/" + job["id"])[1]["id"] == job["id"]
    assert call(app, "POST", "/v1/jobs/" + job["id"] + "/cancel", {})[1]["id"] == job["id"]
    keyed = dict(rerank, idempotency_key="python-owner-" + job["id"])
    from_app, from_other = call(app, "POST", "/v1/jobs", keyed)[1], call(other, "POST", "/v1/jobs", keyed)[1]
    assert from_app["id"] != from_other["id"]
    assert call(app, "POST", "/v1/jobs", keyed)[1]["id"] == from_app["id"]
    assert call(other, "POST", "/v1/jobs", keyed)[1]["id"] == from_other["id"]
    status, body = call("x" * 32, "GET", "/v1/jobs/" + job["id"])
    assert (status, body["error"]["code"]) == (401, "unauthorized"), status
    assert call(other, "GET", "/healthz")[0] == 200
    text, moved = metrics(app, TOKENS_WORKER)
    for owner, submitted in (("app", 2), ("other", 1)):
        name = f'bridge_jobs_submitted_total{{owner="{owner}",task="rerank"}}'
        assert moved[name] - counted[name] == submitted, (name, counted[name], moved[name])
    for token in (app, other):
        assert token not in text


def submit(token, body):
    """Status, Retry-After and decoded body of one submission to the two-token worker."""
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    try:
        with urlopen(Request(TOKENS_WORKER + "/v1/jobs", data=json.dumps(body).encode(), headers=headers),
                     timeout=5) as response:
            return response.status, response.headers["Retry-After"], json.load(response)
    except HTTPError as error:
        return error.code, error.headers["Retry-After"], json.load(error)


def held(samples, owner):
    """Jobs an owner has submitted, all still held: none is older than the worker's 300 s retention."""
    prefix = f'bridge_jobs_submitted_total{{owner="{owner}",'
    return sum(value for series, value in samples.items() if series.startswith(prefix))


def owner_capacity():
    """One token fills its cap and is refused while the other still submits, until the worker as a whole is full."""
    app, other = os.environ["BRIDGE_APP_TOKEN"], os.environ["BRIDGE_OTHER_TOKEN"]
    rejected = 'bridge_submissions_rejected_total{{owner="{}",reason="capacity"}}'
    _, counted = metrics(app, TOKENS_WORKER)
    rerank = {"task": "rerank", "input": {"query": "needle", "documents": ["needle"]}}
    keyed = dict(rerank, idempotency_key="python-cap-" + os.urandom(8).hex())
    first = None
    for _ in range(OWNER_CAPACITY + 1):
        status, retry_after, body = submit(app, rerank if first else keyed)
        if status != 202:
            break
        first = first or body
    assert first is not None and (status, body["error"]["code"]) == (429, "capacity_exceeded"), status
    assert 1 <= int(retry_after) <= 300, retry_after
    status, _, again = submit(app, keyed)
    assert (status, again["id"]) == (202, first["id"]), "a repeated key is answered at the cap"
    accepted = 0
    for _ in range(CAPACITY + 1):
        status, retry_after, body = submit(other, rerank)
        if status != 202:
            break
        accepted += 1
    assert accepted >= 1 and status == 429, (accepted, status)
    _, moved = metrics(other, TOKENS_WORKER)
    assert held(moved, "app") == OWNER_CAPACITY, held(moved, "app")
    assert held(moved, "app") + held(moved, "other") == CAPACITY and held(moved, "other") < OWNER_CAPACITY
    for owner in ("app", "other"):
        name = rejected.format(owner)
        assert moved[name] - counted[name] == 1, (name, counted[name], moved[name])


def wait_ready():
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            if request("/healthz")[0] == 200:
                return
        except (URLError, OSError):
            time.sleep(0.1)
    raise AssertionError("FrankenPHP did not become ready")


def finished(job_id, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _, done, _ = request("/jobs/" + job_id)
        if done["status"] == "succeeded":
            return done
        assert done["status"] in {"queued", "running"}, done
        time.sleep(0.02)
    raise AssertionError("Job did not finish")


def scenario(index):
    documents = ["no matching words"] * (1 + index % 5)
    expected = index % len(documents)
    documents[expected] = f"unique{index} token{index}"
    status, job, _ = request("/rerank", {"query": f"unique{index} token{index}", "documents": documents})
    assert status == 202
    assert job["status"] == "queued"
    done = finished(job["id"], 15)
    assert done["result"]["model"] == "lexical-demo-not-a-model"
    assert len(done["result"]["rankings"]) == len(documents)
    assert done["result"]["rankings"][0]["index"] == expected
    assert done["result"]["rankings"][0]["score"] == 1
    return done["id"]


def main():
    wait_ready()
    _, counted = metrics()
    _, _, before = request("/healthz")
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        ids = list(pool.map(scenario, range(12)))
    assert len(set(ids)) == 12
    _, _, after = request("/healthz")
    assert after > before + 12, "Requests must reuse a persistent FrankenPHP worker"
    try:
        request("/rerank", {"query": "a different request without documents"})
    except HTTPError as error:
        assert error.code == 400
    else:
        raise AssertionError("A later request reused previous input")
    try:
        request("/jobs/" + "a" * 32)
    except HTTPError as error:
        assert error.code == 404
    else:
        raise AssertionError("An unknown job returned another request's result")
    for field in ["query", "documents"]:
        body = {"query": "x", "documents": ["x"]}
        body[field] = "x" * 8193 if field == "query" else ["x" * 8193]
        try:
            request("/rerank", body)
        except HTTPError as error:
            assert error.code == 400, (field, error.code)
        else:
            raise AssertionError("Oversized input was accepted")
        body[field] = "é" * 8192 if field == "query" else ["é" * 8192]
        assert request("/rerank", body)[0] == 202, "Character boundary must allow multibyte text"
    # A large candidate set narrows to top_k through the whole PHP boundary.
    documents = ["no matching words"] * 512
    documents[511] = "needle token"
    status, job, _ = request("/rerank", {"query": "needle token", "documents": documents, "top_k": 3})
    assert status == 202, status
    done = finished(job["id"])
    assert done["result"]["rankings"][0]["index"] == 511
    assert len(done["result"]["rankings"]) == 3
    assert done["progress"] == {"completed": 512, "total": 512}, done["progress"]
    for body in [{"query": "x", "documents": ["x"] * 513}, {"query": "x", "documents": ["x", "y"], "top_k": 3},
                 {"query": "x", "documents": ["y" * 8000] * 25}]:
        try:
            request("/rerank", body)
        except HTTPError as error:
            assert error.code == 400, (len(body["documents"]), error.code)
        else:
            raise AssertionError("Out-of-contract rerank input was accepted")
    # Embedding through the same boundary: identical bags of words give identical vectors.
    status, job, _ = request("/embed", {"texts": ["red apple", "apple red", "blue sky"]})
    assert status == 202, status
    done = finished(job["id"])
    assert done["result"]["model"] == "hashing-bow-not-a-model"
    vectors = done["result"]["vectors"]
    assert len(vectors) == 3 and len(vectors[0]) == done["result"]["dimensions"] == 384
    assert vectors[0] == vectors[1] != vectors[2]
    for body in [{"texts": []}, {"texts": ["x"] * 33}, {"documents": ["x"]}]:
        try:
            request("/embed", body)
        except HTTPError as error:
            assert error.code == 400, (body, error.code)
        else:
            raise AssertionError("Out-of-contract embed input was accepted")
    # Redaction through the same boundary: rules run on the default backend, offsets count code points.
    status, job, _ = request("/redact", {"text": "café: mail x@y.io, card 4111 1111 1111 1111"})
    assert status == 202, status
    done = finished(job["id"])
    assert done["result"]["model"] == "rules-only-not-a-model"
    assert done["result"]["text"] == "café: mail [EMAIL], card [CARD]"
    assert [(s["start"], s["end"], s["source"]) for s in done["result"]["spans"]] == [(11, 17, "rule:email"), (24, 43, "rule:card")]
    for body in [{"text": ""}, {"text": "x", "entities": ["MISC"]}, {"text": "x", "min_score": 2}, {"texts": ["x"]},
                 {"text": "x", "min_score": None}, {"text": "x", "entities": None}, {"text": "x", "entities": [["PER"]]}]:
        try:
            request("/redact", body)
        except HTTPError as error:
            assert error.code == 400, (body, error.code)
        else:
            raise AssertionError("Out-of-contract redact input was accepted")
    # The worker counted those jobs, and its metrics hold numbers only, none of the documents.
    try:
        metrics("not-the-token")
    except HTTPError as error:
        assert error.code == 401, error.code
    else:
        raise AssertionError("Metrics were served without the token")
    text, moved = metrics()
    for task, least in [("rerank", 13), ("embed", 1), ("redact", 1)]:
        for name in [f'bridge_jobs_total{{owner="default",task="{task}",status="succeeded"}}',
                     f'bridge_jobs_submitted_total{{owner="default",task="{task}"}}',
                     f'bridge_job_duration_seconds_count{{task="{task}"}}']:
            assert moved[name] - counted[name] >= least, (name, counted[name], moved[name])
    for document in ["unique0 token0", "needle token", "x@y.io", "4111 1111 1111 1111", "red apple", "café"]:
        assert document not in text, document
    owners()
    owner_capacity()
    print("PASS: 12 concurrent isolated jobs through a reused FrankenPHP worker; top_k narrowing; embedding; redaction; invalid and missing-job cases; worker metrics; jobs scoped to their token; a per-token capacity cap")


if __name__ == "__main__":
    main()
