#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) 2026 doc-extract-filter contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""
doc-extract-filter 合并与切块脚本（合成 / 合并代码）

职责（对应 Skill「多文档合并总结 + 机械合并/递归重切」）：
  1) 把多份源文档**机械首尾连接**成一份母本（每份前插硬边界标记，仅溯源）
  2) 把已合成的大型总总结**递归重切**，以便二次生成缩略文档
  3) 复用 split_document.py 的等宽 / 重叠 / 不切断行逻辑切块

机械拼接与切块都走这里，保证"24 源硬拼接"和"总总结递归重切"用同一套
等宽+重叠+不切断行规则，与 doc-extract-filter 标准流程保持一致。

用法：
  # 多源硬拼接 + 切块（24 篇终稿 / 多份缩略档合并母本）
  python merge_and_chunk.py --inputs a.md b.md c.md --out merged_chunks

  # 文档集开始时先机械合并成一个大文档（纯母本：二进制拼接/剥BOM/CRLF归一/绝不改写正文）
  python merge_and_chunk.py --inputs a.md b.md c.md --merged-out 母本.txt --out merged_chunks

  # 已合成的总总结递归重切（超单批次上下文时）
  python merge_and_chunk.py --file 总总结.txt --out chunks

  # 只看体量（不切）
  python merge_and_chunk.py --inputs a.md b.md --analyze
"""
import argparse
import json
import os
import sys

# 复用同目录切分脚本的等宽/重叠/不切断行逻辑
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import split_document as sd  # noqa: E402


SRC_MARK = "### 来源文件：{name} ###"


def read_text(path):
    """读源文档：剥离 UTF-8 BOM、统一换行为 \\n。

    本脚本产出的是「总结用切块」中间稿，不用于逐字节还原；字节级拼接/还原
    由 split_by_lines.py 负责（二进制模式）。统一换行可避免 CRLF 源与
    "\\n\\n".join 拼接后出现混合换行、句边界被 \\r 干扰。
    """
    if not os.path.isfile(path):
        raise SystemExit(
            f"[错误] 找不到文件：{path}\n"
            f"请检查路径是否正确（Windows 路径用正斜杠 / 或双反斜杠 \\\\，不要混用）。")
    with open(path, 'r', encoding='utf-8-sig', newline=None) as f:
        return f.read().lstrip('\ufeff').replace('\r\n', '\n').replace('\r', '\n')


def concat_sources(paths):
    """机械首尾连接多份源，每份前插硬边界标记（仅溯源，不计入总结）。

    各源换行已由 read_text 统一为 \\n，故拼接结果不会混入 CRLF。
    """
    parts = []
    for p in paths:
        name = os.path.basename(p)
        body = read_text(p)
        if not body.strip():
            print(f"  [警告] 源文档为空或仅空白：{p}（仍保留其来源标记，但不贡献内容）")
        parts.append(SRC_MARK.format(name=name))
        parts.append(body)
    return "\n\n".join(parts)

def concat_binary(paths):
    """机械拼接多份源为纯母本：二进制读、剥 UTF-8 BOM、CRLF/CR 归一为 LF、绝不改写正文。
    不插来源标记（纯母本），各源之间仅用一个空行分隔。
    """
    parts = []
    for p in paths:
        with open(p, "rb") as f:
            b = f.read()
        if b.startswith(b"\xef\xbb\xbf"):
            b = b[3:]
        b = b.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        parts.append(b)
    return b"\n\n".join(parts)


def _derive_size(text, args):
    """推导最终块大小（全局硬红线：块必须严格 < 模型上下文的 1/2）。

    --context-cap 是「上下文容量」，块上限 = context_cap // 2（half_cap）。
    第 1 步切块总结 / 第 5 步检查重切块 / 第 6 步切块检查都受此约束，
    第 5 步重切块固定以 half_cap 为上限。
    信息密度下限 floor（默认 40000）只是「下限建议」，绝不允许凌驾该红线：
    当 floor >= half_cap 时忽略 floor 顶回、强制缩小块并打印明确告警
    （上下文太小、块被强制缩小、块数会变多）。
    打印 ideal / floor / cap / half_cap / 最终块大小 / 钳制原因，不再静默钳死。
    """
    ratio, band = sd.estimate_density(text)
    floor = 10000 if band == 'concentrated' else args.min_chunk
    cap = args.context_cap
    half_cap = (cap // 2) if cap else None
    ideal = None
    clamp_reasons = []

    if args.chunk_size:
        size = float(args.chunk_size)
        ideal = size
        plan = f"手动 --chunk-size {args.chunk_size}"
    else:
        ideal = sd.TARGET_SUMMARY / (ratio * sd.RETENTION)
        size = ideal * (args.looseness or 1.0)
        size = max(size, floor)
        plan = (f"信息密度 band={band} 有用率≈{ratio:.2f} → 理想 {ideal:.0f} "
                f"× 松散{args.looseness or 1.0} → {size:.0f}，下限 {floor}（仅建议）")

    if size > sd.MAX_CHUNK:
        clamp_reasons.append(f"MAX_CHUNK={sd.MAX_CHUNK} 钳制")
        size = float(sd.MAX_CHUNK)

    if half_cap is not None:
        allowed = max(1, half_cap - 1)   # 严格小于 half_cap（留 1 字符安全余量）
        if floor >= half_cap:
            print(f"  [告警] 上下文太小：--context-cap={cap} → half_cap={half_cap}，"
                  f"信息密度下限 floor={floor} >= half_cap；已忽略 floor 顶回，"
                  f"块被强制缩小到 <{half_cap}（块数会变多）。")
        if size < floor and floor < half_cap:
            size = float(floor)
        if size >= half_cap:
            clamp_reasons.append(
                f"硬红线：块={size:.0f} 必须严格 < 1/2 上下文(half_cap={half_cap}) → 钳到 {allowed}")
            size = float(allowed)
        plan += (f" | --context-cap {cap} → half_cap {half_cap}"
                 f"（块上限=其 1/2，最终块必须 < {half_cap}）")
    else:
        size = max(float(size), float(floor))

    size = int(size)
    clamp = "；".join(clamp_reasons) if clamp_reasons else "无（未触发钳制）"
    print(f"  [块大小诊断] ideal={ideal} floor={floor} cap={cap} half_cap={half_cap} "
          f"最终块大小={size} 钳制原因={clamp}")
    return size, plan


def main():
    ap = argparse.ArgumentParser(description="doc-extract-filter 合并与切块")
    ap.add_argument('--inputs', nargs='+', help="多份源文档，机械拼接为母本")
    ap.add_argument('--merged-out', default=None,
                    help="文档集：先把多份源二进制机械拼接成纯母本写到该文件（剥BOM/CRLF归一/绝不改写正文/不插来源标记）")
    ap.add_argument('--file', default=None, help="已合并的单文件（递归重切用）")
    ap.add_argument('--analyze', action='store_true', help="只做体量分析，不切")
    ap.add_argument('--sents-per-chunk', type=int, default=12)
    ap.add_argument('--chunk-size', type=int, default=None)
    ap.add_argument('--min-chunk', type=int, default=40000)
    ap.add_argument('--looseness', type=float, default=1.0)
    ap.add_argument('--context-cap', type=int, default=None,
                    help="上下文容量（减去系统提示词后的可用字符/token）；块大小上限 = 其 1/2（严格小于），"
                         "任何时候切出来的块都必须 < 1/2 上下文；floor 下限建议不得凌驾此红线")
    ap.add_argument('--overlap', type=float, default=0.0)
    ap.add_argument('--overlap-chars', type=int, default=None)
    ap.add_argument('--overlap-auto', action='store_true')
    ap.add_argument('--out', default=None)
    ap.add_argument('--no-mark', action='store_true')
    args = ap.parse_args()

    if args.inputs:
        if args.merged_out:
            # 文档集：开始时先机械合并成一个大文档（纯母本），后续所有步骤基于这一个母本
            raw_merged = concat_binary(args.inputs)
            with open(args.merged_out, "wb") as f:
                f.write(raw_merged)
            text = raw_merged.decode("utf-8", errors="replace")
            print(f"[合并] 已二进制机械拼接 {len(args.inputs)} 份源为纯母本 → {args.merged_out}"
                  f"（{len(text)} 字符；剥BOM/CRLF归一/绝不改写正文）")
        else:
            text = concat_sources(args.inputs)
            print(f"[合并] 已机械拼接 {len(args.inputs)} 份源为母本（{len(text)} 字符）")
    elif args.file:
        text = read_text(args.file)
        print(f"[读取] 已合成文档 {args.file}（{len(text)} 字符）")
    else:
        ap.error("必须提供 --inputs 或 --file")

    if args.analyze:
        sd.analyze(text, args.sents_per_chunk)
        return

    _explicit_overlap = any(x == '--overlap' or x.startswith('--overlap=') or
                            x == '--overlap-chars' or x.startswith('--overlap-chars=')
                            for x in sys.argv[1:])
    if args.overlap_auto or (not _explicit_overlap and not args.overlap_chars):
        # 现行口径「默认 1-4kb」真正落地；显式给比例/固定字符数时按手动覆盖
        args.overlap_chars = sd.auto_overlap_chars(len(text))
        print(f"[overlap-auto] 默认 1-4kb（按信息密度升降档）：重叠量取 {args.overlap_chars} 字符")

    size, plan = _derive_size(text, args)
    print(f"[块大小] {plan} → 最终 {size} 字符")

    sents = sd.split_sentences(text)
    base = sd.split_equal_chunks(sents, size)
    chunks = sd.inject_overlap(base, args.overlap, args.overlap_chars, mark=not args.no_mark)

    manifest = {
        'total_chunks': len(chunks),
        'chunk_size_target': size,
        'overlap_frac': args.overlap,
        'overlap_chars_abs': args.overlap_chars,
        'source_mode': 'merged' if args.inputs else 'single',
        'chunks': [
            {'index': c['index'], 'char_count': c['char_count'],
             'overlap_char_count': c['overlap_char_count'],
             'overlap_source': c['overlap_source']}
            for c in chunks
        ],
    }

    if args.out:
        os.makedirs(args.out, exist_ok=True)
        with open(os.path.join(args.out, 'chunks.json'), 'w', encoding='utf-8') as f:
            json.dump({'manifest': manifest, 'chunks': chunks}, f, ensure_ascii=False, indent=2)
        for c in chunks:
            with open(os.path.join(args.out, f"chunk_{c['index'] + 1:03d}.txt"), 'w', encoding='utf-8') as f:
                f.write(c['text'])
        print(f"已写出 {len(chunks)} 块到 {args.out}/")
    else:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
