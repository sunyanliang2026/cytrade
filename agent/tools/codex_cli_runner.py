"""Prepare and optionally run Codex CLI for one low-risk improvement task.

By default this script only writes a prompt file. Use ``--execute`` only after a
human has reviewed the task and command configuration.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
from datetime import datetime
from pathlib import Path

DEFAULT_TASKS_FILE = "agent/memory/improvement_tasks.yaml"
DEFAULT_PROMPT_DIR = "agent/memory/codex_prompts"


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "task")).strip("-") or "task"


def extract_task_block(tasks_text: str, task_id: str) -> str:
    """Extract a single task block from the generated YAML-like task file."""

    lines = tasks_text.splitlines()
    start = None
    for idx, line in enumerate(lines):
        if line.strip() in {f'- id: "{task_id}"', f"- id: {task_id}"}:
            start = idx
            break
    if start is None:
        raise ValueError(f"task id not found: {task_id}")

    end = len(lines)
    for idx in range(start + 1, len(lines)):
        if lines[idx].startswith("  - id: "):
            end = idx
            break
    return "\n".join(lines[start:end]).rstrip() + "\n"


def build_codex_prompt(*, task_block: str, extra_context: str = "") -> str:
    """Build a constrained prompt for Codex CLI."""

    return f"""You are working on the cytrade repository.

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
{task_block.rstrip()}
```

Additional context:

{extra_context.strip() or 'No additional context.'}
"""


def write_prompt(*, task_id: str, tasks_file: Path, prompt_dir: Path, extra_context: str = "") -> Path:
    task_block = extract_task_block(tasks_file.read_text(encoding="utf-8"), task_id)
    prompt = build_codex_prompt(task_block=task_block, extra_context=extra_context)
    prompt_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prompt_path = prompt_dir / f"{timestamp}_{_slug(task_id)}.md"
    prompt_path.write_text(prompt, encoding="utf-8")
    return prompt_path


def prepare_codex_command(codex_command: str) -> tuple[str | list[str], bool]:
    """Prepare a subprocess command in a cross-platform way."""

    text = str(codex_command or "").strip()
    if not text:
        raise ValueError("codex command is empty")
    if os.name == "nt":
        return text, True
    return shlex.split(text), False


def run_codex(prompt_path: Path, *, cwd: Path, codex_command: str) -> int:
    """Run a user-specified Codex CLI command with the prompt as stdin."""

    command, use_shell = prepare_codex_command(codex_command)
    prompt = prompt_path.read_text(encoding="utf-8")
    proc = subprocess.run(command, cwd=str(cwd), input=prompt, text=True, check=False, shell=use_shell)
    return proc.returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare a constrained Codex CLI prompt for one generated task.")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--tasks-file", default=DEFAULT_TASKS_FILE)
    parser.add_argument("--prompt-dir", default=DEFAULT_PROMPT_DIR)
    parser.add_argument("--cwd", default=".")
    parser.add_argument("--extra-context", default="")
    parser.add_argument("--execute", action="store_true", help="Actually run the Codex CLI command after writing the prompt.")
    parser.add_argument(
        "--codex-command",
        default=os.getenv("CYTRADE_CODEX_COMMAND", "codex"),
        help="Command to run when --execute is set. Example: 'codex exec'.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    prompt_path = write_prompt(
        task_id=args.task_id,
        tasks_file=Path(args.tasks_file),
        prompt_dir=Path(args.prompt_dir),
        extra_context=args.extra_context,
    )
    print(f"wrote Codex prompt: {prompt_path}")
    if not args.execute:
        print("not executing Codex CLI; pass --execute after human review")
        return 0
    return run_codex(prompt_path, cwd=Path(args.cwd).resolve(), codex_command=args.codex_command)


if __name__ == "__main__":
    raise SystemExit(main())
