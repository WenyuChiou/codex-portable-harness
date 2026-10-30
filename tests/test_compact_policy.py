import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import policy_engine
import test_harness


class CompactPolicyTests(unittest.TestCase):
    def call(self, checkpoint, *, compact=False):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/"checkpoint.json"
            path.write_text(json.dumps(checkpoint), encoding="utf-8")
            argv = ["policy_engine.py", "evaluate", str(path)] + (["--compact"] if compact else [])
            output = io.StringIO()
            with patch.object(sys, "argv", argv), contextlib.redirect_stdout(output):
                code = policy_engine.main()
            return code, json.loads(output.getvalue()), len(output.getvalue().encode("utf-8"))

    def test_summary_preserves_continue_and_stop_decisions(self):
        for fixture in [test_harness.PolicyTests().checkpoint(),
                        test_harness.PolicyTests().checkpoint(transcript_bytes=26*1024*1024),
                        test_harness.PolicyTests().checkpoint(cycles=3,tool_calls=75,elapsed_seconds=1800)]:
            full_code, full, full_bytes = self.call(fixture)
            short_code, short, short_bytes = self.call(fixture, compact=True)
            self.assertEqual(full_code, short_code)
            for key in ("decision", "scope", "auto_continue", "spawn_allowed", "checkpoint_required", "reasons", "policy_hash"):
                self.assertEqual(full.get(key), short[key])
            self.assertEqual(short["format"], "decision-summary.v1")
            self.assertLess(short_bytes, full_bytes)

    def test_invalid_checkpoint_stays_fail_closed(self):
        code, result, _ = self.call({}, compact=True)
        self.assertEqual(code, 4)
        self.assertEqual(result["decision"], "stop")
        self.assertFalse(result["spawn_allowed"])

    def test_summary_cannot_be_used_on_mutating_command(self):
        with patch.object(sys, "argv", ["policy_engine.py", "advance", "absent.json", "--compact"]), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as stopped:
                policy_engine.main()
            self.assertEqual(stopped.exception.code, 2)
