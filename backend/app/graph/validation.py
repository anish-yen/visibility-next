"""Step 6: deterministic validation of rewrite artifacts.

Deliberately contains NO prompts and makes NO model calls. Every check below
is a string/set operation over the artifact and the facts the crawl already
established, which means:
  - it adds zero new prompt text to the product, and
  - its verdicts are reproducible, so a failed artifact fails identically on
    every run instead of depending on a sampling temperature.

What it does NOT do: judge whether the copy is persuasive, well written, or
on-brand. That is a human judgement and belongs to the review gate in step 7.
The one substantive thing it does judge is fabrication risk: a numeric claim
that appears nowhere in the crawled site and is not wrapped in a
[CONFIRM: ...] placeholder is treated as invented, because the rewrite
engine's hard rule is that unverified facts become placeholders.
"""

from __future__ import annotations

import re
from typing import Any

from app.graph.state import MAX_UNRESOLVED_PLACEHOLDERS

__all__ = ["validate_artifact", "validate_artifacts", "extract_numeric_claims"]

_CONFIRM_RE = re.compile(r"\[CONFIRM:[^\]]*\]", re.IGNORECASE)
_OPEN_CONFIRM_RE = re.compile(r"\[CONFIRM:(?![^\[]*\])", re.IGNORECASE)
# Numbers that make a factual claim. Bare small integers (list numbering,
# "3 steps") are ignored; percentages, money, multipliers, large counts and
# year-like figures are the ones that assert something checkable.
_NUMERIC_CLAIM_RE = re.compile(
    r"(?:\$\s?\d[\d,.]*\s?[kmb]?\b"      # $5, $1,200, $4k
    r"|\d[\d,.]*\s?%"                     # 40%, 12.5 %
    r"|\d[\d,.]*\s?x\b"                   # 3x
    r"|\b\d{3,}[\d,.]*\b"                 # 500, 500,000
    r"|\b\d[\d,.]*\s?(?:million|billion|thousand)\b)",
    re.IGNORECASE,
)
_TEMPLATE_LEFTOVERS = (
    "lorem ipsum", "todo", "tbd", "xxx", "<brand>", "<company>",
    "{{", "}}", "insert ", "your product here",
)


def _normalize_number(raw: str) -> str:
    return re.sub(r"[\s,]", "", raw).lower().rstrip(".")


def extract_numeric_claims(text: str) -> list[str]:
    """Numeric claims outside any [CONFIRM: ...] placeholder.

    Numbers inside a placeholder are explicitly flagged as unverified by the
    rewrite engine, so they are the founder's to fill in, not fabrications.
    """
    outside = _CONFIRM_RE.sub(" ", text or "")
    return [m.group(0) for m in _NUMERIC_CLAIM_RE.finditer(outside)]


def _crawled_fact_text(target_site: dict[str, Any]) -> str:
    parts: list[str] = []
    for page in target_site.get("pages") or []:
        parts.append(str(page.get("title") or ""))
        parts.append(str(page.get("content_text") or ""))
        for heading in page.get("headings") or []:
            parts.append(str(heading))
    return _normalize_number(" ".join(parts))


def validate_artifact(
    artifact: dict[str, Any],
    *,
    target_site: dict[str, Any],
    brand_label: str,
) -> dict[str, Any]:
    """Return a verdict dict: {artifact_type, passed, failures, warnings}."""
    failures: list[str] = []
    warnings: list[str] = []
    artifact_type = artifact.get("artifact_type", "unknown")

    if artifact.get("status") != "generated":
        failures.append(f"generation did not succeed (status={artifact.get('status')!r})")
        return {
            "artifact_type": artifact_type,
            "passed": False,
            "failures": failures,
            "warnings": warnings,
        }

    content = str(artifact.get("content") or "")
    if not content.strip():
        failures.append("empty content")

    # An unterminated [CONFIRM: swallows the rest of the document, so the
    # founder would paste a broken placeholder into a live page.
    if _OPEN_CONFIRM_RE.search(content):
        failures.append("unterminated [CONFIRM: placeholder")

    found = _CONFIRM_RE.findall(content)
    if len(found) > MAX_UNRESOLVED_PLACEHOLDERS:
        failures.append(
            f"{len(found)} unresolved placeholders (max {MAX_UNRESOLVED_PLACEHOLDERS}) - "
            "too little verified material to be worth shipping"
        )

    # Declared list should match what is actually in the body, otherwise the
    # UI checklist under-reports what the founder still has to fill in.
    declared = {str(p).strip().lower() for p in (artifact.get("placeholders") or [])}
    actual = {p.strip().lower() for p in found}
    missing_from_declared = actual - declared
    if missing_from_declared:
        warnings.append(
            f"{len(missing_from_declared)} placeholder(s) in the body are not listed "
            "in 'placeholders'"
        )

    lowered = content.lower()
    for leftover in _TEMPLATE_LEFTOVERS:
        if leftover in lowered:
            failures.append(f"template leftover {leftover!r}")
            break

    if brand_label and brand_label.lower() not in lowered:
        warnings.append(f"brand {brand_label!r} is never named in the artifact")

    # Fabrication check: the substantive one.
    facts = _crawled_fact_text(target_site)
    invented = [
        claim for claim in extract_numeric_claims(content)
        if _normalize_number(claim).lstrip("$") not in facts
    ]
    if invented:
        failures.append(
            "numeric claim(s) not found in crawled site content and not wrapped in "
            f"[CONFIRM: ...]: {', '.join(sorted(set(invented))[:5])}"
        )

    return {
        "artifact_type": artifact_type,
        "passed": not failures,
        "failures": failures,
        "warnings": warnings,
    }


def validate_artifacts(
    artifacts: list[dict[str, Any]],
    *,
    target_site: dict[str, Any],
    brand_label: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Returns (verdicts, shippable_artifacts) in artifact order."""
    verdicts: list[dict[str, Any]] = []
    shippable: list[dict[str, Any]] = []
    for artifact in artifacts:
        verdict = validate_artifact(
            artifact, target_site=target_site, brand_label=brand_label
        )
        verdicts.append(verdict)
        if verdict["passed"]:
            shippable.append(artifact)
    return verdicts, shippable
