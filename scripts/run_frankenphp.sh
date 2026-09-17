#!/bin/sh
set -eu
# The upstream executable has file capabilities for privileged ports. A fresh
# copy omits those xattrs, allowing execution with ALL capabilities dropped.
# The demo listens on 8080 and does not need privileged-port capabilities.
cp /usr/local/bin/frankenphp /runtime/frankenphp
chmod 700 /runtime/frankenphp
exec /runtime/frankenphp "$@"
