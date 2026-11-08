#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) 2026 text-summary-filter contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""
text-summary-filter 判定视图生成工具。

实战来源：make_view.py。
生成紧凑视图：每行 = L<物理行号>| <首 N 字>…<尾 M 字>；短行原样；空行标 (空行)。
让判定 agent 在有限上下文里通读全量行；视图行号与母本物理行号严格一一对应。
可选 --index 导出行元数据（字节数 / 字符数 / 是否空行）。

所有文件读写一律二进制模式（rb/wb）或显式 newline='' + encoding='utf-8'，
禁止依赖 Windows 文本模式默认换行转换（否则 CRLF 会漂移、行号对不上）。
"""
import argparse
import json
import os


def read_bytes(path):
    if not os.path.isfile(path):
        raise SystemExit(f"[错误] 找不到文件：{path}\n请检查路径（Windows 用正斜杠 / ）是否正确。")
    return open(path, 'rb').read()


def main():
    ap = argparse.ArgumentParser(
        description="text-summary-filter 生成紧凑判定视图（L行号|首N…尾M），行号与母本严格一一对应",
        epilog="示例： python build_line_view.py --source 母本.txt --out 视图.txt --index 行元数据.json")
    ap.add_argument('--source', required=True, help="母本文件（必填）")
    ap.add_argument('--out', required=True, help="视图输出路径（必填）")
    ap.add_argument('--head', type=int, default=55, help="长行保留的前 N 字，默认 55")
    ap.add_argument('--tail', type=int, default=20, help="长行保留的后 M 字，默认 20")
    ap.add_argument('--index', default=None,
                    help="可选：导出行元数据 JSON（字节数/字符数/是否空行）")
    args = ap.parse_args()

    if args.head < 0 or args.tail < 0:
        raise SystemExit("[错误] --head / --tail 必须 >= 0")

    raw = read_bytes(args.source)
    # splitlines() 无 keepends：每个 bytes 元素 = 该物理行内容（不含行尾换行）。
    # 元素个数与 keepends 版严格一致，视图行号 i 直接对应母本第 i 个物理行。
    lines = raw.splitlines()
    assert len(lines) == len(raw.splitlines(keepends=True)), \
        "[致命] 视图行与母本物理行数量不一致"

    out_lines = []
    meta = {}
    for i, line in enumerate(lines, 1):
        try:
            s = line.decode('utf-8')
        except UnicodeDecodeError:
            s = line.decode('utf-8', errors='replace')
        disp_src = s.replace('\t', ' ')
        stripped = disp_src.strip()
        n = len(stripped)
        meta[str(i)] = {
            'bytes': len(line),   # 该行内容（不含换行）的字节数
            'chars': n,           # 去首尾空白后的字符数
            'empty': (n == 0),    # 是否空行
        }
        if n == 0:
            disp = '(空行)'
        elif n <= args.head + args.tail:
            disp = stripped       # 短行原样
        else:
            head = stripped[:args.head] if args.head > 0 else ''
            tail = stripped[-args.tail:] if args.tail > 0 else ''
            disp = head + '…' + tail
        out_lines.append(f"L{i}| {disp}")

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    # 二进制写：禁止依赖 Windows 文本模式默认换行转换
    open(args.out, 'wb').write(('\n'.join(out_lines) + '\n').encode('utf-8'))

    print("=== 判定视图生成 ===")
    print(f"  母本：{args.source}（{len(raw)} 字节）")
    print(f"  物理行：{len(lines)} | 视图行：{len(out_lines)}（严格一一对应）")
    print(f"  非空行：{sum(1 for v in meta.values() if not v['empty'])}"
          f" | 空行：{sum(1 for v in meta.values() if v['empty'])}")
    print(f"  头/尾截取：首 {args.head} 字 … 尾 {args.tail} 字")
    print(f"  视图：{args.out}")
    if args.index:
        os.makedirs(os.path.dirname(os.path.abspath(args.index)), exist_ok=True)
        open(args.index, 'wb').write(json.dumps(meta, ensure_ascii=False, indent=1).encode('utf-8'))
        print(f"  行元数据：{args.index}")


if __name__ == '__main__':
    main()
