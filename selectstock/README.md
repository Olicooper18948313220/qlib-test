# selectstock：美股扫描与研究

独立 Python 子项目，优先读取本地 SQLite。首版提供 Martin Luk Leader（周成交额、月表现）和 Potent（日线强势条件），并导出名单供现有 Premarket Pine 脚本人工筛选。当前阶段输出候选与诊断；买卖规则通过独立插件接口后续加入。

## 安装与首次体验

在本目录使用 Python 3.11：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev,notebook]'
selectstock demo
jupyter lab
```

使用 uv 时可执行 `uv sync --extra dev --extra notebook --locked`，按 `uv.lock` 复现依赖。

Windows 激活命令为 `.venv\Scripts\activate`。在 Notebook 中选择本项目 `.venv` Python 内核。

- `notebooks/01_数据与运行调试.ipynb`：数据长度、截止日期、股票池、两个扫描器开关、数据检查、下载、导出及多周期图表。
- `notebooks/02_扫描器参数实验.ipynb`：只用本地数据调整阈值、比较名单、核对条件与保存参数。

两个 Notebook 默认 `DEMO=True`，建立**独立合成示例库**；虚构代码 DEMOA/DEMOB/DEMOC 仅用于离线学习，不能当作真实选股结果。切换 `DEMO=False`，配置股票与更新开关后使用真实行情。真实模式首次使用需先运行日线更新单元格。

## 真实数据运行

```bash
# 小样本先验证。全市场模式删除 --symbols，或将 configs/data.yaml 的 symbols 留空。
selectstock update --symbols AAPL,NVDA,TSLA
# 以下命令保持同一股票范围与数据库。
selectstock check
selectstock scan --symbols AAPL,NVDA,TSLA
# 分钟线只下载最新扫描入选股与明确的自选股，不受 --symbols 的日线范围替代。
selectstock update --timeframes 15m,5m,1m --watchlist AAPL
# 单独运行某个已启用扫描器；配置中 enabled=false 的扫描器不会执行。
selectstock scan --enabled leader --symbols AAPL,NVDA,TSLA
```

日线默认覆盖两年，15/5 分钟 30 个自然日，一分钟 7 个自然日。已有历史长期保留，扩大请求范围时只补缺。免费分钟历史存在远端保留窗口；程序会提示无法补回的日期，不把截断结果伪装成完整历史。

全市场更新默认按交易所分页获取市值资料，每页至多 250 个报价，并复用时区缓存，减少逐股资料和时区查询。没有按市值预先缩小历史下载股票池；市值缺失的股票仍下载历史，并在扫描时显示资料不足。启用行业排除时使用完整个股资料查询。提速实测、历史长度与 Notebook 续传步骤见 [全量下载与提速](docs/全量下载与提速.md)。

下载默认限制 Yahoo 底层 Session 请求起始间隔为 1–1.2 秒，每 100 次请求额外休息 10 秒；每段 600 次，预算用尽自动休息 60 秒并从当前请求继续。HTTP 429/403 暂停请求并至少冷却 15 分钟后自动恢复，尊重更长的 Retry-After；连续 5 次网络/服务器错误则冷却 5 分钟。同项目的两个 Notebook 和不同数据库共用保护状态，重跑或重启内核不会清除冷却。自动重定向可能产生额外网络往返，所以该预算不等于服务器观测到的所有请求。

下载格提供进度条、当前股票、缓存/异常统计和冷却倒计时。Notebook 首格检测旧模块，提示 Restart。详见 [稳定下载操作指南](docs/稳定下载操作指南.md)。

查看更新报告的 `paused`、`pause_reason`、`resume_after`（UTC）和 `network_requests`。默认自动续传；连续三次恢复仍受限或网络失败才报告 `paused=True`，此时按恢复时间重跑更新格；CLI 暂停时退出码为 2。已完整缓存的数据不消耗 Yahoo 请求预算；休市期间同一收盘交易日的市值资料可以复用。免费源没有固定的“绝不限制”频率，这些默认值用于降低风险，不构成无限下载保证。配置说明见 `docs/数据口径与运行.md`。

每次运行在 `runs/<run_id>` 输出 `leader.csv/.txt`、`potent.csv/.txt`、`combined.csv/.txt`、完整诊断 CSV 和参数/数据覆盖率 JSON。TXT 使用 `NASDAQ:AAPL,NYSE:...` 格式，交易所未确定的代码保留在 CSV，并列入导出元数据。

## 修改与扩展

- `configs/data.yaml`：数据库、历史长度、读取条数、批量大小、时区收盘宽限、重试及离线模式。
- `configs/scanners.yaml`：两个扫描器各自的启用开关、阈值、比较符、窗口与排序。Leader 默认本周累计；`week_policy: completed` 可切换到上一完整周。
- `configs/watchlist.yaml`：分钟线维护的额外自选股。
- `configs/strategies.yaml`：Martin Luk / Qullamaggie 后续买卖插件开关。当前打开会明确抛出未实现错误。
- `src/selectstock/scanners`：添加扫描器；`strategies` 定义未来买卖规则接口。Notebook 和 CLI 共用 runner。

```python
from selectstock.config import load_config, load_experiment
from selectstock import update_data, load_bars, run_scans, export_watchlists

config = load_config(overrides={"data": {"symbols": ["AAPL", "NVDA"]}})
report = update_data(config)
result = run_scans(config, enabled=["leader", "potent"])
print(report.to_dict(), result.coverage)
print(export_watchlists(result))
bars = load_bars("AAPL", "5m", limit=200, config=config)
# config = load_experiment("runs/experiments/liquidity_volatility.yaml")
```

`run_scans(..., persist=False)` 不写扫描历史，适合参数实验。`update_data(..., repair=True)` 显式重取指定区间，用于已知历史修订。更新遇到中断可以直接重跑；事务保证已提交部分可复用。

## 规则、数据与验证

见 `docs/规则与来源.md`、`docs/数据口径与运行.md` 和 `docs/验收记录.md`。Yahoo 与 TradingView 的数据供应商、盘中状态及资料更新节奏不同，结果不会保证逐行一致；每次输出保留本地数据截止时间、公式版本与资料缺失原因。

```bash
python -m pytest -q
python scripts/verify_notebooks.py
python scripts/live_smoke.py  # 访问网络，只下载 AAPL 小样本
```

数据库、下载行情、运行结果、凭据与 Notebook 输出均不提交 Git。
