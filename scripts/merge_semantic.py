#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) 2026 text-summary-filter contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""text-summary-filter 第 3 步「条目合并」真语义合并的机械工具（脚本不调模型）。

三子命令：
  prepare  机械生成「语义相近／描述同类事物」候选簇 + 完整 agent 提示词（只产候选、不裁决）
  verify   全覆盖 / 行号 / reason / 数字指纹零丢失 硬校验 + 逐行去向映射报告
  render   先跑与 verify 相同的硬校验，再渲染合并稿 + 逐行去向映射文档

红线（脚本不做语义）：脚本只做候选簇生成、硬校验、渲染；语义裁决只能由
**1 个前台 agent** 完成，脚本不调模型、不自行改写/删减任何事实。合并稿是
「筛选基准稿按行保留 + 簇内合并」的结果：每个原始物理行号恰好被覆盖一次
（全覆盖、无遗漏、无重复引用）+ 数字/编号/单位指纹零丢失 + 不得凭空新增
原文没有的硬数据（严格只追加语义）。

所有文件读写一律二进制或显式 encoding='utf-8'，不依赖 Windows 文本模式默认换行转换。
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fingerprint  # noqa: E402  复用三类指纹 / 全角半角规整 / 集合层口径

CAND_NAME = "合并_候选簇.json"
PROMPT_NAME = "合并_agent_prompt.md"
COVER_NAME = "合并_覆盖校验.md"
RESULT_DEFAULT = "合并结果.json"

# 裸数字 token：覆盖阈值 / 百分比 / 版本号 / 配比 / 范围 / 公式系数 中的数字
NUM_TOKEN = re.compile(r"\d+(?:\.\d+)?")
# 硬数据 token：数字（含小数/百分比）、设计点编号；用于「同类事物」候选信号
HARD_TOKEN = re.compile(r"\d+(?:\.\d+)?%?|[a-z]{2}-\d{3}", re.IGNORECASE)
CONF_MARK = "【冲突：源稿矛盾】"
AI_MARK = "（AI 推论）"

AGENT_PROMPT = """你是「第 3 步 条目合并 · 语义合并裁决 Agent」。严格前台串行、并发=1，
不启动其他 agent、不并发，跑完才结束。

## 必读（完整读完再动手）
1) 落盘稿（筛选后的原文，物理行号 1-based，自己用 Read 按行号读）：@INPUT@
2) 机械候选簇（只产候选、不裁决）：@CAND@
3) 本提示词全文。

## 你的任务
只处理候选簇内的成员行：把**语义相近／描述同类事物**的语句合并成更简洁的表述，
并在**保证不丢失任何信息／要点**的前提下简化描述。未入簇的行不合并、原样作为独立 item。
脚本不做语义判断，是否合并、怎么合并由你裁决；但脚本会做硬校验，不合规会被拦下重做。

## 硬红线（违反即硬校验失败，必须重做）
- **数字／编号／单位／阈值／百分比／范围／公式系数／版本号／配比，一个不许漏**：
  原文每个数值与编号都必须在合并稿里还能找到（种类零丢失，不取一丢一）。
- **不删任何原文陈述的信息点**：可合并同类、可压缩措辞，但每个信息点必须有落点。
- **同字段多值冲突全部保留**，并显式标 @CONF@；绝不取一丢一、绝不悄悄抹平。
- **不得引入原文没有的事实／数字**：只做合并与简化，不计算、不推断、不新增数值
  （严格只追加语义，合并稿不得出现落盘稿没有的数字/编号种类）。
- 文档外推论必须显式标 @AI@，不与原文混写。
- 不改写数字形态（如 0.80 不要写成 0.8）、不把阿拉伯编号改成中文数字。
- 输出不能在候选簇之外凭空新增条目；每个 item 都要能追溯到你覆盖的那些原始行。

## 输出 JSON（唯一允许写的文件）
只写：@RESULT@ （UTF-8，不要修改任何其他文件、不要改落盘稿/候选簇/提示词）
结构：
{"items":[{"text":"合并或保留后的该条文本","sources":[原行号,...],"reason":"合并/保留依据"}, ...]}
- text：该 item 的正文（一条一行，不带行号前缀）；合并多个行时写成一句/一段，但信息点不丢。
- sources：该 item 覆盖的**原始物理行号**（int，升序，不越界，1..@N@）。
- reason：非空，写明「合并了哪几条 / 保留」及依据。

## 全覆盖（硬校验项，必须满足）
- 落盘稿共 @N@ 个物理行；**每个行号必须恰好出现在某一个 item 的 sources 里一次**
  （不遗漏、不重复引用）。
- **每个未在簇中的原始行也必须作为独立 item 出现在 items 里**（sources 只含它自己，
  含空行/纯分隔行；这类行 text 允许为空或原样空白）。
- 候选簇成员可以合并成一个 item（sources 列出全部成员），也可以因信息不冗余而拆成多个
  item；但无论合并与否，行号覆盖必须恰好一次。
- items 顺序建议按 sources 最小行号升序，便于逐行去向映射。

## 候选簇（机械生成，只产候选、不裁决）
@CLUSTERS@

## 未入簇行号（必须各自作为独立 item）
@UNCLUSTERED@

## 完成判据
写完 @RESULT@ 后自检：结构合法、行号全覆盖且无重复、reason 非空、数字/编号/单位一个没漏、
冲突多值全留并带标记、没有凭空新增数字。通过后**只回一行确认**：
`done merge <items 数>`，不回正文、不粘贴内容给主线程。

## 主线程后续（不是你要做的）
产出后由主线程跑：@RESUME@，脚本会 verify + render 做全覆盖与数字指纹零丢失硬校验，
通过才把合并稿落盘。全程只派你这 1 个前台 agent（强锁 1、并发 1、跑完再起下一个）。
"""


# ---------------------------------------------------------------- 基础工具
def read_text(path: str) -> str:
    if not os.path.isfile(path):
        raise SystemExit("[错误] 找不到文件：%s" % path)
    return open(path, "rb").read().decode("utf-8", errors="replace")


def write_utf8(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    open(path, "wb").write(text.encode("utf-8"))


def is_content(line: str) -> bool:
    """是否含实质内容（中文/字母/数字）；空行、纯分隔符不算。"""
    return bool(re.search(r"[\u4e00-\u9fffA-Za-z0-9]", line))


def tokens_of(text: str) -> set[str]:
    """词元集合：英文按词、数字按整体、中文按单字 + 相邻双字（bigram）。"""
    t = fingerprint.normalize(text)
    out: set[str] = set()
    for m in re.findall(r"[A-Za-z]+|\d+(?:\.\d+)?", t):
        out.add(m.lower())
    cjk = re.findall(r"[\u4e00-\u9fff]", t)
    for c in cjk:
        out.add(c)
    for i in range(len(cjk) - 1):
        out.add(cjk[i] + cjk[i + 1])
    return out


def hard_tokens(text: str) -> set[str]:
    """硬数据 token：数字（含百分比/小数）、设计点编号；排除单字符数字降噪。"""
    return {m for m in HARD_TOKEN.findall(fingerprint.normalize(text)) if len(m) >= 2}


def similarity(a: str, b: str, ta: set[str], tb: set[str],
               ha: set[str], hb: set[str]) -> tuple[float, float, float]:
    """综合分 = max(SequenceMatcher 比率, 词元 Jaccard, 共享硬数据信号)。

    「描述同类事物但用词不同」的两行，字符/词元相似度可能不高，却常共享同一组
    数字/编号/单位；此时以「词元重叠系数 >=0.3 且硬数据重合度」作为第三个信号抬升
    综合分，使它们能被机械候选簇抓到（仍只产候选、不裁决，由前台 agent 定夺）。
    返回 (综合分, SequenceMatcher 比率, 词元 Jaccard)。
    """
    seq = difflib.SequenceMatcher(None, a.strip(), b.strip()).ratio()
    inter = len(ta & tb)
    union = ta | tb
    jac = (inter / len(union)) if union else 0.0
    overlap = inter / min(len(ta), len(tb)) if min(len(ta), len(tb)) else 0.0
    shared = ha & hb
    hard = 0.0
    if shared and overlap >= 0.30 and min(len(ha), len(hb)):
        hard = len(shared) / min(len(ha), len(hb))
    return max(seq, jac, hard), seq, jac


def build_clusters(lines: list[str], threshold: float, max_cluster: int) -> tuple[list[dict], set[int], list[int]]:
    """贪心连通：与簇内任一成员综合相似度 >= threshold 即入簇（单簇 <= max_cluster）。

    只产 size>=2 的候选簇；脚本不裁决簇是否真该合并，交前台 agent。
    """
    toks = [tokens_of(x) for x in lines]
    hards = [hard_tokens(x) for x in lines]
    groups: list[dict] = []
    for i, line in enumerate(lines, 1):
        if not is_content(line):
            continue
        best, best_sim = None, 0.0
        for g in groups:
            if len(g["members"]) >= max_cluster:
                continue
            cur = 0.0
            for m in g["members"]:
                s, _, _ = similarity(line, lines[m - 1], toks[i - 1], toks[m - 1],
                                     hards[i - 1], hards[m - 1])
                if s > cur:
                    cur = s
            if cur >= threshold and cur > best_sim:
                best, best_sim = g, cur
        if best is not None:
            best["members"].append(i)
            best["sims"].append(round(best_sim, 4))
        else:
            groups.append({"members": [i], "sims": []})

    cand: list[dict] = []
    clustered: set[int] = set()
    for g in groups:
        if len(g["members"]) < 2:
            continue
        members = sorted(g["members"])
        clustered.update(members)
        avg = round(sum(g["sims"]) / len(g["sims"]), 4) if g["sims"] else 0.0
        a, b = members[0], members[1]
        comp, seq, jac = similarity(lines[a - 1], lines[b - 1], toks[a - 1], toks[b - 1],
                                    hards[a - 1], hards[b - 1])
        if comp > max(seq, jac) + 1e-9 and (hards[a - 1] & hards[b - 1]):
            reason = "同类事物：共享数字/编号/单位硬数据（词元重叠）"
        elif seq >= jac:
            reason = "字符序列相似（difflib.SequenceMatcher）"
        else:
            reason = "同类事物词元集合重叠（中文双字/英文词 Jaccard）"
        cand.append({
            "id": len(cand) + 1,
            "lines": members,
            "similarity": avg,
            "seq_ratio": round(seq, 4),
            "jaccard": round(jac, 4),
            "reason": reason,
            "preview": [lines[n - 1].strip()[:60] for n in members],
        })
    unclustered = [i for i in range(1, len(lines) + 1) if i not in clustered]
    return cand, clustered, unclustered


# ---------------------------------------------------------------- 硬校验（verify / render 共用）
def load_baseline(path: str, fallback_text: str) -> dict:
    """--fingerprint 是合法基线 JSON 则用它；否则从落盘稿现场抽取（等价逻辑兜底）。"""
    try:
        d = json.loads(open(path, "rb").read().decode("utf-8"))
        if isinstance(d, dict) and all(k in d for k in ("nums", "vals", "conf")):
            return d
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        pass
    return fingerprint.extract(fallback_text, False)


def hard_check(input_path: str, result_path: str, fingerprint_path: str) -> tuple[bool, dict]:
    """第 3 步语义合并的硬校验：全覆盖 / 行号 / reason / 指纹零丢失 / 只追加语义。

    返回 (ok, report)。任一硬校验失败 -> ok=False，errors 列出明确原因（不谎报、不静默）。
    """
    errors: list[str] = []
    warns: list[str] = []
    lines = read_text(input_path).splitlines()
    n = len(lines)
    empty = {"errors": errors, "warns": warns, "N": n, "items": 0,
             "covered": 0, "coverage": "0%", "mapping": {}, "compressed": [],
             "fingerprint": {}, "num_miss": [], "num_add": [], "texts": [],
             "lines": lines}
    if n == 0:
        errors.append("落盘稿为空（0 物理行），拒绝合并")
        return False, empty
    try:
        obj = json.loads(open(result_path, "rb").read().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        errors.append("合并结果.json 读取/解析失败：%s" % e)
        return False, empty
    items = obj.get("items") if isinstance(obj, dict) else None
    if not isinstance(items, list) or not items:
        errors.append("合并结果.json 缺 items 数组或为空")
        return False, empty

    ref: dict[int, int] = {}
    mapping: dict[int, list[int]] = {}
    texts: list[str] = []
    compressed: list[tuple[int, list[int]]] = []
    for idx, it in enumerate(items, 1):
        if not isinstance(it, dict):
            errors.append("item #%d 不是对象" % idx)
            continue
        text = it.get("text")
        sources = it.get("sources")
        reason = it.get("reason")
        if not isinstance(text, str):
            errors.append("item #%d text 不是字符串" % idx)
            text = ""
        if not isinstance(reason, str) or not reason.strip():
            errors.append("item #%d reason 为空" % idx)
        if not isinstance(sources, list) or not sources:
            errors.append("item #%d sources 不是非空数组" % idx)
            continue
        try:
            nums = [int(x) for x in sources]
        except (TypeError, ValueError):
            errors.append("item #%d sources 含非整数行号" % idx)
            continue
        if nums != sorted(nums):
            errors.append("item #%d sources 未升序：%s" % (idx, nums))
        if len(set(nums)) != len(nums):
            errors.append("item #%d sources 内有重复行号：%s" % (idx, nums))
        for ln in nums:
            if not (1 <= ln <= n):
                errors.append("item #%d sources 行号越界：%d（总行数 %d）" % (idx, ln, n))
                continue
            ref[ln] = ref.get(ln, 0) + 1
            mapping.setdefault(ln, []).append(idx)
        # 覆盖含文字原文时 text 不得为空（防信息蒸发）；纯空行/分隔符行允许空 text
        if any(1 <= ln <= n and is_content(lines[ln - 1]) for ln in nums) and not text.strip():
            errors.append("item #%d 覆盖含文字原文但 text 为空（信息丢失）" % idx)
        texts.append(text)
        if len(nums) > 1:
            compressed.append((idx, nums))

    missing = [ln for ln in range(1, n + 1) if ref.get(ln, 0) == 0]
    dup = sorted(ln for ln, c in ref.items() if c > 1)
    if missing:
        errors.append("全覆盖失败：遗漏 %d 个原始行号（样例 %s）" % (len(missing), missing[:12]))
    if dup:
        errors.append("全覆盖失败：%d 个行号被重复引用（样例 %s）" % (len(dup), dup[:12]))

    source_text = "\n".join(lines)
    merged_text = "\n".join(texts)
    base_fp = load_baseline(fingerprint_path, source_text)
    out_fp = fingerprint.extract(merged_text, False)
    fp_detail: dict = {}
    for key, label in (("nums", "设计点编号"), ("vals", "数值+单位"), ("conf", "冲突标记")):
        b = set(fingerprint.as_kinds(base_fp.get(key, []), False))
        o = set(fingerprint.as_kinds(out_fp.get(key, []), False))
        miss, add = sorted(b - o), sorted(o - b)
        fp_detail[key] = {"label": label, "base": len(b), "out": len(o),
                          "miss": miss, "add": add}
        if miss:
            errors.append("硬数据指纹零丢失失败[%s]：缺失种类 %s" % (label, miss[:12]))
        if add:
            errors.append("严格只追加语义失败[%s]：凭空新增原文没有的种类 %s" % (label, add[:12]))
    bnum = set(NUM_TOKEN.findall(fingerprint.normalize(source_text)))
    onum = set(NUM_TOKEN.findall(fingerprint.normalize(merged_text)))
    num_miss, num_add = sorted(bnum - onum), sorted(onum - bnum)
    if num_miss:
        errors.append("数字/编号/单位零丢失失败：缺失裸数字种类 %s" % num_miss[:12])
    if num_add:
        errors.append("严格只追加语义失败：凭空新增裸数字种类 %s" % num_add[:12])

    # 软校验（告警即可，不阻断）
    if "【冲突" in source_text and "【冲突" not in merged_text:
        warns.append("落盘稿含冲突标记，合并稿未出现任何【冲突…】标记，请核对冲突多值是否全留")
    if CONF_MARK in merged_text:
        warns.append("已保留标准冲突标记 %s" % CONF_MARK)
    if ("AI 推论" in merged_text or "AI推论" in merged_text) and AI_MARK not in merged_text:
        warns.append("出现「AI 推论」字样但未用标准标记 %s，请规范" % AI_MARK)

    covered = sum(1 for ln in range(1, n + 1) if ref.get(ln, 0) == 1)
    report = {
        "errors": errors, "warns": warns, "N": n, "items": len(items),
        "covered": covered, "coverage": "%.1f%%" % (100.0 * covered / n),
        "mapping": mapping, "compressed": compressed,
        "fingerprint": fp_detail, "num_miss": num_miss, "num_add": num_add,
        "texts": texts, "lines": lines,
    }
    return (not errors), report


def report_lines(rep: dict) -> list[str]:
    md = ["# 第 3 步语义合并 · 覆盖校验", "",
          "- 落盘稿物理行：%d" % rep["N"],
          "- 合并结果 items：%d" % rep["items"],
          "- 覆盖率：%d/%d（%s）" % (rep["covered"], rep["N"], rep["coverage"]),
          "- 总判定：**%s**" % ("PASS" if not rep["errors"] else "FAIL"), "",
          "## 一、逐行去向映射（原行号 → 合并后 item）", "",
          "| 原行号 | 原文（截断 60 字） | 去向 item | 状态 |", "|---|---|---|---|"]
    for ln in range(1, rep["N"] + 1):
        idxs = rep["mapping"].get(ln, [])
        raw = rep["lines"][ln - 1].strip().replace("|", "/")[:60]
        if not idxs:
            md.append("| L%d | %s | — | **遗漏** |" % (ln, raw))
        elif len(idxs) > 1:
            md.append("| L%d | %s | %s | **重复引用** |" % (ln, raw, idxs))
        else:
            md.append("| L%d | %s | item #%d | 已覆盖 |" % (ln, raw, idxs[0]))
    md += ["", "## 二、被压缩行清单（多个原始行合并为一个 item）", ""]
    if rep["compressed"]:
        md.append("| item | 合并的原始行号 |")
        md.append("|---|---|")
        for idx, nums in rep["compressed"]:
            md.append("| #%d | %s |" % (idx, nums))
    else:
        md.append("- 无（所有 item 均为单源行，或候选簇未合并）")
    md += ["", "## 三、数字／编号／单位指纹差集", "",
           "| 类别 | 基线种类 | 合并稿种类 | 缺失（零丢失） | 新增（只追加） |",
           "|---|---|---|---|---|"]
    for key in ("nums", "vals", "conf"):
        d = rep["fingerprint"].get(key, {})
        md.append("| %s | %s | %s | %s | %s |" %
                  (d.get("label", key), d.get("base", 0), d.get("out", 0),
                   d.get("miss", []) or "∅", d.get("add", []) or "∅"))
    md += ["| 裸数字 token（阈值/百分比/版本/配比/范围/系数） | %d | — | %s | %s |" %
           (len(set(NUM_TOKEN.findall(fingerprint.normalize("\n".join(rep["lines"]))))),
            rep["num_miss"] or "∅", rep["num_add"] or "∅"), "",
           "## 四、软校验告警", ""]
    md += ["- " + w for w in rep["warns"]] or ["- 无"]
    if rep["errors"]:
        md += ["", "## 五、硬校验失败明细（禁止渲染）", ""]
        md += ["- " + e for e in rep["errors"]]
    md.append("")
    return md


def map_lines(rep: dict) -> list[str]:
    md = ["# 第 3 步语义合并 · 逐行去向映射", "",
          "> 输入 = 落盘稿（筛选基准稿按终审剔除行删行后的按行保留稿），输出 = 合并稿；",
          "> 每个原始物理行号恰好被覆盖一次（全覆盖、无遗漏、无重复引用）。", "",
          "| 输入行 | 原文（截断 60 字） | 去向 item |", "|---|---|---|"]
    for ln in range(1, rep["N"] + 1):
        idxs = rep["mapping"].get(ln, [])
        raw = rep["lines"][ln - 1].strip().replace("|", "/")[:60]
        md.append("| L%d | %s | %s |" %
                  (ln, raw, ("item #%d" % idxs[0]) if idxs else "**遗漏**"))
    md += ["", "- 输入 %d 行 / 合并稿 %d 条 item / 覆盖率 %s" %
           (rep["N"], rep["items"], rep["coverage"]), "",
           "## 被压缩行（多行 → 一条）", ""]
    if rep["compressed"]:
        md.append("| item | 合并的原始行号 |")
        md.append("|---|---|")
        for idx, nums in rep["compressed"]:
            md.append("| #%d | %s |" % (idx, nums))
    else:
        md.append("- 无")
    md.append("")
    return md


# ---------------------------------------------------------------- 子命令
def cmd_prepare(a) -> None:
    lines = read_text(a.input).splitlines()
    if not lines:
        raise SystemExit("[错误] 落盘稿为空（0 行），拒绝 prepare")
    cand, clustered, unclustered = build_clusters(lines, a.similarity, a.max_cluster)
    os.makedirs(a.outdir, exist_ok=True)
    cand_path = os.path.join(a.outdir, CAND_NAME)
    prompt_path = os.path.join(a.outdir, PROMPT_NAME)
    result_path = a.result or os.path.join(a.outdir, RESULT_DEFAULT)
    data = {
        "input": os.path.abspath(a.input),
        "total_lines": len(lines),
        "similarity": a.similarity,
        "max_cluster": a.max_cluster,
        "cluster_count": len(cand),
        "clustered_lines": len(clustered),
        "unclustered_lines": len(unclustered),
        "unclustered": unclustered,
        "clusters": cand,
    }
    write_utf8(cand_path, json.dumps(data, ensure_ascii=False, indent=2))

    if cand:
        cl = []
        for c in cand:
            cl.append("- 簇 %d（相似度 %.4f，理由：%s）成员行号 %s：" %
                      (c["id"], c["similarity"], c["reason"], c["lines"]))
            for n, pv in zip(c["lines"], c["preview"]):
                cl.append("    - L%d | %s" % (n, pv))
    else:
        cl = ["- （无候选簇：落盘稿内没有达到相似度阈值的成对行；所有行各自独立 item）"]
    resume = ("python \"%s\" resume --workdir \"%s\"" %
              (os.path.join(HERE, "run_pipeline.py"), a.workdir)) if a.workdir else \
             "python run_pipeline.py resume --workdir \"<WORKDIR>\"（由主线程给出）"
    prompt = (AGENT_PROMPT
              .replace("@INPUT@", os.path.abspath(a.input))
              .replace("@CAND@", cand_path)
              .replace("@RESULT@", result_path)
              .replace("@CONF@", CONF_MARK)
              .replace("@AI@", AI_MARK)
              .replace("@N@", str(len(lines)))
              .replace("@CLUSTERS@", "\n".join(cl))
              .replace("@UNCLUSTERED@", ", ".join("L%d" % x for x in unclustered) or "（无）")
              .replace("@RESUME@", resume))
    write_utf8(prompt_path, prompt)

    print("=== 第 3 步语义合并 · prepare（机械候选簇，只产候选、不裁决）===")
    print("  落盘稿：%s（%d 物理行）" % (os.path.abspath(a.input), len(lines)))
    print("  候选簇数：%d" % len(cand))
    print("  成员行数：%d" % len(clustered))
    print("  未入簇行数：%d" % len(unclustered))
    print("  相似度阈值：%.2f | 单簇上限：%d" % (a.similarity, a.max_cluster))
    print("  候选簇：%s" % cand_path)
    print("  agent 提示词：%s" % prompt_path)
    print("  agent 结果：%s" % result_path)
    print("  下一步：主线程派 **1 个前台 agent**（强锁 1、并发 1、跑完再起下一个、不启动其他 agent），")
    print("          让它完整读完提示词、严格照做、只写自己的 合并结果.json、完成后只回一行确认。")
    print("          产出后由主线程跑：%s" % resume)


def _print_verify(rep: dict) -> None:
    print("=== 第 3 步语义合并 · 全覆盖 / 零丢失 硬校验 ===")
    print("  落盘稿物理行：%d | items：%d | 覆盖：%d/%d（%s）" %
          (rep["N"], rep["items"], rep["covered"], rep["N"], rep["coverage"]))
    for key in ("nums", "vals", "conf"):
        d = rep["fingerprint"].get(key, {})
        print("  [%s] 基线 %s 种 / 合并稿 %s 种 / 缺失 %s 种 / 新增 %s 种" %
              (d.get("label", key), d.get("base", 0), d.get("out", 0),
               len(d.get("miss", [])), len(d.get("add", []))))
    for w in rep["warns"]:
        print("  [警告] " + w)
    if rep["errors"]:
        for e in rep["errors"]:
            print("  [FAIL] " + e)
        print("=== 总结果：FAIL（非零退出，禁止渲染 / 禁止落盘）===")
    else:
        print("=== 总结果：PASS（全覆盖 + 数字指纹零丢失 + 只追加语义）===")


def cmd_verify(a) -> None:
    ok, rep = hard_check(a.input, a.result, a.fingerprint)
    _print_verify(rep)
    os.makedirs(a.outdir, exist_ok=True)
    write_utf8(os.path.join(a.outdir, COVER_NAME), "\n".join(report_lines(rep)) + "\n")
    print("  覆盖校验报告：%s" % os.path.join(a.outdir, COVER_NAME))
    if not ok:
        sys.exit(1)


def cmd_render(a) -> None:
    ok, rep = hard_check(a.input, a.result, a.fingerprint)
    if not ok:
        _print_verify(rep)
        print("[错误] 硬校验未通过：不写合并稿、不写行映射（失败就不写文件）。")
        sys.exit(1)
    # 硬校验通过才落盘：合并稿 = items 的 text 顺序渲染；严格只追加语义已由硬校验保证
    write_utf8(a.out, "\n".join(rep["texts"]) + "\n")
    write_utf8(a.map, "\n".join(map_lines(rep)) + "\n")
    print("=== 第 3 步语义合并 · render（硬校验通过后落盘）===")
    print("  落盘稿物理行：%d | items：%d | 覆盖：%s" %
          (rep["N"], rep["items"], rep["coverage"]))
    print("  合并稿：%s" % a.out)
    print("  逐行去向映射：%s" % a.map)
    print("=== 总结果：PASS ===")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="text-summary-filter 第 3 步「条目合并」语义合并机械工具（脚本不调模型、不裁决语义）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("prepare", help="机械生成语义相近/同类事物候选簇 + 完整 agent 提示词（只产候选、不裁决）")
    p.add_argument("--input", required=True, help="落盘稿（筛选基准稿按行保留后的稿）")
    p.add_argument("--outdir", required=True, help="产物目录（第 3 步 条目合并 步骤文件夹）")
    p.add_argument("--similarity", type=float, default=0.80,
                   help="候选簇相似度阈值（SequenceMatcher 与词元 Jaccard 取较大者，默认 0.80）")
    p.add_argument("--max-cluster", type=int, default=20, help="单簇最大成员数（默认 20）")
    p.add_argument("--result", default=None, help="agent 结果 JSON 路径（默认 <outdir>/合并结果.json）")
    p.add_argument("--workdir", default=None, help="工作目录（用于生成 resume 命令；不给则写通用说明）")
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("verify", help="全覆盖/行号/reason/数字指纹零丢失 硬校验 + 逐行去向映射报告")
    p.add_argument("--input", required=True, help="落盘稿（原始物理行号基准）")
    p.add_argument("--result", required=True, help="agent 产出的 合并结果.json")
    p.add_argument("--fingerprint", required=True, help="硬数据指纹基线 JSON")
    p.add_argument("--outdir", required=True, help="产物目录（输出 合并_覆盖校验.md）")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("render", help="先跑与 verify 相同的硬校验，再渲染合并稿 + 逐行去向映射")
    p.add_argument("--input", required=True, help="落盘稿（原始物理行号基准）")
    p.add_argument("--result", required=True, help="agent 产出的 合并结果.json")
    p.add_argument("--fingerprint", required=True, help="硬数据指纹基线 JSON")
    p.add_argument("--out", required=True, help="合并稿输出路径")
    p.add_argument("--map", required=True, help="逐行去向映射输出路径")
    p.set_defaults(func=cmd_render)
    return ap


def main() -> None:
    a = build_parser().parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
