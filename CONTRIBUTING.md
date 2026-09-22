# Contributing

This is an experimental project. Please discuss new task families, protocol changes, durability or production deployment work in an issue before a large implementation.

Keep pull requests focused. Use strict types and four-space indentation in PHP; use the Python standard library for the core service. Optional model dependencies belong in the model requirements files and the MCP server's in `requirements-mcp.*`, not in the dependency-free worker path.

Run the deterministic unit, HTTP failure and FrankenPHP integration commands in [README.md](README.md). All project builds, tests and dependency acquisition should run in the supplied Docker environments. Do not run downloaded model code or mount credentials into test containers.

Support the PHP branches still receiving upstream active or security maintenance. Run the deterministic suite on every version listed in `.github/workflows/tests.yml`; use its pinned `BRIDGE_PHP_IMAGE` values to reproduce a matrix job locally. PHP warnings/deprecations fail the PHP tests. Keep the Composer PHP range, image matrix, default runtime and [support policy](docs/testing.md) consistent. Review new stable branches before admitting them and retire EOL branches in a documented release; do not create a separate package per PHP version.

Every new behavior needs a successful case, invalid-input cases, and an observable integration test where the PHP/Python boundary is involved. Model-specific changes also need the optional model smoke tests. Record which tests you actually ran in the PR description.

For bug fixes, use a red-green regression workflow: add a test that fails on the current code, observe that failure, implement the smallest fix, then rerun the focused test and relevant integration tests. The initial prototype was not developed entirely test-first; do not describe test coverage as proof of a TDD history.

Never commit `.env`, model weights, caches, private application documents, generated credentials or local environment reports. Update public documentation when the contract changes.

Use descriptive commits such as `feat: add ...`, `fix: reject ...`, or `test: cover ...`. Open changes as pull requests against `main`.
