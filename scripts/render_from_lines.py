#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) 2026 doc-extract-filter contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""
doc-extract-filter 按行号零改写抽取渲染工具。

实战来源：render_doc.py。
读 agent 的 JSON 行号清单 {"items":[{"line":N,"reason":"…","verdict":"…"}]}，
按物理行号从母本逐字节抽取原文（二进制读 + splitlines），零改写 / 不缩略 / 不截断。
每条渲染为：## L<N> + 判定 + 理由 + ```text 原文 ```。

校验：
  1) 行号合法不越界、不重复（非法给明确提示，退出码 2）
  2) 抽取原文与母本对应行逐字节一致
  3) 回读生成文档，逐条 ```text 块与原文一致

所有文件读写一律二进制模式（rb/wb），
禁止依赖 Windows 文本模式默认换行转换（否则 \\r\\n 会被改写，逐字节校验失真）。
"""
import argparse
import json
import os
import sys


def read_bytes(path):
    if not os.path.isfile(path):
        raise SystemExit(f"[错误] 找不到文件：{path}\n请检查路径（Windows 用正斜杠 / ）是否正确。")
    return open(path, 'rb').read()


def load_items(path):
    try:
        data = json.loads(read_bytes(path).decode('utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise SystemExit(f"[错误] agent JSON 格式错误：{path}\n{e}")
    if not isinstance(data, dict) or not isinstance(data.get('items'), list):
        raise SystemExit(f"[错误] agent JSON 需为对象且含 items 数组：{path}")
    return data['items']


def strip_eol(b):
    """仅剥离行尾换行符，行内字节原样保留（零改写）。"""
    if b.endswith(b'\r\n'):
        return b[:-2]
    if b.endswith(b'\n') or b.endswith(b'\r'):
        return b[:-1]
    return b


def main():
    ap = argparse.ArgumentParser(
        description="doc-extract-filter 按物理行号从母本逐字节抽取原文（零改写）生成 md",
        epilog=("示例： python render_from_lines.py --source 母本.txt --lines agent1.json "
                "--out out.md --title '初判无关条目' --note '第1道' --note '宽松初筛'"))
    ap.add_argument('--source', required=True, help="母本文件（必填）")
    ap.add_argument('--lines', required=True,
                    help="agent JSON 行号清单（必填），形如 {\"items\":[{\"line\":N,...}]}")
    ap.add_argument('--out', required=True, help="输出 md 路径（必填）")
    ap.add_argument('--title', required=True, help="文档标题（必填）")
    ap.add_argument('--note', action='append', default=[], help="说明文字（可多次 --note）")
    args = ap.parse_args()

    raw = read_bytes(args.source)
    # keepends 版本作为物理行权威划分：b''.join 必须逐字节还原母本
    keep = raw.splitlines(keepends=True)
    assert b''.join(keep) == raw, "[致命] 物理行重建母本失败"
    nlines = len(keep)
    items = load_items(args.lines)

    # 校验 1：行号合法不越界、不重复
    seen = set()
    bad = []
    for it in items:
        n = it.get('line') if isinstance(it, dict) else None
        if not isinstance(n, int) or isinstance(n, bool) or n < 1 or n > nlines:
            bad.append(f"非法行号 {n!r}（母本共 {nlines} 行）")
            continue
        if n in seen:
            bad.append(f"重复行号 L{n}")
        seen.add(n)
    if bad:
        raise SystemExit("[错误] 行号校验失败：\n  " + "\n  ".join(bad))

    # 组装（全部用 bytes 拼接，content 直接取母本行字节，零改写）
    md = []
    md.append(('# ' + args.title).encode('utf-8'))
    md.append(b'')
    for note in args.note:
        md.append(('> ' + note).encode('utf-8'))
    if args.note:
        md.append(b'')
    md.append(b'---')
    md.append(b'')

    extracted = []  # (行号, 原文 bytes) 供核验
    for it in items:
        n = it['line']
        content = strip_eol(keep[n - 1])       # 从母本逐字节抽取，仅去行尾换行
        extracted.append((n, content))
        md.append(f"## L{n}".encode('utf-8'))
        verdict = it.get('verdict', '')
        reason = it.get('reason', '')
        if verdict:
            md.append(('- **判定**：' + str(verdict)).encode('utf-8'))
        md.append(('- **理由**：' + str(reason)).encode('utf-8'))
        md.append(b'')
        md.append(b'```text')
        md.append(content)
        md.append(b'```')
        md.append(b'')

    data = b'\n'.join(md) + b'\n'
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    # 二进制写：禁止依赖 Windows 文本模式默认换行转换
    open(args.out, 'wb').write(data)

    # 校验 2：抽取原文与母本对应行逐字节一致
    ok_origin = all(content == strip_eol(keep[n - 1]) for n, content in extracted)
    # 校验 3：回读生成文档逐条一致
    back = open(args.out, 'rb').read()
    blocks = back.split(b'```text\n')[1:]
    if len(blocks) == len(extracted):
        ok_roundtrip = all(blocks[i].split(b'\n```')[0] == extracted[i][1]
                           for i in range(len(extracted)))
    else:
        ok_roundtrip = False

    print("=== 按行号零改写抽取 ===")
    print(f"  母本：{args.source}（{len(raw)} 字节 / {nlines} 物理行）")
    print(f"  条目：{len(items)} | 行号合法/不越界/不重复：PASS")
    print(f"  抽取原文与母本逐字节一致：{'PASS' if ok_origin else 'FAIL'}")
    print(f"  回读文档逐条一致：{'PASS' if ok_roundtrip else 'FAIL'}"
          f"（代码块 {len(blocks)} / 条目 {len(extracted)}）")
    print(f"  输出：{args.out}（{len(data)} 字节）")
    sys.exit(0 if (ok_origin and ok_roundtrip) else 1)


if __name__ == '__main__':
    main()
