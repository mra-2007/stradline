"""The plan DSL: a typed, replayable list of tool calls.

Ported in STRUCTURE from `reference/satquery-ai-main/agent/dsl.py` and
`agent/registry.py` (the reuse map allows the planner, dsl, executor and task
structure), rewritten for Strandline's tools and without pydantic, which is
not a dependency of this project.

The language model's ONLY job is to emit a `Plan`. Once emitted the plan is
plain data: `validate` checks it against `TOOLS` with no model in the loop,
`execute` runs it against real implementations with no model in the loop, and
it round-trips through JSON, so a plan can be logged, cached, replayed and
audited. That is what makes an answer reproducible - and reproducibility is
what the copilot gate needs (Rule 5).

A plan can never make the copilot say a number: the tools read the evidence
store, and the store was built from the run folder before the model was asked
anything.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field


class PlanValidationError(ValueError):
    """A plan names an unknown tool, passes an out-of-schema parameter, or
    repeats a step id."""


@dataclass(frozen=True)
class Parameter:
    name: str
    type: str                     # "str" | "int" | "float" | "bool"
    description: str
    required: bool = False
    default: object = None
    choices: tuple = ()
    minimum: float | None = None
    maximum: float | None = None


@dataclass(frozen=True)
class ToolSpec:
    """What a tool is called, what it needs, and what it answers."""

    name: str
    description: str
    parameters: tuple = ()
    capability: str = "flood"     # which evidence capability it needs
    examples: tuple = ()

    def parameter(self, name: str) -> Parameter | None:
        for p in self.parameters:
            if p.name == name:
                return p
        return None


@dataclass(frozen=True)
class Step:
    id: str
    tool: str
    parameters: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"id": self.id, "tool": self.tool, "parameters": dict(self.parameters)}


@dataclass(frozen=True)
class Plan:
    steps: tuple = ()

    def __iter__(self):
        return iter(self.steps)

    def __len__(self) -> int:
        return len(self.steps)

    def as_list(self) -> list:
        return [s.as_dict() for s in self.steps]

    def to_json(self) -> str:
        return json.dumps(self.as_list(), sort_keys=True)

    @classmethod
    def from_list(cls, items) -> "Plan":
        if not isinstance(items, list):
            raise PlanValidationError(f"a plan is a list of steps, got {type(items).__name__}")
        steps = []
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                raise PlanValidationError(f"step {i} is not an object")
            tool = item.get("tool")
            if not isinstance(tool, str):
                raise PlanValidationError(f"step {i} has no tool name")
            params = item.get("parameters") or {}
            if not isinstance(params, dict):
                raise PlanValidationError(f"step {i}: parameters must be an object")
            steps.append(Step(id=str(item.get("id") or f"s{i + 1}"), tool=tool,
                              parameters=params))
        return cls(steps=tuple(steps))


def coerce(spec: ToolSpec, parameters: dict) -> dict:
    """Check and normalise one step's parameters against its tool's schema.

    Unknown names, wrong types and out-of-range values are rejected rather
    than dropped: a silently ignored parameter is a plan that did something
    other than what it says, which is exactly what a replayable trace is for.
    """
    out = {}
    for name, value in parameters.items():
        p = spec.parameter(name)
        if p is None:
            raise PlanValidationError(
                f"tool {spec.name!r}: unknown parameter {name!r}; accepted: "
                f"{sorted(q.name for q in spec.parameters)}")
        out[name] = _coerce_one(spec, p, value)
    for p in spec.parameters:
        if p.name in out:
            continue
        if p.required:
            raise PlanValidationError(f"tool {spec.name!r}: {p.name!r} is required")
        if p.default is not None:
            out[p.name] = p.default
    return out


def _coerce_one(spec: ToolSpec, p: Parameter, value):
    if p.type == "str":
        if not isinstance(value, str):
            raise PlanValidationError(f"{spec.name}.{p.name} must be text")
        value = value.strip()
    elif p.type == "int":
        try:
            value = int(value)
        except (TypeError, ValueError) as err:
            raise PlanValidationError(f"{spec.name}.{p.name} must be a whole number") from err
    elif p.type == "float":
        try:
            value = float(value)
        except (TypeError, ValueError) as err:
            raise PlanValidationError(f"{spec.name}.{p.name} must be a number") from err
    elif p.type == "bool":
        if not isinstance(value, bool):
            raise PlanValidationError(f"{spec.name}.{p.name} must be true or false")
    else:  # pragma: no cover - a ToolSpec with an undeclared type is a bug
        raise PlanValidationError(f"{spec.name}.{p.name}: unknown type {p.type!r}")
    if p.choices and value not in p.choices:
        raise PlanValidationError(
            f"{spec.name}.{p.name} must be one of {list(p.choices)}, got {value!r}")
    if p.minimum is not None and value < p.minimum:
        raise PlanValidationError(f"{spec.name}.{p.name} must be at least {p.minimum}")
    if p.maximum is not None and value > p.maximum:
        raise PlanValidationError(f"{spec.name}.{p.name} must be at most {p.maximum}")
    return value


def validate(plan: Plan, tools: dict) -> Plan:
    """Return the plan with every step's parameters checked and defaulted."""
    if len(plan) == 0:
        raise PlanValidationError("an empty plan answers nothing")
    seen, steps = set(), []
    for step in plan:
        if step.id in seen:
            raise PlanValidationError(f"duplicate step id {step.id!r}")
        seen.add(step.id)
        spec = tools.get(step.tool)
        if spec is None:
            raise PlanValidationError(
                f"step {step.id!r}: unknown tool {step.tool!r}; available: {sorted(tools)}")
        steps.append(Step(id=step.id, tool=step.tool,
                          parameters=coerce(spec, step.parameters)))
    return Plan(steps=tuple(steps))


def schema_text(tools: dict) -> str:
    """The tool contracts, as the text a planner prompt shows the model."""
    lines = []
    for name in sorted(tools):
        spec = tools[name]
        lines.append(f"- {name}: {spec.description}")
        for p in spec.parameters:
            bits = [p.type]
            if p.choices:
                bits.append("one of " + "|".join(str(c) for c in p.choices))
            if p.required:
                bits.append("required")
            elif p.default is not None:
                bits.append(f"default {p.default}")
            lines.append(f"    {p.name} ({', '.join(bits)}): {p.description}")
    return "\n".join(lines)


def spec_as_dict(spec: ToolSpec) -> dict:
    return {"name": spec.name, "description": spec.description,
            "capability": spec.capability,
            "parameters": [asdict(p) for p in spec.parameters]}
