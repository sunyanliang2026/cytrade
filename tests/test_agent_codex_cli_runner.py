from agent.tools.codex_cli_runner import prepare_codex_command


def test_prepare_codex_command_windows_style():
    command, use_shell = prepare_codex_command(r"C:\Users\ysun\AppData\Roaming\npm\codex.cmd")

    assert use_shell is True
    assert command == r"C:\Users\ysun\AppData\Roaming\npm\codex.cmd"
