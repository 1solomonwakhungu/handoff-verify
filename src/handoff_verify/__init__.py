"""handoff-verify: deterministic, zero-LLM claim and scope verification."""

from __future__ import annotations

from handoff_verify.models import (
    AssistantMessage,
    Claim,
    CommandEvent,
    FileEditEvent,
    FileReadEvent,
    Finding,
    PaginationEvent,
    ParsedTranscript,
    ScopeReceipt,
    SearchEvent,
    ToolEvent,
    VerificationReport,
)
from handoff_verify.parsers import auto_parse, detect_format
from handoff_verify.pipeline import verify_transcript

__version__ = "0.1.1"

__all__ = [
    "__version__",
    "AssistantMessage",
    "Claim",
    "CommandEvent",
    "FileEditEvent",
    "FileReadEvent",
    "Finding",
    "PaginationEvent",
    "ParsedTranscript",
    "ScopeReceipt",
    "SearchEvent",
    "ToolEvent",
    "VerificationReport",
    "auto_parse",
    "detect_format",
    "verify_transcript",
]
