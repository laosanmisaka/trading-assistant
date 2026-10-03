"""Durable event journal. Checkpoints are disposable; journal entries own history.

One monitor process owns a journal. A partial final line is recoverable; malformed
complete lines are rejected. Notifications are best-effort: a crash between
commit and desktop delivery can miss a toast, but never loses the durable event.
"""
import json
import os
from pathlib import Path

from utils.atomic import atomic_json


class JournalWriteError(OSError):
    """Stop the writer after I/O failure; never append behind a partial record."""


def identity(event):
    return event.get('event_id') or '|'.join(str(event.get(k, '')) for k in
                                           ('code', 'window_start', 'bi_idx' if 'bi_idx' in event else 'conf'))


def read_events(path, *, strict=False):
    path = Path(path)
    if not path.exists():
        return {}
    latest = {}
    lines = path.read_bytes().splitlines(keepends=True)
    for i, line in enumerate(lines):
        try:
            event = json.loads(line)
            if not isinstance(event, dict) or not event.get('code') or not event.get('conf'):
                continue
        except (json.JSONDecodeError, UnicodeDecodeError):
            if strict and (i != len(lines) - 1 or line.endswith(b'\n')):
                raise ValueError(f'Invalid signal journal line {i + 1}: {path}')
            continue
        latest[identity(event)] = event
    return latest


def recover(journal, checkpoint):
    path = Path(journal)
    if not path.exists() and Path(checkpoint).exists():
        saved = json.loads(Path(checkpoint).read_text(encoding='utf-8'))
        if saved.get('alerts') or saved.get('legacy_alerts'):
            raise ValueError('信号日志缺失但检查点有历史，请恢复日志或核对备份，拒绝清空历史')
    # Validate before removing only an interrupted final record.
    events = read_events(path, strict=True)
    if path.exists():
        raw = path.read_bytes()
        if raw and not raw.endswith(b'\n'):
            tail = raw.rsplit(b'\n', 1)[-1]
            try:
                json.loads(tail)
                with path.open('ab') as stream:
                    stream.write(b'\n')
                    stream.flush()
                    os.fsync(stream.fileno())
            except (json.JSONDecodeError, UnicodeDecodeError):
                with path.open('r+b') as stream:
                    stream.truncate(len(raw) - len(tail))
                    stream.flush()
                    os.fsync(stream.fileno())
    state = {'schema': 3, 'alerts': events}
    atomic_json(checkpoint, state)
    return state


def append_event(path, event):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False, allow_nan=False) + '\n'
    with path.open('a', encoding='utf-8') as stream:
        stream.write(line)
        stream.flush()
        os.fsync(stream.fileno())


def commit_event(state, event, journal, checkpoint):
    try:
        append_event(journal, event)
        state['alerts'][identity(event)] = event
        atomic_json(checkpoint, state)
    except OSError as error:
        raise JournalWriteError(str(error)) from error


def merge_observation(observed, previous, consumed):
    """Rolling replay can change geometry, never an already observed fill."""
    result = dict(observed)
    if previous:
        for field in ('conf', 'entry_dt', 'entry_price', 'strategy_entry'):
            result[field] = previous[field]
    else:
        result['strategy_entry'] = (result['window_id'] not in consumed
                                    and (result['code'], result['window_start']) not in consumed)
    fields = ('active', 'geometry_dt', 'geometry_price', 'strategy_entry')
    if previous and all(result.get(key) == previous.get(key) for key in fields):
        return None
    result['revision'] = previous.get('revision', 0) + 1 if previous else 0
    return result
