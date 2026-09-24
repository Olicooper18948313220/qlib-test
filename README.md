# qlib_quant：可修改的本地量化交易框架

这是从 `D:\quanttrade20260207` 提炼出的新项目。它把数据、因子、选股信号、组合、回测和报告拆开，方便逐层理解和替换。

## 先做什么

1. `python -m qlib_quant.cli validate` 检查 repaired SQLite 数据库。
2. `python -m qlib_quant.cli export` 把股票日线导出为 Qlib 需要的标准字段，并写入数据清单。
   只想先检查 Qlib 输入格式时，可用 `python -m qlib_quant.cli qlib-export --start 2020-01-01 --end 2020-03-31 --max-rows 50000`。
3. `python -m qlib_quant.cli backtest --start 2020-01-01 --end 2024-12-31` 运行可复现的简单基线。
4. 回测会生成 CSV、JSON 指标和权益曲线 PNG。
5. `streamlit run app/main.py` 打开本地网页（若安装了 Streamlit）。

在 Windows 上也可以双击或在 PowerShell 执行 `scripts/run_validate.ps1` 和 `scripts/run_smoke.ps1`。

默认策略是 long-only、每周调仓、最多 20 只、等权、下一交易日开盘成交。旧项目的行业分析和 Excel 交易步骤不在新运行链路中。

## 修改接口

- `configs/factors.yaml`：因子窗口和字段。
- `configs/signals.yaml`：11 个 Caochen 信号的启用开关和阈值。
- `configs/portfolio.yaml`：持仓数、调仓频率、费用、滑点。
- `src/qlib_quant/signals/legacy_v1.py`：信号实现；复制成 `legacy_v2.py` 可做新版本。
- `src/qlib_quant/factors/technical.py`：因子计算；每个函数只接受历史数据，避免未来数据泄漏。

## 数据口径

原数据库的价格是后复权，成交量和成交额是未复权口径。回测的成交价使用后复权价格只是研究近似，报告会始终标明这一点。

## 目录

`data/raw` 保存来源数据，`data/curated` 保存校验后的表，`data/qlib` 保存转换后的 Qlib 数据；`runs` 保存每次运行结果；`tests` 保存不依赖大数据库的单元测试。

