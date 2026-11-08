# text-summary-filter

长文档信息提取与总结 Skill（WorkBuddy Skill）。

## 项目定位

把超长文档做「零丢失」的信息提取与总结。权威流程为 ⓿ 前置 ＋ 7 大步：

⓿ 前置（开场询问包分多回问齐 → 多源机械合并成母本 → 切分预算与上下文容量判定）＋ 1 切块总结（等宽切分，块严格小于模型上下文的 1／2，逐块只追加、全量提取）／2 条目筛选（按 Q8 目标筛选，事实要点全留）／3 条目合并（去重、合并互补、冲突裁决、统一整理）／4 终稿生成（渲染定稿、分卷、行级去向映射）／5 检查（补遗漏、重切块核查）／6 全文检查（遍历最终输出做最后修改）／7 达标判定与复压（体积不达标时回跳复压）。

「零丢失」指信息／要点不丢，**不是逐字节不丢**。现有校验手段为数字指纹零丢失、剔除行内容体检、零散内容块自动探测三层；但纯文字概念层未被程序证明，**不得宣称内容零丢失已被程序证明**（详见「已知限制」）。

> 语义阶段（三道筛选、条目合并裁决、第 5／6 步检查与全文检查）需要串行前台 agent 参与，并非「安装即可一键全自动」；编排器负责机械阶段与状态机。

## 安装

把整个 `text-summary-filter/` 目录放到 WorkBuddy 的 Skill 目录：

```
~/.workbuddy/skills/text-summary-filter/
```

即 `SKILL.md` 位于 `~/.workbuddy/skills/text-summary-filter/SKILL.md`。依赖仅 Python **3.10＋** 标准库（脚本含 PEP 604 联合类型注解，配合 `from __future__ import annotations`），**无第三方包**；已在 Python 3.13 下验证。

## 快速开始

一句话触发即可，例如「用 text-summary-filter 总结这份文档」。触发后先由 `route.py` 做意图路由，再由开场询问包**分多回问齐**（问齐即锁定、问完绝不再问），最后由编排器自动编排执行。

最小可复制命令示例（参数均取自 `init --help`）：

```bash
python scripts/run_pipeline.py init --source <文档路径> --workdir <工作目录> --subject "示例项目"
python scripts/run_pipeline.py status --workdir <工作目录>
python scripts/run_pipeline.py run --workdir <工作目录>
```

把 `<文档路径>`、`<工作目录>` 换成实际路径；`--subject` 用于生成统一判定基准。完整命令序列、参数映射与阶段状态表见 `references/quickstart.md`。

## 目录结构

| 路径 | 说明 |
| --- | --- |
| `SKILL.md` | 入口：frontmatter、第 0 节 ⓿ 前置 ＋ 7 大步流程表、开场询问包、红线速记 1～25 条、分支导航与工具索引 |
| `guide/` | 细则分支：红线全本、切分规格、合并规格、筛选与交付、工具索引与运维 |
| `references/` | 参考件：启动手册、三道筛选协议、零丢失合并、标记协议、输出模板、故障速查、总结方法论 |
| `scripts/` | 可执行脚本：编排器、切分／合并／指纹／筛选／渲染／自检与冒烟 |
| `examples/` | 示例源文档 `sample.md` |

## 自检

```bash
python scripts/skill_selfcheck.py
python scripts/smoke_test.py
```

`skill_selfcheck.py` 固定 9 项检查；单副本（未检测到第二份副本）时第 9 项标 `SKIP`，不计入 PASS／FAIL 且在报告中可见，显式指定 `--mirror` 可启用双副本逐文件比对。`smoke_test.py` 覆盖意图路由、初始化、边界失败模式等断言。两者全项通过才算完成。

## 已知限制

详见 `guide/red-lines.md` 的「当前实现边界」节。如实摘录：第 5／6 步的 agent 补遗漏与全文检查、第 7 步复压单步回跳目前只有流程定义与编排器挂载点，完整语义实现待补；判定基准口径固定，未消费 Q8 `--goal`；超大文档的判定视图分片兜底尚未实现，仍一次生成整份视图；「统一整理／全局摘要／去重／冲突裁决闭环」未实现；开场询问包部分参数目前仅记录进状态、不改变行为；「零丢失」证据只覆盖硬数据指纹集合层，不覆盖纯文字概念，不得宣称内容零丢失已被程序证明。以上与 `guide/red-lines.md`「当前实现边界」节一致，**不宣称已实现**。

## 协议与署名

- 许可：**AGPL-3.0-only**（GNU Affero General Public License，版本 3）。全文见根目录 `LICENSE`。
- 版权：`Copyright (c) 2026 text-summary-filter contributors`。
- 要求：**保留署名与许可声明**（`LICENSE` 全文与源文件头 SPDX 标记不得删除）；**修改后须以同协议开源并提供源码**；**允许二次分发**。
- **允许作为工具用于付费项目的文档总结**：可把本 Skill 及其产出用于付费项目开发，本许可不限制此类使用。
