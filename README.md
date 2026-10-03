# A 股交易辅助系统

PyQt5 桌面工具，提供行情与 K 线、分组、交易记录、持仓成本与盈亏、止损止盈提醒，以及日线候选窗口中的 5min 一买监控。工具不连接券商下单；研究按信号逐点统计，不是组合收益。

从 [文档索引](docs/README.md) 开始。当前策略、历史实验和评审证据已分开标识；旧收益数字不能替代当前版本的验证。

## 启动

在仓库根目录使用 Python 3.12：

```bash
conda env create -n trading-assistant-py312 -f environment.yml
conda activate trading-assistant-py312
python main.py
```

已存在该环境时只需激活。本项目当前会话使用该专用 conda 环境；个人 Windows venv 路径不是项目安装要求。依赖以 [requirements.txt](requirements.txt) 为准；已验证版本和平台边界见 [验证说明](docs/VALIDATION.md)。

首次启动创建 `trading_assistant.db` 和预设分组。用户成交、设置保存在该数据库，日志在 `logs/`。行情、监控日志和研究输出在 `outputs/`，这些运行文件不会随 clone 分发。

## 日常入口

| 目的 | 入口 |
| --- | --- |
| 安装、GUI、提醒和排障 | [使用手册](docs/USER_GUIDE.md) |
| 盘后取数、候选扫描、盘中监控 | [运行手册](docs/OPERATIONS.md) |
| 事件确认、修订、回测和统计口径 | [当前策略合同](docs/TRIPLE_BUY_WALKTHROUGH.md) |
| 数据层、线程、数据库、模块职责 | [技术总览](docs/TECHNICAL_REFERENCE.md) |
| 遗留功能和验证边界 | [已知限制](docs/KNOWN_ISSUES.md) |
| 本轮评审与修复证据 | [整改记录](docs/reviews/REMEDIATION_2026-10-03.md) |
| 静态资源与授权状态 | [第三方资源](THIRD_PARTY_NOTICES.md) |

## 检查

测试临时目录必须在仓库外：

```bash
python -m pytest -q --basetemp=/tmp/trading-assistant-pytest
python scripts/check_standards.py --base 7fbaba8
python -m pip check
```

Windows 可用 `--basetemp="%TEMP%\trading-assistant-pytest"`。默认套件跳过真实网络测试；需要明确检查数据源时加 `--run-network`。更多环境变量和验证结果见 [验证说明](docs/VALIDATION.md)。
