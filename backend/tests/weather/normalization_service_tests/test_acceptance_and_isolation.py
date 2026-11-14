"""Acceptance model, failure isolation, publication gate, scientific rules, SCLK, archive and report tests.

These tests ADD to test_normalization_service_pipeline.py (none of those were removed).
They call ``cli.main`` directly and assert PASS / PARTIAL / FAIL explicitly.
"""
from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

import pytest

from normalization_service import archive, context, normalize, parser, registry, streaming
import test_meda_weather_pipeline as T
from normalization_service.cli import ExitCode, main

mkrow, write_product = T.mkrow, T.write_product


@pytest.fixture()
def dataset(tmp_path):
    return T.build_dataset(tmp_path / "ds")


# ---------------------------------------------------------------- helpers
def clean_dataset(base: Path) -> dict:
    """PS only: every channel has a label unit that matches the mapping -> eligible for PASS."""
    cal, der = base / "cal" / "sol_0230", base / "der" / "PS"
    p1 = write_product(cal, 230, "PS", "calibrated", [
        mkrow("PS", "calibrated", 1000, BAROCAP1_PRESSURE=671.69, BAROCAP2_PRESSURE=662.2, THERMOCAP1_TEMP=283.8,
              PRESSURE_MEASUREMENT_MODE="nominal", TRANSDUCER=1),
        mkrow("PS", "calibrated", 1001, BAROCAP1_PRESSURE=671.67, PRESSURE_MEASUREMENT_MODE="nominal", TRANSDUCER=1)])
    p2 = write_product(der, 230, "PS", "derived", [mkrow("PS", "derived", 1000, PRESSURE=662.25, TRANSDUCER=1),
                                                     mkrow("PS", "derived", 1001, PRESSURE=662.28, TRANSDUCER=1)])
    return {"cal": base / "cal", "der": base / "der", "files": [p1, p2]}


def add_row(path: Path, product: str, level: str, cells: dict, sclk) -> None:
    """Append one CSV row and keep the label's declared record count consistent."""
    row = mkrow(product, level, sclk, **cells)
    path.write_text(path.read_text() + ",".join(row) + "\r\n")
    n = len(path.read_text().splitlines()) - 1
    path.with_suffix(".xml").write_text(T.label_xml(product, level, T.H[(product, level)], n))


def run(tmp_path, ds, *extra, out="o.jsonl", cal=True, der=True):
    out_path = tmp_path / out
    argv = []
    if cal:
        argv += ["--calibrated", ds["cal"]]
    if der:
        argv += ["--derived", ds["der"]]
    argv += ["--output", out_path, "--log-level", "ERROR", *extra]
    code = main([str(a) for a in argv])
    return code, out_path


def report_of(out_path: Path) -> dict:
    ok = Path(str(out_path) + ".report.json")
    if ok.exists():
        return json.loads(ok.read_text())
    failed = sorted(out_path.parent.glob(f"{out_path.name}.report.FAILED.*.json"))
    assert failed, f"no report next to {out_path}: {sorted(p.name for p in out_path.parent.iterdir())}"
    return json.loads(failed[-1].read_text())


def records(path: Path):
    return [json.loads(line) for line in path.read_text().splitlines()]


# ---------------------------------------------------------------- Acceptance
def test_pass_when_all_mandatory_checks_pass_and_nothing_isolated(tmp_path):
    code, out = run(tmp_path, clean_dataset(tmp_path / "d"))
    rep = report_of(out)
    assert code == ExitCode.SUCCESS == 0 and rep["2_acceptance_decision"]["status"] == "PASS"
    assert out.exists() and rep["3_publication_decision"]["dataset"]["status"] == "PUBLISHED"
    checks = rep["19_test_results"]
    assert checks["failed"] == 0 and checks["not_executed"] == 0 and checks["passed"] == checks["executed"] > 5


def test_partial_when_isolatable_issue_exists_and_remainder_is_valid(tmp_path, dataset):
    code, out = run(tmp_path, dataset)
    rep = report_of(out)
    assert code == ExitCode.PARTIAL == 10 and out.exists()
    acc = rep["2_acceptance_decision"]
    assert acc["status"] == "PARTIAL" and acc["publishable"] and acc["reasons"]
    assert acc["mandatory_checks_passed"] == acc["mandatory_checks_total"]
    assert code != ExitCode.SUCCESS  # PARTIAL is never reported as full success


def test_fail_on_critical_failure_corrupt_archive(tmp_path):
    ds = clean_dataset(tmp_path / "d")
    z = tmp_path / "cal.zip"
    T.zip_dir(ds["cal"], z, ds["cal"].parent)
    raw = bytearray(z.read_bytes())
    raw[len(raw) // 3] ^= 0xFF  # flip a byte inside the stored data
    z.write_bytes(bytes(raw))
    out = tmp_path / "o.jsonl"
    out.write_text("PREVIOUS GOOD DATASET")
    code = main(["--calibrated", str(z), "--output", str(out), "--overwrite", "--log-level", "ERROR"])
    assert code == ExitCode.INVALID_INPUT
    assert out.read_text() == "PREVIOUS GOOD DATASET"  # last good dataset untouched
    rep = report_of(out)
    assert rep["2_acceptance_decision"]["status"] == "FAIL"
    insp = rep["5_archive_inspection"][0]
    assert insp["crc_validation"] == "FAILED" and (insp["corrupt_entries"] or insp["unreadable_entries"])
    assert any(f["severity"] == "CRITICAL" for f in rep["14_failure_events"])


def test_no_strict_never_turns_fail_into_pass(tmp_path):
    ds = clean_dataset(tmp_path / "d")
    z = tmp_path / "bad.zip"
    z.write_bytes(b"this is not a zip file")
    for flag in ("--no-strict", "--strict"):
        out = tmp_path / f"o{flag}.jsonl"
        code = main(["--calibrated", str(z), "--output", str(out), "--log-level", "ERROR", flag])
        assert code == ExitCode.INVALID_INPUT and not out.exists()
        assert report_of(out)["2_acceptance_decision"]["status"] == "FAIL"


def test_strict_makes_isolation_a_failure_and_keeps_old_output(tmp_path, dataset):
    out = tmp_path / "o.jsonl"
    out.write_text("OLD")
    code = main(["--calibrated", str(dataset["cal"]), "--derived", str(dataset["der"]), "--output", str(out),
                 "--overwrite", "--strict", "--log-level", "ERROR"])
    assert code == ExitCode.ACCEPTANCE_FAILURE and out.read_text() == "OLD"
    rep = report_of(out)
    assert rep["2_acceptance_decision"]["status"] == "FAIL"
    assert rep["3_publication_decision"]["dataset"]["previous_dataset_preserved"] is True
    rejected = list(tmp_path.glob("o.jsonl.rejected.*.jsonl"))
    assert len(rejected) == 1 and streaming.validate_jsonl(rejected[0]) > 0  # kept for diagnosis


def test_discard_rejected_removes_staging(tmp_path, dataset):
    out = tmp_path / "o.jsonl"
    main(["--calibrated", str(dataset["cal"]), "--derived", str(dataset["der"]), "--output", str(out), "--strict",
          "--discard-rejected", "--log-level", "ERROR"])
    assert not list(tmp_path.glob("*.rejected.*")) and not out.exists()


# ---------------------------------------------------------------- Failure isolation
def _two_ps_sols(base):
    ds = clean_dataset(base)
    # a second Sol whose derived PS product is unreadable (no SCLK column)
    bad = write_product(ds["der"] / "PS", 231, "PS", "derived", [["1", "2"]], header=["A", "B"])
    return ds, bad


def test_one_bad_product_does_not_delete_good_products(tmp_path):
    ds, bad = _two_ps_sols(tmp_path / "d")
    code, out = run(tmp_path, ds)
    rep = report_of(out)
    assert code == ExitCode.PARTIAL and rep["2_acceptance_decision"]["status"] == "PARTIAL"
    prods = {p["product_name"]: p for p in rep["7_product_details"]}
    assert prods[bad.name]["parsing_status"] == "FAILED" and prods[bad.name]["publication_status"] == "EXCLUDED"
    assert prods[bad.name]["exclusion_reason"] and prods[bad.name]["rows_not_processed"] == 1
    good = [p for n, p in prods.items() if n != bad.name]
    assert good and all(p["parsing_status"] == "PARSED" and p["publication_status"] == "PUBLISHED" for p in good)
    assert len(records(out)) == 2  # both good-product Sol-230 records survive
    assert any(f["scope"] == "PRODUCT" and f["isolation_status"] == "ISOLATED" for f in rep["14_failure_events"])
    assert rep["16_excluded_data"] and rep["17_quarantined_data"][0]["rows"] == 1


def test_product_without_label_is_isolated_not_silently_used(tmp_path):
    ds = clean_dataset(tmp_path / "d")
    ds["files"][1].with_suffix(".xml").unlink()
    code, out = run(tmp_path, ds)
    rep = report_of(out)
    bad = [p for p in rep["7_product_details"] if p["parsing_status"] == "FAILED"]
    assert code == ExitCode.PARTIAL and len(bad) == 1 and "label" in bad[0]["exclusion_reason"].lower()
    assert all(m["source_level"] == "calibrated" for r in records(out) for m in r["measurements"].values())


def test_all_products_failing_is_fail_not_partial(tmp_path):
    ds = clean_dataset(tmp_path / "d")
    for f in ds["files"]:
        f.with_suffix(".xml").unlink()
    code, out = run(tmp_path, ds)
    assert report_of(out)["2_acceptance_decision"]["status"] == "FAIL" and not out.exists()
    assert code in (ExitCode.ACCEPTANCE_FAILURE, ExitCode.OUTPUT_FAILURE, ExitCode.NORMALIZATION_FAILURE,
                    ExitCode.PARSING_FAILURE, ExitCode.UNEXPECTED, ExitCode.INVALID_INPUT)


def test_single_channel_failure_isolates_only_that_channel(tmp_path):
    ds = clean_dataset(tmp_path / "d")
    shutil.rmtree(ds["cal"])
    write_product(ds["cal"] / "sol_0230", 230, "PS", "calibrated", [
        mkrow("PS", "calibrated", 1000, BAROCAP1_PRESSURE=671.69, BAROCAP2_PRESSURE=662.2, THERMOCAP1_TEMP=283.8)],
        label_kwargs={"unit_overrides": {"BAROCAP1_PRESSURE": "kelvin"}})  # label contradicts the mapping
    code, out = run(tmp_path, ds)
    rep = report_of(out)
    rec = next(r for r in records(out) if r["sclk_s"] == "1000")
    cn = {s.raw_name: s.canonical for s in normalize.CHANNEL_SPECS[("PS", "calibrated")]}
    assert cn["BAROCAP1_PRESSURE"] not in rec["measurements"]  # isolated ...
    assert rec["quarantined_channels"][cn["BAROCAP1_PRESSURE"]]["raw_value"] == 671.69  # ... raw value preserved
    assert {cn["BAROCAP2_PRESSURE"], cn["THERMOCAP1_TEMP"]} <= set(rec["measurements"])  # others untouched
    ch = next(c for c in rep["8_scientific_validation"]["channels"] if c["source_channel_name"] == "BAROCAP1_PRESSURE")
    assert ch["final_channel_status"] == "INVALID" and ch["unit_compatibility_status"] == "INVALID"
    assert ch["quarantined_cells"] == 1 and "NO" in ch["usable_in_analysis"]
    assert code == ExitCode.PARTIAL
    assert rep["19_test_results"]["checks"][-1]["name"] == "atomic_publication"
    assert any(c["name"] == "channel_isolation_enforced" and c["result"] == "PASS" for c in rep["19_test_results"]["checks"])


def test_single_bad_row_is_rejected_alone(tmp_path):
    ds = clean_dataset(tmp_path / "d")
    bad = ds["files"][1]
    bad.write_text(bad.read_text() + "1002,L\r\n")  # truncated row -> malformed
    bad.with_suffix(".xml").write_text(T.label_xml("PS", "derived", T.H[("PS", "derived")], 3))
    code, out = run(tmp_path, ds)
    rep = report_of(out)
    p = next(p for p in rep["7_product_details"] if p["product_name"] == bad.name)
    assert code == ExitCode.PARTIAL and p["rows_rejected"] == 1 and p["parsing_status"] == "PARTIALLY_PARSED"
    assert p["rows_encountered"] == p["rows_parsed"] + p["rows_rejected"] == 3
    assert len(records(out)) == 2


def test_non_isolatable_failure_mid_pipeline_is_fail_and_keeps_partial_results(tmp_path, monkeypatch):
    ds = clean_dataset(tmp_path / "d")
    from normalization_service import cli
    real = cli.stream_normalize

    def boom(rows, *a, **k):
        for i, _ in enumerate(rows):
            if i == 2:
                raise streaming.OutputValidationError("injected failure")
        return real(iter(()), *a, **k)

    monkeypatch.setattr(cli, "stream_normalize", boom)
    out = tmp_path / "o.jsonl"
    code = main(["--calibrated", str(ds["cal"]), "--derived", str(ds["der"]), "--output", str(out), "--log-level", "ERROR"])
    assert code == ExitCode.VALIDATION_FAILURE and not out.exists()
    rep = report_of(out)
    assert rep["2_acceptance_decision"]["status"] == "FAIL"
    assert rep["1_run_summary"]["exception"]["type"] == "OutputValidationError"
    assert rep["18_partial_execution_results"]["current_stage"] == "parsing_normalization_staging"
    assert rep["18_partial_execution_results"]["last_successful_stage"] == "product_preflight_and_label_verification"
    assert rep["5_archive_inspection"] and rep["7_product_details"] and rep["8_scientific_validation"]["channels"]
    assert any(p["parsing_status"] == "FAILED" for p in rep["7_product_details"])  # the in-flight product


# ---------------------------------------------------------------- Publication
def test_old_output_untouched_on_exception_and_no_temp_leftovers(tmp_path, monkeypatch):
    ds = clean_dataset(tmp_path / "d")
    out = tmp_path / "out" / "o.jsonl"
    out.parent.mkdir()
    out.write_text("OLD")
    from normalization_service import cli

    def boom(*a, **k):
        raise RuntimeError("disk exploded")

    monkeypatch.setattr(cli, "stream_normalize", boom)
    code = main(["--calibrated", str(ds["cal"]), "--output", str(out), "--overwrite", "--log-level", "ERROR"])
    assert code == ExitCode.UNEXPECTED and out.read_text() == "OLD"
    names = sorted(p.name for p in out.parent.iterdir())
    assert names[0] == "o.jsonl" and all(n.startswith("o.jsonl.report.FAILED.") for n in names[1:]) and len(names) == 2


def test_staging_not_published_before_mandatory_validation(tmp_path, monkeypatch):
    ds = clean_dataset(tmp_path / "d")
    out = tmp_path / "o.jsonl"
    seen = {}
    real_check = context.ExecutionContext.check

    def spy(self, name, passed, detail, mandatory=True, **ev):
        if name == "record_accounting":
            seen["output_exists_at_validation"] = out.exists()
            seen["staged"] = [p.name for p in tmp_path.iterdir() if p.name.startswith(".")]
        return real_check(self, name, passed, detail, mandatory, **ev)

    monkeypatch.setattr(context.ExecutionContext, "check", spy)
    assert main(["--calibrated", str(ds["cal"]), "--derived", str(ds["der"]), "--output", str(out),
                 "--log-level", "ERROR"]) == 0
    assert seen["output_exists_at_validation"] is False and seen["staged"]  # staged file existed, output did not
    assert out.exists()


def test_atomic_replace_swaps_whole_file(tmp_path):
    staged, final = tmp_path / "s", tmp_path / "f"
    staged.write_text("NEW")
    final.write_text("OLD")
    streaming.publish_staged(staged, final)
    assert final.read_text() == "NEW" and not staged.exists()
    streaming.discard_staged(tmp_path / "missing")  # idempotent


def test_failed_mandatory_check_blocks_publication(tmp_path, monkeypatch):
    ds = clean_dataset(tmp_path / "d")
    out = tmp_path / "o.jsonl"
    out.write_text("OLD")
    real = streaming._provenance_of

    def incomplete(*a, **k):  # same totals, but nothing counts as complete -> mandatory check must fail
        t, c, i, m, u, lk = real(*a, **k)
        return t, 0, t, m, u, lk

    monkeypatch.setattr(streaming, "_provenance_of", incomplete)
    code = main(["--calibrated", str(ds["cal"]), "--output", str(out), "--overwrite", "--log-level", "ERROR"])
    rep = report_of(out)
    assert code == ExitCode.ACCEPTANCE_FAILURE and out.read_text() == "OLD"
    assert any(c["name"] == "provenance_complete" and c["result"] == "FAIL" for c in rep["19_test_results"]["checks"])
    assert rep["3_publication_decision"]["dataset"]["status"] == "NOT_PUBLISHED"


@pytest.mark.parametrize("role", ["calibrated", "derived"])
def test_output_and_report_cannot_overwrite_input_archive(tmp_path, role):
    ds = clean_dataset(tmp_path / "d")
    z = tmp_path / "in.zip"
    T.zip_dir(ds["cal"], z, ds["cal"].parent)
    before = z.read_bytes()
    assert main([f"--{role}", str(z), "--output", str(z), "--overwrite", "--log-level", "ERROR"]) == ExitCode.INVALID_INPUT
    assert main([f"--{role}", str(z), "--output", str(tmp_path / "x.jsonl"), "--report", str(z), "--log-level", "ERROR"]) == ExitCode.INVALID_INPUT
    assert main([f"--{role}", str(z), "--output", str(tmp_path / "same"), "--report", str(tmp_path / "same"),
                 "--log-level", "ERROR"]) == ExitCode.INVALID_INPUT
    assert z.read_bytes() == before


def test_check_paths_resolves_symlinks_and_rejects_output_inside_input_dir(tmp_path):
    f = tmp_path / "a.zip"
    f.write_text("x")
    link = tmp_path / "link.zip"

    try:
        link.symlink_to(f)
    except OSError as exc:
        pytest.skip(f"Symlink is unavailable on this Windows environment: {exc}")
    with pytest.raises(archive.PathSafetyError):
        archive.check_paths(output=link, calibrated=f)
    d = tmp_path / "dir"
    d.mkdir()
    with pytest.raises(archive.PathSafetyError):
        archive.check_paths(output=d / "o.jsonl", derived=d)


def test_report_write_failure_is_not_hidden_and_dataset_state_is_separate(tmp_path, monkeypatch):
    ds = clean_dataset(tmp_path / "d")
    out = tmp_path / "o.jsonl"
    from normalization_service import cli

    def broken(path, report):
        raise OSError("report disk full")

    monkeypatch.setattr(cli, "write_report_atomic", broken)
    code = main(["--calibrated", str(ds["cal"]), "--derived", str(ds["der"]), "--output", str(out), "--log-level", "ERROR"])
    assert code == ExitCode.OUTPUT_FAILURE  # not reported as success
    assert out.exists() and streaming.validate_jsonl(out) > 0  # the safely published dataset is NOT lost


# ---------------------------------------------------------------- Scientific rules
def test_unknown_unit_is_not_verified(tmp_path):
    ds = clean_dataset(tmp_path / "d")
    shutil.rmtree(ds["cal"])
    write_product(ds["cal"] / "sol_0230", 230, "PS", "calibrated", [mkrow("PS", "calibrated", 1000, BAROCAP1_PRESSURE=1.0)],
                  label_kwargs={"unit_overrides": {"BAROCAP1_PRESSURE": "furlong/fortnight"}})
    code, out = run(tmp_path, ds)
    ch = next(c for c in report_of(out)["8_scientific_validation"]["channels"] if c["source_channel_name"] == "BAROCAP1_PRESSURE")
    assert ch["final_channel_status"] != "SCIENTIFICALLY_VERIFIED" and ch["final_channel_status"] == "INVALID"


def test_missing_unit_in_label_for_a_mapped_unit_is_not_verified(tmp_path):
    ds = clean_dataset(tmp_path / "d")
    shutil.rmtree(ds["cal"])
    write_product(ds["cal"] / "sol_0230", 230, "PS", "calibrated", [mkrow("PS", "calibrated", 1000, BAROCAP1_PRESSURE=1.0)],
                  label_kwargs={"unit_overrides": {"BAROCAP1_PRESSURE": ""}})
    _, out = run(tmp_path, ds)
    ch = next(c for c in report_of(out)["8_scientific_validation"]["channels"] if c["source_channel_name"] == "BAROCAP1_PRESSURE")
    assert ch["final_channel_status"] == "INVALID" and ch["unit_compatibility_status"] == "INVALID"


def test_unknown_scientific_definition_is_not_verified(tmp_path):
    class F:  # a label field with no description
        position, name, data_type, unit, description = 4, "BAROCAP1_PRESSURE", "ASCII_Real", "pascal", ""

    class L:
        path = Path("x.xml")

        def fields_for_columns(self, cols):
            return {"BAROCAP1_PRESSURE": F}

    spec = [s for s in normalize.CHANNEL_SPECS[("PS", "calibrated")] if s.raw_name == "BAROCAP1_PRESSURE"]
    res = registry.verify_channels("PS", "calibrated", ["SCLK", "BAROCAP1_PRESSURE"], L(), spec, label_ref="x.xml")[0]
    assert res["scientific_definition_status"] == "UNVERIFIED" and res["static_status"] == "NUMERICALLY_VALID"


def test_relative_humidity_unit_not_adopted_and_flagged(tmp_path, dataset):
    code, out = run(tmp_path, dataset)
    rh = next(m for r in records(out) for k, m in r["measurements"].items() if k == "relative_humidity_pct")
    assert rh["unit"] is None and rh["unit_verified"] is False and rh["value"] == 12.5
    ch = next(c for c in report_of(out)["8_scientific_validation"]["channels"] if c["canonical_channel_name"] == "relative_humidity_pct")
    assert ch["final_channel_status"] == "UNVERIFIED" and ch["unit_compatibility_status"] == "UNVERIFIED"


def test_rover_velocity_keeps_meter_per_hour_no_conversion():
    spec = next(s for s in normalize.CHANNEL_SPECS[("ANC", "derived")] if s.raw_name == "ROVER_VELOCITY")
    assert spec.label_unit == "meter/hour" and spec.unit == "m/h" and spec.unit_basis == "label"
    assert "converted" in spec.note and "NOT" in spec.note
    rec = normalize.normalize_group([T.mkparsed("ANC", "derived", "5", {"ROVER_VELOCITY": 128.06})])
    assert rec["measurements"][spec.canonical]["value"] == 128.06


def test_solar_longitude_angle_is_not_areocentric_ls(tmp_path, dataset):
    _, out = run(tmp_path, dataset)
    ch = next(c for c in report_of(out)["8_scientific_validation"]["channels"] if c["source_channel_name"] == "SOLAR_LONGITUDE_ANGLE")
    assert "NOT areocentric" in " ".join(ch["notes"]) and "ls" not in ch["canonical_channel_name"].split("_")


def test_unsupported_transformation_is_not_applied():
    for specs in normalize.CHANNEL_SPECS.values():
        for s in specs:
            assert s.unit_basis in ("label", "not_stated", "canonical_symbol") or s.unit_basis  # table has no scale/offset fields
    assert not any(hasattr(s, "scale") or hasattr(s, "offset") for specs in normalize.CHANNEL_SPECS.values() for s in specs)
    rec = normalize.normalize_group([T.mkparsed("PS", "derived", "5", {"PRESSURE": 662.25})])
    assert rec["measurements"]["pressure_pa"]["value"] == 662.25  # identity


def test_unverified_sentinel_rule_keeps_value_and_flags_it():
    row = T.mkparsed("WS", "derived", "5", {"HORIZONTAL_WIND_SPEED": 999999999})
    st = normalize.NormalizationStats()
    m = normalize.normalize_group([row], st)["measurements"]["horizontal_wind_speed_m_s"]
    assert m["value"] == 999999999 and m["raw_value"] == 999999999 and m["validity"] == "unverified_sentinel_candidate"
    assert "UNVERIFIED" in m["validity_reason"] and st.sentinel_hits == {"WS.derived.HORIZONTAL_WIND_SPEED=999999999|UNVERIFIED": 1}


def test_undocumented_global_sentinel_is_not_applied_to_other_channels():
    st = normalize.NormalizationStats()
    rec = normalize.normalize_group([T.mkparsed("PS", "derived", "5", {"PRESSURE": 999999999})], st)
    m = rec["measurements"]["pressure_pa"]
    assert m["validity"] == "valid" and m["value"] == 999999999 and not st.sentinel_hits


def test_verified_rule_applies_only_to_its_scope(tmp_path):
    reg = registry.SentinelRegistry([registry.SentinelRule("WS", "derived", "WIND_DIRECTION", 999999999,
                                                           registry.VERIFIED, "SIS excerpt §x")])
    rec = normalize.normalize_group([T.mkparsed("WS", "derived", "5", {
        "WIND_DIRECTION": 999999999, "HORIZONTAL_WIND_SPEED": 999999999})], sentinel_registry=reg)
    a, b = rec["measurements"]["wind_direction_deg"], rec["measurements"]["horizontal_wind_speed_m_s"]
    assert a["validity"] == "invalid_sentinel" and a["value"] is None and a["raw_value"] == 999999999
    assert b["validity"] == "valid"  # sibling channel with the same number is NOT touched


def test_rejected_rule_is_ordinary_value():
    reg = registry.SentinelRegistry([registry.SentinelRule("WS", "derived", "WIND_DIRECTION", 999999999,
                                                           registry.REJECTED, "disproved")])
    m = normalize.normalize_group([T.mkparsed("WS", "derived", "5", {"WIND_DIRECTION": 999999999})],
                                  sentinel_registry=reg)["measurements"]["wind_direction_deg"]
    assert m["validity"] == "valid"


def test_registry_file_can_promote_rule_and_report_lists_it(tmp_path, dataset):
    f = tmp_path / "reg.json"
    f.write_text(json.dumps({"rules": [{"product": "WS", "level": "derived", "channel": c, "value": 999999999,
                                        "status": "VERIFIED", "evidence": "SIS test excerpt"}
                                       for c in ("HORIZONTAL_WIND_SPEED", "WIND_DIRECTION")]}))
    code, out = run(tmp_path, dataset, "--sentinel-registry", str(f))
    rep = report_of(out)
    assert {r["verification_status"] for r in rep["9_sentinel_registry_and_applied_rules"]["registry"]} == {"VERIFIED"}
    assert rep["9_sentinel_registry_and_applied_rules"]["applied"]["verified_applied"]
    m = next(r for r in records(out) if r["sclk_s"] == "1002")["measurements"]["horizontal_wind_speed_m_s"]
    assert m["validity"] == "invalid_sentinel" and m["value"] is None and m["raw_value"] == 999999999


def test_flag_definitions_are_checked_against_label_text_not_values(tmp_path):
    class F:
        position, name, data_type, unit = 1, "TRANSDUCER", "ASCII_Integer", None
        description = "something else entirely"

    class L:
        path = Path("x.xml")

        def fields_for_columns(self, cols):
            return {"TRANSDUCER": F}

    out = registry.verify_flag_definitions(L(), ["TRANSDUCER"], {"TRANSDUCER": "Transducer in use"})
    assert out[0]["definition_status"] == "UNVERIFIED"


# ---------------------------------------------------------------- SCLK
@pytest.mark.parametrize("raw,canon", [("1000", "1000"), ("1000.5", "1000.5"), ("1000.50", "1000.5"),
                                        ("123456789012.123456789012345", "123456789012.123456789012345")])
def test_sclk_canonical_exact(raw, canon):
    assert parser.canonical_sclk(raw) == canon and isinstance(parser.canonical_sclk(raw), str)


def test_sclk_sqlite_and_jsonl_round_trip_high_precision(tmp_path):
    big = "739318123.1234567890123456789"
    rows = [T.mkparsed("PS", "derived", s, {"PRESSURE": 1.0}) for s in ("1000", "1000.5", big)]
    out = tmp_path / "o.jsonl"
    summ = streaming.stream_normalize(iter(rows), out, 2, workdir=tmp_path)
    assert [json.loads(l)["sclk_s"] for l in out.read_text().splitlines()] == ["1000", "1000.5", parser.canonical_sclk(big)]
    sc = summ.sclk
    assert sc["sqlite_round_trip"]["result"] == "PASSED" and sc["sqlite_round_trip"]["reconstruction_mismatches"] == 0
    assert sc["total_sclk_values"] == 3 and sc["fractional_sclk_count"] == 2 and sc["integer_sclk_count"] == 1
    assert sc["precision_distribution_fraction_digits"] == {"0": 1, "1": 1, "19": 1}
    assert summ.jsonl["jsonl_round_trip"] == "PASSED" and summ.published


@pytest.mark.parametrize("bad", ["abc", "1e5", "-3", "", "1..2", "nan"])
def test_invalid_sclk_rejected_and_counted(tmp_path, bad):
    ds = clean_dataset(tmp_path / "d")
    f = ds["files"][1]
    add_row(f, "PS", "derived", {"PRESSURE": 1.0}, bad)
    code, out = run(tmp_path, ds)
    rep = report_of(out)
    s = rep["11_sclk_validation"]
    assert code == ExitCode.PARTIAL and (s["invalid_sclk_values"] + s["missing_sclk_values"]) == 1
    assert rep["13_record_accounting"]["unexplained_differences"] == []


def test_duplicate_identity_and_grouping_conflict_are_counted(tmp_path):
    ds = clean_dataset(tmp_path / "d")
    f = ds["files"][1]
    add_row(f, "PS", "derived", {"PRESSURE": 662.25, "LMST": "00230M00:00:09.000"}, 1000)  # same SCLK, other LMST
    code, out = run(tmp_path, ds)
    s = report_of(out)["11_sclk_validation"]
    assert s["duplicate_identity_count"] >= 1 and s["grouping_conflicts"] >= 1
    assert s["unresolved_temporal_records"] > 0 and "no absolute time is invented" in s["unresolved_temporal_reason"]
    assert all(r["utc"] is None for r in records(out))


# ---------------------------------------------------------------- Archive
def _zip(tmp_path, members, name="a.zip"):
    z = tmp_path / name
    with zipfile.ZipFile(z, "w") as zf:
        for n, data in members:
            zf.writestr(n, data)
    return z


def test_archive_inspection_orphans_and_missing_pairs(tmp_path):
    z = _zip(tmp_path, [("sol_0230/WE__0230___________CAL_PS__________________P02.CSV", "SCLK\n1\n"),
                        ("sol_0230/WE__0230___________CAL_PS__________________P02.xml", "<x/>"),
                        ("sol_0230/WE__0230___________CAL_ATS_________________P02.CSV", "SCLK\n1\n"),
                        ("sol_0231/WE__0231___________CAL_RDS_________________P02.xml", "<x/>"),
                        ("sol_0231/WE__0231___________CAL_PS__________________P02.CSV", "SCLK\n1\n"),
                        ("sol_0231/WE__0231___________CAL_PS__________________P02.xml", "<x/>")])
    i = archive.inspect_archive(z)
    assert i["total_entries"] == 6 and i["csv_entries"] == 3 and i["xml_entries"] == 3 and i["crc_validation"] == "PASSED"
    assert len(i["csv_without_label"]) == 1 and len(i["orphan_xml_labels"]) == 1
    assert {(m["sol"], m["product"]) for m in i["expected_files_missing"]} >= {(231, "ATS")}
    assert i["ok"]


def test_archive_corrupt_zip_reports_without_raising(tmp_path):
    z = tmp_path / "a.zip"
    z.write_bytes(b"PK\x03\x04garbage" * 5)
    i = archive.inspect_archive(z)
    assert i["ok"] is False and i["discovery_errors"] and i["crc_validation"] == "NOT_EXECUTED"


def test_archive_unreadable_member_and_crc_failure_detected(tmp_path):
    z = _zip(tmp_path, [("a.csv", b"A" * 5000), ("b.csv", "ok")])
    raw = bytearray(z.read_bytes())
    raw[40] ^= 0xFF  # inside the first member's data
    z.write_bytes(bytes(raw))
    i = archive.inspect_archive(z)
    assert i["crc_validation"] == "FAILED" and (i["corrupt_entries"] or i["unreadable_entries"]) and not i["ok"]


def test_archive_duplicate_and_unsafe_paths(tmp_path):
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        z = _zip(tmp_path, [("a.csv", "1"), ("a.csv", "2"), ("../evil.csv", "x"), ("/abs.csv", "x"), ("c:\\w.csv", "x")])
    i = archive.inspect_archive(z)
    assert i["duplicate_paths"] == ["a.csv"] and len(i["unsafe_paths"]) == 3 and not i["ok"]
    with pytest.raises(parser.ArchiveError):
        parser.safe_extract_zip(z, tmp_path / "x")
    assert not (tmp_path / "evil.csv").exists()


def test_cli_reports_inspection_when_extraction_would_fail(tmp_path):
    z = _zip(tmp_path, [("../evil.csv", "x")])
    code = main(["--calibrated", str(z), "--output", str(tmp_path / "o.jsonl"), "--log-level", "ERROR"])
    rep = report_of(tmp_path / "o.jsonl")
    assert code == ExitCode.INVALID_INPUT and rep["5_archive_inspection"][0]["unsafe_paths"]
    assert any(f["error_code"] == "ARCHIVE_UNSAFE_PATH" for f in rep["14_failure_events"])


def test_missing_xml_and_missing_csv_and_orphan_xml_in_a_real_run(tmp_path):
    ds = clean_dataset(tmp_path / "d")
    ds["files"][0].with_suffix(".xml").unlink()  # CSV without label
    orphan = ds["cal"] / "sol_0230" / "WE__0230___________CAL_ATS_________________P02.xml"
    orphan.write_text("<x/>")  # label without CSV
    code, out = run(tmp_path, ds)
    rep = report_of(out)
    codes = {w["code"] for w in rep["15_warnings"]}
    assert {"CSV_WITHOUT_LABEL", "ORPHAN_XML_LABEL"} <= codes and code == ExitCode.PARTIAL


# ---------------------------------------------------------------- Reporting
def test_report_has_every_required_section_and_strict_json(tmp_path, dataset):
    code, out = run(tmp_path, dataset)
    text = Path(str(out) + ".report.json").read_text()
    rep = json.loads(text, parse_constant=lambda c: (_ for _ in ()).throw(AssertionError(c)))  # no NaN/Infinity
    prefixes = [f"{i}_" for i in range(1, 21)]
    assert all(any(k.startswith(p) for k in rep) for p in prefixes) and len(rep) == 21  # +schema version
    for f in rep["14_failure_events"]:
        assert {"severity", "scope", "stage", "entity_id", "error_code", "description", "evidence", "affected_count",
                "isolation_status", "recovery_recommendation"} <= set(f)


def test_product_details_reflect_real_execution(tmp_path, dataset):
    code, out = run(tmp_path, dataset)
    rep = report_of(out)
    prods = rep["7_product_details"]
    assert len(prods) == rep["6_product_discovery"]["products_discovered"] == 11
    assert not any(p["parsing_status"] == "PENDING" for p in prods)
    for p in prods:
        assert p["parsing_status"] == "PARSED" and p["normalization_status"] == "NORMALIZED"
        assert p["validation_status"] == "VALIDATED" and p["publication_status"] == "PUBLISHED"
        assert p["rows_encountered"] == p["rows_parsed"] + p["rows_rejected"] == p["rows_accepted"]
        assert p["source_archive"] and p["xml_label"] and p["measurements_generated"] >= 0
    assert sum(p["rows_accepted"] for p in prods) == dataset["rows"]


def test_counters_are_consistent_and_verifiable(tmp_path, dataset):
    code, out = run(tmp_path, dataset)
    rep = report_of(out)
    acc = rep["13_record_accounting"]
    d = acc["dataset"]
    assert acc["unexplained_differences"] == [] and all(i["holds"] for i in acc["identities"])
    assert d["rows_encountered"] == d["rows_parsed"] + d["rows_rejected"]
    assert d["measurements_generated"] == sum(len(r["measurements"]) for r in records(out))
    assert d["records_written"] == len(records(out))
    prov = rep["12_provenance_validation"]
    assert prov["total_measurements"] == prov["measurements_with_complete_provenance"] == d["measurements_generated"]
    assert prov["missing_provenance_count"] == 0 and prov["unresolved_source_references"] == 0
    assert all(any(s.get("source_archive") for s in r["sources"]) for r in records(out))


def test_unexplained_row_loss_is_a_validation_failure(tmp_path, monkeypatch):
    ds = clean_dataset(tmp_path / "d")
    from normalization_service import cli
    real = cli.stream_normalize

    def lossy(rows, *a, **k):  # silently drops one row between parser and staging
        return real((r for i, r in enumerate(rows) if i != 1), *a, **k)

    monkeypatch.setattr(cli, "stream_normalize", lossy)
    out = tmp_path / "o.jsonl"
    code = main(["--calibrated", str(ds["cal"]), "--derived", str(ds["der"]), "--output", str(out), "--log-level", "ERROR"])
    rep = report_of(out)
    assert code == ExitCode.ACCEPTANCE_FAILURE and not out.exists()
    assert rep["13_record_accounting"]["unexplained_differences"]
    assert rep["2_acceptance_decision"]["status"] == "FAIL"


def test_no_hardcoded_success_claims_in_source():
    src = "".join(
    (T.PKG_DIR / n).read_text(encoding="utf-8")
    for n in ("cli.py", "report.py", "context.py", "streaming.py")
    )
    for forbidden in ('"PENDING"', "exact_string_preservation = True", "exact_string_preservation\": True",
                      'sqlite_round_trip = "PASSED"', "missing_provenance_count = None", "corrupt_or_unreadable_entries = []"):
        assert forbidden not in src, forbidden


def test_check_not_executed_is_never_reported_as_pass():
    ctx = context.ExecutionContext()
    ctx.check("x", None, "could not run")
    assert ctx.checks[0]["result"] == "NOT_EXECUTED"
    ctx.products["p"] = context.ProductRecord("p", "p.csv", "a", "PS", "derived", 1, "p.csv", None, parsing_status="PARSED")
    assert context.compute_acceptance(ctx)["status"] == "FAIL"


def test_acceptance_is_computed_from_facts():
    ctx = context.ExecutionContext()
    ctx.products["p"] = context.ProductRecord("p", "p.csv", "a", "PS", "derived", 1, "p.csv", None, parsing_status="PARSED")
    ctx.check("ok", True, "d")
    assert context.compute_acceptance(ctx)["status"] == "PASS"
    ctx.fail("ERROR", "CHANNEL", "s", "c", "X", "isolated", isolation_status="ISOLATED")
    assert context.compute_acceptance(ctx)["status"] == "PARTIAL"
    ctx.fail("CRITICAL", "DATASET", "s", "d", "Y", "boom", isolation_status="NOT_ISOLATABLE")
    assert context.compute_acceptance(ctx)["status"] == "FAIL"
    ctx.strict = True
    assert context.jsonable({"a": float("nan"), "b": float("inf"), "c": {1, 2}}) == {"a": None, "b": None, "c": [1, 2]}
