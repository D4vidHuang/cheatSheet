# 四模型全量运行

用户于 2026-10-21 要求直接提交全量测试。本次固定请求分母为 **1,099 × 4 = 4,396**：
Qwen3.5-2B、Qwen3.5-4B、经用户确认的 Qwen3.5-9B，以及 Qwen3.8-27B。
全部使用 `configs/models.lock.json` 中的不可变提交。27B 是不同代际参照，不能把差异全部解释为模型规模。

## 任务和调度

`scripts/submit_full_run.py` 创建独立的 release，冻结源代码、配置、原始输入副本、
规范任务及完整任务 ID 清单，然后逐文件 SHA-256 核对服务器副本。不会覆盖上游数据和旧运行。
本次使用 `split=all`，包含原先 dev、pilot、test 的全部任务；原有分组划分仍保存在数据中，
但这次不能宣称只在 891 条未参与开发的正式测试集上测量。

每个模型有 16 个 array shard：按不可变 task ID 排序后，第 i 片取
`tasks[i::16]`。每片 68 或 69 条，同一模型不重叠、不遗漏。四个 GPU array 一共
64 个子作业；默认每个 array 同时运行最多 1 片，因此同时最多请求 4 块 GPU。
每片申请 1 块 RTX PRO 6000、8 CPU、96 GB 主存，wall time 为 12 小时。
该时限是执行上限，尚未有本数据集 GPU 实测可用于承诺耗时。

一个额外 CPU 汇总作业使用 `afterany` 依赖四个完整 array，失败、超时和取消的请求也进入分母。
DAIC 维护期间作业可能保持 pending；进入队列不等于启动，更不等于产生了模型输出。
脚本不绕过管理员排空，不修改集群状态，不取消其他任务。

## 模型输入和预算

模型只收到允许的 BEFORE 单文件和任务指令；AFTER 文件、提交 diff、历史评测结果只给评测器。
使用 BF16、不量化、greedy、关闭 thinking，每题至多保存一次生成记录。
统一输入上限 24,576 tokens，输出上限 8,192 tokens，总上下文 32,768 tokens。
vLLM 显式使用 `language_model_only=True`，避免文本任务的图像/视频模块分析。

已有 CPU tokenizer 审计表明每模型有 29 条超过输入预算。这些任务保存 `out_of_context`，
保留在 1,099 分母中，不截短也不悄悄删除。参考单文件输出长度只是表示方式压力的诊断；
不是完整参考补丁，也不能据此认定模型是否具备重构能力。

## 运行收据与重启

服务器运行目录为 release 下的：

```text
runs/full/<run_id>/<model>/shard-00000/
  model/protocol.json
  model/records/<task_id>.json
  model/ledger.jsonl
  attempts/<SLURM_JOB_ID>-restart-<SLURM_RESTART_COUNT>/
    attempt.json
    runner.log
    environment.txt
    gpu-start.txt
    gpu-final.txt
    validation.json
    score.json
    predictions.jsonl
    result.json
    exit_code.txt
```

array 在 wall time 前 120 秒请求 TERM；worker 向整个生成进程组转发信号，
保留已写好的原始输出和收据，记录终止状态。Slurm 允许 requeue 不代表脚本自动反复提交。
重启只能继续未保存记录的任务；已有失败记录也不会重新采样。

残留 `active.lock` 和 worker 锁不会直接删除。只有 `sacct` 中对应旧分配明确进入终态，
或同一作业存在更大的 `SLURM_RESTART_COUNT`，才允许记录证据后恢复。
不同节点续跑还必须通过核心 CLI 的硬件/软件等价校验；每次环境和 GPU 身份单独保留。
模型、数据、提示、预算、源代码或软件版本发生变化时拒绝混入旧运行。

提交过程有独立持久化 journal：已确认的 job ID 不重复提交；如果 SSH 中断发生在
`sbatch` 回执未确认期间，保留未知状态并要求先核对调度器，避免重复占用 GPU。

## 结果边界

worker 即使收到生成错误也尽量保存验证、导出和格式诊断。
缺失收据、无效收据或所有可生成任务均为 `generation_error` 会让该 shard 返回非零。
格式正确、字符串匹配、Slurm `COMPLETED` 与 `receipt.complete=true` 都不能代替重构正确性。

`scripts/aggregate_full_run.py` 检查完整计划、逐片任务范围、协议、源代码和记录哈希，
按模型、项目、重构类型和测试/其他源码汇总状态、缺失分母及可测成本。
汇总保留 `functional_success=null` 和 `refactoring_success=null`，直到完成独立的目标重构检测、
验证过基线的编译、真实测试，以及完整参考补丁/无操作对照审计。
当前不能把生成阶段诊断发布成 SWE-Refactor 语义性能成绩。

所有权重、缓存、逐题原始输出和参考答案继续留在 Git 忽略目录；仅提交程序、配置、
简洁的提交回执及聚合报告。
