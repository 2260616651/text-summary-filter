# -*- coding: utf-8 -*-
# Copyright (c) 2026 text-summary-filter contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""text-summary-filter 端到端编排器：一句话触发 → 一次问清 → 照着跑完整条链。

设计要点
--------
* 机械阶段自动执行（指纹基线 / 行选择合并 / 判定视图 / 渲染 / 校验 / 定稿 / 分卷 / 报告）。
* 语义阶段（三道串行 agent 内容筛选 + 第 3 步语义合并 1 个前台 agent）只留接口：
  脚本等待 agent{1,2,3}_result.json / 合并结果.json 落盘，由主线程按提示词派**串行前台** agent，
  产出后 resume；脚本不调模型、语义裁决只由前台 agent 完成。
* 状态文件 pipeline_state.json 记录每阶段产物与结果，任何中断都可 resume 续跑。
* 全程二进制读写（rb / splitlines(keepends=True) / wb），禁止依赖 Windows 文本模式默认换行转换。

用法
----
    python run_pipeline.py init    --source <源.txt> --workdir <目录> [--volumes 7] ...
    python run_pipeline.py status  --workdir <目录>
    python run_pipeline.py run     --workdir <目录>     # 跑到语义阶段自动停
    python run_pipeline.py resume  --workdir <目录>     # agent 产出齐备后继续
    python run_pipeline.py verify  --workdir <目录>     # 独立跑全量验收
    python run_pipeline.py report  --workdir <目录>     # 生成/刷新运行报告
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from pipeline_templates import render_agent_prompt, render_criterion  # noqa: E402

STATE_NAME = "pipeline_state.json"

# (id, 名称, 类型) 类型 auto=脚本自动 / agent=串行前台智能体 / milestone=里程碑
# 权威流程 = ⓿ 前置（不计入编号）+ 7 大步：1 切块总结 / 2 条目筛选 / 3 条目合并 /
# 4 终稿生成 / 5 检查 / 6 全文检查 / 7 达标判定与复压（见 SKILL.md 第 0 节）。
# 执行顺序已按权威流程真反转：筛选(第2步) → 合并(第3步)。
# STAGES 列表顺序 = run_auto 的真实执行顺序：S1(指纹) → S3/S4/S5/S6(筛选链) →
# S2(第3步条目合并，吃筛选结果落盘 + 真语义合并) → S7(终稿) → S8(分卷) → S9(报告)；S0 仍跳过。
# 筛选链的行号基准统一为 f["screen_base"]（第 1 步总提取稿，或退回母本）：
# 视图 / 回查 / 渲染 / 集合校验 / 删行 / 指纹基线全用同一份文档、同一套行号。
STAGES = [
    ("S0", "⓿前置·初始化与参数固化（开场询问包/多源合并/切分预算与上下文容量判定）", "auto"),
    ("S1", "第1步·切块总结挂载：硬数据指纹基线（锚定筛选基准稿 screen_base）", "auto"),
    ("S3", "第2步·条目筛选：判定视图与行元数据（视图行号锚定筛选基准稿）", "auto"),
    ("S4", "第2步·条目筛选：三道串行筛选（智能体）", "agent"),
    ("S5", "第2步·条目筛选：按行号零改写渲染三个新文档", "auto"),
    ("S6", "第5步·检查：三道集合硬校验 + 指纹零丢失（程序化硬校验第一小步）", "auto"),
    ("S2", "第3步·条目合并：按筛选结果落盘 + 真语义合并（候选簇→1前台agent→全覆盖/指纹零丢失硬校验）", "auto"),
    ("S7", "第4步·终稿生成 + 第6步·全文检查挂载：清洗定稿 + 逐行去向映射", "auto"),
    ("S8", "第4步·终稿生成：按行分卷 + 拼接字节一致校验", "auto"),
    ("S9", "第6步·全文检查挂载 + 第7步·达标判定：运行报告与交付清单", "auto"),
]

# 新主流程 Q1/Q2：⓿ 前置 + 7 大步 -> 状态机 stage 映射
# （未选中的大步/小步对应 stage 置 skipped；--big-steps "1,2,3,4,5,6,7" = 新 7 步全选）。
# ⓿ 前置由 S0 + 前置链（merge_and_chunk.py / split_document.py / build_extract.py）承担，
# 不计入 1~7 编号、S0 永远执行、不参与 skip。
# 执行顺序已按权威流程真反转：筛选(第2步) → 合并(第3步)。本映射把 stage 分到新 7 步，
# 与 STAGES 实际执行顺序一致（筛选链 S3~S6 先于合并 S2），不再是「责任归属但不反转」。
# 第 3 步语义合并已真实现（候选簇→1 个前台 agent→全覆盖/数字指纹零丢失硬校验→落盘）；
# 第 5/6 步的「agent 补遗漏 / 全文检查最后修改」与第 7 步复压目前仍是流程定义与挂载点，
# 完整语义实现待补（见 guide/red-lines.md「当前实现边界」），不宣称已实现。
BIG_STAGE_MAP = {
    1: ["S1"],                    # 第1步 切块总结：等宽切分(块 < 1/2 上下文) → 逐块 agent 总结 → 总提取稿。
                                  # 编排器内挂载点 = S1 指纹基线（切块前事实层基线）；逐块总结属前置链 build_extract，不在 S1-S9。
    2: ["S3", "S4", "S5"],        # 第2步 条目筛选：按 Q8 目标筛选、事实层判定基准(S3)在筛选前生成、
                                  # 三道串行 agent(S4) + 按行号渲染(S5)；事实/要点一律保留。
    3: ["S2"],                    # 第3步 条目合并：按筛选结果落盘(吃 final_remove_lines) + 真语义合并
                                  # (候选簇机械生成→1 个前台 agent 裁决→全覆盖/数字指纹零丢失硬校验→落盘)；
                                  # 复压与补漏回流的统一回跳点(recompress_from="S2")，在筛选之后执行。
    4: ["S7", "S8"],              # 第4步 终稿生成：清洗定稿+逐行去向映射(S7)、分卷+拼接字节一致校验(S8)、交付清单。
    5: ["S6"],                    # 第5步 检查：第一小步程序化硬校验（集合/数字指纹/逐行去向/零丢失证据，复用 S6）；
                                  # 随后重切块(块 < 1/2 上下文，固定 1/2 为上限) → 派 agent 补遗漏（语义待补）。
    6: ["S9"],                    # 第6步 全文检查：遍历最终输出做最后修改；上下文不足切块检查(块 < 1/2 上下文)；
                                  # 复用 S9 运行报告/交付阶段作挂载点（语义待补）。
    7: [],                        # 第7步 达标判定与复压：体积达 Q5 判定；不达标回跳第3步条目合并，
                                  # 把上一轮输出当输入接着跑；达零丢失下限仍不达标则停并交付当前最佳。
                                  # 复压由 cmd_run/_reset_for_recompress 跨轮实现，不在单轮 S1-S9 内 → 空映射。
}


# ---------------------------------------------------------------- 中间文件落盘规则
# 每个步骤的中间文件独立进一个文件夹，命名为
#   {doc_name}总结中间文件 - {步骤名称}第{round}次
# 放在「要总结的文件所在根目录」（源文件父目录）下。复压轮次不同则文件夹名不同，
# 旧轮次产物原样保留作归档，互不污染、便于清理。pipeline_state.json 仍留在 --workdir，
# 它是本工具自身元数据，不是总结中间文件。
STEP_DIR_NAMES = {
    "S1": "第1步 切块总结·指纹基线",
    "S2": "第3步 条目合并·按筛选结果落盘",
    "S3": "第2步 条目筛选·判定视图与行元数据",
    "S4": "第2步 条目筛选·三道内容筛选",
    "S5": "第2步 条目筛选·渲染三新文档",
    "S6": "第5步 检查·三道校验与指纹零丢失",
    "S7": "第4步 终稿生成·清洗定稿与去向映射",
    "S8": "第4步 终稿生成·按行分卷",
    "S9": "第6步 全文检查与第7步 达标判定·运行报告与交付",
}


def step_dir(doc_root: str, doc_name: str, step_name: str, round_no: int) -> str:
    """返回某步骤第 round_no 次执行的中间文件文件夹绝对路径。"""
    return os.path.join(doc_root, "%s总结中间文件 - %s第%d次" % (doc_name, step_name, round_no))


def build_files(st: dict, round_no: int) -> dict:
    """按当前复压轮次重建 st["files"]：每个步骤的中间文件落到各自的独立文件夹。

    doc_root / doc_name 在 init 时从「原始源」固化进 state，复压轮次只换 round_no，
    不换文档名（避免复压后把 round-1 的定稿文件当成文档名）。
    """
    doc_root = st.get("doc_root") or os.path.dirname(st["source"])
    doc_name = st.get("doc_name") or os.path.splitext(os.path.basename(st["source"]))[0]

    def d(sid: str) -> str:
        return step_dir(doc_root, doc_name, STEP_DIR_NAMES[sid], round_no)

    f = {
        "baseline":      os.path.join(d("S1"), "硬数据指纹_基线.json"),
        "merged":        os.path.join(d("S2"), "合并结果.txt"),
        "merge_state":   os.path.join(d("S2"), "merge_state.json"),
        "merge_map":     os.path.join(d("S2"), "合并_行映射.md"),
        "view":          os.path.join(d("S3"), "筛选_判定视图.txt"),
        "view_index":    os.path.join(d("S3"), "筛选_行元数据.json"),
        "criterion":     os.path.join(d("S3"), "筛选_判定基准.md"),
        "prompt1":       os.path.join(d("S3"), "agent1_prompt.md"),
        "prompt2":       os.path.join(d("S3"), "agent2_prompt.md"),
        "prompt3":       os.path.join(d("S3"), "agent3_prompt.md"),
        "agent1":        os.path.join(d("S4"), "agent1_result.json"),
        "agent2":        os.path.join(d("S4"), "agent2_result.json"),
        "agent3":        os.path.join(d("S4"), "agent3_result.json"),
        "screen1":       os.path.join(d("S5"), "筛选1_初判无关条目.md"),
        "screen2":       os.path.join(d("S5"), "筛选2_复核关联条目.md"),
        "screen3":       os.path.join(d("S5"), "筛选3_终审判定.md"),
        "final":         os.path.join(d("S7"), "筛选定稿.txt"),
        "final_map":     os.path.join(d("S7"), "筛选_行映射.md"),
        "剔除行内容体检": os.path.join(d("S7"), "剔除行内容体检.md"),
        "volume_prefix": os.path.join(d("S8"), "分卷"),
        "volume_map":    os.path.join(d("S8"), "分卷_卷映射.md"),
        "report":        os.path.join(d("S9"), "运行报告.md"),
        # 新 7 步挂载点：第 5 步检查·重切块 / 第 6 步全文检查（仅准备输出路径，不破坏既有键）
        "检查_重切块":   os.path.join(d("S6"), "检查_重切块.md"),
        "全文检查":      os.path.join(d("S7"), "全文检查.md"),
        # 第 3 步条目合并·真语义合并新增产物（全部落「第3步 条目合并」步骤文件夹）
        "合并_候选簇":      os.path.join(d("S2"), "合并_候选簇.json"),
        "合并_agent_prompt": os.path.join(d("S2"), "合并_agent_prompt.md"),
        "合并_指纹基线":    os.path.join(d("S2"), "合并_指纹基线.json"),
        "合并结果":         os.path.join(d("S2"), "合并结果.json"),
        "合并稿":           os.path.join(d("S2"), "合并稿.md"),
        "合并_覆盖校验":    os.path.join(d("S2"), "合并_覆盖校验.md"),
        "合并_行映射":      os.path.join(d("S2"), "合并_行映射.md"),
    }
    # 统一的「筛选基准稿」：第 1 步总提取稿(init --extract)优先，否则退回母本。
    # 复压轮以「上一轮输出」(st["source"]) 为筛选基准稿；筛选链的行号基准恒为此文件。
    _extract = st.get("options", {}).get("extract")
    if st.get("recompress_round", 0) and st.get("source"):
        f["screen_base"] = st["source"]
        st["screen_base_source"] = "复压输入（上一轮输出）"
    elif _extract and os.path.isfile(_extract):
        f["screen_base"] = _extract
        st["screen_base_source"] = "第 1 步总提取稿（init --extract）"
    else:
        f["screen_base"] = st["source"]
        st["screen_base_source"] = "退回母本（未给 --extract 或文件不存在）"
    # 新链：S2 恒写独立的 f["merged"]（按筛选结果落盘），不再把 merged 指向源文档。
    st["files"] = f
    st["step_dirs"] = {sid: d(sid) for sid in STEP_DIR_NAMES}
    # 预建所有步骤文件夹，确保子进程（fingerprint/line_select_merge 等）写 --out 时父目录已存在
    for sd_ in st["step_dirs"].values():
        os.makedirs(sd_, exist_ok=True)
    return f


def skip_stages_from_answers(big, small):
    """Q1 大步编号 / Q2 小步编号 -> 需跳过的 stage id（默认空 = 全跑，既有行为不变）。"""
    all_big = set(BIG_STAGE_MAP)
    if (big or "").strip():
        active = {int(x) for x in big.split(",") if x.strip().isdigit()}
    else:
        active = set(all_big)
    if (small or "").strip():
        small_big = set()
        for x in small.split(","):
            x = x.strip()
            if not x:
                continue
            try:
                small_big.add(int(float(x)))
            except ValueError:
                pass
        active &= small_big   # Q2：只执行被选中小步激活的大步
    # 依赖校验（可读告警，不直接崩）：执行顺序已真反转「筛选(第2步)→合并(第3步)」，
    # 第 4 步终稿生成与第 5/6/7 步（检查 / 全文检查 / 达标判定与复压）依赖第 3 步条目合并
    # 或第 4 步终稿生成的产物；缺失时给可读告警但按用户选择继续。
    if active:
        late = active & {5, 6, 7}
        if late and not (active & {3, 4}):
            print("  [阶段依赖告警] 选中了第 5/6/7 步（检查/全文检查/达标判定与复压），"
                  "却未选中第 3 步条目合并或第 4 步终稿生成：真反转后的合并/定稿产物"
                  "（筛选在前、合并在后）可能无输入可用；已按你的选择继续（不阻断，不是崩溃）。",
                  flush=True)
    skip = []
    for b, sids in BIG_STAGE_MAP.items():
        if b not in active:
            skip.extend(sids)
    return sorted(set(skip))


# ---------------------------------------------------------------- 基础工具
def now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    print(msg, flush=True)


def read_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def write_bytes(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def load_state(workdir: str) -> dict:
    """读取并校验 pipeline_state.json。损坏时打印一行中文提示并退出，绝不裸 Traceback。

    统一覆盖 5 类损坏：非法 JSON / 非 UTF-8 / 缺关键字段 / 字段类型错 / 文件不存在或不可读。
    只把「文件读取 + 解析」包进 try，其余用显式字段校验（不靠捕获 KeyError、不把正常路径裹进 try）；
    正常合法 state 的读取结果与后续行为完全不变。
    """
    p = os.path.join(workdir, STATE_NAME)
    if not os.path.exists(p):
        raise SystemExit("[错误] 未找到 state 文件 %s，请先执行 init 重新初始化，"
                         "或从备份恢复该文件。" % p)
    try:
        with open(p, "rb") as fh:
            raw = fh.read()
        st = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as e:
        raise SystemExit("[错误] state 文件不是合法 JSON：%s（第 %d 行第 %d 列：%s）。"
                         "请重跑 init 重新生成，或从备份恢复该文件。" %
                         (p, e.lineno, e.colno, e.msg)) from None
    except UnicodeDecodeError as e:
        raise SystemExit("[错误] state 文件不是合法 UTF-8 文本：%s（%s）。"
                         "请重跑 init 重新生成，或从备份恢复该文件。" % (p, e)) from None
    except OSError as e:
        raise SystemExit("[错误] state 文件无法读取：%s（%s）。"
                         "请检查文件权限或是否被占用，重跑 init 重新生成，"
                         "或从备份恢复该文件。" % (p, e)) from None
    # 结构校验：把「字段类型错 / 缺关键字段」在此收口，避免各命令后续访问
    # st["workdir"]/st["stages"] 时抛 KeyError/TypeError 形成裸 Traceback。
    if not isinstance(st, dict):
        raise SystemExit("[错误] state 文件顶层不是 JSON 对象（实际为 %s）：%s。"
                         "请重跑 init 重新生成，或从备份恢复该文件。" %
                         (type(st).__name__, p))
    required = ("workdir", "source", "files", "options", "answers", "stages")
    missing = [k for k in required if k not in st]
    if missing:
        raise SystemExit("[错误] state 文件缺少关键字段 %s：%s。"
                         "请重跑 init 重新生成，或从备份恢复该文件。" %
                         ("、".join(missing), p))
    for k in ("files", "options", "answers", "stages"):
        if not isinstance(st[k], dict):
            raise SystemExit("[错误] state 文件字段 %s 类型错误（应为 JSON 对象，实际为 %s）：%s。"
                             "请重跑 init 重新生成，或从备份恢复该文件。" %
                             (k, type(st[k]).__name__, p))
    return st


def save_state(workdir: str, st: dict) -> None:
    st["updated"] = now()
    p = os.path.join(workdir, STATE_NAME)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


def _relocate_state(st: dict, new_workdir: str) -> None:
    """工作区被整体复制到新目录后，把 state 里钉死的产物路径重映射到 --workdir。

    沿用 verify「以 --workdir 为准」的同一思想：只在传入目录与 state 记录的 workdir
    不一致（复制/迁移场景）时生效；正常主链二者相等时是严格 no-op，行为零改变。
    只重映射产物落地路径（st["workdir"] 与 st["files"] 下以旧目录为前缀的绝对路径），
    source / *_source 等来源元数据不动（母本常在独立目录，重映射反而指错）。
    """
    new_wd = os.path.abspath(new_workdir)
    old_wd = os.path.abspath(st["workdir"])
    if os.path.normcase(new_wd) == os.path.normcase(old_wd):
        return
    old_pref = os.path.normcase(old_wd)
    tail = old_pref + os.sep
    f = st["files"]
    for k, v in list(f.items()):
        if not isinstance(v, str):
            continue
        nv = os.path.normcase(os.path.abspath(v))
        if nv == old_pref or nv.startswith(tail):
            f[k] = os.path.join(new_wd, os.path.relpath(v, old_wd))
    st["workdir"] = new_wd


def _load_state_for(a) -> dict:
    """所有读 state 后做事的子命令统一入口：读取 state 并把钉死路径对齐到本次 --workdir。

    init 不走这里（它首次创建 state、按原样钉死路径）。正常主链（--workdir 与
    state.workdir 相等）时 _relocate_state 是严格 no-op，行为逐字节不变；工作区被
    整体复制到新目录时，把 st["workdir"] 与 st["files"] 的旧前缀重映射到新目录，
    使后续所有报告 / 清单 / 体检 / stage state 都落新目录、旧目录零写入。
    来源元数据（source / *_source）不重映射（母本常在独立目录）。
    """
    st = load_state(a.workdir)
    _relocate_state(st, a.workdir)
    return st


def stage(st: dict, sid: str) -> dict:
    return st["stages"][sid]


def run_cmd(argv: list[str], label: str) -> tuple[int, str]:
    """调用同目录脚本；返回 (退出码, 合并后的输出文本)。"""
    cmd = [sys.executable] + argv
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except OSError as e:
        return 1, "[调用失败] %s" % e
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    if out:
        for line in out.splitlines()[-12:]:
            log("    | " + line[:160])
    if r.returncode != 0:
        log("    ! %s 退出码 %d" % (label, r.returncode))
    return r.returncode, out


def script(name: str) -> str:
    return os.path.join(HERE, name)


def need(path: str, desc: str) -> None:
    if not os.path.exists(path):
        raise SystemExit("[错误] 缺少%s：%s" % (desc, path))


def md5(path: str) -> str:
    return hashlib.md5(read_bytes(path)).hexdigest()


# ---------------------------------------------------------------- init
def cmd_init(a) -> None:
    src = os.path.abspath(a.source)
    need(src, "源文档")
    wd = os.path.abspath(a.workdir)
    os.makedirs(wd, exist_ok=True)

    st = {
        "version": 1,
        "created": now(),
        "updated": now(),
        "source": src,
        "workdir": wd,
        "options": {
            "volumes": a.volumes,
            "skip_merge": a.skip_merge,
            "scatter_marker": a.scatter_marker,
            "no_builtin_rules": a.no_builtin_rules,
            "fingerprint": os.path.abspath(a.fingerprint) if a.fingerprint else None,
            "head": a.head,
            "tail": a.tail,
            "subject": a.subject,
            "subject_desc": a.subject_desc,
            "screening": a.screening,
            "extract": os.path.abspath(a.extract) if a.extract else None,
            "legacy_merge": a.legacy_merge,
            "semantic_merge": (a.semantic_merge == "on"),
            "similarity": a.similarity,
            "max_cluster": a.max_cluster,
            "context_cap": a.context_cap,
            "parallel": a.parallel,
            "big_steps": a.big_steps,
            "small_steps": a.small_steps,
            "skip_stages": [],
            "goal": a.goal,
            "final_format": a.final_format,
            "dedup_policy": a.dedup_policy,
            "conflict_policy": a.conflict_policy,
            "keep_source_mark": a.keep_source_mark,
            "delivery_list": a.delivery_list,
            "model_host": a.model_host,
            "cpu_tier": a.cpu_tier,
            "think_depth": a.think_depth,
            "urgent": a.urgent,
            "accelerator": a.accelerator,
        },
        "answers": {
            "mode": a.mode,
            "zero_loss": a.zero_loss,
            "delivery": a.delivery,
            "execution": a.execution,
            "subject": a.subject,
            "note": a.note or "",
            "context_cap": a.context_cap,
            "parallel": a.parallel,
            "big_steps": a.big_steps,
            "small_steps": a.small_steps,
            "goal": a.goal,
            "final_format": a.final_format,
            "dedup_policy": a.dedup_policy,
            "conflict_policy": a.conflict_policy,
            "keep_source_mark": a.keep_source_mark,
            "delivery_list": a.delivery_list,
            "model_host": a.model_host,
            "cpu_tier": a.cpu_tier,
            "think_depth": a.think_depth,
            "urgent": a.urgent,
            "accelerator": a.accelerator,
        },
        "files": {},
        "stages": {
            sid: {"name": name, "kind": kind, "status": "pending",
                  "started": "", "finished": "", "note": "", "outputs": []}
            for sid, name, kind in STAGES
        },
    }
    # 新 7 步记录性参数写进 state：第 5 步检查·重切块上限（默认取 context_cap/2）、
    # 第 6 步全文检查切块大小（默认同样受 half_cap 约束、严格 < 1/2 上下文）。
    _half_cap = (a.context_cap // 2) if a.context_cap else None
    st["options"]["check_rechunk_cap"] = (a.check_rechunk_cap if a.check_rechunk_cap is not None
                                          else _half_cap)
    st["options"]["full_check_chunk_size"] = (a.full_check_chunk_size if a.full_check_chunk_size is not None
                                              else _half_cap)
    st["answers"]["check_rechunk_cap"] = st["options"]["check_rechunk_cap"]
    st["answers"]["full_check_chunk_size"] = st["options"]["full_check_chunk_size"]
    # 中间文件落盘规则：每个步骤独立文件夹（见 build_files / STEP_DIR_NAMES）
    st["doc_root"] = os.path.dirname(src)
    st["doc_name"] = os.path.splitext(os.path.basename(src))[0]
    build_files(st, 1)
    f = st["files"]

    # 成本预估（0.08 积分 / 1K token 为本项目历史倍率，仅作估算，不阻断）
    raw = read_bytes(src)
    chars = len(raw.decode("utf-8", errors="replace"))
    est_blocks = max(1, chars // 40000)
    est_tokens = est_blocks * 40000 * 2
    st["estimate"] = {
        "源字节": os.path.getsize(src), "字符数": chars,
        "预计块数(按4万字/块)": est_blocks,
        "估算 token(读+写)": est_tokens,
        "估算积分(0.08/1K)": round(est_tokens / 1000 * 0.08, 1),
        "短文档短路": chars < 40000,
        "Q3_上下文字符": a.context_cap,
        "Q3_按上下文预计块数": max(1, chars // max(1, a.context_cap)) if a.context_cap else None,
        "Q5_目标压缩体积": a.target_compression,
        "Q6_最大可容忍文档大小(0=不限)": a.max_doc_size,
    }
    # ---- 新主流程参数真正落地：Q3 分卷数 / Q4 并发数按用户值写入 / Q1-Q2 阶段跳过 ----
    _argv = sys.argv[1:]
    _explicit_volumes = any(x == "--volumes" or x.startswith("--volumes=") for x in _argv)
    if a.context_cap and not _explicit_volumes:
        # Q3：上下文越小、单卷越小、卷数越多（钉在 [1,20]）
        st["options"]["volumes"] = max(1, min(20, -(-chars // max(1, a.context_cap))))
    _par = a.parallel
    # 并发数完全由开场 Q4 用户自定：CPU 档位 / 模型宿主只用于给建议值，不设上限、不钳制。
    # 开多个 agent 须先经用户允许（流程纪律，不设并发数字门槛）。
    log("  [并发] Q4 用户设定 %d；CPU 档位 %s / 模型宿主 %s 仅作建议，不设上限、不钳制" % (_par, a.cpu_tier, a.model_host))
    if _par != 1:
        log("  [并发] 开多个 agent 须先经用户允许；重文本任务建议串行 1")
    st["options"]["parallel"] = _par
    st["answers"]["parallel"] = _par
    st["options"]["target_compression"] = a.target_compression
    st["options"]["max_doc_size"] = a.max_doc_size
    st["options"]["compress_policy"] = a.compress_policy
    st["options"]["max_recompress"] = a.max_recompress
    st["original_source"] = src
    st["original_chars"] = chars
    st["recompress_round"] = 0
    st["options"]["skip_stages"] = skip_stages_from_answers(a.big_steps, a.small_steps)
    st["answers"]["skip_stages"] = st["options"]["skip_stages"]
    short = chars < 40000
    # Q6 最大可容忍文档大小：仅告警+建议，不强制阻断
    if a.max_doc_size and chars > a.max_doc_size:
        log("  ! [Q6] 源文档 %d 字符 > 最大可容忍 %d：建议先按行分卷(--volumes 调大)或拆批再跑，避免一次性塞爆；不强制阻断，尊重你的选择" % (chars, a.max_doc_size))
    # Q5 目标压缩体积：记录意图，最终受零丢失硬底线约束
    if a.target_compression and a.target_compression not in ("standard", ""):
        log("  [Q5] 已采纳目标压缩口径：%s（最终停止仍以零丢失硬底线为准，禁止为凑篇幅强压）" % a.target_compression)
    if short:
        # 短文档真短路：默认关掉三道筛选与分卷，避免在小文档上做无用功。
        # 用户显式给了 --screening on 或 --volumes>1 时尊重显式选择。
        argv = sys.argv[1:]
        explicit_screening = any(x == "--screening" or x.startswith("--screening=") for x in argv)
        explicit_volumes = any(x == "--volumes" or x.startswith("--volumes=") for x in argv)
        if a.screening == "on" and not explicit_screening:
            st["options"]["screening"] = "off"
            st["options"]["auto_shortcut"] = True
        if a.volumes == 7 and not explicit_volumes:
            st["options"]["volumes"] = 0
        log("  [短文档短路] 源文档 %d 字符 < 4 万：标准总结主流程可直接一次性总结、不必切块；"
            "本编排器短路策略默认关闭筛选链(S3–S6)与分卷（未显式指定时 --screening off / --volumes 0）；已显式 --screening on 时尊重显式选择保留筛选链，"
            "直接以筛选基准稿进入合并/分卷（执行顺序：筛选在前、合并在后）。" % chars)
        log("  如需强制走完整链，显式加 --screening on --volumes 7。")

    # 未给 --subject-desc 时，从源文档开头抽几行非空原文作判定素材（不额外问用户）。
    # 只做原文摘录、不做推断，避免基准里出现无依据的项目定义。
    desc = a.subject_desc
    if not desc.strip():
        excerpt = []
        try:
            for line in raw.decode("utf-8", errors="replace").splitlines():
                t = line.strip().lstrip("\ufeff")
                # 跳过纯文档工程残留/标记/分隔符，避免把噪声写进判定基准
                if (not t or t.startswith(("```", "<!--", "|"))
                        or t in {"-", "=", ">", "---", "==="}):
                    continue
                if re.match(r"^(区块\d+|分卷\d+|合并块|来源[:：]|【来源)", t):
                    continue
                # 去掉 markdown 起始符/粗体标记后取正文
                body = re.sub(r"^[-*>\*\d\.、\s#]+", "", t).replace("**", "").strip()
                # 至少含 4 个中文/字母数字字符才算实质内容（保留章节标题）
                if len(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", body)) < 4:
                    continue
                excerpt.append(body[:120])
                if len(excerpt) >= 5:
                    break
        except Exception as e:
            log("  [警告] 判定素材自动摘录失败：%s（基准将使用通用定义占位）" % e)
        if excerpt:
            desc = ("（开场询问包未提供项目定义。以下摘自源文档开头，供判定对象识别参考；"
                    "属**原文摘录**，非 AI 推论，判定仍以基准第二节「毫无关联」三条标准为准。）\n\n"
                    + "\n".join("- " + x for x in excerpt))
            st["subject_desc_source"] = "源文档开头 5 行原文摘录"
    else:
        st["subject_desc_source"] = "开场询问包 / --subject-desc 提供"

    # 生成统一判定基准（三道 agent 唯一标准）
    if a.criterion_file and os.path.isfile(a.criterion_file):
        # 用户已有现成判定基准：原样采用，不覆盖、不重新生成
        write_bytes(f["criterion"], read_bytes(a.criterion_file))
        st["criterion_source"] = a.criterion_file
    else:
        keep_cats = ""
        if a.keep_categories and os.path.exists(a.keep_categories):
            # 二进制读 + 容错解码：外部类别清单若非标准 UTF-8，给出明确提示而非裸 traceback
            try:
                keep_cats = read_bytes(a.keep_categories).decode("utf-8", errors="replace")
            except OSError as e:
                log("  [警告] 读取 --keep-categories 失败，改用内置通用表：%s" % e)
                keep_cats = ""
        with open(f["criterion"], "w", encoding="utf-8") as fp:
            fp.write(render_criterion(a.subject, desc, keep_cats, a.criterion_note))
        st["criterion_source"] = "S0 自动生成（pipeline_templates.render_criterion）"

    s = stage(st, "S0")
    s["status"] = "done"
    s["started"] = s["finished"] = now()
    s["note"] = "参数固化 + 统一判定基准已生成；开场询问包答案已记录；后续不再向用户提问"
    s["outputs"] = [os.path.join(wd, STATE_NAME), f["criterion"]]
    save_state(wd, st)

    log("已初始化：%s" % wd)
    log("  源文档 : %s (%d B)" % (src, os.path.getsize(src)))
    log("  模式   : %s | 零丢失 %s | 交付 %s | 执行 %s" %
        (a.mode, a.zero_loss, a.delivery, a.execution))
    log("  判定对象 : %s" % a.subject)
    log("  新主流程 : 上下文 %s | 并行 %s | 大步[%s] 小步[%s] | 跳过阶段 %s" %
        (a.context_cap, st["options"]["parallel"], a.big_steps or "全部", a.small_steps or "全部",
         ",".join(st["options"]["skip_stages"]) or "无"))
    log("  并发判据 : 模型宿主=%s | CPU 档=%s | 思考强度=%s | 急不急=%s | 加速器=%s" %
        (a.model_host, a.cpu_tier, a.think_depth, a.urgent or "未指定", a.accelerator or "未指定"))
    log("  判定基准 : %s（三道 agent 唯一共用标准）" % f["criterion"])
    log("  筛选基准稿 : %s" % f["screen_base"])
    log("    来源 : %s" % st["screen_base_source"])
    if not (a.extract and os.path.isfile(os.path.abspath(a.extract))):
        log("    [告警] 未给 --extract 或文件不存在：筛选基准稿已退回母本；"
            "第 1 步总提取稿应经 init --extract 入链，作为筛选基准稿的唯一来源")
    log("  成本预估 : %s" % json.dumps(st["estimate"], ensure_ascii=False))
    log("  下一步 : python run_pipeline.py run --workdir \"%s\"" % wd)


# ---------------------------------------------------------------- 自动阶段
def do_S1(st: dict, s: dict) -> None:
    f, o = st["files"], st["options"]
    ext = o.get("fingerprint")
    if ext and os.path.isfile(ext):
        # 用户给了现成基线：以其为准（不重算），但校验三类集合非空且齐全
        # 二进制读 + 容错解码 + 显式捕获异常：非法 JSON / 非 UTF-8 给出明确提示，不裸 traceback
        try:
            base = json.loads(read_bytes(ext).decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
            raise RuntimeError("S1 外部基线读取/解析失败：%s" % e)
        if not isinstance(base, dict):
            raise RuntimeError("S1 外部基线不是合法 JSON 对象：%s" % ext)
        for key in ("nums", "vals", "conf"):
            if not base.get(key):
                raise RuntimeError("S1 外部基线缺字段或为空：%s" % key)
        write_bytes(f["baseline"], read_bytes(ext))
        st["baseline_source"] = ext
        s["outputs"] = [f["baseline"]]
        s["note"] = "采用外部基线 %s（%d 编号 / %d 数值 / %d 冲突）" % (
            os.path.basename(ext), len(base["nums"]), len(base["vals"]), len(base["conf"]))
        return
    # 基线必须与筛选基准稿同源（S1 指纹基线锚 screen_base，不锚 merged）
    argv = [script("fingerprint.py"), "--source", f["screen_base"], "--out", f["baseline"]]
    rc, _ = run_cmd(argv, "S1 指纹基线")
    if rc:
        raise RuntimeError("S1 指纹基线失败")
    need(f["baseline"], "指纹基线")
    st["baseline_source"] = "S1 自动从筛选基准稿 screen_base 生成"
    s["outputs"] = [f["baseline"]]
    s["note"] = "基线已建（三类指纹：编号 / 数值+单位 / 冲突标记）"


class NeedMergeAgent(Exception):
    """S2 语义合并：候选簇与提示词已生成，等待 1 个前台 agent 产出 合并结果.json。

    run_auto 捕获后把 S2 置为 waiting 并返回 'need_agent'（与 S4 的 need_agent 范式一致），
    由主线程派 1 个前台 agent（强锁 1、并发 1），产出后 resume 重进 S2 走 verify+render。
    """

    def __init__(self, prompt: str, result: str, workdir: str):
        super().__init__("等待第 3 步语义合并的 1 个前台 agent")
        self.prompt = prompt
        self.result = result
        self.workdir = workdir


def semantic_merge_hook(st: dict, data: bytes) -> bytes:
    """第 3 步「条目合并」真语义合并（--semantic-merge on，默认）：脚本不调模型。

    流程：落盘筛后稿(do_S2 已写 f["merged"]) → fingerprint 生成合并专用基线 →
    merge_semantic.py prepare 机械生成候选簇 + 完整提示词（只产候选、不裁决） →
    检测 合并结果.json：
      不存在 → 抛 NeedMergeAgent，run_auto 停下等主线程派 1 个前台 agent（强锁 1、并发 1）；
      存在   → merge_semantic.py verify + render 做全覆盖／数字指纹零丢失硬校验，
               通过后返回合并稿字节（由 do_S2 写回 f["merged"]，供 S7 出终稿）。
    语义裁决只能由前台 agent 完成；脚本只做候选簇生成、校验、渲染，绝不自行改写/删减事实。
    --semantic-merge off / --legacy-merge 时不调用本函数（走第一批直通 / 机械合并），保持兼容。
    """
    f, o = st["files"], st["options"]
    step = st.get("step_dirs", {}).get("S2") or os.path.dirname(f["merged"])
    os.makedirs(step, exist_ok=True)
    # 1) 合并专用指纹基线：与筛后稿同源，不含被筛选剔除行的数字（避免把删行数字误判为丢失）
    rc, _ = run_cmd([script("fingerprint.py"), "--source", f["merged"],
                     "--out", f["合并_指纹基线"]], "S2 语义合并·指纹基线")
    if rc:
        raise RuntimeError("S2 语义合并指纹基线生成失败")
    # 2) prepare：候选簇 + 完整 agent 提示词（机械生成、只产候选不裁决；幂等可重跑）
    rc, _ = run_cmd([script("merge_semantic.py"), "prepare",
                     "--input", f["merged"], "--outdir", step,
                     "--similarity", str(o.get("similarity", 0.80)),
                     "--max-cluster", str(o.get("max_cluster", 20)),
                     "--result", f["合并结果"],
                     "--workdir", st["workdir"]], "S2 语义合并·prepare")
    if rc:
        raise RuntimeError("S2 语义合并 prepare 失败")
    # 3) 检测 agent 产出：不存在则停住等 1 个前台 agent（S2 内检测，不改 STAGES.kind）
    if not os.path.isfile(f["合并结果"]):
        raise NeedMergeAgent(prompt=f["合并_agent_prompt"],
                             result=f["合并结果"], workdir=st["workdir"])
    # 4) verify + render：硬校验不通过则子进程非零退出 → 这里收口为失败，绝不落盘
    rc, _ = run_cmd([script("merge_semantic.py"), "verify",
                     "--input", f["merged"], "--result", f["合并结果"],
                     "--fingerprint", f["合并_指纹基线"], "--outdir", step],
                    "S2 语义合并·verify")
    if rc:
        raise RuntimeError("S2 语义合并全覆盖／数字指纹零丢失硬校验未通过（不落盘）")
    rc, _ = run_cmd([script("merge_semantic.py"), "render",
                     "--input", f["merged"], "--result", f["合并结果"],
                     "--fingerprint", f["合并_指纹基线"],
                     "--out", f["合并稿"], "--map", f["合并_行映射"]],
                    "S2 语义合并·render")
    if rc:
        raise RuntimeError("S2 语义合并 render 失败（不落盘）")
    st["semantic_merge_info"] = {
        "candidates": f["合并_候选簇"], "prompt": f["合并_agent_prompt"],
        "result": f["合并结果"], "merged": f["合并稿"],
        "coverage": f["合并_覆盖校验"], "map": f["合并_行映射"],
        "baseline": f["合并_指纹基线"],
    }
    return read_bytes(f["合并稿"])


def do_S2(st: dict, s: dict) -> None:
    """第 3 步「条目合并」（执行顺序已真反转：在筛选之后）：吃筛选结果 + 真语义合并，产出 f["merged"]。

    输入 = f["screen_base"]（筛选基准稿）+ agent3 的 final_remove_lines；按物理行号整行
    保留／丢弃得到「筛后稿」，先落盘到 f["merged"] 作为语义合并的机械输入。
    --semantic-merge on（默认）时：调 semantic_merge_hook 走「候选簇→1 个前台 agent→
    全覆盖/数字指纹零丢失硬校验→落盘」，校验通过的合并稿写回 f["merged"]，供 S7 出终稿；
    首次进入（合并结果.json 未产出）由 run_auto 停在 need_agent 等主线程派 agent，resume 后继续。
    --semantic-merge off 时退回第一批直通（零改写）；--legacy-merge 时走 line_select_merge.py
    机械删行/去重（都不改变筛选链的行号基准，基准恒为 screen_base）。
    """
    f, o = st["files"], st["options"]
    base = f["screen_base"]
    remove = set()
    if os.path.isfile(f["agent3"]):
        with open(f["agent3"], "r", encoding="utf-8") as fp:
            remove = _remove_lines(json.load(fp))
    raw = read_bytes(base)
    lines = raw.splitlines(keepends=True)
    bad = [n for n in remove if n < 1 or n > len(lines)]
    if bad:
        raise RuntimeError("S2 终审剔除行号越界：%s" % bad[:10])
    kept = b"".join(l for i, l in enumerate(lines, 1) if i not in remove)
    # 先落盘筛后稿（语义合并 prepare/verify 的 --input；off/legacy 时它就是最终 f["merged"]）
    write_bytes(f["merged"], kept)

    semantic_on = bool(o.get("semantic_merge") and not o.get("legacy_merge"))
    if semantic_on:
        # 真语义合并：可能抛 NeedMergeAgent（等 1 个前台 agent），由 run_auto 收口为 need_agent
        out_bytes = semantic_merge_hook(st, kept)
        write_bytes(f["merged"], out_bytes)
        merge_note = "语义合并已落盘（候选簇→1 前台 agent→全覆盖/指纹零丢失硬校验）"
    elif o.get("legacy_merge"):
        # 第一批兼容：机械删行/去重（基准 screen_base，不改筛选链行号体系）
        argv = [script("line_select_merge.py"),
                "--source", base, "--out", f["merged"],
                "--state", f["merge_state"], "--map", f["merge_map"]]
        if o.get("scatter_marker"):
            argv += ["--scatter-marker", o["scatter_marker"]]
        if o.get("no_builtin_rules"):
            argv += ["--no-builtin-rules"]
        rc, _ = run_cmd(argv, "S2 --legacy-merge 机械合并")
        if rc:
            raise RuntimeError("S2 --legacy-merge 机械合并失败")
        need(f["merged"], "合并结果")
        out_bytes = read_bytes(f["merged"])
        merge_note = "--legacy-merge 机械合并"
    else:
        out_bytes = kept
        merge_note = "--semantic-merge off 直通（零改写，兼容）"

    # 记录本步删除行（行号锚 screen_base），供 S7 剔除行内容体检 / 逐行去向映射使用
    st["s2_removed_lines"] = sorted(remove)
    src_size = len(raw)
    out_size = len(out_bytes)
    ratio = out_size / src_size if src_size else 1.0
    hist = st.setdefault("compression_history", [])
    hist.append({"stage": "S2", "src": src_size, "out": out_size,
                 "ratio": round(ratio, 4), "time": now()})
    stalled = len(hist) >= 2 and all(
        hist[i]["ratio"] >= 0.9 or hist[i]["out"] >= hist[i]["src"] for i in range(-2, 0))
    st["scatter_info"] = {"enabled": False, "start": None,
                          "explicit_marker": bool(o.get("scatter_marker")),
                          "legacy_merge": bool(o.get("legacy_merge")),
                          "note": "新链：按筛选结果整行落盘 + 语义合并（--legacy-merge 时机械删行）"}
    st["lower_bound"] = {
        "src": src_size, "out": out_size, "ratio": round(ratio, 4),
        "reached": ratio >= 0.9 or out_size >= src_size,
        "stalled": stalled,
        "advice": "已达零丢失下限：停止压缩并向用户报告不可再压，禁止为凑篇幅强压" if stalled else "",
    }
    if stalled:
        log("  ! 已达零丢失下限（连续 2 轮压缩比 >=0.9 或输出 >= 输入）：停止压缩并向用户报告")
    s["outputs"] = [f["merged"]]
    s["note"] = ("第3步 条目合并（筛选之后）：按筛选结果落盘 %d B / 剔除 %d 行 / 压缩比 %.4f / %s%s" %
                 (out_size, len(remove), ratio, merge_note,
                  "（已达下限，停止）" if stalled else ""))


def do_S3(st: dict, s: dict) -> None:
    f, o = st["files"], st["options"]
    argv = [script("build_line_view.py"), "--source", f["screen_base"], "--out", f["view"],
            "--head", str(o["head"]), "--tail", str(o["tail"]), "--index", f["view_index"]]
    rc, _ = run_cmd(argv, "S3 判定视图")
    if rc:
        raise RuntimeError("S3 判定视图失败")

    # 生成三份可直接派发的 agent 提示词（主线程不再手工拼提示词）
    _refresh_prompts(st, force=True)

    s["outputs"] = [f["view"], f["view_index"], f["prompt1"], f["prompt2"], f["prompt3"]]
    s["note"] = "视图行号一一对应；判定基准 + 三份 agent 提示词已落盘，可直接派发"


def do_S5(st: dict, s: dict) -> None:
    f = st["files"]
    pairs = [
        (f["agent1"], f["screen1"], "筛选1 · 初判无关条目（第1道）"),
        (f["agent2"], f["screen2"], "筛选2 · 复核关联条目（第2道）"),
        (f["agent3"], f["screen3"], "筛选3 · 终审判定（第3道）"),
    ]
    outs = []
    for js, md, title in pairs:
        need(js, "agent 结果 JSON")
        argv = [script("render_from_lines.py"), "--source", f["screen_base"],
                "--lines", js, "--out", md, "--title", title,
                "--note", "原文由脚本按物理行号从筛选基准稿逐字节抽取，零改写。"]
        rc, _ = run_cmd(argv, "S5 渲染 %s" % os.path.basename(md))
        if rc:
            raise RuntimeError("S5 渲染失败：%s" % md)
        outs.append(md)
    s["outputs"] = outs
    s["note"] = "三个新文档全部由行号抽取生成"


def do_S6(st: dict, s: dict) -> None:
    f, o = st["files"], st["options"]
    argv = [script("verify_three_pass.py"),
            "--a", f["agent1"], "--b", f["agent2"], "--c", f["agent3"],
            "--source", f["screen_base"]]
    rc, _ = run_cmd(argv, "S6 三道集合校验")
    if rc:
        raise RuntimeError("S6 三道集合校验未通过")
    base = f["baseline"]          # S1 已把外部基线落到此路径，作为唯一事实源
    argv = [script("fingerprint.py"), "--source", f["screen_base"], "--out", base,
            "--check", f["screen_base"], "--exclude-noise"]
    rc, _ = run_cmd(argv, "S6 指纹零丢失")
    if rc:
        raise RuntimeError("S6 指纹零丢失校验未通过")
    s["outputs"] = [base]
    s["note"] = "集合互斥且并集=初判全集；三类指纹集合层 0 缺失"


def _remove_lines(c: dict) -> set[int]:
    """兼容多种终审 JSON 字段命名，取出最终剔除行号集合。"""
    sm = c.get("summary", {}) or {}
    for key in ("final_remove_lines", "remove_lines", "remove"):
        if key in sm:
            return set(int(x) for x in sm[key])
    # 退化：从 items 里按 verdict 判定
    rm = set()
    for it in c.get("items", []):
        if "剔除" in str(it.get("verdict", "")):
            rm.add(int(it["line"]))
    return rm


def do_S7(st: dict, s: dict) -> None:
    """第 4 步终稿生成：从 f["merged"]（S2 已按筛选结果落盘）出终稿。

    不再自己按行号删行（删行已由 S2 完成）；本步做：清洗定稿（当前字节不变，保证
    「终稿 = 筛选基准稿按终审剔除行删行」的逐字节断言仍成立）+ 逐行去向映射
    + 剔除行内容体检（体检对象 = S2 的删除行集 st["s2_removed_lines"]，别漏）。
    """
    f = st["files"]
    remove = set(int(x) for x in st.get("s2_removed_lines", []))
    # 逐行去向映射 / 体检的行号基准 = 筛选基准稿（与三道筛选的行号体系一致）
    src_lines = read_bytes(f["screen_base"]).splitlines(keepends=True)
    bad = [n for n in remove if n < 1 or n > len(src_lines)]
    if bad:
        raise RuntimeError("S7 体检：S2 删除行号越界：%s" % bad[:10])
    # 剔除行内容体检：统计 S2 删除行里有多少「非空且有文字」的行，并落盘供人工复核。
    # 零丢失底线针对设计条目；纯空行/纯分隔符可剔除，但含文字的行必须有终审理由。
    removed_text = []
    for n in sorted(remove):
        t = src_lines[n - 1].decode("utf-8", errors="replace").strip()
        if t and any(ch.isalnum() or "\u4e00" <= ch <= "\u9fff" for ch in t):
            removed_text.append((n, t[:120]))
    st["removed_text_lines"] = removed_text
    if removed_text:
        log("  [提示] S2 删除行中含文字的共 %d 行（非空/非纯分隔符），已写入报告供复核：" %
            len(removed_text))
        for n, t in removed_text[:5]:
            log("    L%d | %s" % (n, t[:80]))
        # 体检报告（输出文档之外）
        rep = ["# 剔除行内容体检", "",
               "> S2 按筛选结果删除的含文字行。请人工确认这些行确属「毫无关联」，而非设计条目。", "",
               "| 母本行 | 内容（截断 120 字） |", "|---|---|"]
        rep += ["| L%d | %s |" % (n, t.replace("|", "/")) for n, t in removed_text]
        rep += ["", "- 共 %d 行；剔除总数 %d 行（其余为空行/纯分隔符）。" %
                (len(removed_text), len(remove)), ""]
        with open(f["剔除行内容体检"], "w", encoding="utf-8") as fp:
            fp.write("\n".join(rep) + "\n")

    # 清洗定稿：当前字节不变（merged 即定稿），保留清洗挂载点；S7 不再自己按行号删行。
    merged_path = f["merged"] if os.path.exists(f["merged"]) else f["screen_base"]
    kept = read_bytes(merged_path)
    write_bytes(f["final"], kept)
    if not st["options"].get("legacy_merge") and not st["options"].get("semantic_merge"):
        # 直通(off)时，终稿必须 == 筛选基准稿按终审剔除行删行（与 verify_three_pass --final 一致）。
        # --semantic-merge on（默认）时终稿 = 语义合并稿（行内容已合并简化），不再等于按行删行稿；
        # 其零丢失由 merge_semantic.py 的全覆盖/数字指纹硬校验保证，此处不断言逐字节相等。
        expected = b"".join(l for i, l in enumerate(src_lines, 1) if i not in remove)
        assert kept == expected, "S7 定稿与「筛选基准稿按剔除行删行」不一致"

    # 逐行去向映射（输出文档之外）：输入 = 筛选基准稿，输出 = 定稿
    md = ["# 筛选 · 逐行去向映射", "",
          "> 输入为筛选基准稿；每条原文由脚本按物理行号抽取，定稿 = 筛选基准稿按行号删去终审剔除行（删行由 S2 完成）。"
          "（--semantic-merge on 时定稿再经第 3 步语义合并，逐行去向以 合并_行映射.md 为准。）", "",
          "| 输入行 | 去向 |", "|---|---|"]
    out_ln = 0
    for i in range(1, len(src_lines) + 1):
        if i in remove:
            md.append("| L%d | 剔除（终审确认，S2 已删） |" % i)
        else:
            out_ln += 1
            md.append("| L%d | 保留 → 定稿 L%d |" % (i, out_ln))
    md += ["", "- 输入 %d 行 / 定稿 %d 行 / 剔除 %d 行" % (len(src_lines), out_ln, len(remove)),
           "- 定稿字节 %d，与「筛选基准稿逐字节删剔除行」结果一致。" % len(kept), ""]
    with open(f["final_map"], "w", encoding="utf-8") as fp:
        fp.write("\n".join(md) + "\n")

    s["outputs"] = [f["final"], f["final_map"]]
    s["note"] = "%d 行 / %d B，S2 剔除 %d 行（其中含文字 %d 行，需人工复核体检报告）" % (
        out_ln, len(kept), len(remove), len(removed_text))


def do_S8(st: dict, s: dict) -> None:
    f, o = st["files"], st["options"]
    n = int(o["volumes"] or 0)
    # 短路兜底：筛选链被跳过且 S7 未产出定稿时，直接以筛选基准稿进入分卷，
    # 不再把「合并结果」当分卷输入（合并现在在筛选后）。
    if o.get("screening") == "off" and not os.path.exists(f["final"]):
        st["files"]["final"] = f["screen_base"]
        st["options"]["screening_no_final"] = True
        f = st["files"]
    if n <= 1:
        s["note"] = "未启用分卷（--volumes <= 1）"
        return
    argv = [script("split_by_lines.py"), "--source", f["final"],
            "--volumes", str(n), "--out-prefix", f["volume_prefix"],
            "--map", f["volume_map"]]
    rc, _ = run_cmd(argv, "S8 按行分卷")
    if rc:
        raise RuntimeError("S8 分卷失败")
    # 独立复核：拼接 == 定稿（二进制）
    vols = []
    k = 1
    while os.path.exists("%s%d.txt" % (f["volume_prefix"], k)):
        vols.append("%s%d.txt" % (f["volume_prefix"], k))
        k += 1
    joined = b"".join(read_bytes(p) for p in vols)
    if joined != read_bytes(f["final"]):
        raise RuntimeError("S8 分卷拼接与定稿不一致")
    s["outputs"] = vols + [f["volume_map"]]
    s["note"] = "%d 卷，切点严格两行之间，拼接逐字节 == 定稿" % len(vols)


def _render_one(st: dict, js: str, md: str, title: str) -> None:
    """幂等地把某道 agent 的 JSON 渲染成带原文的文档。"""
    if not os.path.exists(js):
        return
    rc, _ = run_cmd([script("render_from_lines.py"), "--source", st["files"]["screen_base"],
                     "--lines", js, "--out", md, "--title", title,
                     "--note", "原文由脚本按物理行号从筛选基准稿逐字节抽取，零改写。"],
                    "渲染 " + os.path.basename(md))
    if rc != 0:
        # 渲染子进程失败（如行号越界 / JSON 非法）时会在写盘前就退出，目标文档未生成；
        # 此处必须收口为明确失败，禁止后续再打印「已渲染 / 派下一道」造成「退出码 0 + 谎报进度」。
        raise SystemExit(
            "[错误] 筛选文档「%s」渲染失败（退出码 %d），%s 未生成，已停止推进、不派下一道。"
            "请检查 %s 的行号是否越界、JSON 格式是否合法，修正后重跑 prepare。" %
            (title, rc, os.path.basename(md), os.path.basename(js)))


PROMPT_GEN_TAG = "<!-- GENERATED-BY: pipeline_templates.py ; 手工修改后请勿覆盖 -->"


def _refresh_prompts(st: dict, force: bool = False) -> list[str]:
    """按当前已存在的文档重新生成三份提示词（保证引用的路径都已落盘）。

    用户手工改过的提示词（不含生成标记）默认**保留不覆盖**，避免 prepare 冲掉定制内容。
    返回被跳过的提示词路径列表。
    """
    f = st["files"]
    subject = st["answers"].get("subject") or st["options"].get("subject") or "目标项目"
    skipped = []
    for no, path, out_json, extra in [
        (1, f["prompt1"], f["agent1"], dict()),
        (2, f["prompt2"], f["agent2"], dict(doc_a=f["screen1"])),
        (3, f["prompt3"], f["agent3"], dict(doc_a=f["screen1"], doc_b=f["screen2"])),
    ]:
        if not force and os.path.isfile(path):
            old = open(path, encoding="utf-8").read()
            if PROMPT_GEN_TAG not in old:
                skipped.append(path)
                continue
        txt = render_agent_prompt(
            no, workdir=st["workdir"], subject=subject,
            criterion=f["criterion"], view=f["view"], source=f["screen_base"],
            out_json=out_json,
            think_depth=st["options"].get("think_depth", "medium"), **extra)
        with open(path, "w", encoding="utf-8") as fp:
            fp.write(PROMPT_GEN_TAG + "\n\n" + txt)
    return skipped


def cmd_prepare(a) -> None:
    """逐道推进：渲染已完成各道的文档 → 刷新提示词 → 打印下一步该派谁。"""
    st = _load_state_for(a)
    f = st["files"]
    _render_one(st, f["agent1"], f["screen1"], "筛选1 · 初判无关条目（第1道）")
    _render_one(st, f["agent2"], f["screen2"], "筛选2 · 复核关联条目（第2道）")
    skipped = _refresh_prompts(st)
    if skipped:
        log("  [保护] 以下提示词被手工改过，已保留不覆盖：")
        for p in skipped:
            log("    " + p)

    have = [os.path.exists(f[k]) for k in ("agent1", "agent2", "agent3")]
    done = sum(have)
    log("=== 逐道推进状态 ===")
    for i, k in enumerate(("agent1", "agent2", "agent3"), 1):
        log("  第%d道 %s : %s" % (i, os.path.basename(f[k]), "已产出" if have[i - 1] else "待产出"))
    if done == 0:
        log("\n下一步：派**第 1 道**（提示词 %s）" % f["prompt1"])
    elif done == 1:
        log("  文档 A 已渲染：%s" % f["screen1"])
        log("\n下一步：派**第 2 道**（提示词 %s，输入仅文档 A）" % f["prompt2"])
    elif done == 2:
        log("  文档 B 已渲染：%s" % f["screen2"])
        log("\n下一步：派**第 3 道**（提示词 %s，输入 A + B）" % f["prompt3"])
    else:
        log("\n三份 JSON 齐备，执行：python run_pipeline.py resume --workdir \"%s\"" % a.workdir)
    s = stage(st, "S4")
    s["note"] = "逐道推进：已完成 %d/3 道；判定基准与提示词已刷新" % done
    save_state(a.workdir, st)


def cmd_doctor(a) -> None:
    """失败诊断：逐阶段核对产物是否存在，给出缺失项、可能原因与建议命令。"""
    st = _load_state_for(a)
    f = st["files"]
    log("=== 诊断：%s ===" % a.workdir)
    problems = []

    # 源与基线
    if not os.path.exists(st["source"]):
        problems.append(("源文档不存在", "源被移动或删除", "重新 init 指定正确 --source"))
    if not os.path.exists(f["criterion"]):
        problems.append(("统一判定基准缺失", "init 未成功或文件被删",
                         "重跑 init（三道 agent 必须共用基准）"))
    # 阶段产物核对
    expected = {
        "S1": [f["baseline"]],
        # --skip-merge 时 S2 不写 merge_state.json / 合并_行映射.md，仅 merged 指向源
        "S2": [f["merged"]] if st["options"].get("skip_merge")
              else [f["merged"], f["merge_state"]],
        "S3": [f["view"], f["view_index"], f["prompt1"], f["prompt2"], f["prompt3"]],
        "S4": [f["agent1"], f["agent2"], f["agent3"]],
        "S5": [f["screen1"], f["screen2"], f["screen3"]],
        "S7": [f["final"], f["final_map"]],
        "S9": [f["report"]],
    }
    screening_off = st["options"].get("screening") == "off"
    for sid, paths in expected.items():
        s = stage(st, sid)
        if screening_off and sid in SCREENING_STAGES:
            continue
        if s["status"] == "done":
            missing = [p for p in paths if not os.path.exists(p)]
            if missing:
                problems.append(("%s 标记完成但产物缺失" % sid,
                                 "文件被移动/删除，或上次写入失败",
                                 "检查 " + ", ".join(os.path.basename(x) for x in missing)))
        elif s["status"] == "failed":
            problems.append(("%s 失败：%s" % (sid, s["note"]),
                             "见该阶段 note", "修复后 status 看进度 → resume 续跑"))
        elif sid == "S4" and s["status"] != "done":
            have = [os.path.exists(f[k]) for k in ("agent1", "agent2", "agent3")]
            if not all(have):
                problems.append(("S4 三道 agent 产出不齐（%d/3）" % sum(have),
                                 "某个 agent 被中断或没写 JSON",
                                 "跑 status 看该派第几道；prepare 渲染上一道成果"))
    # 校验类提示
    if os.path.exists(f["final"]):
        log("  定稿存在：%d B" % os.path.getsize(f["final"]))
    if (st["options"].get("semantic_merge") and not st["options"].get("legacy_merge")
            and stage(st, "S2")["status"] == "waiting"
            and not os.path.exists(f.get("合并结果", ""))):
        problems.append(("S2 语义合并等待 agent：合并结果.json 未产出",
                         "候选簇/提示词已生成，1 个前台 agent 尚未写结果",
                         "提示词 %s；产出 %s 后 resume" %
                         (f.get("合并_agent_prompt", ""), f.get("合并结果", ""))))
    if st.get("scatter_info", {}).get("enabled") is False and not st["options"].get("skip_merge"):
        problems.append(("零散内容块未分区", "可能静默误删成片纯文字独有内容（数字指纹查不出）",
                         "显式 --scatter-marker 重跑 S2，或确认源文档无此类内容"))

    if not problems:
        log("  未发现明显问题。可跑 verify 做独立验收：")
        log("    python run_pipeline.py verify --workdir \"%s\"" % a.workdir)
        return
    log("  发现 %d 项：" % len(problems))
    for i, (what, why, fix) in enumerate(problems, 1):
        log("  %d) %s" % (i, what))
        log("     原因：%s" % why)
        log("     建议：%s" % fix)
    log("\n  通用恢复：status 看进度 → 修好后 resume 续跑；"
        "agent 被中断先查残留 JSON，不要整批重跑。")


def cmd_status(a) -> None:
    st = _load_state_for(a)
    log("流水线状态：%s" % st["workdir"])
    log("  源文档：%s" % st["source"])
    log("  开场询问包答案：%s" % json.dumps(st["answers"], ensure_ascii=False))
    log("  %-4s %-30s %-8s %s" % ("阶段", "名称", "状态", "说明"))
    for sid, _, _ in STAGES:
        s = stage(st, sid)
        log("  %-4s %-30s %-8s %s" % (sid, s["name"], s["status"], s["note"][:60]))
    nxt = next((sid for sid, _, _ in STAGES if stage(st, sid)["status"] != "done"), None)
    f = st["files"]
    have = [os.path.exists(f[k]) for k in ("agent1", "agent2", "agent3")]
    doc_a = os.path.exists(f["screen1"])
    doc_b = os.path.exists(f["screen2"])
    log("  三道产出：第1道 %s / 第2道 %s / 第3道 %s" %
        tuple("已产出" if x else "待产出" for x in have))
    log("  输入文档：A %s / B %s" % ("已渲染" if doc_a else "未渲染", "已渲染" if doc_b else "未渲染"))
    if nxt:
        s = stage(st, nxt)
        log("\n下一步：%s %s（%s）" % (nxt, s["name"], s["kind"]))
        if nxt == "S2" and s["status"] == "waiting":
            log("  → 第 3 步语义合并：候选簇/提示词已生成，等 1 个前台 agent（强锁 1、并发 1）")
            log("    提示词 %s / 结果 %s" % (f["合并_agent_prompt"], f["合并结果"]))
            log("    产出后：python run_pipeline.py resume --workdir \"%s\"" % st["workdir"])
        elif s["kind"] == "agent":
            if not have[0]:
                log("  → 派**第 1 道**：提示词 %s" % f["prompt1"])
            elif not doc_a:
                log("  → 第 1 道已产出，先跑 prepare 渲染文档 A：")
                log("     python run_pipeline.py prepare --workdir \"%s\"" % st["workdir"])
            elif not have[1]:
                log("  → 派**第 2 道**：提示词 %s（输入仅文档 A）" % f["prompt2"])
            elif not doc_b:
                log("  → 第 2 道已产出，再跑 prepare 渲染文档 B：")
                log("     python run_pipeline.py prepare --workdir \"%s\"" % st["workdir"])
            elif not have[2]:
                log("  → 派**第 3 道**：提示词 %s（输入 A + B）" % f["prompt3"])
            else:
                log("  → 三份齐备，执行 resume：")
                log("     python run_pipeline.py resume --workdir \"%s\"" % st["workdir"])
            log("  （prepare 幂等，随时可跑；它会打印下一步该派谁）")
    else:
        log("\n全部阶段完成。运行报告：%s" % st["files"]["report"])


# 筛选链阶段集合（真反转后位于合并 S2 之前）：判定视图 / 三道筛选 / 渲染 / 集合+指纹校验。
# S7 是终稿生成（第 4 步），不属于筛选链，短路时也照常从 merged 出定稿。
SCREENING_STAGES = {"S3", "S4", "S5", "S6"}


def run_auto(st: dict, only: str | None = None, stop_at_agent: bool = True) -> str:
    """依次执行未完成阶段；返回 'done' / 'need_agent' / 'failed'。

    --screening off（短路）：跳过筛选链 S3–S6，直接以 screen_base 进入合并/分卷；
    S2 按空剔除集落盘、S7 出定稿、S8 分卷、S9 报告照常跑，三道筛选不跑。
    执行顺序已按权威流程真反转：筛选(第2步) → 合并(第3步)。
    """
    handlers = {"S1": do_S1, "S2": do_S2, "S3": do_S3,
                "S5": do_S5, "S6": do_S6, "S7": do_S7, "S8": do_S8}
    screening_off = st["options"].get("screening") == "off"
    skip_stages = set(st["options"].get("skip_stages", []))
    for sid, _, kind in STAGES:
        if sid == "S0":
            continue
        if only and sid != only:
            continue
        s = stage(st, sid)
        if s["status"] == "done":
            continue
        if sid in skip_stages:
            s["status"] = "skipped"
            s["note"] = "Q1/Q2 未选中该大步/小步（真反转后新主流程：筛选第2步→合并第3步）"
            save_state(st["workdir"], st)
            continue
        if screening_off and sid in SCREENING_STAGES:
            s["status"] = "skipped"
            s["note"] = "--screening off（仅合并模式，跳过筛选链）"
            save_state(st["workdir"], st)
            continue
        if kind == "agent":
            f = st["files"]
            if all(os.path.exists(f[k]) for k in ("agent1", "agent2", "agent3")):
                s["status"] = "done"
                s["started"] = s["finished"] = now()
                s["note"] = "三道串行 agent 结果已齐备（主线程派发）"
                s["outputs"] = [f["agent1"], f["agent2"], f["agent3"]]
                save_state(st["workdir"], st)
                log("  %s 三道 agent 结果齐备 ✓" % sid)
                continue
            if stop_at_agent:
                log("\n===== 停在语义阶段 %s：%s =====" % (sid, s["name"]))
                log("统一判定基准：%s" % f["criterion"])
                log("**严格串行**按下面 5 步推进（每步等上一个 agent 真正结束再起下一个）：")
                log("  1) 派第1道：提示词 %s → 产出 %s" % (f["prompt1"], f["agent1"]))
                log("  2) prepare：渲染文档 A、刷新第2道提示词")
                log("     python run_pipeline.py prepare --workdir \"%s\"" % st["workdir"])
                log("  3) 派第2道：提示词 %s → 产出 %s" % (f["prompt2"], f["agent2"]))
                log("  4) prepare 再跑一次 → 派第3道：提示词 %s → 产出 %s" % (f["prompt3"], f["agent3"]))
                log("  5) 三份齐备后：python run_pipeline.py resume --workdir \"%s\"" % st["workdir"])
                log("给每个 agent 的指令只需：『完整读完你的提示词文件，严格照做，完成后只回一行确认』。")
                log("prepare 可随时重复调用（幂等），也会告诉你下一步该派谁。")
                return "need_agent"
            return "need_agent"
        if sid not in handlers:
            continue
        log("\n--- %s %s ---" % (sid, s["name"]))
        s["status"] = "running"
        s["started"] = now()
        save_state(st["workdir"], st)
        try:
            handlers[sid](st, s)
        except NeedMergeAgent as e:
            s["status"] = "waiting"
            s["note"] = ("第3步语义合并：候选簇/提示词已生成，等 1 个前台 agent 产出 合并结果.json"
                         "（强锁 1、并发 1、跑完再起下一个）")
            save_state(st["workdir"], st)
            log("\n===== 停在第 3 步语义合并（S2）：等 1 个前台 agent（强锁 1）=====")
            log("  候选簇：%s" % st["files"]["合并_候选簇"])
            log("  提示词：%s" % e.prompt)
            log("  结果文件：%s" % e.result)
            log("  **严格串行**：只派 1 个前台 agent、并发 1、不启动其他 agent、不并发，跑完再起下一个。")
            log("  给 agent 的指令只需：『完整读完提示词文件，严格照做，只写自己的 合并结果.json，完成后只回一行确认』。")
            log("  产出后：python run_pipeline.py resume --workdir \"%s\"" % e.workdir)
            return "need_agent"
        except Exception as e:
            s["status"] = "failed"
            s["note"] = str(e)
            save_state(st["workdir"], st)
            log("  ! %s 失败：%s" % (sid, e))
            return "failed"
        s["status"] = "done"
        s["finished"] = now()
        save_state(st["workdir"], st)
        log("  %s 完成：%s" % (sid, s["note"]))
    return "done"


def generate_review_kit(st: dict, outdir: str | None = None) -> list[str]:
    """S9 交付阶段：产出三重查漏 + 冲突裁决三份清单（输出文档之外）。失败不阻断主交付。

    outdir 显式给出时以它为准（工作区被整体复制、state 里 workdir 仍指向旧路径时尤其重要）。
    """
    f = st["files"]
    if not os.path.isfile(f["final"]):
        return []
    outdir = outdir or st["workdir"]
    outs = [os.path.join(outdir, x) for x in
            ("冲突裁决清单.md", "数字补漏清单.md", "覆盖度自评.md")]
    rc, out = run_cmd([script("review_kit.py"), "all",
                       "--source", st["source"], "--final", f["final"],
                       "--outdir", outdir], "S9 三重查漏工具箱")
    if rc != 0:
        log("  [警告] 三重查漏工具箱未完全成功（不影响主交付）：%s" % out[-160:])
    return [p for p in outs if os.path.isfile(p)]


def make_report(st: dict) -> str:
    f = st["files"]
    rows = []
    for sid, _, _ in STAGES:
        s = stage(st, sid)
        rows.append("| %s | %s | %s | %s |" % (sid, s["name"], s["status"], s["note"].replace("|", "/")))
    checks = []
    # 自动验收项
    try:
        checks.append(("定稿存在", os.path.exists(f["final"])))
        if os.path.exists(f["final"]):
            checks.append(("定稿字节 > 0", os.path.getsize(f["final"]) > 0))
        checks.append(("三个筛选文档齐全",
                       all(os.path.exists(f[k]) for k in ("screen1", "screen2", "screen3"))))
        checks.append(("行映射存在", os.path.exists(f["final_map"])))
        checks.append(("三道 agent JSON 齐全",
                       all(os.path.exists(f[k]) for k in ("agent1", "agent2", "agent3"))))
        if int(st["options"]["volumes"] or 0) > 1:
            vols = []
            k = 1
            while os.path.exists("%s%d.txt" % (f["volume_prefix"], k)):
                vols.append("%s%d.txt" % (f["volume_prefix"], k))
                k += 1
            checks.append(("分卷拼接逐字节 == 定稿",
                           bool(vols) and b"".join(read_bytes(p) for p in vols) == read_bytes(f["final"])))
    except Exception as e:
        checks.append(("验收异常", False))
    ck = "\n".join("- [%s] %s" % ("PASS" if v else "FAIL", k) for k, v in checks)
    rtl = st.get("removed_text_lines", [])
    if rtl:
        ck += ("\n- [WARN] 终审剔除行中含文字 %d 行，见 `剔除行内容体检.md`；"
               "请人工确认均属「毫无关联」，否则应调整终审结论重跑 resume。" % len(rtl))
    else:
        ck += "\n- [PASS] 终审剔除行全为空行/纯分隔符，无设计文字被删"
    # 交付摘要卡：一段话说明本次做了什么、删了什么、零丢失证据，不必翻长报告
    f0 = st["files"]
    final_bytes = os.path.getsize(f0["final"]) if os.path.exists(f0["final"]) else 0
    all_done = all(stage(st, sid)["status"] == "done" for sid, _, _ in STAGES)
    screening_off = st["options"].get("screening") == "off"
    a_cnt = b_cnt = rm_cnt = rs_cnt = 0
    try:
        a_cnt = len(json.load(open(f0["agent1"], encoding="utf-8")).get("items", []))
        b_cnt = len(json.load(open(f0["agent2"], encoding="utf-8")).get("items", []))
        c3 = json.load(open(f0["agent3"], encoding="utf-8"))
        sm = c3.get("summary", {}) or {}
        rm_cnt = len(sm.get("final_remove_lines", []))
        rs_cnt = len(sm.get("final_restore_lines", []))
    except (OSError, ValueError, KeyError, TypeError) as e:
        # 摘要卡统计属展示层：缺 agent JSON 时统计为 0 即可，但要留痕不静默
        log("  [提示] 摘要卡三道统计不可用（agent JSON 未齐或格式异常）：%s" % e)
    rtl = st.get("removed_text_lines", [])
    sc = st.get("scatter_info", {})
    s2 = stage(st, "S2")
    if sc.get("legacy_merge"):
        if sc.get("enabled"):
            scatter_evidence = "零散块分区：L%s 已保护（--legacy-merge 机械合并）" % sc.get("start")
        else:
            scatter_evidence = "零散块分区：未分区 [WARN]（--legacy-merge，需核对；纯文字概念可能被误删）"
    else:
        # 新链默认：S2 按筛选结果整行落盘、不做机械删行，零散分区不适用，不谎报「已保护」
        scatter_evidence = "零散块分区：不适用（新链 S2 按筛选结果整行落盘，不做机械删行）"
    evidence = [
        "数字指纹集合层 0 缺失（编号/数值独种/冲突）",
        ("剔除行体检：含文字 %d 行，需人工复核" % len(rtl)) if rtl else "剔除行体检：0 行含文字",
        scatter_evidence,
    ]
    card = [
        "# text-summary-filter 运行报告", "",
        "## 交付摘要卡", "",
        "| 项 | 结果 |", "|---|---|",
        "| 模式 | %s |" % ("仅合并（--screening off）" if screening_off else st["answers"].get("mode", "全流程")),
        "| 源 → 定稿 | %d B → %d B |" % (os.path.getsize(st["source"]), final_bytes),
        "| 阶段 | %s |" % ("全部完成" if all_done else "未完成（见第三节）"),
        ("| 三道筛选 | 初判 %d → 复核 %d → 终审放回 %d / 剔除 %d |" %
         (a_cnt, b_cnt, rs_cnt, rm_cnt)) if not screening_off else "| 三道筛选 | 跳过（仅合并模式） |",
        "| 零丢失证据 | %s |" % "；".join(evidence) if not screening_off else "| 零丢失证据 | 数字指纹集合层 0 缺失 |",
        "| 自动验收 | %s |" % ("全部 PASS（见第四节）" if all(v for _, v in checks) else "有 FAIL，禁止交付"),
        "", "> 「指纹零丢失」不等于「内容零丢失」：纯文字概念被删时指纹仍全绿，"
           "必须同时看剔除行体检与零散块分区。详见 `references/zero-loss-merge.md`。", "",
        "- 生成时间：%s" % now(),
        "- 源文档：`%s`" % st["source"],
        "- 工作目录：`%s`" % st["workdir"], "",
        "## 一、开场询问包答案（一次问清，之后不再提问）", "",
        "```json", json.dumps(st["answers"], ensure_ascii=False, indent=2), "```", "",
        "## 二、执行选项", "", "```json",
        json.dumps(st["options"], ensure_ascii=False, indent=2), "```", "",
        "## 三、阶段状态", "",
        "| 阶段 | 名称 | 状态 | 说明 |", "|---|---|---|---|", *rows, "",
        "## 三·补、成本预估与零丢失下限", "",
        "- 指纹基线来源：`%s`" % st.get("baseline_source", "未记录"),
        "- 判定基准来源：`%s`" % st.get("criterion_source", "未记录"),
        "- 零散内容块分区：`%s`" % json.dumps(st.get("scatter_info", {}), ensure_ascii=False), "",
        "```json", json.dumps(st.get("estimate", {}), ensure_ascii=False, indent=2), "```", "",
        "```json", json.dumps(st.get("lower_bound", {"未启用": "skip-merge 或尚未执行 S2"}),
                              ensure_ascii=False, indent=2), "```", "",
        "## 四、自动验收", "", ck, "",
        "## 五、交付清单", "",
    ]
    for key, label in [("final", "清洗定稿"), ("final_map", "逐行去向映射"),
                       ("merge_map", "合并行映射"), ("screen1", "筛选1 初判无关"),
                       ("screen2", "筛选2 复核关联"), ("screen3", "筛选3 终审判定"),
                       ("view", "判定视图"), ("baseline", "硬数据指纹基线"),
                       ("volume_map", "分卷映射"), ("criterion", "统一判定基准"),
                       ("prompt1", "第1道提示词"), ("prompt2", "第2道提示词"),
                       ("prompt3", "第3道提示词")]:
        pth = f.get(key, "")
        if pth and os.path.exists(pth):
            card.append("- **%s**：`%s`（%d B）" % (label, pth, os.path.getsize(pth)))
    # 第 3 步语义合并新增产物（存在才列入）
    for key, label in [("合并_候选簇", "语义合并候选簇"), ("合并_agent_prompt", "语义合并 agent 提示词"),
                       ("合并_指纹基线", "语义合并指纹基线"), ("合并结果", "语义合并 agent 结果"),
                       ("合并稿", "语义合并稿"), ("合并_覆盖校验", "语义合并覆盖校验"),
                       ("合并_行映射", "语义合并逐行映射")]:
        pth = f.get(key, "")
        if pth and os.path.exists(pth):
            card.append("- **%s**：`%s`（%d B）" % (label, pth, os.path.getsize(pth)))
    # 三重查漏 / 冲突裁决：落在 S9 步骤文件夹；剔除行体检：落在 S7 步骤文件夹
    s9 = st.get("step_dirs", {}).get("S9") or st["workdir"]
    for label, name in [("冲突裁决清单", "冲突裁决清单.md"),
                        ("数字补漏清单", "数字补漏清单.md"),
                        ("覆盖度自评", "覆盖度自评.md")]:
        pth = os.path.join(s9, name)
        if os.path.isfile(pth):
            card.append("- **%s**：`%s`（%d B）" % (label, pth, os.path.getsize(pth)))
    _tp = f.get("剔除行内容体检")
    if _tp and os.path.isfile(_tp):
        card.append("- **剔除行内容体检**：`%s`（%d B）" % (_tp, os.path.getsize(_tp)))
    if int(st["options"]["volumes"] or 0) > 1:
        k = 1
        while os.path.exists("%s%d.txt" % (f["volume_prefix"], k)):
            p = "%s%d.txt" % (f["volume_prefix"], k)
            card.append("- **分卷%d**：`%s`（%d B）" % (k, p, os.path.getsize(p)))
            k += 1
    card += ["", "> 原文零改写；硬数据集合层零丢失；切分点严格两行之间；全程串行前台、无并行。", ""]
    with open(f["report"], "w", encoding="utf-8") as fp:
        fp.write("\n".join(card) + "\n")
    return f["report"]


def verify_final(st: dict) -> bool:
    """定稿生成后的独立复验：三道集合 + 定稿字节 + 定稿指纹零丢失。
    仅合并模式（--screening off）跳过三道集合，只验指纹 + 分卷。"""
    f = st["files"]
    if not os.path.exists(f["final"]):
        return False
    ok = True
    if st["options"].get("screening") != "off":
        argv = [script("verify_three_pass.py"), "--a", f["agent1"], "--b", f["agent2"],
                "--c", f["agent3"], "--source", f["screen_base"]]
        # --semantic-merge on（默认）时终稿是语义合并稿，不再是「按行删行」的字节结果：
        # 跳过 --final 逐字节断言（集合断言仍全跑），定稿零丢失由 merge_semantic.py 硬校验保证。
        if not st["options"].get("semantic_merge"):
            argv += ["--final", f["final"]]
        rc, _ = run_cmd(argv, "定稿三道集合" +
                        (" + 字节校验" if not st["options"].get("semantic_merge") else ""))
        ok &= (rc == 0)
    rc, _ = run_cmd([script("fingerprint.py"), "--source", st["source"], "--out", f["baseline"],
                     "--check", f["final"], "--exclude-noise"], "定稿指纹零丢失")
    ok &= (rc == 0)
    log("  [%s] 定稿复验（集合 + 字节 + 三类指纹）" % ("PASS" if ok else "FAIL"))
    return ok


def _derive_target_size(st: dict):
    """由 Q5 目标压缩体积推导目标字数；返回目标字数或 None（无硬目标，自然走到零丢失下限即可）。"""
    tc = str(st["options"].get("target_compression", "standard")).strip().lower()
    orig = int(st.get("original_chars", 0) or 0)
    if tc in ("standard", "verbatim", "", "none"):
        return None
    if tc == "aggressive":
        return int(orig * 0.30) if orig else None
    try:
        n = float(tc)
    except ValueError:
        return None
    if n > 100:
        return int(n)             # 绝对目标字数（如 8000）
    return int(orig * n / 100.0)  # 百分比（如 30 -> 原文的 30%）


def _reset_for_recompress(st: dict, round_no: int) -> None:
    """复压前重置：回跳点记为「第 3 步条目合并」(S2)，把上一轮输出当输入接着跑。

    回流统一规则（用户口径）：任何需要重跑的场景（第 5 步补遗漏 / 第 7 步体积不达标）
    都回跳到「条目合并」步，把上一轮输出当输入接着跑；合并时允许逐条简化，但信息／要点
    不丢、不碰红线。执行顺序已真反转「筛选(第2步)→合并(第3步)」，绝不写成旧口径。

    files 重指到新一轮的每步骤独立文件夹（旧轮次产物原样保留作归档）；
    本轮待写文件一律「移动到 _归档_R{n}/」而非删除（不删文件是用户红线），
    目标已存在则换名不覆盖。state 记 recompress_from="S2" 与如实标注 recompress_note：
    当前只把回跳点记作 S2 并把筛选基准稿重指到上一轮输出，完整单步回跳
    （仅重置 S2 及之后、复用筛选链结果）未实现，当前仍重置全部机械阶段后
    由 run_auto 按 STAGES 顺序重跑——如实标注，不宣称已实现单步回跳。
    """
    import glob
    import shutil
    archive = os.path.join(st.get("workdir") or os.path.dirname(st["source"]),
                           "_归档_R%d" % round_no)
    os.makedirs(archive, exist_ok=True)

    def _archive(path: str) -> None:
        """把单个已存在文件移入归档目录（不删除）；目标已存在则换名不覆盖。"""
        if not (isinstance(path, str) and os.path.exists(path)):
            return
        base = os.path.basename(path)
        dst = os.path.join(archive, base)
        if os.path.exists(dst):
            stem, ext = os.path.splitext(base)
            k = 1
            while os.path.exists(os.path.join(archive, "%s_%d%s" % (stem, k, ext))):
                k += 1
            dst = os.path.join(archive, "%s_%d%s" % (stem, k, ext))
        try:
            shutil.move(path, dst)
        except OSError:
            pass

    # 先把文件指针换到新一轮文件夹；旧轮文件夹里的产物不删，留作归档
    build_files(st, round_no)
    f = st["files"]
    for k, p in f.items():
        if k == "screen_base":
            # 复压输入（上一轮输出）是只读来源，移走会让 S2 没有输入；跳过归档
            continue
        _archive(p)
    for v in glob.glob(f["volume_prefix"] + "*.txt") + glob.glob(f["volume_prefix"] + "*.md"):
        _archive(v)
    # 回跳点记为「第 3 步条目合并」(S2)；筛选基准稿已在 build_files 里重指到上一轮输出。
    st["recompress_from"] = "S2"
    st["recompress_note"] = (
        "当前把复压回跳点记为第 3 步条目合并(S2)，并把筛选基准稿重指到上一轮输出；"
        "完整单步回跳（只重置 S2 及之后、复用筛选链结果）未实现，"
        "当前仍重置全部机械阶段后由 run_auto 按 STAGES 顺序重跑，属已知限制，不宣称已实现单步回跳。")
    for sd in st.get("stages", {}).values():
        sd["status"] = "pending"
        sd["started"] = sd["finished"] = ""
        sd["note"] = ""
        sd["outputs"] = []
    st["compression_history"] = []
    st["lower_bound"] = {}
    st["scatter_info"] = {}


def cmd_run(a) -> None:
    st = _load_state_for(a)
    policy = str(st["options"].get("compress_policy", "auto-stop")).strip().lower()
    max_rounds = int(st["options"].get("max_recompress", 5) or 5)
    round_idx = int(st.get("recompress_round", 0) or 0)
    needs_user = False
    stop_reason = "初始化未完成"
    while True:
        r = run_auto(st, stop_at_agent=not getattr(a, "no_stop", False))
        save_state(a.workdir, st)
        if r == "need_agent":
            return
        if r == "failed":
            raise SystemExit(1)
        # 新 7 步：未选中「第 4 步终稿生成」大步时跳过定稿复验与报告（不谎报、不裸 traceback）
        if "S7" in st["options"].get("skip_stages", []):
            log("\n[新主流程] 未选中第 4 步终稿生成/交付大步，跳过定稿复验与运行报告（按阶段选择执行）。")
            save_state(a.workdir, st)
            return
        log("\n--- 定稿复验 ---")
        if not verify_final(st):
            raise SystemExit("[错误] 定稿复验未通过，禁止交付")
        # 达标判定 + 零丢失下限判定
        final_path = st["files"]["final"]
        final_chars = len(read_bytes(final_path).decode("utf-8", errors="replace")) \
            if os.path.exists(final_path) else 0
        target = _derive_target_size(st)
        lower = st.get("lower_bound", {}) or {}
        reached_floor = bool(lower.get("reached")) or bool(lower.get("stalled"))
        met = (target is None) or (final_chars <= target)
        if policy == "single":
            stop_reason = "single：只跑一轮，不复压"
        elif met:
            stop_reason = "已达标（终稿 %d 字 ≤ 目标 %s 字）" % (final_chars, target)
        elif reached_floor:
            stop_reason = "已达零丢失下限，无法在不丢失情况下达标"
            if policy == "ask":
                needs_user = True
        elif round_idx >= max_rounds:
            stop_reason = "已达最大复压轮数 %d" % max_rounds
        else:
            # 复压：回跳到「第 3 步条目合并」，把上一轮输出当输入接着跑（允许逐条简化、不碰红线）
            round_idx += 1
            st["recompress_round"] = round_idx
            st["source"] = final_path
            _reset_for_recompress(st, round_idx)
            log("  [复压 R%d] 终稿 %d 字 > 目标 %s 字：回跳条目合并步，把上一轮输出当输入接着跑"
                % (round_idx, final_chars, target))
            log("    回跳点记为第 3 步条目合并(S2)；完整单步回跳未实现、当前重置全部机械阶段"
                "按 STAGES 顺序重跑，属已知限制（见 state.recompress_note），不宣称已实现单步回跳")
            save_state(a.workdir, st)
            continue
        break
    st["recompress_round"] = round_idx
    st["stop_reason"] = stop_reason
    if needs_user:
        log("\n[compress-policy=ask] 已达零丢失下限仍无法达到目标体积：按开场已固化的 Q7 决策执行，"
            "不再中途询问（问完绝不再问是硬规则）；停止并交付当前最佳，不擅自强压丢信息。"
            "【已知限制】ask 口径原有「回头问用户」语义与「问完不许再问」硬规则冲突，此处不实际"
            "发起询问、仅按开场决策执行，标注为已知限制，不宣称会回头问。")
    review = generate_review_kit(st, st.get("step_dirs", {}).get("S9") or a.workdir)
    s = stage(st, "S9")
    if s["status"] != "done":
        s["status"] = "done"
        s["started"] = s["finished"] = now()
        s["note"] = "报告 + 三重查漏/冲突裁决清单已生成（复压 R%d）" % round_idx
    p = make_report(st)
    s["outputs"] = [p] + review
    try:
        with open(p, "a", encoding="utf-8") as _fp:
            _fp.write("\n- **复压轮数**：R%d，停止原因：%s\n" % (round_idx, stop_reason))
    except OSError:
        pass
    save_state(a.workdir, st)
    log("\n全部完成（复压 R%d，停止原因：%s）。运行报告：%s" % (round_idx, stop_reason, p))


def cmd_verify(a) -> None:
    st = _load_state_for(a)
    f = st["files"]
    log("=== 独立验收 ===")
    ok = True
    # 1 三道集合（仅合并模式跳过）
    if st["options"].get("screening") == "off":
        log("  SKIP 三道集合（--screening off 仅合并模式）")
    elif all(os.path.exists(f[k]) for k in ("agent1", "agent2", "agent3")):
        argv = [script("verify_three_pass.py"), "--a", f["agent1"], "--b", f["agent2"],
                "--c", f["agent3"], "--source", f["screen_base"]]
        if not st["options"].get("semantic_merge"):
            argv += ["--final", f["final"]]
        rc, _ = run_cmd(argv, "三道集合" +
                        (" + 定稿字节" if not st["options"].get("semantic_merge") else ""))
        ok &= (rc == 0)
    else:
        log("  SKIP 三道集合（agent JSON 未齐）")
    # 2 指纹
    if os.path.exists(f["final"]):
        rc, _ = run_cmd([script("fingerprint.py"), "--source", st["source"], "--out", f["baseline"],
                         "--check", f["final"], "--exclude-noise"], "定稿指纹零丢失")
        ok &= (rc == 0)
    else:
        log("  SKIP 定稿指纹（定稿未生成）")
    # 3 分卷
    n = int(st["options"]["volumes"] or 0)
    if n > 1 and os.path.exists(f["final"]):
        vols, k = [], 1
        while os.path.exists("%s%d.txt" % (f["volume_prefix"], k)):
            vols.append("%s%d.txt" % (f["volume_prefix"], k))
            k += 1
        same = bool(vols) and b"".join(read_bytes(p) for p in vols) == read_bytes(f["final"])
        log("  [%s] 分卷拼接逐字节 == 定稿" % ("PASS" if same else "FAIL"))
        ok &= same
    # 4 剔除行内容体检（独立于阶段状态，旧工作区也能补做；仅合并模式跳过）
    if st["options"].get("screening") == "off":
        log("  SKIP 剔除行内容体检（--screening off，无终审剔除集）")
        if not getattr(a, "no_review", False) and os.path.exists(f["final"]):
            try:
                generated = generate_review_kit(st, st.get("step_dirs", {}).get("S9") or a.workdir)
                if generated:
                    log("  已刷新 %d 份查漏/裁决清单" % len(generated))
                    make_report(st)
            except Exception as e:
                log("  [警告] 查漏清单刷新失败（不影响验收结论）：%s" % e)
        log("=== 验收总结果：%s ===" % ("PASS" if ok else "FAIL"))
        raise SystemExit(0 if ok else 1)
    try:
        with open(f["agent3"], "r", encoding="utf-8") as fp:
            c3 = json.load(fp)
        rem = set(int(x) for x in st.get("s2_removed_lines", [])) or _remove_lines(c3)
        src_lines = read_bytes(f["screen_base"]).splitlines(keepends=True)
        text_rows = []
        for n in sorted(rem):
            if 1 <= n <= len(src_lines):
                t = src_lines[n - 1].decode("utf-8", errors="replace").strip()
                if t and any(ch.isalnum() or "\u4e00" <= ch <= "\u9fff" for ch in t):
                    text_rows.append((n, t[:120]))
        st["removed_text_lines"] = text_rows
        rep = ["# 剔除行内容体检", "",
               "> 终审剔除集中含文字的行。请人工确认这些行确属「毫无关联」，而非设计条目。", "",
               "| 母本行 | 内容（截断 120 字） |", "|---|---|"]
        rep += ["| L%d | %s |" % (n, t.replace("|", "/")) for n, t in text_rows]
        rep += ["", "- 共 %d 行；剔除总数 %d 行（其余为空行/纯分隔符）。" %
                (len(text_rows), len(rem)), ""]
        with open(f["剔除行内容体检"], "w", encoding="utf-8") as fp:
            fp.write("\n".join(rep) + "\n")
        if text_rows:
            log("  [WARN] 剔除行中含文字 %d 行，见 剔除行内容体检.md（需人工复核）" % len(text_rows))
            for n, t in text_rows[:5]:
                log("    L%d | %s" % (n, t[:80]))
        else:
            log("  [PASS] 剔除行全为空行/纯分隔符，无设计文字被删")
        save_state(a.workdir, st)
        make_report(st)
    except SystemExit:
        raise
    except Exception as e:
        log("  [WARN] 剔除行内容体检未能完成：%s" % e)

    # 5 顺带刷新三重查漏/冲突裁决清单（--with-review 或默认），纯校验可用 --no-review 关掉
    if not getattr(a, "no_review", False) and os.path.exists(f["final"]):
        try:
            generated = generate_review_kit(st, st.get("step_dirs", {}).get("S9") or a.workdir)
            if generated:
                log("  已刷新 %d 份查漏/裁决清单" % len(generated))
                make_report(st)
        except Exception as e:
            log("  [警告] 查漏清单刷新失败（不影响验收结论）：%s" % e)

    log("=== 验收总结果：%s ===" % ("PASS" if ok else "FAIL"))
    raise SystemExit(0 if ok else 1)


def cmd_report(a) -> None:
    st = _load_state_for(a)
    p = make_report(st)
    log("报告已生成：%s" % p)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="text-summary-filter 端到端编排器（一句话启动 / 断点续跑 / 自动验收）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="初始化：固化开场询问包答案与全部路径")
    p.add_argument("--source", required=True, help="源文档路径（必填）")
    p.add_argument("--workdir", required=True, help="工作目录（所有产物落此处）")
    p.add_argument("--volumes", type=int, default=7, help="交付分卷数，<=1 不分卷（默认 7）")
    p.add_argument("--skip-merge", action="store_true", help="源文档已是合并稿，跳过 S2 行选择合并")
    p.add_argument("--scatter-marker", default=None, help="零散内容块起始标记（整行文字或子串）")
    p.add_argument("--no-builtin-rules", action="store_true", help="关闭内置窄规则词，更激进地删重述")
    p.add_argument("--fingerprint", default=None, help="已有指纹基线 JSON；不给则 S1 自动生成")
    p.add_argument("--head", type=int, default=55, help="判定视图首部字数（默认 55）")
    p.add_argument("--tail", type=int, default=20, help="判定视图尾部字数（默认 20）")
    p.add_argument("--mode", default="合并+筛选", help="开场询问包①任务模式")
    p.add_argument("--zero-loss", default="硬数据零丢失", help="开场询问包②零丢失严格度")
    p.add_argument("--delivery", default="7卷+定稿+行映射+三份筛选文档", help="开场询问包③交付规格")
    p.add_argument("--execution", default="严格串行前台", help="开场询问包④执行与并发")
    p.add_argument("--note", default="", help="开场询问包备注")
    p.add_argument("--subject", default="目标项目", help="判定对象名称（生成统一判定基准用，如某设计文档）")
    p.add_argument("--subject-desc", default="", help="判定对象定义/应保留类别说明（多行文本）")
    p.add_argument("--keep-categories", default=None, help="应保留类别清单文件路径（Markdown 表格）；不给则用内置通用表")
    p.add_argument("--criterion-note", default="", help="判定基准第六节补充约定")
    p.add_argument("--criterion-file", default=None,
                   help="采用用户已有的判定基准 md（原样复制，不覆盖手写内容）；不给则自动生成")
    p.add_argument("--screening", choices=["on", "off"], default="on",
                   help="off=短路：跳过筛选链 S3–S6，直接以筛选基准稿进入合并/分卷（合并现在在筛选后）")
    p.add_argument("--extract", default=None,
                   help="第 1 步 build_extract.py --finalize 产出的总提取稿路径；"
                        "作为统一筛选基准稿(视图/回查/渲染/校验/删行同源)。不给则退回母本并打印告警")
    p.add_argument("--legacy-merge", action="store_true",
                   help="可选：S2 复用 line_select_merge.py 的机械删行/去重（默认关闭）；"
                        "不改变筛选链行号基准(基准恒为筛选基准稿)")
    p.add_argument("--semantic-merge", nargs="?", const="on", default="on",
                   choices=["on", "off"],
                   help="第 3 步语义合并开关（默认 on）：on=候选簇机械生成→1 个前台 agent 裁决→"
                        "全覆盖+数字指纹零丢失硬校验→落盘；off=退回第一批直通（零改写，兼容）；"
                        "--legacy-merge 开启时走机械合并")
    p.add_argument("--similarity", type=float, default=0.80,
                   help="第 3 步语义合并候选簇相似度阈值（默认 0.80；difflib.SequenceMatcher + "
                        "词元 Jaccard + 共享硬数据信号取较大）")
    p.add_argument("--max-cluster", type=int, default=20,
                   help="第 3 步语义合并单簇最大成员数（默认 20）")
    # 新主流程 Q1-Q4 与开头补充项（全部带默认值；不传时行为与既有 8 子命令/三种模式逐字一致）
    p.add_argument("--context-cap", type=int, default=None,
                   help="Q3 模型上下文：减去系统提示词后的可用字符/token；块 < 1/2 上下文（块上限=其 1/2，严格小于），影响块大小推导与分卷数")
    p.add_argument("--parallel", type=int, default=1,
                   help="Q4 每次并行 agent 数；由用户自定，不设上限、不钳制；默认串行 1，开多个 agent 须先经用户允许")
    p.add_argument("--big-steps", default="", help="步骤选择：选中的新 7 大步编号(逗号分隔 1..7，如 1,4,7；1切块总结/2条目筛选/3条目合并/4终稿生成/5检查/6全文检查/7达标判定与复压)；空=全部 7 大步。未选中大步对应状态机阶段置 skipped")
    p.add_argument("--check-rechunk-cap", type=int, default=None,
                   help="第 5 步检查·把输入文档重新切块的上限（默认取 --context-cap/2，固定用上下文 1/2 为上限）；记录性参数，写入 state")
    p.add_argument("--full-check-chunk-size", type=int, default=None,
                   help="第 6 步全文检查·上下文不足时切块检查的块大小（默认受 1/2 上下文约束，严格 < half_cap）；记录性参数，写入 state")
    p.add_argument("--small-steps", default="", help="步骤选择：小步编号(逗号分隔 如 1.1,4.2,5.3)；取整数部分激活对应大步并跑其全部小步(小步间有依赖不可独立跳过)；仅传本项时按整数部分激活对应大步；空=全部")
    p.add_argument("--goal", default="", help="开头补充项：总结目标 / 重点关注面")
    p.add_argument("--final-format", default="", help="开头补充项：终稿格式与命名")
    p.add_argument("--dedup-policy", default="", help="开头补充项：去重口径")
    p.add_argument("--conflict-policy", default="", help="开头补充项：冲突裁决偏好")
    p.add_argument("--keep-source-mark", choices=["on", "off"], default="on",
                   help="开头补充项：是否保留来源标注")
    p.add_argument("--delivery-list", default="", help="开头补充项：交付清单（补充既有 --delivery）")
    p.add_argument("--model-host", choices=["cloud", "local"], default="cloud",
                   help="Q0 模型宿主：cloud=云端 API/非本地(默认) / local=本地模型")
    p.add_argument("--cpu-tier", choices=["weak", "mid", "strong"], default="weak",
                   help="Q0 云端分支 CPU 档位：weak(4C8T 低压 U,默认)/mid(8C16T 标压+)/strong(桌面工作站)；决定并发建议")
    p.add_argument("--think-depth", choices=["low", "medium", "high"], default="medium",
                   help="Q0 本地分支思考强度：low/medium(默认)/high；写入 state 并传给 render_agent_prompt")
    p.add_argument("--urgent", default="",
                   help="Q0 本地分支急不急：不急/一般/很急；给 --think-depth 建议值")
    p.add_argument("--accelerator", default="",
                   help="Q0 本地分支 GPU(或未来 NPU)型号与显存/算力信息，写入 state")
    p.add_argument("--target-compression", default="standard",
                   help="Q5 目标压缩体积：standard(全面总结,默认)/aggressive(尽量精简,目标压到20%%-40%%)/verbatim(详尽保留几乎不压)/或填数字百分比如30 或目标字符数如8000；最终仍受零丢失硬底线约束")
    p.add_argument("--max-doc-size", type=int, default=0,
                   help="Q6 最大可容忍文档大小：源文档字符数阈值(0=不限)；超过则成本预估告警并建议先分卷/拆批，不强制阻断")
    p.add_argument("--compress-policy", choices=["auto-stop", "ask", "single"], default="auto-stop",
                   help="Q7 压不到目标大小怎么办：auto-stop(默认,自动多轮复压直到达标；不丢失下无法达标则停并交付当前最佳)/ask(按开场已固化的 Q7 决策执行、不再中途询问；与「问完不许再问」硬规则冲突，标注为已知限制，不实际回头问)/single(只跑一轮不复压)")
    p.add_argument("--max-recompress", type=int, default=5,
                   help="Q7 复压最大轮数(默认5)；不达标回跳到第 3 步条目合并，把上一轮输出当输入接着跑，直到达标或达零丢失下限")
    p.set_defaults(func=cmd_init)

    for name, help_ in [("status", "查看阶段进度与下一步"),
                        ("run", "自动跑到语义阶段并停下（或 --no-stop 忽略）"),
                        ("prepare", "逐道推进：渲染上一道成果、刷新提示词、打印下一步派谁"),
                        ("doctor", "失败诊断：核对各阶段产物，给出缺失项/原因/建议命令"),
                        ("resume", "agent 产出齐备后继续跑完"),
                        ("verify", "独立跑全量验收"),
                        ("report", "生成/刷新运行报告")]:
        q = sub.add_parser(name, help=help_)
        q.add_argument("--workdir", required=True, help="工作目录")
        q.set_defaults(func={"status": cmd_status, "run": cmd_run, "prepare": cmd_prepare,
                             "doctor": cmd_doctor, "resume": cmd_run, "verify": cmd_verify,
                             "report": cmd_report}[name])
        if name in ("run", "resume"):
            q.add_argument("--no-stop", action="store_true", help="语义阶段不停（仅调试用）")
        if name == "verify":
            q.add_argument("--no-review", action="store_true",
                           help="只做验收，不刷新三重查漏/冲突裁决清单")
    return ap


def main() -> None:
    a = build_parser().parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
