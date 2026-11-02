from typing import Protocol

import pandas as pd


class Strategy(Protocol):
    """Future strategies consume history only; scanners do not place orders."""

    def generate_signals(self, bars: dict[str, pd.DataFrame], parameters: dict) -> pd.DataFrame: ...


REGISTRY = {"martin_luk": None, "qullamaggie": None}


def validate_strategies(settings):
    if set(settings) - set(REGISTRY):
        raise ValueError("未知买卖策略")
    for name, value in settings.items():
        if set(value) != {"enabled"} or type(value["enabled"]) is not bool:
            raise ValueError(f"{name} 必须设置 enabled 布尔开关")
        if value["enabled"] and REGISTRY[name] is None:
            raise NotImplementedError(f"{name} 买卖策略尚未实现；目前仅支持扫描器")
