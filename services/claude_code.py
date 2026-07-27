"""
Claude Code service — all LLM calls go through here via Claude Code CLI subprocess.
No Anthropic SDK. No API key. Uses Claude Pro subscription via the 'claude' CLI.

Two modes:
  think()  — agent reasoning (Engineering Manager, Architect, QA, etc.)
  execute() — code implementation in a managed project directory
"""
import subprocess
import shutil
from pathlib import Path


def _get_cli() -> str:
    path = shutil.which("claude")
    if not path:
        raise RuntimeError("'claude' CLI not found. Install Claude Code: https://claude.ai/code")
    return path


def think(system_prompt: str, user_prompt: str) -> str:
    """
    Call Claude for agent reasoning — planning, designing, reviewing, reporting.
    Runs non-interactively and returns the response as a string.
    """
    full_prompt = f"{system_prompt}\n\n---\n\n{user_prompt}"
    result = subprocess.run(
        [_get_cli(), "--print", "--dangerously-skip-permissions"],
        input=full_prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Claude Code error: {result.stderr}")
    return result.stdout.strip()


def execute(prompt: str, working_dir: str | Path) -> str:
    """
    Run Claude Code inside a project directory for code implementation.
    working_dir is the root of the project to work in.
    """
    cwd = Path(working_dir)
    if not cwd.exists():
        raise ValueError(f"Working directory does not exist: {cwd}")

    result = subprocess.run(
        [_get_cli(), "--print", "--dangerously-skip-permissions"],
        input=prompt,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=3600,  # code tasks can take up to an hour for large features
    )
    if result.returncode != 0:
        raise RuntimeError(f"Claude Code execution error: {result.stderr}")
    return result.stdout.strip()


def read_files(paths: list[str | Path], working_dir: str | Path) -> str:
    """
    Ask Claude Code to read and summarize specific files.
    Used by Architect to understand existing code before designing a spec.
    """
    file_list = "\n".join(str(p) for p in paths)
    prompt = f"Read these files and summarize their structure and purpose:\n{file_list}"
    return execute(prompt, working_dir)
