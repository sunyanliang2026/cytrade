import ast
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "qmt_native_smoke.py"


def test_native_smoke_runs_and_writes_userdata_log(tmp_path):
    (tmp_path / "log").mkdir()
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path), "123"],
        capture_output=True,
        text=True,
        check=True,
    )
    logs = list((tmp_path / "log").glob("qmt_native_smoke_*.log"))

    assert len(logs) == 1
    assert "QMT_NATIVE_SMOKE SCRIPT_START" in result.stdout
    assert "QMT_NATIVE_SMOKE SCRIPT_OK" in logs[0].read_text(encoding="ascii")


def test_native_smoke_uses_python36_syntax_and_no_trading_api():
    source = SCRIPT.read_text(encoding="ascii")
    ast.parse(source, feature_version=(3, 6))
    assert "def init(" not in source
    assert "def handlebar(" not in source
    assert "xtquant" not in source
    assert "passorder" not in source


def test_native_smoke_falls_back_to_temp_when_qmt_path_is_unreadable(tmp_path):
    (tmp_path / "log").mkdir()
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path / "missing")],
        capture_output=True,
        text=True,
        check=True,
        env={"TEMP": str(tmp_path)},
    )

    assert "SCRIPT_OK" in result.stdout
    assert "SCRIPT_OK" in next(tmp_path.glob("qmt_native_smoke_*.log")).read_text(
        encoding="ascii")
