"""Real HTTP -> FrankenPHP worker -> PHP client -> Python process acceptance test."""

import concurrent.futures
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE = os.environ.get("FRANKENPHP_URL", "http://frankenphp:8080")


def request(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = Request(BASE + path, data=data, headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=5) as response:
        return response.status, json.load(response), int(response.headers["X-Demo-Worker-Requests"])


def wait_ready():
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            if request("/healthz")[0] == 200:
                return
        except (URLError, OSError):
            time.sleep(0.1)
    raise AssertionError("FrankenPHP did not become ready")


def scenario(index):
    documents = ["no matching words"] * (1 + index % 5)
    expected = index % len(documents)
    documents[expected] = f"unique{index} token{index}"
    status, job, _ = request("/rerank", {"query": f"unique{index} token{index}", "documents": documents})
    assert status == 202
    assert job["status"] == "queued"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        _, done, _ = request("/jobs/" + job["id"])
        if done["status"] == "succeeded":
            assert done["result"]["model"] == "lexical-demo-not-a-model"
            assert len(done["result"]["rankings"]) == len(documents)
            assert done["result"]["rankings"][0]["index"] == expected
            assert done["result"]["rankings"][0]["score"] == 1
            return done["id"]
        assert done["status"] in {"queued", "running"}, done
        time.sleep(0.02)
    raise AssertionError("Job did not finish")


def main():
    wait_ready()
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
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        _, done, _ = request("/jobs/" + job["id"])
        if done["status"] == "succeeded":
            break
        assert done["status"] in {"queued", "running"}, done
        time.sleep(0.05)
    assert done["status"] == "succeeded", done
    assert [item["index"] for item in done["result"]["rankings"]][:1] == [511]
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
    print("PASS: 12 concurrent isolated jobs through a reused FrankenPHP worker; top_k narrowing; invalid and missing-job cases")


if __name__ == "__main__":
    main()
