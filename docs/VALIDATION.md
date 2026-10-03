# 验证说明

本轮使用 macOS、专用 conda `trading-assistant-py312`、Python 3.12.13。当前安装快照见 [requirements-macos-py312-2026-10-03.txt](reviews/requirements-macos-py312-2026-10-03.txt)；它记录已测组合，不是跨平台锁文件。最低依赖组合没有单独安装验证。

核心版本：czsc 1.0.1、PyQt5 5.15.11、pandas 3.0.3、NumPy 2.4.6、Matplotlib 3.10.9、pytest 9.0.3、AKShare 1.18.64、mootdx 0.11.7、baostock 0.9.4、exchange_calendars 4.13.2、radon 6.0.1。

## 可重复命令

在仓库根目录运行，临时目录放仓外：

```bash
conda activate trading-assistant-py312
TA_CHECK_TMP=$(mktemp -d /tmp/trading-assistant-check.XXXXXX)
export CZSC_HOME="$TA_CHECK_TMP/czsc"
export MPLCONFIGDIR="$TA_CHECK_TMP/mpl"
export QT_QPA_PLATFORM=offscreen
export PYTHONDONTWRITEBYTECODE=1
python -m pytest -q --basetemp="$TA_CHECK_TMP/pytest"
python -m pip check
python scripts/check_standards.py --base 7fbaba8
python scripts/check_docs.py
```

Windows 对应设置同名环境变量，并将 `--basetemp` 指向 `%TEMP%` 下独立目录。不要将仓库根目录本身传给 pytest 的 basetemp。

最终执行结果统一维护在 [整改记录](reviews/REMEDIATION_2026-10-03.md)，不在多个文档复制易过期的数量。默认 8 个 `network` 用例跳过；使用 `--run-network` 才会显式测试远端接口。旧永久跳过的三买结构用例已换为真实 czsc 离线前缀/跨入口行为测试。

## 覆盖与边界

覆盖持仓完整周期、手动提醒重启、真实结构首次观察与撤销、日志中断恢复、日历假日、缓存失败、任务参数与错误分支、真实 QThread 结束前后的队列和退出、最低佣金/零权重/初始回撤、统计权重、字体和内联 HTML 边界。

新增测试按一组完整行为组织，优先扩展现有用例；没有给每个辅助函数机械添加测试。GitHub Actions 配置 Ubuntu/Windows Python 3.12 离线测试，但新增配置本身不等于两个平台已经运行通过。

未完成的外部验收：真实行情源当日响应、Windows 桌面通知/计划任务、真实市场逐时快照与样本外收益。缺失的私有 `outputs/` 历史报告未重造；旧数字不作为本轮修复通过条件。字体回退测试故意禁用中文字体，会出现缺字警告，图像导出仍成功；正常中文展示需要本机中文字体。

历史缺陷复现脚本 `docs/reviews/reproduce_2026_10_03.py` 只适用于评审基线 `2edb3fb`，不能在修复分支当作正确性测试或 CI 通过条件。
