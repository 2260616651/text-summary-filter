# -*- coding: utf-8 -*-
# Copyright (c) 2026 text-summary-filter contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""意图路由：把用户「一句话」映射成 run_pipeline.py init 的建议参数。

只做确定性关键词路由，不做语义猜测；命中多条时按优先级取最具体的一条，
并把「未命中/冲突」明确报出来，交主线程在开场询问包里确认（不擅自拍板）。

用法：
    python route.py --text "把这份文档合并压缩并筛选" --source "<源.txt>" --workdir "<目录>"
    python route.py --text "照旧" --source ... --workdir ... --json
输出：一行可直接复制的 init 命令 + 理由；--json 时额外输出结构化结果。
"""
from __future__ import annotations

import argparse
import json

# (优先级, 模式名, 关键词组, init 关键参数, 说明)
# 关键词组：任一命中即可；越具体的模式排越前
RULES = [
    (10, "完整总结链",
     ["多份源", "多篇", "多个文档", "先拼接", "机械拼接", "从头总结", "从原始稿"],
     ["--mode 完整总结链"],
     "输入是多份源：先 merge_and_chunk.py --inputs 拼成母本，再 init；不在 init 参数里体现", []),
    (15, "仅内容筛选",
     ["不用合并", "不合并", "别合并", "不要合并", "合并就免了", "合并免了",
      "只要筛选", "只挑重点", "筛一下", "筛一下就好"],
     ["--skip-merge", "--mode 仅筛选"],
     "否定合并+筛选：明确「不/别/不用合并」或「只要筛选」时按仅内容筛选；本规则优先于 prio20 的「合并/压缩」排除词承接，肯定式「合并压缩并筛选」仍由 prio40 全流程承接",
     ["合并压缩并", "合并并筛", "合并+筛", "合并筛选", "并筛", "再筛", "和筛", "+筛",
      "别筛选", "不筛选", "不用筛选", "免筛选", "不要筛选", "筛选就免了"]),
    (20, "仅内容筛选",
     ["只筛选", "仅筛选", "内容筛选", "剔除无关", "去掉无关", "拎出无关", "去掉杂质", "筛一遍"],
     ["--skip-merge", "--mode 仅筛选"],
     "源文档已是合并稿，跳过 S2 行选择合并", ["合并", "压缩"]),
    (25, "仅条目合并",
     ["别筛选", "不筛选", "不用筛选", "免筛选", "不要筛选", "筛选就免了"],
     ["--screening off", "--mode 仅合并"],
     "否定式只合并：明确「别/不/不用/免筛选」时按仅条目合并；肯定式「合并压缩并筛选」仍由 prio40 全流程承接", []),
    (30, "仅条目合并",
     ["只合并", "仅合并", "合并压缩", "条目合并", "压缩一下", "压一压"],
     ["--screening off", "--mode 仅合并"],
     "只做合并与零丢失校验；不分卷、不进三道筛选（三道筛选需另行触发）",
     ["并筛", "再筛", "筛选", "+筛", "和筛"]),
    (40, "合并+筛选全流程",
     ["合并压缩并筛选", "合并并筛选", "合并+筛选", "合并筛选", "全流程", "一条龙",
      "跑一遍", "跑完", "照旧", "按上次", "按上次的流程", "一句话跑完", "默认",
      "你看着办", "继续", "开始吧", "直接跑", "全跑"],
     ["--mode 合并+筛选", "--volumes 7"],
     "默认全流程：S0–S9 全跑", []),
]

# 主题词：用于提示 --subject 该填什么（不自动填，交开场询问包确认）
SUBJECT_HINTS = [
    ("示例项目", "示例整合包"),
    ("试玩", "《试玩》整合包"),
    ("设计文档", "设计文档"),
    ("聊天记录", "聊天记录"),
]


def route(text: str, source: str, workdir: str, volumes: int | None = None,
          skip_merge: bool = False) -> dict:
    t = (text or "").strip()
    matched = []
    for prio, mode, kws, args, note, excludes in RULES:
        hits = [k for k in kws if k in t]
        if not hits:
            continue
        # 排除词：例如「合并压缩并筛选」不应被「仅条目合并」抢走
        if any(x in t for x in excludes):
            continue
        matched.append((prio, mode, hits, args, note))
    if not matched:
        picked = (40, "合并+筛选全流程", [], ["--mode 合并+筛选", "--volumes 7"],
                  "未命中任何关键词，按默认全流程（建议开场询问包 Q1 确认）")
        ambiguous = False
    else:
        matched.sort(key=lambda x: x[0])
        picked = matched[0]
        ambiguous = len(matched) > 1

    prio, mode, hits, args, note = picked
    cmd = ['python scripts/run_pipeline.py init',
           '--source "%s"' % source,
           '--workdir "%s"' % workdir]
    cmd += [a for a in args if not (volumes is not None and a.startswith("--volumes"))]
    if volumes is not None:
        cmd.append("--volumes %d" % volumes)
    if skip_merge:
        cmd.append("--skip-merge")

    subject = ""
    for kw, val in SUBJECT_HINTS:
        if kw in t:
            subject = val
            break
    if subject:
        cmd.append('--subject "%s"' % subject)

    return {
        "text": t,
        "mode": mode,
        "priority": prio,
        "hits": hits,
        "ambiguous": ambiguous,
        "alternatives": [{"mode": m[1], "hits": m[2]} for m in matched[1:]] if ambiguous else [],
        "note": note,
        "subject_hint": subject,
        "init_command": " ".join(cmd),
        "ask_user": ambiguous or prio == 40 and not hits,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="text-summary-filter 一句话意图路由")
    ap.add_argument("--text", required=True, help="用户原话")
    ap.add_argument("--source", default="<SOURCE>", help="源文档路径")
    ap.add_argument("--workdir", default="<WORKDIR>", help="工作目录")
    ap.add_argument("--volumes", type=int, default=None, help="覆盖分卷数")
    ap.add_argument("--skip-merge", action="store_true", help="强制跳过合并")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    a = ap.parse_args()

    r = route(a.text, a.source, a.workdir, a.volumes, a.skip_merge)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return
    print("=== 意图路由 ===")
    print("  用户原话 : %s" % r["text"])
    print("  判定模式 : %s（命中：%s）" % (r["mode"], ", ".join(r["hits"]) or "无，取默认"))
    print("  说明     : %s" % r["note"])
    if r["ambiguous"]:
        print("  ⚠ 多模式命中，其它候选：%s —— 请在开场询问包 Q1 与用户确认" %
              ", ".join(x["mode"] for x in r["alternatives"]))
    if r["subject_hint"]:
        print("  判定对象提示：--subject \"%s\"（请开场询问包确认）" % r["subject_hint"])
    print("  建议命令 :")
    print("    " + r["init_command"])
    if r["ask_user"]:
        print("  提示：本次判定不确定，开场询问包 Q1 请务必让用户确认模式（不要替用户拍板）。")


if __name__ == "__main__":
    main()
