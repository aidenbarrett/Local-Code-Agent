#!/usr/bin/env bash
# Run the test suite as an unprivileged user when invoked as root.
#
# Root ignores file mode bits, so any test about read-only files, protected
# directories or permission failures passes vacuously as root. The oracle
# snapshot bug shipped that way. Containers usually run as root; real machines
# do not. This makes the container tell the truth.
set -eu
here="$(cd "$(dirname "$0")/.." && pwd)"
if [ "$(id -u)" -ne 0 ]; then
    exec python3 "$here/measurement/run_test_suite.py" "${@:-tests}"
fi
user=lca-tests
id "$user" >/dev/null 2>&1 || useradd -m "$user"
work="$(mktemp -d /tmp/lca-tests.XXXXXX)"
cp -r "$here" "$work/repo"
chown -R "$user" "$work"
su "$user" -s /bin/bash -c "cd '$work/repo' && python3 measurement/run_test_suite.py ${*:-tests}"
status=$?
rm -rf "$work"
exit $status
