# Agent quality gate

Overall: **PASS**

## PASS - safety_scan

```text
no risky diff patterns found
```

## PASS - C:\Users\ysun\miniconda3\envs\cytrade311\python.exe -m py_compile agent/sensors/parse_monitor_logs.py agent/sensors/parse_pytest_output.py agent/sensors/parse_git_diff.py agent/loops/post_morning_review.py agent/loops/generate_improvement_tasks.py agent/gates/quality_gate.py agent/tools/codex_cli_runner.py

```text
exit=0
```

## PASS - C:\Users\ysun\miniconda3\envs\cytrade311\python.exe -m pytest tests\test_agent_monitor_review.py

```text
============================= test session starts =============================
platform win32 -- Python 3.11.15, pytest-9.0.3, pluggy-1.6.0 -- C:\Users\ysun\miniconda3\envs\cytrade311\python.exe
cachedir: .pytest_cache
rootdir: C:\Users\ysun\workspace\cytrade
configfile: pytest.ini
plugins: anyio-4.13.0
collecting ... collected 5 items

tests/test_agent_monitor_review.py::test_parse_monitor_logs_builds_morning_acceptance_summary PASSED [ 20%]
tests/test_agent_monitor_review.py::test_parse_monitor_logs_detects_possible_real_order_lines PASSED [ 40%]
tests/test_agent_monitor_review.py::test_generate_tasks_for_active_strategies_without_signals PASSED [ 60%]
tests/test_agent_monitor_review.py::test_quality_gate_safety_scan_blocks_dry_run_disablement PASSED [ 80%]
tests/test_agent_monitor_review.py::test_parse_pytest_output_summary_line PASSED [100%]

============================== 5 passed in 0.04s ==============================
```
