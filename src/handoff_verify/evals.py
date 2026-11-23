"""Seeded evaluation suite runner and formatters for handoff-verify."""

from __future__ import annotations

import json
import math
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from handoff_verify.models import VerificationReport
from handoff_verify.parsers import auto_parse
from handoff_verify.pipeline import verify_transcript

EvalOutcome = Literal["TP", "TN", "FP", "FN"]

DEFAULT_RECALL_TARGET = 0.90
DEFAULT_MAX_FP = 0


class EvalSuiteError(ValueError):
    """Raised when an eval suite directory, manifest, or case file is missing or invalid."""


@dataclass(frozen=True)
class EvalCaseSpec:
    """Single seeded evaluation case specification loaded from manifest.json."""

    id: str
    file: str
    category: str
    expected_pass: bool
    expected_codes: tuple[str, ...]
    format: str
    description: str


@dataclass
class EvalCaseResult:
    """Verification outcome and timing for a single eval case."""

    case_id: str
    file: str
    category: str
    expected_format: str
    detected_format: str
    description: str
    expected_pass: bool
    actual_pass: bool
    expected_codes: list[str]
    actual_codes: list[str]
    codes_matched: bool
    outcome: EvalOutcome
    claims_count: int
    findings_count: int
    duration_ms: float
    report: VerificationReport | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.case_id,
            "file": self.file,
            "category": self.category,
            "expected_format": self.expected_format,
            "detected_format": self.detected_format,
            "description": self.description,
            "expected_pass": self.expected_pass,
            "actual_pass": self.actual_pass,
            "expected_codes": list(self.expected_codes),
            "actual_codes": list(self.actual_codes),
            "codes_matched": self.codes_matched,
            "outcome": self.outcome,
            "claims_count": self.claims_count,
            "findings_count": self.findings_count,
            "duration_ms": self.duration_ms,
        }


@dataclass
class EvalCategorySummary:
    """Aggregated eval counts for a single category."""

    category: str
    total: int = 0
    clean_total: int = 0
    failure_total: int = 0
    tp: int = 0
    tn: int = 0
    fp: int = 0
    fn: int = 0
    codes_matched: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "total": self.total,
            "clean_total": self.clean_total,
            "failure_total": self.failure_total,
            "tp": self.tp,
            "tn": self.tn,
            "fp": self.fp,
            "fn": self.fn,
            "codes_matched": self.codes_matched,
        }


@dataclass
class EvalSummary:
    """Aggregate precision, recall, latency, and per-category breakdown for an eval run."""

    cases_dir: str
    strict: bool
    total: int
    clean_total: int
    failure_total: int
    tp: int
    tn: int
    fp: int
    fn: int
    precision: float
    recall: float
    false_positive_rate: float
    expected_code_match_rate: float
    median_ms: float
    p95_ms: float
    max_ms: float
    passed_targets: bool
    by_category: dict[str, EvalCategorySummary]
    results: list[EvalCaseResult]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "cases_dir": self.cases_dir,
            "strict": self.strict,
            "passed_targets": self.passed_targets,
            "targets": {
                "max_false_positives": DEFAULT_MAX_FP,
                "min_recall": DEFAULT_RECALL_TARGET,
            },
            "metrics": {
                "total": self.total,
                "clean_total": self.clean_total,
                "failure_total": self.failure_total,
                "tp": self.tp,
                "tn": self.tn,
                "fp": self.fp,
                "fn": self.fn,
                "precision": self.precision,
                "recall": self.recall,
                "false_positive_rate": self.false_positive_rate,
                "expected_code_match_rate": self.expected_code_match_rate,
                "median_ms": self.median_ms,
                "p95_ms": self.p95_ms,
                "max_ms": self.max_ms,
            },
            "by_category": {k: v.to_dict() for k, v in self.by_category.items()},
            "results": [r.to_dict() for r in self.results],
        }


def resolve_evals_dir(cases_dir: Path | None = None) -> Path:
    """Resolve the evals directory containing ``manifest.json``.

    Search order when ``cases_dir`` is omitted:
    1. ``./evals`` relative to the current working directory.
    2. ``<repo_root>/evals`` relative to the installed ``handoff_verify`` package.
    """
    if cases_dir is not None:
        candidate = cases_dir.expanduser().resolve()
        if (candidate / "manifest.json").is_file():
            return candidate
        # Allow passing `evals/cases` directly when `evals/manifest.json` sits in parent
        if candidate.name == "cases" and (candidate.parent / "manifest.json").is_file():
            return candidate.parent
        raise EvalSuiteError(
            f"eval manifest not found in {candidate} (expected {candidate / 'manifest.json'})"
        )

    cwd_evals = (Path.cwd() / "evals").resolve()
    if (cwd_evals / "manifest.json").is_file():
        return cwd_evals

    pkg_path = Path(__file__).resolve()
    for parent in pkg_path.parents[:4]:
        candidate = parent / "evals"
        if (candidate / "manifest.json").is_file():
            return candidate

    raise EvalSuiteError(
        "could not locate evals/manifest.json (checked ./evals and package root); "
        "pass --cases-dir <path>"
    )


def load_eval_manifest(evals_dir: Path) -> list[EvalCaseSpec]:
    """Load and validate ``manifest.json`` from ``evals_dir``."""
    manifest_path = evals_dir / "manifest.json"
    if not manifest_path.is_file():
        raise EvalSuiteError(f"eval manifest not found: {manifest_path}")

    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvalSuiteError(f"failed to load eval manifest {manifest_path}: {exc}") from exc

    if isinstance(raw, dict):
        raw_cases = raw.get("cases")
    elif isinstance(raw, list):
        raw_cases = raw
    else:
        raise EvalSuiteError(f"invalid eval manifest {manifest_path}: expected object or list")

    if not isinstance(raw_cases, list) or not raw_cases:
        raise EvalSuiteError(
            f"invalid eval manifest {manifest_path}: 'cases' must be a non-empty list"
        )

    specs: list[EvalCaseSpec] = []
    seen_ids: set[str] = set()
    resolved_root = evals_dir.resolve()

    for idx, item in enumerate(raw_cases):
        if not isinstance(item, dict):
            raise EvalSuiteError(f"manifest case #{idx} must be a JSON object")

        case_id = item.get("id")
        rel_file = item.get("file")
        category = item.get("category")
        expected_pass = item.get("expected_pass")
        expected_codes = item.get("expected_codes")
        fmt = item.get("format")
        description = item.get("description")

        if not isinstance(case_id, str) or not case_id.strip():
            raise EvalSuiteError(f"manifest case #{idx} missing non-empty string 'id'")
        if case_id in seen_ids:
            raise EvalSuiteError(f"duplicate eval case id in manifest: {case_id!r}")
        seen_ids.add(case_id)

        if not isinstance(rel_file, str) or not rel_file.strip():
            raise EvalSuiteError(f"manifest case {case_id!r} missing non-empty string 'file'")
        if not isinstance(category, str) or not category.strip():
            raise EvalSuiteError(f"manifest case {case_id!r} missing non-empty string 'category'")
        if not isinstance(expected_pass, bool):
            raise EvalSuiteError(f"manifest case {case_id!r} 'expected_pass' must be boolean")
        if not isinstance(expected_codes, list) or not all(
            isinstance(c, str) and c.strip() for c in expected_codes
        ):
            raise EvalSuiteError(
                f"manifest case {case_id!r} 'expected_codes' must be a list of strings"
            )
        if expected_pass and expected_codes:
            raise EvalSuiteError(
                f"manifest case {case_id!r} has expected_pass=true with non-empty expected_codes"
            )
        if not isinstance(fmt, str) or fmt not in ("claude_code", "codex", "generic"):
            raise EvalSuiteError(
                f"manifest case {case_id!r} 'format' must be claude_code, codex, or generic"
            )
        if not isinstance(description, str) or not description.strip():
            raise EvalSuiteError(
                f"manifest case {case_id!r} missing non-empty string 'description'"
            )

        case_path = (resolved_root / rel_file).resolve()
        try:
            case_path.relative_to(resolved_root)
        except ValueError as exc:
            raise EvalSuiteError(
                f"manifest case {case_id!r} path escapes evals dir: {rel_file!r}"
            ) from exc
        if not case_path.is_file():
            raise EvalSuiteError(f"manifest case {case_id!r} file not found: {case_path}")

        specs.append(
            EvalCaseSpec(
                id=case_id.strip(),
                file=rel_file.strip(),
                category=category.strip(),
                expected_pass=expected_pass,
                expected_codes=tuple(c.strip() for c in expected_codes),
                format=fmt,
                description=description.strip(),
            )
        )

    return specs


def _percentile_ms(values: list[float], pct: float) -> float:
    """Compute nearest-rank percentile for a non-empty list of durations."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil((pct / 100.0) * len(ordered)))
    return round(ordered[min(len(ordered) - 1, rank - 1)], 3)


def run_eval_suite(
    cases_dir: Path | None = None,
    *,
    strict: bool = False,
) -> EvalSummary:
    """Run every case in the seeded eval suite and compute precision, recall, and latency."""
    evals_dir = resolve_evals_dir(cases_dir)
    specs = load_eval_manifest(evals_dir)

    results: list[EvalCaseResult] = []
    durations: list[float] = []
    by_category: dict[str, EvalCategorySummary] = {}

    tp = tn = fp = fn = 0
    clean_total = failure_total = 0
    failure_codes_matched = 0

    for spec in specs:
        case_path = (evals_dir / spec.file).resolve()

        t0 = time.perf_counter()
        transcript = auto_parse(case_path)
        if not transcript.events and not transcript.assistant_messages:
            raise EvalSuiteError(
                f"eval case {spec.id!r} has no recognizable transcript events or assistant "
                f"messages: {case_path}"
            )
        report = verify_transcript(transcript, strict=strict)
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 3)
        durations.append(elapsed_ms)

        actual_pass = report.passed
        actual_codes = sorted({f.code for f in report.findings})

        cat = by_category.setdefault(spec.category, EvalCategorySummary(category=spec.category))
        cat.total += 1

        if spec.expected_pass:
            clean_total += 1
            cat.clean_total += 1
            if actual_pass:
                outcome: EvalOutcome = "TN"
                tn += 1
                cat.tn += 1
            else:
                outcome = "FP"
                fp += 1
                cat.fp += 1
            codes_matched = actual_pass and not actual_codes
        else:
            failure_total += 1
            cat.failure_total += 1
            if not actual_pass:
                outcome = "TP"
                tp += 1
                cat.tp += 1
            else:
                outcome = "FN"
                fn += 1
                cat.fn += 1
            expected_set = set(spec.expected_codes)
            codes_matched = (not actual_pass) and expected_set.issubset(set(actual_codes))
            if codes_matched:
                failure_codes_matched += 1
                cat.codes_matched += 1

        results.append(
            EvalCaseResult(
                case_id=spec.id,
                file=spec.file,
                category=spec.category,
                expected_format=spec.format,
                detected_format=transcript.source_format,
                description=spec.description,
                expected_pass=spec.expected_pass,
                actual_pass=actual_pass,
                expected_codes=list(spec.expected_codes),
                actual_codes=actual_codes,
                codes_matched=codes_matched,
                outcome=outcome,
                claims_count=len(report.claims),
                findings_count=len(report.findings),
                duration_ms=elapsed_ms,
                report=report,
            )
        )

    total = len(results)
    precision = round(tp / (tp + fp), 4) if (tp + fp) > 0 else (1.0 if fp == 0 else 0.0)

    recall = round(tp / (tp + fn), 4) if (tp + fn) > 0 else 1.0
    false_positive_rate = round(fp / clean_total, 4) if clean_total > 0 else 0.0
    expected_code_match_rate = (
        round(failure_codes_matched / failure_total, 4) if failure_total > 0 else 1.0
    )

    median_ms = round(statistics.median(durations), 3) if durations else 0.0
    p95_ms = _percentile_ms(durations, 95.0)
    max_ms = round(max(durations), 3) if durations else 0.0

    passed_targets = (fp <= DEFAULT_MAX_FP) and (
        recall >= DEFAULT_RECALL_TARGET if failure_total > 0 else True
    )

    return EvalSummary(
        cases_dir=str(evals_dir),
        strict=strict,
        total=total,
        clean_total=clean_total,
        failure_total=failure_total,
        tp=tp,
        tn=tn,
        fp=fp,
        fn=fn,
        precision=precision,
        recall=recall,
        false_positive_rate=false_positive_rate,
        expected_code_match_rate=expected_code_match_rate,
        median_ms=median_ms,
        p95_ms=p95_ms,
        max_ms=max_ms,
        passed_targets=passed_targets,
        by_category=by_category,
        results=results,
    )


def format_eval_json(summary: EvalSummary) -> str:
    """Format an EvalSummary as stable indented JSON."""
    return json.dumps(summary.to_dict(), indent=2)


def format_eval_text(summary: EvalSummary, *, verbose: bool = False) -> str:
    """Format an EvalSummary as concise plain text."""
    status = "PASS" if summary.passed_targets else "FAIL"
    lines = [
        f"EVAL {status}: {summary.total} cases ({summary.clean_total} clean, "
        f"{summary.failure_total} planted failures)",
        f"  Precision:        {summary.precision * 100:.1f}% "
        f"(TP={summary.tp}, FP={summary.fp}, TN={summary.tn}, FN={summary.fn})",
        f"  Recall:           {summary.recall * 100:.1f}% ({summary.tp}/{summary.failure_total})",
        f"  False positive:   {summary.false_positive_rate * 100:.1f}% "
        f"({summary.fp}/{summary.clean_total})",
        f"  Code match rate:  {summary.expected_code_match_rate * 100:.1f}%",
        f"  Latency:          median={summary.median_ms:.2f}ms, "
        f"p95={summary.p95_ms:.2f}ms, max={summary.max_ms:.2f}ms",
        "",
        "By category:",
    ]
    for name in sorted(summary.by_category):
        cat = summary.by_category[name]
        lines.append(
            f"  - {name:<22} total={cat.total:<2} "
            f"(clean={cat.clean_total}, fail={cat.failure_total}) "
            f"TP={cat.tp} TN={cat.tn} FP={cat.fp} FN={cat.fn}"
        )

    mismatches = [r for r in summary.results if r.outcome in ("FP", "FN") or not r.codes_matched]
    if verbose:
        lines.append("")
        lines.append("Case results:")
        for r in summary.results:
            exp_label = "CLEAN" if r.expected_pass else f"FAIL({','.join(r.expected_codes)})"
            got_label = "PASS" if r.actual_pass else f"FAIL({','.join(r.actual_codes)})"
            code_mark = "ok" if r.codes_matched else "code-mismatch"
            lines.append(
                f"  [{r.outcome}] {r.case_id:<44} {r.detected_format:<11} "
                f"exp={exp_label:<32} got={got_label:<32} ({code_mark}, {r.duration_ms:.2f}ms)"
            )
    elif mismatches:
        lines.append("")
        lines.append("Mismatches:")
        for r in mismatches:
            lines.append(
                f"  [{r.outcome}] {r.case_id}: expected_pass={r.expected_pass} "
                f"expected_codes={r.expected_codes} actual_pass={r.actual_pass} "
                f"actual_codes={r.actual_codes}"
            )

    return "\n".join(lines)


def format_eval_markdown(summary: EvalSummary, *, verbose: bool = False) -> str:
    """Format an EvalSummary as clean GitHub-flavored Markdown."""
    status = "PASS" if summary.passed_targets else "FAIL"
    cases_val = f"{summary.total} ({summary.clean_total} clean / {summary.failure_total} failures)"
    prec_val = f"{summary.precision * 100:.1f}% ({summary.tp} TP, {summary.fp} FP)"
    rec_val = f"{summary.recall * 100:.1f}% ({summary.tp}/{summary.failure_total})"
    fpr_val = f"{summary.false_positive_rate * 100:.1f}% ({summary.fp}/{summary.clean_total})"
    lines = [
        f"### handoff-verify Eval Suite: {status}",
        "",
        "| Metric | Value | Target |",
        "| --- | ---: | ---: |",
        f"| Cases | {cases_val} | — |",
        f"| Precision | {prec_val} | 100.0% |",
        f"| Recall | {rec_val} | >= 90.0% |",
        f"| False Positive Rate | {fpr_val} | 0.0% |",
        f"| Expected Code Match | {summary.expected_code_match_rate * 100:.1f}% | 100.0% |",
        f"| Median Latency | {summary.median_ms:.2f} ms | < 50.0 ms |",
        f"| P95 / Max Latency | {summary.p95_ms:.2f} ms / {summary.max_ms:.2f} ms | — |",
        "",
        "#### Breakdown by Category",
        "",
        "| Category | Total | Clean | Failures | TP | TN | FP | FN |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name in sorted(summary.by_category):
        cat = summary.by_category[name]
        lines.append(
            f"| `{name}` | {cat.total} | {cat.clean_total} | {cat.failure_total} "
            f"| {cat.tp} | {cat.tn} | {cat.fp} | {cat.fn} |"
        )

    rows_to_show = (
        summary.results
        if verbose
        else [r for r in summary.results if r.outcome in ("FP", "FN") or not r.codes_matched]
    )
    if rows_to_show:
        heading = "#### All Cases" if verbose else "#### Mismatches"
        lines.extend(
            [
                "",
                heading,
                "",
                "| Case | Format | Outcome | Expected Codes | Actual Codes | Latency |",
                "| --- | --- | --- | --- | --- | ---: |",
            ]
        )
        for r in rows_to_show:
            exp_codes = ", ".join(f"`{c}`" for c in r.expected_codes) or "—"
            act_codes = ", ".join(f"`{c}`" for c in r.actual_codes) or "—"
            lines.append(
                f"| `{r.case_id}` | `{r.detected_format}` | **{r.outcome}** "
                f"| {exp_codes} | {act_codes} | {r.duration_ms:.2f} ms |"
            )

    return "\n".join(lines)


def format_eval_summary(
    summary: EvalSummary,
    fmt: str = "text",
    *,
    verbose: bool = False,
) -> str:
    """Dispatch eval summary formatting to text, markdown, or json."""
    if fmt == "json":
        return format_eval_json(summary)
    if fmt == "markdown":
        return format_eval_markdown(summary, verbose=verbose)
    if fmt == "text":
        return format_eval_text(summary, verbose=verbose)
    raise ValueError(f"Unsupported output format: {fmt!r}")
