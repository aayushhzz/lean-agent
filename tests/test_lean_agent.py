import json
from types import SimpleNamespace
import unittest

import lean_agent


class FakeClient:
    """Deterministic stand-in for anthropic.Anthropic."""

    def __init__(self, replies, input_tokens=50, output_tokens=20):
        self._replies = list(replies)
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        text = self._replies.pop(0)
        return SimpleNamespace(
            content=[SimpleNamespace(text=text)],
            usage=SimpleNamespace(
                input_tokens=self._input_tokens,
                output_tokens=self._output_tokens,
            ),
        )


class TestParseReply(unittest.TestCase):
    def test_plain_json(self):
        obj = lean_agent.parse_reply('{"notes": "ok", "action": "done", "summary": "hi"}')
        self.assertEqual(obj["action"], "done")
        self.assertEqual(obj["summary"], "hi")

    def test_markdown_fenced_and_prose(self):
        text = 'Sure, here is the step:\n```json\n{"notes": "step 1", "action": "run", "cmd": "ls"}\n```\nThanks!'
        obj = lean_agent.parse_reply(text)
        self.assertEqual(obj["cmd"], "ls")

    def test_ignores_leading_non_json_brace(self):
        text = 'Note {not json} -> {"notes": "n", "action": "done", "summary": "s"}'
        obj = lean_agent.parse_reply(text)
        self.assertEqual(obj["summary"], "s")

    def test_no_json_raises(self):
        with self.assertRaises(ValueError):
            lean_agent.parse_reply("just plain text without braces")

    def test_json_array_only_raises(self):
        with self.assertRaises(ValueError):
            lean_agent.parse_reply("[1, 2, 3]")


class TestBuildPromptAndShell(unittest.TestCase):
    def test_build_user_prompt_defaults(self):
        prompt = lean_agent.build_user_prompt("do x", "", "")
        self.assertIn("TASK:\ndo x", prompt)
        self.assertIn("NOTES:\n(none)", prompt)
        self.assertIn("OUTPUT:\n(none)", prompt)

    def test_run_shell_command_success(self):
        out = lean_agent.run_shell_command("printf 'hello-lean'")
        self.assertEqual(out, "hello-lean")

    def test_run_shell_command_nonzero_exit(self):
        out = lean_agent.run_shell_command("echo oops && exit 3")
        self.assertIn("oops", out)
        self.assertIn("[exit code: 3]", out)

    def test_run_shell_command_truncates_output(self):
        out = lean_agent.run_shell_command("python3 -c \"print('a' * 3000)\"")
        self.assertEqual(len(out), lean_agent.OUTPUT_LIMIT)

    def test_run_shell_command_timeout(self):
        out = lean_agent.run_shell_command("python3 -c 'import time; time.sleep(2)'", timeout=1)
        self.assertIn("timed out after 1s", out)


class TestRunAgent(unittest.TestCase):
    def test_multi_step_completion_and_constant_context(self):
        long_note = "x" * 500
        client = FakeClient([
            json.dumps({"notes": long_note, "action": "run", "cmd": "echo 42"}),
            json.dumps({"notes": "got 42", "action": "done", "summary": "The answer is 42"}),
        ])
        logs = []
        res = lean_agent.run_agent(
            "compute answer",
            client=client,
            auto_approve=True,
            command_runner=lambda cmd, cwd, timeout: "42\n",
            print_fn=logs.append,
        )
        self.assertTrue(res.completed)
        self.assertEqual(res.steps, 2)
        self.assertEqual(res.summary, "The answer is 42")
        self.assertEqual(res.input_tokens, 100)
        self.assertEqual(res.output_tokens, 40)
        # Each API call must send only a single user message (no accumulated history)
        for call in client.calls:
            self.assertEqual(len(call["messages"]), 1)
        # Notes passed into step 2 must be truncated to NOTES_LIMIT
        step2_msg = client.calls[1]["messages"][0]["content"]
        self.assertIn("x" * lean_agent.NOTES_LIMIT, step2_msg)
        self.assertNotIn("x" * (lean_agent.NOTES_LIMIT + 1), step2_msg)
        self.assertIn("OUTPUT:\n42\n", step2_msg)

    def test_invalid_json_recovers_on_next_step(self):
        client = FakeClient([
            "oops not json",
            json.dumps({"notes": "fixed", "action": "done", "summary": "recovered"}),
        ])
        res = lean_agent.run_agent("recover", client=client, auto_approve=True, print_fn=lambda _: None)
        self.assertTrue(res.completed)
        self.assertEqual(res.summary, "recovered")
        self.assertIn("not a valid JSON object", client.calls[1]["messages"][0]["content"])

    def test_declined_command_does_not_execute(self):
        client = FakeClient([
            json.dumps({"notes": "try rm", "action": "run", "cmd": "rm -rf /tmp/nope"}),
            json.dumps({"notes": "aborted", "action": "done", "summary": "stopped safely"}),
        ])
        executed = []
        res = lean_agent.run_agent(
            "safe task",
            client=client,
            auto_approve=False,
            confirm_fn=lambda _: "n",
            command_runner=lambda cmd, cwd, timeout: executed.append(cmd) or "",
            print_fn=lambda _: None,
        )
        self.assertTrue(res.completed)
        self.assertEqual(executed, [])
        self.assertIn("User declined", client.calls[1]["messages"][0]["content"])

    def test_empty_cmd_and_unknown_action_handled(self):
        client = FakeClient([
            json.dumps({"notes": "n1", "action": "run", "cmd": "   "}),
            json.dumps({"notes": "n2", "action": "fly"}),
            json.dumps({"notes": "n3", "action": "done", "summary": "ok"}),
        ])
        res = lean_agent.run_agent("edge cases", client=client, auto_approve=True, print_fn=lambda _: None)
        self.assertTrue(res.completed)
        self.assertIn("No command given.", client.calls[1]["messages"][0]["content"])
        self.assertIn("Unsupported action 'fly'", client.calls[2]["messages"][0]["content"])

    def test_step_cap_returns_incomplete_and_main_exits_1(self):
        client = FakeClient([
            json.dumps({"notes": "1", "action": "run", "cmd": "echo 1"}),
            json.dumps({"notes": "2", "action": "run", "cmd": "echo 2"}),
        ])
        rc = lean_agent.main(["never finish", "--max-steps", "2", "--yes"], client=client)
        self.assertEqual(rc, 1)

    def test_main_returns_0_on_success(self):
        client = FakeClient([
            json.dumps({"notes": "done", "action": "done", "summary": "finished"}),
        ])
        rc = lean_agent.main(["quick task", "--yes"], client=client)
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
