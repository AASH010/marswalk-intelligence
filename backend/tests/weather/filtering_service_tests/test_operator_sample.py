"""Data-level checks for the OPERATOR to run on their own Normalized JSONL.

Skipped unless the environment variable MEDA_NORMALIZED_SAMPLE points to a
Normalized JSONL file. The package author has NOT run these tests: no
Normalized dataset was available during development.

    PowerShell:  $env:MEDA_NORMALIZED_SAMPLE = "meda_normalized.jsonl"
    Bash:        export MEDA_NORMALIZED_SAMPLE=meda_normalized.jsonl
    python -m unittest tests.test_operator_sample -v
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

SAMPLE = os.environ.get("MEDA_NORMALIZED_SAMPLE")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@unittest.skipUnless(SAMPLE and Path(SAMPLE).is_file(), "set MEDA_NORMALIZED_SAMPLE to a Normalized JSONL file")
class OperatorSampleRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from filteration_service.cli import main
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        cls.src = Path(SAMPLE)
        cls.before = _sha(cls.src)
        cls.out, cls.rep = d / "meda_filtered.jsonl", d / "filtering_report.json"
        cls.code = main(["--input", str(cls.src), "--output", str(cls.out), "--report", str(cls.rep)])
        cls.report = json.loads(cls.rep.read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_input_untouched(self):
        self.assertEqual(_sha(self.src), self.before)
        self.assertEqual(self.report["input"]["sha256"], self.before)

    def test_run_completed(self):
        self.assertIn(self.code, (0, 1))
        self.assertNotEqual(self.report["processing_status"], "failed")

    def test_output_validates_against_filtered_schema(self):
        from filteration_service.schemas.filtered_schema_validator import validate_jsonl_file
        self.assertEqual([str(e) for e in validate_jsonl_file(self.out)], [])

    def test_report_arithmetic(self):
        t = self.report["totals"]
        self.assertEqual(t["measurements_evaluated"],
                         t["measurements_accepted"] + t["measurements_rejected"] + t["measurements_review_required"])
        written = sum(1 for _ in self.out.open(encoding="utf-8"))
        self.assertEqual(t["records_written"], written)
        in_filtered = sum(len(json.loads(l)["measurements"]) for l in self.out.open(encoding="utf-8"))
        # source-selection drops are counted as rejected, so accepted == measurements present in the output
        self.assertEqual(t["measurements_accepted"], in_filtered)

    def test_accepted_measurements_are_unchanged_copies(self):
        src = {}
        with self.src.open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    r = json.loads(line)
                    src[(r["sol"], r["sclk_s"])] = r
        with self.out.open(encoding="utf-8") as fh:
            for line in fh:
                f = json.loads(line)
                r = src[(f["sol"], f["sclk_s"])]
                for key in ("sol", "sclk_s", "lmst", "ltst", "utc", "quality", "sources"):
                    self.assertEqual(f[key], r[key], key)
                for name, meas in f["measurements"].items():
                    self.assertEqual(meas, r["measurements"][name], name)
                    self.assertEqual(meas["validity"], "valid")


if __name__ == "__main__":
    unittest.main()
