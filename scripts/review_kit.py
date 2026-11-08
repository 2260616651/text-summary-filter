# -*- coding: utf-8 -*-
# Copyright (c) 2026 doc-extract-filter contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""交付前「三重查漏 + 冲突裁决」工具箱（纯标准库）。

对应 SKILL.md 原则 5/6/7 与「追加与统一整理·冲突裁决」：
  - `conflicts`  抽冲突标记 → 编号化 registry（含跨批合并去重）→ 待用户四选一裁决清单
  - `numbers`    原文 vs 终稿数字指纹**种类差集** → 带原文上下文的补漏清单
  - `coverage`   原文结构清单（章节/条款编号）→ 终稿规则级落点覆盖度自评

产出均为 Markdown 清单，落在**输出文档之外**；主线程据此批量问用户或回补，不删任何原文。

用法：
    python review_kit.py conflicts --source 原文.txt [--out 冲突清单.md]
    python review_kit.py numbers   --source 原文.txt --final 终稿.txt --out 补漏清单.md
    python review_kit.py coverage  --source 原文.txt --final 终稿.txt --out 覆盖度.md
    python review_kit.py all       --source 原文.txt --final 终稿.txt --outdir 目录
"""
from __future__ import annotations

import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fingerprint as fp  # noqa: E402

CONF_PAT = re.compile(r"【冲突[：:][^】]*】")
# 编号化 registry：⚠️并存N / J1~N / 冲突总 registry 里的 ≡ 合并关系
REGISTRY_PAT = re.compile(r"(?:⚠️?\s*并存\s*\d+|J\d+|并存\d+≡J\d+|J\d+≡J\d+)")
HEADING_PAT = re.compile(r"^(#{1,6}\s+.+|第[一二三四五六七八九十百]+[章节]\s*.+|[一二三四五六七八九十]+、.+)")
CLAUSE_PAT = re.compile(r"\b([a-z]{2}-\d{3})\b")
RULE_LEVEL_HINT = re.compile(r"(\d+(?:\.\d+)?\s*(?:%|％|格|层|级|秒|分钟|小时|天|KB|MB|GB|kg|g|cm|m|km|倍|点|条|个)"
                             r"|必须|不许|禁止|若|当|则|阈值|条件|公式|上限|下限|触发)")


def read(path: str) -> str:
    with open(path, "rb") as f:
        return f.read().decode("utf-8", errors="replace").replace("\r\n", "\n")


def conflicts(source: str, out: str) -> str:
    txt = read(source)
    marks = CONF_PAT.findall(txt)
    # 同类冲突文本去重（同一口径多处出现只留一条，但记录出现次数）
    seen: dict[str, int] = {}
    for m in marks:
        seen[m] = seen.get(m, 0) + 1
    # 编号化 registry 与 ≡ 合并关系
    regs = sorted(set(REGISTRY_PAT.findall(txt)))
    merges = [r for r in regs if "≡" in r]

    lines = ["# 冲突裁决清单（交付前批量问用户）", "",
             "> 由 `review_kit.py conflicts` 生成。**不得自动二选一**："
             "每个独立口径都要问用户「保留前 / 保留后 / 都保留 / 自定义」，批量一次问完。",
             "",
             "- 冲突标记总数：%d 处，独立口径 %d 条" % (len(marks), len(seen)),
             "- 编号化 registry：%s" % (", ".join(regs) if regs else "无"),
             "- 跨批合并关系：%s" % (", ".join(merges) if merges else "无"),
             "", "## 独立口径（逐条裁决）", "",
             "| # | 冲突口径 | 出现次数 | 裁决 |", "|---|---|---|---|"]
    for i, (m, c) in enumerate(sorted(seen.items()), 1):
        lines.append("| %d | %s | %d | 待用户四选一 |" % (i, m.replace("|", "/"), c))
    lines += ["", "> 大致重复（互补/各带不同信息）**全部保留**，不进本清单；"
              "完全重复才指回前文不重写。无人值守时可自动取信息更完整的一版，"
              "但必须在终稿文末列「待确认冲突项」供事后裁决。", ""]
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return "独立口径 %d 条 / 编号化 registry %d 项 / 合并关系 %d 项" % (
        len(seen), len(regs), len(merges))


def numbers(source: str, final: str, out: str) -> tuple[str, int]:
    src = read(source)
    fin = read(final)
    a = fp.extract(src, exclude_noise=True)
    b = fp.extract(fin, exclude_noise=True)
    sets = {
        "设计点编号": (set(a["nums"]), set(b["nums"])),
        "数值+单位": (set(a["vals"]), set(b["vals"])),
        "冲突标记": (set(a["conf"]), set(b["conf"])),
    }
    lines = ["# 数字指纹补漏清单（差集逐个补回）", "",
             "> 口径：**种类覆盖**（只报种类差集，忽略出现次数下降）；"
             "比对前已做全角→半角规整、噪声排除。差集项须回原文定位后补回终稿，补完才允许交付。",
             "", "| 类别 | 基线种类 | 终稿种类 | 缺失 |", "|---|---|---|---|"]
    total_missing = 0
    missing_by_cat = {}
    for k, (x, y) in sets.items():
        miss = sorted(x - y)
        missing_by_cat[k] = miss
        total_missing += len(miss)
        lines.append("| %s | %d | %d | %d |" % (k, len(x), len(y), len(miss)))
    lines += ["", "## 差集明细（带原文上下文）", ""]
    src_lines = src.splitlines()
    for k, miss in missing_by_cat.items():
        if not miss:
            continue
        lines.append("### %s（%d 项）" % (k, len(miss)))
        lines.append("")
        for v in miss:
            hit = next((i for i, l in enumerate(src_lines, 1) if v in l), None)
            ctx = src_lines[hit - 1].strip()[:160] if hit else "（原文未定位到，可能是正则伪值）"
            lines.append("- `%s` → 原文 L%s：%s" % (v, hit if hit else "?", ctx.replace("|", "/")))
        lines.append("")
    lines += ["## 噪声定性（不计真漏）", "",
              "- 图片 CDN 哈希 / auth_key / 13 位时间戳 / 纯章节序号不承载内容；",
              "- 大数字子串伪值（如 `6%` 实为 `6.25%` 的一部分）应先由程序排除；",
              "- 全角/半角差异（`％` vs `%`）已规整，不算漏。", "",
              "- **总判定**：%s" % ("PASS（0 缺失）" if total_missing == 0
                                    else "需补回 %d 项后才允许交付" % total_missing), ""]
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return "缺失 %d 项（编号 %d / 数值 %d / 冲突 %d）" % (
        total_missing, len(missing_by_cat["设计点编号"]),
        len(missing_by_cat["数值+单位"]), len(missing_by_cat["冲突标记"])), total_missing


def coverage(source: str, final: str, out: str) -> tuple[str, int, int]:
    src, fin = read(source), read(final)
    heads = []
    for l in src.splitlines():
        m = HEADING_PAT.match(l.strip())
        if m:
            heads.append(m.group(1).strip()[:80])
    # 去重保序
    heads = list(dict.fromkeys(heads))
    src_clauses = set(CLAUSE_PAT.findall(src))
    fin_clauses = set(CLAUSE_PAT.findall(fin))
    fin_text = fin

    lines = ["# 结构清单覆盖度自评", "",
             "> 对应 SKILL.md 原则 6：整理前先盘点原文结构清单作核对基线，"
             "终稿里每个核心子系统必须有**规则级落点**（可执行参数/条件/公式/流程）。"
             "「接XX大改」「详见XX」式裸引用不算覆盖；分散碎片要合并成独立模块，不许蒸发。",
             "",
             "- 原文结构清单：%d 条（章节/条款标题）" % len(heads),
             "- 条款编号：原文 %d 种 / 终稿 %d 种" % (len(src_clauses), len(fin_clauses)),
             "", "| # | 结构条目 | 终稿落点 | 规则级? |", "|---|---|---|---|"]
    covered = 0
    rule_level = 0
    for i, h in enumerate(heads, 1):
        # 用标题中的关键词在终稿里找对应段落
        key = re.sub(r"^[#\s]+", "", h)[:12]
        pos = fin_text.find(key)
        has = pos >= 0
        if has:
            window = fin_text[pos:pos + 400]
            rl = bool(RULE_LEVEL_HINT.search(window))
        else:
            rl = False
        covered += int(has)
        rule_level += int(has and rl)
        lines.append("| %d | %s | %s | %s |" %
                     (i, h.replace("|", "/"),
                      "有" if has else "**无（疑似蒸发）**",
                      "是" if rl else "否（需展开或标注「原文即引用、无细则」）"))
    miss_clause = sorted(src_clauses - fin_clauses)
    lines += ["", "## 条款编号差集", "",
              "- 原文有、终稿无：%s" % (", ".join(miss_clause[:30]) if miss_clause else "无"),
              "- 共 %d 项（条款序号类需程序定性排除纯编号噪声）" % len(miss_clause),
              "", "## 覆盖度自评", "",
              "- 结构清单 **%d / %d** 条在终稿有落点" % (covered, len(heads)),
              "- 其中 **%d** 条具备规则级落点" % rule_level,
              "- 裸引用不算覆盖；分散碎片强制合并成独立功能模块。", ""]
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return "结构 %d/%d 有落点，%d 条规则级" % (covered, len(heads), rule_level), covered, len(heads)


def main() -> None:
    ap = argparse.ArgumentParser(description="交付前三重查漏 + 冲突裁决工具箱")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("conflicts", "numbers", "coverage", "all"):
        p = sub.add_parser(name)
        p.add_argument("--source", required=True, help="原始母本")
        if name in ("numbers", "coverage", "all"):
            p.add_argument("--final", required=True, help="终稿")
        if name == "all":
            p.add_argument("--outdir", required=True, help="三份清单输出目录")
        else:
            p.add_argument("--out", required=True, help="清单输出路径")
    a = ap.parse_args()

    if a.cmd == "all":
        os.makedirs(a.outdir, exist_ok=True)
        d = a.outdir
        print("=== 三重查漏 + 冲突裁决 ===")
        print("  冲突：", conflicts(a.source, os.path.join(d, "冲突裁决清单.md")))
        msg, n = numbers(a.source, a.final, os.path.join(d, "数字补漏清单.md"))
        print("  数字：", msg)
        cov, c, t = coverage(a.source, a.final, os.path.join(d, "覆盖度自评.md"))
        print("  覆盖：", cov)
        print("  已写出 3 份清单到", d)
        # 数字差集非空时不阻断（需人工补回），但明确提示
        raise SystemExit(0)
    if a.cmd == "conflicts":
        print(conflicts(a.source, a.out))
    elif a.cmd == "numbers":
        msg, n = numbers(a.source, a.final, a.out)
        print(msg, "→", a.out)
        raise SystemExit(0 if n == 0 else 1)
    else:
        cov, c, t = coverage(a.source, a.final, a.out)
        print(cov, "→", a.out)


if __name__ == "__main__":
    main()
