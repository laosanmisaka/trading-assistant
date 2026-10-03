"""Small reproducibility manifest alongside research reports; no market data copied."""
import hashlib
from importlib.metadata import version
from pathlib import Path
import platform
import subprocess

from data.cache_files import metadata
from utils.atomic import atomic_json

ROOT = Path(__file__).resolve().parents[1]


def fingerprint(path):
    path = Path(path)
    return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'bytes': path.stat().st_size}


def write_manifest(path, *, codes, cache_dir, parameters, pool=None):
    inputs, missing = [], []
    for code in codes:
        for period in ('daily', '5min'):
            file = Path(cache_dir) / f'{period}_{code}.csv'
            if file.exists():
                inputs.append({**fingerprint(file), 'metadata': metadata(file),
                               'provenance': 'verified' if metadata(file) else 'legacy-unverified'})
            else:
                missing.append(str(file))
    sources = [fingerprint(file) for folder in ('core', 'scripts', 'data', 'utils')
               for file in sorted((ROOT / folder).rglob('*.py'))]
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    dirty = bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True))
    packages = {name: version(name) for name in ('czsc', 'numpy', 'pandas', 'baostock', 'exchange_calendars')}
    atomic_json(path, {'method': 'observable-v1', 'commit': commit, 'dirty': dirty,
                      'source_files': sources, 'python': platform.python_version(), 'packages': packages,
                      'parameters': parameters, 'pool': fingerprint(pool) if pool else None,
                      'codes': list(codes), 'inputs': inputs, 'missing': missing,
                      'limits': ['Current survivor pool', 'Close fills omit costs and slippage',
                                 'Adjusted data may contain later revisions', 'Bar-count holding period']})
