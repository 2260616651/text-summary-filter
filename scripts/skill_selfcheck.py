# -*- coding: utf-8 -*-
# Copyright (c) 2026 text-summary-filter contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""text-summary-filter Skill 一键自检（纯标准库）。

检查项：
  1. 目录与必需文件齐全（SKILL.md / references / scripts / examples）
  2. frontmatter 合法（name / description 存在）
  3. 全部脚本语法可编译（用 compile()，不落盘、不用 nul）
  4. 全部脚本 --help 可运行（含编排器 8 个子命令）
  5. SKILL.md / references / guide 里出现的 --参数 都真实存在于脚本 add_argument
  6. SKILL.md / references 里反引号引用的 `xxx.py`、`xxx.md` 文件确实存在
  7. 第二份副本（若存在）逐文件 md5 一致
  8. 关键红线关键词在 SKILL.md 中仍有落点（防「删提示」回归）
  9. 端到端编排器子命令齐全

用法：
    python scripts/skill_selfcheck.py [--skill-dir <路径>] [--mirror <路径>] [--json <报告路径>]
退出码 0 = 全通过；1 = 有 FAIL。

第 9 项口径：在白名单口径之上叠加「全量差集透明化」——递归扫描两份副本
全部 .md/.py 生成 {相对路径: md5}，按目录名显式豁免 _backup/、__pycache__/；
除豁免外任何仅存在于单侧或两侧 md5 不同的 .md/.py 一律 FAIL 并列出具体相对路径，
PASS 时透明打印豁免目录与豁免文件清单（防「本该同步却漏同步仍报绿」）。
差集判定抽成纯函数 diff_relmaps()，自检内置 3 例纯内存回归，零落盘、零删除，不新增第 10 项。
单副本适配：未显式传 --mirror 且默认镜像目录不存在时，该项判 SKIP（不计 PASS/不计 FAIL，
报告里以 [SKIP] 显式列出）；显式指定 --mirror 时目录不存在仍判 FAIL。
第 9 项护栏：--skill-dir 与 --mirror 指向同一目录（自比）判 FAIL；真实差异一律 FAIL
并列出差异文件。检查项总数恒为 9 项，SKIP 不伪装成 PASS。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

DEFAULT_SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_MIRROR = os.path.expanduser(r"~\.workbuddy\skills\text-summary-filter")

# 全量差集扫描时按「目录名」显式豁免的历史/运行时目录；
# 非 .md/.py 的运行时产物（如 _user_meta.json、*.pyc）本来就不在扫描范围。
EXEMPT_DIRS = {"_backup", "__pycache__"}

REQUIRED_FILES = [
    "SKILL.md",
    "examples/sample.md",
    "references/summarization-methodology.md",
    "references/zero-loss-merge.md",
    "references/three-pass-screening.md",
    "references/troubleshooting.md",
    "references/quickstart.md",
    "references/output-template.md",
    "references/markup-protocol.md",
    "scripts/split_document.py",
    "scripts/merge_and_chunk.py",
    "scripts/fingerprint.py",
    "scripts/split_by_lines.py",
    "scripts/build_line_view.py",
    "scripts/render_from_lines.py",
    "scripts/verify_three_pass.py",
    "scripts/line_select_merge.py",
    "scripts/pipeline_templates.py",
    "scripts/run_pipeline.py",
    "scripts/skill_selfcheck.py",
    "guide/red-lines.md",
    "guide/splitting.md",
    "guide/merging.md",
    "guide/screening-delivery.md",
    "guide/tools-ops.md",
]

# 用户长期红线；缺任何一条都视为「提示被误删」
REDLINE_KEYWORDS = [
    "绝不切断行", "块内容零回流", "数字", "不删任何文档内信息", "来源必须标明",
    "并发", "429", "总结前先去重", "只追加", "统一整理", "保留前", "读一次写一次",
    "二次检查", "只改位置", "缩略档", "零改写", "行映射", "二进制", "CRLF",
    "判定基准", "拿不准", "零丢失下限", "保留版权与许可声明", "前台", "只回一行确认",
    "一句话启动", "run_pipeline.py", "line_select_merge.py", "quickstart.md",
    # 第三轮新增的语义护栏
    "剔除行内容体检", "零散内容块自动探测", "指纹零丢失", "不等于", "内容零丢失",
    "prepare", "route.py", "skill_selfcheck.py", "--screening", "no-auto-scatter",
]

SUBCOMMANDS = ["init", "status", "run", "prepare", "doctor", "resume", "verify", "report"]

# 新增脚本与文档也要纳入必需清单（已有项不重复追加）
REQUIRED_FILES += ["scripts/route.py", "scripts/build_extract.py",
                   "scripts/smoke_test.py", "scripts/review_kit.py"]


def md5(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def scan_relmap(root: str, exts: tuple[str, ...] = (".md", ".py")) -> dict[str, str]:
    """递归扫描 root 下所有 .md/.py，返回 {相对路径(正斜杠): md5}。

    扫描阶段不剔除任何目录（含 _backup/、__pycache__/），保证「全量」口径可见；
    豁免统一留到 diff_relmaps 里按目录名判定。md5 走二进制读，规避 CRLF 陷阱。
    """
    out: dict[str, str] = {}
    for base, _dirs, files in os.walk(root):
        for fn in files:
            if fn.endswith(exts):
                full = os.path.join(base, fn)
                rel = os.path.relpath(full, root).replace("\\", "/")
                out[rel] = md5(full)
    return out


def diff_relmaps(map_a: dict[str, str], map_b: dict[str, str],
                 exempt_dirs: set[str]) -> tuple[list[tuple[str, str]], list[str]]:
    """纯函数：比较两份 {相对路径: md5} 映射，返回 (diff, exempt_paths)。

    diff: [(相对路径, 原因)]，原因 ∈ {仅A侧, 仅B侧, md5不同}，按路径排序。
    exempt_paths: 命中豁免目录、未参与比较的相对路径（排序去重）。
    exempt_dirs 是目录名集合；相对路径任意一级「目录段」命中即豁免（文件名本身不算目录）。
    只依赖入参字典、不碰文件系统——便于用内存字典直接回归，零落盘、零删除。
    """
    exempt_dirs = set(exempt_dirs)

    def is_exempt(rel: str) -> bool:
        parts = rel.replace("\\", "/").split("/")
        return any(p in exempt_dirs for p in parts[:-1])

    exempt_paths = sorted({r for r in map_a if is_exempt(r)} |
                          {r for r in map_b if is_exempt(r)})
    keys_a = {r for r in map_a if not is_exempt(r)}
    keys_b = {r for r in map_b if not is_exempt(r)}
    diff: list[tuple[str, str]] = []
    for rel in sorted(keys_a | keys_b):
        if rel not in keys_b:
            diff.append((rel, "仅A侧"))
        elif rel not in keys_a:
            diff.append((rel, "仅B侧"))
        elif map_a[rel] != map_b[rel]:
            diff.append((rel, "md5不同"))
    return diff, exempt_paths


def selftest_diff_relmaps() -> tuple[bool, str]:
    """差集纯函数的 3 例纯内存回归；零落盘、零删除、不新建/删除任何文件。

    ① 单侧多一个普通 .py → 能被检出（仅A侧）
    ② _backup/ 下的差异 → 被豁免（不进 diff、进 exempt_paths）
    ③ 同路径 md5 不同 → 能被检出
    """
    ex = {"_backup", "__pycache__"}
    cases: list[tuple[str, bool]] = []

    d1, _ = diff_relmaps(
        {"scripts/a.py": "aaa", "SKILL.md": "m"}, {"SKILL.md": "m"}, ex)
    cases.append(("单侧普通.py可检出",
                  any(r == "scripts/a.py" and why == "仅A侧" for r, why in d1)))

    d2, ex2 = diff_relmaps(
        {"_backup/old.py": "x", "SKILL.md": "m"}, {"SKILL.md": "m"}, ex)
    cases.append(("_backup差异被豁免",
                  not any(r == "_backup/old.py" for r, _ in d2)
                  and "_backup/old.py" in ex2))

    d3, _ = diff_relmaps(
        {"scripts/a.py": "aaa"}, {"scripts/a.py": "bbb"}, ex)
    cases.append(("同路径md5不同可检出",
                  any(r == "scripts/a.py" and why == "md5不同" for r, why in d3)))

    bad = [name for name, ok in cases if not ok]
    return (not bad), ("3例全PASS" if not bad else "自测失败: " + ", ".join(bad))


def main() -> None:
    ap = argparse.ArgumentParser(description="text-summary-filter Skill 一键自检")
    ap.add_argument("--skill-dir", default=DEFAULT_SKILL, help="Skill 根目录")
    ap.add_argument("--mirror", default=None,
                    help="第二份副本；显式指定时不存在判 FAIL，未显式指定且默认镜像不存在则判 SKIP")
    ap.add_argument("--json", default=None, help="可选：把结果写成 JSON 报告")
    a = ap.parse_args()

    root = os.path.abspath(a.skill_dir)
    results: list[tuple[str, str, str]] = []

    def check(name: str, ok: bool | None, detail: str = "") -> None:
        status = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
        results.append((name, status, detail))

    # 1 必需文件
    missing = [f for f in REQUIRED_FILES if not os.path.isfile(os.path.join(root, f))]
    check("必需文件齐全", not missing, "缺: " + ", ".join(missing) if missing else
          "%d 个" % len(REQUIRED_FILES))

    sk_path = os.path.join(root, "SKILL.md")
    sk = open(sk_path, encoding="utf-8").read() if os.path.isfile(sk_path) else ""

    # 2 frontmatter
    fm = re.match(r"^---\s*\n(.*?)\n---\s*\n", sk, re.S)
    check("frontmatter 合法", bool(fm) and "name:" in (fm.group(1) if fm else "")
          and "description:" in (fm.group(1) if fm else ""),
          "%d 字符" % (len(fm.group(1)) if fm else 0))

    # 3 语法
    scripts = sorted(f for f in os.listdir(os.path.join(root, "scripts"))
                     if f.endswith(".py")) if os.path.isdir(os.path.join(root, "scripts")) else []
    bad_syntax = []
    for f in scripts:
        p = os.path.join(root, "scripts", f)
        try:
            compile(open(p, "rb").read(), p, "exec")
        except SyntaxError as e:
            bad_syntax.append("%s: %s" % (f, e))
    check("脚本语法全通过", not bad_syntax,
          "%d 个脚本" % len(scripts) + (" | " + "; ".join(bad_syntax) if bad_syntax else ""))

    # 4 --help 冒烟（顶层全部脚本 + 编排器 8 个子命令，合并为一项，检查数保持可控）
    bad_help = []
    for f in scripts:
        p = os.path.join(root, "scripts", f)
        try:
            r = subprocess.run([sys.executable, p, "--help"], capture_output=True,
                               timeout=25, text=True, encoding="utf-8", errors="replace")
            if r.returncode != 0:
                bad_help.append(f)
        except Exception as e:
            # 不静默吞异常：--help 子进程启动失败要留痕，便于定位环境问题
            print("[警告] 脚本 --help 调用异常：%s：%s" % (f, e), file=sys.stderr)
            bad_help.append(f)
    rp_help = os.path.join(root, "scripts", "run_pipeline.py")
    bad_sub = []
    if os.path.isfile(rp_help):
        for c in SUBCOMMANDS:
            try:
                r = subprocess.run([sys.executable, rp_help, c, "--help"], capture_output=True,
                                   timeout=25, text=True, encoding="utf-8", errors="replace")
                if r.returncode != 0:
                    bad_sub.append("%s(exit %d)" % (c, r.returncode))
            except Exception as e:
                bad_sub.append("%s(%s)" % (c, e))
    else:
        bad_sub.append("run_pipeline.py 不存在")
    sub_note = "%d 个子命令 --help：%s" % (
        len(SUBCOMMANDS), "全部 exit 0" if not bad_sub else "失败 " + ", ".join(bad_sub))
    check("脚本 --help 可运行(含编排器子命令)", not bad_help and not bad_sub,
          "%d 个脚本" % len(scripts) +
          (" | 失败: " + ", ".join(bad_help) if bad_help else "") + " | " + sub_note)

    # 5 参数真实存在
    real_args: set[str] = set()
    for f in scripts:
        s = open(os.path.join(root, "scripts", f), encoding="utf-8").read()
        real_args |= set(re.findall(r"add_argument\(\s*[\"'](--[a-z][a-z0-9-]*)", s))
    docs = sk
    refdir = os.path.join(root, "references")
    if os.path.isdir(refdir):
        for f in os.listdir(refdir):
            if f.endswith(".md"):
                docs += "\n" + open(os.path.join(refdir, f), encoding="utf-8").read()
    guidedir = os.path.join(root, "guide")
    if os.path.isdir(guidedir):
        for f in os.listdir(guidedir):
            if f.endswith(".md"):
                docs += "\n" + open(os.path.join(guidedir, f), encoding="utf-8").read()
    used_args = set(re.findall(r"--[a-z][a-z0-9-]*", docs))
    # argparse 内置参数与通用 CLI 约定，不属于「脚本自定义参数」，不算编造
    BUILTIN_ARGS = {"--help", "--version", "--verbose", "--quiet", "--json"}
    phantom = sorted(used_args - real_args - BUILTIN_ARGS)
    check("文档参数零编造", not phantom,
          "文档用 %d 个 / 脚本有 %d 个" % (len(used_args), len(real_args)) +
          (" | 不存在: " + ", ".join(phantom) if phantom else ""))

    # 6 文档引用的 Skill 内文件存在
    # 只检查「确实声称在 Skill 包内」的文件名，排除历史脚本名/示例文件名/占位符。
    # 判定：带 scripts/ 或 references/ 或 examples/ 前缀的引用，或名字在本次包内的脚本/references 清单里。
    refs = set(re.findall(r"`((?:scripts/|references/|examples/)?[A-Za-z0-9_\-]+\.(?:py|md|txt|json))`", docs))
    known = set(scripts) | set(os.listdir(refdir) if os.path.isdir(refdir) else []) | {
        "SKILL.md", "chunks.json", "sample.md", "pipeline_state.json",
        "_merge_state.json", "运行报告.md", "筛选_判定基准.md", "筛选_判定视图.txt",
        "agent1_result.json", "agent2_result.json", "agent3_result.json",
        "agent1_prompt.md", "agent2_prompt.md", "agent3_prompt.md",
        "筛选1_初判无关条目.md", "筛选2_复核关联条目.md", "筛选3_终审判定.md",
        "筛选定稿.txt", "硬数据指纹_基线.json", "merge_state.json",
    }
    # 明确排除：历史实战脚本名、示例源文档、占位符（这些是描述性引用，不要求在包内存在）
    HISTORICAL = {
        "build_fingerprint.py", "split_and_map.py", "split_into_volumes.py",
        "merge_v9.py", "merge_v2.py", "extract_numbers.py", "audit_numbers.py",
        "verify_numbers2.py", "extract_facts.py", "cross_check.py", "selfcheck5.py",
        "dedup_adjacent.py", "dedup_safe.py", "merge_summaries.py", "make_index.py",
        "chat_log_01.txt", "chat_log_02.txt", "chunk_xxx.txt", "chunk_NNN.txt",
        "chunk_001.txt", "group_XX.md", "group_14.md", "agentN_result.json",
        "a.md", "b.md", "c.md", "report.md", "1.txt", "总总结.txt",
    }
    not_found = []
    checked = 0
    for r in sorted(refs):
        base = os.path.basename(r)
        # 带目录前缀的引用**优先**检查（无论文件名是否已知），确保 references/ scripts/ 引用真实存在
        if "/" in r:
            if base in HISTORICAL or "_NNN" in base or "_XX" in base or "*" in base:
                continue
            checked += 1
            if not os.path.isfile(os.path.join(root, r)):
                not_found.append(r)
            continue
        if base in HISTORICAL or base in known or "_NNN" in base or "_XX" in base or "*" in base:
            continue
        # 纯文件名且以本 Skill 脚本前缀命名的才检查
        if re.match(r"^(line_|split_|build_|render_|verify_|fingerprint_|run_|skill_|pipeline_|merge_and_)", base):
            checked += 1
            cand = [os.path.join(root, d, base) for d in ("", "scripts", "references", "examples")]
            if not any(os.path.isfile(c) for c in cand):
                not_found.append(r)
    check("文档引用的 Skill 内文件存在", not not_found,
          "检查 %d 个包内引用" % checked + (" | 未找到: " + ", ".join(not_found[:12]) if not_found else ""))

    # 7 红线关键词：主文件 + 全部 references + 全部 guide（细节下沉后仍算「在 Skill 包内」）
    all_doc = sk
    for _f in (os.listdir(refdir) if os.path.isdir(refdir) else []):
        if _f.endswith(".md"):
            all_doc += "\n" + open(os.path.join(refdir, _f), encoding="utf-8").read()
    if os.path.isdir(guidedir):
        for _f in os.listdir(guidedir):
            if _f.endswith(".md"):
                all_doc += "\n" + open(os.path.join(guidedir, _f), encoding="utf-8").read()
    miss_red = [k for k in REDLINE_KEYWORDS if k not in all_doc]
    main_only = sum(1 for k in REDLINE_KEYWORDS if k not in sk)
    check("红线提示零丢失", not miss_red,
          "%d 个关键词（主文件命中 %d，其余下沉在 references）" %
          (len(REDLINE_KEYWORDS), len(REDLINE_KEYWORDS) - main_only) +
          (" | 全包仍缺: " + ", ".join(miss_red) if miss_red else ""))

    # 8 编排器子命令
    rp = os.path.join(root, "scripts", "run_pipeline.py")
    rps = open(rp, encoding="utf-8").read() if os.path.isfile(rp) else ""
    miss_cmd = [c for c in SUBCOMMANDS if ('"%s"' % c) not in rps and ("'%s'" % c) not in rps]
    check("编排器子命令齐全", not miss_cmd,
          "%d 个" % len(SUBCOMMANDS) + (" | 缺: " + ", ".join(miss_cmd) if miss_cmd else ""))

    # 9 两副本同步：白名单口径 + 全量 .md/.py 差集透明化 + 差集纯函数内存自测
    # 单副本适配：未显式传 --mirror 且默认镜像目录不存在 → SKIP（不计 PASS/不计 FAIL）。
    # 护栏保留：显式传 --mirror 时目录不存在仍 FAIL；自比（同一目录）仍 FAIL；真实差异仍 FAIL。
    mirror_explicit = a.mirror is not None
    mirror = os.path.abspath(a.mirror if a.mirror is not None else DEFAULT_MIRROR)
    selftest_ok, selftest_detail = selftest_diff_relmaps()
    same_dir = (os.path.normcase(os.path.abspath(root)) ==
                os.path.normcase(os.path.abspath(mirror)))
    if same_dir:
        # 自比：绝不能计入 PASS（护栏不许回退）
        check("两副本逐文件一致(全量差集)", False,
              "检测到 --skill-dir 与 --mirror 指向同一目录，双副本比对无意义，"
              "请显式指定另一副本 | %s | 差集函数自测: %s" %
              (mirror, selftest_detail))
    elif not os.path.isdir(mirror) and not mirror_explicit:
        # 单副本模式：默认镜像不存在且未显式指定 → SKIP，不伪装成 PASS
        check("两副本逐文件一致(全量差集)", None,
              "单副本模式：未检测到镜像副本，跳过双写比对；显式指定 --mirror 可启用 | "
              "差集函数自测: %s" % selftest_detail)
    elif not os.path.isdir(mirror):
        # 显式指定但目录不存在：仍判 FAIL
        check("两副本逐文件一致(全量差集)", False,
              "镜像目录不存在，无法完成双副本比对：%s | 差集函数自测: %s" %
              (mirror, selftest_detail))
    else:
        map_a = scan_relmap(root)
        map_b = scan_relmap(mirror)
        diff, exempt_paths = diff_relmaps(map_a, map_b, EXEMPT_DIRS)
        compared = len(set(map_a) | set(map_b)) - len(exempt_paths)
        exempt_dir_note = "豁免目录: " + ", ".join(sorted(d + "/" for d in EXEMPT_DIRS))
        exempt_files_note = "豁免 %d 个文件" % len(exempt_paths)
        if exempt_paths:
            exempt_files_note += ": " + ", ".join(exempt_paths[:10])
            if len(exempt_paths) > 10:
                exempt_files_note += " …(+%d)" % (len(exempt_paths) - 10)
        parts = ["%d 个 .md/.py 参与比对" % compared,
                 exempt_dir_note, exempt_files_note,
                 "差集函数自测: " + selftest_detail]
        if diff:
            parts.append("不一致(%d): " % len(diff) +
                         ", ".join("%s(%s)" % (r, why) for r, why in diff[:10]))
            if len(diff) > 10:
                parts.append("…(+%d)" % (len(diff) - 10))
        ok = (not diff) and selftest_ok
        check("两副本逐文件一致(全量差集)", ok, " | ".join(parts))

    # 输出
    print("=== text-summary-filter Skill 自检 ===")
    print("目录:", root)
    for name, status, detail in results:
        print("  [%s] %-28s %s" % (status, name, detail))
    passed = sum(1 for _, s, _ in results if s == "PASS")
    failed = sum(1 for _, s, _ in results if s == "FAIL")
    skipped = sum(1 for _, s, _ in results if s == "SKIP")
    print("=== 总结果：%d/%d PASS ===" % (passed, len(results)))
    if skipped:
        print("=== 其中 SKIP %d 项（不计入 PASS / 不计入 FAIL）：%s ===" %
              (skipped, ", ".join(n for n, s, _ in results if s == "SKIP")))

    if a.json:
        os.makedirs(os.path.dirname(os.path.abspath(a.json)), exist_ok=True)
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump({"root": root,
                       "checks": [{"name": n, "status": s, "ok": s == "PASS", "detail": d}
                                  for n, s, d in results],
                       "passed": passed, "failed": failed, "skipped": skipped,
                       "total": len(results)},
                      f, ensure_ascii=False, indent=2)
        print("JSON 报告:", a.json)

    raise SystemExit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
