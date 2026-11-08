#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) 2026 doc-extract-filter contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""
doc-extract-filter 三道筛选协议硬校验工具。

实战来源：finalize_filter.py。
三道协议：
  A 为初判全集；B ⊆ A；C.items 覆盖 B 每条；
  C 的 final_remove_lines / final_restore_lines（或等价字段）互斥且并集 == A；
  行号无越界；reason 非空。
可选 --final：校验定稿 = 母本按行号删去 remove 集后的逐字节结果（二进制）。
逐项打印 PASS/FAIL，退出码表示总结果（全 PASS=0，否则 1；致命错误=2）。

所有文件读写一律二进制模式（rb/wb），
禁止依赖 Windows 文本模式默认换行转换（否则定稿逐字节校验必然失真）。
"""
import argparse
import json
import os
import sys

# 「或等价字段」：按候选 key 名在嵌套 dict 里查找，兼容不同 agent 的字段命名/位置
REMOVE_KEYS = ('final_remove_lines', 'remove_lines', 'remove', '剔除', 'final_remove')
RESTORE_KEYS = ('final_restore_lines', 'restore_lines', 'restore', '放回',
                'final_keep_lines', 'final_restore')


def read_bytes(path):
    if not os.path.isfile(path):
        raise SystemExit(f"[错误] 找不到文件：{path}\n请检查路径（Windows 用正斜杠 / ）是否正确。")
    return open(path, 'rb').read()


def load_json(path):
    try:
        return json.loads(read_bytes(path).decode('utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise SystemExit(f"[错误] JSON 格式错误：{path}\n{e}")


def items_of(obj, label):
    if not isinstance(obj, dict) or not isinstance(obj.get('items'), list):
        raise SystemExit(f"[错误] {label} 需为含 items 数组的对象")
    return obj['items']


def deep_find(obj, keys):
    """在嵌套 dict/list 里按候选 key 名查找第一个出现的值（支持「或等价字段」）。"""
    if isinstance(obj, dict):
        for k in keys:
            if k in obj:
                return obj[k]
        for v in obj.values():
            r = deep_find(v, keys)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = deep_find(v, keys)
            if r is not None:
                return r
    return None


def line_set(items):
    out = set()
    for it in items:
        if isinstance(it, dict):
            n = it.get('line')
            if isinstance(n, int) and not isinstance(n, bool):
                out.add(n)
    return out


def lines_ok(items, nlines):
    for it in items:
        if not isinstance(it, dict):
            return False
        n = it.get('line')
        if not isinstance(n, int) or isinstance(n, bool) or not (1 <= n <= nlines):
            return False
    return True


def reasons_ok(items):
    for it in items:
        if not isinstance(it, dict) or not str(it.get('reason', '')).strip():
            return False
    return True


def main():
    ap = argparse.ArgumentParser(
        description="doc-extract-filter 三道筛选协议硬校验（集合互斥/覆盖/定稿逐字节）",
        epilog=("示例： python verify_three_pass.py --a agent1.json --b agent2.json "
                "--c agent3.json --source 母本.txt --final 定稿.txt"))
    ap.add_argument('--a', required=True, help="第1道 agent JSON（初判全集）")
    ap.add_argument('--b', required=True, help="第2道 agent JSON（复核抢救，须为 A 子集）")
    ap.add_argument('--c', required=True, help="第3道 agent JSON（终审，含 remove/restore 字段）")
    ap.add_argument('--source', required=True, help="母本文件（必填，用于行号越界与定稿校验）")
    ap.add_argument('--final', default=None,
                    help="可选：定稿文件，校验其 = 母本删去 remove 集后的逐字节结果")
    args = ap.parse_args()

    A = items_of(load_json(args.a), '--a')
    B = items_of(load_json(args.b), '--b')
    C = load_json(args.c)
    C_items = items_of(C, '--c')
    raw = read_bytes(args.source)
    lines = raw.splitlines(keepends=True)
    nlines = len(lines)

    a_set = line_set(A)
    b_set = line_set(B)
    c_set = line_set(C_items)
    remove_raw = deep_find(C, REMOVE_KEYS)
    restore_raw = deep_find(C, RESTORE_KEYS)

    results = []

    def check(name, ok, detail=''):
        results.append((name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ''))

    print("=== 三道协议硬校验 ===")
    print(f"  母本：{args.source}（{len(raw)} 字节 / {nlines} 物理行）")

    # 1) A 为初判全集（自身结构合法、非空、无重复、reason 非空）
    a_ok = (len(A) > 0 and len(A) == len(a_set) and lines_ok(A, nlines) and reasons_ok(A))
    check("A 为初判全集（非空/行号合法/无重复/reason 非空）", a_ok, f"{len(A)} 条")

    # 2) B ⊆ A
    b_ok = (len(B) > 0 and len(B) == len(b_set) and b_set <= a_set
            and lines_ok(B, nlines) and reasons_ok(B))
    check("B ⊆ A（复核条目均来自初判，行号/reason 合法）", b_ok,
          f"B {len(b_set)} 条 / A {len(a_set)} 条")

    # 3) C.items 覆盖 B 每条
    cov_ok = (b_set <= c_set)
    check("C.items 覆盖 B 每条", cov_ok,
          f"C.items {len(c_set)} 条，B 未覆盖 {len(b_set - c_set)} 条")

    # 4) remove / restore 字段存在、互斥、并集 == A
    remove = set()
    if remove_raw is None or restore_raw is None:
        check("C 含 remove/restore 字段", False,
              f"remove={'缺' if remove_raw is None else '有'}, restore={'缺' if restore_raw is None else '有'}")
        check("remove/restore 互斥且并集 == A", False, "字段缺失，无法判定")
    else:
        try:
            remove = set(int(x) for x in remove_raw)
            restore = set(int(x) for x in restore_raw)
        except (TypeError, ValueError) as e:
            check("C 含 remove/restore 字段", True, "字段存在但非整数行号列表")
            check("remove/restore 互斥且并集 == A", False, str(e))
        else:
            check("C 含 remove/restore 字段", True,
                  f"remove {len(remove)} / restore {len(restore)}")
            rr_ok = (remove.isdisjoint(restore) and (remove | restore) == a_set)
            check("remove/restore 互斥且并集 == A", rr_ok,
                  f"交集 {len(remove & restore)}，并集 {len(remove | restore)} / A {len(a_set)}")

    # 5) 行号无越界（A/B/C.items/remove/restore）
    all_nums = a_set | b_set | c_set | remove
    if restore_raw is not None:
        try:
            all_nums |= set(int(x) for x in restore_raw)
        except (TypeError, ValueError) as e:
            # 不静默吞异常：第 4 项已把「非整数行号」判为 FAIL，这里留一条可追溯警告
            print("  [警告] restore 行号含非整数，越界检查跳过该字段：%s" % e,
                  file=sys.stderr)
    bad = [n for n in all_nums if not (1 <= n <= nlines)]
    check("行号无越界（全部 1..%d）" % nlines, not bad,
          "越界行号 " + (str(sorted(bad)[:10]) if bad else "无"))

    # 6) reason 非空（A/B/C.items）
    r_ok = reasons_ok(A) and reasons_ok(B) and reasons_ok(C_items)
    check("reason 非空（A/B/C.items 每条）", r_ok)

    # 7) --final 定稿逐字节
    if args.final is not None:
        final = read_bytes(args.final)
        kept = b''.join(l for i, l in enumerate(lines, 1) if i not in remove)
        f_ok = (final == kept)
        check("--final 定稿 = 母本删去 remove 集后的逐字节结果", f_ok,
              f"定稿 {len(final)} B / 期望 {len(kept)} B")

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"=== 总结果：{passed}/{total} 项 PASS ===")
    sys.exit(0 if passed == total else 1)


if __name__ == '__main__':
    main()
