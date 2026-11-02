import matplotlib.pyplot as plt

from .indicators.technical import ema_series
from .runner import load_bars


def plot_symbol(symbol, config, limit=150, regular_only=True):
    fig, axes = plt.subplots(4, 1, figsize=(12, 10), constrained_layout=True)
    for ax, tf in zip(axes, ("1d", "15m", "5m", "1m")):
        frame = load_bars(
            symbol, tf, end=config["data"]["end"], limit=limit + 250 if tf == "1d" else limit, config=config
        )
        if not frame.empty and regular_only:
            frame = frame.loc[frame["session"].eq("regular")]
        if not frame.empty:
            if tf == "1d":
                for span in (21, 50):
                    frame[f"ema_{span}"] = ema_series(frame["close"], span)
            frame = frame.tail(limit)
            ax.plot(frame["ts"], frame["close"], label="Close")
            if tf == "1d":
                for span in (21, 50):
                    ax.plot(frame["ts"], frame[f"ema_{span}"], label=f"EMA {span}")
            ax.legend()
        ax.set_title(f"{symbol} | {tf} | {'SYNTHETIC' if config['data'].get('demo') else 'Yahoo'}")
        ax.tick_params(axis="x", rotation=20)
    return fig
