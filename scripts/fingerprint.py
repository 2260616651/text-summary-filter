#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) 2026 doc-extract-filter contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""
doc-extract-filter 硬数据指纹工具（抽取 / 集合层零丢失校验）。

实战来源：build_fingerprint.py（基线生成）+ finalize_filter.py / merge_v9.py（零丢失校验）。
三类硬数据指纹：
    1) 设计点编号   \\b[a-z]{2}-\\d{3}\\b
    2) 数值 + 单位   \\d+(?:\\.\\d+)?\\s*(单位表)
    3) 冲突标记     【冲突[：:][^】]*】

生成模式：从 --source 抽取，写 --out (baseline.json)。
校验模式：给 --check 时做「集合层」比对 —— 只比种类覆盖、只报种类差集，
          明确忽略出现次数下降；打印每类 基线/输出/缺失，退出码 0/1。
          基线优先取 --out 现成的指纹 JSON；若 --out 不是现成基线，则从 --source 现场抽取并写出。

比对前一律做「全角→半角」规整（％→%、全角数字→半角）。
--exclude-noise 排除 CDN 哈希 / auth_key / 13 位时间戳 / 纯章节序号 / 大数字子串。

所有文件读写一律二进制模式（rb/wb）或显式 newline='' + encoding='utf-8'，
禁止依赖 Windows 文本模式默认换行转换（否则 CRLF 漂移会让指纹与字节校验失真）。
"""
import argparse
import json
import os
import re
import sys

# ---- 全角→半角规整表：％→%、全角数字 ０-９→0-9 ----
_FULL2HALF = {0xFF05: ord('%')}
for _i in range(10):
    _FULL2HALF[0xFF10 + _i] = ord('0') + _i

DEFAULT_UNITS = ['KB', 'MB', 'GB', 'kg', 'g', 'cm', 'm', 'km', '格', '区块', '天', '秒', '分钟', '小时',
                 '年', '%', '倍', '层', '级', '个', '条', '点', '元', 'W', 'T', 'Hz', 'KB/s', 'MB/s']
UNITS = list(DEFAULT_UNITS)


def _build_val_pat(units):
    """单位按**长度降序**排列，避免 'KB' 抢先匹配掉 'KB/s'（否则 100KB/s 只会被抽成 100KB）。"""
    ordered = sorted(set(units), key=lambda u: (-len(u), u))
    escaped = '|'.join(re.escape(u) for u in ordered)
    return re.compile(r'\d+(?:\.\d+)?\s*(?:' + escaped + r')')


def load_units_file(path):
    """从外部文件追加单位词（每行一个，# 开头为注释，空行跳过）。
    用于不同领域文档（如 '台/件/人/亩/吨/℃'）不改代码即可扩表。
    二进制读 + errors='replace'，避免非 UTF-8 词表文件抛未捕获的 UnicodeDecodeError。"""
    if not path:
        return []
    if not os.path.isfile(path):
        raise ValueError("单位词表文件不存在：%s" % path)
    try:
        raw = open(path, 'rb').read()
        text = raw.decode('utf-8', errors='replace')
    except OSError as e:
        raise ValueError("单位词表文件读取失败：%s\n%s" % (path, e))
    extra = []
    for line in text.splitlines():
        t = line.strip()
        if not t or t.startswith('#'):
            continue
        extra.append(t)
    return extra


def set_units(extra=None):
    """重建 VAL_PAT；extra 为单位列表（追加到默认表）。"""
    global UNITS, VAL_PAT
    UNITS = list(dict.fromkeys(list(DEFAULT_UNITS) + list(extra or [])))
    VAL_PAT = _build_val_pat(UNITS)
    return UNITS


ID_PAT = re.compile(r'\b[a-z]{2}-\d{3}\b')
VAL_PAT = _build_val_pat(UNITS)
CONF_PAT = re.compile(r'【冲突[：:][^】]*】')

# 噪声 token 规则（token 级过滤，保证「现成基线」与「现场抽取」两侧口径一致）
NOISE_TOKEN_PATS = [
    re.compile(r'auth_key', re.IGNORECASE),            # auth_key 鉴权串
    re.compile(r'\b[a-f0-9]{32,}\b', re.IGNORECASE),  # CDN 长哈希
    re.compile(r'\b\d{13}\b'),                         # 13 位毫秒时间戳
    re.compile(r'^\s*\d+(?:\.\d+)+\s*$'),              # 纯章节序号（层级编号 1.2.3）
    re.compile(r'\b\d{10,}\b'),                        # 大数字子串
]


def normalize(text):
    """比对前规整：全角→半角（％→%、全角数字→半角）。"""
    return str(text).translate(_FULL2HALF)


def is_noise(token):
    return any(p.search(token) for p in NOISE_TOKEN_PATS)


def read_text(path):
    if not os.path.isfile(path):
        raise SystemExit(f"[错误] 找不到文件：{path}\n请检查路径（Windows 用正斜杠 / ）是否正确。")
    try:
        return open(path, 'rb').read().decode('utf-8')
    except UnicodeDecodeError as e:
        raise SystemExit(f"[错误] 文件不是合法 UTF-8：{path}\n{e}")


def extract(text, exclude_noise=False):
    """抽取三类指纹。nums/conf 去重排序；vals 保留全部命中（含重复，与实战基线一致）。"""
    text = normalize(text)
    nums = sorted(set(ID_PAT.findall(text)))
    vals = VAL_PAT.findall(text)
    conf = sorted(set(CONF_PAT.findall(text)))
    if exclude_noise:
        nums = [x for x in nums if not is_noise(x)]
        vals = [x for x in vals if not is_noise(x)]
        conf = [x for x in conf if not is_noise(x)]
    return {
        'nums': nums,
        'vals': vals,
        'conf': conf,
        'total_lines': len(text.splitlines()),
    }


def load_baseline(path):
    """若 --out 是现成且合法的指纹 JSON 则读取，否则返回 None（回退到从 source 抽取）。"""
    if not os.path.isfile(path):
        return None
    try:
        data = json.loads(open(path, 'rb').read().decode('utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise SystemExit(f"[错误] 基线 JSON 格式错误：{path}\n{e}")
    if isinstance(data, dict) and all(k in data for k in ('nums', 'vals', 'conf')):
        return data
    return None


def write_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    # 二进制写：禁止依赖 Windows 文本模式默认换行转换
    open(path, 'wb').write(json.dumps(data, ensure_ascii=False, indent=1).encode('utf-8'))


def _degrade(v):
    """复合单位降级：4.8MB/s -> {4.8MB/s, 4.8MB}，4.8MB 不变；
    双向兼容新旧正则口径（旧正则把 100KB/s 抽成 100KB 会丢失 /s），不掩盖真丢失。"""
    s = normalize(v)
    out = {s}
    m = re.match(r'^(.*?)/s$', s)
    if m:
        out.add(m.group(1))
    return out


def as_kinds(values, exclude_noise):
    """集合层口径：规整后取种类集合；复合单位降级展开；可选剔除噪声 token。"""
    kinds = set()
    for v in values:
        kinds |= _degrade(v)
    if exclude_noise:
        kinds = {k for k in kinds if not is_noise(k)}
    return kinds


def compare(baseline, output, exclude_noise):
    """集合层比对：只比种类覆盖，忽略出现次数下降。返回是否全部通过。"""
    print("=== 集合层校验（只比种类覆盖，明确忽略出现次数下降）===")
    labels = (('nums', '设计点编号'), ('vals', '数值+单位'), ('conf', '冲突标记'))
    all_ok = True
    for key, label in labels:
        b = as_kinds(baseline.get(key, []), exclude_noise)
        o = as_kinds(output.get(key, []), exclude_noise)
        miss = b - o
        ok = not miss
        all_ok = all_ok and ok
        print(f"  [{label}] 基线 {len(b)} 种 | 输出 {len(o)} 种 | 缺失 {len(miss)} 种  "
              f"{'PASS' if ok else 'FAIL'}")
        if miss:
            sample = sorted(miss)[:8]
            more = ' ...' if len(miss) > 8 else ''
            print(f"      缺失样例：{sample}{more}")
    print("=== 总判定：" + ("PASS（硬数据种类零丢失）" if all_ok else "FAIL（存在种类缺失）") + " ===")
    return all_ok


def main():
    ap = argparse.ArgumentParser(
        description="doc-extract-filter 硬数据指纹工具：抽取三类硬数据指纹 / 集合层零丢失校验",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("示例：\n"
                "  生成基线： python fingerprint.py --source 原文.txt --out 基线.json\n"
                "  校验产物： python fingerprint.py --source 原文.txt --out 基线.json --check 产物.txt\n"
                "             （--out 若是现成基线 JSON 则直接读取，否则从 --source 抽取并写出）\n"
                "  排除噪声： 追加 --exclude-noise"))
    ap.add_argument('--source', required=True,
                    help="原文（必填）：生成模式的抽取对象；校验模式下 --out 非基线时的基线来源")
    ap.add_argument('--out', required=True,
                    help="baseline.json 路径（必填）：生成模式写出；校验模式若为现成基线则读取")
    ap.add_argument('--check', default=None,
                    help="待校验文件；给出即进入校验模式（集合层种类覆盖比对，退出码 0/1）")
    ap.add_argument('--exclude-noise', action='store_true',
                    help="排除 CDN 哈希 / auth_key / 13 位时间戳 / 纯章节序号 / 大数字子串")
    ap.add_argument('--allow-empty-baseline', action='store_true',
                    help="允许基线三类指纹全空时仍以 0 退出（默认拒绝，避免「假绿」通过验收）")
    ap.add_argument('--units-file', default=None,
                    help="可选：外部单位词表文件（每行一个单位，# 注释），追加到默认单位表；"
                         "不同领域文档可加 台/件/人/亩/吨/℃ 等，不改代码")
    args = ap.parse_args()

    if args.units_file:
        try:
            extra = load_units_file(args.units_file)
            set_units(extra)
            print(f"[单位表] 已从 {args.units_file} 追加 {len(extra)} 个单位；"
                  f"合计 {len(UNITS)} 个（复合单位按长度降序优先匹配）")
        except ValueError as e:
            raise SystemExit("[错误] %s" % e)

    if not args.check:
        # ---- 生成模式 ----
        data = extract(read_text(args.source), args.exclude_noise)
        write_json(args.out, data)
        print("=== 生成硬数据指纹基线 ===")
        print(f"  来源：{args.source}")
        print(f"  总行数：{data['total_lines']}")
        print(f"  设计点编号 {len(data['nums'])} 种 | 数值+单位 {len(set(data['vals']))} 种"
              f"（命中 {len(data['vals'])} 次）| 冲突标记 {len(data['conf'])} 种")
        print(f"  全角→半角规整：开启 | 除噪：{'开启' if args.exclude_noise else '关闭'}")
        if not (data['nums'] or data['vals'] or data['conf']):
            print("  [警告] 基线三类指纹全部为空！后续集合层校验会「零丢失 PASS」但属于**假绿**："
                  "原文可能是空文件、非 UTF-8、或不含本工具识别的编号/数值单位/冲突标记。"
                  "请确认文档内容，必要时扩充单位词表或用语义层核对（剔除行内容体检）。")
        print(f"  已写出：{args.out}")
        return

    # ---- 校验模式 ----
    baseline = load_baseline(args.out)
    if baseline is None:
        baseline = extract(read_text(args.source), args.exclude_noise)
        write_json(args.out, baseline)
        print(f"[基线] --out 非现成基线，已从 --source 抽取并写出：{args.out}")
    else:
        print(f"[基线] 读取现成基线：{args.out}")
    output = extract(read_text(args.check), args.exclude_noise)
    print(f"[输出] 待校验文件：{args.check}（共 {output['total_lines']} 行）")
    print(f"[口径] 集合层（种类覆盖，忽略出现次数下降）| 全角→半角规整：开启"
          f" | 除噪：{'开启' if args.exclude_noise else '关闭'}")
    empty_base = not (baseline['nums'] or baseline['vals'] or baseline['conf'])
    if empty_base:
        print("[警告] 基线三类指纹全空，本次「零缺失」属**假绿**，不能作为零丢失证据；"
              "请检查源文档或改用语义层核对（剔除行内容体检）。")
    ok = compare(baseline, output, args.exclude_noise)
    if empty_base and not args.allow_empty_baseline:
        print("[拒绝] 基线全空且未给 --allow-empty-baseline：禁止以假绿结果通过验收（退出码 1）。")
        sys.exit(1)
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
