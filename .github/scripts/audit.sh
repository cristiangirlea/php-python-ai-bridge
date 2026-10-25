#!/bin/sh
# The audit job of model-smoke.yml, run from the repository root. The scanners can be replaced through
# PIP_AUDIT_CMD and COMPOSER_AUDIT_CMD, so tests/python/test_audit_script.py can run it with stubs.
set -u

PIP_AUDIT_CMD=${PIP_AUDIT_CMD:-pipx run --spec pip-audit==2.10.1 pip-audit}
COMPOSER_AUDIT_CMD=${COMPOSER_AUDIT_CMD:-composer_audit}

# Takes the accepted ids as a JSON list; a read-only mount, so the audit cannot change the checkout.
composer_audit() {
    docker run --rm -v "$PWD:/work:ro" -w /work -e COMPOSER_HOME=/tmp/composer \
        composer:2@sha256:af98f42dfff7c68ba8d53c2164fd9fde1087b7d449514baa38c418b1f6bc4bac \
        sh -ec 'composer config --global audit.ignore --json "$1" && composer audit --locked --no-interaction' sh "$1"
}

# Ids are checked against a fixed shape so they can be passed on unquoted and into JSON; each needs a reason.
pip_ignore=
json=
while read -r id reason || [ -n "$id" ]; do
    case $id in '' | '#'*) continue ;; esac
    printf '%s\n' "$id" | LC_ALL=C grep -Eqx '[A-Z]+-[A-Za-z0-9-]+' || { echo "::error::malformed id: $id"; exit 1; }
    [ -n "$reason" ] || { echo "::error::$id has no reason"; exit 1; }
    pip_ignore="$pip_ignore --ignore-vuln $id"
    json="${json:+$json,}\"$id\""
done < .github/audit-ignore.txt

# Every scanner runs even when an earlier one fails, so one run reports all three locks.
status=0
# --disable-pip audits the pins as written and installs nothing; the locks cannot share one call (idna differs).
for lock in requirements-model.lock requirements-mcp.lock; do
    $PIP_AUDIT_CMD --disable-pip -r "$lock" --require-hashes $pip_ignore || status=1
done
$COMPOSER_AUDIT_CMD "[$json]" || status=1
exit "$status"
