#!/usr/bin/env bash
# Run the compatibility test subset as an unprivileged user when invoked as root.
set -eu
here="$(cd "$(dirname "$0")/../.." && pwd)"
if [ "$(id -u)" -ne 0 ]; then
    exec python3 "$here/internal/measurement/run_test_suite.py" "${@:-$here/internal/tests}"
fi
user=lca-tests
id "$user" >/dev/null 2>&1 || useradd -m "$user"
work="$(mktemp -d /tmp/lca-tests.XXXXXX)"
cp -r "$here" "$work/repo"
chown -R "$user" "$work"
su "$user" -s /bin/bash -c "cd '$work/repo' && python3 internal/measurement/run_test_suite.py ${*:-internal/tests}"
status=$?
rm -rf "$work"
exit $status
