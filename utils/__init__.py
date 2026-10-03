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
    """交易所日历 + 配置时段；日历超范围时停止实时刷新并记录原因。"""
    from core.trading_calendar import is_session, local_now, CalendarUnavailable
    import logging
    now = now or local_now()
    try:
        if not is_session(now.date()):
            return False
    except CalendarUnavailable as error:
        logging.getLogger(__name__).error("%s", error)
        return False
    return any(start <= now.time() <= end for start, end in trading_sessions())
