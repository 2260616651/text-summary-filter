#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) 2026 doc-extract-filter contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""
doc-extract-filter 按行二进制等分切卷工具。

实战来源：split_and_map.py。
- 全程二进制：rb 读 + bytes.splitlines(keepends=True) + wb 写，杜绝 CRLF 翻译。
- 按字节阈值 total*(k+1)//N 等分，切分点严格吸附在「两行之间」，绝不切断行。
- 硬校验 b''.join(volumes) == raw，不等直接 assert 失败。
- 可选 --map 生成卷边界映射文档（每卷字节数、起始/结束物理行号）。
- 输出：<前缀>1.txt ... <前缀>N.txt，打印每卷字节数与行号范围。

所有文件读写一律二进制模式（rb/wb），
禁止依赖 Windows 文本模式默认换行转换（否则 CRLF 会被改写，拼接校验必然失真）。
"""
import argparse
import os


def read_bytes(path):
    if not os.path.isfile(path):
        raise SystemExit(f"[错误] 找不到文件：{path}\n请检查路径（Windows 用正斜杠 / ）是否正确。")
    return open(path, 'rb').read()


def main():
    ap = argparse.ArgumentParser(
        description="doc-extract-filter 按行二进制等分切卷（切点在两行之间，拼接逐字节硬校验）",
        epilog=("输出：<前缀>1.txt ... <前缀>N.txt\n"
                "示例： python split_by_lines.py --source 母本.txt --volumes 7 --out-prefix 卷_ --map 映射.md"))
    ap.add_argument('--source', required=True, help="待切分文件（必填）")
    ap.add_argument('--volumes', type=int, required=True, help="卷数 N（必填，>=1）")
    ap.add_argument('--out-prefix', required=True,
                    help="输出前缀（必填），生成 <前缀><序号>.txt，序号从 1 起")
    ap.add_argument('--map', default=None,
                    help="可选：写出卷边界映射文档（每卷字节数、起止物理行号）")
    args = ap.parse_args()

    if args.volumes < 1:
        raise SystemExit("[错误] --volumes 必须 >= 1")

    raw = read_bytes(args.source)
    total = len(raw)
    if total == 0:
        raise SystemExit("[错误] 源文档为 0 字节，拒绝切分（避免产出空卷垃圾）：%s" % args.source)
    # bytes 版 splitlines(keepends=True)：每个元素含行尾换行，切点天然落在两行之间
    lines = raw.splitlines(keepends=True)
    if not lines or all(not x.strip() for x in lines):
        raise SystemExit("[错误] 源文档没有任何非空白内容，拒绝切分：%s" % args.source)
    n = args.volumes
    if n > len(lines):
        raise SystemExit("[错误] --volumes %d 超过物理行数 %d：请减小卷数（每卷至少 1 行）"
                         % (n, len(lines)))

    # 按字节阈值等分，吸附到「当前行末尾」（即两行之间）
    targets = [total * (k + 1) // n for k in range(n - 1)]
    bounds = [0]
    cum = 0
    j = 0
    for idx, line in enumerate(lines):
        cum += len(line)
        while j < len(targets) and cum >= targets[j]:
            bounds.append(idx + 1)
            j += 1
    while len(bounds) < n + 1:
        bounds.append(len(lines))
    bounds = bounds[:n + 1]
    bounds[-1] = len(lines)

    volumes = [b''.join(lines[bounds[k]:bounds[k + 1]]) for k in range(n)]

    # 硬校验：拼接必须逐字节等于母本
    assert b''.join(volumes) == raw, "[致命] 拼接校验失败：分卷合并 != 原文件"

    outdir = os.path.dirname(os.path.abspath(args.out_prefix))
    if outdir:
        os.makedirs(outdir, exist_ok=True)

    print("=== 按行二进制等分切卷 ===")
    print(f"  源文件：{args.source}（{total} 字节 / {len(lines)} 物理行）")
    print(f"  卷数：{n} | 拼接校验 b''.join(volumes) == raw ：PASS")
    map_rows = []
    for k in range(n):
        a, b = bounds[k], bounds[k + 1]
        data = volumes[k]
        path = f"{args.out_prefix}{k + 1}.txt"
        # 二进制写：禁止依赖 Windows 文本模式默认换行转换
        open(path, 'wb').write(data)
        lo, hi = a + 1, b
        print(f"  卷{k + 1:>2d}: {len(data):>8d} 字节  行号 L{lo}–L{hi}  -> {path}")
        map_rows.append((k + 1, len(data), lo, hi, os.path.basename(path)))

    if args.map:
        os.makedirs(os.path.dirname(os.path.abspath(args.map)), exist_ok=True)
        md = [
            "# 分卷边界映射（按行二进制等分）",
            "",
            f"- 源文件：`{os.path.basename(args.source)}`（{total} 字节 / {len(lines)} 物理行）",
            f"- 卷数：{n}；切分点严格落在两行之间，绝不切断行",
            "- 拼接校验：`b''.join(volumes) == raw` PASS（逐字节一致）",
            "",
            "| 卷 | 字节数 | 起始物理行 | 结束物理行 | 文件 |",
            "|---|---|---|---|---|",
        ]
        for k, sz, lo, hi, name in map_rows:
            md.append(f"| 卷{k} | {sz} | L{lo} | L{hi} | `{name}` |")
        open(args.map, 'wb').write(('\n'.join(md) + '\n').encode('utf-8'))
        print(f"  映射文档：{args.map}")


if __name__ == '__main__':
    main()
