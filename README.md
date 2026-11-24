# lean-agent

A minimal, stateless CLI task agent powered by Claude.

Instead of resending a growing conversation history on every turn, `lean-agent` makes each step a fresh, small API call containing only:
1. The original `TASK`
2. A compact `NOTES` string (capped at 300 characters) rewritten by the model each turn
3. The truncated `OUTPUT` (last 2,000 characters) of the previous shell command

This keeps per-turn input tokens roughly constant regardless of task length.

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

Set your API key before running:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

## Usage

```bash
# Run with interactive command approval (default)
lean-agent "Find the largest Python file in this directory"

# Or invoke the script directly
python3 lean_agent.py "Count TODOs in src/" --workdir ./my-repo --max-steps 10

# Auto-approve shell commands (use with caution)
lean-agent "Check git status and summarize modified files" --yes
```

### CLI Options

| Flag | Default | Description |
|---|---|---|
| `task` | *(required)* | Natural-language task for the agent |
| `--workdir DIR` | `.` | Working directory for shell commands |
| `--model MODEL` | `claude-haiku-4-5` | Anthropic model ID |
| `--max-steps N` | `15` | Maximum agent steps before stopping |
| `--timeout S` | `120` | Per-command timeout in seconds |
| `--yes` | `False` | Run commands without interactive `run? [y/N]` confirmation |

Exit code is `0` when the agent returns `"action": "done"`, and `1` if the step cap is reached without finishing.

## Running Tests

Tests run completely offline using a fake Anthropic client (no API key or `anthropic` package required):

```bash
python3 -m unittest discover -s tests -v
```

Or with `pytest`:

```bash
pytest -v
```
