"""Configuration.

Two separate things live here, and keeping them separate is the point:

  * ModelConfig  - where the LLM lives. Home CPU, work NPU, does not matter.
  * RepoConfig   - how THIS repository is configured, built and tested.

The model never invents a build command. It reads the profile out of
`.local-agent.toml` at the repository root. The skill decides *when* to build.
The config decides *how*. Different responsibilities.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_NAME = ".local-agent.toml"


class ConfigError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelConfig:
    """The only thing that changes between the NUC and the Panther Lake box."""

    base_url: str = "http://127.0.0.1:8000/v3"
    model: str = "OpenVINO/Qwen3-Coder-30B-A3B-Instruct-int4-ov"
    api_key: str = "unused"
    device_note: str = "CPU"
    temperature: float = 0.2
    max_tokens: int = 2048
    # Sent on every request and recorded in identity. Gate Zero showed llama.cpp
    # defaulting to top_k 40 / top_p 0.95 / min_p 0.05 and OVMS will differ, so
    # two backends at "temperature 0.2" would otherwise be sampling differently
    # and we would call the difference a model difference. The values below are
    # what was actually running when the first measurements were taken.
    top_k: int = 40
    top_p: float = 0.95
    min_p: float = 0.05
    # Three different clocks, because a stalled server defeats any one of them.
    #   connect_timeout_s   the socket. Refused or unreachable shows up here.
    #   read_timeout_s      silence: no bytes at all for this long. Per read, so
    #                       a trickle of one token every few seconds never trips
    #                       it. That is exactly what happened on the first real
    #                       run: a 21-minute request never timed out because a
    #                       token arrived every ~6 s.
    #                       It must also survive a legitimate prefill, because
    #                       nothing is sent while the server is still reading
    #                       the prompt. On CPU the 30B prefills at ~50 tok/s
    #                       measured, so a full 12000-token context is about
    #                       four minutes of honest silence. A 120 s read
    #                       timeout would abort that valid work and file it as
    #                       server death. Profiles that prefill slowly raise
    #                       this; the deadline and the throughput floor still
    #                       bound the pathological case.
    #   request_deadline_s  wall clock for the whole request, streamed or not.
    # And a throughput floor: once decoding has started, if the rolling rate
    # stays under stall_tok_s for stall_window_s, the request is a stall and is
    # aborted as one. The 30B decodes at ~16 tok/s on the NUC, the 8B at ~7.5;
    # the stall was 0.16. Tune per profile for slow accelerators.
    connect_timeout_s: float = 10.0
    read_timeout_s: float = 120.0
    request_deadline_s: float = 600.0
    stall_tok_s: float = 1.0
    stall_window_s: float = 60.0
    # Kept for callers that still pass it; maps onto read_timeout_s.
    request_timeout_s: float = 600.0

    # Streaming is on by default because it is the only way to measure time to
    # first token, which is the number that decides whether an agent loop is
    # usable on a bandwidth-starved machine.
    stream: bool = True

    # Keep this comfortably under whatever the server will accept. On an NPU
    # pipeline compiled with a fixed MAX_PROMPT_LEN, overrunning is not an
    # error, it is garbage output, so leave headroom.
    context_budget_tokens: int = 12_000

    # Token budgets for what a single tool result, and all of them together, may
    # occupy. Bytes alone are the wrong unit: a 16 kB source or log excerpt can
    # be several thousand tokens, and three of those will eat an NPU context
    # budget alive. Set per tier, because the NPU's context is the scarce one.
    max_tool_result_tokens: int = 1_500
    max_total_tool_tokens: int = 8_000

    # Configuration identity. These do not change behaviour on their own, but
    # every report carries them, because comparing "8B with thinking on" against
    # "30B with thinking off" and blaming the model would be a rotten mistake.
    runtime: str = "ovms"          # llamacpp | ovms | cloud | genai
    runtime_version: str = ""      # e.g. b10816-427291b5b, OVMS 2026.3.0
    quant: str = ""                # e.g. int4-cw-ov, Q4_K_M, UD-Q4_K_XL
    tool_parser: str = ""          # qwen3coder | hermes3 | jinja-template
    # None means "whatever the server defaults to", which is itself a finding.
    # False actively disables Qwen3 thinking via chat_template_kwargs.
    thinking: bool | None = None

    # Which rung of the ladder this endpoint is. The orchestrator sends each
    # skill to the cheapest tier that can do its job, and escalates only when
    # the cheap one fails. See `local_agent.llm.router`.
    tier: str = "strong"

    @staticmethod
    def from_env(prefix: str = "LOCAL_AGENT_") -> "ModelConfig":
        base = ModelConfig()
        return ModelConfig(
            base_url=os.environ.get(f"{prefix}BASE_URL", base.base_url),
            model=os.environ.get(f"{prefix}MODEL", base.model),
            api_key=os.environ.get(f"{prefix}API_KEY", base.api_key),
            device_note=os.environ.get(f"{prefix}DEVICE", base.device_note),
            temperature=float(os.environ.get(f"{prefix}TEMPERATURE", base.temperature)),
            max_tokens=int(os.environ.get(f"{prefix}MAX_TOKENS", base.max_tokens)),
            request_timeout_s=float(
                os.environ.get(f"{prefix}TIMEOUT", base.request_timeout_s)
            ),
            stream=os.environ.get(f"{prefix}STREAM", "1") not in ("0", "false", "no"),
            context_budget_tokens=int(
                os.environ.get(f"{prefix}CONTEXT_BUDGET", base.context_budget_tokens)
            ),
            tier=os.environ.get(f"{prefix}TIER", base.tier),
            max_tool_result_tokens=int(
                os.environ.get(f"{prefix}MAX_TOOL_RESULT_TOKENS",
                               base.max_tool_result_tokens)
            ),
            max_total_tool_tokens=int(
                os.environ.get(f"{prefix}MAX_TOTAL_TOOL_TOKENS",
                               base.max_total_tool_tokens)
            ),
            runtime=os.environ.get(f"{prefix}RUNTIME", base.runtime),
            runtime_version=os.environ.get(f"{prefix}RUNTIME_VERSION",
                                           base.runtime_version),
            quant=os.environ.get(f"{prefix}QUANT", base.quant),
            tool_parser=os.environ.get(f"{prefix}TOOL_PARSER", base.tool_parser),
        )

    def identity(self) -> dict[str, object]:
        """Everything that must be quoted alongside any number from this endpoint."""
        return {
            "model": self.model,
            "device": self.device_note,
            "runtime": self.runtime,
            "runtime_version": self.runtime_version or "not recorded",
            "quant": self.quant,
            "tool_parser": self.tool_parser,
            "thinking": self.thinking,
            "context_budget_tokens": self.context_budget_tokens,
            "max_tool_result_tokens": self.max_tool_result_tokens,
            "temperature": self.temperature,
            "top_k": self.top_k,
            "top_p": self.top_p,
            "min_p": self.min_p,
            # All four clocks, not two of them. A run that quotes only the
            # 600s deadline invites the reading that nothing can abort sooner,
            # when in fact a stream below stall_tok_s for stall_window_s is
            # killed long before the deadline is reached.
            "connect_timeout_s": self.connect_timeout_s,
            "read_timeout_s": self.read_timeout_s,
            "request_deadline_s": self.request_deadline_s,
            "stall_tok_s": self.stall_tok_s,
            "stall_window_s": self.stall_window_s,
            "stream": self.stream,
        }


# Named presets. `local-agent --profile ptl-npu` and nothing else in the agent
# changes.
MODEL_PRESETS: dict[str, ModelConfig] = {
    "nuc-cpu-30b": ModelConfig(
        base_url="http://127.0.0.1:8000/v3",
        model="OpenVINO/Qwen3-Coder-30B-A3B-Instruct-int4-ov",
        device_note="CPU",
        runtime="ovms",
        quant="int4-ov",
        tool_parser="qwen3coder",
    ),
    "nuc-cpu-8b": ModelConfig(
        base_url="http://127.0.0.1:8001/v3",
        model="OpenVINO/Qwen3-8B-int4-cw-ov",
        device_note="CPU",
        runtime="ovms",
        quant="int4-cw-ov",
        tool_parser="hermes3",
        tier="cheap",
    ),
    # --------------------------------------------------------- llama-server
    # A first-class local backend, not a temporary hack: the agent must work
    # perfectly with no OpenVINO on the machine at all. Both profiles use port
    # 8080 because only one server runs at a time on the NUC, and the `model`
    # field is the `--alias` you pass, so it stays a stable id rather than an
    # absolute GGUF path.
    "nuc-llama-30b": ModelConfig(
        base_url="http://127.0.0.1:8080/v1",
        model="qwen3-coder-30b",
        device_note="CPU",
        runtime="llamacpp",
        runtime_version="b10816-427291b5b",
        quant="UD-Q4_K_XL",
        tool_parser="jinja-template",
        thinking=False,
        tier="strong",
        # Prefill on the NUC CPU is slower under a long context than the
        # short-prompt qualification suggests: 47.6 tok/s median in the first
        # probe, 29.6 in the second once the context reached 10.5k. A full
        # 12000-token prompt is therefore up to ~8 minutes of honest silence
        # before the first byte, and nothing is sent during it. Budget from
        # the slow measurement, not the fast one.
        read_timeout_s=480.0,
        request_deadline_s=900.0,
    ),
    "nuc-llama-8b": ModelConfig(
        base_url="http://127.0.0.1:8080/v1",
        model="qwen3-8b",
        device_note="CPU",
        runtime="llamacpp",
        runtime_version="b10816-427291b5b",
        quant="Q4_K_M",
        tool_parser="jinja-template",
        thinking=False,
        tier="cheap",
        # Prefill on the NUC CPU is slower under a long context than the
        # short-prompt qualification suggests: 47.6 tok/s median in the first
        # probe, 29.6 in the second once the context reached 10.5k. A full
        # 12000-token prompt is therefore up to ~8 minutes of honest silence
        # before the first byte, and nothing is sent during it. Budget from
        # the slow measurement, not the fast one.
        read_timeout_s=480.0,
        request_deadline_s=900.0,
    ),
    # ---------------------------------------------------------- Panther Lake
    # Core Ultra X7 358H: 16 cores (4P + 8E + 4LPE), NPU 5 at 50 TOPS int8, and
    # an Arc B390 iGPU at 122 TOPS int8. The iGPU is the bigger engine by a
    # factor of two and a half, has no static-shape prompt limit, and is the
    # device Intel documents for the 30B coder model through OVMS. It is not the
    # afterthought the NPU-first framing makes it look like.
    #
    # Run all three against the same machine and the same memory, changing one
    # thing at a time. That is the experiment; NUC against laptop is only the
    # memory-bandwidth control.
    # The NPU pipeline is static shape: prompt length is fixed when the blob is
    # compiled, and overrunning it produces garbage output rather than an error.
    # Two presets on purpose.
    #
    # Intel documents `--max_prompt_len 16384` with
    # `NPUW_LLM_PREFILL_ATTENTION_HINT: PYRAMID` for this model on NPU. But
    # their long-context guidance recommends NPU for short-to-medium contexts,
    # typically up to 8K, their published NPU prefix-cache measurements stop at
    # 8K, and a larger configured maximum costs latency and memory even for a
    # 1K request. So 8K is the daily path and 16K is the stress path, and the
    # difference between them is something to measure rather than assume.
    "ptl-npu-8b": ModelConfig(
        base_url="http://127.0.0.1:18000/v3",
        model="OpenVINO/Qwen3-8B-int4-cw-ov",
        device_note="NPU (8K)",
        context_budget_tokens=7_500,   # blob compiled at --max_prompt_len 8192
        max_tool_result_tokens=900,
        max_total_tool_tokens=4_000,
        tier="cheap",
    ),
    "ptl-npu-8b-16k": ModelConfig(
        base_url="http://127.0.0.1:18010/v3",
        model="OpenVINO/Qwen3-8B-int4-cw-ov",
        device_note="NPU (16K)",
        context_budget_tokens=15_000,  # blob compiled at --max_prompt_len 16384
        max_tool_result_tokens=1_500,
        max_total_tool_tokens=9_000,
        tier="cheap",
    ),
    # Intel's own AI PC benchmark page reports Qwen3-30B-A3B INT4 in the
    # mid to high 40s tok/s on an X7 368H, which carries the same B390 and the
    # same LPDDR5X-9600 interface. Their OVMS deployment for the Coder variant
    # wants 19 GB or more of GPU-accessible memory, which your 64 GB covers
    # comfortably. This is very likely the daily driver.
    "ptl-gpu-30b": ModelConfig(
        base_url="http://127.0.0.1:18001/v3",
        model="OpenVINO/Qwen3-Coder-30B-A3B-Instruct-int4-ov",
        device_note="GPU (Arc B390)",
        tier="strong",
    ),
    "ptl-cpu-30b": ModelConfig(
        base_url="http://127.0.0.1:18002/v3",
        model="OpenVINO/Qwen3-Coder-30B-A3B-Instruct-int4-ov",
        device_note="CPU",
    ),
    # Quality ceiling reference ONLY, and not on the critical path.
    #
    # Dense 27B, so every parameter is read per token and decode is roughly five
    # times slower than the MoE on the same memory. It fits in 64 GB and it does
    # run; it is just slow, which makes it a reference, not a daily driver.
    #
    # More importantly: the OpenVINO conversions of this model (int4 and int8
    # both) are marked EXPERIMENTAL and require OpenVINO 2026.4 nightly plus
    # nightly GenAI builds. Do not put the POC on that foundation. Run it once,
    # in a throwaway environment, to find out what score is achievable at all.
    "ceiling-27b-dense": ModelConfig(
        base_url="http://127.0.0.1:18003/v3",
        model="OpenVINO/Qwen3.8-27B-int4-ov",
        device_note="GPU (Arc B390), experimental stack",
        max_tokens=1024,
    ),
    # ---------------------------------------------------------------- cloud
    # Inert without configuration. Present so the router has somewhere to send
    # a task neither local tier could finish, and so "cloud required" stops
    # being a hypothetical column in the ledger. Set LOCAL_AGENT_CLOUD_BASE_URL
    # and LOCAL_AGENT_CLOUD_API_KEY to activate it.
    "cloud": ModelConfig(
        base_url=os.environ.get("LOCAL_AGENT_CLOUD_BASE_URL", ""),
        model=os.environ.get("LOCAL_AGENT_CLOUD_MODEL", "unset"),
        api_key=os.environ.get("LOCAL_AGENT_CLOUD_API_KEY", "unset"),
        device_note="remote",
        runtime="cloud",
        tier="strong",
    ),
}

# Models deliberately not offered as presets, with the reason, so nobody has to
# rediscover it:
#
#   Qwen3.6-35B-A3B  Intel has run this INT4 on a Panther Lake iGPU with only
#                 32 GB of RAM, so the 30B Coder on your 64 GB B390 is not a
#                 shower thought. Worth a look only after the matrix is done.
#
#   Qwen3.5-9B    Dense 9B (config.json has no MoE fields; layer_types alternate
#                 linear_attention and full_attention over 32 layers). Being
#                 dense it reads roughly 5 GB per token at int4, so it is in the
#                 same decode class as Qwen3-8B with no speed advantage to show
#                 for the risk. Its Gated DeltaNet blocks are also a known NPU
#                 problem: openvinotoolkit/openvino issue 35209 reports
#                 inconsistent tensor shapes in the gated delta rule loop on NPU.
#                 Interesting later, wrong thing to bring up a POC on.


# --------------------------------------------------------------------------
# repository
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BuildProfile:
    name: str
    configure: list[str] = field(default_factory=list)
    build: list[str] = field(default_factory=list)
    test: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Policy:
    allow_build: bool = True
    allow_test: bool = True
    allow_patch: bool = False
    allow_commit: bool = False
    command_timeout_seconds: int = 900
    max_tool_calls: int = 40
    max_repeat_calls: int = 3
    max_tool_result_bytes: int = 16_000
    max_read_bytes: int = 400_000


@dataclass(frozen=True)
class RepoConfig:
    root: Path
    name: str
    build_dir: str
    run_dir: str
    profiles: dict[str, BuildProfile]
    default_profile: str
    policy: Policy
    skills_dir: str = ".github/skills"

    def profile(self, name: str | None = None) -> BuildProfile:
        key = name or self.default_profile
        if key not in self.profiles:
            raise ConfigError(
                f"unknown build profile {key!r}; configured: {sorted(self.profiles)}"
            )
        return self.profiles[key]

    @property
    def run_path(self) -> Path:
        return self.root / self.run_dir


def _as_cmd(value: object, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ConfigError(f"{where} must be a list of strings, got {value!r}")
    return list(value)


def load_repo_config(root: Path) -> RepoConfig:
    root = root.resolve()
    path = root / DEFAULT_CONFIG_NAME
    if not path.is_file():
        raise ConfigError(
            f"no {DEFAULT_CONFIG_NAME} at {root}. The agent refuses to guess how to "
            "build a repository it has never seen."
        )

    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    repo = raw.get("repo", {})
    profiles_raw = raw.get("profiles", {})
    if not profiles_raw:
        raise ConfigError(f"{path} defines no [profiles.*] section")

    profiles: dict[str, BuildProfile] = {}
    for name, body in profiles_raw.items():
        profiles[name] = BuildProfile(
            name=name,
            configure=_as_cmd(body.get("configure"), f"profiles.{name}.configure"),
            build=_as_cmd(body.get("build"), f"profiles.{name}.build"),
            test=_as_cmd(body.get("test"), f"profiles.{name}.test"),
            env={str(k): str(v) for k, v in (body.get("env") or {}).items()},
        )

    default_profile = repo.get("default_profile") or next(iter(profiles))
    pol = raw.get("policy", {})

    return RepoConfig(
        root=root,
        name=repo.get("name", root.name),
        build_dir=repo.get("build_dir", "build"),
        run_dir=repo.get("run_dir", ".local-agent/runs"),
        profiles=profiles,
        default_profile=default_profile,
        skills_dir=repo.get("skills_dir", ".github/skills"),
        policy=Policy(
            allow_build=bool(pol.get("allow_build", True)),
            allow_test=bool(pol.get("allow_test", True)),
            allow_patch=bool(pol.get("allow_patch", False)),
            allow_commit=bool(pol.get("allow_commit", False)),
            command_timeout_seconds=int(pol.get("command_timeout_seconds", 900)),
            max_tool_calls=int(pol.get("max_tool_calls", 40)),
            max_repeat_calls=int(pol.get("max_repeat_calls", 3)),
            max_tool_result_bytes=int(pol.get("max_tool_result_bytes", 16_000)),
            max_read_bytes=int(pol.get("max_read_bytes", 400_000)),
        ),
    )


def find_repo_root(start: Path) -> Path:
    """Walk up looking for `.local-agent.toml`, then `.git`."""
    start = start.resolve()
    for candidate in (start, *start.parents):
        if (candidate / DEFAULT_CONFIG_NAME).is_file():
            return candidate
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return start
