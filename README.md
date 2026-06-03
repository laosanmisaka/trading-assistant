# A股交易辅助系统

这是一个基于 PyQt5 + AKShare 的 A 股桌面交易辅助工具。项目当前处于半成品/接手阶段，已经具备分组管理、行情刷新、K 线/分时图、交易记录、止损止盈提醒、买点扫描和做 T 模拟框架，但部分功能还存在未接入、口径待确认和明显 BUG。

本文档已按接手项目的需要重新整理：

- [使用文档](docs/USER_GUIDE.md): 安装、启动、日常使用流程、数据文件和排障。
- [技术文档](docs/TECHNICAL_REFERENCE.md): 目录、文件、数据流、数据库表、类和函数说明。

## 当前能力概览

- 桌面 GUI: PyQt5 主窗口、分组列表、股票表格、K 线/分时图、系统托盘提醒。
- 行情数据: AKShare 获取日线/周线/月线、分钟线、分时数据和股票名称。
- 数据缓存: SQLite 持久化历史 K 线、股票名称、交易记录和设置；内存缓存减少 API 调用。
- 交易记录: 买入/卖出记录增删改查，计算持仓数量、成本和盈亏。
- 止损止盈: 自动计算止损/止盈线，支持手动覆写，触发提醒高亮。
- 买点扫描: 周线底分型、日线 MACD 金叉、短周期缩量回踩中枢三选二触发。
- 做 T 框架: 模型接口、模拟模型、交易状态机、回测模拟器。当前未接入主 UI，投入使用前需要补齐验证。

## 环境要求

- Python 3.10 及以上。
- macOS/Windows/Linux 均可尝试运行，但 GUI 和 PyQt5 在不同系统上的依赖安装方式可能不同。
- 需要可访问 AKShare 数据源的网络环境。

依赖见 [requirements.txt](requirements.txt):

```txt
PyQt5>=5.15.9
akshare>=1.14.0
mplfinance>=0.12.10a0
matplotlib>=3.7.0
pandas>=2.0.0
numpy>=1.24.0
```

## 快速启动

```bash
git clone https://github.com/ThereWasAYang/trading-assistant.git
cd trading-assistant

conda env create -n ta-py312 -f environment.yml
conda activate ta-py312

python main.py
```

如果环境已创建，后续只需要：

```bash
conda activate ta-py312
python main.py
```

首次启动会在项目根目录创建 `trading_assistant.db`，并创建三个预设分组：`持仓中`、`已清仓`、`跟踪中`。日志写入 `logs/`。这些运行时文件已被 `.gitignore` 排除。

## 测试

```bash
python -m pytest -q
```

本次接手审阅时，在 `py312` Conda 环境安装依赖后执行结果为：

```txt
114 passed in 52.77s
```

注意：当前测试包含真实 AKShare 网络请求，也会删除项目根目录的 `trading_assistant.db`。运行测试前请确认没有重要本地数据。

## 项目结构

```txt
trading-assistant/
├── main.py
├── config.py
├── requirements.txt
├── resources/
├── core/
│   ├── technical.py
│   ├── alert_engine.py
│   ├── buy_point_scanner.py
│   └── trading/
├── data/
│   ├── models.py
│   ├── database.py
│   ├── market_data.py
│   └── market_data_manager.py
├── ui/
├── utils/
├── tests/
└── docs/
```

详细到每个文件、类和函数的说明见 [技术文档](docs/TECHNICAL_REFERENCE.md)。

## 接手建议

1. 先向项目维护者索取私有审阅记录，确认遗留风险后再改核心逻辑。
2. 向原开发者确认 30 分钟/60 分钟周期、止损规则、做 T 资金模型等业务口径。
3. 修复测试夹具删除真实数据库的问题，再补充不依赖网络的单元测试。
4. 将行情 API 调用、数据库路径、缓存策略和业务计算拆出更明确的接口，降低 GUI 线程和业务逻辑耦合。

本工具只用于交易辅助和代码研究，不构成投资建议。
