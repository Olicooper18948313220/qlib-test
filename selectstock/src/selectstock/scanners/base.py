import math
import operator

OPERATORS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le}


def compare(value, threshold, op):
    if (
        value is None
        or threshold is None
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or math.isnan(threshold)
    ):
        return None
    return bool(OPERATORS[op](value, threshold))


def finish(checks, issues):
    unknown = [name for name, passed in checks.items() if passed is None]
    failures = [name for name, passed in checks.items() if passed is False]
    status = "insufficient" if issues or unknown else "failed" if failures else "passed"
    return {
        "status": status,
        "reasons": ";".join(issues + ["unknown:" + x for x in unknown] + failures),
        **{"check_" + key: value for key, value in checks.items()},
    }
