from .base import compare, finish


class PotentScanner:
    keys = {
        "enabled",
        "min_market_cap",
        "market_cap_op",
        "ema_fast",
        "ema_slow",
        "ema_fast_op",
        "ema_slow_op",
        "volatility_days",
        "min_volatility",
        "volatility_op",
        "min_day_turnover",
        "day_turnover_op",
        "open_change_lower",
        "open_change_upper",
        "outside_inclusive",
        "excluded_industries",
        "sort_by",
        "ascending",
    }
    sort_fields = {"change_pct", "open_change_pct", "volatility_1w", "day_turnover", "price", "market_cap"}

    @staticmethod
    def evaluate(metrics, settings, issues):
        value = metrics.get("open_change_pct")
        left = compare(value, settings["open_change_lower"], "<=" if settings["outside_inclusive"] else "<")
        right = compare(value, settings["open_change_upper"], ">=" if settings["outside_inclusive"] else ">")
        checks = {
            "market_cap": compare(
                metrics.get("market_cap"), settings["min_market_cap"], settings["market_cap_op"]
            ),
            "ema_fast": compare(
                metrics.get("price"), metrics.get("ema_fast", float("nan")), settings["ema_fast_op"]
            )
            if metrics.get("ema_fast") is not None
            else None,
            "ema_slow": compare(
                metrics.get("price"), metrics.get("ema_slow", float("nan")), settings["ema_slow_op"]
            )
            if metrics.get("ema_slow") is not None
            else None,
            "volatility": compare(
                metrics.get("volatility_1w"), settings["min_volatility"], settings["volatility_op"]
            ),
            "day_turnover": compare(
                metrics.get("day_turnover"), settings["min_day_turnover"], settings["day_turnover_op"]
            ),
            "open_change_outside": (left or right) if left is not None and right is not None else None,
            "industry": (metrics.get("industry") not in settings["excluded_industries"])
            if not settings["excluded_industries"] or metrics.get("industry")
            else None,
        }
        return finish(checks, issues)
