"""工具函数"""

from datetime import datetime, time
from typing import Optional

import config


def _parse_hhmm(value: str) -> time:
    """把配置里的 'HH:MM' 字符串解析为 datetime.time"""
    hour, minute = value.strip().split(":")
    return time(int(hour), int(minute))


def trading_sessions() -> tuple:
    """当前配置定义的交易时段

    返回形如 ((上午起, 上午止), (下午起, 下午止))。

    每次调用都重新读取 config 模块属性，因此修改配置（或测试中替换常量）
    立即生效 —— 不在导入期缓存，避免出现「改了 config 却要重启才生效」。
    """
    return (
        (_parse_hhmm(config.TRADING_START_MORNING),
         _parse_hhmm(config.TRADING_END_MORNING)),
        (_parse_hhmm(config.TRADING_START_AFTERNOON),
         _parse_hhmm(config.TRADING_END_AFTERNOON)),
    )


def is_trading_time(now: Optional[datetime] = None) -> bool:
    """判断是否为 A 股交易时间 (周一至周五, 交易时段内)

    判断依据:
      1. 周一至周五 —— **不含节假日日历**，法定节假日需由调用方另行判断
      2. 落在 config 定义的交易时段内:
         TRADING_START_MORNING ~ TRADING_END_MORNING
         或 TRADING_START_AFTERNOON ~ TRADING_END_AFTERNOON

    此前时段是硬编码的 time(9, 30) / time(15, 0) 字面量，导致改 config
    不生效 —— 已改为读配置（2026-09-17）。

    now 参数仅供测试注入，默认取当前时间。
    """
    now = now or datetime.now()
    if now.weekday() >= 5:      # 周六 / 周日
        return False
    t = now.time()
    return any(start <= t <= end for start, end in trading_sessions())
