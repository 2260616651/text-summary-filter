## 工具索引与运维

### 脚本用法

```bash
# 只看体量报告（自生专注力分析，不切）
python scripts/split_document.py --analyze report.md

# 切分并输出等宽、带重叠、带标记的分块到 out/ 目录
python scripts/split_document.py --sents-per-chunk 12 --out out report.md

# 手动指定块大小（覆盖自生专注力推导）
python scripts/split_document.py --chunk-size 800 --out out report.md

# 手动把重叠量钉死在 1-4kb 区间内的固定字符数，用 --overlap-chars
python scripts/split_document.py --overlap-chars 2048 --out out report.md

# 不要 OVERLAP 标记时用 --no-mark
python scripts/split_document.py --no-mark --out out report.md

# 块下限 40000（仅下限建议）、松散结构×1.5 放大、默认 1-4kb 重叠、单块严格 < 1/2 上下文（--context-cap 的 1/2 为上限；floor 不得凌驾该红线）
python scripts/split_document.py --min-chunk 40000 --looseness 1.5 --overlap-auto --context-cap 60000 --out out report.md
```

输出：`<out>/chunks.json`（含每块文本、目标块大小、重叠来源、重叠字符数）和分文件 `chunk_001.txt …`。

> **块 < 1/2 上下文硬红线（全局，代码必须真正执行）**：任何时候切出来的块都必须严格小于 `--context-cap` 的 1/2（第 1 步切块总结 / 第 5 步检查重切块 / 第 6 步切块检查均适用，第 5 步固定以 1/2 为上限）；`--min-chunk 40000` 只是下限建议，half_cap 更小时被忽略、块强制缩小并打印告警。钳制会打印 ideal / floor / cap / half_cap / 最终块大小 / 钳制原因。详见 `guide/splitting.md`。

#### 多源合并与递归重切（merge_and_chunk.py）

把多份源机械拼接成母本再切块，或把已合成的大总总结递归重切以便二次生成缩略文档（详见「多文档合并总结 + 机械合并/递归重切」）：

```bash
# 多源硬拼接 + 切块（24 篇终稿 / 多份缩略档合并为母本）
python scripts/merge_and_chunk.py --inputs a.md b.md c.md --out merged_chunks

# 已合成的总总结递归重切（超单批次上下文时）
python scripts/merge_and_chunk.py --file 总总结.txt --out chunks

# 只看体量（不切）
python scripts/merge_and_chunk.py --inputs a.md b.md --analyze
```

机械拼接 / 递归重切统一复用 `split_document.py` 的等宽、重叠、不切断行逻辑（`--chunk-size` / `--looseness` / `--overlap-auto` / `--context-cap` 等参数同切分脚本）。

#### 新增脚本（零丢失合并 ／ 三道筛选 ／ 行映射，2026-11 实跑通过）

全部纯 Python 标准库实现、二进制读写、中文路径可用、本环境已实跑通过；细则分别见 `references/zero-loss-merge.md` 与 `references/three-pass-screening.md`。

- `fingerprint.py`（参数 `--source` / `--out` / `--check` / `--exclude-noise`）：切块前建硬数据指纹基线、终稿做种类差集校验。示例：`python scripts/fingerprint.py --source 母本.txt --out 基线.json --check 终稿.txt --exclude-noise`
- `split_by_lines.py`（参数 `--source` / `--volumes` / `--out-prefix` / `--map`）：二进制按行切分并单独产出行映射。示例：`python scripts/split_by_lines.py --source 母本.txt --volumes 7 --out-prefix 分卷 --map 卷映射.md`（产出 `分卷1.txt … 分卷7.txt`）
- `build_line_view.py`（参数 `--source` / `--out` / `--head` / `--tail` / `--index`）：生成三道筛选判定视图。示例：`python scripts/build_line_view.py --source 母本.txt --out 判定视图.txt --head 55 --tail 20 --index 行元数据.json`
- `render_from_lines.py`（参数 `--source` / `--lines` / `--out` / `--title` / `--note`）：按 agent 行号清单从母本零改写抽取生成新文档。示例：`python scripts/render_from_lines.py --source 母本.txt --lines agent1_result.json --out 筛选1.md --title "筛选1 · 初判无关条目"`
- `verify_three_pass.py`（参数 `--a` / `--b` / `--c` / `--source` / `--final`）：三道终局集合硬校验。示例：`python scripts/verify_three_pass.py --a agent1_result.json --b agent2_result.json --c agent3_result.json --source 母本.txt --final 筛选定稿.txt`
- `merge_semantic.py`（第 3 步真语义合并机械工具，子命令 `prepare` / `verify` / `render`；参数 `--input` / `--outdir` / `--result` / `--fingerprint` / `--out` / `--map` / `--similarity` / `--max-cluster` / `--workdir`）：脚本不调模型——`prepare` 机械生成候选簇 + 完整 agent 提示词、`verify` 做全覆盖/数字指纹零丢失硬校验、`render` 先校验再渲染合并稿 + 逐行去向映射。示例：
  ```bash
  python scripts/merge_semantic.py prepare --input 落盘稿.md --outdir 第3步文件夹 \
      --similarity 0.80 --max-cluster 20 --result 合并结果.json --workdir 工作目录
  python scripts/merge_semantic.py verify --input 落盘稿.md --result 合并结果.json \
      --fingerprint 基线.json --outdir 第3步文件夹
  python scripts/merge_semantic.py render --input 落盘稿.md --result 合并结果.json \
      --fingerprint 基线.json --out 合并稿.md --map 合并_行映射.md
  ```

#### 端到端编排（2026-11-05 新增，一句话启动的主执行器）

- `line_select_merge.py`（参数 `--source` / `--out` / `--state` / `--map` / `--scatter-marker` / `--keep-rule` / `--fingerprint` / `--check` / `--no-builtin-rules` / `--min-cjk`）：条目合并的**可选机械删行/去重**（新链由 `run_pipeline.py` 的 `--legacy-merge` 开关调用，默认关闭，不再改变筛选链行号基准）——程序化行选择，每行原样保留或整行丢弃、绝不改写；AUTH/CAND 分区 + 中文 4-gram 独有性 + Pass2 零 3-gram 重叠强制救回；自动产物外行映射与 `_merge_state.json`。示例：
  ```bash
  python scripts/line_select_merge.py --source 母本.txt --out 合并结果.txt \
      --state merge_state.json --map 合并_行映射.md \
      --scatter-marker "零散内容块标记" --fingerprint 基线.json --check
  ```
  默认开启内置窄规则词（更保守、多留重述行）；`--no-builtin-rules` 关闭后更激进。实测某设计文档样本：默认口径更保守、多留重述行；关闭内置规则后更激进，两种口径三类硬数据指纹均零丢失。
- `run_pipeline.py`（子命令 `init` / `status` / `run` / `resume` / `verify` / `report`）：把上面各脚本串成 S0–S9 状态机（**执行顺序已按权威流程真反转：筛选(第2步)→合并(第3步)**；STAGES 顺序 = 真实执行顺序，见 `SKILL.md` 第 0 节），机械阶段全自动、语义阶段 S4 自动停下等三道 agent 的 JSON、支持断点续跑与自动验收，产出 `pipeline_state.json` 与 `运行报告.md`。完整用法见 `references/quickstart.md` 第 3 节；典型序列：
  ```bash
  python scripts/run_pipeline.py init   --source 母本.txt --workdir 工作目录 --volumes 7
  python scripts/run_pipeline.py run    --workdir 工作目录      # 跑到 S4 自动停
  # 按 quickstart 第4节提示词串行派 3 个前台 agent，产出 agent1/2/3_result.json
  python scripts/run_pipeline.py resume --workdir 工作目录      # 续跑到 S9
  python scripts/run_pipeline.py verify --workdir 工作目录      # 非 0 退出码禁止交付
  ```
- **第 3 步语义合并的 agent 派发（强锁 1，硬规则）**：`--semantic-merge on`（默认）时，`run_pipeline.py run` 跑到 S2 会先由 `scripts/merge_semantic.py prepare` 生成候选簇与 `合并_agent_prompt.md`（提示词位置 = `pipeline_state.json` 的 `files["合并_agent_prompt"]`，落在「第3步 条目合并」步骤文件夹），然后**停住**打印下一步。主线程**只派 1 个前台 agent**、并发 1、不启动其他 agent、不并发、跑完再起下一个，让它完整读完提示词、只写自己的 `合并结果.json`（`files["合并结果"]`）、完成后只回一行确认；产出后主线程跑 `python scripts/run_pipeline.py resume --workdir 工作目录` 触发 `verify`+`render` 硬校验落盘。可复制命令序列见 `references/quickstart.md` 第 3c 节；`--semantic-merge off` 退回第一批直通（零改写）。
  ```bash
  # run 停在 S2 后（提示词/结果路径由 run 打印，也可从 pipeline_state.json 的 files 读）
  python scripts/run_pipeline.py status --workdir 工作目录     # 看 S2 waiting 与提示词/结果路径
  # 派 1 个前台 agent：读完 <合并_agent_prompt.md>，产出 <合并结果.json>，只回一行确认
  python scripts/run_pipeline.py resume --workdir 工作目录     # verify+render 硬校验通过后落盘
  ```
- `build_extract.py`（完整总结链前置工具）：输入是多份源文档时，`run_pipeline.py` 不直接处理，先用 `merge_and_chunk.py --inputs` 机械拼接+切块，再由本脚本 `check` / `template` / `append` / `append --all`（批量）/ `check --finalize` 把逐块摘要严格只追加成单一总提取稿（`# === 来自 chunk_NNN ===` 硬边界、\`\` 完成标记、防重复写入保护、块号连续校验、逐块子 Agent 提示词模板），最后以该稿经 `run_pipeline.py init --extract` 入链，作为统一**筛选基准稿**（`f["screen_base"]`），筛选链视图/回查/渲染/校验/删行全以此为准；不给 `--extract` 则退回母本并打印告警。逐块总结本身由主线程串行前台派 agent 完成，脚本不调模型、块内容零回流。命令见 `references/quickstart.md` 第 1 节。
- `pipeline_templates.py`（模块，非 CLI）：`render_criterion()` 生成三道共用的五节判定基准，`render_agent_prompt(1/2/3, …)` 生成三份可直接派发的提示词；判定对象由 `--subject` 注入，不含任何特定项目内容。由 `run_pipeline.py` 自动调用。
- `skill_selfcheck.py`：Skill 包一键自检（必需文件 / frontmatter / 全部脚本语法与 `--help` / 文档参数零编造 / 包内引用存在 / 红线关键词零丢失 / 编排器子命令齐全 / 两副本逐文件 md5 一致），9 项全 PASS 才 exit 0。示例：`python scripts/skill_selfcheck.py --json 报告.json`。
- `review_kit.py`（交付前三重查漏 + 冲突裁决）：子命令 `conflicts`（抽 `【冲突…】` → 独立口径去重 + 编号化 registry + 待用户四选一裁决清单）、`numbers`（原文 vs 终稿数字指纹种类差集 → 带原文行号上下文的补漏清单）、`coverage`（原文结构清单 → 终稿规则级落点覆盖度自评 X/Y）、`all`（一次出三份）。三份清单都落在输出文档之外，由 `run_pipeline.py` 的 S9 自动调用并列入交付清单。对应原则 5/6/7 与冲突裁决。示例：`python scripts/review_kit.py all --source 原文.txt --final 终稿.txt --outdir 目录`。

交付格式与标记协议：`references/output-template.md`（终稿 txt 骨架、来源声明、覆盖度自评）、  
`references/markup-protocol.md`（`【冲突：源稿矛盾】` / `⟦OVERLAP⟧` / `### 来源文件： ###` 等标记协议，  
以及标准总结主流程的逐块总结／合并压缩／内容保真／遗漏审查四类 agent 提示词铁律）。

### 故障速查与执行纪律

完整踩坑表（44 条）、中断／失败恢复判定树、校验命令速查见 `references/troubleshooting.md`。以下为本环境实测执行纪律，硬性：

- 后台 agent 会被回收零产出，必须前台跑：本环境后台／子 agent 三次实测均被 killed、零产出，面板所见只是回收前的上下文残影；一切合并、筛选、切分、校验一律前台串行执行，不再重试后台方式。
- 框架报 failed 先查文件系统再下结论：agent 常已写完文件、仅卡在最后合并／回传；先 `ls` ／ `wc -l` 看产出，缺什么补什么，别急着重跑浪费积分；先得出「已产出／部分产出／零产出」结论再动作，只补缺失区间、不整批重跑，并加防重复写入保护。
- 被中断先查残留 JSON 再补跑：WB 崩溃／用户截断／超时会留半截产物（如压缩比异常即判不完整），先备份可疑半成品、查残留文件与 JSON，再前台补跑缺失检查。
- agent 只回一行确认、不回正文：提示词写死「只回一行确认」，块原文与摘要正文任何时刻不回流主线程，否则撑爆上下文（实测触发 `400 prompt too long`）。
- 默认串行 1；并发数由 Q4 用户自定、不设上限、不钳制，CPU 档位只给建议值；用户当次要求串行时严格串行：开多 agent 前必须征得用户同意（本机并发主判据是 CPU 档位、不看可用内存；429 为云端配额次要风险、代价约等 2 小时）；用户本次明确「严格串行、不启动其他 agent」时，一律前台单线串行，不并行、不派发其他 agent。
- 机械步骤尽量合并为单次命令（硬性，性能纪律）：同类批处理（如逐块 `template` / `append`）必须写成**一次 `for` 循环或批量参数**，**禁止一个块一次 CLI 调用**；前置探路（`--help` / `wc -l`）也合并成一条命令。理由：每次工具调用背后是 LLM 生成命令的推理 + shell/Python 冷启动（实测约 **1.3s/次**），瓶颈是**工具调用次数**而非语言本身，减少调用次数才是有效提速。可复制批量示例：
  ```bash
  # 单次工具调用：一次 for 循环生成全部块提示词
  for i in $(seq 1 18); do
    python scripts/build_extract.py template --chunks "<切块目录>" --extract "<总提取稿.md>" --index "$i"
  done
  # 单次工具调用：一次 append --all 把全部 summary_NNN.md 按块号升序追加
  python scripts/build_extract.py append --chunks "<切块目录>" --extract "<总提取稿.md>" --all
  ```

### 自检门禁

改动或部署后跑 `python scripts/skill_selfcheck.py` 与 `python scripts/smoke_test.py`，全项通过才算完成。
