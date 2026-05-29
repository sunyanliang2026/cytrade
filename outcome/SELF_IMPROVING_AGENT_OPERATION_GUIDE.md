# cytrade 自我进化智能体补丁操作手册

版本日期：2026-05-24  
适用工程：`cytrade-main`  
适用基线：`f238d0f Add stock-pool sources and dry-run monitor session`  
配套增量包：`cytrade_self_improving_agent_patch.zip` / `cytrade_self_improving_agent.patch`

---

## 0. 先读这段：本补丁要解决什么

当前工程主线仍然是 **MainSealFollow Level2 dry-run 监控验证**：

1. 08:50 自动生成股票池。
2. 09:30 后监听行情和 Level2 数据。
3. 盘中只做 dry-run 验证，不发送真实订单。
4. 盘后/午间用日志复盘触发链路。

本补丁不是“交易智能体”，而是在现有系统外层加一套第一版 **自我进化闭环**：

```text
早盘 dry-run 日志
  -> 自动解析
  -> 生成 morning review
  -> 生成低风险改进任务
  -> 生成受约束 Codex CLI prompt
  -> 跑质量闸门
  -> 人工 review 后再合并
  -> 经验写回项目大脑
```

它对应 AI 自我进化公司的五层 loop：

```text
sensor layer       -> agent/sensors/
policy / decision  -> AGENTS.md + agent/policies/
tool layer         -> agent/tools/
quality gate       -> agent/gates/
learning mechanism -> agent/memory/
```

---

## 1. 安全边界

任何时候都不要让智能体自动扩大交易权限。

本阶段允许 AI / Codex 做：

```text
补日志
补日志解析器
补 dry-run 复盘报告
补测试
补文档
整理失败原因
生成低风险 patch
```

本阶段不允许 AI / Codex 自动做：

```text
开启实盘
把 CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN 改成 false
修改账号、密码、QMT 路径、Webhook secret、.env
修改真实下单路径
放宽风控阈值
自动调整买入金额
绕过测试失败继续提交
自动 merge / 自动部署
```

建议原则：

```text
Codex 可以生成 patch，但最终必须由人 review。
Codex 可以运行测试，但不能代替人判断实盘风险。
Codex 可以更新 AGENTS.md，但安全规则只能收紧，不能放宽。
```

---

## 2. 增量包内容

`cytrade_self_improving_agent_patch.zip` 内包含：

```text
README_APPLY.md
cytrade_self_improving_agent.patch
quality_gate_report.md
changed_files.txt
changed_files/
```

补丁会新增或修改这些核心内容：

```text
AGENTS.md
agent/README.md
agent/sensors/parse_monitor_logs.py
agent/sensors/parse_pytest_output.py
agent/sensors/parse_git_diff.py
agent/loops/post_morning_review.py
agent/loops/generate_improvement_tasks.py
agent/tools/codex_cli_runner.py
agent/tools/test_runner.py
agent/gates/quality_gate.py
agent/gates/safety_gate.py
agent/memory/project_brain.md
agent/memory/known_failures.yaml
agent/memory/lessons/main_seal_follow.md
agent/policies/allowed_changes.yaml
agent/policies/main_seal_follow_guardrails.yaml
agent/prompts/*.md
docs/SELF_IMPROVING_AGENT_SYSTEM.md
tests/test_agent_monitor_review.py
.gitignore
pyproject.toml
```

其中最重要的是：

| 文件 | 作用 |
|---|---|
| `AGENTS.md` | 给 Codex CLI 的项目规则、安全边界、验证命令 |
| `agent/sensors/parse_monitor_logs.py` | 解析 `MONITOR_SESSION`、`Runtime heartbeat`、`MSF_EVENT`、mock trade 等日志 |
| `agent/loops/post_morning_review.py` | 生成早盘 dry-run 复盘报告和摘要 JSON |
| `agent/loops/generate_improvement_tasks.py` | 根据摘要生成低风险改进任务 |
| `agent/tools/codex_cli_runner.py` | 为单个任务生成受约束 Codex CLI prompt，默认不执行 |
| `agent/gates/quality_gate.py` | 安全扫描、py_compile、pytest 质量闸门 |
| `agent/memory/` | 项目大脑、运行报告、经验沉淀、Codex prompt 存放目录 |

---

## 3. 应用前准备

### 3.1 确认当前没有正在运行的监控任务

不要在 `run_main_seal_follow_monitor_session.py` 正在跑的时候应用补丁。建议在收盘后或没有任务运行时操作。

Windows 可以先查看计划任务状态：

```powershell
Get-ScheduledTask -TaskName "Cytrade MainSealFollow Monitor"
```

如需临时停止正在运行的任务：

```powershell
Stop-ScheduledTask -TaskName "Cytrade MainSealFollow Monitor"
```

### 3.2 进入工程目录

PowerShell：

```powershell
cd C:\path\to\cytrade-main
```

Git Bash / Linux / macOS：

```bash
cd /path/to/cytrade-main
```

### 3.3 检查 git 状态

```bash
git status --short
```

理想状态：除了运行时产物 `config/main_seal_follow_pool.csv` 外，没有其他未提交改动。

如果有本地修改，先提交或 stash：

```bash
git add <your_files>
git commit -m "Save local changes before self-improving agent patch"
```

或者：

```bash
git stash push -m "before self-improving agent patch"
```

### 3.4 单独备份运行时股票池 CSV

`config/main_seal_follow_pool.csv` 是运行时产物，不应该参与代码提交。应用补丁前可以备份一份：

PowerShell：

```powershell
Copy-Item config\main_seal_follow_pool.csv config\main_seal_follow_pool.backup_before_agent_patch.csv -ErrorAction SilentlyContinue
```

Git Bash / Linux / macOS：

```bash
cp config/main_seal_follow_pool.csv config/main_seal_follow_pool.backup_before_agent_patch.csv 2>/dev/null || true
```

---

## 4. 推荐方式：用 git apply 应用补丁

把 `cytrade_self_improving_agent.patch` 放到任意本地路径，例如：

```text
C:\Users\<you>\Downloads\cytrade_self_improving_agent.patch
```

先做冲突检查：

PowerShell：

```powershell
git apply --check C:\Users\<you>\Downloads\cytrade_self_improving_agent.patch
```

Git Bash / Linux / macOS：

```bash
git apply --check /path/to/cytrade_self_improving_agent.patch
```

如果没有输出，表示检查通过。然后正式应用：

PowerShell：

```powershell
git apply C:\Users\<you>\Downloads\cytrade_self_improving_agent.patch
```

Git Bash / Linux / macOS：

```bash
git apply /path/to/cytrade_self_improving_agent.patch
```

查看应用结果：

```bash
git status --short
```

你应该能看到新增的 `agent/`、`AGENTS.md`、`docs/SELF_IMPROVING_AGENT_SYSTEM.md`、`tests/test_agent_monitor_review.py` 等文件。

---

## 5. 备选方式：覆盖 changed_files

只有在本地没有 git，或者 `git apply` 无法使用时，才使用这个方式。

做法：把 zip 里的 `changed_files/` 内容覆盖到工程根目录。

注意：覆盖方式没有冲突检查。如果你的本地文件和补丁基线不一致，可能会覆盖本地修改。优先使用 `git apply --check`。

---

## 6. 应用后验证

### 6.1 确认 Python 环境

使用项目原本的 Python 环境，例如：

PowerShell：

```powershell
C:\Users\ysun\miniconda3\envs\cytrade311\python.exe --version
```

如果你的环境变量里已经指向正确环境，也可以直接：

```bash
python --version
```

### 6.2 编译新增模块

```bash
python -m py_compile agent/sensors/parse_monitor_logs.py agent/sensors/parse_pytest_output.py agent/sensors/parse_git_diff.py agent/loops/post_morning_review.py agent/loops/generate_improvement_tasks.py agent/gates/quality_gate.py agent/tools/codex_cli_runner.py
```

### 6.3 跑新增测试

```bash
python -m pytest tests/test_agent_monitor_review.py -q
```

预期结果：

```text
5 passed
```

### 6.4 跑质量闸门

```bash
python -m agent.gates.quality_gate --pytest tests/test_agent_monitor_review.py --output agent/memory/runs/initial_quality_gate.md
```

预期看到：

```text
Overall: PASS
```

### 6.5 可选：复跑原有 MainSealFollow 测试子集

```bash
python -m pytest tests/test_collect_main_seal_pool.py tests/test_import_iwencai_pool.py tests/test_run_main_seal_follow_monitor_session.py tests/test_main_seal_follow_strategy.py -q
```

如果出现：

```text
ModuleNotFoundError: No module named 'chinese_calendar'
```

说明当前 Python 环境缺少项目依赖，安装：

PowerShell / Bash：

```bash
python -m pip install "chinese-calendar>=1.9"
```

然后重新跑测试。

---

## 7. 第一次早盘 dry-run 后怎么用

早盘仍然按原来的计划任务运行：

```text
Task name: Cytrade MainSealFollow Monitor
Trigger: weekdays 08:50
```

早盘或午间结束后，进入工程目录，执行：

```bash
python -m agent.loops.post_morning_review --run-id 2026-05-25 --print-paths
```

如果日志不在默认位置，可以显式传入日志文件或通配符：

```bash
python -m agent.loops.post_morning_review logs/system.log logs/trade.log --run-id 2026-05-25 --print-paths
```

也可以传通配符：

```bash
python -m agent.loops.post_morning_review "logs/system.*.log" "logs/trade.*.log" --run-id 2026-05-25 --print-paths
```

生成文件：

```text
agent/memory/runs/2026-05-25_morning.md
agent/memory/runs/2026-05-25_morning_summary.json
agent/memory/improvement_tasks.yaml
```

你首先要人工打开：

```text
agent/memory/runs/2026-05-25_morning.md
```

重点看这些验收项：

```text
pool generation 是否成功
09:30 后 strategies 是否 > 0
tick_subscriptions 是否增长
l2_stocks 是否增长
是否出现 entry_signal_accepted
是否出现 dry_run_probe_trade_recorded
是否出现 [ORDER] [TRADE] [MOCK]
是否有疑似真实订单风险
日志是否足够复盘
```

---

## 8. 用 Codex CLI 处理一个低风险任务

### 8.1 查看自动生成的任务

打开：

```text
agent/memory/improvement_tasks.yaml
```

选择一个 `risk: low` 且 `type` 为 observability / testing / documentation / replay 的任务。

不要选择会改策略阈值、下单路径、金额、账户配置的任务。

### 8.2 只生成 Codex prompt，不执行

假设任务 ID 是：

```text
add-top-blocked-reason-summary
```

执行：

```bash
python -m agent.tools.codex_cli_runner --task-id add-top-blocked-reason-summary
```

它会生成类似文件：

```text
agent/memory/codex_prompts/20260525_143000_add-top-blocked-reason-summary.md
```

这一步默认不会执行 Codex CLI，也不会改代码。

### 8.3 人工检查 prompt

打开 prompt，确认里面至少包含这些约束：

```text
Read AGENTS.md first and obey it.
Do not enable real trading.
Do not change CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN from true to false.
Do not modify .env files, account credentials, QMT paths, or webhook secrets.
Add or update focused tests when code changes.
Run the validation commands listed in the task when possible.
```

如果 prompt 不符合预期，直接删除，不要执行。

### 8.4 推荐的 Codex 使用方式：交互模式

进入工程目录：

```bash
cd /path/to/cytrade-main
codex
```

然后把上一步生成的 prompt 内容粘贴给 Codex。

交互模式的好处是：你能实时看到 Codex 准备读哪些文件、改哪些文件、跑哪些命令。涉及安全边界时，直接拒绝或要求它改小范围 patch。

### 8.5 可选：由脚本调用 Codex

只有在你检查过 prompt，并且确认本机 Codex CLI 配置正确后，才使用 `--execute`。

示例：

```bash
python -m agent.tools.codex_cli_runner --task-id add-top-blocked-reason-summary --execute --codex-command "codex"
```

如果你的 Codex 非交互命令是 `codex exec`，可以这样：

```bash
python -m agent.tools.codex_cli_runner --task-id add-top-blocked-reason-summary --execute --codex-command "codex exec"
```

也可以通过环境变量设置：

PowerShell：

```powershell
$env:CYTRADE_CODEX_COMMAND = "codex exec"
python -m agent.tools.codex_cli_runner --task-id add-top-blocked-reason-summary --execute
```

Bash：

```bash
export CYTRADE_CODEX_COMMAND="codex exec"
python -m agent.tools.codex_cli_runner --task-id add-top-blocked-reason-summary --execute
```

---

## 9. Codex 改完后必须做的检查

### 9.1 看 diff

```bash
git diff --stat
git diff
```

重点检查是否出现危险改动：

```text
CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN=False
CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN=false
real order / live order 相关执行路径
account_id / password / token / webhook / qmt path
position sizing / buy amount / strategy threshold
.env
config/local_runtime.json
```

### 9.2 跑质量闸门

```bash
python -m agent.gates.quality_gate --pytest tests/test_agent_monitor_review.py --output agent/memory/runs/post_codex_quality_gate.md
```

如果 Codex 修改了某个已有策略模块，要增加相应测试目标，例如：

```bash
python -m agent.gates.quality_gate --pytest tests/test_agent_monitor_review.py --pytest tests/test_main_seal_follow_strategy.py --output agent/memory/runs/post_codex_quality_gate.md
```

### 9.3 跑人工指定测试

根据 Codex 改动的文件，补跑相关测试。例如：

```bash
python -m pytest tests/test_agent_monitor_review.py -q
python -m pytest tests/test_main_seal_follow_strategy.py -q
```

### 9.4 人工 review 后提交

确认只涉及低风险改动后：

```bash
git add AGENTS.md agent docs tests pyproject.toml .gitignore
git commit -m "Add self-improving agent review loop"
```

如果这是 Codex 后续生成的小 patch，可以按任务提交：

```bash
git add <changed_files>
git commit -m "Improve MainSealFollow dry-run observability"
```

---

## 10. 每日运行节奏建议

### 早盘前

```text
确认计划任务存在
确认 QMT / xtquant 行情环境可用
确认 dry-run 没被关闭
确认前一天没有遗留失败任务
```

### 早盘 08:50 - 12:00

```text
让 Cytrade MainSealFollow Monitor 正常运行
不要在运行中应用补丁
只观察日志，不手动干预策略代码
```

### 午间 / 盘后

```bash
python -m agent.loops.post_morning_review --run-id YYYY-MM-DD --print-paths
```

然后人工读：

```text
agent/memory/runs/YYYY-MM-DD_morning.md
agent/memory/improvement_tasks.yaml
```

### 下午

选择一个低风险任务：

```bash
python -m agent.tools.codex_cli_runner --task-id <task-id>
```

检查 prompt 后交给 Codex。

### 晚上

```bash
python -m agent.gates.quality_gate --pytest tests/test_agent_monitor_review.py --output agent/memory/runs/YYYY-MM-DD_quality_gate.md
```

人工 review，合并低风险 patch，把经验写回：

```text
agent/memory/project_brain.md
agent/memory/known_failures.yaml
agent/memory/lessons/main_seal_follow.md
AGENTS.md
```

---

## 11. 回滚方法

### 11.1 补丁刚应用，还没有提交

如果想撤销本次补丁：

```bash
git apply -R /path/to/cytrade_self_improving_agent.patch
```

如果反向补丁失败，可以用：

```bash
git restore .
git clean -fd
```

注意：`git clean -fd` 会删除未跟踪文件。执行前先确认没有要保留的运行报告或本地文件。

### 11.2 已经提交

查看提交：

```bash
git log --oneline -5
```

撤销提交但保留改动：

```bash
git reset --soft HEAD~1
```

生成反向提交：

```bash
git revert <commit_hash>
```

---

## 12. 常见问题

### Q1：`git apply --check` 报错怎么办？

先确认工程是否接近基线：

```bash
git log --oneline -5
```

再确认是否有本地改动：

```bash
git status --short
```

如果你本地已经改过同名文件，先 stash 或手工合并。不要直接覆盖。

### Q2：`post_morning_review` 生成的报告里没有事件怎么办？

可能日志不在默认位置。显式传日志路径：

```bash
python -m agent.loops.post_morning_review C:\path\to\system.log C:\path\to\trade.log --run-id 2026-05-25 --print-paths
```

或者用通配符：

```bash
python -m agent.loops.post_morning_review "logs/system.*.log" "logs/trade.*.log" --run-id 2026-05-25 --print-paths
```

### Q3：Codex CLI 找不到怎么办？

检查命令：

PowerShell：

```powershell
Get-Command codex
```

Bash：

```bash
which codex
```

如果你的命令不是 `codex`，用 `--codex-command` 或 `CYTRADE_CODEX_COMMAND` 指定。

### Q4：质量闸门失败怎么办？

打开报告：

```text
agent/memory/runs/post_codex_quality_gate.md
```

按失败类型处理：

```text
safety_scan failed -> diff 里有危险模式，优先回滚或手工删除
py_compile failed -> 修语法错误
pytest failed -> 修测试或修代码，不要跳过
```

### Q5：能不能让 Codex 自动执行全部任务？

不建议。第一阶段只允许“一次一个低风险任务”，并且默认只生成 prompt，不自动执行。

### Q6：能不能让 Codex 自动 merge？

不建议。本阶段必须人工 review 后再 merge。

---

## 13. 第一版成功标准

这套闭环第一版不追求自动交易，也不追求自动优化收益。做到下面这些就算成功：

```text
1. 早盘日志能自动解析。
2. 系统能判断 dry-run 是否通过最低验收。
3. 系统能说明失败原因，而不是只输出 failed。
4. 系统能生成 1-3 个低风险改进任务。
5. Codex CLI 能基于单个任务生成小 patch。
6. 质量闸门能拦住明显危险 diff。
7. 人类能看到 diff、测试结果、风险说明。
8. 人工批准后，经验能写回项目大脑。
```

---

## 14. 推荐的第一天完整命令清单

下面是一套从应用补丁到生成首份复盘报告的最小流程。

### 应用补丁

```bash
cd /path/to/cytrade-main
git status --short
git apply --check /path/to/cytrade_self_improving_agent.patch
git apply /path/to/cytrade_self_improving_agent.patch
```

### 验证补丁

```bash
python -m py_compile agent/sensors/parse_monitor_logs.py agent/sensors/parse_pytest_output.py agent/sensors/parse_git_diff.py agent/loops/post_morning_review.py agent/loops/generate_improvement_tasks.py agent/gates/quality_gate.py agent/tools/codex_cli_runner.py
python -m pytest tests/test_agent_monitor_review.py -q
python -m agent.gates.quality_gate --pytest tests/test_agent_monitor_review.py --output agent/memory/runs/initial_quality_gate.md
```

### 早盘后生成复盘

```bash
python -m agent.loops.post_morning_review --run-id 2026-05-25 --print-paths
```

### 生成 Codex prompt

```bash
python -m agent.tools.codex_cli_runner --task-id <task-id>
```

### Codex 改完后检查

```bash
git diff --stat
git diff
python -m agent.gates.quality_gate --pytest tests/test_agent_monitor_review.py --output agent/memory/runs/post_codex_quality_gate.md
```

### 人工确认后提交

```bash
git add AGENTS.md agent docs tests pyproject.toml .gitignore
git commit -m "Add self-improving agent review loop"
```

---

## 15. 参考资料

- 已上传项目状态文档：`PROJECT_STATUS_20260524.md`
- 已上传文章：`用AI构建一家能自我进化的公司(1).pdf`
- OpenAI Codex CLI 文档：`https://developers.openai.com/codex/cli`
- OpenAI AGENTS.md 文档：`https://developers.openai.com/codex/guides/agents-md`

