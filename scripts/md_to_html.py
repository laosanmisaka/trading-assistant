# -*- coding: utf-8 -*-
"""Markdown 报告 → **单文件** HTML（自包含、无外链、手机能看）

    python scripts/md_to_html.py outputs/daily5min_any_full50.md
    python scripts/md_to_html.py outputs/daily5min_any_full50.md --out /tmp/x.html
    python scripts/md_to_html.py --all            # outputs/*.md → *.html + index.html
    python scripts/md_to_html.py --all --out-dir outputs/html

为什么不用现成的 markdown 库
----------------------------
1. **不引依赖**：项目 venv 里没有 `markdown`，为了发个报告装一个包不值当。
2. 报告结构完全可控（标题 / 表格 / 列表 / 粗体 / 行内代码 / 引用 / 分隔线），
   手写转换器 60 行就够，且能用上 CSS 把**表格**做成手机可横向滚动 —— 这是
   报告里最要紧的元素（数字多、列宽），通用库反而要额外配插件。

生成的是一个 HTML 文件、CSS 内嵌 ⇒ 微信/邮件直接发，对方不用装任何东西。
"""
from __future__ import annotations

import argparse
import html as _html
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = ROOT / "outputs"

CSS = """
:root{color-scheme:light}
*{box-sizing:border-box}
body{margin:0;padding:24px 16px 64px;background:#f6f7f9;color:#1f2328;
 font:15px/1.75 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
main{max-width:1000px;margin:0 auto;background:#fff;border:1px solid #e3e6ea;border-radius:10px;
 padding:28px 30px 40px;box-shadow:0 1px 3px rgba(0,0,0,.05)}
h1{font-size:23px;margin:0 0 18px;padding-bottom:12px;border-bottom:2px solid #eef0f3;line-height:1.4}
h2{font-size:18px;margin:34px 0 12px;padding-left:10px;border-left:4px solid #4c7df0}
h3{font-size:15.5px;margin:22px 0 8px;color:#2b3038}
p{margin:10px 0}
table{border-collapse:collapse;width:100%;margin:12px 0;font-size:13px;
 font-variant-numeric:tabular-nums}
.wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
th,td{border:1px solid #e3e6ea;padding:6px 10px;text-align:right;white-space:nowrap}
th{background:#f2f4f7;font-weight:600}
td:first-child,th:first-child{text-align:left}
tbody tr:nth-child(even){background:#fafbfc}
code{background:#f2f4f7;padding:1px 5px;border-radius:4px;font-size:12.5px;
 font-family:ui-monospace,Consolas,monospace}
blockquote{margin:12px 0;padding:8px 14px;background:#fff8e6;border-left:4px solid #f0b429;color:#5c4708}
ul,ol{margin:10px 0;padding-left:24px}
li{margin:4px 0}
hr{border:0;border-top:1px solid #e3e6ea;margin:28px 0}
a{color:#2f6fe4}
footer{margin-top:30px;color:#8a9199;font-size:12px;text-align:center}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px;margin-top:16px}
.card{display:block;padding:14px 16px;border:1px solid #e3e6ea;border-radius:8px;
 background:#fff;text-decoration:none;color:inherit;transition:.15s}
.card:hover{border-color:#4c7df0;box-shadow:0 2px 8px rgba(76,125,240,.12)}
.card b{display:block;font-size:14.5px;margin-bottom:4px}
.card span{color:#8a9199;font-size:12px}
@media (max-width:640px){body{padding:12px 8px 40px}main{padding:18px 14px 28px;border-radius:8px}
 h1{font-size:19px}h2{font-size:16.5px}th,td{padding:5px 7px;font-size:12px}}
"""


def _inline(s: str) -> str:
    s = _html.escape(s, quote=False)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    return s


def _cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


_SEP = re.compile(r"^\|?[\s\-:|]+\|?$")
_H = re.compile(r"^(#{1,6})\s+(.*)$")
_UL = re.compile(r"^\s*[-*]\s+(.*)$")
_OL = re.compile(r"^\s*\d+\.\s+(.*)$")
_STOP = ("|", "#", "-", "*", ">", "`")


def md_to_html(md: str) -> str:
    lines = md.splitlines()
    n = len(lines)
    out: list[str] = []
    i = 0
    while i < n:
        s = lines[i].strip()
        if not s:
            i += 1
            continue

        # ---- 表格（下一行是分隔行才算） ----
        if s.startswith("|") and i + 1 < n and _SEP.match(lines[i + 1].strip()):
            head = _cells(lines[i])
            i += 2
            body: list[list[str]] = []
            while i < n and lines[i].strip().startswith("|"):
                body.append(_cells(lines[i]))
                i += 1
            out.append(
                '<div class="wrap"><table><thead><tr>'
                + "".join(f"<th>{_inline(c)}</th>" for c in head)
                + "</tr></thead><tbody>"
                + "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>"
                          for r in body)
                + "</tbody></table></div>")
            continue

        # ---- 标题 ----
        m = _H.match(s)
        if m:
            lv = len(m.group(1))
            out.append(f"<h{lv}>{_inline(m.group(2))}</h{lv}>")
            i += 1
            continue

        # ---- 分隔线 ----
        if re.match(r"^(\*{3,}|-{3,}|_{3,})$", s):
            out.append("<hr>")
            i += 1
            continue

        # ---- 无序 / 有序列表 ----
        if _UL.match(lines[i]):
            items = []
            while i < n and _UL.match(lines[i]):
                items.append(_UL.match(lines[i]).group(1))
                i += 1
            out.append("<ul>" + "".join(f"<li>{_inline(x)}</li>" for x in items) + "</ul>")
            continue
        if _OL.match(lines[i]):
            items = []
            while i < n and _OL.match(lines[i]):
                items.append(_OL.match(lines[i]).group(1))
                i += 1
            out.append("<ol>" + "".join(f"<li>{_inline(x)}</li>" for x in items) + "</ol>")
            continue

        # ---- 引用 ----
        if s.startswith(">"):
            out.append(f"<blockquote>{_inline(s.lstrip('> ').strip())}</blockquote>")
            i += 1
            continue

        # ---- 段落：合并连续普通行 ----
        buf = [s]
        i += 1
        while i < n:
            t = lines[i].strip()
            if not t or t.startswith(_STOP) or _OL.match(lines[i]) or _H.match(t):
                break
            buf.append(t)
            i += 1
        out.append("<p>" + _inline(" ".join(buf)) + "</p>")
    return "\n".join(out)


def page(title: str, body: str, *, subtitle: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_html.escape(title)}</title>
<style>{CSS}</style></head>
<body><main>
{body}
<footer>{_html.escape(subtitle) or f"生成于 {datetime.now():%Y-%m-%d %H:%M}"}</footer>
</main></body></html>
"""


def convert(src: Path, dst: Path | None = None) -> Path:
    md = src.read_text(encoding="utf-8")
    m = re.search(r"^#\s+(.+)$", md, re.M)
    title = m.group(1).replace("**", "") if m else src.stem
    dst = dst or src.with_suffix(".html")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(page(title, md_to_html(md)), encoding="utf-8")
    return dst


def build_index(mds: list[Path], out_dir: Path) -> Path:
    """所有报告 → 一个目录页（按修改时间倒序）"""
    items = sorted(mds, key=lambda p: p.stat().st_mtime, reverse=True)
    cards = []
    for p in items:
        t = re.search(r"^#\s+(.+)$", p.read_text(encoding="utf-8"), re.M)
        title = t.group(1).replace("**", "") if t else p.stem
        when = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        cards.append(f'<a class="card" href="{p.with_suffix(".html").name}">'
                     f"<b>{_html.escape(title)}</b><span>{p.name} · {when}</span></a>")
    idx = out_dir / "index.html"
    idx.write_text(page("trading-assistant 报告目录",
                        f"<h1>trading-assistant 报告目录</h1>\n"
                        f"<p>共 {len(items)} 份，按更新时间倒序。</p>\n"
                        f'<div class="grid">{"".join(cards)}</div>'),
                   encoding="utf-8")
    return idx


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Markdown 报告 → 单文件 HTML")
    ap.add_argument("files", nargs="*", type=Path, help="要转换的 .md（可多个）")
    ap.add_argument("--all", action="store_true", help="转换 outputs 下所有 *.md（跳过 _ 开头）")
    ap.add_argument("--dir", type=Path, default=DEFAULT_DIR, help="--all 的扫描目录")
    ap.add_argument("--out", type=Path, default=None, help="单文件时的输出路径")
    ap.add_argument("--out-dir", type=Path, default=None, help="统一输出目录")
    a = ap.parse_args(argv)

    if a.all:
        mds = [p for p in sorted(a.dir.glob("*.md")) if not p.name.startswith("_")]
        if not mds:
            print(f"{a.dir} 下没有 *.md", file=sys.stderr)
            return 1
        out_dir = a.out_dir or a.dir
        out_dir.mkdir(parents=True, exist_ok=True)
        for p in mds:
            print(f"  {p.name} → {convert(p, out_dir / p.with_suffix('.html').name).name}",
                  flush=True)
        idx = build_index(mds, out_dir)
        print(f"目录页：{idx}（{len(mds)} 份）")
        return 0

    if not a.files:
        ap.print_help()
        return 2
    if a.out and len(a.files) > 1:
        print("--out 只能配单个输入文件", file=sys.stderr)
        return 2
    for p in a.files:
        if not p.exists():
            print(f"✗ 不存在：{p}", file=sys.stderr)
            continue
        dst = a.out or (a.out_dir / p.with_suffix(".html").name if a.out_dir else None)
        print(f"{p} → {convert(p, dst)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
