"""Typed planner fields and small, scoped corrections without regenerating a plan."""
from __future__ import annotations

import copy
import json
from typing import Literal

from pydantic import TypeAdapter, ValidationError

from .models import Mission


ENFORCED_FIELDS = ("allowed_origins", "sources", "source_policy", "retrieval_policy", "access_profile",
                   "access_profile_ref", "external_model_content", "model_execution", "publication_window",
                   "artifact_roles", "completeness", "urls", "scope")
EXTRA_TYPES = {"access_profile_ref": str, "external_model_content": Literal["none", "metadata", "selected_page_content"],
               "model_execution": str,
               "allowed_tools": list[str], "capabilities": list[str]}


class PlanFieldError(ValueError):
    code = "planner_field_validation"

    def __init__(self, issues):
        self.issues = issues
        super().__init__("; ".join(f"{'.'.join(item['path'])}: {item['message']}" for item in issues))

    def public(self):
        return {"code": self.code, "fields": self.issues,
                "message": str(self), "action": "Correct the named fields; the existing plan and conversation are preserved."}


def _typed_value(name, value):
    annotation = (Mission.model_fields[name].rebuild_annotation() if name in Mission.model_fields
                  else EXTRA_TYPES[name])
    adapter = TypeAdapter(annotation)
    return adapter.dump_python(adapter.validate_json(json.dumps(value), strict=True), mode="json", by_alias=True)


def typed_constraints(values, *, mission=None, relocate_descriptions=False):
    """Validate reserved policy keys. Arbitrary descriptive keys remain supported.

    Only model-authored prose with an independently valid typed mission value may
    move to descriptions. Trusted operator policy is never relocated or discarded.
    """
    result = copy.deepcopy(values)
    issues, moved = [], []
    for key in (*ENFORCED_FIELDS, "allowed_tools", "capabilities"):
        if key not in result:
            continue
        try:
            result[key] = _typed_value(key, result[key])
        except (ValidationError, ValueError, TypeError):
            value = result[key]
            if relocate_descriptions and isinstance(value, str) and mission and key in mission:
                try:
                    _typed_value(key, mission[key])
                    descriptions = result.setdefault("descriptions", {})
                    if isinstance(descriptions, dict) and key not in descriptions:
                        descriptions[key] = result.pop(key)
                        moved.append(key)
                        continue
                except (ValidationError, ValueError, TypeError):
                    pass
            issues.append({"path": ["constraints", key], "message": "Use the typed Mission field; put explanatory prose in constraints.descriptions."})
    if issues:
        raise PlanFieldError(issues)
    return result, moved


def correct_fields(draft, changes, allowed_paths):
    if not isinstance(changes, list) or not changes or len(changes) > 20:
        raise ValueError("Provide 1-20 field corrections")
    result = copy.deepcopy(draft)
    for change in changes:
        if not isinstance(change, dict) or set(change) - {"path", "value"}:
            raise ValueError("Each correction requires only path and value")
        path = change.get("path")
        if not isinstance(path, list) or path not in allowed_paths or "value" not in change:
            raise ValueError("Correction is outside the rejected field scope")
        target = result
        for key in path[:-1]:
            if not isinstance(target, dict) or key not in target:
                raise ValueError("Correction path does not exist")
            target = target[key]
        target[path[-1]] = copy.deepcopy(change["value"])
    return result


def validation_issues(exc):
    if isinstance(exc, PlanFieldError):
        return exc.issues
    if isinstance(exc, ValidationError):
        # Never return Pydantic input/context fields: they can contain credentials.
        return [{"path": (["mission", *[str(value) for value in error["loc"][:1]]]
                           if exc.title == "Mission" else [str(value) for value in error["loc"]]),
                 "message": f"Invalid field ({error['type']}); use the declared plan schema."}
                for error in exc.errors(include_input=False, include_context=False, include_url=False)][:20]
    return []
