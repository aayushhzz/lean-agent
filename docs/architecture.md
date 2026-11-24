# lean-agent Architecture

`lean-agent` is intentionally implemented as a single self-contained module ([`lean_agent.py`](../lean_agent.py)) with zero state persistence between API calls.

## Constant-Context Loop

Most CLI agents append every assistant turn and tool output to a growing `messages` array. `lean-agent` replaces conversation history with a fixed-budget state tuple passed as a single user message on every turn:

```text
TASK:
<original task string>

NOTES:
<model-rewritten scratchpad, truncated to NOTES_LIMIT = 300 chars>

OUTPUT:
<tail of last shell command stdout+stderr, truncated to OUTPUT_LIMIT = 2000 chars>
```

Each call invokes `client.messages.create(...)` with `max_tokens=400`, a compact `SYSTEM` prompt, and a single `user` message.

## JSON Action Protocol

The model must return a single JSON object (extracted robustly via `parse_reply`, which scans for the first valid JSON `dict` using `json.JSONDecoder.raw_decode`):

```json
{
  "notes": "Found 12 Python files; largest is server.py (42 KB). Ready to report.",
  "action": "done",
  "summary": "The largest Python file is server.py at 42 KB."
}
```

Or to execute a command:

```json
{
  "notes": "Need file sizes under current directory.",
  "action": "run",
  "cmd": "find . -name '*.py' -exec wc -c {} + | sort -nr | head -n 5"
}
```

## Fault & Safety Handling

- **Malformed JSON**: Feeds `"Your last reply was not a valid JSON object. Reply with JSON only."` back as `OUTPUT` and continues the loop.
- **Unsupported action / empty command**: Feeds an explicit corrective message into `OUTPUT` without crashing.
- **Interactive approval**: Unless `--yes` (`auto_approve=True`) is set, every command prompts `run? [y/N]`. Declining feeds `"User declined this command. Try another approach."` back to the model.
- **Timeouts & non-zero exit codes**: `run_shell_command` runs via `subprocess.run(..., shell=True, timeout=timeout)`, appends `[exit code: N]` on failure, and keeps only the last `2,000` characters.

## Programmatic Interface

`run_agent()` accepts injectable dependencies (`client`, `confirm_fn`, `command_runner`, `print_fn`), making it straightforward to embed or test offline without invoking real APIs or shell commands (as demonstrated in [`tests/test_lean_agent.py`](../tests/test_lean_agent.py)).
