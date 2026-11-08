#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) 2026 doc-extract-filter contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""
doc-extract-filter 切分脚本
- 分析文档"自生专注力"，由文档自身特征推导块大小
- 切成大小相等的块（吸附到句子自然边界，绝不切断行）
- 默认 1-4kb 重叠（可按文档信息密度升降档），重叠区打 ⟦OVERLAP⟧ 标记
仅用标准库。
"""
import argparse
import json
import os
import re
import sys

SENT_SPLIT = re.compile(r'(?<=[。！？!?；;\n])')
# markdown 结构行识别：用于「结构不可分单元」与 --prefer-headings 的边界吸附
FENCE_LINE = re.compile(r'^\s*```')        # 围栏起止行（``` 或 ```python 等语言标记）
TABLE_LINE = re.compile(r'^\s*\|')         # 表格行（含 |---| 分隔行）
HEADING_LINE = re.compile(r'^\s*#{1,6} ')  # ATX 标题行（# 后须有空格）
OVERLAP_HEAD = "\n⟦OVERLAP 来自块{src}尾部·重复区⟧\n"
OVERLAP_TAIL = "\n⟦OVERLAP 结束⟧\n"
MIN_CHUNK = 40000  # 块大小下限「建议」（信息密度推导用）：用户规定「一个文档块最少 40kb」(≈40000 中文字符)。
                   # ⚠️ 全局硬红线：任何时候切出来的块都必须严格 < 模型上下文的 1/2；
                   # 本下限不得凌驾于 half_cap 红线——当 half_cap <= 40000 时忽略本下限、强制缩小块。
MAX_CHUNK = 400000 # 40kb × 10：松散结构乘到上限的边界；实际还受 --context-cap 与 half_cap 约束
MAX_CHUNKS = 200   # 规模保护：块数上限，超过则放大块大小，避免短句流被切成过碎
TARGET_SUMMARY = 3000   # 单块总结后目标文本量(字符)：用户要求压在 2000~4000
RETENTION = 0.85        # 摘要对"有用信息"的保留率（其余为冗余/寒暄被压缩掉）


def auto_overlap_chars(total_chars):
    # 现行口径：默认 1-4kb 绝对值重叠（1024~4096 字符），可按文档信息密度升降档。
    # 文档越大/越松散，边界语境越贵，取大档保衔接；密集条款式取小档省 token。
    # 需钉死固定值时用 --overlap-chars 手动覆盖。
    if total_chars >= 500_000:
        return 4096
    if total_chars >= 100_000:
        return 2048
    return 1024


def estimate_density(text):
    """基于信息含量启发式估算'有用信息比率'与结构 band。
    用户经验：正常 AI↔人 对话，百字里仅 4~5 字有用（≈0.05）；
    一条条集中文档比率很高（≈0.85）。块大小由密度反推：
    目标摘要字数 = 块字符 × 有用率 × 保留率 → 块 = 目标摘要 ÷(有用率×保留率)。
    同时顾及自生上下文（平均句长/列表密度/段落分布），多信号融合，不盲信单一信号、
    不低估也不高估自己。"""
    chars = len(text)
    if chars == 0:
        return 0.40, 'normal'
    lines = [l for l in text.split('\n') if l.strip()]
    n_lines = max(1, len(lines))
    short_lines = sum(1 for l in lines if len(l.strip()) < 20)
    short_ratio = short_lines / n_lines
    bullets = len(re.findall(r'(?m)^\s*[-*•·]', text))
    numbered = len(re.findall(r'(?m)^\s*\d+[.、）]', text))
    clause = len(re.findall(r'(?m)^[a-z]{1,3}[-/]?[一-龥]', text))
    list_density = (bullets + numbered + clause) / max(1, chars / 1000.0)
    sents = split_sentences(text)
    avg_sent = chars / max(1, len(sents))
    paras = [p for p in re.split(r'\n\s*\n', text) if p.strip()]
    avg_para = sum(len(p) for p in paras) / max(1, len(paras))
    # 判定（多信号融合，避免低估/高估）
    if list_density >= 6 and avg_sent < 120:
        return 0.85, 'concentrated'    # 一条条集中文档：压缩不了多少
    if short_ratio >= 0.5 and avg_sent < 45:
        return 0.06, 'chat'            # 对话闲聊：百字四五字有用
    if avg_para >= 600 or (avg_sent > 80 and short_ratio < 0.2):
        return 0.18, 'loose'           # 宽大松散叙述
    if list_density >= 2 and avg_sent < 90:
        return 0.65, 'compact'         # 紧凑混合（偏密）
    return 0.40, 'normal'


def read_text(path):
    # BOM 剥离：文件用 utf-8-sig 去 UTF-8 BOM；stdin 及残留 BOM 再用 lstrip('\ufeff') 兜底，
    # 保证首句不带 \ufeff（否则污染句边界）。
    # 换行分工（务必勿混）：本脚本是「总结用切块」，刻意沿用文本模式的换行翻译（CRLF→LF），
    # 这是切块产物的既定行为；它不负责逐字节还原，切块产物也绝不可拿来做字节级拼接校验。
    # 逐字节一致性校验 / 按行分卷还原由 split_by_lines.py（二进制模式 rb/wb）承担。
    if path == '-':
        return sys.stdin.read().lstrip('\ufeff')
    with open(path, 'r', encoding='utf-8-sig') as f:
        return f.read().lstrip('\ufeff')


def estimate_tokens(text):
    # 粗略估计：CJK 字符≈1 token；连续非 CJK 每 ~4 字符≈1 token 或按词
    cjk = len(re.findall(r'[\u4e00-\u9fff\u3400-\u4dbf]', text))
    non_cjk = len(re.sub(r'[\u4e00-\u9fff\u3400-\u4dbf]', '', text))
    words = len(re.findall(r'[A-Za-z0-9]+', text))
    return cjk + max(non_cjk // 4, words)


def _split_sentences_impl(text, preserve_structure):
    # 返回 [(单元文本, 边界标记)]，边界标记 ∈ {'head','blank',None}：
    #   'head'=该单元起点是 #{1,6} 标题行；'blank'=该单元起点前是空行（段落起点）。
    # 二者供 --prefer-headings 在等宽目标附近吸附切点；普通单元为 None。
    # 按行扫描：\n 本就是旧版 SENT_SPLIT 的切点，逐行 split + strip 与「整文 split + strip」
    # 对普通文本完全等价，故默认结果不变；只在命中 markdown 结构时把连续行合并为不可分单元。
    lines = text.split('\n')
    pairs = []
    i = 0
    n = len(lines)
    prev_blank = False
    while i < n:
        line = lines[i]
        if preserve_structure and FENCE_LINE.match(line):
            # 围栏代码块：起止 ``` 之间（含语言标记行）整块作为一个单元，绝不从中间劈开
            block = [line]
            i += 1
            while i < n:
                block.append(lines[i])
                closed = bool(FENCE_LINE.match(lines[i]))
                i += 1
                if closed:
                    break
            unit = '\n'.join(block).strip()
            if unit:
                pairs.append((unit, None))
            prev_blank = False
            continue
        if preserve_structure and TABLE_LINE.match(line):
            # 连续表格行（含 |---| 分隔行）合并为一个单元
            block = [line]
            i += 1
            while i < n and TABLE_LINE.match(lines[i]):
                block.append(lines[i])
                i += 1
            unit = '\n'.join(block).strip()
            if unit:
                pairs.append((unit, None))
            prev_blank = False
            continue
        if preserve_structure and HEADING_LINE.match(line):
            # 标题行 + 紧随其后的段落首行合并，防止标题被切到下块、正文留在上块
            block = [line]
            i += 1
            k = i
            while k < n and not lines[k].strip():   # 跳过标题与段落之间的空行
                k += 1
            if (k < n and not FENCE_LINE.match(lines[k])
                    and not TABLE_LINE.match(lines[k])
                    and not HEADING_LINE.match(lines[k])):
                block.extend(lines[i:k + 1])
                i = k + 1
            unit = '\n'.join(block).strip()
            if unit:
                pairs.append((unit, 'head'))
            prev_blank = False
            continue
        # 普通行（preserve_structure=False 时所有行都走这里，等同旧行为）
        if not line.strip():
            prev_blank = True
            i += 1
            continue
        parts = SENT_SPLIT.split(line)
        first = True
        for s in parts:
            t = s.strip()
            if not t:
                continue
            if first and HEADING_LINE.match(line):
                flag = 'head'
            elif first and prev_blank:
                flag = 'blank'
            else:
                flag = None
            pairs.append((t, flag))
            first = False
        prev_blank = False
        i += 1
    return pairs


def split_sentences(text, preserve_structure=True, with_meta=False):
    # 整篇拆成句子单元（句末标点/换行处断），原子不可再分，保证不切断行。
    # 默认 preserve_structure=True：markdown 围栏/表格/标题作不可分单元（--no-preserve-structure 可退回旧行为）。
    # with_meta=True 额外返回平行标记列表，供 --prefer-headings 用；默认返回纯字符串列表，完全兼容旧调用。
    pairs = _split_sentences_impl(text, preserve_structure)
    units = [p[0] for p in pairs]
    if with_meta:
        return units, [p[1] for p in pairs]
    return units


def derive_chunk_size(sentences, total_chars, sents_per_chunk, min_chunk=MIN_CHUNK,
                      context_cap=None):
    # 自生专注力：块大小由文档自身的句长分布 + 总规模共同决定，不靠外部预算硬塞。
    # 关键修复：旧公式 块大小=平均句长×句数 会让"短句流"（聊天记录，平均句长十几字）
    # 推出极小的块、切成几百个碎块——方向反了：句子越碎，单句信息越少，
    # 越需要更多句子才能凑成一个完整意思，块应更大而非更小。
    # 新公式：
    #   1) 句长驱动 raw   = 平均句长 × 每块期望句数（保留原意图，越大块越大）
    #   2) 规模保护 floor = 总字符 / MAX_CHUNKS（最多约 MAX_CHUNKS 块，防过碎）
    #   3) 取二者较大，再夹紧到 [min_chunk, MAX_CHUNK]
    # 注：min_chunk 默认 40000，只是「信息密度推导的下限建议」，looseness 放大在 main 里乘。
    # 全局硬红线：任何时候切出来的块都必须严格 < 模型上下文的 1/2（见 MIN_CHUNK 注释）。
    # 传入 context_cap 时块上限 = context_cap//2；min_chunk 不得凌驾该红线，
    # min_chunk >= half_cap 时忽略下限顶回、强制缩小块并打印告警。不传 context_cap 时行为不变。
    n = max(1, len(sentences))
    avg = total_chars / n
    raw = avg * sents_per_chunk
    floor = total_chars / MAX_CHUNKS
    size = max(raw, floor)
    size = int(max(min_chunk, min(MAX_CHUNK, size)))
    if context_cap:
        half_cap = context_cap // 2
        allowed = max(1, half_cap - 1)
        if size >= half_cap:
            size = allowed
            clamp = (f"硬红线：块必须严格 < 1/2 上下文(half_cap={half_cap}) → 钳到 {allowed}")
            if min_chunk >= half_cap:
                print(f"  [告警] 上下文太小：context_cap={context_cap} → half_cap={half_cap}，"
                      f"min_chunk={min_chunk} >= half_cap；已忽略下限顶回，"
                      f"块被强制缩小（块数会变多）。")
            print(f"  [块大小诊断/derive_chunk_size] min_chunk={min_chunk} cap={context_cap} "
                  f"half_cap={half_cap} 最终块大小={size} 钳制原因={clamp}")
    return size


def split_equal_chunks(sentences, chunk_size):
    # 目标等宽：句子依次装入，达到 chunk_size 就吸附到句边界立即截断（不切断行）
    # 单句超 chunk_size：整句单独成块（宁可超，不切断）
    chunks = []
    cur = []
    cur_len = 0
    for s in sentences:
        if len(s) > chunk_size:
            if cur:
                chunks.append('\n'.join(cur))
                cur = []
                cur_len = 0
            chunks.append(s)  # 整句成块，绝不切断
            continue
        cur.append(s)
        cur_len += len(s) + 1
        if cur_len >= chunk_size:
            chunks.append('\n'.join(cur))
            cur = []
            cur_len = 0
    if cur:
        chunks.append('\n'.join(cur))
    return chunks


def split_equal_chunks_prefer_headings(sents, chunk_size, flags):
    # --prefer-headings：切块点优先吸附到最近的 #{1,6} 标题行或空行边界。
    # 在等宽目标（累计到 chunk_size）附近搜索，切点相对目标的偏移不超过 chunk_size 的 15%；
    # 窗口内找不到标题/空行边界，则退回原有句/段边界吸附（与 split_equal_chunks 一致）。
    # 切点始终落在句子单元之间，绝不切断行；单单元超 chunk_size 仍整句成块（宁可超、不切断）。
    win = int(chunk_size * 0.15)
    chunks = []
    n = len(sents)
    i = 0
    while i < n:
        if len(sents[i]) > chunk_size:
            chunks.append(sents[i])
            i += 1
            continue
        acc = 0
        j = i
        best_cut = None      # (acc, j)：窗口内离目标最近的优先边界（切在 j 之前）
        fallback = None      # 原逻辑切点：累计首次 >= chunk_size 时切在 j+1 之后
        while j < n:
            # 与 split_equal_chunks 一致：块中间遇到单超单元也整块保留（下轮单独成块），先在此 flush 当前块
            if len(sents[j]) > chunk_size:
                if fallback is None:
                    fallback = j
                break
            # sents[j] 起点累计位置 = acc；若它是标题/空行边界且落在窗口内，记为候选切点
            if flags[j] in ('head', 'blank') and (chunk_size - win) <= acc <= (chunk_size + win):
                if best_cut is None or abs(acc - chunk_size) < abs(best_cut[0] - chunk_size):
                    best_cut = (acc, j)
            acc += len(sents[j]) + 1
            if fallback is None and acc >= chunk_size:
                fallback = j + 1
            if acc >= chunk_size + win:
                break
            j += 1
        if best_cut is not None:
            end = best_cut[1]
        elif fallback is not None:
            end = fallback
        else:
            end = n
        if end <= i:            # 至少推进一个单元，避免死循环
            end = i + 1
        chunks.append('\n'.join(sents[i:end]))
        i = end
    return chunks


def inject_overlap(base_chunks, overlap_frac, overlap_chars_abs=None, mark=True):
    out = []
    for i, chunk in enumerate(base_chunks):
        if i == 0 or (overlap_frac <= 0 and not overlap_chars_abs):
            out.append({'index': i, 'text': chunk, 'char_count': len(chunk),
                        'overlap_char_count': 0, 'overlap_source': None})
            continue
        target = overlap_chars_abs if overlap_chars_abs else int(len(chunk) * overlap_frac)
        prev = base_chunks[i - 1]
        prev_units = split_sentences(prev)
        picked = []
        acc = 0
        for u in reversed(prev_units):
            if acc + len(u) > target and picked:
                break
            picked.insert(0, u)
            acc += len(u)
        overlap_text = ''.join(picked)
        if overlap_text and mark:
            merged = OVERLAP_HEAD.format(src=i - 1) + overlap_text + OVERLAP_TAIL + chunk
        else:
            merged = (overlap_text + '\n' + chunk) if overlap_text else chunk
        out.append({'index': i, 'text': merged, 'char_count': len(merged),
                    'overlap_char_count': len(overlap_text), 'overlap_source': i - 1})
    return out


def analyze(text, sents_per_chunk=12):
    chars = len(text)
    toks = estimate_tokens(text)
    sents = split_sentences(text)
    ratio, band = estimate_density(text)
    floor = 10000 if band == 'concentrated' else MIN_CHUNK
    ideal = TARGET_SUMMARY / (ratio * RETENTION)
    size = int(max(ideal, floor))
    n = max(1, -(-chars // size))  # ceil
    avg = chars / max(1, len(sents))
    print("=== 体量分析（自生专注力 + 信息密度）===")
    print(f"字符数: {chars}")
    print(f"估算 token: {toks} (粗略)")
    print(f"句子数: {len(sents)} | 平均句长: {avg:.0f} 字符")
    print(f"信息密度 band={band} | 有用率≈{ratio:.2f}（对话≈0.06 / 松散≈0.18 / 正常≈0.40 / 集中一条条≈0.85）")
    print(f"→ 密度算法块大小: ~{size} 字符（目标摘要 {TARGET_SUMMARY} 字 ÷ 有用率÷保留率；下限 {floor}）")
    print(f"预计块数: {n}（目标等宽，按句边界吸附；末块可能因余数略短）")
    print(f"默认 1-4kb 重叠（按信息密度升降档）：文档 {chars} 字符 → {auto_overlap_chars(chars)} 字符")
    return size


def main():
    ap = argparse.ArgumentParser(description="doc-extract-filter 切分脚本")
    ap.add_argument('file', nargs='?', default='-', help="输入文件，'-' 表示 stdin")
    ap.add_argument('--analyze', action='store_true', help="只做体量分析，不切分")
    ap.add_argument('--sents-per-chunk', type=int, default=12,
                    help="每块期望句数（自生专注力参数，越大块越大；短句文档另有规模保护防过碎）")
    ap.add_argument('--chunk-size', type=int, default=None,
                    help="手动指定块大小，覆盖自生专注力推导（仍受 --context-cap 与 MAX_CHUNK 约束）")
    ap.add_argument('--min-chunk', type=int, default=40000,
                    help="块大小下限建议(字符)，默认 40000（≈40kb）；不得凌驾「块 < 1/2 上下文」硬红线，half_cap 更小时被忽略")
    ap.add_argument('--looseness', type=float, default=1.0,
                    help="结构松散时块大小放大系数 1.2~10，仅自动推导时生效；紧结构用 1.0")
    ap.add_argument('--context-cap', type=int, default=None,
                    help="上下文容量【减去系统提示词后的可用字符/token 数】；块大小上限 = 其 1/2（严格小于），任何时候切出的块都必须 < 1/2 上下文")
    ap.add_argument('--overlap', type=float, default=0.0, help="手动按比例重叠(相对每块，0~0.5)；默认不按比例，改由 1-4kb 绝对值自动档位决定（见 --overlap-auto / --overlap-chars）")
    ap.add_argument('--overlap-chars', type=int, default=None, help="固定重叠字符数(覆盖 --overlap)")
    ap.add_argument('--overlap-auto', action='store_true',
                    help="重叠量默认取 1-4kb 绝对值（1024/2048/4096），按文档体量与信息密度升降档；不给任何重叠参数时也默认启用")
    ap.add_argument('--out', default=None, help="输出目录")
    ap.add_argument('--no-mark', action='store_true', help="不打 ⟦OVERLAP⟧ 标记")
    ap.add_argument('--no-preserve-structure', action='store_true',
                    help="关闭 markdown 结构保护：围栏代码块/连续表格行/标题行不再作不可分单元（默认开启保护，整块不切断，绝不切断行）")
    ap.add_argument('--prefer-headings', action='store_true',
                    help="切块点优先吸附到最近的 # 标题行或空行边界（在等宽目标附近搜索，偏移不超过目标块大小 15%%；找不到退回句/段边界）。默认关闭")
    args = ap.parse_args()

    text = read_text(args.file)
    if args.analyze:
        analyze(text, args.sents_per_chunk)
        return

    _explicit_overlap = any(x == '--overlap' or x.startswith('--overlap=') or
                            x == '--overlap-chars' or x.startswith('--overlap-chars=')
                            for x in sys.argv[1:])
    if args.overlap_auto or (not _explicit_overlap and not args.overlap_chars):
        # 现行口径「默认 1-4kb」真正落地；仅当用户显式给比例/固定字符数时按手动覆盖
        args.overlap_chars = auto_overlap_chars(len(text))
        print(f"[overlap-auto] 默认 1-4kb（按信息密度升降档）：文档 {len(text)} 字符 → 重叠量取 {args.overlap_chars} 字符")

    # ---- 信息密度算法定块大小（默认；--chunk-size 可强制覆盖）----
    # 全局硬红线：任何时候切出来的块都必须严格 < 模型上下文的 1/2；
    # --context-cap 是上下文容量，块上限 = 其 1/2（half_cap），floor 只是下限建议、不得凌驾。
    ratio, band = estimate_density(text)
    floor = 10000 if band == 'concentrated' else args.min_chunk
    cap = args.context_cap
    half_cap = (cap // 2) if cap else None
    ideal = None
    clamp_reasons = []
    if args.chunk_size:
        size = float(args.chunk_size)
        ideal = size
        plan = f"手动 --chunk-size {size:.0f}"
    else:
        ideal = TARGET_SUMMARY / (ratio * RETENTION)   # 块 = 目标摘要 ÷(有用率×保留率)
        size = ideal
        if args.looseness:
            size = ideal * args.looseness              # 用户手动松散系数(紧凑0.1~1 / 松散1.2~10)
        size = max(size, floor)                        # 下限：集中文档1万，其余4万（仅建议）
        plan = (f"信息密度 band={band} 有用率≈{ratio:.2f} → 理想块 {ideal:.0f} 字符 "
                f"× 松散系数{args.looseness or 1.0} → 落地 {size:.0f}，下限 {floor}（仅建议）")

    if size > MAX_CHUNK:
        clamp_reasons.append(f"MAX_CHUNK={MAX_CHUNK} 钳制")
        size = float(MAX_CHUNK)

    if half_cap is not None:
        allowed = max(1, half_cap - 1)   # 严格小于 half_cap
        if floor >= half_cap:
            print(f"  [告警] 上下文太小：--context-cap={cap} → half_cap={half_cap}，"
                  f"下限 floor={floor} >= half_cap；已忽略下限顶回，块强制缩小（块数会变多）。")
        if size < floor and floor < half_cap:
            size = float(floor)
        if size >= half_cap:
            clamp_reasons.append(
                f"硬红线：块={size:.0f} 必须严格 < 1/2 上下文(half_cap={half_cap}) → 钳到 {allowed}")
            size = float(allowed)
        plan += f" | 块上限=1/2 上下文(half_cap {half_cap}，最终块必须 < {half_cap})"
    else:
        size = max(float(size), float(floor))

    size = int(size)
    clamp = "；".join(clamp_reasons) if clamp_reasons else "无（未触发钳制）"
    print(f"  [块大小诊断] ideal={ideal} floor={floor} cap={cap} half_cap={half_cap} "
          f"最终块大小={size} 钳制原因={clamp}")
    print(f"[块大小] {plan} → 最终 {size} 字符")
    preserve = not args.no_preserve_structure
    if args.prefer_headings:
        sents, flags = split_sentences(text, preserve_structure=preserve, with_meta=True)
        base = split_equal_chunks_prefer_headings(sents, size, flags)
    else:
        sents = split_sentences(text, preserve_structure=preserve)
        base = split_equal_chunks(sents, size)
    # 结构保护下若单个不可分单元（围栏/表格/标题）超过上限，按「宁可超、不切断行」整块保留，并提示
    if preserve:
        big = [s for s in sents if len(s) > size and '\n' in s]
        if big:
            print(f"[结构保护] 有 {len(big)} 个 markdown 不可分单元超过目标块大小 {size} 字符"
                  f"（含 --context-cap 约束）；按『宁可超、不切断行』原则整块保留为单独块，未从中间劈开。")
    chunks = inject_overlap(base, args.overlap, args.overlap_chars, mark=not args.no_mark)

    manifest = {
        'total_chunks': len(chunks),
        'chunk_size_target': size,
        'overlap_frac': args.overlap,
        'overlap_chars_abs': args.overlap_chars,
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
        print(f"已写出 {len(chunks)} 块（目标等宽 {size} 字符）到 {args.out}/")
    else:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        for c in chunks:
            src = c['overlap_source']
            print(f"\n----- 块 {c['index'] + 1}/{len(chunks)} 字符{c['char_count']} "
                  f"重叠{c['overlap_char_count']} from #{src} -----")
            print(c['text'][:400] + ('...' if len(c['text']) > 400 else ''))


if __name__ == '__main__':
    main()
