"""Small typed rule interpreter with complete evaluated-condition traces."""

import operator
from typing import Any

MISSING = object()


def resolve(context: dict, path: str) -> Any:
    value = context
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            return MISSING
        value = value[key]
    return value


def same(left: Any, right: Any) -> bool:
    # Python considers True == 1; the business DSL intentionally does not.
    if type(left) in {int, float} and type(right) in {int, float}:
        return left == right
    return type(left) is type(right) and left == right


def evaluate(condition: dict, context: dict) -> tuple[bool, dict]:
    if len(condition) == 1:
        kind, children = next(iter(condition.items()))
        if kind == "not":
            result, trace = evaluate(children, context)
            return not result, {"operator": kind, "matched": not result, "children": [trace]}
        # Evaluate every child (no short-circuit) so the explanation is complete.
        evaluated = [evaluate(child, context) for child in children]
        result = (all if kind == "all" else any)(item[0] for item in evaluated)
        return result, {"operator": kind, "matched": result, "children": [x[1] for x in evaluated]}
    actual = resolve(context, condition["field"])
    op = condition["op"]
    expected = condition.get("value")
    present = actual is not MISSING
    if op == "exists":
        matched = present
    elif op == "missing":
        matched = not present
    elif not present:
        matched = False
    elif op in {"eq", "ne"}:
        matched = same(actual, expected)
        if op == "ne":
            matched = not matched
    elif op in {"in", "not_in"}:
        matched = any(same(actual, item) for item in expected)
        if op == "not_in":
            matched = not matched
    else:
        compare = {"gt": operator.gt, "gte": operator.ge, "lt": operator.lt, "lte": operator.le}[op]
        matched = (
            type(actual) in {int, float}
            and type(expected) in {int, float}
            and compare(actual, expected)
        )
    return matched, {
        "field": condition["field"],
        "operator": op,
        "present": present,
        "actual": actual if present else None,
        "expected": expected,
        "matched": matched,
    }
