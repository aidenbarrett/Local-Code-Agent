"""Run tests and reduce the output to evidence.

Named `testing.py` rather than `tests.py` so it cannot be confused with the
project's own test suite by any tool that walks the source tree.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .base import (
    BlockedError,
    DomainStatus,
    ExecutionStatus,
    Reason,
    Risk,
    ToolError,
    ToolRegistry,
    ToolResult,
    relpath,
)
from .context import ToolContext
from .logs import parse_test_log
from .runner import run_command

# ctest -R takes a regular expression, so the filter has to be able to be one.
# The old validator stripped |^$() and then demanded [A-Za-z0-9_.:-], which
# allowed `.` but not `*`: `.*timeout.*` is a perfectly legal ctest pattern and
# was refused as "not a simple ctest regular expression". The 30B lost four
# calls to that in the Slice 3 run, on a task it then solved anyway.
#
# The command is executed as an argument list, never through a shell, so the
# quoting risk is theoretical. It is still cheap to exclude the characters that
# would matter if a profile ever did use one, and to require that the pattern
# actually compiles, so a broken regex is refused here with a clear message
# instead of by ctest with a confusing one.
_FILTER_FORBIDDEN = re.compile(r"[;&`<>\n\r\x00\"']")
_FILTER_MAX = 200

# What a C or C++ build turns into a binary. A change to any of these makes the
# artefacts in the build directory older than the truth.
_SOURCE_SUFFIXES = (".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".txt", ".cmake")


BUILD_STAMP = ".local-agent-build-ok"
PROFILE_STAMP = ".local-agent-configured-profile"


@dataclass(frozen=True)
class BuildRecord:
    """What the last successful FULL build was, and when.

    `profile` is None for a stamp written by an older package, which said only
    "ok". Unknown provenance is treated exactly like no provenance: a stamp
    that cannot name its profile cannot support a claim about one.
    """

    profile: str | None
    at: float


def touch_build_stamp(root: Any, build_dir: str, profile: str) -> None:
    """Record that a FULL compile of `profile` succeeded just now.

    Written only by an untargeted, successful build_target. A targeted build
    says one thing is current; this stamp is read as "every source in the tree
    is represented by a current binary", and a targeted build cannot support
    that. Refreshing it after `build_target(target="test_text_util")` made an
    unrelated stale test binary look fresh, which is the tool lying to the
    agent about what it is running.

    The profile is written INTO the stamp rather than inferred from the
    configure marker beside it, because those two record different facts.
    `configure_project(profile="release")` moves the marker to release without
    compiling anything, and a run_test that trusted the marker then reported
    old debug binaries as a passing release build.
    """
    build = root / build_dir
    try:
        build.mkdir(parents=True, exist_ok=True)
        stamp = build / BUILD_STAMP
        # Two writes on purpose. The recorded time has to come from the SAME
        # clock as the source mtimes it will be compared against, which is the
        # filesystem's, not `time.time()`. They agree on an ordinary local
        # disk and can disagree on a network mount or a VM whose guest clock
        # has drifted, and a build recorded as later than it happened would
        # hide a real stale source.
        #
        # So: write, ask the filesystem what time it just used, then write that
        # into the content. The second write's own mtime is a hair later than
        # the value stored, which errs towards calling a source stale rather
        # than fresh. That is the right direction to be wrong in.
        stamp.write_text("{}\n")
        at = stamp.stat().st_mtime
        stamp.write_text(json.dumps({"profile": profile, "at": at}) + "\n")
    except OSError:  # pragma: no cover - never load-bearing
        pass


def build_record(root: Any, build_dir: str) -> BuildRecord | None:
    """The last successful full build, or None if there has not been one.

    None is the important case. It used to be silently equivalent to "nothing
    is stale", which is how a tree that had never been built by this agent
    could return a green full-suite result and have it counted as proof.
    """
    stamp = root / build_dir / BUILD_STAMP
    try:
        text = stamp.read_text()
    except OSError:
        return None
    try:
        payload = json.loads(text)
        profile, at = payload.get("profile"), payload.get("at")
    except (ValueError, AttributeError):
        return None  # legacy "ok\n" stamp: built, but by whom, when, for what?

    # The time comes out of the CONTENT, never from the file's mtime.
    #
    # Reading the mtime made the stamp forgeable by touching it, and touching a
    # file inside the repository was a tool call the model already had:
    # propose_patch a byte, apply_patch, and the staleness baseline jumped to
    # now with nothing compiled. Writes into the build directory are refused
    # outright as well, and these are two independent locks on the same door
    # because that door decides whether a result counts as proof.
    if not isinstance(at, (int, float)):
        return None
    if not isinstance(profile, str) or not profile:
        return None
    return BuildRecord(profile=profile, at=float(at))


def configured_profile(root: Any, build_dir: str) -> str | None:
    """Which profile this build directory was last configured for, if known."""
    try:
        return (root / build_dir / PROFILE_STAMP).read_text().strip() or None
    except OSError:
        return None


def set_configured_profile(root: Any, build_dir: str, name: str) -> None:
    """Record the configuration the build directory now holds.

    Both profiles share one build directory, and configure only ran when
    CMakeCache.txt was absent, so `build_target(profile="release")` on a tree
    configured for debug printed "build (release) succeeded" over a Debug
    cache. A label the tool cannot support is worse than no label. The build
    stamp is dropped at the same time: a binary compiled under the previous
    configuration is not current under this one.
    """
    build = root / build_dir
    try:
        build.mkdir(parents=True, exist_ok=True)
        (build / PROFILE_STAMP).write_text(name + "\n")
        (build / BUILD_STAMP).unlink(missing_ok=True)
    except OSError:  # pragma: no cover - never load-bearing
        pass


def _stale_sources(root: Any, build_dir: str, record: BuildRecord | None) -> list[str]:
    """Source files modified since the build directory was last written.

    ctest does not build. A model that edits a source file and reruns the test
    gets the previous binary's result, identical to the one before the edit,
    and no amount of rereading the source explains why. The 30B spent ten tool
    calls on exactly that: it fixed the defect correctly, reran, saw the same
    failure, checked the file, saw its own correct fix, and looped until the
    repeat guard stopped it.

    So the tool says it. Deterministically, from mtimes, with no guessing.
    """
    if record is None:
        # No successful full build recorded, so there is nothing to compare
        # mtimes against. This is NOT "nothing is stale": it is "freshness is
        # unknown", and the caller reports it as its own invalidating
        # condition. Returning [] here and letting the result stand as a PASS
        # is precisely how an unbuilt profile verified old binaries.
        return []
    newest_build = record.at

    stale: list[str] = []
    for path in root.rglob("*"):
        parts = path.parts
        if build_dir in parts or ".git" in parts or ".local-agent" in parts:
            continue
        if path.name in (BUILD_STAMP, PROFILE_STAMP):
            continue
        if not path.is_file() or path.suffix.lower() not in _SOURCE_SUFFIXES:
            continue
        try:
            if path.stat().st_mtime > newest_build:
                stale.append(str(path.relative_to(root)))
        except OSError:  # pragma: no cover
            continue
    return sorted(stale)


def register(reg: ToolRegistry, ctx: ToolContext) -> None:
    @reg.add(
        "run_test",
        "Run the configured test command, optionally filtered to tests whose name "
        "matches a regular expression. Returns which tests failed and why, plus a "
        "log artifact for detail.",
        {
            "type": "object",
            "properties": {
                "profile": {"type": "string"},
                "name_filter": {
                    "type": "string",
                    "description": "ctest -R regular expression. Prefer this over "
                                   "running the whole suite when reproducing one failure.",
                },
                "rerun_failed": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
        Risk.EXECUTE,
    )
    def run_test(
        profile: str | None = None,
        name_filter: str | None = None,
        rerun_failed: bool = False,
    ) -> ToolResult:
        if not ctx.repo.policy.allow_test:
            raise BlockedError("running tests is disabled by repository policy")
        prof = ctx.repo.profile(profile)
        if not prof.test:
            raise ToolError(f"profile {prof.name!r} defines no test step")

        command = list(prof.test)
        if name_filter:
            if len(name_filter) > _FILTER_MAX:
                raise ToolError(
                    f"name_filter is {len(name_filter)} characters; the limit is "
                    f"{_FILTER_MAX}. Filter to one test, not to a description of one."
                )
            if _FILTER_FORBIDDEN.search(name_filter):
                raise ToolError(
                    "name_filter may not contain shell metacharacters "
                    "(; & ` < > quotes or newlines). It is a ctest regular "
                    "expression, for example ring_buffer or ^ring_buffer$."
                )
            try:
                re.compile(name_filter)
            except re.error as exc:
                raise ToolError(
                    f"name_filter is not a valid regular expression: {exc}. "
                    "Use list_tests to see the names that exist."
                ) from exc
            command += ["-R", name_filter]
        if rerun_failed:
            command += ["--rerun-failed"]

        # Three separate questions, and answering only the first two is what
        # let a green result stand over binaries nobody had built:
        #
        #   is a full build recorded at all           record is None
        #   was that build for the profile asked for  record.profile
        #   is it older than the sources              stale
        #
        # The configure marker answers none of them. It says how the tree is
        # CONFIGURED, which is a promise about the next build, not a fact about
        # the binaries sitting in the directory now.
        record = build_record(ctx.root, ctx.repo.build_dir)
        stale = _stale_sources(ctx.root, ctx.repo.build_dir, record)
        no_build_record = record is None
        build_profile_mismatch = record is not None and record.profile != prof.name

        # Both profiles share one build directory, so asking for one and
        # getting the other is possible and silent. ctest will happily run
        # whatever binaries are there and the result would be labelled with the
        # profile that was requested rather than the one that was built.
        active = configured_profile(ctx.root, ctx.repo.build_dir)
        wrong_profile = active is not None and active != prof.name

        outcome = run_command(command, ctx.root, ctx.run_root, ctx.timeout, prof.env)
        report = parse_test_log(outcome.combined_path.read_text(errors="replace"))

        ran_nothing = bool(
            outcome.ok and not report.passed and not report.totals.get("total")
        )

        if outcome.timed_out:
            summary = (
                f"the orchestrator killed the test run after its own {ctx.timeout}s "
                "budget expired; this is not a ctest timeout"
            )
        elif ran_nothing:
            # ctest is happy to run nothing and exit 0. With a name_filter that
            # matches no test, "all tests passed (0 test(s))" is true and
            # useless, and it is the same lie as the unbuilt tree: nothing ran,
            # and the tool implied everything is fine. The 30B was sent down
            # this hole twice in the Slice 3 run, on `Crash` and on `timeout`,
            # and spent four calls climbing back out.
            no_match = (
                f"no test matched {name_filter!r}" if name_filter
                else "the suite contains no tests"
            )
            summary = (
                f"ctest ran 0 tests and exited 0: {no_match}. Nothing was "
                "verified. Use list_tests to see the names that exist."
            )
        elif outcome.ok:
            summary = (
                f"all tests passed ({report.totals.get('total', len(report.passed))} "
                f"test(s)) in {outcome.elapsed_s:.1f}s"
            )
        elif not report.failed and not report.passed:
            # ctest exited non-zero and found nothing to run. That is not a
            # test failure, it is an unbuilt or unconfigured tree, and a model
            # told "0 test(s) FAILED" will run the tests again instead of
            # building. Say what actually happened.
            summary = (
                f"ctest ran no tests (exit {outcome.exit_code}): the project is not "
                "configured or not built. Run configure_project and build_target first."
            )
        else:
            names = ", ".join(f["name"] for f in report.failed[:5]) or "see log"
            summary = f"{len(report.failed)} test(s) FAILED: {names}"

        # Every one of these means the binaries ctest just executed are not the
        # binaries this source tree would produce. Prepended in reverse order
        # of severity so the most fundamental ends up first, in front of a
        # result that may be a fossil.
        if wrong_profile:
            summary = (
                f"PROFILE MISMATCH: the build directory is configured for "
                f"{active!r}, not {prof.name!r}, and ctest does not configure or "
                f"compile. Nothing about {prof.name!r} was verified. Run "
                f"configure_project and build_target for {prof.name!r} first. "
                f"-- {summary}"
            )
        if stale:
            shown = ", ".join(stale[:3]) + (f" and {len(stale) - 3} more" if len(stale) > 3 else "")
            summary = (
                f"STALE: {len(stale)} source file(s) changed since the last build "
                f"({shown}); ctest does not compile, so this result is from the OLD "
                f"binary. Run build_target, then run_test again. -- {summary}"
            )
        if build_profile_mismatch:
            built = record.profile if record and record.profile else "an unknown profile"
            summary = (
                f"NOT BUILT FOR {prof.name!r}: the last successful full build was "
                f"for {built}. Configuring for a profile is not building it, so "
                f"these are the previous profile's binaries and nothing about "
                f"{prof.name!r} was verified. Run build_target(profile="
                f"{prof.name!r}) first. -- {summary}"
            )
        if no_build_record:
            summary = (
                "NO BUILD RECORDED: no successful full build of this tree has "
                "been observed, so there is no way to tell whether these "
                "binaries match the source. ctest does not compile. Run "
                f"build_target(profile={prof.name!r}), then run_test again. "
                f"-- {summary}"
            )

        # Anything that makes the executed binaries unrepresentative of the
        # tree. The prose used to say all of this while the typed result still
        # said PASS, which meant the orchestrator recorded a verified run that
        # the evaluator then rejected. The type has to say it too.
        unproven = wrong_profile or bool(stale) or build_profile_mismatch or no_build_record

        # A ctest TIMEOUT is a real, reportable test result. Our runner running
        # out of patience is not: it tells you nothing about the code.
        return ToolResult(
            ok=outcome.ok and not ran_nothing and not unproven,
            exit_code=outcome.exit_code,
            summary=summary,
            artifacts=[relpath(ctx.root, outcome.combined_path)],
            execution_status=(
                ExecutionStatus.ERROR if outcome.timed_out else ExecutionStatus.OK
            ),
            domain_status=(
                # Nothing ran is not a pass. It is not a failure either: the
                # code under test was never exercised, so the honest answer is
                # that we do not know. This also stops an empty run satisfying
                # a verification contract.
                #
                # `unproven` joins it, PASS and FAIL alike. A stale suite that
                # comes back red is no more informative than one that comes
                # back green: both describe a binary that no longer exists in
                # source form. Reporting the red one as a FAIL would send a
                # diagnosis after a defect that may already be fixed.
                DomainStatus.UNKNOWN if (outcome.timed_out or ran_nothing or unproven)
                else (DomainStatus.PASS if outcome.ok else DomainStatus.FAIL)
            ),
            reason=(
                Reason.ORCHESTRATOR_TIMEOUT if outcome.timed_out
                else Reason.NO_BUILD_RECORD if no_build_record
                else Reason.PROFILE_MISMATCH if (build_profile_mismatch or wrong_profile)
                else Reason.STALE_BINARY if stale
                else None
            ),
            data={
                "command": command,
                "profile": prof.name,
                "elapsed_s": round(outcome.elapsed_s, 2),
                "killed_by_orchestrator": outcome.timed_out,
                "stale_sources": stale,
                "ran_nothing": ran_nothing,
                "configured_profile": active,
                "profile_mismatch": wrong_profile,
                "no_build_record": no_build_record,
                "build_profile_mismatch": build_profile_mismatch,
                "built_profile": record.profile if record else None,
                "ctest_reported_timeouts": [
                    f["name"] for f in report.failed if f["status"] == "Timeout"
                ],
                **report.as_dict(),
            },
        )

    @reg.add(
        "list_tests",
        "List the tests the configured test runner knows about, without executing "
        "them.",
        {
            "type": "object",
            "properties": {"profile": {"type": "string"}},
            "additionalProperties": False,
        },
        Risk.EXECUTE,
    )
    def list_tests(profile: str | None = None) -> ToolResult:
        prof = ctx.repo.profile(profile)
        if not prof.test:
            raise ToolError(f"profile {prof.name!r} defines no test step")
        outcome = run_command(
            [*prof.test, "-N"], ctx.root, ctx.run_root, ctx.timeout, prof.env
        )
        text = outcome.combined_path.read_text(errors="replace")
        names = re.findall(r"^\s*Test\s+#\d+:\s+(\S+)", text, flags=re.MULTILINE)
        return ToolResult(
            ok=outcome.ok,
            exit_code=outcome.exit_code,
            summary=f"{len(names)} test(s) registered",
            data={"tests": names},
        )
