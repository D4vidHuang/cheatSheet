# SWE-Refactor 本地 Java 数据说明

本文只依据本目录实际提供的文件和代码。统计可由 `python3 scripts/audit_dataset.py` 重建，机器可读结果在 [`reports/dataset_audit.json`](../reports/dataset_audit.json)。这里引用的编译、覆盖率和纯重构标记是数据集原有记录；本次审计没有重新克隆、构建或执行这 18 个 Java 项目。

## 先理解一个任务要求什么

可以把每条数据理解成：“给你重构前的 Java 方法、所在文件和一种指定的重构操作，请产生修改后的代码，保持原有行为。”例如把一个长方法中的一段逻辑提取成独立方法，或者把某个方法移到更合适的类中。

这不是 SWE-bench 那种“读 issue、定位未知 bug、修复整个仓库”的任务，也没有逐条自然语言 issue 和专门配套的隐藏测试补丁。这里的每条任务来源于真实提交中被标记为纯重构的一次方法级操作，同一提交可以贡献多条操作。模型能否生成合法代码、能否实现指定重构、能否通过原项目测试，是三个需要分别验证的问题。

本地的规范输入是 [`data/SWE-Refactor/pure_refactoring_data.json`](../data/SWE-Refactor/pure_refactoring_data.json)：**1,099 个 Java 任务，18 个项目，445 个不同的“项目 + 提交”组合**。文件为 JSON 数组，大小 67,505,459 字节；所有记录的 `filePathBefore` 和 `filePathAfter` 都以 `.java` 结尾。每条任务有唯一 `uniqueId`，这 1,099 个 ID 没有重复。

数据 SHA-256：

```text
69e1846e6f1624cb693262de2648c973470ee2ce8a753e38d1666f5d620c3fdc
```

## 六类任务

| 类型 | 数量 | 通俗解释 | 本地实例 |
|---|---:|---|---|
| Extract Method | 441 | 从原方法抽出一段逻辑，形成新方法，并让原方法调用它 | Checkstyle 从 `shouldIgnoreNumberOfParameters` 抽出 `isIgnoredOverriddenMethod` |
| Move Method | 410 | 把方法从一个类移到另一个类，并处理调用和可见性 | Checkstyle 把 `Violation.setLocale` 移到 `LocalizedMessage` |
| Extract And Move Method | 142 | 先抽出新方法，再放到另一个类中 | Checkstyle 从 `Main.main` 抽出 `exit`，放进 `Main.ExitHelper` |
| Inline Method | 71 | 把被调用方法的逻辑直接放回调用者，消除不必要的间接调用 | Checkstyle 把 `SinglelineDetector.resetState` 内联进 `processLines` |
| Move And Rename Method | 21 | 方法移动到另一个类时同时改名 | Checkstyle 将 `NoWhitespaceBeforeCheck.isWhitespace` 移为 `CommonUtil.isCodePointWhitespace` |
| Move And Inline Method | 14 | 将一个类的方法逻辑移到另一个类的调用方法中并内联 | Checkstyle 将 `CheckUtil.createFullType` 的逻辑内联到 `AbstractClassCouplingCheck.ClassContext.visitType` |

Extract Method 和 Move Method 合计 851 条，占 77.4%，因此总体平均分会主要受这两类影响。只有 14 条的 Move And Inline Method 必须报告原始分子和分母，避免把一两道题的变化写成稳定趋势。

“Move”不一定意味着跨文件：类可以嵌套在同一个 `.java` 文件中。全数据只有 121 条的前后文件路径不同，不能据此把其他 Move 任务误判为标注错误。

## 来自哪些项目

| 项目 | 任务数 | 项目 | 任务数 |
|---|---:|---|---:|
| guava | 300 | junit5 | 105 |
| pmd | 125 | commons-io | 93 |
| checkstyle | 91 | hibernate-search | 89 |
| hibernate-orm | 63 | commons-lang | 59 |
| javaparser | 56 | shenyu | 22 |
| gson | 21 | zxing | 21 |
| jadx | 18 | junit4 | 18 |
| hertzbeat | 9 | mockito | 4 |
| shardingsphere-elasticjob | 3 | shiro | 2 |

Guava 单独占 27.3%，其中 Move Method 有 250 条。比较模型时，除了全部任务的 micro average，还应报告每项目平均后再平均的 macro average，以及每种重构类型的结果。否则一个模型只要在 Guava 的特定模式上表现好，总分就可能明显上升。

733 条任务的源文件位于 `test` / `tests` 路径，占 66.7%；另外 366 条有 JaCoCo 的行覆盖率等计数。**这里相当大一部分是在重构测试代码本身**，不能把全部结果解释为生产代码重构能力。最终结果应至少分开报告这两个子集。

## 一条记录有哪些字段

| 用途 | 字段 | 使用方法 |
|---|---|---|
| 任务身份 | `projectName`, `uniqueId`, `commitId`, `type` | `uniqueId` 用于去重、结果对齐；`commitId` 指真实重构提交，不是已验证的模型输入基线 |
| 重构前输入 | `sourceCodeBeforeRefactoring`, `sourceCodeBeforeForWhole`, `filePathBefore` | 前者是待重构片段，后者是所提供的重构前文件内容；构建评测前需与真实父提交核对 |
| 输入定位信息 | `packageNameBefore`, `classNameBefore`, `methodNameBefore`, `classSignatureBefore` 及对应 `*Set` | 说明方法和类；部分复杂操作涉及多个方法或类 |
| 额外上下文 | `callInfo`, `callGraph`, `invokedMethodSet` | 调用相关信息；需要单独核验提取版本和是否只来自父提交 |
| 历史开发者描述 | `description` | 包含目标新方法名、目标类、操作结果等；是否提供给模型会改变任务难度 |
| 标准答案与差异 | `sourceCodeAfterRefactoring`, `sourceCodeAfterForWhole`, `filePathAfter`, `diffSourceCode`, `diffSourceCodeSet`, `diffLocations` | 由独立评分阶段读取；不能整条记录直接序列化进 prompt |
| 原有可执行性元数据 | `compileCommand`, `compileJDK`, `compileResultBefore`, `compileResultCurrent`, `coverageInfo`, `testResult` / `hasTestC` | 只是原数据记录；必须在服务器重跑基线和候选验证 |
| 原有纯度元数据 | `isPureRefactoring`, `purityCheckResultList`, `moveFileExist` | 全部记录声称是纯重构并存在所需移动文件，不能代替新的功能测试 |

`sourceCodeAfterRefactoring` 在不同操作里表示的代码角色不同，不能假定它总是“把原片段直接替换掉就能得到完整补丁”。跨类移动也常需要修改原调用方、目标类、imports 和可见性。推荐统一输出可应用的文件修改或补丁，再在实际仓库里独立验证。

`callInfo` 有 643 条可用，456 条为 `N/A`；`callGraph` 只有 262 条存在。这些上下文覆盖不均。如果使用调用上下文，应报告有/无上下文两个子集，且先查明其提取提交；原数据字段名本身不能证明没有答案信息。

文件长度差异也很大：重构前文件的中位数为 15,153 字符，95 分位为 74,845 字符，最大为 390,360 字符。**字符数不是 token 数**，实验必须使用相应模型的真实 tokenizer 和聊天模板统计输入长度，再给输出预留固定预算；不要默默截断文件后仍把任务当作完整输入。

## 描述字段决定实验在测什么

以 Extract Method 为例，`description` 会明确写出“从哪个方法提取哪个新方法”，有时还给出精确签名；Move 类操作还会给出目标类。这相当于把历史开发者已经做出的选择提供给模型。

因此可以做两种明确区分的实验：

1. **操作类型输入（operation-only）**：模型只知道 Extract Method 等类型和重构前代码，需要自己决定具体结构。所提供的 `llm_refactoring.py:724-765` 主流程使用的是这种类型信息，不能未经说明把 `description` 加进去后声称是同一基线的复现。
2. **给定目标的重构（guided refactoring）**：模型额外得到 `description`，测试其执行明确重构要求的能力。这种实验成立，但结论应写为给定开发者目标的重构执行能力。应与操作类型输入分开报告。

无论哪一种，都不能把 `sourceCodeAfter*`、`diffSourceCode*` 或历史模型输出放入输入。`diffLocations` 和 `uniqueId` 中还含真实改动的前后行号，若把它们当定位提示，也应单独标为给定位置的实验条件。运行器可以在输出文件里保留 ID 以匹配结果，模型本身无需看到它。

## 必须先处理的数据与评测问题

以下都来自本地静态检查，而不是对作者论文结论的推断。

| 问题 | 数量/位置 | 对新实验的影响 |
|---|---|---|
| 项目分文件没有覆盖完整数据 | `code/data/*/*_pure_refactoring_data.json` 只有 10 个项目、703 条；缺 396 条 | 使用根目录的 1,099 条规范文件，不把项目副本拼接进去；副本的元数据也与规范文件不同 |
| 克隆列表也只有 10 个项目 | `code/repos.txt` | 还需补全其余 8 个项目，核验仓库 URL 与历史提交能否获取 |
| 记录的测试开关不一致 | `testResult=True` 有 1,098 条，另 1 条只有 `hasTestC=True` | 不能要求两个字段同时存在，也不能将字段缺失当作测试失败 |
| Gold 提交曾被标为编译失败 | 2 条 Checkstyle Extract Method，共享提交 `50a9a64b6d70aa722dcf754ad5d46e37784f5b05` | 独立检查原始基线与参考变化，记录失败原因；不直接将其从分母消失 |
| 原构建命令跳过测试 | 300 条 Guava 的命令含 `-DskipTests` | 构建成功不能算测试通过；需要独立、真实执行的测试命令 |
| 构建命令不能直接解析 | 93 条 Commons IO 命令缺少末尾单引号 | 使用经过人工审查并版本化的命令配置，不原样交给 shell |
| 片段为空 | 3 条，均为 Move And Inline Method | 不能执行 `whole.replace('', output)`；应取真实上下文并采用文件/补丁级处理 |
| 原片段在整文件出现多次 | 3 条：2 条 JUnit 4 Move、1 条 Hibernate Search Move And Inline | 不能无条件字符串全量替换，应做唯一性检查或语法定位 |
| 同源任务强相关 | 按项目、提交、原路径、原整文件分组，重复超额 584 条 | 开发/测试划分按提交分组；比较模型时也按提交聚类估计不确定性 |
| 测试方法覆盖率是人工标记 | 733 条 `coverageInfo.testMethod` | `pre_process_data.py:193-195` 对测试方法直接记 `covered=1`，不是测得的生产方法覆盖率 |

原附带评测脚本也不适合直接成为新实验的评分器：

- `evaluation_for_refactoring.py:23-24` 在结果缺失或未同时为真时直接返回，导致这些记录不会进入新的评测。
- 同文件 `:69`、`:212` 在成功解析输出后立即返回 `True, True`，后面的 Extract / Inline 的 RefactoringMiner 与编译逻辑不可达。解析成功不能算重构或测试成功。
- 同文件 `:33` 将 Move And Inline Method 映射为固定失败。
- `compile_experiment.py:47-55` 的当前默认构建命令使用 Gradle 并 `-x test`，与各个项目的实际 Maven/Gradle 流程不同。
- `llm_refactoring.py:778-780` 捕获异常后直接 `continue`，结果文件只保存成功处理到的记录。新实验必须预先锁定分母，并为每条任务写成功、失败、超时或环境错误收据。

RefactoringMiner 判断的是是否检测到某类结构变化，测试检查的是运行行为，二者不等价。只通过测试的空改动可能没有完成重构；检测到一次重构也不能保证行为保持。建议主指标要求“补丁可应用 + 编译成功 + 实际测试通过 + 指定重构被独立检测到”，同时分别报告每个阶段。

## 本地目录的其余内容

`experimental result/` 保存历史模型和提示方案的输出，包括 GPT-3.5、GPT-4o-mini、CodeLlama、Qwen2.5-Coder 等已有结果和工作簿。它们既不是新模型的成绩，也不是训练集；不要把它们混入新运行的检索或 prompt。

`code/` 包括旧的生成、RAG、多代理、覆盖率、构建和评估代码，以及 RefactoringMiner / Checkstyle 的 Java 工具。工具和依赖的版本应在新实验中固定，但原脚本内存在本机路径、旧模型配置和评测短路，不能仅靠“脚本能跑”判断实验成立。

本地提供的数据目录未附顶层 README、LICENSE 或 CITATION。维护本研究仓库时保留原始数据不变，将其作为外部输入并通过 `.gitignore` 排除，跟踪审计脚本、数据指纹和自己编写的实验代码/文档；正式再分发数据前再确认来源与许可。
