from .base import compare, finish


class LeaderScanner:
    keys = {
        "enabled",
        "min_market_cap",
        "market_cap_op",
        "average_volume_days",
        "min_average_volume",
        "average_volume_op",
        "min_week_turnover",
        "week_turnover_op",
        "performance_days",
        "week_policy",
        "sort_by",
        "ascending",
    }
    sort_fields = {"performance_1m", "week_turnover", "average_volume", "price", "market_cap"}

    @staticmethod
    def evaluate(metrics, settings, issues):
        checks = {
            "market_cap": compare(
                metrics.get("market_cap"), settings["min_market_cap"], settings["market_cap_op"]
            ),
            "average_volume": compare(
                metrics.get("average_volume"), settings["min_average_volume"], settings["average_volume_op"]
            ),
            "week_turnover": compare(
                metrics.get("week_turnover"), settings["min_week_turnover"], settings["week_turnover_op"]
            ),
            "performance_available": compare(metrics.get("performance_1m"), float("-inf"), ">"),
            "week_data_complete": metrics.get("week_data_complete"),
        }
        return finish(checks, issues)
