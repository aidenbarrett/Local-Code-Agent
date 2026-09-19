from .orchestrator import RunResult, dump_transcript, format_report
from .contracts import Orchestrator as _ContractOrchestrator
from .outcome import WorkerOutcome
from .policy import (
    PolicyAction,
    PolicyDecision,
    PolicyEngine,
    cli_approval,
    deny_all_approvals,
)
from .skills import Skill, SkillLibrary, default_search_path
from .context import CompactionEvent, ContextManager
from .state import AgentState, Phase, RunMetrics, ToolCallRecord

# Product-facing name. The underlying class remains named Orchestrator while the
# Generation-2 base-prompt fingerprint explicitly selects methods from that
# class. Keeping the frozen class source intact avoids buying a generation
# boundary for a product vocabulary cleanup.
TaskWorker = _ContractOrchestrator

# Narrow compatibility for existing explicit imports during PR-E migration.
# Do not advertise this as the product vocabulary.
Orchestrator = _ContractOrchestrator

__all__ = [
    "TaskWorker",
    "RunResult",
    "format_report",
    "dump_transcript",
    "PolicyEngine",
    "PolicyAction",
    "PolicyDecision",
    "WorkerOutcome",
    "cli_approval",
    "deny_all_approvals",
    "Skill",
    "SkillLibrary",
    "default_search_path",
    "ContextManager",
    "CompactionEvent",
    "RunMetrics",
    "AgentState",
    "Phase",
    "ToolCallRecord",
]
