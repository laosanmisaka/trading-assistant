"""Consistent history snapshots and latest-session quote aggregation."""
from dataclasses import asdict

import pandas as pd

from data.models import KLineData, RealtimeQuote


def latest_session(bars):
    ordered = sorted({bar['timestamp']: bar for bar in bars}.values(), key=lambda b: b['timestamp'])
    if not ordered:
        return []
    day = ordered[-1]['timestamp'][:10]
    return [bar for bar in ordered if bar['timestamp'].startswith(day)]


def aggregate_daily(daily, period):
    if not daily:
        return []
    frame = pd.DataFrame([asdict(bar) for bar in daily])
    frame['dt'] = pd.to_datetime(frame['date'])
    groups = frame.groupby(frame['dt'].dt.to_period('W-FRI' if period == 'weekly' else 'M'))
    result = []
    for _, group in groups:
        result.append(KLineData(code=daily[0].code, date=group['date'].iloc[-1],
                                open=float(group['open'].iloc[0]), high=float(group['high'].max()),
                                low=float(group['low'].min()), close=float(group['close'].iloc[-1]),
                                volume=int(group['volume'].sum()), period=period))
    return result


def quote_from_history(code, daily, minute):
    bars = latest_session(minute)
    if bars:
        day, price = bars[-1]['timestamp'][:10], bars[-1]['close']
        previous = [bar.close for bar in daily if bar.date[:10] < day]
        pre_close = previous[-1] if previous else price
        op, high, low = bars[0]['open'], max(b['high'] for b in bars), min(b['low'] for b in bars)
        volume, timestamp = sum(b['volume'] for b in bars), bars[-1]['timestamp']
    elif daily:
        last = daily[-1]
        price, pre_close = last.close, daily[-2].close if len(daily) > 1 else last.close
        op, high, low, volume, timestamp = last.open, last.high, last.low, last.volume, last.date
    else:
        return None
    if price <= 0:
        return None
    return RealtimeQuote(code=code, name='', price=price,
                         change_pct=round((price / pre_close - 1) * 100, 2) if pre_close > 0 else 0,
                         change_amt=round(price - pre_close, 2), volume=volume,
                         high=high, low=low, open=op, pre_close=pre_close, timestamp=timestamp)
