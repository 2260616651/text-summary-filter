# -*- coding: utf-8 -*-
# Copyright (c) 2026 text-summary-filter contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""完整总结链前置工具：把逐块摘要「只追加」成单一总提取稿，并校验块覆盖。

背景：run_pipeline.py 处理的是「已有一份汇总文档」；若输入是**多份源文档**，
需先 merge_and_chunk.py --inputs 机械拼接 + 切块，再逐块总结（一块一 Agent、只追加），
最后把总提取稿作为 run_pipeline.py 的 --source。本脚本负责后半段的**落盘与校验**：

  - `append`   ：把某个块的摘要追加进总提取稿（带 # === 来自 chunk_NNN === 硬边界），
                 严格 append 模式，绝不覆盖/重写已有内容，防重复写入；
                 `--all` 批量模式一次扫全目录 summary_NNN.md 按块号升序逐个追加。
  - `check`    ：核对哪些块已落盘、块号连续无遗漏、无重复，打印下一步该总结哪一块。
  - `template` ：生成某个块的「逐块总结子 Agent 提示词」，路径与块号自动注入。

逐块总结本身由主线程派**串行前台** agent 完成（本脚本不调用模型，避免上下文回流）。
用法：
    python build_extract.py check    --chunks <chunks目录> --extract <总提取稿.md>
    python build_extract.py template --chunks <chunks目录> --extract <总提取稿.md> --index 1
    python build_extract.py append   --chunks <chunks目录> --extract <总提取稿.md> --index 1 --summary <块摘要.md>
    python build_extract.py append   --chunks <chunks目录> --extract <总提取稿.md> --all   # 批量：扫全部 summary_NNN.md 升序追加
    python build_extract.py check    --chunks <chunks目录> --extract <总提取稿.md> --finalize
"""
from __future__ import annotations

import argparse
import json
import os
import re

MARKER = "# === 来自 {name} ==="
DONE_MARK = "<!-- BLOCK-DONE: {idx} -->"

AGENT_TEMPLATE = """你是「文档总结子 Agent（上下文隔离）」。严格串行前台工作，不要启动其他 agent。

## 必读
1. **只读这一块**：`{chunk_path}`
2. 本块自带的 `⟦OVERLAP 来自块N尾部·重复区⟧ … ⟦OVERLAP 结束⟧` 属本块内容，正常阅读。

## 铁律（硬性）
- 高保真保留：**关键设定 / 系统 / 清单 / 数字参数与阈值 / 决策与结论 / 风险点 / 流程——数字一个不许漏**。
  参数、阈值、百分比、坐标、编号、公式系数、版本号、配比、范围全部保留；丢一个设定就失真。
- 剔除寒暄套话、客套填充、与目标无关的闲聊；但**不删任何原文陈述的信息**，可适当压缩表达。
- 按主题组织，一条一行、宁滥勿漏；每条设计点单独 `- ` 起始。
- **只追加、不删改、不去重、不裁决冲突**：本块摘要产出后直接交付，过程中不处理重复与冲突。
- `【已废弃】` / `【已废弃·疑似】` 放**行首**全角括号，禁内联「废弃/否决」；
  同字段多值**全部保留**并标 `【冲突：源稿矛盾】`，绝不取一丢一；
  同源 `[来源：原始稿文件名]` 只标一次，**绝不填 `chunk_xxx.txt`**。
- 文档外推论显式标「（AI 推论）」，不与原文混写。

## 输出
把本块摘要写到：`{summary_path}`（UTF-8 Markdown）。
**严禁读写共享的总提取稿** `{extract_path}`（由 build_extract.py append 统一追加，防写冲突）。

## 完成判据
必须处理完本块**全部内容**，不得中途停止；打印「输入字节数 / 输出字节数 / 条数」三项统计。
完成后**只回一行确认**：`done {idx} <条数>`，不回正文、不粘贴块内容给主线程。
"""


def list_chunks(chunks_dir: str) -> list[str]:
    fs = [f for f in os.listdir(chunks_dir)
          if re.fullmatch(r"chunk_\d+\.txt", f)]
    return sorted(fs, key=lambda x: int(re.search(r"(\d+)", x).group(1)))


def read_done(extract: str) -> set[int]:
    if not os.path.isfile(extract):
        return set()
    txt = open(extract, encoding="utf-8").read()
    return set(int(x) for x in re.findall(r"<!-- BLOCK-DONE: (\d+) -->", txt))


def index_of(name: str) -> int:
    return int(re.search(r"(\d+)", name).group(1))


def cmd_check(a) -> None:
    chunks = list_chunks(a.chunks)
    if not chunks:
        raise SystemExit("[错误] 未找到 chunk_NNN.txt：%s" % a.chunks)
    done = read_done(a.extract)
    expect = set(index_of(f) for f in chunks)
    missing = sorted(expect - done)
    extra = sorted(done - expect)
    print("=== 逐块摘要覆盖检查 ===")
    print("  块文件 %d 个（%s … %s）" %
          (len(chunks), chunks[0], chunks[-1]))
    print("  已落盘 %d / %d 块" % (len(done & expect), len(expect)))
    if extra:
        print("  [警告] 多余块号（不在切分清单里）：%s" % extra)
    if missing:
        print("  待总结块号：%s" % missing[:20])
        nxt = missing[0]
        print("\n下一步：派第 %d 块（串行前台），提示词可用：" % nxt)
        print("  python build_extract.py template --chunks \"%s\" --extract \"%s\" --index %d" %
              (a.chunks, a.extract, nxt))
    else:
        print("  [PASS] 全部块已落盘、块号连续无遗漏")
        if a.finalize:
            size = os.path.getsize(a.extract) if os.path.isfile(a.extract) else 0
            print("\n  总提取稿：`%s`（%d B / %d 行）" %
                  (a.extract, size,
                   open(a.extract, encoding="utf-8").read().count("\n") + 1))
            print("  下一步：把它作为 --source 进入 run_pipeline.py：")
            print("    python scripts/route.py --text \"合并压缩并筛选\" --source \"%s\" --workdir <目录>" %
                  a.extract)
        else:
            print("  加 --finalize 可输出总提取稿统计与入链命令。")
    raise SystemExit(0 if not missing and not extra else (0 if missing else 1))


def cmd_template(a) -> None:
    chunks = list_chunks(a.chunks)
    by_idx = {index_of(f): f for f in chunks}
    if a.index not in by_idx:
        raise SystemExit("[错误] 块号 %d 不存在（可用：%s）" %
                         (a.index, sorted(by_idx)[:10]))
    name = by_idx[a.index]
    txt = AGENT_TEMPLATE.format(
        chunk_path=os.path.join(a.chunks, name),
        summary_path=os.path.join(a.chunks, "summary_%03d.md" % a.index),
        extract_path=a.extract,
        idx=a.index,
    )
    out = a.out or os.path.join(a.chunks, "prompt_%03d.md" % a.index)
    with open(out, "w", encoding="utf-8") as fp:
        fp.write(txt)
    print("已生成第 %d 块提示词：%s" % (a.index, out))
    print("  摘要输出路径：%s" % os.path.join(a.chunks, "summary_%03d.md" % a.index))


def append_block(extract: str, index: int, summary_path: str,
                 force: bool) -> tuple[str, str]:
    """执行单块追加，保持既有语义：严格追加('a')、绝不覆盖、块号去重、带 BLOCK-DONE 标记。

    返回 (status, msg)：status='ok' 已追加；'skip' 该块已追加过（防重复写入保护）。
    检查顺序与旧版 cmd_append 一致（先摘要存在性、再块号去重），保证单块模式行为不变。
    """
    if not os.path.isfile(summary_path):
        raise SystemExit("[错误] 块摘要不存在：%s" % summary_path)
    done = read_done(extract)
    if index in done and not force:
        return "skip", "第 %d 块已追加过（防重复写入保护），跳过。确需覆盖请加 --force。" % index
    summary = open(summary_path, encoding="utf-8").read().rstrip()
    if not summary:
        raise SystemExit("[错误] 块摘要为空，拒绝追加。")
    block = (MARKER.format(name="chunk_%03d.txt" % index) + "\n"
             + DONE_MARK.format(idx=index) + "\n\n"
             + summary + "\n\n")
    # 严格 append：绝不覆盖已有内容；用 'a' 而非 'w'
    with open(extract, "a", encoding="utf-8") as fp:
        fp.write(block)
    done.add(index)
    return "ok", "已追加第 %d 块（%d B）。总提取稿现有 %d 块 / %d B。" % (
        index, len(block.encode("utf-8")), len(done), os.path.getsize(extract))


def cmd_append(a) -> None:
    # 互斥与必填校验：--all 自动扫描，不接受单块参数；非 --all 仍需 --index + --summary。
    if a.all and (a.index is not None or a.summary is not None):
        raise SystemExit("[错误] --all 与 --index/--summary 互斥：--all 会自动扫描全部 "
                         "summary_NNN.md，不要再指定单块参数。")
    if not a.all and (a.index is None or a.summary is None):
        raise SystemExit("[错误] 非 --all 模式必须同时提供 --index 与 --summary；"
                         "或用 --all 批量追加全部块。")
    if a.all:
        summaries = [f for f in os.listdir(a.chunks)
                     if re.fullmatch(r"summary_\d+\.md", f)]
        if not summaries:
            raise SystemExit("[错误] 未在 %s 找到 summary_NNN.md（--all 无可追加块）。" % a.chunks)
        summaries.sort(key=lambda x: int(re.search(r"(\d+)", x).group(1)))
        ok = skipped = 0
        for f in summaries:
            idx = index_of(f)
            status, msg = append_block(a.extract, idx, os.path.join(a.chunks, f), a.force)
            print(msg)
            if status == "ok":
                ok += 1
            else:
                skipped += 1
        size = os.path.getsize(a.extract) if os.path.isfile(a.extract) else 0
        print("=== --all 汇总：找到 %d 个摘要，成功追加 %d 块，跳过 %d 块，总提取稿 %d B ===" %
              (len(summaries), ok, skipped, size))
        return
    # 单块模式：行为与旧版完全一致（已追加过直接报错退出，而非跳过）
    status, msg = append_block(a.extract, a.index, a.summary, a.force)
    if status == "skip":
        raise SystemExit("[错误] 第 %d 块已追加过（防重复写入保护）。确需覆盖请加 --force。" % a.index)
    print(msg)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="完整总结链前置工具：逐块摘要只追加 + 块覆盖校验 + 子 Agent 提示词生成")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("check", "template", "append"):
        p = sub.add_parser(name)
        p.add_argument("--chunks", required=True, help="切块目录（含 chunk_NNN.txt）")
        p.add_argument("--extract", required=True, help="总提取稿路径（单一文档，append 模式写）")
        if name == "check":
            p.add_argument("--finalize", action="store_true", help="全部完成时输出入链命令")
        if name == "template":
            p.add_argument("--index", type=int, required=True, help="块号")
            p.add_argument("--out", default=None, help="提示词输出路径")
        if name == "append":
            p.add_argument("--all", action="store_true",
                           help="批量模式：扫描 --chunks 下全部 summary_NNN.md，按块号升序逐个追加")
            p.add_argument("--index", type=int, default=None,
                           help="块号（单块模式必填，与 --all 互斥）")
            p.add_argument("--summary", default=None,
                           help="该块的摘要文件（单块模式必填，与 --all 互斥）")
            p.add_argument("--force", action="store_true", help="允许覆盖已追加的块（慎用）")
    a = ap.parse_args()
    {"check": cmd_check, "template": cmd_template, "append": cmd_append}[a.cmd](a)


if __name__ == "__main__":
    main()
