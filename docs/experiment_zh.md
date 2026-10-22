# SWE-Refactor：四个 Qwen 模型的实验方案

本实验比较模型执行**给定开发者目标的 Java 重构**的能力。主实验使用 `Qwen/Qwen3.5-2B`、`Qwen/Qwen3.5-4B`、`Qwen/Qwen3.5-9B`、`Qwen/Qwen3.8-27B`；用户已确认将原请求中的 8B 改为 Qwen3.5-9B。具体权重版本锁在 [`configs/models.lock.json`](../configs/models.lock.json)，生成预算锁在 [`configs/experiment.json`](../configs/experiment.json)。Qwen3-8B 仅保留为可选对照，不自动加入主实验。

Qwen3.5 三个模型可以支持同一家族内的规模比较；Qwen3.8-27B 同时改变了家族和规模，因此不能把其全部差异归因于参数数量。所有模型只使用文本输入；不启用图像能力，不进行微调。

目前运行器能够完成数据冻结、生成、可恢复收据、格式与单文件答案匹配诊断，并提供单独的仓库级评测程序。**生成完成、Slurm 作业完成、格式正确或答案字符串匹配，都不是已经验证的重构成功率。**正式结果需要下文的独立仓库评测与控制实验。

## 1. 固定问题与任务条件

研究问题：在相同任务、输入信息、输出预算和精度下，四个模型分别有多少任务能够完成指定重构，并通过真实编译及回归测试？成功率提高需要付出多少 GPU 时间、延迟与显存成本？

本地完整数据有 1,099 个 Java 任务、18 个项目、445 个“项目 + 提交”组，包含六种操作。详细数量、例子和数据缺陷见 [`dataset_zh.md`](dataset_zh.md)。它们不是 1,099 个独立 issue，同一提交可能贡献多个高度相似的任务。

主条件命名为 `stored_before_file_only / instruction_conditioned_refactoring`。模型看到：

- `type`：指定重构类型；
- `description`：历史开发者的具体目标，可能包括新方法名、签名、目标类；
- 一个重构前文件的路径与完整代码，即 `sourceCodeBeforeForWhole`。

模型看不到 `sourceCodeAfter*`、`diffSourceCode*`、`diffLocations`、历史模型输出、评测结果或完整原始 JSON 行。任务 ID、提交 ID 等只用于运行器和评分器对齐，不放入模型消息。`description` 本身已经提供历史目标，因此论文结论应写为“给定目标的重构执行能力”，不能称为自动发现重构机会，也不能直接冒充原附带提示方案的复现。

这个输入条件具有明确限制：移动任务常需目标类、调用方等其他文件，而当前模型只得到一个文件。它测量的是这个固定上下文条件下的执行能力；不能把失败都归因于推理能力，也不能外推为拥有完整代码仓库的代理性能。后续若增加父提交中的目标类和调用方，应另建协议版本，四个模型全部重跑，并只从父提交提取上下文。当前版本没有实现检索、代理或自动补充目标文件。

输出统一为严格 JSON：

```json
{"files":[{"path":"src/main/java/example/A.java","content":"完整 Java 源代码"}]}
```

模型必须给出所有修改或新增 Java 文件的完整内容；删除文件使用 `content: null`。多文件操作不能只写出被移动的方法而丢失原文件的其余成员。运行器保留原始文本，解析失败计入分母，不用额外 LLM 修补输出。

## 2. 数据划分与运行规模

先按 `(projectName, commitId)` 分组，再用 seed 42 固定划分，保证同一真实提交的任务不跨开发与测试集。冻结后的实际划分是：

| 子集 | 提交组 | 任务数 | 四模型各生成一次 | 用途 |
|---|---:|---:|---:|---|
| dev | 44 | 92 | 368 | 开发提示、排查格式和环境 |
| pilot | 44 | 116 | 464 | 测运行时、上下文溢出、控制实验和预算 |
| test | 357 | 891 | 3,564 | 协议冻结后的主要模型比较 |
| 全部 | 445 | 1,099 | 4,396 | 完整覆盖的描述性统计，分子集报告 |

建议分三步执行：

1. **技术 smoke**：每模型先做 2–6 条真实生成，验证权重、tokenizer、聊天模板、JSON、收据、显存和退出状态。理想的六条覆盖六个操作，但任意 smoke 都必须保存实际任务列表。smoke 不能用来排名，也不等于 116 条 pilot。
2. **pilot**：固定 116 条任务，跑四个模型，核对完整记录数并测量耗时；完成仓库级基线、gold 和 no-op 控制。利用 pilot 设定构建超时、修复项目环境。若更改提示、上下文或生成预算，增加协议版本后重跑整个 pilot。
3. **formal test**：冻结协议后对 891 条任务每模型生成一次，主要比较使用这个子集。需要覆盖全部数据时再运行 `--split all`，清楚区分 dev/pilot/test；不能把调参用过的任务混入未见测试成绩。

这些划分不消除模型预训练中可能出现的公开 Java 代码。没有训练数据审计时，不声称这是无污染或未见仓库测试。

## 3. 模型与生成设置

| 设置 | 固定值 |
|---|---|
| 权重 | 锁定的官方模型仓库与 revision |
| 数值精度 | BF16，主实验不量化 |
| 输入 | 相同文本内容，分别使用模型自己的真实 tokenizer / chat template |
| 上下文总长 | 32,768 tokens |
| 输入上限 | 24,576 tokens，包含系统消息与模板开销 |
| 输出上限 | 8,192 tokens |
| 解码 | greedy，temperature 0、top-p 1、seed 42 |
| 思考模式 | `enable_thinking=false`，运行前核对对应模板确实支持 |
| 每任务候选数 | 1；不做重试挑选、best-of 或评测反馈修复 |

超过实际 tokenizer 输入预算的任务记为 `out_of_context`，保留在全任务分母；不能静默截断源码。达到输出上限记为 `truncated`，即使半成品碰巧能够解析也不当作正常完成。所有模型的相同 token 数预算不意味着相同字符数量，应同时报告实际 token 计数与共同可容纳任务子集的结果。

2026-10-21 已在 DAIC01 用四个固定版本的真实 tokenizer 审计全部 1,099 条，见
[`token_budget_summary.json`](../reports/token_budget_summary.json)。四个模型在当前模板下均有
1,070 条输入可容纳、29 条输入超限；238 条所存参考后文件的完整 JSON 表示超过输出预算，
其中 210 条输入本身可容纳。输入和所存参考输出都能容纳的有 860 条。
这暴露了整文件输出方式的成本；参考长度不是最短合法解长度，且只有单个 after 文件，
不能用它断言任务必然无解。主报告必须给出长度分层，后续可将 diff 输出作为独立新协议比较，
不能把当前 S0 中的表示方式损失都解释成模型推理失败。此次审计仅 tokenization，没有推理回答。

seed 使配置可复现，但不同 GPU、并行策略、库版本不保证逐位相同。锁定运行环境和依赖版本，保存 Python / vLLM / PyTorch / Transformers / CUDA 版本、GPU 型号、tensor parallel、作业 ID、模型 revision、输入及输出哈希。速度比较尽量在相同 GPU 类型上完成；27B 若使用不同 GPU 数量，报告 GPU-seconds，而不只比较 wall time。

## 4. 独立评分：每一层回答不同问题

生成器与评分器分离。生成器只能读取 source-only 任务；评测私有文件可以读取原数据、参考答案和完整 gold 补丁。`gold.jsonl` 中保存的单个 after 文件不是完整仓库补丁，不能直接当作所有操作的可执行 gold。

| 指标 | 通过条件 | 能支持的结论 |
|---|---|---|
| 格式有效率 | 严格解析 JSON 与合法路径/文件内容 | 模型是否遵守输出协议 |
| 非空改动率 | 相对父提交实际有 Java 修改 | 排除原样返回代码 |
| 指定类型检测率 | 独立 RefactoringMiner 检测到相同复合类型，且定位在原目标方法 | 是否观察到对应结构变换 |
| 目标匹配 | 候选与独立 gold 的目标方法声明及类匹配 | 在本 guided 条件中是否实现指定目标 |
| 编译通过率 | 已通过基线控制的相同构建配置下，候选成功编译 | 源码能否在该项目环境构建 |
| 回归测试通过率 | 独立执行测试、生成新的 JUnit XML，实际执行数大于 0，失败/错误为 0 | 被执行的回归测试未发现行为回归 |
| 联合成功率 | 可应用且有改动、目标结构变换与目标匹配、编译及测试均通过 | 满足这套操作性标准的重构成功率 |
| stored-after 文件匹配率 | 输出包含的某文件与存储的 after 文件完全相同 | 单文件字符串诊断；不是功能或完整补丁正确率 |

测试通过不是对所有输入的语义等价证明；RefactoringMiner 检测也不是。联合成功率必须连同运行过的测试范围与已知盲点说明。允许与开发者代码形式不同的正确结果，不能以 CodeBLEU、相似度或完整代码 exact match 作为主要正确性指标。

对复合操作严格匹配复合类型：只 Move 不能满足 Move And Rename，只 Extract 不能满足 Extract And Move。当前独立评分器还比较 private gold 中右侧方法声明的文件/签名及类身份，避免“在同一个方法中抽出了另一个 helper”被计为完成指定目标。这是保守的 guided 目标检查：例如声明参数名或签名的有效变体可能被拒绝。对争议案例保留 RM JSON、diff 和日志，盲于模型身份进行人工判定，并同时报告原始自动结果与预先定义的判定规则。

当前 `python -m swe_refactor score` 只生成格式与单文件匹配诊断，明确把 `functional_success`、`refactoring_success` 留空。真实结构/构建/测试评测使用独立的 [`scripts/evaluate_repositories.py`](../scripts/evaluate_repositories.py)。不读取原脚本的 `refactoringMinerResult` 或 `compileAndTestResult` 作为新模型成功证据。

## 5. 基线、gold 与 no-op 控制

每条进入正式仓库评分的任务依次做以下核验：

1. 克隆对应项目到一次性目录，解析 `commitId^1`，固定完整父提交 SHA。核对任务提供的重构前文件与该父提交的文件逐字相同。不同则记录 `base_source_mismatch`，先解决版本对应关系，不直接覆盖真实基线。
2. 在父提交执行已审查的编译和测试命令。需要的 JDK 由项目配置提供。编译和测试必须分开记录，测试必须有当次生成的非空 XML 收据；基线失败属于环境/数据可评测性问题。
3. 在独立副本中创建空提交，运行 RefactoringMiner；无改动必须不检测到任何重构。空改动即使编译及测试通过，也不能是重构成功。
4. 在另一个父提交副本上应用人工核对过的**完整、任务对应的 gold 文件修改**。需要记录准确的 `base_commit` 和来源说明。gold 必须通过相同的 RM、编译、测试流程，才证明本任务在当前评分器中有可达的正例。
5. 从相同父提交建立新的候选副本，应用模型输出，提交候选，在编译前运行独立 RM，再编译及测试。每个模型/任务互相隔离，原仓库及数据不修改。

不能把整个历史 `commitId` 的全部变化一股脑作为该任务的 gold：同一提交可能包含多个重构与其他变化。单任务 gold 需要补齐原类、目标类、调用方和 imports，并说明抽取依据。当前数据只有单个完整 after 文件，尤其 121 条前后文件路径不同的任务，不能据此自动重建所有正确补丁。

缺少完整 gold 时，评分器仍可输出候选 RM/编译/测试观察，但 `eligible=false`、`strict_success=null`，状态为 `evaluated_gold_pending`。这应当被报告为“独立评分尚未完成”，不能把 null 当成模型失败、成功或 0%。原片段为空或无法唯一定位的条目也必须保留为数据定位问题并单独报告。

### 测试代码本身也会被重构

数据有 733 条源文件位于测试目录。候选不允许修改与目标源/目标文件无关的测试文件。对于测试代码重构，保留测试数量、断言、异常预期和边界用例，并核对是否只是通过删除测试逻辑取得通过。当前运行器只能拒绝无关测试文件修改，不能自动证明所有合法测试文件中的断言都保持。

正式报告应将 733 条测试代码任务与 366 条其他任务分开；对前者增加固定的外部行为检查或独立人工检查，记录实际覆盖与检查分母。未经这些检查，既有测试通过只能解释为“配置的回归测试通过”，不能宣称所有测试重构保持了原测试意图。不得让模型修改构建脚本、评分器、超时设置、测试命令或测试结果 XML。

## 6. 独立仓库评分的配置与命令

为每个项目建立私有 JSON 配置，例如 `configs/repositories.local.json`。以下只是格式例子，实际命令与 JDK 必须在该历史父提交验证：

```json
{
  "example-project": {
    "repository": "/server/swe-refactor/repositories/example-project",
    "java_home": "/server/jdks/jdk-11",
    "compile_command": ["./mvnw", "-DskipTests", "test-compile"],
    "test_command": ["./mvnw", "test"],
    "test_report_globs": ["**/target/surefire-reports/TEST-*.xml"],
    "timeout_seconds": 1800,
    "miner_timeout_seconds": 300
  }
}
```

这里 `compile_command` 跳过测试是为了分开记录编译与测试；`test_command` 必须真正执行，不能带跳过测试的参数。Gradle 项目应使用经过核验的 Gradle wrapper 命令与 `**/build/test-results/test/TEST-*.xml` 等收据路径。不得未经审查执行原数据的 `compileCommand` 字符串：其中存在缺失引号与跳过测试的情况。项目命令使用 argv 数组执行，不经过 shell。

完整 gold 补丁是私有 JSONL，一行一个任务：

```json
{"task_id":"任务哈希","base_commit":"完整父提交SHA","provenance":"核对完整变更的来源与范围","files":[{"path":"src/main/java/example/A.java","content":"完整gold文件"}]}
```

准备任务、导出模型收据并独立评分：

```bash
python -m swe_refactor prepare --output artifacts/dataset-v1 --seed 42
python -m swe_refactor export --run runs/qwen35-2b-test --output runs/qwen35-2b-test/predictions.jsonl
python scripts/evaluate_repositories.py \
  --tasks artifacts/dataset-v1/tasks.jsonl \
  --predictions runs/qwen35-2b-test/predictions.jsonl \
  --protocol runs/qwen35-2b-test/protocol.json \
  --repositories configs/repositories.local.json \
  --refactoring-miner data/SWE-Refactor/code/data/tools/RefactoringMiner-3.0.10/bin/RefactoringMiner \
  --gold-patches artifacts/private-gold/complete-patches.jsonl \
  --output runs/qwen35-2b-test/repository-evaluation \
  --work-root /server/scratch/swe-evaluation
```

规范 `tasks.jsonl` 包含全部子集；`--protocol` 按本次生成的 `selected_task_ids` 选择任务并核对任务哈希，避免没有生成的 dev/pilot 条目意外加入 test 分母。评分器逐条写 `evaluations.jsonl`、日志、RM JSON 与 `summary.json`；已存在的输出目录会拒绝覆盖。项目源码使用可删除的临时克隆，日志保留在输出目录。

RM 使用官方文档中的 `-c <repository> <candidate_commit> -json <output>`。命令选项与 Java 要求见 [RefactoringMiner 官方说明](https://github.com/tsantalis/RefactoringMiner/blob/master/documentation/how-to.md#how-to-run-refactoringminer-from-the-command-line)。本地所带 3.0.10 已验证 `-h`，且在人工构造的 Java toy 仓库上实际验证空改动不产生检测、Extract Method 产生检测、编译及 Java 断言运行通过；这只是评分器技术 smoke，不能当作 SWE-Refactor 或四个模型的成绩。

## 7. 分母、统计与成本

每个模型都为预先选中的任务写一条终态记录：正常生成、格式无效、输出截断、超上下文、生成错误或缺失。报告总任务数、真实生成数、输出有效数和每个失败原因，不得只统计成功写入代码的样本。

仓库级结果至少同时报告：

- **全任务覆盖**：1,099 或 test 891 的预设分母，其中多少可构建、gold 完整、可独立评分；
- **可评测子集联合成功率**：共同通过父提交、gold、no-op 控制的固定任务集合，四模型使用同一分母；
- **保守全任务已验证比例**：已验证成功数 / 所有指定任务数，明确未验证项不是已知模型失败；
- 各个阶段失败数量，包括环境错误、版本不对应、缺 gold、RM 工具错误、候选编译失败和候选测试失败。

候选编译失败在基线及 gold 已通过时通常是模型失败；依赖服务器失联、JDK 丢失等是环境错误，应保留原始记录并在恢复后使用同一候选重评，不能重新生成来挑更好的答案。OOM、超时、下载失败、空输出、截断都必须有可审计的状态。若 27B 只完成一半任务，也报告缺失的一半。

当前独立评分器将候选阶段的 RM 工具错误/超时、编译或测试工具缺失/超时记为
`candidate_evaluation_error`，保留 `eligible=true` 与 `strict_success=null`，等待原候选重评。
汇总分别给出 `scored_eligible` 和 `unscored_eligible`；已评分成功率使用前者，
同时保留成功数除以全请求任务数的保守已验证比例。候选真实编译/测试返回失败则计入失败。
四模型最终配对比较仍须使用共同完成控制与评分的固定任务集合，并报告被排除任务数。

性能表至少包含总体 micro average、项目 macro average、六种操作、测试/其他源文件、文件长度或 token 分桶。项目 macro average 应说明纳入的项目数，某项目没有可评测任务时不能默默跳过。Guava 占 300 条，不能只凭总体均值概括所有项目。

模型差异采用相同任务的配对比较；以 `(project, commit)` 为抽样单元做配对 cluster bootstrap，可在项目内分层，报告 95% 区间。不要把同一提交的多个相似任务当作独立样本计算过窄的区间。只有 14 条的 Move And Inline 必须给出原始成功数/分母。这里的区间反映任务采样的不确定性；greedy 单次生成不能估计随机解码波动。

同时记录输入/输出 tokens、模型加载与预热时间、每任务生成延迟 p50/p95、tokens/s、总 wall time、GPU-seconds、峰值 GPU 显存、编译/测试耗时和超时率。生成成本与独立评测 CPU 成本分别统计；不把四个模型共用的基线构建重复计入某个模型的生成成本。量化或更小上下文可以作为额外效率实验，但需另设协议，不能混入 BF16 主比较。

## 8. 可以宣布什么完成

数据冻结、单元测试通过、toy RM 验证、服务器可访问或 smoke 生成成功，都只完成了相应步骤。一次完整模型运行需要：选中任务数与终态收据数一致、原始输出和哈希可验证、诊断结果可复算。一次正式重构能力比较还需要：共同可评测集合、baseline/gold/no-op 控制、真实仓库 RM/构建/测试日志、完整失败分母以及分组统计。没有这些证据时应保留结果为空，并说明具体还缺哪一层。
