from runtime.heartbeat import _build_heartbeat_signature, start_runtime_heartbeat


class _FakeSettings:
    CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN = True
    RUNTIME_HEARTBEAT_INTERVAL_SEC = 5
    RUNTIME_HEARTBEAT_STABLE_REPEAT = 1


class _FakeStopEvent:
    def __init__(self):
        self.calls = 0

    def wait(self, interval):
        self.calls += 1
        return self.calls > 1


class _FakeThread:
    def __init__(self, target, daemon=False, name=""):
        self._target = target
        self.daemon = daemon
        self.name = name

    def start(self):
        self._target()


class _FakeDataSub:
    def get_l2_subscription_map(self):
        return {}

    def get_latest_data_status(self):
        return {"latest_data_time": None, "last_recv_time": None, "data_delay_ms": 0.0}

    def get_subscription_list(self):
        return []


class _FakeRunner:
    def get_runtime_status(self):
        return {
            "strategy_count": 2,
            "last_strategy_event": "",
            "last_strategy_event_time": None,
            "last_round_total_process_ms": 0.0,
        }


class _FakeConn:
    def get_last_error(self):
        return {}

    def is_trading_ready(self):
        return True

    def is_connected(self):
        return True


class _FakeWatchdog:
    def __init__(self):
        self.heartbeats = []

    def register_heartbeat(self, source):
        self.heartbeats.append(source)


def test_runtime_heartbeat_refreshes_watchdog_when_market_is_quiet(monkeypatch):
    monkeypatch.setattr("runtime.heartbeat.threading.Thread", _FakeThread)
    watchdog = _FakeWatchdog()

    start_runtime_heartbeat(
        {
            "settings": _FakeSettings(),
            "runner": _FakeRunner(),
            "data_sub": _FakeDataSub(),
            "conn_mgr": _FakeConn(),
            "watchdog": watchdog,
        },
        _FakeStopEvent(),
        mode="unit",
    )

    assert watchdog.heartbeats == ["strategy_runner"]


def test_runtime_heartbeat_signature_ignores_volatile_market_timestamps():
    base_runner_status = {
        "strategy_count": 2,
        "last_strategy_event": "tick:1",
        "last_strategy_event_time": "2026-07-02 13:18:40",
    }
    later_runner_status = {
        **base_runner_status,
        "last_strategy_event_time": "2026-07-02 13:19:10",
    }

    first = _build_heartbeat_signature(
        _FakeSettings(),
        base_runner_status,
        2,
        {},
        connected=True,
        trading_ready=True,
        conn_last_error={},
    )
    second = _build_heartbeat_signature(
        _FakeSettings(),
        later_runner_status,
        2,
        {},
        connected=True,
        trading_ready=True,
        conn_last_error={},
    )

    assert first == second
