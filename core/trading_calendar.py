"""XSHG sessions shared by GUI and monitor; no weekday-only holiday fallback.

The versioned exchange_calendars holiday table has a finite supported range.
Outside that range callers receive CalendarUnavailable and must update the
calendar dependency instead of inventing sessions. Input wall times use Shanghai.
"""
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

SHANGHAI = ZoneInfo('Asia/Shanghai')


class CalendarUnavailable(ValueError):
    pass


def local_now():
    return datetime.now(SHANGHAI).replace(tzinfo=None)


@lru_cache(maxsize=8)
def _calendar(year):
    try:
        return xcals.get_calendar('XSHG', start=f'{year - 1}-01-01', end=f'{year}-12-31')
    except ValueError as error:
        raise CalendarUnavailable(f'交易日历未覆盖 {year}，请更新 exchange_calendars: {error}') from error


def is_session(day):
    day = pd.Timestamp(day).normalize().tz_localize(None)
    return bool(_calendar(day.year).is_session(day))


def recent_sessions(n=2, today=None):
    if n <= 0:
        return []
    end = pd.Timestamp(today or local_now().date()).normalize()
    calendar = _calendar(end.year)
    days = calendar.sessions[calendar.sessions <= end]
    if len(days) < n:
        raise CalendarUnavailable('请求的交易日数量超出已加载日历')
    return [str(day.date()) for day in days[-n:]]


def latest_closed_session(now=None):
    from datetime import timedelta, time
    now = now or local_now()
    day = now.date() if now.time() >= time(15, 5) else now.date() - timedelta(days=1)
    return recent_sessions(1, day)[-1]
