"""Data-free consistency tests.

These tests need NO dataset and build NO MEDA-like records. They check the
package's static structure: schema files, schema derivation, rule registry
integrity, configuration gating, and Normalizer independence.
Run:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path


from pathlib import Path

# Project root: marswalk-intelligence/
PROJECT_ROOT = Path(__file__).resolve().parents[4]

# Filtering service
PKG = (
    PROJECT_ROOT
    / "backend"
    / "app"
    / "services"
    / "weather"
    / "filteration_service"
)

SCHEMAS = PKG / "schemas"
ROOT = PKG



def _load(name: str) -> dict:
    return json.loads((SCHEMAS / name).read_text(encoding="utf-8"))


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(("." * node.level) + (node.module or ""))
    return found


class SchemaFiles(unittest.TestCase):
    def test_schemas_load_in_engine(self):
        from filteration_service.schemas._schema_engine import SchemaEngine, load_schema
        for name in ("normalized_schema.json", "filtered_schema.json"):
            SchemaEngine(load_schema(SCHEMAS / name))  # raises on unsupported keyword / bad $ref

    def test_versions_come_from_schema_ids(self):
        from filteration_service.schemas.filtered_schema_validator import FILTERED_SCHEMA_VERSION
        from filteration_service.schemas.normalized_schema_validator import NORMALIZED_SCHEMA_VERSION
        self.assertTrue(_load("normalized_schema.json")["$id"].endswith("v" + NORMALIZED_SCHEMA_VERSION))
        self.assertTrue(_load("filtered_schema.json")["$id"].endswith("v" + FILTERED_SCHEMA_VERSION))

    def test_filtered_schema_is_derived_from_normalized(self):
        n, f = _load("normalized_schema.json"), _load("filtered_schema.json")
        for key in ("sol", "sclk_s", "lmst", "ltst", "utc", "sources", "missing", "conflicts"):
            self.assertEqual(n["properties"][key], f["properties"][key], key)
        for name in ("uncertainty", "source_provenance", "conflict_record"):
            self.assertEqual(n["$defs"][name], f["$defs"][name], name)
        extra = set(f["properties"]) - set(n["properties"])
        self.assertEqual(extra, {"filtering_metadata"})
        dropped = set(n["$defs"]["measurement"]["properties"]) - set(f["$defs"]["accepted_measurement"]["properties"])
        self.assertEqual(dropped, {"raw_value", "validity_reason"})
        added = set(f["$defs"]["accepted_measurement"]["properties"]) - set(n["$defs"]["measurement"]["properties"])
        self.assertEqual(added, set(), "the filter must not add fields to measurement objects")

    def test_normalized_required_members_match_normalizer_contract(self):
        # normalize.py / streaming._REQUIRED_KEYS
        self.assertEqual(
            set(_load("normalized_schema.json")["required"]),
            {"sol", "sclk_s", "lmst", "ltst", "utc", "measurements", "quality", "sources"},
        )

    def test_filtered_schema_defines_no_unproduced_members(self):
        f = _load("filtered_schema.json")
        self.assertNotIn("rejected_summary", json.dumps(f))
        self.assertEqual(
            set(f["$defs"]["filtering_metadata"]["properties"]),
            {"ruleset_version", "measurements_evaluated", "measurements_accepted",
             "measurements_rejected", "measurements_review_required"},
        )


class Independence(unittest.TestCase):
    def test_no_normalizer_import(self):
        for path in PKG.rglob("*.py"):
            for mod in _imports(path):
                self.assertNotEqual(mod.split(".")[0], "normalization_service", f"{path} imports the Normalizer")

    def test_validators_contain_no_filtering(self):
        for name in ("normalized_schema_validator.py", "filtered_schema_validator.py"):
            mods = _imports(SCHEMAS / name)
            for banned in ("..rules", "..filter", "rules", "filter"):
                self.assertNotIn(banned, mods, name)

    def test_only_standard_library(self):
        import sys
        stdlib = set(sys.stdlib_module_names)
        for path in PKG.rglob("*.py"):
            for mod in _imports(path):
                if mod.startswith("."):
                    continue
                self.assertIn(mod.split(".")[0], stdlib | {"filteration_service"}, f"{path}: {mod}")


class RuleRegistry(unittest.TestCase):
    def test_every_rule_documents_itself(self):
        from filteration_service.rules import RuleStatus, get_all_rules
        for r in get_all_rules():
            self.assertTrue(r.description.strip(), r.rule_id)
            if r.status == RuleStatus.ACTIVE:
                self.assertTrue(r.fn is not None and r.evidence.strip() and r.evidence != "NOT AVAILABLE", r.rule_id)
            else:
                self.assertIsNone(r.fn, r.rule_id)
                self.assertTrue(r.pending_documentation.strip(), r.rule_id)

    def test_default_ruleset_has_no_threshold_or_flag_policy(self):
        from filteration_service.rules import build_ruleset
        ids = {r.rule_id for r in build_ruleset().active}
        self.assertEqual(ids, {"FILT-001", "FILT-002", "FILT-003", "FILT-004", "FILT-005"})

    def test_pending_rules_cannot_be_enabled_by_config(self):
        from filteration_service.rules import RulesConfigError, build_ruleset
        with self.assertRaises(RulesConfigError):
            build_ruleset({"disabled_rules": [{"rule_id": "FILT-P01", "justification": "x"}]})

    def test_core_rules_cannot_be_disabled(self):
        from filteration_service.rules import RulesConfigError, build_ruleset
        for rid in ("FILT-001", "FILT-002", "FILT-003"):
            with self.assertRaises(RulesConfigError):
                build_ruleset({"disabled_rules": [{"rule_id": rid, "justification": "x"}]})

    def test_policies_require_evidence(self):
        from filteration_service.rules import RulesConfigError, build_ruleset
        no_evidence = {"policy_id": "A", "variables": ["v"], "min": 0, "decision": "rejected"}
        with self.assertRaises(RulesConfigError):
            build_ruleset({"range_policies": [no_evidence]})
        with self.assertRaises(RulesConfigError):
            build_ruleset({"range_policies": [{**no_evidence, "evidence": "  "}]})
        with self.assertRaises(RulesConfigError):
            build_ruleset({"flag_policies": [{"policy_id": "F", "flag": "X", "variables": ["v"],
                                              "trigger_values": [0], "decision": "review_required"}]})

    def test_policy_cannot_accept(self):
        from filteration_service.rules import RulesConfigError, build_ruleset
        with self.assertRaises(RulesConfigError):
            build_ruleset({"range_policies": [{"policy_id": "A", "variables": ["v"], "min": 0,
                                               "decision": "accepted", "evidence": "doc"}]})

    def test_source_selection_priority_needs_evidence_and_known_members(self):
        from filteration_service.rules import RulesConfigError, build_ruleset
        with self.assertRaises(RulesConfigError):
            build_ruleset({"source_selection": [{"group_id": "pressure", "policy": "priority",
                                                 "priority": ["pressure_pa"]}]})
        with self.assertRaises(RulesConfigError):
            build_ruleset({"source_selection": [{"group_id": "pressure", "policy": "priority",
                                                 "priority": ["not_a_member"], "evidence": "doc"}]})
        with self.assertRaises(RulesConfigError):
            build_ruleset({"source_selection": [{"group_id": "unknown", "policy": "keep_all"}]})

    def test_default_source_policy_is_keep_all(self):
        from filteration_service.rules import build_ruleset
        self.assertTrue(all(p.policy == "keep_all" for p in build_ruleset().source_policies.values()))

    def test_example_rules_config_is_inert(self):
        from filteration_service.rules import build_ruleset, load_rules_config
        cfg = load_rules_config(SCHEMAS / "rules_config.example.json")
        self.assertEqual({r.rule_id for r in build_ruleset(cfg).active},
                         {r.rule_id for r in build_ruleset().active})

    def test_flag_wording_matches_documented_flags(self):
        from filteration_service.rules import FLAG_WORDING
        self.assertEqual(len(FLAG_WORDING), 14)


class SchemaEngineGeneric(unittest.TestCase):
    """Engine behaviour on trivial, non-MEDA schemas."""

    def test_engine_basics(self):
        from filteration_service.schemas._schema_engine import SchemaEngine
        e = SchemaEngine({"type": "object", "required": ["a"], "additionalProperties": False,
                          "properties": {"a": {"type": ["integer", "null"]}}})
        self.assertIsNone(e.first_error({"a": 1}))
        self.assertIsNone(e.first_error({"a": None}))
        self.assertIsNotNone(e.first_error({}))
        self.assertIsNotNone(e.first_error({"a": True}))      # bool is not an integer
        self.assertIsNotNone(e.first_error({"a": 1, "b": 2}))  # extra member

    def test_unsupported_keyword_fails_closed(self):
        from filteration_service.schemas._schema_engine import SchemaDefinitionError, SchemaEngine
        with self.assertRaises(SchemaDefinitionError):
            SchemaEngine({"type": "object", "patternProperties": {}})

    def test_engine_does_not_mutate_instance(self):
        from filteration_service.schemas._schema_engine import SchemaEngine
        inst = {"a": [1, 2]}
        before = json.dumps(inst)
        list(SchemaEngine({"type": "object"}).iter_errors(inst))
        self.assertEqual(json.dumps(inst), before)


class CliSurface(unittest.TestCase):
    def test_parser_exposes_specified_options(self):
        from filteration_service.cli import _parser
        a = _parser().parse_args(["--input", "i", "--output", "o", "--report", "r", "--rules", "c", "--strict"])
        self.assertEqual((a.input, a.output, a.report, a.rules, a.strict), ("i", "o", "r", "c", True))

    def test_missing_input_is_fatal_without_reading_anything(self):
        from filteration_service.cli import main
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(main(["--input", str(Path(d) / "absent.jsonl")]), 2)


if __name__ == "__main__":
    unittest.main()
