# Security policy

## Supported versions

Only the latest minor release receives security fixes. Older releases are not patched; upgrade to the latest
one first. See [the security boundaries](docs/security.md) for what the service does and
does not protect, and [the operations guide](docs/operations.md) for the supported deployment tier.

## Reporting a vulnerability

Report it privately through GitHub's private vulnerability reporting on this repository: open the
**Security** tab and choose **Report a vulnerability**. Please do not open a public issue, pull request or
discussion for it, and do not send it by email.

Include the affected version or commit, the component, the steps or input that reproduce it, and what an
attacker gains. Do not include real personal data or credentials in the report.

## Response

- An acknowledgement within 7 days.
- A fix, or a statement of why no fix will be made, within 90 days of the report.
- Credit in the advisory, if you want it, once a fix or statement is published.

## Scope

In scope:

- The Python worker (`worker/`): its HTTP service, job store, worker pool and task validation.
- The PHP client (`src/`), including the optional Laravel and Symfony integrations. GitHub's CodeQL does not
  support PHP: the client is covered by PHPStan at level max and the hostile-peer tests, not CodeQL.
- The MCP server (`mcp_server/`).
- The container images and limits defined in `docker/compose.yaml`.

Out of scope:

- The example routes and configuration in `examples/`, which deliberately omit end-user authentication and
  per-job ownership.
- The demo backends, which are deterministic stand-ins and not meant to be correct models.
- Vulnerabilities in third-party packages or model files with no impact specific to this project: report
  those upstream.

## Deployment rule

The worker is for trusted networks only, behind the application's own authentication. Its bearer token
authorises a service, not an end user: the application in front of it must authenticate its users and check
that a job belongs to the user asking for it. A report that depends on reaching the worker directly from an
untrusted network, without that application in front, describes a deployment this project rules out.
