# Security boundaries

This experimental service is designed for trusted application-to-service traffic on a private network. Do not expose the example routes or Python server directly to the internet.

- Generate a service token of at least 32 non-whitespace ASCII characters. Never commit it. A token grants access to every job in that service instance.
- Enforce end-user authentication and per-job ownership in the PHP application. The example deliberately omits those application-specific policies and publishes no ports.
- Only trusted task implementations are allowed. Python child processes inherit the service environment; they are not suitable for arbitrary user-provided scripts.
- Container configuration uses a non-root user, read-only filesystem, dropped capabilities, no-new-privileges, CPU/memory/PID bounds, and an internal-only network for execution. It mounts only this checkout and the optional dependency/model cache, never a Docker socket or home directory.
- Dependency acquisition is separate and internet-enabled. Execution installs hash-checked wheels from the local cache without internet access. The model downloader retrieves fixed files at a pinned revision and checks their hashes; it does not execute model-repository code.
- Model files, native libraries and runtime dependencies still carry supply-chain risk. Review upgrades, regenerate locks deliberately and rerun tests before changing pins.
- Requests and results may contain sensitive documents. They live in RAM, may be present in process/core dumps, and are not encrypted by this service. Minimize retention and use appropriate host/storage policies. Embedding vectors are derived from their input text and can reveal much of it; store and transmit them with the same care as the text.
- Access logs are disabled to avoid accidentally recording identifiers and inputs. Internal exceptions become generic task errors. This is not a full observability or compliance solution.
- Cancel/timeout terminates the task process, but cannot roll back previously completed external effects. No task in the prototype modifies an external system.
- The default health endpoint proves HTTP service availability, not model integrity, ability to spawn processes or successful inference. Use a controlled model smoke test for readiness validation.

The FrankenPHP executable is copied from its pinned image into a task-only executable tmpfs. This removes upstream file capabilities without granting the container capabilities. Other temporary storage is non-executable; the optional Python dependency tmpfs must permit native library mappings.
