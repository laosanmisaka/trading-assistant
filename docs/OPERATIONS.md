# 数据与监控运行手册

在仓库根目录激活 `trading-assistant-py312`。默认目录为 `outputs/cache_min`；默认监控池是 `scripts/pool_mainboard_all.txt`，仅界定扫描范围，不是无偏回测样本。

## 取数与盘后任务

```bash
python scripts/fetch_min.py --period daily --pool scripts/pool_mainboard_all.txt
python scripts/fetch_min.py --period 5min --pool scripts/pool_random500.txt
python scripts/daily_routine.py --no-toast
```

`fetch_min.py` 实际参数是单个 `--period`，没有 `--periods daily,5min`。起点由 `DEFAULT_START` 定义，可用 `--start`/`--end` 覆盖；请求起点不代表接口确实提供该时点数据。应检查 CSV 首尾和元数据，不能把旧机器的 2024 年起点当作公共仓库保证。

缓存文件为 `{period}_{code}.csv`，列为 `dt,open,high,low,close,volume`。旁边的 `.csv.meta.json` 保存来源、复权、请求区间、实际首尾、行数和 SHA-256。baostock 使用前复权；元数据的 `complete` 表示请求分段全部成功，不证明上游没有漏行。无元数据的旧缓存会完整重取以建立可追溯基线。

完整分段失败或任一分段失败都不会发布部分缓存。增量必须有可比较重叠；价格漂移或无重叠时全量重取。CSV 与元数据分别原子替换；两次替换间中断会导致哈希不匹配，下次强制重取。不要手工修改元数据来绕过校验。

盘后流程为：日线更新 → 候选扫描 → 相关 5min 更新 → GUI 自动分组同步。失败返回非零，停止依赖该步骤的后续动作，保留旧文件供检查。扫描缺股或异常不发布不完整 watchlist。

- `--cache-dir PATH`、`--pool PATH`、`--limit N` 会传递给扫描子进程。
- `--watchlist PATH` 指定输出；默认是缓存父目录下的 `watchlist.json`。
- `--skip-scan` 只更新日线，不读取旧 watchlist 做同步。
- `--skip-5min` 跳过分钟缓存；`--skip-sync` 跳过 GUI 分组。
- `--limit` 是局部调试，不接管全量 GUI 分组。

## 监控

```bash
python scripts/scan_candidates.py
python scripts/monitor_buy.py --once --no-toast
python scripts/monitor_buy.py
```

扫描输出 schema 2，包括稳定窗口 ID、首次可用时刻及最近已收盘交易日 as_of。未验证或过期的日线缓存不发布名单，监控拒绝过期 as_of。旧 watchlist 必须重扫，不能猜测历史可用时刻。旧版无稳定 event_id 的信号保留作历史，不进入当前有效买点分组。运行中的监控每轮重读文件，空窗口不会让进程退出；跨日仍会重载。

监控使用上海时区和 XSHG 开市日历，盘中拉新浪已收口 5min bar。一个标的的坏响应不阻断其他标的；日志写入失败则停止写进程，避免在半条记录后继续追加。默认热窗口 5 分钟、旧窗口每 6 轮，非交易时间短周期检查。

`monitor_signals.jsonl` 是事实日志，`monitor_state.json` 是可重建检查点。恢复先折叠日志各事件的最新版本，尾部半条记录可以截去；中间损坏的完整记录会报错，需从备份核对修复。检查点有历史而日志缺失时拒绝恢复，不会清空检查点。不要直接删除日志来“清状态”。指定 `--codes` 也必须使用已验证、最新的日线缓存。

旧事件的确认时刻和成交价不会因几何变化被改写；当前几何、资格和撤销状态以修订记录更新。同一窗口的首次观察即消耗入场资格，不因后来撤销而倒选更好信号。日志先持久化，再更新检查点，最后尝试通知；崩溃可能漏一次桌面通知，不能承诺操作系统弹窗恰好一次，历史记录仍可恢复。

只有近 10 分钟新增、有效的首次入场发通知，历史补算只记录。一个日志路径由一个进程锁定；重启脚本不会搜索或杀掉别的仓库进程。

## 调度

Windows 批处理 `scripts/start_monitor.bat` 使用当前 `python`，也可设置 `TRADING_ASSISTANT_PYTHON` 为专用环境解释器绝对路径。不含个人用户名、磁盘路径或批量终止命令。

```bash
python scripts/daily_routine.py --register-tasks
```

该命令才会注册任务；普通运行不注册。任务使用运行此命令的 Python，名称带仓库路径哈希，18:05 盘后更新、09:25 启动监控。工作日调度不等于开市日，监控会再校验日历。macOS/Linux 使用系统调度器调用同一脚本；此仓库不自动安装调度。
