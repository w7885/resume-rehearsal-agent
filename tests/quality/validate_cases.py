"""Validate synthetic evaluation specifications; never call an Agent or provider."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CASES = Path(__file__).with_name("cases.json")


def validate(payload: dict) -> list[str]:
    errors: list[str] = []
    if payload.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if payload.get("data_classification") != "synthetic":
        errors.append("only synthetic data is allowed in this shared fixture")
    if payload.get("provider_calls") != 0 or payload.get("live_model_evaluation") != "not_run":
        errors.append("fixture preparation must not claim model evaluation")
    groups = payload.get("groups", [])
    cases = payload.get("cases", [])
    if not isinstance(groups, list) or len(set(groups)) != 6:
        errors.append("exactly six named scenario groups are required")
    if not isinstance(cases, list) or not cases:
        return errors + ["cases must be a non-empty list"]
    names: set[str] = set()
    seen_groups: set[str] = set()
    for index, case in enumerate(cases, 1):
        if not isinstance(case, dict):
            errors.append(f"case {index}: must be an object")
            continue
        name = case.get("name")
        label = name or f"case {index}"
        if not isinstance(name, str) or not name.strip() or name in names:
            errors.append(f"{label}: missing or duplicate descriptive name")
        names.add(name)
        if case.get("group") not in groups:
            errors.append(f"{label}: unknown group")
        else:
            seen_groups.add(case["group"])
        for field in ("provenance", "original"):
            if not isinstance(case.get(field), str) or not case[field].strip():
                errors.append(f"{label}: {field} must be non-empty text")
        if case.get("data_classification") != "synthetic":
            errors.append(f"{label}: must explicitly identify synthetic material")
        target = case.get("target", {})
        if not isinstance(target, dict) or not target.get("job") or not target.get("section"):
            errors.append(f"{label}: target job and section are required")
        for field in ("ordered_inputs", "allowed_facts", "must_preserve", "forbidden_additions", "expected_actions"):
            value = case.get(field)
            if not isinstance(value, list) or not value or any(not isinstance(x, str) or not x.strip() for x in value):
                errors.append(f"{label}: {field} must be a non-empty text list")
        state = case.get("precondition", {})
        if not isinstance(state, dict) or not state.get("phase") or not isinstance(state.get("prior_questions"), list):
            errors.append(f"{label}: explicit setup phase and prior questions are required")
        supplements = case.get("supplements")
        if not isinstance(supplements, list) or any(not isinstance(s, dict) or s.get("role") not in {"resume", "supplement", "jd"} or not s.get("text") for s in supplements):
            errors.append(f"{label}: supplements need a role and exact text")
        scoring = case.get("scoring", {})
        if not isinstance(scoring, dict):
            errors.append(f"{label}: scoring must be an object")
        elif (scoring.get("status") != "not_evaluated" or scoring.get("scores") is not None
              or scoring.get("reviewer") is not None or scoring.get("evidence") != []):
            errors.append(f"{label}: human scores must remain unassigned in the input fixture")
        elif len(scoring.get("hard_gates", [])) != 5 or len(scoring.get("dimensions", [])) != 3:
            errors.append(f"{label}: five hard gates and three scoring dimensions are required")
    if seen_groups != set(groups):
        errors.append("each named group must contain a case")
    by_name = {c.get("name"): c for c in cases if isinstance(c, dict)}
    for case in cases:
        if not isinstance(case, dict):
            continue
        pair = case.get("paired_case")
        if pair:
            other = by_name.get(pair)
            if not other or other.get("paired_case") != case.get("name"):
                errors.append(f"{case.get('name')}: paired case must exist and link back")
            elif other.get("original") != case.get("original") or other.get("target", {}).get("job") == case.get("target", {}).get("job"):
                errors.append(f"{case.get('name')}: paired jobs must differ but original must match")
    return errors


def git_value(*args: str) -> str | None:
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    return result.stdout.strip() if result.returncode == 0 else None


def template(payload: dict, source: bytes) -> dict:
    return {
        "record_type": "unexecuted_evaluation_template",
        "prepared_by": "assistant; not an independent acceptance by team member C",
        "baseline_commit": git_value("rev-parse", "HEAD"),
        "candidate_commit": None,
        "working_tree_status_at_template_generation": git_value("status", "--short"),
        "fixture_sha256": hashlib.sha256(source).hexdigest(),
        "environment": {"python": None, "application_version": None, "model": None, "model_parameters": None},
        "budget_authorization": {"authorized": False, "reference": None, "current_ledger_cents": None, "other_machine_spend_cents": None, "maximum_this_run_cents": None},
        "results": [{
            "case_name": case["name"], "evaluation_kind": "not_run", "status": "not_evaluated",
            "evaluator": None, "timestamp": None, "input_snapshot": case,
            "observed_actions": [], "raw_outputs": [], "tool_trace": [],
            "must_preserve_checks": [{"item": x, "passed": None, "evidence": None} for x in case["must_preserve"]],
            "hard_gate_results": {x: None for x in case["scoring"]["hard_gates"]},
            "human_scores": {x: None for x in case["scoring"]["dimensions"]},
            "score_evidence": [], "failure_reason": None, "technical_owner": None,
            "provider_calls": None, "cost_cents": None, "latency_ms": None,
            "baseline_comparison": None, "independent_acceptance_by_c": False,
        } for case in payload["cases"]],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--print-template", action="store_true", help="print an unscored record template to stdout")
    args = parser.parse_args()
    try:
        raw = args.cases.read_bytes()
        payload = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(payload, dict):
            raise ValueError("root must be an object")
        errors = validate(payload)
    except (OSError, ValueError, TypeError) as exc:
        print(json.dumps({"valid": False, "errors": [str(exc)]}, ensure_ascii=False))
        return 1
    if errors:
        print(json.dumps({"valid": False, "errors": errors}, ensure_ascii=False, indent=2))
        return 1
    result = template(payload, raw) if args.print_template else {
        "valid": True, "case_count": len(payload["cases"]), "group_count": len(payload["groups"]),
        "fixture_sha256": hashlib.sha256(raw).hexdigest(), "provider_calls": 0,
        "what_was_verified": "fixture structure only; no Agent, model or database execution",
        "human_scores": "unassigned", "independent_acceptance_by_c": False,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
