# DAIC01 运行说明

2026-10-21 的执行状态：四个固定版本模型缓存已就绪，服务器源文件和规范数据的 SHA-256
一致。技术测试每模型 2 条，共 8 条，作业 27798–27801 已串行提交；截至检查时均未启动。
Slurm GPU 节点处于 DRAIN/DRNG 状态，gpu50 的管理员原因字段为 `Vulnerabilities`。
这属于集群可用性阻塞，不是模型生成失败。没有产生新模型回答或正式性能成绩。
用户随后提供了 Koen Mulderij 的 [DAIC 管理员公告](https://mattermost.tudelft.nl/daic/pl/54d4n1fuqbrjdy6eba6se6cx9a)：
当天宣布的内核漏洞影响 DAIC，计算节点会排空并重启，已有作业继续，新作业暂停启动；
具体时间线尚待更新。这里依据用户提供的公告内容及当前 Slurm 状态记录，没有绕过维护。

用户已于 2026-10-21 确认 DAIC01 与 Qwen3.5-2B/4B/9B、Qwen3.8-27B。
当天已独立确认 SSH 身份 `yhuang18`、Kerberos、共享目录实际写入/回读/删除、
Slurm association `daic-eemcs-st-se`。预检不分配 GPU，也不代表某个作业已执行。

独立部署目录：

```text
/tudelft.net/staff-umbrella/cheatsheet research/swe-refactor-qwen-20261021
```

保留已有 `cheat/`、`CheatSheet/` 和 CoReFusion 运行不变。已验证可导入依赖的解释器为已有环境：

```text
/tudelft.net/staff-umbrella/cheatsheet research/cheat/type-inference/qwen35/runtime/venv/bin/python
```

只读复用它，不向该环境安装/升级包。已核实可导入 torch 2.11.0+cu130、vLLM 0.23.0、
Transformers 5.19.0；每个 GPU 作业仍保存完整 `pip freeze` 与 GPU 型号/驱动。
新模型下载到本实验的 `.cache/hf/`。三个小模型通过只读使用的缓存目录链接复用其
已固定版本，生成时启用离线模式；27B 使用用户指定的 Qwen3.8 官方固定提交。

## 数据冻结和部署

```bash
# 本地仓库根目录；密码始终只输入用户自己的终端
ssh -t daic01 kinit
python3 scripts/server_preflight.py
PYTHONPATH=src python3 -m unittest discover -s tests -v
git add .
python3 scripts/deploy_daic.py
```

部署只发送 Git 已跟踪/暂存的源文件与规范 JSON，不发送旧结果、模型或原始上游脚本。
通过 tar 字节流写入，避免该 NFS 导出拒绝 chmod 的问题；逐文件 SHA-256 回读核验。
不执行远端删除，不重置旧项目。源代码改变后需重新部署并用新运行目录，不能混入旧收据。

进入远端独立目录后：

```bash
export SWE_ROOT='/tudelft.net/staff-umbrella/cheatsheet research/swe-refactor-qwen-20261021'
export SWE_PYTHON='/tudelft.net/staff-umbrella/cheatsheet research/cheat/type-inference/qwen35/runtime/venv/bin/python'
export SWE_HF_HOME="$SWE_ROOT/.cache/hf"
cd "$SWE_ROOT"
export PYTHONPATH="$SWE_ROOT/src"
"$SWE_PYTHON" -m swe_refactor prepare --output prepared/swe-refactor
```

新机器应在独立虚拟环境解析 `requirements-inference.in` 并保存精确依赖锁，
先验证四个 checkpoint 加载，再冻结环境。不要把兼容版本范围当成已经冻结的环境。

## 模型下载与 token 预算审计

模型必须使用 lock 中的 revision；例如：

```bash
export HF_HOME="$SWE_HF_HOME"
"$(dirname "$SWE_PYTHON")/hf" download Qwen/Qwen3.8-27B \
  --revision 1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0
HF_HUB_OFFLINE=1 "$SWE_PYTHON" scripts/audit_token_budgets.py
```

预算审计只用 CPU tokenizer，不产生模型回答。它报告各模型真实聊天模板输入长度及
所存参考后文件的 JSON 输出长度。参考文件不是完整补丁，也不是最短可行答案，
因此输出超长是表示方式压力的诊断，不能直接断言模型不具备该重构能力。
正式分析报告共同可装入上下文的子集与完整固定分母两种视图，不能悄悄截短或删除难题。

## 分阶段运行

先在 pilot 的固定前 6 条做技术测试。这 6 条不覆盖所有六种重构类型，不能报告为代表性性能。
再按实验文档验证语义评测正负对照，执行完整 116 条 pilot，冻结协议后运行 891 条 test。
四个模型使用相同任务、prompt、预算、BF16、单样本 greedy 和关闭 thinking。

```bash
mkdir -p logs
export SWE_MODEL=qwen35-2b
export SWE_SPLIT=pilot SWE_LIMIT=6 SWE_RUN_KIND=smoke
sbatch --export=ALL --output=logs/%x-%j.out scripts/run_model.sbatch
```

把 `SWE_MODEL` 依次设为 `qwen35-4b`、`qwen35-9b`、`qwen38-27b`。
每次申请 1 块 RTX PRO 6000 GPU、96 GB 主存；实际 GPU 显存必须以作业内记录为准。
脚本默认 `all` 分区和已核实账户。作业被接受并不代表获得分配；检查 pending reason、
作业内 `nvidia-smi` 和 `sacct`。不取消其他任务，不自动扩大 GPU 数量或改用量化模型。

pilot 去掉 `SWE_LIMIT`，设置 `SWE_RUN_KIND=pilot`；test 再设 `SWE_SPLIT=test`。
大规模提交前先根据 pilot 实测时间设置足够 wall time；当前 2 小时是技术测试上限，
不是全量运行耗时预测。需要改变预算/协议时生成新版本，保持旧收据。

## 检查结果

每个作业目录为 `runs/<kind>/<model>-<jobid>/`，模型收据在其 `model/` 子目录。
`runner.log`、`environment.txt`、GPU 首尾快照和 exit code 在父目录。
`model/receipt.json`、`model/diagnostics.json` 与导出的 `predictions.jsonl` 是后续入口。
必须检查实际状态计数；作业 exit=0 或 `receipt.complete=true` 均不能代表生成或重构成功。

```bash
squeue --me
sacct -j JOBID --format=JobID,State,Elapsed,AllocTRES,ExitCode
"$SWE_PYTHON" -m swe_refactor validate --prepared prepared/swe-refactor --run RUN/model
"$SWE_PYTHON" -m swe_refactor score --prepared prepared/swe-refactor --run RUN/model
"$SWE_PYTHON" -m swe_refactor export --run RUN/model --output RUN/predictions.jsonl
```

正式仓库评测使用 `scripts/evaluate_repositories.py --help`。需要为每个项目提供经审查的
仓库路径、按任务 JDK 版本配置的编译/测试 argv、JUnit XML glob，以及完整参考补丁。
绝不直接执行数据中的 `compileCommand`，也不采信历史 `testResult`。
完整参考补丁尚未构建/验证的任务保留在请求分母，`strict_success` 保持空值。
Java/JDK 8/11/17/21、Maven/Gradle 与 RefactoringMiner 是独立于推理环境的评测依赖；
当前预检未在登录节点 PATH 中找到 Java/Maven，不能据此声称正式 Java 评测已经可用。

回读可使用文件字节传输或 tar；校验 SHA-256、收据和日志后，再将紧凑聚合报告放到
`reports/` 提交。`runs/` 保留本地/服务器，不进入 GitHub。
