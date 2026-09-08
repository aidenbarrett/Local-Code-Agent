"""Configure and build.

The model chooses *whether* to build and *which* profile. It never chooses the
command line. That comes from `.local-agent.toml`, which is the only thing that
changes when this agent moves from a synthetic sandbox to a real work tree.
"""

from __future__ import annotations

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
from .testing import configured_profile, set_configured_profile, touch_build_stamp
from .logs import parse_build_log
from .runner import run_command


def _profile_names(ctx: ToolContext) -> list[str]:
    return sorted(ctx.repo.profiles)


def register(reg: ToolRegistry, ctx: ToolContext) -> None:
    profiles = _profile_names(ctx)

    @reg.add(
        "configure_project",
        "Run the configured CMake configure step for a build profile. Only needed "
        "when the build directory is missing or the configuration changed.",
        {
            "type": "object",
            "properties": {
                "profile": {"type": "string", "enum": profiles or None} if profiles
                else {"type": "string"},
            },
            "additionalProperties": False,
        },
        Risk.EXECUTE,
    )
    def configure_project(profile: str | None = None) -> ToolResult:
        if not ctx.repo.policy.allow_build:
            raise BlockedError("building is disabled by repository policy")
        prof = ctx.repo.profile(profile)
        if not prof.configure:
            raise ToolError(f"profile {prof.name!r} defines no configure step")

        outcome = run_command(
            prof.configure, ctx.root, ctx.run_root, ctx.timeout, prof.env
        )
        if outcome.ok:
            set_configured_profile(ctx.root, ctx.repo.build_dir, prof.name)
        report = parse_build_log(outcome.combined_path.read_text(errors="replace"))
        return ToolResult(
            ok=outcome.ok,
            exit_code=outcome.exit_code,
            summary=(
                f"configure ({prof.name}) "
                + ("succeeded" if outcome.ok else "FAILED")
                + f" in {outcome.elapsed_s:.1f}s"
            ),
            artifacts=[relpath(ctx.root, outcome.combined_path)],
            data={
                "command": prof.configure,
                "timed_out": outcome.timed_out,
                **report.as_dict(),
            },
        )

    @reg.add(
        "build_target",
        "Run the configured build. Returns structured compiler and linker "
        "diagnostics, not the raw log. Page the log artifact if you need context "
        "around a diagnostic.",
        {
            "type": "object",
            "properties": {
                "profile": {"type": "string"},
                "target": {
                    "type": "string",
                    "description": "Optional single target; builds everything if omitted.",
                },
            },
            "additionalProperties": False,
        },
        Risk.EXECUTE,
    )
    def build_target(profile: str | None = None, target: str | None = None) -> ToolResult:
        if not ctx.repo.policy.allow_build:
            raise BlockedError("building is disabled by repository policy")
        prof = ctx.repo.profile(profile)
        if not prof.build:
            raise ToolError(f"profile {prof.name!r} defines no build step")

        command = list(prof.build)
        if target:
            if not target.replace("_", "").replace("-", "").replace(".", "").isalnum():
                raise ToolError("target name must be a plain identifier")
            command += ["--target", target]

        # Configure on demand rather than making the model remember to, and
        # also when the tree is configured for a DIFFERENT profile. Both
        # profiles share one build directory, so without this the tool prints
        # "build (release) succeeded" over a cache that still says Debug.
        #
        # `active is None` with a cache present is unknown provenance, and it
        # reconfigures too. A CMakeCache.txt with no marker beside it can be a
        # human's build tree, a tree left by an older package, or a marker
        # someone deleted; in all three the cache may hold any profile at all,
        # and taking it on trust let a request for release compile against a
        # debug cache and label the result release.
        active = configured_profile(ctx.root, ctx.repo.build_dir)
        cache = (ctx.root / ctx.repo.build_dir / "CMakeCache.txt").is_file()
        needs_configure = not cache or active is None or active != prof.name
        if prof.configure and needs_configure:
            pre = run_command(prof.configure, ctx.root, ctx.run_root, ctx.timeout, prof.env)
            if pre.ok:
                set_configured_profile(ctx.root, ctx.repo.build_dir, prof.name)
            if not pre.ok:
                report = parse_build_log(pre.combined_path.read_text(errors="replace"))
                return ToolResult(
                    ok=False,
                    exit_code=pre.exit_code,
                    summary="configure step failed before the build could start",
                    artifacts=[relpath(ctx.root, pre.combined_path)],
                    data={"command": prof.configure, **report.as_dict()},
                )

        outcome = run_command(command, ctx.root, ctx.run_root, ctx.timeout, prof.env)
        report = parse_build_log(outcome.combined_path.read_text(errors="replace"))

        if outcome.ok and not outcome.timed_out and not target:
            # Untargeted only. The stamp means "every source is represented by
            # a current binary", which a targeted build cannot support.
            #
            # The only thing in the tree that says "a compile succeeded at this
            # moment". run_test compares source mtimes against it.
            #
            # The obvious alternative, the newest mtime anywhere under build/,
            # is wrong: ctest writes build/Testing/LastTest.log every time it
            # runs, so editing a source and then running the tests twice made
            # the staleness warning vanish with nothing recompiled. A timestamp
            # written by the test runner is not evidence of a build.
            #
            # The profile goes in the stamp. run_test needs to know not just
            # that a full build succeeded but which profile it produced, and
            # the configure marker cannot answer that: configure moves the
            # marker without compiling anything.
            touch_build_stamp(ctx.root, ctx.repo.build_dir, prof.name)

        if outcome.timed_out:
            summary = f"build TIMED OUT after {ctx.timeout}s"
        elif outcome.ok:
            summary = (
                f"build ({prof.name}) succeeded in {outcome.elapsed_s:.1f}s with "
                f"{len(report.warnings)} warning(s)"
            )
        else:
            summary = (
                f"build ({prof.name}) FAILED with {len(report.errors)} compiler "
                f"error(s) and {len(report.link_errors)} link error(s)"
            )

        # Our own wall clock killing the build is an orchestrator fact, not a
        # statement about the code. A build that fails to compile is evidence.
        return ToolResult(
            ok=outcome.ok,
            exit_code=outcome.exit_code,
            summary=summary,
            artifacts=[relpath(ctx.root, outcome.combined_path)],
            execution_status=(
                ExecutionStatus.ERROR if outcome.timed_out else ExecutionStatus.OK
            ),
            domain_status=(
                DomainStatus.UNKNOWN if outcome.timed_out
                else (DomainStatus.PASS if outcome.ok else DomainStatus.FAIL)
            ),
            reason=Reason.ORCHESTRATOR_TIMEOUT if outcome.timed_out else None,
            data={
                "command": command,
                "profile": prof.name,
                "elapsed_s": round(outcome.elapsed_s, 2),
                "killed_by_orchestrator": outcome.timed_out,
                **report.as_dict(),
            },
        )
