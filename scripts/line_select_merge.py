#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) 2026 text-summary-filter contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""
text-summary-filter · 程序化行选择合并（零改写 / 零硬数据丢失）。

实现来源：早期合并脚本（实测硬数据零丢失）。
核心：每一行要么「原样保留」，要么「整行丢弃」，绝不改写任何字符。
      输出 = 被保留物理行按原序逐字节拼接 —— 绕开语义漂移与 Write token 上限。

分区策略：
  AUTH（一律全保）：### 段、顶层 # 标题、所有 【…】 开头标题行、以及「零散内容块」
                    （由 --scatter-marker 指定标记所在行起至文末；未指定则不做该分区）。
  CAND（条件保留）：**N. 章节叙述 / 【合并块】/ summary 块，仅当该行携带
                    「AUTH 区 + 零散块中都缺失的硬数据 token」才保留。
  硬数据 token 三类：① 设计点编号 \\b[a-z]{2}-\\d{3}\\b
                    ② 数值 + 单位
                    ③ 冲突标记 【冲突[：:][^】]*】
  另可加 --keep-rule 关键词（窄规则，默认内置一组通用中文规则词）。

  4-gram 独有性：零散内容块的行若含 AUTH 区没有的中文 4-gram，也保留（防丢独有概念）。
  Pass2 救回：Pass1 被删的行，若其所有中文 3-gram 都未出现在当前输出中
              （零重叠，且中文字符 >= --min-cjk），强制救回（如「天然五行反应场」）。
  纯重述行（所有 4-gram 都在 AUTH 区、无独有硬 token、无窄规则词）→ 丢弃。

状态：全脚本统一只用 DEL 一个删除状态，杜绝「外层判 DEL、内层只认
      DEL_REDUNDANT 导致 while 死循环 SIGTERM」事故（troubleshooting B7）。

命令行：见 --help（中文）。所有文件读写一律二进制模式 rb / wb，
      splitlines(keepends=True) 保留 CRLF 行尾，禁止依赖 Windows 文本模式换行转换。
"""
import argparse
import json
import os
import re
import sys


# ===================== 内置常量表（均为通用结构，不含任何特定文档专属内容 / 具体行号）=====================

# 单位词表：与 fingerprint.py / merge_v9.py 完全一致，保证决策口径与校验口径对齐
UNITS = ['KB', 'MB', 'GB', 'kg', 'g', 'cm', 'm', 'km', '格', '区块', '天', '秒', '分钟', '小时',
         '年', '%', '倍', '层', '级', '个', '条', '点', '元', 'W', 'T', 'Hz', 'KB/s', 'MB/s']
UNIT_ALT = '|'.join(UNITS)

# 决策用数值正则：宽口径（v9 口径，额外收全角百分号 / 间隔号，防独有 token 漏判）
BROAD_EXTRA = '个条点元格区块天秒分钟小时年倍层级%·％％'
ID_PAT = re.compile(r'\b[a-z]{2}-\d{3}\b')
CONF_PAT = re.compile(r'【冲突[：:][^】]*】')
VAL_PAT = re.compile(r'\d+(?:\.\d+)?\s*(?:' + UNIT_ALT + r')')                    # 校验用（fingerprint.py 口径）
BROAD_VAL = re.compile(r'\d+(?:\.\d+)?\s*(?:' + UNIT_ALT + r'|[' + BROAD_EXTRA + r'])')  # 决策用（v9 口径）

# 分区识别：通用结构标记（不是内容）
RE_H3 = re.compile(r'^###')            # ### 段
RE_HEADING = re.compile(r'^#{1,3} ')   # 顶层 # 标题（含 ### 带空格）
RE_NUMHDR = re.compile(r'^\*\*[0-9]+\.')  # **N. 章节标题
RE_BRACKET = re.compile(r'^【')         # 【…】标题 / 合并块

# 内置窄规则词（通用中文规则语气词；可用 --keep-rule 追加，--no-builtin-rules 关闭）
BUILTIN_KEEP_RULES = (
    '必须', '禁止', '不得', '严禁', '强制', '绝不', '硬性',
    '不可修改', '不允许', '只允许', '一律', '唯一约束',
)

# 状态常量：删除状态有且仅有 DEL 一个
KEEP_AUTH = 'KEEP_AUTH'
KEEP_UNIQUE = 'KEEP_UNIQUE'
KEEP_UNIQ4 = 'KEEP_UNIQ4'
KEEP_FORCE = 'KEEP_FORCE'
KEEP_RULE = 'KEEP_RULE'
KEEP_HDR = 'KEEP_HDR'
DEL = 'DEL'

STATUS_LABEL = {
    KEEP_AUTH: '权威段原样保留',
    KEEP_HDR: '章节标题',
    KEEP_UNIQUE: '独有硬数据',
    KEEP_RULE: '窄规则命中',
    KEEP_UNIQ4: '独有概念(4-gram)',
    KEEP_FORCE: 'Pass2 强制救回',
    DEL: '删除',
}

# 全角→半角规整表（与 fingerprint.py 一致：全角 ％→%、全角数字→半角）
_FULL2HALF = {0xFF05: ord('%')}
for _i in range(10):
    _FULL2HALF[0xFF10 + _i] = ord('0') + _i


def normalize(text):
    return text.translate(_FULL2HALF)


class MergeError(Exception):
    """可预期的用户输入 / 环境错误：给明确中文提示，不裸 traceback。"""


# ===================== 基础工具 =====================

def read_bytes(path, label):
    if not os.path.isfile(path):
        raise MergeError(f"找不到{label}：{path}\n请检查路径是否存在（Windows 建议用正斜杠 / ）。")
    try:
        with open(path, 'rb') as f:
            return f.read()
    except OSError as e:
        raise MergeError(f"读取{label}失败：{path}\n{e}")


def write_bytes(path, data, label):
    parent = os.path.dirname(os.path.abspath(path))
    try:
        os.makedirs(parent, exist_ok=True)
        with open(path, 'wb') as f:
            f.write(data)
    except OSError as e:
        raise MergeError(f"写出{label}失败：{path}\n{e}")
    # 回读逐字节校验（硬门槛）
    if read_bytes(path, label) != data:
        raise MergeError(f"写出{label}后回读不一致：{path}")


def decode_utf8(raw, label):
    try:
        return raw.decode('utf-8')   # BOM 会保留为 \ufeff 字符，UTF-8 双射，不影响字节还原
    except UnicodeDecodeError as e:
        raise MergeError(f"{label}不是合法 UTF-8：{e}")


def toks(s):
    """硬数据 token 集合（决策口径：编号 / 冲突 / 数值单位·宽）。"""
    return set(ID_PAT.findall(s)) | set(CONF_PAT.findall(s)) | set(BROAD_VAL.findall(s))


def cjk(s):
    return re.sub(r'[^\u4e00-\u9fff]', '', s)


def cjk_n(s, n):
    c = cjk(s)
    return set(c[j:j + n] for j in range(len(c) - n + 1))


def cjk4(s):
    return cjk_n(s, 4)


def cjk3(s):
    return cjk_n(s, 3)


def extract_fingerprint(text):
    """与 fingerprint.py extract() 一致（不含除噪）：nums/conf 去重排序，vals 保留重复。"""
    nt = normalize(text)
    return {
        'nums': sorted(set(ID_PAT.findall(nt))),
        'vals': VAL_PAT.findall(nt),
        'conf': sorted(set(CONF_PAT.findall(nt))),
    }


def load_baseline(path):
    raw = read_bytes(path, '指纹基线 JSON')
    txt = decode_utf8(raw, '指纹基线 JSON')
    try:
        data = json.loads(txt)
    except json.JSONDecodeError as e:
        raise MergeError(f"指纹基线 JSON 格式错误：{path}\n{e}")
    if not isinstance(data, dict) or not all(k in data for k in ('nums', 'vals', 'conf')):
        raise MergeError(f"指纹基线缺少 nums / vals / conf 字段：{path}")
    for k in ('nums', 'vals', 'conf'):
        if not isinstance(data[k], list):
            raise MergeError(f"指纹基线字段 {k} 须为数组：{path}")
    return data


def load_rules(path):
    raw = read_bytes(path, '规则关键词文件')
    txt = decode_utf8(raw, '规则关键词文件')
    return [ln.strip() for ln in txt.splitlines()
            if ln.strip() and not ln.strip().startswith('#')]


def as_kinds(values):
    """集合层口径：规整后取种类集合（忽略出现次数下降）。"""
    return set(normalize(str(v)) for v in values)


# ===================== 核心流程 =====================

def compare_fingerprint(baseline, out_text):
    """集合层零丢失校验：set(基线种类) - set(输出种类)，只报种类差集。
    返回 (all_ok, lines, per_class)。带「字面复核」兜底：差集项若仍作为字面子串
    出现在输出文本中（多为不同抽取器口径差异），不计为真实缺失。"""
    nt = normalize(out_text)
    out_fp = extract_fingerprint(out_text)
    labels = (('nums', '设计点编号'), ('vals', '数值+单位(独种)'), ('conf', '冲突标记'))
    lines = ['=== 集合层零丢失校验（只比种类覆盖，明确忽略出现次数下降）===']
    per_class = {}
    all_ok = True
    for key, label in labels:
        b = as_kinds(baseline.get(key, []))
        o = as_kinds(out_fp.get(key, []))
        miss = b - o                                   # 严格种类差集
        real = sorted(x for x in miss if x not in nt)  # 字面复核后仍缺失
        ok = not real
        all_ok = all_ok and ok
        per_class[key] = {'baseline': len(b), 'output': len(o),
                          'strict_miss': len(miss), 'real_miss': len(real), 'real': real}
        lines.append(f"  [{label}] 基线 {len(b)} 种 | 输出 {len(o)} 种 | "
                     f"严格差集 {len(miss)} 种 | 真实缺失 {len(real)} 种  {'PASS' if ok else 'FAIL'}")
        if real:
            lines.append(f"      缺失项：{real[:12]}{' ...' if len(real) > 12 else ''}")
        elif miss:
            lines.append(f"      注：{len(miss)} 项经字面复核仍在输出中（抽取口径差异），不计缺失")
    lines.append('=== 总判定：' + ('PASS（硬数据种类零丢失）' if all_ok else 'FAIL（存在种类缺失）') + ' ===')
    return all_ok, lines, per_class


def build_map_doc(args, mp, src_bytes, src_lines, out_bytes, out_lines,
                  deleted, scatter_start, scatter_enabled, zero_loss):
    """逐行去向映射文档：连续删除合并为区间并注明原因；删除行绝不出现 输出LNone。"""
    tag = {KEEP_AUTH: '权威段原样', KEEP_HDR: '章节标题', KEEP_UNIQUE: '独有硬数据',
           KEEP_RULE: '窄规则命中', KEEP_UNIQ4: '独有概念(4-gram)',
           KEEP_FORCE: 'Pass2 强制救回', DEL: '删除'}

    md = []
    md.append('# 行选择合并 · 逐行去向映射')
    md.append('')
    md.append(f'- 源文档：`{args.source}`（{src_bytes} 字节 / {src_lines} 行）')
    md.append(f'- 合并文档：`{args.out}`（{out_bytes} 字节 / {out_lines} 行）')
    if scatter_enabled:
        md.append(f'- 零散内容块起点：L{scatter_start}（标记：`{args.scatter_marker}`）')
    else:
        md.append('- 零散内容块：未分区（未指定 --scatter-marker）')
    md.append('- 状态：KEEP_AUTH 权威段 / KEEP_HDR 章节标题 / KEEP_UNIQUE 独有硬数据 / '
              'KEEP_RULE 窄规则 / KEEP_UNIQ4 独有概念 / KEEP_FORCE Pass2 救回 / DEL 删除')
    md.append('- 映射格式：输入 L → 输出 L；连续删除合并为区间并注明原因；删除行不产生输出行号')
    md.append('')
    md.append('## 逐输入行映射')
    md.append('')

    i, n = 0, len(mp)
    while i < n:
        src, status, out_ln = mp[i]
        if status == DEL:
            # 统一只认 DEL 一个删除状态：j 必定前进，不会死循环（troubleshooting B7）
            j = i
            while j < n and mp[j][1] == DEL:
                j += 1
            if j - i == 1:
                md.append(f'- L{src} → 删除（冗余重述，无独有硬数据 / 窄规则词，已并入权威段）')
            else:
                md.append(f'- L{src}~L{mp[j - 1][0]} → 删除（{j - i} 行冗余重述，无独有硬数据 / 窄规则词）')
            i = j
        else:
            md.append(f'- L{src} → 输出L{out_ln}（{tag.get(status, status)}）')
            i += 1

    md.append('')
    md.append('## 统计')
    md.append('')
    verdict = '硬数据 0 丢失' if zero_loss else '硬数据存在缺失（见 --check 校验输出）'
    md.append(f'- 输入 {src_lines} 行 / 输出 {out_lines} 行 / 删除 {deleted} 行 / {verdict}')
    md.append('')

    text = '\n'.join(md) + '\n'
    assert '输出LNone' not in text, '行映射文档残留 输出LNone'
    return text


def run(args):
    # ---------- 1. 读源（二进制）→ 字符行 / 字节行双视图 ----------
    raw = read_bytes(args.source, '源文档')
    text = decode_utf8(raw, '源文档')
    slines = text.splitlines(keepends=True)      # str：正则判断用
    blines = raw.splitlines(keepends=True)       # bytes：实际写出用（含 CRLF / BOM 原样）
    if raw.startswith(b'\xef\xbb\xbf'):
        print('[提示] 源文档含 UTF-8 BOM，已按二进制原样保留（不剥离）。')
    if len(slines) != len(blines):
        raise MergeError('源文档的字符行与字节行切分数量不一致（可能含 Unicode 行分隔符），'
                         '无法保证逐字节还原；请改用标准 \\n / \\r\\n 行尾。')
    # UTF-8 双射：每个 str 行 encode 回去应与对应 bytes 行完全相同（含 BOM 在首行）
    if not all(s.encode('utf-8') == b for s, b in zip(slines, blines)):
        raise MergeError('源文档字符行与字节行逐行不一致，无法保证零改写。')
    nlines = len(slines)
    if nlines == 0:
        raise MergeError(f"源文档为空（0 行）：{args.source}")
    print(f"=== 程序化行选择合并 ===")
    print(f"  源：{args.source}（{len(raw)} 字节 / {nlines} 行）")

    # ---------- 2. AUTH / CAND 分区（粘性 zone，逐行推进）----------
    zone = 'AUTH'
    is_cand = [False] * (nlines + 1)
    for i, l in enumerate(slines, 1):
        s = l.strip().lstrip('\ufeff')
        if RE_H3.match(s):
            zone = 'AUTH'
        elif RE_HEADING.match(s):
            zone = 'AUTH'
        elif RE_NUMHDR.match(s):
            zone = 'CAND'
        elif RE_BRACKET.match(s):
            zone = 'CAND'
        is_cand[i] = (zone == 'CAND')

    # ---------- 3. 零散内容块起点 ----------
    # 用户可能漏传 --scatter-marker，导致整片独有「零散内容」被当 CAND 误删。
    # 因此未指定时自动探测「同源/仅标此一次/零散内容」类结构标记行；
    # 可用 --no-auto-scatter 显式关闭自动探测。
    scatter_start = nlines + 1
    scatter_enabled = False
    AUTO_SCATTER_HINTS = ("本块全条同源", "仅标此一次", "零散内容", "本块同源")
    if args.scatter_marker:
        # 优先：含标记且为标题结构行（【…】/ #…）；退而：任意含标记的首行
        for i, l in enumerate(slines, 1):
            st = l.strip().lstrip('\ufeff')
            if args.scatter_marker in l and (st.startswith('【') or st.startswith('#')):
                scatter_start = i
                break
        else:
            for i, l in enumerate(slines, 1):
                if args.scatter_marker in l:
                    scatter_start = i
                    break
        if scatter_start <= nlines:
            scatter_enabled = True
            preview = slines[scatter_start - 1].strip()[:60]
            print(f"  零散起点：L{scatter_start}（指定标记 `{args.scatter_marker}`；命中行 `{preview}`）")
        else:
            print(f"[警告] 未找到 --scatter-marker 指定的标记文字 `{args.scatter_marker}`，"
                  f"改走自动探测。")
    if not scatter_enabled and not getattr(args, 'no_auto_scatter', False):
        for i, l in enumerate(slines, 1):
            st = l.strip().lstrip('\ufeff')
            if (st.startswith('【') or st.startswith('#')) and any(h in l for h in AUTO_SCATTER_HINTS):
                scatter_start = i
                scatter_enabled = True
                print(f"  零散起点：L{scatter_start}（**自动探测**到同源标记；命中行 `{st[:60]}`）")
                break
    if not scatter_enabled:
        print(f"  零散起点：未分区（scatter_start={scatter_start}）。"
              f"若源文档存在成片独有内容，请用 --scatter-marker 指定起始标记，否则可能被误删。")

    # ---------- 4. AUTH 区 + 零散块的 token / 4-gram 基线 ----------
    auth_tokens = set()
    for i, l in enumerate(slines, 1):
        if (not is_cand[i]) or (i >= scatter_start):
            auth_tokens |= toks(l)
    auth_4 = cjk4(''.join(l for i, l in enumerate(slines, 1)
                          if (not is_cand[i]) and not l.strip().lstrip('\ufeff').startswith('【')))

    # ---------- 5. 窄规则词 ----------
    rules = []
    if not args.no_builtin_rules:
        rules.extend(BUILTIN_KEEP_RULES)
    if args.keep_rule:
        loaded = load_rules(args.keep_rule)
        rules.extend(loaded)
        print(f"  窄规则词：内置 {0 if args.no_builtin_rules else len(BUILTIN_KEEP_RULES)} 个"
              f" + 文件 {len(loaded)} 个 = 合计 {len(rules)} 个")
    else:
        print(f"  窄规则词：内置 {0 if args.no_builtin_rules else len(BUILTIN_KEEP_RULES)} 个"
              f"（未指定 --keep-rule）")
    rules = tuple(dict.fromkeys(rules))  # 去重保序

    def rule_hit(s):
        return any(w in s for w in rules)

    # ---------- 6. Pass1 / Pass2 ----------
    def build(force):
        out, mp, ol, k, d = [], [], 0, 0, 0
        for i, l in enumerate(slines, 1):
            s = l.strip().lstrip('\ufeff')
            if (not is_cand[i]) or s.startswith('【') or (i in force):
                tag = KEEP_AUTH if (not is_cand[i] or s.startswith('【')) else KEEP_FORCE
                ol += 1
                out.append(l)
                mp.append((i, tag, ol))
                k += 1
            elif i >= scatter_start:
                # 零散内容块：独有硬 token → 独有 4-gram → 窄规则 → 否则删
                if toks(s) - auth_tokens:
                    ol += 1
                    out.append(l)
                    mp.append((i, KEEP_UNIQUE, ol))
                    k += 1
                elif cjk4(s) - auth_4:
                    ol += 1
                    out.append(l)
                    mp.append((i, KEEP_UNIQ4, ol))
                    k += 1
                elif rule_hit(s):
                    ol += 1
                    out.append(l)
                    mp.append((i, KEEP_RULE, ol))
                    k += 1
                else:
                    mp.append((i, DEL, None))
                    d += 1
            else:
                # CAND 区（scatter 之前）：**N. 标题 → 独有硬 token → 窄规则 → 否则删
                if RE_NUMHDR.match(s):
                    ol += 1
                    out.append(l)
                    mp.append((i, KEEP_HDR, ol))
                    k += 1
                elif toks(s) - auth_tokens:
                    ol += 1
                    out.append(l)
                    mp.append((i, KEEP_UNIQUE, ol))
                    k += 1
                elif rule_hit(s):
                    ol += 1
                    out.append(l)
                    mp.append((i, KEEP_RULE, ol))
                    k += 1
                else:
                    mp.append((i, DEL, None))
                    d += 1
        return out, mp, k, d

    out1, mp1, k1, d1 = build(set())
    out1_text = ''.join(out1)
    out1_3 = cjk3(out1_text)
    force = set()
    for i, l in enumerate(slines, 1):
        if mp1[i - 1][1] == DEL:
            c = cjk(l)
            if len(c) >= args.min_cjk and not (cjk3(l) & out1_3):
                force.add(i)
    out2, mp2, k2, d2 = build(force)
    out_text = ''.join(out2)

    pass1_bytes = len(out1_text.encode('utf-8'))
    print(f"  Pass1：{pass1_bytes} 字节（删 {d1} 行） | Pass2 强制救回 {len(force)} 行")

    # ---------- 7. mapping 自检（覆盖 / 行号 / 状态一致性）----------
    if len(mp2) != nlines or [e[0] for e in mp2] != list(range(1, nlines + 1)):
        raise MergeError('mapping 未覆盖 L1–L末（存在遗漏 / 重复 / 乱序行号）。')
    kept = [e for e in mp2 if e[1] != DEL]
    deleted = [e for e in mp2 if e[1] == DEL]
    if any(e[2] is None for e in kept):
        raise MergeError('存在 KEEP 行 out_ln 为 None（行号异常）。')
    if any(e[2] is not None for e in deleted):
        raise MergeError('存在 DEL 行 out_ln 非 None（行号异常）。')
    if any(e[2] != idx for idx, e in enumerate(kept, 1)):
        raise MergeError('输出行号不连续（行号异常）。')
    bad_status = sorted({e[1] for e in mp2} - set(STATUS_LABEL))
    if bad_status:
        raise MergeError(f'mapping 出现未知状态：{bad_status}')
    if len(kept) != len(out2) or len(kept) != k2:
        raise MergeError('mapping 保留行数与输出行数不一致。')

    # ---------- 8. 输出 = 保留物理行原样逐字节拼接 ----------
    out_bytes = b''.join(blines[src - 1] for src, _st, _ol in kept)
    assert out_bytes == b''.join(l.encode('utf-8') for l in out2), '字节视图与字符视图输出不一致'
    assert out_bytes == b''.join(blines[src - 1] for src, _st, _ol in kept), '输出非保留行原序拼接'
    write_bytes(args.out, out_bytes, '合并文档')

    out_lines = len(out2)
    ratio = (len(out_bytes) / len(raw) * 100.0) if raw else 0.0
    print(f"  输出：{len(out_bytes)} 字节 / {out_lines} 行 | 保留 {k2} / 删除 {d2} | 压缩比 {ratio:.1f}%")

    # ---------- 9. 内置零丢失断言（源 vs 输出，v9 决策口径）----------
    src_ids, o_ids = set(ID_PAT.findall(text)), set(ID_PAT.findall(out_text))
    src_cf, o_cf = set(CONF_PAT.findall(text)), set(CONF_PAT.findall(out_text))
    src_vl = set(VAL_PAT.findall(text)) | set(BROAD_VAL.findall(text))
    o_vl = set(VAL_PAT.findall(out_text)) | set(BROAD_VAL.findall(out_text))
    inner_miss = (src_ids - o_ids) | (src_cf - o_cf) | (src_vl - o_vl)
    if inner_miss:
        raise MergeError('内置零丢失断言失败，源文本硬数据未在输出中覆盖：'
                         f'{sorted(inner_miss)[:10]}')
    print(f"  内置零丢失断言（源 {len(src_ids)} 编号 / {len(src_vl)} 数值 / {len(src_cf)} 冲突 → 输出）：PASS")

    # ---------- 10. --fingerprint + --check：集合层零丢失校验 ----------
    check_ok = None
    check_lines = []
    check_detail = None
    if args.check:
        if args.fingerprint:
            baseline = load_baseline(args.fingerprint)
            print(f"  [基线] 读取现成基线：{args.fingerprint}")
        else:
            baseline = extract_fingerprint(text)
            print('  [基线] 未指定 --fingerprint，已从 --source 现场抽取（口径同 fingerprint.py）')
        check_ok, check_lines, check_detail = compare_fingerprint(baseline, out_text)
        for ln in check_lines:
            print('  ' + ln)

    zero_loss = (inner_miss == set()) and (check_ok is not False)
    status_counts = {}
    for _src, st, _ol in mp2:
        status_counts[st] = status_counts.get(st, 0) + 1

    # ---------- 11. 写 --state（单一事实源；删除状态统一为 DEL）----------
    state = {
        'mapping': [[src, st, ol] for src, st, ol in mp2],
        'scatter_start': scatter_start,
        'stats': {
            'source': args.source,
            'source_bytes': len(raw),
            'source_lines': nlines,
            'out_bytes': len(out_bytes),
            'out_lines': out_lines,
            'kept': k2,
            'deleted': d2,
            'pass1_bytes': pass1_bytes,
            'pass1_deleted': d1,
            'pass2_rescued': len(force),
            'scatter_enabled': scatter_enabled,
            'min_cjk': args.min_cjk,
            'builtin_rules_used': not args.no_builtin_rules,
            'rule_words': len(rules),
            'compression_ratio': round(len(out_bytes) / len(raw), 6) if raw else 0.0,
            'status_counts': status_counts,
            'builtin_zero_loss': True,
            'fingerprint_check': check_detail,
            'hard_data_zero_loss': bool(zero_loss),
        },
    }
    write_bytes(args.state,
                json.dumps(state, ensure_ascii=False, indent=1).encode('utf-8'),
                '状态文件 _merge_state.json')

    # ---------- 12. 写 --map（输出文档之外的逐行去向映射）----------
    if args.map:
        map_text = build_map_doc(args, mp2, len(raw), nlines, len(out_bytes),
                                 out_lines, d2, scatter_start, scatter_enabled, zero_loss)
        write_bytes(args.map, map_text.encode('utf-8'), '行映射文档')
        print(f"  行映射：{args.map}（{len(map_text.splitlines())} 行）")

    # ---------- 13. 摘要 / 退出码 ----------
    print(f"  状态文件：{args.state}（mapping {len(mp2)} 条，状态 {sorted(status_counts)}）")
    print(f"  已写出：{args.out}")
    if args.check:
        print('=== 硬数据校验结论：' + ('零丢失' if check_ok else '存在种类缺失') + ' ===')
        if not check_ok:
            print('[结果] 存在硬数据种类缺失，退出码 1。')
            return 1
    print('=== 完成（硬数据零丢失）===')
    return 0


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8')
        except (AttributeError, ValueError) as e:
            # 不静默吞异常：流不支持重配置时留痕（多为被重定向/旧环境，不影响主流程）
            print("[警告] 标准流编码重配置失败，沿用默认编码：%s" % e,
                  file=sys.stderr)

    ap = argparse.ArgumentParser(
        description='text-summary-filter · 程序化行选择合并：整行原样保留 / 整行丢弃，零改写、零硬数据丢失',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            '示例：\n'
            '  python line_select_merge.py --source 合并稿.txt --out 合并结果.txt \\\n'
            '      --state _merge_state.json --map 行映射.md \\\n'
            '      --scatter-marker "零散内容" --fingerprint 硬数据指纹_基线.json --check\n'
            '\n'
            '说明：\n'
            '  · 每行要么原样保留、要么整行丢弃，绝不改写任何字符；输出 = 保留行按原序逐字节拼接。\n'
            '  · --state 的 mapping 为 [[输入行号, 状态, 输出行号或null], ...]，删除状态统一只用 DEL。\n'
            '  · --fingerprint + --check 做集合层零丢失校验（只比种类、忽略次数下降），'
            '任一硬数据种类缺失即退出码 1；只给 --check 时基线从 --source 现场抽取。\n'
            '  · --map 单独产出逐行去向映射（连续删除合并为区间并注明原因），不混入输出正文。\n'
            '  · 纯标准库、UTF-8、全程二进制 rb / splitlines(keepends=True) / wb。'))
    ap.add_argument('--source', required=True, help='源文档路径（必填，UTF-8）')
    ap.add_argument('--out', required=True, help='合并文档输出路径（必填）')
    ap.add_argument('--state', required=True,
                    help='状态文件 _merge_state.json 输出路径（必填，含 mapping / scatter_start / stats）')
    ap.add_argument('--map', default=None,
                    help='可选：逐行去向映射文档（.md），输出文档之外单独存放')
    ap.add_argument('--scatter-marker', default=None,
                    help='可选：零散内容块标记文字；取「首个含该文字的标题行」起至文末为零散块；'
                         '未指定时自动探测「本块全条同源/仅标此一次/零散内容」类标记行')
    ap.add_argument('--no-auto-scatter', action='store_true',
                    help='关闭零散内容块自动探测；仅在显式给了 --scatter-marker 时分区')
    ap.add_argument('--keep-rule', default=None,
                    help='可选：窄规则关键词文件（每行一词，# 开头为注释）；与内置规则词合并')
    ap.add_argument('--fingerprint', default=None,
                    help='可选：硬数据指纹基线 JSON（含 nums / vals / conf），配合 --check 校验')
    ap.add_argument('--check', action='store_true',
                    help='可选：合并后做集合层零丢失校验；缺失任一硬数据种类即非零退出码')
    ap.add_argument('--min-cjk', type=int, default=3,
                    help='Pass2 救回的最小中文字符数阈值，默认 3')
    ap.add_argument('--no-builtin-rules', action='store_true',
                    help='关闭内置窄规则词（用于精确复刻旧版「纯硬 token」判定）')
    args = ap.parse_args()

    if args.min_cjk < 0:
        print('[错误] --min-cjk 必须 >= 0。')
        sys.exit(2)
    if not args.check and args.fingerprint:
        print('[提示] 已指定 --fingerprint 但未给 --check，本次不做基线校验（仅执行内置零丢失断言）。')

    try:
        code = run(args)
    except MergeError as e:
        print(f'[错误] {e}')
        sys.exit(2)
    except AssertionError as e:
        print(f'[致命] 自检断言未通过：{e}')
        sys.exit(3)
    except Exception as e:  # 不裸 traceback
        print(f'[错误] 未预期异常：{type(e).__name__}: {e}')
        sys.exit(2)
    sys.exit(code)


if __name__ == '__main__':
    main()
