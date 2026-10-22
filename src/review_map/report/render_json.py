"""Render the reading plan as stable JSON for other tools.

The output is built field by field (not dumped from internal models), so internal
changes don't silently change the format. Breaking changes bump `schema_version`.

Schema version 1:

    {
      "schema_version": 1,
      "tool": {"name": "review-map", "version": str},
      "change": {"base": str, "head": str, "base_sha": str|null, "head_sha": str|null},
      "summary": {
        "files_changed", "files_skipped", "units_changed", "lines_changed",
        "budget_lines", "max_units", "read_units", "read_lines": int,
        "highest_level": "high"|"medium"|"low"|null,
        "unmentioned": int, "claimed_but_missing": int
      },
      "claims": null | {          # null when no description was given
        "description": str|null, "source": "llm"|"heuristic", "model": str|null,
        "low_confidence": bool,
        "files", "functions", "behaviors", "out_of_scope": [str]
      },
      "items": [{                  # every changed unit, highest priority first
        "unit_id": "path::name", "path": str, "old_path": str|null, "name": str,
        "kind": "function"|"method"|"class"|"type"|"namespace"|"module",
        "lines": {"start", "end", "old_start", "old_end": int|null},
        "added": int, "removed": int,
        "risk": {"score": 0-100, "level": str,
                 "reasons": [{"rule": str, "points": int, "message": str}]},
        "scope": null | {"level": "function"|"file"|"folder"|"none",
                         "reasons": [str], "out_of_scope": [str]},
        "boost": int, "priority": 0-100, "level": str,
        "section": "read_carefully"|"skim", "rank": int|null, "unmentioned": bool
      }],
      "file_violations": [{"path": str, "item": str}],
      "claimed_but_missing": [{"kind": "file"|"function", "claim": str}],
      "not_checkable": [str],
      "skipped_files": [str],
      "notes": [str]
    }
"""

import json
from typing import Any

from review_map import __version__
from review_map.models import PlanItem, ReadingPlan

SCHEMA_VERSION = 1


def _item(item: PlanItem, has_scope: bool) -> dict[str, Any]:
    u = item.unit
    return {
        "unit_id": u.unit_id,
        "path": u.path,
        "old_path": u.old_path,
        "name": u.name,
        "kind": u.kind.value,
        "lines": {
            "start": u.start_line,
            "end": u.end_line,
            "old_start": u.old_start_line,
            "old_end": u.old_end_line,
        },
        "added": u.added,
        "removed": u.removed,
        "risk": {
            "score": item.risk.score,
            "level": item.risk.level.value,
            "reasons": [
                {"rule": r.rule, "points": r.points, "message": r.message}
                for r in item.risk.reasons
            ],
        },
        "scope": {
            "level": item.scope_level.value if item.scope_level else None,
            "reasons": item.scope_reasons,
            "out_of_scope": item.out_of_scope_items,
        }
        if has_scope
        else None,
        "boost": item.boost,
        "priority": item.priority,
        "level": item.level.value,
        "section": item.section.value,
        "rank": item.rank,
        "unmentioned": item.is_unmentioned,
    }


def plan_to_dict(plan: ReadingPlan) -> dict[str, Any]:
    s = plan.summary
    claims = plan.claims
    scope = plan.scope
    highest = plan.highest_level
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": {"name": "review-map", "version": __version__},
        "change": {
            "base": plan.base,
            "head": plan.head,
            "base_sha": plan.base_sha,
            "head_sha": plan.head_sha,
        },
        "summary": {
            "files_changed": s.files_changed,
            "files_skipped": s.files_skipped,
            "units_changed": s.units_changed,
            "lines_changed": s.lines_changed,
            "budget_lines": s.budget_lines,
            "max_units": s.max_units,
            "read_units": s.read_units,
            "read_lines": s.read_lines,
            "highest_level": highest.value if highest else None,
            "unmentioned": len(plan.unmentioned) + len(plan.file_violations),
            "claimed_but_missing": len(scope.claimed_but_missing) if scope else 0,
        },
        "claims": {
            "description": plan.description,
            "source": claims.source.value,
            "model": claims.model,
            "low_confidence": claims.low_confidence,
            "files": claims.files,
            "functions": claims.functions,
            "behaviors": claims.behaviors,
            "out_of_scope": claims.out_of_scope,
        }
        if claims
        else None,
        "items": [_item(i, scope is not None) for i in plan.items],
        "file_violations": [{"path": v.path, "item": v.item} for v in plan.file_violations],
        "claimed_but_missing": [
            {"kind": m.kind, "claim": m.claim} for m in (scope.claimed_but_missing if scope else [])
        ],
        "not_checkable": scope.not_checkable if scope else [],
        "skipped_files": plan.skipped_files,
        "notes": plan.notes,
    }


def render_json(plan: ReadingPlan) -> str:
    return json.dumps(plan_to_dict(plan), indent=2, ensure_ascii=False) + "\n"
