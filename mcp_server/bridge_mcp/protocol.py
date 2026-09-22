"""A second client of docs/protocol.md beside the PHP one, with the same checks. Standard library only."""

import http.client
import json
import re
import time
from urllib.parse import urlsplit

MAX_BYTES = 262144
TERMINAL = frozenset({"succeeded", "failed", "timed_out", "cancelled"})
STATUSES = TERMINAL | {"queued", "running"}
JOB_ID = re.compile(r"[a-f0-9]{32}")
ERROR_CODE = re.compile(r"[a-z_]{1,40}")


class BridgeError(Exception):
    """A protocol, transport or task failure. Messages never contain the token or worker internals."""

    def __init__(self, message: str, code: str, status: int | None = None, job_id: str | None = None):
        super().__init__(message)
        self.code, self.status, self.job_id = code, status, job_id


class Bridge:
    def __init__(self, base_url: str, token: str, request_timeout_s: float = 5.0):
        parts = urlsplit(base_url)
        if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password
                or parts.query or parts.fragment or parts.path not in ("", "/")
                or re.search(r"[\x00-\x20\x7f]", base_url)):
            raise ValueError("BRIDGE_URL must be an HTTP(S) origin without credentials")
        if len(token) < 32 or re.search(r"[^\x21-\x7e]", token):
            raise ValueError("BRIDGE_TOKEN must contain at least 32 non-whitespace ASCII characters")
        if not 0 < request_timeout_s <= 300:
            raise ValueError("request timeout must be between 0 and 300 seconds")
        self._scheme, self._host, self._port = parts.scheme, parts.hostname, parts.port
        self._token = token
        self._timeout = request_timeout_s

    def health(self) -> dict:
        body = self._request("GET", "/healthz")
        if body.get("protocol") != 1 or not isinstance(body.get("backend"), str):
            raise BridgeError("Unexpected health response", "invalid_response")
        return body

    def submit(self, task: str, payload: dict, timeout_ms: int) -> dict:
        if not 100 <= timeout_ms <= 300000:
            raise ValueError("timeout_ms must be between 100 and 300000")
        job = self._job(self._request("POST", "/v1/jobs", {"task": task, "input": payload, "timeout_ms": timeout_ms}))
        if job["task"] != task or job["status"] != "queued" or job["timeout_ms"] != timeout_ms:
            raise BridgeError("Submission response does not match the request", "invalid_response")
        return job

    def get(self, job_id: str) -> dict:
        return self._job(self._request("GET", "/v1/jobs/" + self._id(job_id)), job_id)

    def cancel(self, job_id: str) -> dict:
        return self._job(self._request("POST", "/v1/jobs/" + self._id(job_id) + "/cancel", {}), job_id)

    def run(self, task: str, payload: dict, deadline_s: float, on_progress=None, poll_s: float = 0.1) -> dict:
        """Submit, poll to a terminal state and return the result.

        A local deadline cancels the remote job, unlike the PHP client's wait(): a web request can
        come back for its job later, an abandoned tool call cannot.
        """
        # The worker's own deadline is a backstop two seconds behind the local one, so the local
        # wait normally fires first and cancels explicitly instead of leaving a timed-out job.
        job = self.submit(task, payload, max(100, min(300000, int(deadline_s * 1000) + 2000)))
        deadline = time.monotonic() + deadline_s
        reported = None
        try:
            while True:
                job = self.get(job["id"])
                progress = job.get("progress")
                if on_progress and progress and (progress["completed"], progress["total"]) != reported:
                    reported = (progress["completed"], progress["total"])
                    on_progress(*reported)
                if job["status"] in TERMINAL:
                    break
                if time.monotonic() >= deadline:
                    raise BridgeError(f"Job did not finish within {deadline_s:g}s and was cancelled", "wait_timeout",
                                      job_id=job["id"])
                time.sleep(poll_s)
        except BaseException:
            # Whatever interrupted the wait, nobody will come back for this job.
            try:
                self.cancel(job["id"])
            except BridgeError:
                pass
            raise
        if job["status"] == "succeeded":
            return job["result"]
        code = (job.get("error") or {}).get("code", job["status"])
        raise BridgeError(f"Job {job['status']}: {code}", code, job_id=job["id"])

    @staticmethod
    def _id(job_id: str) -> str:
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
            raise ValueError("Invalid job ID")
        return job_id

    @staticmethod
    def _job(body: dict, expected_id: str | None = None) -> dict:
        keys = ("id", "task", "status", "result", "error", "progress", "timeout_ms")
        if any(key not in body for key in keys):
            raise BridgeError("Incomplete job response", "invalid_response")
        progress, error = body["progress"], body["error"]
        if (not isinstance(body["id"], str) or not JOB_ID.fullmatch(body["id"])
                or not isinstance(body["task"], str) or not body["task"]
                or body["status"] not in STATUSES
                or type(body["timeout_ms"]) is not int or not 100 <= body["timeout_ms"] <= 300000
                or (body["status"] == "succeeded") != isinstance(body["result"], dict)
                or (body["status"] in ("failed", "timed_out")) != isinstance(error, dict)
                or (error is not None and not (isinstance(error.get("code"), str) and isinstance(error.get("message"), str)))
                or (progress is not None and not (isinstance(progress, dict)
                                                  and type(progress.get("completed")) is int and type(progress.get("total")) is int
                                                  and 0 <= progress["completed"] <= progress["total"] and progress["total"] >= 1))):
            raise BridgeError("Invalid job response", "invalid_response")
        if expected_id is not None and body["id"] != expected_id:
            raise BridgeError("Job ID does not match the request", "invalid_response")
        return body

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
            if len(data) > MAX_BYTES:
                raise BridgeError("Request exceeds 262144 bytes", "invalid_request")
        headers = {"Authorization": "Bearer " + self._token, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        connection_class = http.client.HTTPSConnection if self._scheme == "https" else http.client.HTTPConnection
        # http.client follows no redirects and reads no proxy variables, matching the PHP client.
        connection = connection_class(self._host, self._port, timeout=self._timeout)
        try:
            connection.request(method, path, data, headers)
            response = connection.getresponse()
            status, content_type = response.status, response.getheader("Content-Type") or ""
            raw = response.read(MAX_BYTES + 1)
        except (OSError, http.client.HTTPException) as error:
            raise BridgeError("Bridge transport failed: " + type(error).__name__, "transport_error") from None
        finally:
            connection.close()
        if len(raw) > MAX_BYTES:
            raise BridgeError("Response exceeds 262144 bytes", "invalid_response", status)
        is_json = content_type.split(";")[0].strip().lower() == "application/json"
        decoded = None
        if is_json:
            try:
                decoded = json.loads(raw)
            except ValueError:
                decoded = None
        if not 200 <= status < 300:
            # The status is the fact; only the worker's fixed error code is reflected from the body, never
            # free text, and a proxy's HTML error page contributes nothing but its status.
            code = decoded.get("error", {}).get("code") if isinstance(decoded, dict) and isinstance(decoded.get("error"), dict) else None
            code = code if isinstance(code, str) and ERROR_CODE.fullmatch(code) else "http_error"
            raise BridgeError(f"Bridge returned HTTP {status}: {code}", code, status)
        if not is_json:
            raise BridgeError("Expected a JSON response", "invalid_response", status)
        if decoded is None:
            raise BridgeError("Malformed JSON response", "invalid_response", status)
        if not isinstance(decoded, dict):
            raise BridgeError("Expected a JSON object", "invalid_response", status)
        return decoded
