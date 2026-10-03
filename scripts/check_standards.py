"""Check AGENTS limits for changed Python functions/files and Markdown documents.

Usage: python scripts/check_standards.py --base 7fbaba8
Physical code lines exclude comments, blanks and bracket/comma-only lines.
File totals additionally exclude imports; docstrings count as code text.
Cyclomatic complexity uses radon, including assertions. Untouched historical
functions are reported only with --all; changed containing functions are checked.
"""
from __future__ import annotations

import argparse
import ast
import io
from pathlib import Path
import subprocess
import textwrap
import tokenize

from radon.complexity import cc_visit

ROOT = Path(__file__).resolve().parents[1]


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True)


def functions(tree, prefix=""):
    result = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = f"{prefix}.{node.name}" if prefix else node.name
            if not isinstance(node, ast.ClassDef):
                result[name] = node
            result.update(functions(node, name))
        else:
            result.update(functions(node, prefix))
    return result


def effective_lines(source):
    lines = set()
    text_lines = source.splitlines()
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type not in (tokenize.NAME, tokenize.NUMBER, tokenize.STRING, tokenize.OP):
            continue
        if token.type == tokenize.OP and token.string in "()[]{},":
            continue
        lines.update(i for i in range(token.start[0], token.end[0] + 1)
                     if text_lines[i - 1].strip())
    return lines


def function_limits(name, node, source, lines):
    segment = textwrap.dedent(ast.get_source_segment(source, node))
    blocks = cc_visit(segment)
    complexity = blocks[0].complexity
    args = node.args
    arity = len(args.posonlyargs + args.args + args.kwonlyargs)
    arity += int(args.vararg is not None) + int(args.kwarg is not None)
    size = len(lines.intersection(range(node.lineno, node.end_lineno + 1)))
    return [f"{name}: {label} {value} > {limit}"
            for label, value, limit in [("lines", size, 100), ("CC", complexity, 20),
                                        ("arguments", arity, 9)] if value > limit]


def check_python(path, base, check_all):
    source = (ROOT / path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    lines = effective_lines(source)
    import_lines = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            import_lines.update(range(node.lineno, node.end_lineno + 1))
    errors = []
    size = len(lines - import_lines)
    if size > 1000:
        errors.append(f"file lines {size} > 1000")
    old = subprocess.run(["git", "show", f"{base}:{path}"], cwd=ROOT,
                         text=True, capture_output=True)
    previous = functions(ast.parse(old.stdout)) if old.returncode == 0 else {}
    for name, node in functions(tree).items():
        prior = previous.get(name)
        if check_all or prior is None or ast.dump(prior) != ast.dump(node):
            errors.extend(function_limits(name, node, source, lines))
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="HEAD")
    parser.add_argument("--all", action="store_true", help="also check historical functions")
    args = parser.parse_args()
    tracked = set(git("ls-files").splitlines())
    untracked = set(git("ls-files", "--others", "--exclude-standard").splitlines())
    changed = set(git("diff", "--name-only", args.base, "--").splitlines()) | untracked
    paths = sorted((tracked | untracked) if args.all else changed)
    failures = []
    for path in paths:
        file = ROOT / path
        if not file.is_file():
            continue
        if file.suffix == ".py":
            failures.extend(f"{path}: {e}" for e in check_python(path, args.base, args.all))
        elif file.suffix == ".md":
            length = len(file.read_text(encoding="utf-8"))
            if length > 30000:
                failures.append(f"{path}: characters {length} > 30000")
    for failure in failures:
        print(failure)
    print(f"Checked {len(paths)} paths against {args.base}; {len(failures)} violations")
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
