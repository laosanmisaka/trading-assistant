"""CSV cache plus hash-bound provenance, published by atomic replacement.

Complete means every requested provider segment answered successfully. It does
not prove the provider has no omissions or that prices were historically known.
An interrupted pair replacement invalidates metadata and forces a full refetch.
"""
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from utils.atomic import atomic_json, atomic_text


def metadata(path):
    path = Path(path)
    try:
        result = json.loads(Path(str(path) + '.meta.json').read_text(encoding='utf-8'))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if result.get('schema') == 1 and result.get('complete') and result.get('sha256') == digest:
            return result
    except (OSError, ValueError):
        pass
    return None


def validate_frame(frame):
    required = ['dt', 'open', 'high', 'low', 'close', 'volume']
    frame = frame[required].copy()
    times = pd.to_datetime(frame['dt'], errors='raise')
    if times.isna().any():
        raise ValueError('Cache contains invalid timestamps')
    values = frame[required[1:]].apply(pd.to_numeric, errors='raise')
    if not np.isfinite(values.to_numpy(dtype=float)).all():
        raise ValueError('Cache contains nonfinite OHLCV')
    if ((values[['open', 'high', 'low', 'close']] <= 0).any().any()
            or (values['volume'] < 0).any()
            or (values['low'] > values[['open', 'close']].min(axis=1)).any()
            or (values['high'] < values[['open', 'close']].max(axis=1)).any()):
        raise ValueError('Cache contains invalid OHLCV ranges')
    frame[required[1:]] = values
    return frame.sort_values('dt').drop_duplicates('dt', keep='last').reset_index(drop=True)


def publish(path, frame, *, source, adjustment, start, end):
    frame = validate_frame(frame)
    if frame.empty:
        raise ValueError('Refusing to replace cache with empty data')
    text = frame.to_csv(index=False, lineterminator='\n')
    digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
    atomic_text(path, text)
    atomic_json(str(path) + '.meta.json', {
        'schema': 1, 'source': source, 'adjustment': adjustment, 'complete': True,
        'requested_start': str(start), 'requested_end': str(end), 'rows': len(frame),
        'first': str(frame['dt'].iloc[0]), 'last': str(frame['dt'].iloc[-1]),
        'sha256': digest, 'fetched_at': str(pd.Timestamp.now(tz='Asia/Shanghai')),
    })
    return frame
