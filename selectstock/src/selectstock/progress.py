"""Small dependency-free Notebook/terminal progress renderer."""

import html
import sys
import time


class DownloadProgress:
    def __init__(self, notebook=False, stream=None):
        self.notebook = notebook
        self.stream = stream or sys.stderr
        self.started = time.monotonic()
        self.last = 0
        self.state = {}
        self.handle = None

    def __call__(self, **event):
        self.state.update(event)
        now = time.monotonic()
        stage = self.state.get("stage", "preparing")
        final = stage in {"completed", "partial", "paused", "stopped"}
        if now - self.last < 0.5 and not final:
            return
        self.last = now
        s = self.state
        done, total = s.get("processed", 0), s.get("total", 0)
        elapsed = now - self.started
        eta = f"{elapsed / done * (total - done):.0f}秒" if done >= 3 else "计算中"
        label = {
            "preparing": "准备",
            "metadata": "证券资料",
            "history": "下载行情",
            "waiting": "等待恢复",
            "resuming": "恢复",
            "completed": "完成",
            "partial": "部分完成",
            "stopped": "用户停止",
            "paused": "暂停",
        }.get(stage, stage)
        text = (
            f"{label} | {done}/{total or '?'} | {s.get('symbol', '')} {s.get('timeframe', '')} | "
            f"下载 {s.get('downloaded', 0)} 缓存 {s.get('cached', 0)} 跳过 {s.get('skipped', 0)} "
            f"异常 {s.get('failures', 0)} 请求 {s.get('network_requests', 0)} | "
            f"已用 {elapsed:.0f}秒 预计剩余 {eta}"
        )
        if stage == "waiting":
            text += f" | {s.get('reason', '')} | {s.get('remaining_seconds', 0):.0f}秒后恢复 {s.get('resume_after', '')}"
        if self.notebook:
            try:
                from IPython.display import HTML, display

                content = HTML(
                    f'<progress value="{done}" max="{max(total, 1)}" style="width:100%"></progress>'
                    f'<pre style="white-space:pre-wrap">{html.escape(text)}</pre>'
                )
                if self.handle is None:
                    self.handle = display(content, display_id=True)
                    if self.handle is None:
                        self.notebook = False
                else:
                    self.handle.update(content)
                if self.notebook:
                    return
            except (ImportError, AttributeError):
                self.notebook = False
        print(text, file=self.stream, flush=True)
