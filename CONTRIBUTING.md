# Contributing

This is an experimental project. Please discuss new task families, protocol changes, durability or production deployment work in an issue before a large implementation.

Keep pull requests focused. Use strict types and four-space indentation in PHP; use the Python standard library for the core service. Optional model dependencies belong in the model requirements files, not in the dependency-free worker path.

Run the deterministic unit, HTTP failure and FrankenPHP integration commands in [README.md](README.md). All project builds, tests and dependency acquisition should run in the supplied Docker environments. Do not run downloaded model code or mount credentials into test containers.

Every new behavior needs a successful case, invalid-input cases, and an observable integration test where the PHP/Python boundary is involved. Model-specific changes also need the optional model smoke tests. Record which tests you actually ran in the PR description.

Never commit `.env`, model weights, caches, private application documents, generated credentials or local environment reports. Update public documentation when the contract changes.

Use descriptive commits such as `feat: add ...`, `fix: reject ...`, or `test: cover ...`. Open changes as pull requests against `main`.
