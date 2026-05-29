You are working on the cytrade repository.

Read AGENTS.md first and obey it.

Implement exactly one low-risk improvement task. Keep the patch small and reviewable.

Hard constraints:
- Do not enable real trading.
- Do not change CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN from true to false.
- Do not modify .env files, config/local_runtime.json, account credentials, QMT paths, or webhook secrets.
- Do not change position sizing, strategy thresholds, or order-routing behavior unless the task explicitly says human_required=true and the human has approved it.
- Add or update focused tests when code changes.
- Run the validation commands listed in the task when possible.
- Summarize changed files, tests run, and safety impact.

Task:

```yaml
  - id: "flag-invalid-market-data-session"
    title: "Flag invalid monitor session when market data never connects"
    risk: "low"
    type: "observability"
    reason: "Strategies were created, but heartbeats never showed usable market data; make this failure explicit before any strategy tuning."
    human_required: false
    allowed_files:
      - "agent/sensors/parse_monitor_logs.py"
      - "agent/loops/post_morning_review.py"
      - "docs/SELF_IMPROVING_AGENT_SYSTEM.md"
      - "tests/test_agent_monitor_review.py"
    validation:
      - "python -m pytest tests/test_agent_monitor_review.py -q"
      - "python -m agent.gates.quality_gate --pytest tests/test_agent_monitor_review.py"
```

Additional context:

No additional context.
