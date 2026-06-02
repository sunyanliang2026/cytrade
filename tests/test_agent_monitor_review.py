import json
from pathlib import Path

from agent.gates.quality_gate import scan_diff_for_safety
from agent.loops.generate_improvement_tasks import dump_tasks_yaml, generate_tasks
from agent.loops.post_morning_review import run_post_morning_review, select_run_session_events
from agent.sensors.parse_monitor_logs import format_markdown, parse_log_line, summarize_events
from agent.sensors.parse_pytest_output import parse_pytest_output


def _event(line: str):
    event = parse_log_line(line, source="sample.log")
    assert event is not None
    return event


def test_parse_monitor_logs_builds_morning_acceptance_summary():
    payload = {
        "event": "entry_signal_accepted",
        "stock": "000001.SZ",
        "dry_run": True,
        "reason": "limit_up_follow",
    }
    lines = [
        "MONITOR_SESSION pool_generated output=config/main_seal_follow_pool.csv source=combined total=52 amount=1000.0",
        "MONITOR_SESSION monitor_start csv=config/main_seal_follow_pool.csv stop_time=12:00 dry_run=True summary_mode=True",
        "MONITOR_SESSION session_start mode=market-only-monitor dry_run=True csv=config/main_seal_follow_pool.csv stop_at=12:00 account_connected=false",
        "Runtime heartbeat mode=market-only-monitor dry_run=True connected=True strategies=52 tick_subscriptions=52 l2_stocks=3 l2_kinds=9 latest_data_time=2026-05-25T09:45:00 data_delay_ms=100 process_ms=3.2",
        "MSF_EVENT " + json.dumps(payload),
        "MSF_EVENT " + json.dumps({"event": "dry_run_probe_trade_recorded", "stock": "000001.SZ", "dry_run": True}),
        "[ORDER] [TRADE] [MOCK] observation filled uuid=abc code=000001.SZ price=10.000 qty=100 traded_shares=1200 threshold=1000",
        "MONITOR_SESSION session_stop reason=noon_stop stop_time=12:00 strategy_count=52 tick_subscriptions=52 l2_stocks=3",
        "MONITOR_SESSION stopped system_log=logs/system.1.log trade_log=logs/trade.1.log dry_run=True real_order_sent=false",
    ]

    summary = summarize_events(_event(line) for line in lines)

    assert summary["pool_total"] == 52
    assert summary["max_strategies"] == 52
    assert summary["max_tick_subscriptions"] == 52
    assert summary["max_l2_stocks"] == 3
    assert summary["mock_trade_count"] == 1
    assert summary["stocks_with_msf_events"] == 1
    assert summary["stock_chains"][0]["stock"] == "000001.SZ"
    assert summary["stock_chains"][0]["entry_signal_accepted_count"] == 1
    assert summary["stock_chains"][0]["dry_run_probe_trade_count"] == 1
    assert summary["stock_chain_groups"]["entry_accepted"] == ["000001.SZ"]
    assert summary["stock_chain_groups"]["probe_trade_recorded"] == ["000001.SZ"]
    assert summary["checks"]["minimum_acceptance"] is True
    assert summary["checks"]["market_data_active"] is True
    assert summary["checks"]["dry_run_probe_trade_seen"] is True
    assert summary["real_order_suspected"] is False
    assert summary["invalid_monitor_reason"] == ""
    assert summary["review_verdict"] == "accepted"

    report = format_markdown(summary)
    assert "Session verdict" in report
    assert "Verdict: `accepted`" in report
    assert "Minimum acceptance" in report
    assert "Entry signal accepted" in report
    assert "Stock outcome groups" in report
    assert "Dry-run probe filled: `1` 000001.SZ" in report
    assert "Stock event chains" in report
    assert "| 000001.SZ |" in report
    assert "Invalid monitor reason: `none`" in report


def test_parse_monitor_logs_detects_possible_real_order_lines():
    summary = summarize_events(
        [
            _event("[ORDER] live order submitted code=000001.SZ price=10.0 qty=100"),
            _event("MONITOR_SESSION stopped dry_run=True real_order_sent=false"),
        ]
    )

    assert summary["real_order_suspected"] is True
    assert summary["checks"]["no_real_order_suspected"] is False

    tasks = generate_tasks(summary)
    assert tasks[0]["id"] == "investigate-possible-real-order-line"
    assert tasks[0]["risk"] == "high"
    assert tasks[0]["human_required"] is True


def test_parse_monitor_logs_ignores_internal_order_lines():
    summary = summarize_events(
        [
            _event("[ORDER] 注册订单 uuid=abc code=000001.SZ dir=BUY price=10.0 qty=100"),
            _event("[ORDER] 订单状态变更 uuid=abc status=FILLED"),
            _event("[ORDER] [TRADE] 忽略重复成交 trade_id=1 xt_order_id=2"),
            _event("[ORDER] 忽略无策略归属成交 xt_order_id=2 code=000001.SZ price=10.0 qty=100"),
            _event("[ORDER] 下单拦截 uuid=abc code=000001.SZ dir=BUY price=10.0 qty=100 reason=dry_run live_enabled=false"),
            _event("MONITOR_SESSION stopped dry_run=True real_order_sent=false"),
        ]
    )

    assert summary["possible_real_order_count"] == 0
    assert summary["real_order_suspected"] is False
    assert summary["checks"]["no_real_order_suspected"] is True


def test_parse_monitor_logs_flags_invalid_market_data_session():
    summary = summarize_events(
        [
            _event("MONITOR_SESSION pool_generated total=52"),
            _event("MONITOR_SESSION monitor_start dry_run=True"),
            _event("Runtime heartbeat mode=market-only-monitor dry_run=True connected=False strategies=52 tick_subscriptions=0 l2_stocks=52 l2_kinds=52 latest_data_time= last_recv_time= data_delay_ms=0 process_ms=0"),
            _event("Runtime heartbeat mode=market-only-monitor dry_run=True connected=False strategies=52 tick_subscriptions=0 l2_stocks=52 l2_kinds=52 latest_data_time= last_recv_time= data_delay_ms=0 process_ms=0"),
            _event("MONITOR_SESSION stopped dry_run=True real_order_sent=false"),
        ]
    )

    assert summary["checks"]["strategies_after_activation"] is True
    assert summary["checks"]["market_data_active"] is False
    assert summary["checks"]["invalid_monitor_session"] is True
    assert summary["invalid_monitor_reason"] == "market_data_not_connected"
    assert summary["review_verdict"] == "invalid_monitor_session"
    assert summary["checks"]["minimum_acceptance"] is False

    report = format_markdown(summary)
    assert "Verdict: `invalid_monitor_session`" in report
    assert "Invalid monitor session: `PASS` reason=`market_data_not_connected`" in report
    assert "Invalid monitor reason: `market_data_not_connected`" in report


def test_stock_chain_markdown_limits_details_but_keeps_json_complete():
    events = []
    for index in range(10):
        events.append(
            _event(
                "MSF_EVENT "
                + json.dumps(
                    {
                        "event": "entry_signal_blocked",
                        "stock": "000001.SZ",
                        "name": "平安银行",
                        "state": "WAIT_SIGNAL",
                        "reason": f"reason_{index}",
                        "source": "l2_order_queue",
                        "dry_run": True,
                        "metrics": {"front50_depth_lot": index},
                    },
                    ensure_ascii=False,
                )
            )
        )

    summary = summarize_events(events)
    report = format_markdown(summary)

    assert summary["stock_chains"][0]["event_count"] == 10
    assert len(summary["stock_chains"][0]["events"]) == 10
    assert "Detail limit" in report
    assert "omitted `2` more events for this stock" in report


def test_select_run_session_events_keeps_only_latest_stopped_session():
    repo_root = Path.cwd()
    events = [
        _event("MONITOR_SESSION monitor_start dry_run=True"),
        _event("Runtime heartbeat strategies=12 tick_subscriptions=12 l2_stocks=3 l2_kinds=9"),
        _event("MONITOR_SESSION stopped system_log=./logs/system.1.log trade_log=./logs/trade.1.log dry_run=True real_order_sent=false"),
        _event("MONITOR_SESSION monitor_start dry_run=True"),
        _event("Runtime heartbeat strategies=52 tick_subscriptions=52 l2_stocks=6 l2_kinds=12"),
        _event("MONITOR_SESSION stopped system_log=./logs/system.2.log trade_log=./logs/trade.2.log dry_run=True real_order_sent=false"),
    ]
    events[0].source = str(repo_root / "logs" / "system.1.log")
    events[1].source = str(repo_root / "logs" / "system.1.log")
    events[2].source = str(repo_root / "logs" / "system.1.log")
    events[3].source = str(repo_root / "logs" / "system.2.log")
    events[4].source = str(repo_root / "logs" / "system.2.log")
    events[5].source = str(repo_root / "logs" / "system.2.log")

    events[2].fields["_logged_at"] = "2026-05-24T11:30:00"
    events[5].fields["_logged_at"] = "2026-05-25T11:30:00"

    filtered = select_run_session_events(events, repo_root=repo_root, run_id="2026-05-25")
    summary = summarize_events(filtered)

    assert len(filtered) == 3
    assert summary["session_events"]["stopped"] == 1
    assert summary["max_strategies"] == 52


def test_select_run_session_events_does_not_fallback_to_old_session_for_run_id():
    repo_root = Path.cwd()
    events = [
        _event("Runtime heartbeat connected=False strategies=98 tick_subscriptions=0 l2_stocks=98"),
        _event("MONITOR_SESSION stopped system_log=./logs/system.old.log trade_log=./logs/trade.old.log dry_run=True real_order_sent=false"),
    ]
    events[0].fields["_logged_at"] = "2026-06-02T11:47:37"
    events[0].source = str(repo_root / "logs" / "system.today.log")
    events[1].fields["_logged_at"] = "2026-05-26T10:00:00"
    events[1].source = str(repo_root / "logs" / "system.old.log")

    filtered = select_run_session_events(events, repo_root=repo_root, run_id="2026-06-02")
    summary = summarize_events(filtered)

    assert filtered == []
    assert summary["heartbeat_count"] == 0
    assert summary["review_verdict"] == "invalid_monitor_session"
    assert summary["invalid_monitor_reason"] == "monitor_session_not_found"
    assert "stopped" not in summary["session_events"]


def test_run_post_morning_review_uses_latest_session_pair(tmp_path: Path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    system1 = logs_dir / "system.1.log"
    trade1 = logs_dir / "trade.1.log"
    system2 = logs_dir / "system.2.log"
    trade2 = logs_dir / "trade.2.log"

    system1.write_text(
        "\n".join(
            [
                '{"asctime":"2026-05-24T17:05:07","message":"MONITOR_SESSION monitor_start dry_run=True"}',
                '{"asctime":"2026-05-24T17:05:37","message":"Runtime heartbeat strategies=12 tick_subscriptions=12 l2_stocks=1 l2_kinds=3"}',
                f'{{"asctime":"2026-05-24T17:07:00","message":"MONITOR_SESSION stopped system_log=./logs/{system1.name} trade_log=./logs/{trade1.name} dry_run=True real_order_sent=false"}}',
            ]
        ),
        encoding="utf-8",
    )
    trade1.write_text("[ORDER] 注册订单 uuid=abc code=000001.SZ dir=BUY price=10.0 qty=100\n", encoding="utf-8")
    system2.write_text(
        "\n".join(
            [
                '{"asctime":"2026-05-25T08:50:16","message":"MONITOR_SESSION monitor_start dry_run=True"}',
                '{"asctime":"2026-05-25T09:30:37","message":"Runtime heartbeat connected=True strategies=52 tick_subscriptions=52 l2_stocks=6 l2_kinds=12 latest_data_time=2026-05-25T09:30:36"}',
                '{"asctime":"2026-05-25T09:35:00","message":"MSF_EVENT ' + json.dumps({"event": "entry_signal_accepted", "stock": "000001.SZ", "dry_run": True}).replace('"', '\\"') + '"}',
                f'{{"asctime":"2026-05-25T12:00:00","message":"MONITOR_SESSION stopped system_log=./logs/{system2.name} trade_log=./logs/{trade2.name} dry_run=True real_order_sent=false"}}',
            ]
        ),
        encoding="utf-8",
    )
    trade2.write_text("[ORDER] [TRADE] [MOCK] observation filled uuid=abc code=000001.SZ price=10.000 qty=100 traded_shares=1200 threshold=1000\n", encoding="utf-8")

    report = tmp_path / "morning.md"
    summary_json = tmp_path / "morning.json"
    tasks = tmp_path / "tasks.yaml"

    old_cwd = Path.cwd()
    try:
        import os
        os.chdir(tmp_path)
        paths = run_post_morning_review(
            logs=["logs/system.*.log", "logs/trade.*.log"],
            run_id="2026-05-25",
            report_path=str(report),
            summary_json_path=str(summary_json),
            tasks_path=str(tasks),
        )
    finally:
        os.chdir(old_cwd)

    summary = json.loads(paths["summary_json"].read_text(encoding="utf-8"))
    assert summary["max_strategies"] == 52
    assert summary["session_events"]["stopped"] == 1
    assert summary["reviewed_sources"] == sorted([str(system2.resolve()), str(trade2.resolve())])
    assert summary["real_order_suspected"] is False
    assert summary["review_verdict"] == "needs_review"


def test_generate_tasks_for_active_strategies_without_signals():
    summary = summarize_events(
        _event(line)
        for line in [
            "MONITOR_SESSION pool_generated total=52",
            "MONITOR_SESSION monitor_start dry_run=True",
            "Runtime heartbeat strategies=52 tick_subscriptions=52 l2_stocks=0 l2_kinds=0",
            "MONITOR_SESSION stopped dry_run=True real_order_sent=false",
        ]
    )

    tasks = generate_tasks(summary)
    task_ids = {task["id"] for task in tasks}

    assert "add-top-blocked-reason-summary" in task_ids
    assert dump_tasks_yaml(tasks).startswith("# Generated by agent.loops.generate_improvement_tasks")


def test_generate_tasks_prioritizes_invalid_market_data_session():
    summary = summarize_events(
        _event(line)
        for line in [
            "MONITOR_SESSION pool_generated total=52",
            "MONITOR_SESSION monitor_start dry_run=True",
            "Runtime heartbeat mode=market-only-monitor dry_run=True connected=False strategies=52 tick_subscriptions=0 l2_stocks=52 l2_kinds=52 latest_data_time= last_recv_time= data_delay_ms=0 process_ms=0",
            "MONITOR_SESSION stopped dry_run=True real_order_sent=false",
        ]
    )

    tasks = generate_tasks(summary)

    assert tasks[0]["id"] == "capture-account-login-diagnostics"
    assert tasks[0]["type"] == "diagnostics"


def test_quality_gate_safety_scan_blocks_dry_run_disablement():
    diff = """
diff --git a/config/settings.py b/config/settings.py
+CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN=False
"""

    findings = scan_diff_for_safety(diff)

    assert any("dry-run safety flag" in finding for finding in findings)


def test_parse_pytest_output_summary_line():
    output = "===================== 50 passed, 1 warning in 8.42s ====================="

    parsed = parse_pytest_output(output)

    assert parsed["passed"] == 50
    assert parsed["warnings"] == 1
    assert parsed["success"] is True
