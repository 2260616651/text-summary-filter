# -*- coding: utf-8 -*-
# Copyright (c) 2026 text-summary-filter contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""一句话启动链路冒烟测试：证明「触发 → 路由 → 4问参数 → 编排 → 交付」没被改坏。

纯标准库；只读 Skill 包 + 在临时目录产出，不改任何既有文档。
覆盖：
  1. route.py 对 5 类典型说法给出正确模式与参数；另有 35 条表达鲁棒性回归（否定式/口语省略/限定词，纯内存零落盘零删除）
  2. run_pipeline.py init 能生成判定基准 / 成本预估 / 状态文件
  3. run → S4 停住时三份提示词与基准都已落盘、路径真实存在
  4. prepare 幂等、能渲染文档 A 并刷新提示词
  5. 三份 agent JSON（可用 --agent-dir 提供现成样本）齐备后 resume 跑完 S5–S9
  6. verify 全 PASS、运行报告含交付摘要卡与零丢失证据
  7. skill_selfcheck.py 9/9 PASS
  8. 边界：空输入明确拒绝（split_by_lines 对 0 字节文件非零退出、不产空卷）
  9. 边界：CRLF 源按行二进制切卷后拼接逐字节还原

用法：
    python scripts/smoke_test.py [--agent-dir <含 agent1/2/3_result.json 的目录>] \
                                 [--out <临时目录>] [--keep]
不带 --agent-dir 时只跑到 S4（验证前半链）；带了才跑完整链。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
PY = sys.executable


def run(cmd: list[str]) -> tuple[int, str]:
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def main() -> None:
    ap = argparse.ArgumentParser(description="一句话启动链路冒烟测试")
    ap.add_argument("--agent-dir", default=None,
                    help="含 agent1/2/3_result.json 的目录；不给则只验证到 S4")
    ap.add_argument("--out", default=None, help="临时工作目录；不给则自动建")
    ap.add_argument("--keep", action="store_true", help="保留临时目录（默认清理）")
    a = ap.parse_args()

    wd = a.out or tempfile.mkdtemp(prefix="ds_smoke_")
    os.makedirs(wd, exist_ok=True)
    src = os.path.join(SKILL, "examples", "sample.md")
    results: list[tuple[str, bool, str]] = []

    def ck(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, bool(ok), detail))
        print("  [%s] %-34s %s" % ("PASS" if ok else "FAIL", name, detail))

    print("=== text-summary-filter 一句话启动链路冒烟测试 ===")
    print("  工作目录:", wd, "\n")

    # 1 意图路由
    cases = [
        ("把这份文档合并压缩并筛选", "合并+筛选全流程", "--mode 合并+筛选"),
        ("只筛选", "仅内容筛选", "--skip-merge"),
        ("只合并", "仅条目合并", "--screening off"),
        ("照旧", "合并+筛选全流程", "--volumes 7"),
        ("多份源文档先拼接再总结", "完整总结链", "完整总结链"),
    ]
    route_ok = True
    for text, mode, token in cases:
        rc, out = run([PY, os.path.join(HERE, "route.py"), "--text", text,
                       "--source", src, "--workdir", wd])
        good = rc == 0 and ("判定模式 : " + mode) in out and token in out
        route_ok &= good
        if not good:
            print("    路由失败:", text, "|", out[:200])
    ck("意图路由 5 类说法", route_ok)

    # 1b 表达鲁棒性回归：35 条真实中文说法（单意图/否定式/多意图混合/口语省略/别·不·免·只）
    # 纯内存：直接 import route 模块调 route() 函数，不落盘、不新建/删除任何文件。
    # (说法, 期望模式, 期望 ask_user)；ask=True 表示「未命中关键词→按设计转开场询问包 Q1 确认」。
    sys.path.insert(0, HERE)
    import route as _route
    route_cases = [
        ("只合并", "仅条目合并", False),
        ("只筛选", "仅内容筛选", False),
        ("照旧", "合并+筛选全流程", False),
        ("多份源文档先拼接再总结", "完整总结链", False),
        ("把这份文档合并压缩并筛选", "合并+筛选全流程", False),
        ("压缩一下", "仅条目合并", False),
        ("去掉无关内容", "仅内容筛选", False),
        ("帮我把内容筛选一遍", "仅内容筛选", False),
        ("从头总结", "完整总结链", False),
        ("跑一遍", "合并+筛选全流程", False),
        ("只合并别筛选", "仅条目合并", False),
        ("帮我筛一下内容，合并就免了", "仅内容筛选", False),
        ("全文都要别删", "合并+筛选全流程", True),
        ("先不合并只挑重点", "仅内容筛选", False),
        ("只要筛选", "仅内容筛选", False),
        ("别筛选，只合并就行", "仅条目合并", False),
        ("不用合并，筛选一下就好", "仅内容筛选", False),
        ("合并压缩，不用筛选", "仅条目合并", False),
        ("整理成一份", "合并+筛选全流程", True),
        ("把这几份总结合起来顺便去掉废话", "合并+筛选全流程", True),
        ("合并并筛选", "合并+筛选全流程", False),
        ("把文档压一压", "仅条目合并", False),
        ("剔除无关条目", "仅内容筛选", False),
        ("一句话跑完", "合并+筛选全流程", False),
        ("默认", "合并+筛选全流程", False),
        ("合并压缩并筛选", "合并+筛选全流程", False),
        ("合并+筛选", "合并+筛选全流程", False),
        ("合并筛选", "合并+筛选全流程", False),
        ("合并压缩并筛一下", "合并+筛选全流程", True),
        ("照旧跑一遍", "合并+筛选全流程", False),
        ("只合并就行了别筛选", "仅条目合并", False),
        ("不用筛选，只合并就好", "仅条目合并", False),
        ("多篇文档先拼接", "完整总结链", False),
        ("筛一遍就行", "仅内容筛选", False),
        ("从头总结多份源", "完整总结链", False),
    ]
    route_reg_ok = True
    for text, mode, ask in route_cases:
        r = _route.route(text, "<源>", "<目录>")
        good = (r["mode"] == mode and r["ask_user"] == ask)
        route_reg_ok &= good
        if not good:
            print("    路由回归失败:", text, "| 得到", r["mode"], r["ask_user"],
                  "| 期望", mode, ask)
    ck("route 35 条表达回归（否定式/口语省略/限定词）", route_reg_ok,
       "%d 条" % len(route_cases))

    # 2 init（源文档复制到临时目录，使中间文件落盘目录也在临时目录内，随默认清理）
    src_local = os.path.join(wd, "sample.md")
    shutil.copy2(src, src_local)
    rc, out = run([PY, os.path.join(HERE, "run_pipeline.py"), "init",
                   "--source", src_local, "--workdir", wd, "--volumes", "0",
                   "--subject", "示例文档"])
    ck("init 成功", rc == 0)
    state_path = os.path.join(wd, "pipeline_state.json")
    st = json.load(open(state_path, encoding="utf-8")) if os.path.isfile(state_path) else {}
    f = st.get("files", {})
    sd = st.get("step_dirs", {})
    ck("判定基准已生成", os.path.isfile(f.get("criterion", "")))
    ck("成本预估与短路判定", "estimate" in st and "短文档短路" in str(st.get("estimate", {})),
       str(st.get("estimate", {}).get("短文档短路", "?")))
    ck("短文档自动短路", st.get("options", {}).get("auto_shortcut") is True)
    ck("中间文件进入步骤独立文件夹（非 workdir 平铺）",
       os.path.isfile(f.get("criterion", ""))
       and f.get("criterion", "").replace("\\", "/").startswith(
           os.path.dirname(src_local).replace("\\", "/"))
       and "/sample总结中间文件 - " in f.get("criterion", "").replace("\\", "/"))

    # 3 run 到 S4（短文档会一路跑完，需用大文档验证 S4 停）
    big = None  # 如需本地大文档验证 S4 停止，将相对路径填入下方元组（严禁写绝对路径）
    # for cand in (r"examples/big_sample.txt",):
    #     if os.path.isfile(cand):
    #         big = cand
    #         break
    if big:
        wd2 = os.path.join(wd, "full")
        os.makedirs(wd2, exist_ok=True)
        big_local = os.path.join(wd, "big.txt")
        shutil.copy2(big, big_local)
        run([PY, os.path.join(HERE, "run_pipeline.py"), "init",
             "--source", big_local, "--workdir", wd2, "--skip-merge",
             "--volumes", "0", "--screening", "on", "--subject", "示例",
             # 最小适配（第二批）：--semantic-merge 默认 on 会让 S2 多一处 agent 停靠，
             # 既有完整链断言按第一批直通口径关闭语义合并，保证既有 18 项行为不变。
             "--semantic-merge", "off"])
        st2 = json.load(open(os.path.join(wd2, "pipeline_state.json"), encoding="utf-8"))
        f2 = st2["files"]
        sd2 = st2.get("step_dirs", {})
        if a.agent_dir:
            for k in ("agent1", "agent2", "agent3"):
                p = os.path.join(a.agent_dir, k + "_result.json")
                if os.path.isfile(p):
                    shutil.copy2(p, f2[k])
        rc, out = run([PY, os.path.join(HERE, "run_pipeline.py"), "run", "--workdir", wd2])
        if a.agent_dir:
            ck("run 完整链跑完", rc == 0 and "全部完成" in out,
               "含三份 agent JSON")
            rc2, out2 = run([PY, os.path.join(HERE, "run_pipeline.py"), "verify",
                             "--workdir", wd2])
            ck("verify 全 PASS", rc2 == 0 and "验收总结果：PASS" in out2)
            rep = f2["report"]
            ck("交付摘要卡与零丢失证据",
               os.path.isfile(rep) and "## 交付摘要卡" in open(rep, encoding="utf-8").read()
               and "零丢失证据" in open(rep, encoding="utf-8").read())
            review = ["冲突裁决清单.md", "数字补漏清单.md", "覆盖度自评.md"]
            ck("三重查漏/冲突裁决清单齐备",
               all(os.path.isfile(os.path.join(sd2.get("S9", wd2), x)) for x in review),
               ", ".join(review))
            ck("清单已列入运行报告",
               os.path.isfile(rep) and all(x.replace(".md", "") in open(rep, encoding="utf-8").read()
                                          for x in review))
        else:
            ck("run 正确停在 S4", rc == 0 and "停在语义阶段 S4" in out)
            prompts = [f2["prompt1"], f2["prompt2"], f2["prompt3"]]
            ck("三份提示词与基准齐备",
               all(os.path.isfile(p) for p in prompts) and os.path.isfile(f2["criterion"]))
            # 提示词里的路径真实存在
            txt = "".join(open(p, encoding="utf-8").read() for p in prompts)
            # 路径分隔符归一化（Windows 反斜杠 vs 正斜杠）
            norm = txt.replace("\\", "/")
            view_path = f2["view"].replace("\\", "/")
            big_path = st2["source"].replace("\\", "/")
            ck("提示词引用的基准/视图/母本存在",
               view_path in norm and big_path in norm,
               "视图=%s 母本=%s" % (view_path in norm, big_path in norm))
            rc3, out3 = run([PY, os.path.join(HERE, "run_pipeline.py"), "prepare",
                             "--workdir", wd2])
            ck("prepare 幂等可跑", rc3 == 0 and "逐道推进状态" in out3)
            rc4, out4 = run([PY, os.path.join(HERE, "run_pipeline.py"), "doctor",
                             "--workdir", wd2])
            ck("doctor 能诊断 S4 未齐", rc4 == 0 and "S4" in out4)
    else:
        print("  [SKIP] 未找到大文档样本，跳过 S4/完整链验证")
        # 不静默吞掉这些检查项：显式记为 FAIL，避免「少跑几项却仍报全绿」的假绿。
        # 它们依赖外部大文档样本（见上方候选路径），缺失是环境配置问题而非脚本缺陷。
        for nm in ("run 正确停在 S4", "三份提示词与基准齐备",
                   "提示词引用的基准/视图/母本存在", "prepare 幂等可跑",
                   "doctor 能诊断 S4 未齐"):
            ck(nm, False, "未找到大文档样本，无法验证（非脚本缺陷）")

    # 4 Skill 自检
    rc, out = run([PY, os.path.join(HERE, "skill_selfcheck.py")])
    ck("Skill 自检 9/9", rc == 0 and "9/9 PASS" in out)

    # 5 边界失败模式（不依赖外部大文档样本，稳定可复跑；产物落在 wd 随临时目录清理）
    empty = os.path.join(wd, "edge_empty.txt")
    open(empty, "wb").close()  # 0 字节
    rc, out = run([PY, os.path.join(HERE, "split_by_lines.py"), "--source", empty,
                   "--volumes", "3", "--out-prefix", os.path.join(wd, "edge_vol_")])
    ck("空输入明确拒绝（非零退出且不产空卷）", rc != 0 and "拒绝" in out)

    crlf = os.path.join(wd, "edge_crlf.txt")
    crlf_raw = "第1行\r\n第2行\r\n第3行\r\n".encode("utf-8")
    open(crlf, "wb").write(crlf_raw)
    pfx = os.path.join(wd, "edge_crlf_vol_")
    rc, out = run([PY, os.path.join(HERE, "split_by_lines.py"), "--source", crlf,
                   "--volumes", "2", "--out-prefix", pfx])
    v1, v2 = pfx + "1.txt", pfx + "2.txt"
    joined = (open(v1, "rb").read() + open(v2, "rb").read()) \
        if (os.path.isfile(v1) and os.path.isfile(v2)) else b""
    ck("CRLF 源二进制拼接逐字节还原", rc == 0 and joined == crlf_raw,
       "拼接 %d B / 原始 %d B" % (len(joined), len(crlf_raw)))

    # 15 损坏 state 统一中文报错（非法 JSON / 缺字段 / 类型错 / 不存在·不可读）
    # 纯内存：直接 import run_pipeline 调 load_state，用 mock 把「文件读取」替换成内存字节，
    # 全程零落盘、零删除、不新建任何文件（用户红线）；捕获 SystemExit 校验提示内容。
    import io
    from unittest import mock
    import run_pipeline as _rp

    def _state_reaction(raw: bytes, exists: bool = True, open_exc=None) -> str:
        """把一份内存 state 交给 load_state，返回其处置结果标记；绝不碰文件系统。"""
        with mock.patch.object(_rp.os.path, "exists", return_value=exists):
            op = (mock.patch("builtins.open", side_effect=open_exc)
                  if open_exc is not None
                  else mock.patch("builtins.open", return_value=io.BytesIO(raw)))
            with op:
                try:
                    _rp.load_state("<虚拟工作目录>")
                except SystemExit as e:
                    return "EXIT:" + str(e.code)
                except Exception as e:  # 未收口成 SystemExit = 会裸 Traceback，判 FAIL
                    return "LEAK:%s:%s" % (type(e).__name__, e)
                return "NOEXIT"

    def _full_state(drop=None, override=None) -> bytes:
        s = {"workdir": "w", "source": "s", "files": {}, "options": {},
             "answers": {}, "stages": {}}
        if drop:
            s.pop(drop, None)
        if override:
            s.update(override)
        return json.dumps(s, ensure_ascii=False).encode("utf-8")

    state_cases = [
        ("非法JSON", b'{"workdir": "x", ', True, None, "JSON"),
        ("缺关键字段", _full_state(drop="files"), True, None, "关键字段"),
        ("顶层类型错", b"[1, 2, 3]", True, None, "JSON 对象"),
        ("子字段类型错", _full_state(override={"files": []}), True, None, "类型错误"),
        ("文件不存在", b"", False, None, "未找到"),
        ("不可读", b"", True, OSError("模拟权限不足"), "无法读取"),
    ]
    state_detail = []
    state_ok = True
    for label, raw, ex, exc, kw in state_cases:
        msg = _state_reaction(raw, exists=ex, open_exc=exc)
        good = (msg.startswith("EXIT:") and kw in msg
                and "[错误]" in msg and "init" in msg and "备份" in msg)
        state_ok &= good
        state_detail.append("%s:%s" % (label, "OK" if good else "FAIL(" + msg[:60] + ")"))
    ck("损坏 state 统一中文报错(4类·纯内存)", state_ok, "; ".join(state_detail))

    # 16 复制工作区 --workdir 适配（纯内存零落盘零删除）
    # 命题：6 个读 state 的子命令在 --workdir 指向新目录（工作区被整体复制）时，
    # 都经统一入口把 st.workdir / st.files 旧前缀对齐到新目录；source 不重映射；
    # 二者相等（正常主链）时严格 no-op。mock 掉全部 IO，wraps 保留真实重映射逻辑。
    _old_ws = os.path.join("F:", os.sep, "ds_r9_old_ws")
    _new_ws = os.path.join(tempfile.gettempdir(), "ds_r9_new_ws")
    _mother = os.path.join("D:", os.sep, "mother", "母本.txt")
    _keys = ("baseline", "merged", "merge_state", "merge_map", "view", "view_index",
             "agent1", "agent2", "agent3", "screen1", "screen2", "screen3",
             "final", "final_map", "volume_prefix", "volume_map", "report",
             "criterion", "prompt1", "prompt2", "prompt3")

    def _r9_state(wd):
        return {
            "version": 1, "workdir": wd, "source": _mother,
            "files": {k: os.path.join(wd, k + ".txt") for k in _keys},
            "options": {"volumes": 0, "screening": "off", "skip_merge": True,
                        "subject": "x", "head": 55, "tail": 20},
            "answers": {},
            "stages": {sid: {"name": n, "kind": k, "status": "done", "started": "",
                             "finished": "", "note": "", "outputs": []}
                       for sid, n, k in _rp.STAGES},
        }

    _cmds = {
        "status": _rp.cmd_status, "doctor": _rp.cmd_doctor,
        "prepare": _rp.cmd_prepare, "report": _rp.cmd_report,
        "verify": _rp.cmd_verify, "resume": _rp.cmd_run,
    }
    _real_reloc = _rp._relocate_state
    _ns = argparse.Namespace(workdir=_new_ws, no_review=False, no_stop=False)
    _r9_ok = True
    _r9_detail = []
    for _name, _fn in _cmds.items():
        _st = _r9_state(_old_ws)
        with mock.patch.object(_rp, "load_state", return_value=_st), \
             mock.patch.object(_rp, "_relocate_state", wraps=_real_reloc) as _spy, \
             mock.patch.object(_rp, "log"), \
             mock.patch.object(_rp, "save_state"), \
             mock.patch.object(_rp, "make_report",
                               return_value=os.path.join(_new_ws, "运行报告.md")) as _m_rep, \
             mock.patch.object(_rp, "generate_review_kit", return_value=[]), \
             mock.patch.object(_rp, "_render_one"), \
             mock.patch.object(_rp, "_refresh_prompts", return_value=[]), \
             mock.patch.object(_rp, "run_cmd", return_value=(0, "")), \
             mock.patch.object(_rp, "run_auto", return_value="done"), \
             mock.patch.object(_rp, "verify_final", return_value=True), \
             mock.patch.object(_rp.os.path, "exists", return_value=False), \
             mock.patch.object(_rp.os.path, "isfile", return_value=False), \
             mock.patch.object(_rp.os.path, "getsize", return_value=1):
            try:
                _fn(_ns)
            except SystemExit:
                pass
        _called = _spy.called and _spy.call_args[0][1] == _new_ws
        _wd_ok = _st["workdir"] == os.path.abspath(_new_ws)
        _files_ok = all(v.startswith(os.path.abspath(_new_ws))
                        for v in _st["files"].values() if isinstance(v, str))
        _src_ok = _st["source"] == _mother
        _rep_ok = (not _m_rep.called) or _m_rep.call_args[0][0]["files"]["report"].startswith(
            os.path.abspath(_new_ws))
        _good = _called and _wd_ok and _files_ok and _src_ok and _rep_ok
        _r9_ok &= _good
        _r9_detail.append("%s:%s" % (_name, "OK" if _good else "FAIL"))
    # 正常主链 no-op：--workdir == state.workdir 时逐字节不变
    _same = _r9_state(_old_ws)
    _before = json.dumps(_same, ensure_ascii=False, sort_keys=True)
    _real_reloc(_same, _old_ws)
    _noop = json.dumps(_same, ensure_ascii=False, sort_keys=True) == _before
    _r9_ok &= _noop
    _r9_detail.append("no-op:%s" % ("OK" if _noop else "FAIL"))
    ck("复制工作区 6 子命令 --workdir 适配(纯内存)", _r9_ok, "; ".join(_r9_detail))

    # 17 渲染失败必须非零退出且不谎报（纯内存零落盘零删除）
    # 复现 R10 假绿：run_cmd 返回非 0 时 _render_one 必须抛 SystemExit 收口，
    # 不得让 cmd_prepare 继续打印「文档 A 已渲染 / 派下一道」成功文案，也不得裸 Traceback。
    # 直接 import cmd_prepare，mock 掉全部 IO 与子进程，断言 SystemExit + 中文提示 + 无成功文案。
    import contextlib
    _r11_state = {
        "version": 1, "workdir": "<虚拟工作目录>", "source": "<母本>",
        # 最小适配（第一批）：新链 _render_one / _refresh_prompts 的行号基准改为 screen_base，
        # 这里只给 fixture 补该键；断言一条未删、检查强度不变。
        "files": {k: os.path.join("<虚拟工作目录>", k)
                  for k in ("merged", "screen_base", "agent1", "agent2", "agent3",
                            "screen1", "screen2", "screen3",
                            "prompt1", "prompt2", "prompt3")},
        "options": {}, "answers": {},
        "stages": {sid: {"name": n, "kind": k, "status": "pending", "started": "",
                         "finished": "", "note": "", "outputs": []}
                   for sid, n, k in _rp.STAGES},
    }
    _r11_ns = argparse.Namespace(workdir="<虚拟工作目录>", no_review=False, no_stop=False)
    _buf = io.StringIO()
    _caught = False
    _code = None
    _leak = None
    with mock.patch.object(_rp, "_load_state_for", return_value=_r11_state), \
         mock.patch.object(_rp, "run_cmd", return_value=(1, "[模拟] 渲染子进程失败")) as _m_run, \
         mock.patch.object(_rp.os.path, "exists", return_value=True), \
         mock.patch.object(_rp, "save_state"), \
         mock.patch.object(_rp, "_refresh_prompts", return_value=[]) as _m_refresh, \
         contextlib.redirect_stdout(_buf):
        try:
            _rp.cmd_prepare(_r11_ns)
        except SystemExit as e:
            _caught = True
            _code = e.code
        except Exception as e:  # 未收口成 SystemExit = 会裸 Traceback / 假绿，判 FAIL
            _leak = "%s:%s" % (type(e).__name__, e)
    _printed = _buf.getvalue()
    _msg = _code if isinstance(_code, str) else ""
    _all_out = _printed + _msg
    _nonzero = _caught and (_code not in (0, None))
    _hint_ok = ("错误" in _msg and "渲染失败" in _msg and "退出码" in _msg
                and "JSON" in _msg and "行号" in _msg)
    _no_false = ("已渲染" not in _all_out and "下一步" not in _all_out
                 and "Traceback" not in _all_out)
    _no_leak = _leak is None
    _stopped_early = _m_run.called and not _m_refresh.called
    _r11_ok = _nonzero and _hint_ok and _no_false and _no_leak and _stopped_early
    ck("渲染失败非零退出且不谎报(纯内存)", _r11_ok,
       "SystemExit=%s 非零=%s 中文提示=%s 无谎报=%s 无泄漏=%s 提前收口=%s" %
       (_caught, _nonzero, _hint_ok, _no_false, _no_leak, _stopped_early))

    # 18~21 第二批新增：第 3 步真语义合并（merge_semantic.py）回归（纯临时目录，不污染 Skill 包）
    ms = os.path.join(HERE, "merge_semantic.py")
    rp = os.path.join(HERE, "run_pipeline.py")
    ck("语义合并脚本 4 个 --help",
       all(run([PY, ms, *args, "--help"])[0] == 0
           for args in ([], ["prepare"], ["verify"], ["render"])))
    ih = run([PY, rp, "init", "--help"])[1]
    ck("init --help 含语义合并 3 参数",
       all(x in ih for x in ("--semantic-merge", "--similarity", "--max-cluster")))

    sm_wd = os.path.join(wd, "merge_e2e")
    os.makedirs(sm_wd, exist_ok=True)
    sm_src = os.path.join(sm_wd, "落盘稿.md")
    sm_lines = [
        "设备工作温度上限为 85℃，超过阈值会自动断电。",
        "设备的工作温度最高可到 85 摄氏度，越过阈值即自动断电。",
        "冷却风扇转速设定为 2400 RPM，允许误差 ±5%。",
        "散热风扇的转速配置为 2400 转/分，容差 ±5%。",
        "设计点 aa-101：版本号 v1.2.3，配比 3:1，范围 1~9。",
        "阈值 0.80，公式系数 2.5，编号 bb-202，单位 kg。",
        "电源功率标称 500W，实测 480W。",
        "同字段多值冲突：功率标称 500W，另一处记为 520W。",
        "【冲突：源稿矛盾】功率标称存在 500W 与 520W 两种记录。",
        "来源：原始稿 A（保留来源标注）。", "",
        "结论：以上为全部硬数据，不得遗漏。",
    ]
    open(sm_src, "wb").write(("\n".join(sm_lines) + "\n").encode("utf-8"))
    sm_out = os.path.join(sm_wd, "out")
    sm_res = os.path.join(sm_out, "合并结果.json")
    sm_base = os.path.join(sm_wd, "基线.json")
    run([PY, os.path.join(HERE, "fingerprint.py"), "--source", sm_src,
         "--out", sm_base, "--exclude-noise"])
    rc_p, _ = run([PY, ms, "prepare", "--input", sm_src, "--outdir", sm_out,
                   "--similarity", "0.80", "--max-cluster", "20", "--result", sm_res])
    cand_path = os.path.join(sm_out, "合并_候选簇.json")
    cand = json.load(open(cand_path, encoding="utf-8")) if os.path.isfile(cand_path) else {}
    cl_ok = rc_p == 0 and cand.get("cluster_count", 0) >= 1 and any(
        set(c["lines"]) >= {1, 2} or set(c["lines"]) >= {3, 4} for c in cand.get("clusters", []))
    ck("语义合并 prepare 抓到相似/同类候选簇", cl_ok, "簇数=%s" % cand.get("cluster_count"))

    sm_items = [
        {"text": "设备工作温度上限为 85℃（最高 85 摄氏度），超过阈值会自动断电。", "sources": [1, 2], "reason": "语义相近合并，数字 85 全留"},
        {"text": "冷却/散热风扇转速设定为 2400 RPM（2400 转/分），允许误差 ±5%。", "sources": [3, 4], "reason": "同类事物合并"},
        {"text": "设计点 aa-101：版本号 v1.2.3，配比 3:1，范围 1~9。", "sources": [5], "reason": "独立保留"},
        {"text": "阈值 0.80，公式系数 2.5，编号 bb-202，单位 kg。", "sources": [6], "reason": "独立保留"},
        {"text": "电源功率标称 500W，实测 480W。", "sources": [7], "reason": "独立保留"},
        {"text": "【冲突：源稿矛盾】同字段多值冲突：功率标称 500W，另一处记为 520W；功率标称存在 500W 与 520W 两种记录（多值全留）。", "sources": [8, 9], "reason": "冲突多值全留"},
        {"text": "来源：原始稿 A（保留来源标注）。", "sources": [10], "reason": "独立保留"},
        {"text": "", "sources": [11], "reason": "空行保留"},
        {"text": "结论：以上为全部硬数据，不得遗漏。", "sources": [12], "reason": "独立保留"},
    ]
    json.dump({"items": sm_items}, open(sm_res, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    sm_merged = os.path.join(sm_wd, "合并稿.md")
    sm_map = os.path.join(sm_wd, "合并_行映射.md")
    rc_v, out_v = run([PY, ms, "verify", "--input", sm_src, "--result", sm_res,
                       "--fingerprint", sm_base, "--outdir", sm_out])
    rc_r, _ = run([PY, ms, "render", "--input", sm_src, "--result", sm_res,
                   "--fingerprint", sm_base, "--out", sm_merged, "--map", sm_map])
    ck("语义合并 合规结果 verify+render 通过",
       rc_v == 0 and rc_r == 0 and "总结果：PASS" in out_v
       and os.path.isfile(sm_merged) and os.path.isfile(sm_map)
       and os.path.isfile(os.path.join(sm_out, "合并_覆盖校验.md")))
    bad1 = os.path.join(sm_wd, "bad_line.json")
    json.dump({"items": sm_items[:-1]}, open(bad1, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    rb1, ob1 = run([PY, ms, "verify", "--input", sm_src, "--result", bad1,
                    "--fingerprint", sm_base, "--outdir", sm_out])
    bad_items = [dict(x) for x in sm_items]
    bad_items[3] = {"text": "阈值 0.8，公式系数 2.5，编号 bb-202，单位 kg。", "sources": [6], "reason": "独立保留"}
    bad2 = os.path.join(sm_wd, "bad_num.json")
    json.dump({"items": bad_items}, open(bad2, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    rb2, ob2 = run([PY, ms, "verify", "--input", sm_src, "--result", bad2,
                    "--fingerprint", sm_base, "--outdir", sm_out])
    no_out = os.path.join(sm_wd, "should_not_exist.md")
    rb3, _ = run([PY, ms, "render", "--input", sm_src, "--result", bad1,
                  "--fingerprint", sm_base, "--out", no_out,
                  "--map", os.path.join(sm_wd, "no_map.md")])
    ck("语义合并 漏行号/丢数字硬校验拦住",
       rb1 != 0 and rb2 != 0 and rb3 != 0 and not os.path.isfile(no_out)
       and "FAIL" in ob1 and "FAIL" in ob2,
       "漏行 rc=%s 丢数字 rc=%s render rc=%s" % (rb1, rb2, rb3))

    # 流程级：--screening off 短路 → run 停 S2 语义合并 → 手工结果 → resume 全完成 + 阶段顺序不回退
    fl_wd = os.path.join(wd, "merge_flow")
    os.makedirs(fl_wd, exist_ok=True)
    fl_src = os.path.join(fl_wd, "样例.txt")
    open(fl_src, "wb").write(("\n".join(sm_lines) + "\n").encode("utf-8"))
    run([PY, rp, "init", "--source", fl_src, "--workdir", fl_wd,
         "--screening", "off", "--volumes", "0", "--semantic-merge", "on", "--subject", "样例"])
    fl_st = json.load(open(os.path.join(fl_wd, "pipeline_state.json"), encoding="utf-8"))
    order_ok = list(fl_st["stages"].keys()) == ["S0", "S1", "S3", "S4", "S5", "S6", "S2", "S7", "S8", "S9"]
    base_ok = fl_st["files"].get("screen_base") == fl_src
    rc_run, out_run = run([PY, rp, "run", "--workdir", fl_wd])
    stop_ok = rc_run == 0 and "停在第 3 步语义合并" in out_run
    json.dump({"items": sm_items}, open(fl_st["files"]["合并结果"], "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    rc_res, out_res = run([PY, rp, "resume", "--workdir", fl_wd])
    fl_st2 = json.load(open(os.path.join(fl_wd, "pipeline_state.json"), encoding="utf-8"))
    done_ok = (rc_res == 0 and "全部完成" in out_res
               and fl_st2["stages"]["S2"]["status"] == "done"
               and os.path.isfile(fl_st2["files"]["merged"])
               and os.path.isfile(fl_st2["files"]["final"]))
    ck("语义合并 流程级 S2 停靠/resume/不回退",
       order_ok and base_ok and stop_ok and done_ok,
       "顺序=%s 筛选基准=%s 停靠=%s 完成=%s" % (order_ok, base_ok, stop_ok, done_ok))

    passed = sum(1 for _, ok, _ in results if ok)
    print("\n=== 总结果：%d/%d PASS ===" % (passed, len(results)))
    if not a.keep and not a.out:
        shutil.rmtree(wd, ignore_errors=True)
        print("  临时目录已清理:", wd)
    raise SystemExit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
