"""Check repository Markdown length and local link targets, including new files."""
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


def main():
    files = subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard'],
                                    cwd=ROOT, text=True).splitlines()
    errors, count = [], 0
    for filename in sorted(set(files)):
        path = ROOT / filename
        if path.suffix != '.md' or not path.is_file():
            continue
        count += 1
        source = path.read_text(encoding='utf-8')
        if len(source) > 30000:
            errors.append(f'{filename}: {len(source)} characters > 30000')
        # Fenced examples and inline code are not navigable Markdown links.
        source = re.sub(r'```.*?```', '', source, flags=re.S)
        source = re.sub(r'`[^`]*`', '', source)
        targets = re.findall(r'\]\(([^\s)]+)(?:\s+"[^"\\n]*")?\)', source)
        for raw in targets:
            target = urlsplit(raw.strip('<>'))
            if target.scheme or target.netloc or not target.path:
                continue
            local = path.parent / unquote(target.path)
            if not local.exists():
                errors.append(f'{filename}: missing link {raw}')
    print('\n'.join(errors))
    print(f'Checked {count} Markdown documents; {len(errors)} errors')
    return bool(errors)


if __name__ == '__main__':
    raise SystemExit(main())
