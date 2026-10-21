# CheatSheet：SWE-Refactor / Qwen 实验

这个仓库维护 SWE-Refactor Java 数据审计、可复现实验方案和 DAIC01 运行工具。
原始数据留在本地 `data/SWE-Refactor/`，不进入 Git。

本地规范数据为 **1,099 个任务、18 个 Java 项目、6 类方法重构**。
用户确认的模型是 **Qwen3.5-2B / 4B / 9B 与 Qwen3.8-27B**；固定版本见
[`configs/models.lock.json`](configs/models.lock.json)。27B 与小模型跨代，
不能把差异全部归因于参数规模。

从这里阅读：

- [数据里有什么、六类任务和具体例子](docs/dataset_zh.md)
- [实验问题、对照、划分、指标与统计](docs/experiment_zh.md)
- [DAIC01 部署、运行、收据与结果回读](docs/server_runbook_zh.md)
- [只读数据审计](reports/dataset_audit.json)
- [服务器预检快照](reports/server_preflight.json)
- [当前实验状态](reports/experiment_status.json) / [四模型 token 预算审计](reports/token_budget_summary.json)

2026-10-21：DAIC 因内核漏洞维护暂停新作业，四个技术测试仍排队，**模型输出为 0**。
数据审计、四个 tokenizer 的全数据预算检查及独立 Java toy 评测已完成；正式性能尚未得到。

当前实现的 **S0** 是“给定开发者重构要求 + 所提供的重构前单文件”的基线。
121 条跨文件任务缺少目标文件的重构前上下文；整文件 JSON 输出也有长度压力。
S0 可以评估受限输入下的执行能力，正式仓库级结论还需要父提交上下文、完整参考补丁、
各项目实际构建测试和独立 RefactoringMiner 验证。

```bash
python3 -m pip install -e .
python3 scripts/audit_dataset.py
python3 -m swe_refactor prepare --output prepared/swe-refactor
python3 -m unittest discover -s tests -v
```

无 GPU 的核心安装只有标准库。推理依赖在 GPU 服务器单独安装/冻结，参见运行说明。

```bash
# 在兼容 CUDA / vLLM 的 GPU 分配内运行
python3 -m swe_refactor generate --prepared prepared/swe-refactor \
  --model qwen35-2b --split pilot --limit 6 --run runs/example/model
python3 -m swe_refactor validate --prepared prepared/swe-refactor --run runs/example/model
python3 -m swe_refactor score --prepared prepared/swe-refactor --run runs/example/model
```

数据按“项目 + 提交”分组，固定 seed=42：开发集 92 条、pilot 116 条、测试集 891 条。
相同提交的任务不会跨集合。`--split all` 是完整 1,099 条的描述性视图；
调试过的开发/pilot 不能混入独立测试结论。

`validate.complete` 表示收据齐全且校验通过，包含错误/超长/格式失败记录。
`score` 只计算格式、覆盖率和所存参考文件的一致性诊断；**这些不等于重构正确率**。
独立仓库评测器 `scripts/evaluate_repositories.py` 要求真实父提交、干净临时克隆、
参考补丁正对照、空修改负对照、编译、真实测试 XML 收据与目标重构检测；
参考补丁或环境未验证时保持 `strict_success=null`。

每条输出记录数据/模型/代码/环境版本、原始回答、token 数、耗时、错误状态和 SHA-256。
恢复运行拒绝混合不同版本，不会偷偷重采样失败题。

大数据、权重、缓存、生成结果、服务器本地配置和凭据均由 `.gitignore` 排除。
CI 使用合成输入运行完整性测试，不下载权重或外部 Java 项目。
原始数据及第三方代码保留各自许可；本仓库暂未选定新代码的分发许可。
