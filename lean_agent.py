#!/usr/bin/env python3
"""Lean task agent.

Every step is a fresh, small API call. The chat history is never re-sent.
Progress lives in a short NOTES string (max 300 chars) that the model rewrites
each step, so the input per call stays roughly constant no matter how long the
task runs. Cache reads/writes stay near zero because nothing large repeats.

Usage:
  python lean_agent.py "task text" [--workdir DIR] [--model MODEL] [--max-steps N] [--timeout S] [--yes]
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import subprocess
import sys
from typing import Any, Callable, Sequence

SYSTEM = """You complete a task one step at a time. Each turn you see the TASK,
your NOTES from earlier steps, and the OUTPUT of the last command.
Reply with ONLY one JSON object, no prose:
{"notes": "<updated notes, max 300 chars: facts found, progress, next step>",
 "action": "run" or "done",
 "cmd": "<one shell command, only when action is run>",
 "summary": "<final answer, only when action is done>"}
Use small, focused commands. Never repeat a command already run."""

DEFAULT_MODEL = "claude-haiku-4-5"
NOTES_LIMIT = 300
OUTPUT_LIMIT = 2000
DEFAULT_TIMEOUT = 120


@dataclass
class AgentResult:
    summary: str
    completed: bool
    steps: int
    notes: str
    input_tokens: int
    output_tokens: int


def build_user_prompt(task: str, notes: str, last_output: str) -> str:
    return (
        f"TASK:\n{task}\n\n"
        f"NOTES:\n{notes or '(none)'}\n\n"
        f"OUTPUT:\n{last_output or '(none)'}"
    )


def parse_reply(text: str) -> dict[str, Any]:
    """Extract and parse the first JSON object from a model reply."""
    decoder = json.JSONDecoder()
    idx = 0
    while True:
        start = text.find("{", idx)
        if start == -1:
            raise ValueError("no JSON object in reply")
        try:
            obj, _ = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            idx = start + 1
            continue
        if isinstance(obj, dict):
            return obj
        idx = start + 1


def create_default_client() -> Any:
    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError(
            "The 'anthropic' package is required to call the Claude API. "
            "Install it with: pip install -r requirements.txt"
        ) from exc
    return anthropic.Anthropic()


def ask(
    client: Any,
    model: str,
    task: str,
    notes: str,
    last_output: str,
) -> tuple[dict[str, Any], Any]:
    user = build_user_prompt(task, notes, last_output)
    resp = client.messages.create(
        model=model,
        max_tokens=400,
        system=SYSTEM,
        messages=[{"role": "user", "content": user}],
    )
    text = resp.content[0].text
    return parse_reply(text), resp.usage


def run_shell_command(cmd: str, workdir: str = ".", timeout: int = DEFAULT_TIMEOUT) -> str:
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"Command timed out after {timeout}s."

    combined = proc.stdout + proc.stderr
    if proc.returncode != 0:
        suffix = f"\n[exit code: {proc.returncode}]"
        combined = combined + suffix if combined else f"[exit code: {proc.returncode}]"
    return combined[-OUTPUT_LIMIT:]


def run_agent(
    task: str,
    *,
    client: Any = None,
    model: str = DEFAULT_MODEL,
    workdir: str = ".",
    max_steps: int = 15,
    timeout: int = DEFAULT_TIMEOUT,
    auto_approve: bool = False,
    confirm_fn: Callable[[str], str] = input,
    command_runner: Callable[[str, str, int], str] = run_shell_command,
    print_fn: Callable[[str], None] = print,
) -> AgentResult:
    if max_steps < 1:
        raise ValueError("max_steps must be >= 1")
    if client is None:
        client = create_default_client()

    notes = ""
    last_output = ""
    tot_in = 0
    tot_out = 0
    steps_taken = 0

    for step in range(1, max_steps + 1):
        steps_taken = step
        try:
            data, usage = ask(client, model, task, notes, last_output)
        except (ValueError, json.JSONDecodeError):
            last_output = "Your last reply was not a valid JSON object. Reply with JSON only."
            continue

        tot_in += getattr(usage, "input_tokens", 0)
        tot_out += getattr(usage, "output_tokens", 0)
        notes = str(data.get("notes", ""))[:NOTES_LIMIT]

        action = str(data.get("action", "")).strip().lower()
        if action == "done":
            summary = str(data.get("summary", ""))
            print_fn(summary)
            print_fn(f"tokens: input={tot_in} output={tot_out}")
            return AgentResult(
                summary=summary,
                completed=True,
                steps=steps_taken,
                notes=notes,
                input_tokens=tot_in,
                output_tokens=tot_out,
            )

        if action != "run":
            last_output = f"Unsupported action '{action}'. Use 'run' or 'done'."
            continue

        cmd = str(data.get("cmd", "")).strip()
        if not cmd:
            last_output = "No command given."
            continue

        print_fn(f"[{step}] $ {cmd}")
        if not auto_approve and confirm_fn("run? [y/N] ").strip().lower() != "y":
            last_output = "User declined this command. Try another approach."
            continue

        last_output = command_runner(cmd, workdir, timeout)

    print_fn("Step cap reached without a final answer.")
    print_fn(f"tokens: input={tot_in} output={tot_out}")
    return AgentResult(
        summary="",
        completed=False,
        steps=steps_taken,
        notes=notes,
        input_tokens=tot_in,
        output_tokens=tot_out,
    )


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Stateless, constant-context CLI task agent powered by Claude."
    )
    ap.add_argument("task", help="Task description for the agent to complete")
    ap.add_argument("--workdir", default=".", help="Working directory for shell commands")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"Anthropic model ID (default: {DEFAULT_MODEL})")
    ap.add_argument("--max-steps", type=int, default=15, help="Maximum number of steps (default: 15)")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Per-command timeout in seconds (default: 120)")
    ap.add_argument("--yes", action="store_true", help="Run commands without asking for confirmation")
    return ap


def main(argv: Sequence[str] | None = None, *, client: Any = None) -> int:
    args = build_parser().parse_args(argv)
    result = run_agent(
        args.task,
        client=client,
        model=args.model,
        workdir=args.workdir,
        max_steps=args.max_steps,
        timeout=args.timeout,
        auto_approve=args.yes,
    )
    return 0 if result.completed else 1


if __name__ == "__main__":
    sys.exit(main())
