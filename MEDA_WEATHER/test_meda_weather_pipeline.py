"""Quality gate for the MEDA weather pipeline.

Run with ``pytest`` from anywhere; no local dataset is needed - a small
synthetic dataset is built in a temporary directory from the real MEDA column
names/units (audited against the PDS4 labels of the Sol 230-239 archives).

Optional real-label audit (skipped unless configured)::

    MEDA_LABEL_ROOTS=/path/calibrated_dir:/path/derived_dir pytest -k real_labels
"""
from __future__ import annotations

import ast
import csv
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import tracemalloc
import zipfile
from decimal import Decimal
from pathlib import Path

import pytest

try:
    import meda_weather  # noqa: F401
except ImportError:  # run as a loose file: make the package importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from meda_weather import cli, normalize, parser, streaming  # noqa: E402
from meda_weather.cli import ExitCode  # noqa: E402

PKG_DIR = Path(parser.__file__).resolve().parent
PKG_PARENT = PKG_DIR.parent

# =========================================================================
# Synthetic dataset built from audited MEDA headers / label units
# =========================================================================

H = {
    ("ATS", "calibrated"): "SCLK,LMST,LTST,ATS_LOCAL_TEMP1,ATS_LOCAL_TEMP2,ATS_LOCAL_TEMP3,ATS_LOCAL_TEMP4,ATS_LOCAL_TEMP5",
    ("ENG", "calibrated"): "SCLK,LMST,LTST,ICU_Analog_TH_HK_TM,ICU_Analog_H_PRT_TEMP_1,ICU_Analog_TIRS_ACQ_TEMP_TM,ICU_Analog_5V_CURRENT_TM,ICU_Analog_8V_CURRENT_TM,ICU_Analog_11VN_CURRENT_TM,ICU_Analog_3V3_HK_MUX,ICU_Analog_POWER_H_SENSE,ICU_Analog_POWER_P1,ICU_Analog_R1_CAL_PRT,ICU_Analog_POWER_P2,ICU_Analog_R2_CAL_PRT,ICU_Analog_8V_FRANGI_TM,ICU_Analog_MEDA_RDS_TEMP,ICU_Analog_B1_SEC_PWR_TLM,ICU_Analog_CAM_PCB_PRT,ICU_Analog_B2_SEC_PWR_TLM,ICU_Analog_CAM_CCD_PRT,ICU_Analog_8V_H_PRT_TM,ICU_Analog_H_PRT_TEMP_2,ICU_Analog_12V_HTR_CURRENT_TM,ICU_Analog_WS1_PRT,ICU_Analog_WS2_PRT",
    ("PS", "calibrated"): "SCLK,LMST,LTST,BAROCAP1_PRESSURE,BAROCAP1_PRESSURE_UNCERTAINTY,BAROCAP2_PRESSURE,BAROCAP2_PRESSURE_UNCERTAINTY,BAROCAP3_PRESSURE,BAROCAP3_PRESSURE_UNCERTAINTY,THERMOCAP1_TEMP,THERMOCAP1_TEMP_UNCERTAINTY,THERMOCAP2_TEMP,THERMOCAP2_TEMP_UNCERTAINTY,PRESSURE_MEASUREMENT_MODE,TRANSDUCER",
    ("RDS", "calibrated"): "SCLK,LMST,LTST," + ",".join(
        [f"RDS_LAT_{i}" for i in range(1, 9)] + [f"RDS_TOP_{i}" for i in range(1, 9)]
        + [f"RDS_LAT_{i}_HG" for i in range(1, 9)] + [f"RDS_TOP_{i}_HG" for i in range(1, 9)]
        + ["ROVER_HGA_STILL", "ROVER_STILL"]),
    ("TIRS", "calibrated"): "SCLK,LMST,LTST,DOWNWARD_LW_IRRADIANCE,DOWNWARD_LW_IRRADIANCE_UNCERTAINTY,AIR_TEMP,AIR_TEMP_UNCERTAINTY,UPWARD_SW_IRRADIANCE,UPWARD_SW_IRRADIANCE_UNCERTAINTY,UPWARD_LW_IRRADIANCE,UPWARD_LW_IRRADIANCE_UNCERTAINTY,GROUND_TEMP,GROUND_TEMP_UNCERTAINTY,RSM_HEAD_OUTSIDE_TIRS_UPWARD_LOOKING_FOV,WHEEL_OUTSIDE_TIRS_DOWNWARD_LOOKING_FOV,SUN_OUTSIDE_TIRS_FOV,ROVER_LOW_TILT,TIRS_GROUND_FOOTPRINT_NOT_IN_SHADOW,ROVER_HGA_OFF,SKYCAM_OFF,ROVER_STILL",
    ("WS", "calibrated"): "SCLK,LMST,LTST,BOOM1_HORIZONTAL_WIND_SPEED,BOOM1_HORIZONTAL_WIND_SPEED_UNCERTAINTY,BOOM1_VERTICAL_WIND_SPEED,BOOM1_VERTICAL_WIND_SPEED_UNCERTAINTY,BOOM1_WIND_DIRECTION,BOOM1_WIND_DIRECTION_UNCERTAINTY,BOOM2_HORIZONTAL_WIND_SPEED,BOOM2_HORIZONTAL_WIND_SPEED_UNCERTAINTY,BOOM2_VERTICAL_WIND_SPEED,BOOM2_VERTICAL_WIND_SPEED_UNCERTAINTY,BOOM2_WIND_DIRECTION,BOOM2_WIND_DIRECTION_UNCERTAINTY,BOOM1_ALG_STATE_STABLE,BOOM2_ALG_STATE_STABLE,ROVER_STILL",
    ("ANC", "derived"): "SCLK,LMST,LTST,SOLAR_LONGITUDE_ANGLE,SOLAR_ZENITHAL_ANGLE,ROVER_POSITION_X,ROVER_POSITION_Y,ROVER_POSITION_Z,ROVER_VELOCITY,ROVER_PITCH,ROVER_YAW,ROVER_ROLL,MASTHEAD_AZIMUTH,MASTHEAD_ELEVATION",
    ("PS", "derived"): "SCLK,LMST,LTST,PRESSURE,PRESSURE_UNCERTAINTY,TRANSDUCER",
    ("RHS", "derived"): "SCLK,LMST,LTST,LOCAL_RELATIVE_HUMIDITY,LOCAL_RELATIVE_HUMIDITY_UNCERTAINTY,HUMIDITY_LOCAL_TEMP,HUMIDITY_LOCAL_TEMP_UNCERTAINTY,VOLUME_MIXING_RATIO,VOLUME_MIXING_RATIO_UNCERTAINTY",
    ("RHS", "calibrated"): "SCLK,LMST,LTST,HUMICAP_A_HUMIDITY,HUMICAP_A_HUMIDITY_UNCERTAINTY,HUMICAP_B_HUMIDITY,HUMICAP_B_HUMIDITY_UNCERTAINTY,HUMICAP_TEMP,HUMICAP_TEMP_UNCERTAINTY,THERMOCAP1_TEMP,THERMOCAP1_TEMP_UNCERTAINTY,THERMOCAP2_TEMP,THERMOCAP2_TEMP_UNCERTAINTY",
    ("TIRS", "derived"): "SCLK,LMST,LTST,DOWNWARD_LW_IRRADIANCE,DOWNWARD_LW_IRRADIANCE_UNCERTAINTY,UPWARD_LW_IRRADIANCE,UPWARD_LW_UNCERTAINTY,RSM_HEAD_OUTSIDE_TIRS_UPWARD_LOOKING_FOV,WHEEL_OUTSIDE_TIRS_DOWNWARD_LOOKING_FOV,SUN_OUTSIDE_TIRS_FOV,ROVER_LOW_TILT,TIRS_GROUND_FOOTPRINT_NOT_IN_SHADOW,ROVER_HGA_OFF,SKYCAM_OFF,ROVER_STILL",
    ("WS", "derived"): "SCLK,LMST,LTST,HORIZONTAL_WIND_SPEED,HORIZONTAL_WIND_SPEED_UNCERTAINTY,VERTICAL_WIND_SPEED,VERTICAL_WIND_SPEED_UNCERTAINTY,WIND_DIRECTION,WIND_DIRECTION_UNCERTAINTY,BOTH_BOOMS_USED_FOR_RETRIEVAL,ROVER_STILL",
}
H = {k: v.split(",") for k, v in H.items()}

# Where the real XML label spells a field differently from the CSV header.
LABEL_SPELLING = {
    ("WS", "calibrated"): {"BOOM1_ALG_STATE_STABLE": "BOOM1_ALG_IN_STABLE_STATE",
                           "BOOM2_ALG_STATE_STABLE": "BOOM2_ALG_IN_STABLE_STATE"},
    ("PS", "derived"): {"PRESSURE_UNCERTAINTY": "PRESSURE UNCERTAINTY"},
    ("RHS", "derived"): {"HUMIDITY_LOCAL_TEMP_UNCERTAINTY": "LOCAL_TEMP_UNCERTAINTY",
                         "VOLUME_MIXING_RATIO_UNCERTAINTY": "VOLUME_MIXING_RATION_UNCERTAINTY"},
}

# Independent snapshot of the <unit> values found in the real labels
# (label spelling -> unit; absent key = label states no unit).
_UNITS = {"SCLK": "second"}
for _i in range(1, 6):
    _UNITS[f"ATS_LOCAL_TEMP{_i}"] = "kelvin"
for _i in (1, 2, 3):
    _UNITS[f"BAROCAP{_i}_PRESSURE"] = "pascal"
    _UNITS[f"BAROCAP{_i}_PRESSURE_UNCERTAINTY"] = "pascal"
for _i in (1, 2):
    _UNITS[f"THERMOCAP{_i}_TEMP"] = "kelvin"
    _UNITS[f"THERMOCAP{_i}_TEMP_UNCERTAINTY"] = "kelvin"
_UNITS.update({
    "PRESSURE": "pascal", "PRESSURE UNCERTAINTY": "kelvin",  # label quirk, see normalize.py
    "HUMICAP_TEMP": "kelvin", "HUMICAP_TEMP_UNCERTAINTY": "kelvin",
    "HUMIDITY_LOCAL_TEMP": "kelvin", "LOCAL_TEMP_UNCERTAINTY": "kelvin",
    "VOLUME_MIXING_RATIO": "ppm", "VOLUME_MIXING_RATION_UNCERTAINTY": "ppm",
    "DOWNWARD_LW_IRRADIANCE": "watt/m2", "DOWNWARD_LW_IRRADIANCE_UNCERTAINTY": "watt/m2",
    "UPWARD_SW_IRRADIANCE": "watt/m2", "UPWARD_SW_IRRADIANCE_UNCERTAINTY": "watt/m2",
    "UPWARD_LW_IRRADIANCE": "watt/m2", "UPWARD_LW_IRRADIANCE_UNCERTAINTY": "watt/m2",
    "UPWARD_LW_UNCERTAINTY": "watt/m2",
    "AIR_TEMP": "kelvin", "GROUND_TEMP": "kelvin", "GROUND_TEMP_UNCERTAINTY": "kelvin",
    "HORIZONTAL_WIND_SPEED": "meter/second", "VERTICAL_WIND_SPEED": "meter/second",
    "WIND_DIRECTION": "degree",
    "SOLAR_LONGITUDE_ANGLE": "degree", "SOLAR_ZENITHAL_ANGLE": "degree",
    "ROVER_POSITION_X": "meter", "ROVER_POSITION_Y": "meter", "ROVER_POSITION_Z": "meter",
    "ROVER_VELOCITY": "meter/hour", "ROVER_PITCH": "degree", "ROVER_YAW": "degree",
    "ROVER_ROLL": "degree", "MASTHEAD_AZIMUTH": "degree", "MASTHEAD_ELEVATION": "degree",
})
for _b in (1, 2):
    for _n in ("HORIZONTAL_WIND_SPEED", "VERTICAL_WIND_SPEED"):
        _UNITS[f"BOOM{_b}_{_n}"] = "meter/second"
    _UNITS[f"BOOM{_b}_WIND_DIRECTION"] = "degree"
for _tok in ("TH_HK_TM H_PRT_TEMP_1 TIRS_ACQ_TEMP_TM MEDA_RDS_TEMP CAM_PCB_PRT CAM_CCD_PRT H_PRT_TEMP_2 WS1_PRT WS2_PRT").split():
    _UNITS[f"ICU_Analog_{_tok}"] = "kelvin"
for _tok in "5V_CURRENT_TM 8V_CURRENT_TM 11VN_CURRENT_TM 12V_HTR_CURRENT_TM".split():
    _UNITS[f"ICU_Analog_{_tok}"] = "amp"
for _tok in "3V3_HK_MUX POWER_P1 POWER_P2 8V_FRANGI_TM B1_SEC_PWR_TLM B2_SEC_PWR_TLM 8V_H_PRT_TM".split():
    _UNITS[f"ICU_Analog_{_tok}"] = "volt"
_UNITS["ICU_Analog_R1_CAL_PRT"] = "ohm"
_UNITS["ICU_Analog_R2_CAL_PRT"] = "ohm"
for _side in ("LAT", "TOP"):
    for _n in range(1, 9):
        for _suf in ("", "_HG"):
            _UNITS[f"RDS_{_side}_{_n}{_suf}"] = "amp" if (_side == "LAT" and _n == 1) else "watt/m2"

_STRING_FIELDS = {"LMST", "LTST", "PRESSURE_MEASUREMENT_MODE"}
_INTEGER_FIELDS = {
    "TRANSDUCER", "ROVER_STILL", "ROVER_HGA_STILL", "ROVER_HGA_OFF", "SKYCAM_OFF", "ROVER_LOW_TILT",
    "SUN_OUTSIDE_TIRS_FOV", "BOTH_BOOMS_USED_FOR_RETRIEVAL", "BOOM1_ALG_STATE_STABLE",
    "BOOM2_ALG_STATE_STABLE", "RSM_HEAD_OUTSIDE_TIRS_UPWARD_LOOKING_FOV",
    "WHEEL_OUTSIDE_TIRS_DOWNWARD_LOOKING_FOV", "TIRS_GROUND_FOOTPRINT_NOT_IN_SHADOW",
}
FILE_TOKEN = {"ANC": "ANCILLARY"}


def label_xml(product, level, header, records, *, overrides=None, processing_level=None, unit_overrides=None):
    spelling = dict(LABEL_SPELLING.get((product, level), {}))
    spelling.update(overrides or {})
    units = dict(_UNITS)
    units.update(unit_overrides or {})
    fields = []
    for number, name in enumerate(header, start=1):
        label_name = spelling.get(name, name)
        if name in _STRING_FIELDS:
            dtype = "ASCII_String"
        elif name in _INTEGER_FIELDS:
            dtype = "ASCII_Integer"
        else:
            dtype = "ASCII_Real"
        unit = units.get(label_name)
        unit_xml = f"<unit>{unit}</unit>" if unit else ""
        fields.append(
            f"<Field_Delimited><name>{label_name}</name><field_number>{number}</field_number>"
            f"<data_type>{dtype}</data_type>{unit_xml}<description>d</description></Field_Delimited>"
        )
    plevel = processing_level or level.capitalize()
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Product_Observational xmlns="http://pds.nasa.gov/pds4/pds/v1">'
        f"<Identification_Area><logical_identifier>urn:test:{product}:{level}</logical_identifier>"
        "<version_id>1.0</version_id></Identification_Area>"
        f"<Observation_Area><Primary_Result_Summary><processing_level>{plevel}</processing_level>"
        "</Primary_Result_Summary></Observation_Area>"
        f"<File_Area_Observational><Table_Delimited><records>{records}</records>"
        f"<Record_Delimited><fields>{len(header)}</fields>{''.join(fields)}</Record_Delimited>"
        "</Table_Delimited></File_Area_Observational></Product_Observational>"
    )


def product_filename(sol, product, level, release=2, ext="CSV"):
    prefix = "CAL" if level == "calibrated" else "DER"
    token = FILE_TOKEN.get(product, product)
    return f"WE__{sol:04d}___________{prefix}_{token}__________________P{release:02d}.{ext}"


def mkrow(product, level, sclk, **values):
    """Row (list of str) in header order; unspecified columns are empty cells."""
    header = H[(product, level)]
    values = {k: ("" if v is None else str(v)) for k, v in values.items()}
    row = []
    for name in header:
        if name == "SCLK":
            row.append(str(sclk))
        elif name == "LMST":
            row.append(values.pop("LMST", "00230M00:00:01.000"))
        elif name == "LTST":
            row.append(values.pop("LTST", "0230 00:21:20"))
        else:
            row.append(values.pop(name, ""))
    assert not values, f"unknown columns for {product}.{level}: {sorted(values)}"
    return row


def write_product(directory, sol, product, level, rows, *, release=2, header=None, label=True, label_kwargs=None):
    directory.mkdir(parents=True, exist_ok=True)
    header = header if header is not None else H[(product, level)]
    path = directory / product_filename(sol, product, level, release)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\r\n")
        writer.writerow(header)
        writer.writerows(rows)
    if label:
        xml = label_xml(product, level, header, len(rows), **(label_kwargs or {}))
        path.with_suffix(".xml").write_text(xml, encoding="utf-8")
    return path


def build_dataset(base: Path, sol: int = 230):
    """Calibrated (sol_0230/ layout) + derived (derived/<PROD>/ layout)."""
    cal = base / "MEDA_Weather_ds" / f"sol_{sol:04d}"
    der = base / "derived"
    R = {}
    R[("WS", "calibrated")] = [
        mkrow("WS", "calibrated", 1000, BOOM1_HORIZONTAL_WIND_SPEED=4.5, BOOM1_WIND_DIRECTION=90.0,
              BOOM2_HORIZONTAL_WIND_SPEED=4.7, BOOM2_WIND_DIRECTION=95.5, BOOM1_ALG_STATE_STABLE=1,
              BOOM2_ALG_STATE_STABLE=1, ROVER_STILL=1),
        mkrow("WS", "calibrated", "1000.5", BOOM1_HORIZONTAL_WIND_SPEED=4.4, BOOM1_WIND_DIRECTION=89.0, ROVER_STILL=1),
        mkrow("WS", "calibrated", 1002, BOOM1_HORIZONTAL_WIND_SPEED=0, BOOM1_WIND_DIRECTION=0, ROVER_STILL=0),
    ]
    R[("WS", "derived")] = [
        mkrow("WS", "derived", 1000, HORIZONTAL_WIND_SPEED=4.6, WIND_DIRECTION=91.5,
              BOTH_BOOMS_USED_FOR_RETRIEVAL=1, ROVER_STILL=1),
        mkrow("WS", "derived", 1002, HORIZONTAL_WIND_SPEED=999999999, WIND_DIRECTION=999999999, ROVER_STILL=1),
    ]
    R[("PS", "calibrated")] = [
        mkrow("PS", "calibrated", 1000, BAROCAP1_PRESSURE=671.69, BAROCAP2_PRESSURE=662.20,
              BAROCAP3_PRESSURE=662.26, THERMOCAP1_TEMP=283.84, THERMOCAP2_TEMP=283.90,
              PRESSURE_MEASUREMENT_MODE="nominal", TRANSDUCER=1),
        mkrow("PS", "calibrated", 1001, BAROCAP1_PRESSURE=671.67, BAROCAP2_PRESSURE=662.23,
              BAROCAP3_PRESSURE=662.29, PRESSURE_MEASUREMENT_MODE="nominal", TRANSDUCER=1),
    ]
    R[("PS", "derived")] = [
        mkrow("PS", "derived", 1000, PRESSURE=662.25, TRANSDUCER=1),
        mkrow("PS", "derived", 1001, PRESSURE=662.28, TRANSDUCER=1),
    ]
    R[("RHS", "derived")] = [
        mkrow("RHS", "derived", 1001, LOCAL_RELATIVE_HUMIDITY=12.5, LOCAL_RELATIVE_HUMIDITY_UNCERTAINTY=2.0,
              HUMIDITY_LOCAL_TEMP=215.3, HUMIDITY_LOCAL_TEMP_UNCERTAINTY=1.0,
              VOLUME_MIXING_RATIO=210.0, VOLUME_MIXING_RATIO_UNCERTAINTY=40.0),
        mkrow("RHS", "derived", 1003, LOCAL_RELATIVE_HUMIDITY=11.9, HUMIDITY_LOCAL_TEMP=214.8),
    ]
    R[("TIRS", "calibrated")] = [
        mkrow("TIRS", "calibrated", 1000, DOWNWARD_LW_IRRADIANCE=3.77, DOWNWARD_LW_IRRADIANCE_UNCERTAINTY=0.3,
              AIR_TEMP=210.5, UPWARD_SW_IRRADIANCE=0, GROUND_TEMP=289.05, GROUND_TEMP_UNCERTAINTY=0.8,
              RSM_HEAD_OUTSIDE_TIRS_UPWARD_LOOKING_FOV=1, SUN_OUTSIDE_TIRS_FOV=1, ROVER_LOW_TILT=1,
              TIRS_GROUND_FOOTPRINT_NOT_IN_SHADOW=1, ROVER_STILL=1),
    ]
    R[("TIRS", "derived")] = [
        mkrow("TIRS", "derived", 1000, DOWNWARD_LW_IRRADIANCE=3.9, DOWNWARD_LW_IRRADIANCE_UNCERTAINTY=0.4,
              UPWARD_LW_IRRADIANCE=120.5, RSM_HEAD_OUTSIDE_TIRS_UPWARD_LOOKING_FOV=1, ROVER_STILL=1),
    ]
    R[("ATS", "calibrated")] = [
        mkrow("ATS", "calibrated", 1000, ATS_LOCAL_TEMP1=207.1, ATS_LOCAL_TEMP2=207.2, ATS_LOCAL_TEMP3=207.3,
              ATS_LOCAL_TEMP4=207.4, ATS_LOCAL_TEMP5=207.5),
        mkrow("ATS", "calibrated", 1003, ATS_LOCAL_TEMP1=208.1),
    ]
    R[("ANC", "derived")] = [
        mkrow("ANC", "derived", 1000, SOLAR_LONGITUDE_ANGLE=-7.30, SOLAR_ZENITHAL_ANGLE=137.74,
              ROVER_POSITION_X=-641.83, ROVER_POSITION_Y=492.25, ROVER_POSITION_Z=-3.26,
              ROVER_VELOCITY=128.06, ROVER_PITCH=4.52, ROVER_YAW=27.66, ROVER_ROLL=-5.27),
        mkrow("ANC", "derived", 1001, SOLAR_LONGITUDE_ANGLE=-7.30, SOLAR_ZENITHAL_ANGLE=137.74,
              ROVER_VELOCITY=0.00),
    ]
    R[("RDS", "calibrated")] = [
        mkrow("RDS", "calibrated", 1000, RDS_LAT_1=1.2e-7, RDS_LAT_2=3.5, RDS_TOP_1_HG=2.5, ROVER_STILL=1),
    ]
    R[("ENG", "calibrated")] = [
        mkrow("ENG", "calibrated", 1000, ICU_Analog_TH_HK_TM=283.63, ICU_Analog_5V_CURRENT_TM=0.48,
              ICU_Analog_POWER_P1=0),
    ]
    paths = {}
    for (product, level), rows in R.items():
        folder = cal if level == "calibrated" else der / product
        paths[(product, level)] = write_product(folder, sol, product, level, rows)
    keys = {str(r[0]) for rows in R.values() for r in rows}
    return {
        "cal": cal.parent, "der": der, "rows": sum(len(r) for r in R.values()),
        "keys": {parser.canonical_sclk(k) for k in keys}, "paths": paths, "R": R, "base": base,
    }


def zip_dir(directory: Path, destination: Path, arc_root: Path) -> Path:
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(directory.rglob("*")):
            if file.is_file():
                archive.write(file, file.relative_to(arc_root).as_posix())
    return destination


@pytest.fixture()
def dataset(tmp_path):
    return build_dataset(tmp_path / "ds")


def read_jsonl(path):
    with Path(path).open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream]


def mkparsed(product, level, sclk, raw, *, file="f.csv", row=2, lmst="L", ltst="T", sol=230):
    return {
        "sol": sol, "sclk": sclk, "lmst": lmst, "ltst": ltst,
        "source": {"source_level": level, "product": product, "source_file": file,
                   "source_row": row, "release": 2, "original_sclk": str(sclk)},
        "raw": raw,
    }


# =========================================================================
# A. Parser
# =========================================================================

ALL_PRODUCT_TOKENS = [("ATS", "ATS"), ("ENG", "ENG"), ("PS", "PS"), ("RDS", "RDS"), ("RHS", "RHS"),
                      ("TIRS", "TIRS"), ("WS", "WS"), ("ANC", "ANCILLARY")]


@pytest.mark.parametrize("product,token", ALL_PRODUCT_TOKENS)
@pytest.mark.parametrize("level,prefix", [("calibrated", "CAL"), ("derived", "DER")])
def test_identify_every_product_and_level(product, token, level, prefix):
    name = f"WE__0235___________{prefix}_{token}__________________P02.CSV"
    assert parser.identify_file(Path("any/where") / name) == (235, product, level)


def test_identify_legacy_names_and_sol_directory_fallback():
    assert parser.identify_file(Path("resources/MEDA_Weather_ds/sol_0230/WE__0230_CAL_WS.CSV")) == (230, "WS", "calibrated")
    assert parser.identify_file(Path("derived/WE__0230_DER_ANCILLARY.CSV")) == (230, "ANC", "derived")
    assert parser.identify_file(Path("sol_0231/CAL_PS_P02.CSV")) == (231, "PS", "calibrated")


def test_identify_rejects_unrelated_and_unknown():
    assert parser.identify_file(Path("notes.csv")) is None
    assert parser.identify_file(Path("WE__0230___________CAL_SKYCAM__P01.CSV")) is None
    assert parser.identify_file(Path("CAL_WS.CSV")) is None  # no Sol anywhere


def test_discovery_recursive_with_labels_and_explicit_skips(tmp_path):
    ds = build_dataset(tmp_path)
    (ds["cal"] / "sol_0230" / "unrelated_table.csv").write_text("a,b\n1,2\n")
    (ds["cal"] / "sol_0230" / "WE__0230___________CAL_SKYCAM__P01.CSV").write_text("SCLK\n1\n")
    (tmp_path / "derived" / "deep" / "er").mkdir(parents=True)
    (tmp_path / "derived" / "deep" / "er" / "lonely.xml").write_text("<x/>")
    found = parser.discover_products(tmp_path)
    assert found.csv_found == 13
    assert len(found.products) == 11
    reasons = {s.path.name: s.reason for s in found.skipped}
    assert reasons["unrelated_table.csv"] == "not_meda_product"
    assert reasons["WE__0230___________CAL_SKYCAM__P01.CSV"] == "unsupported_product:SKYCAM"
    assert all(p.label_path is not None for p in found.products)  # XML discovered
    assert [p.path.name for p in found.orphan_labels and []] == []  # attribute exists
    assert any(o.name == "lonely.xml" for o in found.orphan_labels)
    assert found.counts_by_product()[("WS", "calibrated")] == 1
    assert found.counts_by_product()[("ANC", "derived")] == 1
    assert found.sols() == [230]


def test_discovery_is_deterministic_and_sol_filter(tmp_path):
    ds = build_dataset(tmp_path)
    write_product(ds["cal"] / "sol_0231", 231, "PS", "calibrated", [mkrow("PS", "calibrated", 5, BAROCAP1_PRESSURE=1)])
    first = [p.path.name for p in parser.discover_products(tmp_path).products]
    assert first == [p.path.name for p in parser.discover_products(tmp_path).products]
    only_231 = parser.discover_products(tmp_path, sols=(231, 231))
    assert [p.sol for p in only_231.products] == [231]
    assert sum(1 for s in only_231.skipped if s.reason == "outside_sol_range") == 11


def test_discovery_keeps_highest_release_only(tmp_path):
    folder = tmp_path / "sol_0230"
    write_product(folder, 230, "PS", "derived", [mkrow("PS", "derived", 1, PRESSURE=1)], release=1)
    newer = write_product(folder, 230, "PS", "derived", [mkrow("PS", "derived", 1, PRESSURE=2)], release=3)
    found = parser.discover_products(tmp_path)
    assert [p.path for p in found.products] == [newer]
    assert [s.reason for s in found.skipped] == ["superseded_release"]


def test_discovery_missing_root():
    with pytest.raises(FileNotFoundError):
        parser.discover_products("definitely/not/here")


def test_parse_rows_values_and_provenance(dataset):
    path = dataset["paths"][("PS", "calibrated")]
    rows = list(parser.iter_product_rows(path))
    assert len(rows) == 2
    r = rows[0]
    assert r["sol"] == 230 and r["sclk"] == "1000"
    assert r["lmst"] == "00230M00:00:01.000" and r["ltst"] == "0230 00:21:20"
    assert r["source"] == {
        "source_level": "calibrated", "product": "PS", "source_file": path.name,
        "release": 2, "original_sclk": "1000", "source_row": 2,
    }
    assert rows[1]["source"]["source_row"] == 3
    raw = r["raw"]
    assert raw["BAROCAP1_PRESSURE"] == 671.69 and isinstance(raw["BAROCAP1_PRESSURE"], float)
    assert raw["TRANSDUCER"] == 1 and isinstance(raw["TRANSDUCER"], int)
    assert raw["PRESSURE_MEASUREMENT_MODE"] == "nominal"
    assert raw["BAROCAP1_PRESSURE_UNCERTAINTY"] is None  # empty cell, key present
    assert list(raw) == [c for c in H[("PS", "calibrated")] if c not in ("SCLK", "LMST", "LTST")]


def test_parse_zero_is_a_value_and_text_stays_text(tmp_path):
    p = write_product(tmp_path, 230, "WS", "derived", [
        mkrow("WS", "derived", 7, HORIZONTAL_WIND_SPEED=0, WIND_DIRECTION="0.0", ROVER_STILL="NaN"),
    ], label=False)
    raw = next(parser.iter_product_rows(p))["raw"]
    assert raw["HORIZONTAL_WIND_SPEED"] == 0 and raw["HORIZONTAL_WIND_SPEED"] is not None
    assert raw["WIND_DIRECTION"] == 0.0
    assert raw["ROVER_STILL"] == "NaN"  # never turned into a float / None


def test_parse_different_column_order(tmp_path):
    header = H[("PS", "derived")]
    shuffled = ["SCLK", "LMST", "LTST", "TRANSDUCER", "PRESSURE_UNCERTAINTY", "PRESSURE"]
    a = write_product(tmp_path / "a", 230, "PS", "derived", [mkrow("PS", "derived", 9, PRESSURE=12.5, TRANSDUCER=2)], label=False)
    b_dir = tmp_path / "b"
    b = write_product(b_dir, 230, "PS", "derived", [["9", "L", "T", "2", "", "12.5"]], header=shuffled, label=False)
    ra, rb = next(parser.iter_product_rows(a)), next(parser.iter_product_rows(b))
    assert ra["raw"]["PRESSURE"] == rb["raw"]["PRESSURE"] == 12.5
    assert ra["raw"]["TRANSDUCER"] == rb["raw"]["TRANSDUCER"] == 2
    assert header != shuffled


def test_parse_missing_required_column(tmp_path):
    p = write_product(tmp_path, 230, "PS", "derived", [["L", "T", "1"]], header=["LMST", "LTST", "PRESSURE"], label=False)
    with pytest.raises(parser.MissingColumnError) as err:
        list(parser.iter_product_rows(p))
    assert p.name in str(err.value) and err.value.source_file == p.name


@pytest.mark.parametrize("bad,reason", [
    ("", "missing_sclk"), ("abc", "invalid_sclk"), ("-5", "invalid_sclk"),
    ("1e3", "invalid_sclk"), ("NaN", "invalid_sclk"), ("1.2.3", "invalid_sclk"),
])
def test_parse_bad_sclk_rows_are_rejected_counted_and_explained(tmp_path, bad, reason):
    rows = [mkrow("PS", "derived", 10, PRESSURE=1), mkrow("PS", "derived", bad, PRESSURE=2),
            mkrow("PS", "derived", 11, PRESSURE=3)]
    p = write_product(tmp_path, 230, "PS", "derived", rows, label=False)
    stats = parser.ParseStats()
    out = list(parser.iter_product_rows(p, stats=stats))
    assert [r["sclk"] for r in out] == ["10", "11"]
    assert stats.rows_read == 3 and stats.rows_yielded == 2 and stats.rows_rejected == 1
    assert stats.rejections == {reason: 1}
    sample = stats.rejection_samples[0]
    assert sample["source_file"] == p.name and sample["line"] == 3
    with pytest.raises(parser.InvalidSclkError) as err:  # raise mode keeps file + line
        list(parser.iter_product_rows(p, on_error="raise"))
    assert err.value.line == 3 and err.value.source_file == p.name


def test_parse_field_count_mismatch_and_blank_line(tmp_path):
    p = write_product(tmp_path, 230, "PS", "derived", [mkrow("PS", "derived", 1, PRESSURE=1)], label=False)
    with p.open("a", newline="") as fh:
        fh.write("2,L,T,5\r\n\r\n3,L,T,1,,2\r\n")
    stats = parser.ParseStats()
    assert [r["sclk"] for r in parser.iter_product_rows(p, stats=stats)] == ["1", "3"]
    assert stats.rejections == {"field_count_mismatch": 1, "blank_line": 1}
    with pytest.raises(parser.MalformedCsvError):
        list(parser.iter_product_rows(p, on_error="raise"))


def test_parse_malformed_files(tmp_path):
    empty = tmp_path / "WE__0230___________DER_PS__P01.CSV"
    empty.write_text("")
    with pytest.raises(parser.MalformedCsvError):
        list(parser.iter_product_rows(empty))
    dup = tmp_path / "WE__0231___________DER_PS__P01.CSV"
    dup.write_text("SCLK,SCLK,PRESSURE\n1,1,1\n")
    with pytest.raises(parser.MalformedCsvError):
        list(parser.iter_product_rows(dup))
    with pytest.raises(parser.UnsupportedProductError):
        list(parser.iter_product_rows(tmp_path / "random.csv"))
    with pytest.raises(parser.MalformedCsvError):
        list(parser.iter_product_rows(tmp_path / "WE__0232___________DER_PS__P01.CSV"))  # does not exist


def test_parse_label_fields_units_and_name_mismatch_warning(dataset):
    path = dataset["paths"][("WS", "calibrated")]
    label = parser.parse_label(path.with_suffix(".xml"))
    assert label.processing_level == "Calibrated"
    assert label.declared_table_records == 3
    by_name = {f.name: f for f in label.fields}
    assert by_name["BOOM1_HORIZONTAL_WIND_SPEED"].unit == "meter/second"
    assert by_name["SCLK"].data_type == "ASCII_Real" and by_name["SCLK"].unit == "second"
    stats = parser.ParseStats()
    list(parser.iter_product_rows(path, stats=stats))
    assert stats.warnings["label_name_mismatch"] == 2  # BOOMn_ALG_STATE_STABLE vs ..._IN_STABLE_STATE
    assert stats.warnings.get("rows_differ_from_label_declared_records") is None


def test_parse_label_errors(tmp_path):
    bad = tmp_path / "x.xml"
    bad.write_text("<not-closed>")
    with pytest.raises(parser.XmlLabelError):
        parser.parse_label(bad)
    empty = tmp_path / "y.xml"
    empty.write_text("<a xmlns='http://pds.nasa.gov/pds4/pds/v1'/>")
    with pytest.raises(parser.XmlLabelError):
        parser.parse_label(empty)
    with pytest.raises(parser.XmlLabelError):
        parser.parse_label(tmp_path / "absent.xml")


def test_label_level_contradiction_and_record_count_mismatch(tmp_path):
    rows = [mkrow("PS", "derived", 1, PRESSURE=1)]
    p = write_product(tmp_path / "a", 230, "PS", "derived", rows, label_kwargs={"processing_level": "Calibrated"})
    with pytest.raises(parser.XmlLabelError):
        list(parser.iter_product_rows(p))
    q = write_product(tmp_path / "b", 230, "PS", "derived", rows)
    q.with_suffix(".xml").write_text(label_xml("PS", "derived", H[("PS", "derived")], 99))
    stats = parser.ParseStats()
    list(parser.iter_product_rows(q, stats=stats))
    assert stats.warnings["rows_differ_from_label_declared_records"] == 1


def test_non_numeric_text_in_numeric_field_is_flagged(tmp_path):
    p = write_product(tmp_path, 230, "PS", "derived", [mkrow("PS", "derived", 1, PRESSURE="oops")])
    stats = parser.ParseStats()
    row = next(parser.iter_product_rows(p, stats=stats))
    assert row["raw"]["PRESSURE"] == "oops" and stats.warnings["non_numeric_in_numeric_field"] == 1
    with pytest.raises(parser.DataTypeError) as err:
        list(parser.iter_product_rows(p, on_error="raise"))
    assert err.value.line == 2 and err.value.value == "oops"


def test_parse_exposes_lmst_sol_warning(tmp_path):
    p = write_product(tmp_path, 230, "PS", "derived", [mkrow("PS", "derived", 1, PRESSURE=1, LMST="00999M00:00:00.000")], label=False)
    stats = parser.ParseStats()
    assert len(list(parser.iter_product_rows(p, stats=stats))) == 1
    assert stats.warnings["lmst_sol_differs_from_file_sol"] == 1


def test_iter_archive_rows_reads_everything_in_deterministic_order(dataset):
    a = list(parser.iter_archive_rows(dataset["cal"].parent))
    b = list(parser.iter_archive_rows(dataset["cal"].parent))
    assert a == b and len(a) == dataset["rows"]


def test_safe_extract_zip(tmp_path, dataset):
    z = zip_dir(dataset["cal"], tmp_path / "c.zip", dataset["base"])
    out = parser.safe_extract_zip(z, tmp_path / "x")
    assert parser.discover_products(out).products
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../escape.txt", "x")
    with pytest.raises(parser.ArchiveError):
        parser.safe_extract_zip(evil, tmp_path / "y")
    (tmp_path / "fake.zip").write_text("not a zip")
    with pytest.raises(parser.ArchiveError):
        parser.safe_extract_zip(tmp_path / "fake.zip", tmp_path / "z")


# =========================================================================
# B. SCLK precision (mandatory)
# =========================================================================

SCLK_SAMPLES = ["100", "100.0", "100.001", "100.010", "100.125", "100.999"]


def test_sclk_canonical_values_are_exact():
    assert [parser.canonical_sclk(s) for s in SCLK_SAMPLES] == ["100", "100", "100.001", "100.01", "100.125", "100.999"]
    for s in SCLK_SAMPLES:
        assert parser.parse_sclk(s) == Decimal(s)  # exact decimal value, nothing rounded
        assert parser.canonical_sclk(parser.canonical_sclk(s)) == parser.canonical_sclk(s)  # idempotent


def test_sclk_equivalent_numbers_share_one_canonical_form():
    assert parser.canonical_sclk("100") == parser.canonical_sclk("100.0") == parser.canonical_sclk("0100.000")
    assert parser.canonical_sclk("100.010") == parser.canonical_sclk("100.01")
    assert parser.canonical_sclk("100.001") != parser.canonical_sclk("100.01")  # different values stay different


def test_sclk_no_float_collision():
    a, b = "688111313.00000001", "688111313.00000002"
    assert float(a) == float(b)  # why float is forbidden: these collide as floats
    assert parser.canonical_sclk(a) != parser.canonical_sclk(b)
    assert parser.sclk_sort_parts(a) < parser.sclk_sort_parts(b)
    with pytest.raises(TypeError):
        parser.canonical_sclk(100.125)


def test_sclk_ordering_is_numeric_not_lexicographic():
    values = ["1000", "99.9999", "100", "100.001", "100.01", "100.125", "100.999", "99", "100.0009"]
    by_parts = sorted(values, key=lambda v: parser.sclk_sort_parts(v))
    by_decimal = sorted(values, key=Decimal)
    assert by_parts == by_decimal


@pytest.mark.parametrize("bad", ["", " ", "1e3", "+5", "-1", "5.", ".5", "1,5", "0x10", "NaN", "Infinity", "9" * 30])
def test_sclk_invalid_forms_rejected(bad):
    with pytest.raises(parser.InvalidSclkError):
        parser.parse_sclk(bad, source_file="f.csv", line=7)


def test_sclk_survives_csv_parser_storage_normalizer_and_json(tmp_path):
    """CSV -> parser -> SQLite staging -> normaliser -> JSON, all six sample values."""
    a = write_product(tmp_path / "a", 230, "PS", "derived",
                      [mkrow("PS", "derived", s, PRESSURE=i + 1) for i, s in enumerate(["100", "100.001", "100.010", "100.125", "100.999"])])
    b = write_product(tmp_path / "b", 230, "WS", "derived",
                      [mkrow("WS", "derived", s, HORIZONTAL_WIND_SPEED=i + 1) for i, s in enumerate(["100.0", "100.01"])])
    rows = list(parser.iter_product_rows(a)) + list(parser.iter_product_rows(b))
    assert {r["sclk"] for r in rows} == {"100", "100.001", "100.01", "100.125", "100.999"}
    assert {r["source"]["original_sclk"] for r in rows} >= {"100.010", "100.0"}  # original text kept

    out = tmp_path / "o.jsonl"
    streaming.stream_normalize(iter(rows), out)
    records = read_jsonl(out)
    assert [r["sclk_s"] for r in records] == ["100", "100.001", "100.01", "100.125", "100.999"]
    by_sclk = {r["sclk_s"]: r for r in records}
    # numerically equivalent values merged, different values did not
    assert {s["product"] for s in by_sclk["100"]["sources"]} == {"PS", "WS"}
    assert {s["product"] for s in by_sclk["100.01"]["sources"]} == {"PS", "WS"}
    assert {s["product"] for s in by_sclk["100.001"]["sources"]} == {"PS"}
    assert by_sclk["100.01"]["sources"][0]["original_sclk"] in ("100.010", "100.01", "100.0")
    assert all(isinstance(r["sclk_s"], str) for r in records)  # JSON number would round


def test_sqlite_staging_never_stores_sclk_as_float_or_truncated_integer(tmp_path):
    stage = streaming._Stage(tmp_path / "s.sqlite")
    rows = [mkparsed("PS", "derived", parser.canonical_sclk(s), {"PRESSURE": 1.0}, row=i)
            for i, s in enumerate(SCLK_SAMPLES, start=2)]
    stage.insert_batch([stage.to_tuple(r) for r in rows])
    cols = {r[1]: r[2] for r in stage.conn.execute("PRAGMA table_info(source_rows)")}
    assert cols["sclk"] == "TEXT" and cols["sclk_frac"] == "TEXT" and cols["sclk_int"] == "INTEGER"
    stored = stage.conn.execute("SELECT typeof(sclk_int), typeof(sclk_frac), typeof(sclk), sclk, sclk_int, sclk_frac "
                                "FROM source_rows ORDER BY sol, sclk_int, sclk_frac, id").fetchall()
    assert all(t[:3] == ("integer", "text", "text") for t in stored)
    assert [t[3] for t in stored] == ["100", "100", "100.001", "100.01", "100.125", "100.999"]
    assert [t[5] for t in stored] == ["", "", "001", "01", "125", "999"]
    keys = {(t[4], t[5]) for t in stored}
    assert len(keys) == 5  # 100 and 100.0 -> one key; no accidental collisions
    stage.close()


# =========================================================================
# C. Normalizer
# =========================================================================


def _full_raw(product, level):
    specs = normalize.CHANNEL_SPECS[(product, level)]
    raw = {}
    for i, s in enumerate(specs, start=1):
        raw[s.raw_name] = 10.0 + i
        if s.uncertainty:
            raw[s.uncertainty] = 0.5
    return raw


@pytest.mark.parametrize("key", sorted(normalize.CHANNEL_SPECS))
def test_every_supported_mapping_produces_traceable_measurements(key):
    product, level = key
    row = mkparsed(product, level, "5", _full_raw(product, level), file=f"{product}_{level}.csv", row=9)
    record = normalize.normalize_group([row])
    specs = normalize.CHANNEL_SPECS[key]
    assert set(record["measurements"]) == {s.canonical for s in specs}
    for spec in specs:
        m = record["measurements"][spec.canonical]
        assert m["source_channel"] == spec.raw_name and m["source_product"] == product
        assert m["source_level"] == level and m["source_file"] == f"{product}_{level}.csv" and m["source_row"] == 9
        assert m["unit"] == spec.unit and m["source_unit"] == spec.label_unit and m["validity"] == "valid"
        assert m["unit_basis"] in ("label", "bundle_description", "not_stated")
        if spec.uncertainty:
            assert m["uncertainty"]["value"] == 0.5 and m["uncertainty"]["source_channel"] == spec.uncertainty
    json.dumps(record, allow_nan=False)


def test_all_eight_products_both_levels_are_covered_by_the_mapping():
    covered = {k for k in normalize.CHANNEL_SPECS} | {("ENG", "calibrated"), ("RDS", "calibrated")}
    assert {p for p, _ in covered} == {"ATS", "ENG", "PS", "RDS", "RHS", "TIRS", "WS", "ANC"}


def test_raw_channel_products_keep_every_channel():
    eng = {"ICU_Analog_TH_HK_TM": 283.63, "ICU_Analog_POWER_H_SENSE": None, "ICU_Analog_POWER_P1": 0}
    rds = {"RDS_LAT_1": 1.2e-7, "RDS_TOP_3": None, "ROVER_STILL": 1}
    record = normalize.normalize_group([mkparsed("ENG", "calibrated", "5", eng), mkparsed("RDS", "calibrated", "5", rds)])
    assert record["raw_channels"]["ENG.calibrated"] == eng
    assert record["raw_channels"]["RDS.calibrated"] == rds
    assert record["raw_channels"]["ENG.calibrated"]["ICU_Analog_POWER_P1"] == 0  # zero stays zero
    assert record["measurements"] == {}  # no invented canonical measurements for ENG/RDS
    assert normalize.CHANNEL_UNITS[("ENG", "calibrated", "ICU_Analog_5V_CURRENT_TM")] == "amp"
    assert normalize.CHANNEL_UNITS[("RDS", "calibrated", "RDS_LAT_1")] == "amp"
    assert normalize.CHANNEL_UNITS[("RDS", "calibrated", "RDS_TOP_2_HG")] == "watt/m2"


def test_channels_are_never_averaged_or_merged():
    ps = {f"BAROCAP{i}_PRESSURE": 600.0 + i for i in (1, 2, 3)}
    ats = {f"ATS_LOCAL_TEMP{i}": 200.0 + i for i in range(1, 6)}
    ws = {"BOOM1_HORIZONTAL_WIND_SPEED": 3.0, "BOOM2_HORIZONTAL_WIND_SPEED": 5.0}
    rec = normalize.normalize_group([
        mkparsed("PS", "calibrated", "5", ps), mkparsed("ATS", "calibrated", "5", ats),
        mkparsed("WS", "calibrated", "5", ws)])
    m = rec["measurements"]
    assert [m[f"pressure_barocap{i}_pa"]["value"] for i in (1, 2, 3)] == [601.0, 602.0, 603.0]
    assert [m[f"ats_local_temp_{i}_k"]["value"] for i in range(1, 6)] == [201.0, 202.0, 203.0, 204.0, 205.0]
    assert m["boom1_horizontal_wind_speed_m_s"]["value"] == 3.0 and m["boom2_horizontal_wind_speed_m_s"]["value"] == 5.0
    assert not any(("mean" in k or "avg" in k or "average" in k) for k in m)
    assert not any(k in m for k in ("pressure_pa", "air_temp_k", "horizontal_wind_speed_m_s"))


def test_calibrated_and_derived_stay_separate():
    tirs_c = mkparsed("TIRS", "calibrated", "5", {"DOWNWARD_LW_IRRADIANCE": 3.0, "ROVER_STILL": 1}, file="c.csv")
    tirs_d = mkparsed("TIRS", "derived", "5", {"DOWNWARD_LW_IRRADIANCE": 4.0, "ROVER_STILL": 0}, file="d.csv")
    rec = normalize.normalize_group([tirs_d, tirs_c])
    c = rec["measurements"]["downward_lw_irradiance_cal_w_m2"]
    d = rec["measurements"]["downward_lw_irradiance_derived_w_m2"]
    assert (c["value"], c["source_level"], c["source_file"]) == (3.0, "calibrated", "c.csv")
    assert (d["value"], d["source_level"], d["source_file"]) == (4.0, "derived", "d.csv")
    assert rec["quality"]["TIRS.calibrated.ROVER_STILL"] == 1 and rec["quality"]["TIRS.derived.ROVER_STILL"] == 0
    assert "conflicts" not in rec  # different levels are not conflicts
    ws = normalize.normalize_group([mkparsed("WS", "calibrated", "5", {"BOOM1_HORIZONTAL_WIND_SPEED": 5.0}),
                                    mkparsed("WS", "derived", "5", {"HORIZONTAL_WIND_SPEED": 4.0})])
    assert {"boom1_horizontal_wind_speed_m_s", "horizontal_wind_speed_m_s"} <= set(ws["measurements"])


def test_preference_helper_is_explicit_and_skips_invalid():
    rec = normalize.normalize_group([
        mkparsed("WS", "derived", "5", {"HORIZONTAL_WIND_SPEED": 999999999}),
        mkparsed("WS", "calibrated", "5", {"BOOM1_HORIZONTAL_WIND_SPEED": 5.0})])
    pick = normalize.prefer_derived_measurement(rec, "horizontal_wind_speed_m_s", ["boom1_horizontal_wind_speed_m_s"])
    assert pick["source_level"] == "calibrated" and pick["value"] == 5.0
    assert rec["measurements"]["horizontal_wind_speed_m_s"]["raw_value"] == 999999999  # original untouched
    assert normalize.prefer_derived_measurement(rec, "nope", []) is None


def test_missing_values_are_distinguished_and_zero_is_not_missing():
    rec = normalize.normalize_group([mkparsed("WS", "derived", "5", {
        "HORIZONTAL_WIND_SPEED": 0, "VERTICAL_WIND_SPEED": None, "WIND_DIRECTION": 0.0})])
    assert rec["measurements"]["horizontal_wind_speed_m_s"]["value"] == 0
    assert rec["measurements"]["wind_direction_deg"]["value"] == 0.0
    assert "vertical_wind_speed_m_s" not in rec["measurements"]
    assert rec["missing"] == {"vertical_wind_speed_m_s": "empty_cell"}
    absent = normalize.normalize_group([mkparsed("WS", "derived", "5", {"HORIZONTAL_WIND_SPEED": 1.0})])
    assert absent["missing"]["wind_direction_deg"] == "column_absent"
    text = normalize.normalize_group([mkparsed("WS", "derived", "5", {"HORIZONTAL_WIND_SPEED": "garbage"})])
    m = text["measurements"]["horizontal_wind_speed_m_s"]
    assert m["validity"] == "non_numeric" and m["value"] is None and m["raw_value"] == "garbage"


def test_quality_flags_preserved_verbatim_and_not_interpreted():
    raw = {"ROVER_STILL": 1, "BOTH_BOOMS_USED_FOR_RETRIEVAL": 7, "HORIZONTAL_WIND_SPEED": 2.0,
           "HORIZONTAL_WIND_SPEED_UNCERTAINTY": 0.0}
    rec = normalize.normalize_group([mkparsed("WS", "derived", "5", raw)])
    assert rec["quality"] == {
        "WS.derived.ROVER_STILL": 1, "WS.derived.BOTH_BOOMS_USED_FOR_RETRIEVAL": 7,
        "WS.derived.HORIZONTAL_WIND_SPEED_UNCERTAINTY": 0.0}  # unknown flag value 7 kept, 0.0 kept
    blob = json.dumps(rec).lower()
    assert "good" not in blob and "bad" not in blob
    assert "motionless" in normalize.FLAG_DEFINITIONS["ROVER_STILL"]  # official label wording only


def test_uncertainty_preserved_and_never_invented():
    rec = normalize.normalize_group([mkparsed("PS", "calibrated", "5", {
        "BAROCAP1_PRESSURE": 600.0, "BAROCAP1_PRESSURE_UNCERTAINTY": 1.5,
        "BAROCAP2_PRESSURE": 601.0, "BAROCAP2_PRESSURE_UNCERTAINTY": None})])
    m = rec["measurements"]
    assert m["pressure_barocap1_pa"]["uncertainty"] == {
        "value": 1.5, "unit": "Pa", "source_channel": "BAROCAP1_PRESSURE_UNCERTAINTY", "validity": "valid"}
    assert "uncertainty" not in m["pressure_barocap2_pa"]
    assert rec["quality"]["PS.calibrated.BAROCAP1_PRESSURE_UNCERTAINTY"] == 1.5
    assert "PS.calibrated.BAROCAP2_PRESSURE_UNCERTAINTY" not in rec["quality"]


def test_sentinel_handling_scoped_documented_and_reversible():
    rec = normalize.normalize_group([mkparsed("WS", "derived", "5", {
        "HORIZONTAL_WIND_SPEED": 999999999, "WIND_DIRECTION": 999999999, "VERTICAL_WIND_SPEED": 99999999,
        "ROVER_STILL": 999999999})])
    h = rec["measurements"]["horizontal_wind_speed_m_s"]
    assert h["validity"] == "invalid_sentinel" and h["value"] is None and h["raw_value"] == 999999999
    assert "SIS" in h["validity_reason"]
    assert rec["measurements"]["wind_direction_deg"]["validity"] == "invalid_sentinel"
    assert rec["measurements"]["vertical_wind_speed_m_s"]["validity"] == "valid"  # 8 nines: not a sentinel
    assert rec["quality"]["WS.derived.ROVER_STILL"] == 999999999  # flags are never reinterpreted
    assert normalize.classify_value(1) == ("valid", None) and normalize.classify_value(0) == ("valid", None)
    assert normalize.classify_value(9999999999)[0] == "invalid_sentinel"
    stats = normalize.NormalizationStats()
    normalize.normalize_group([mkparsed("WS", "derived", "5", {"HORIZONTAL_WIND_SPEED": 999999999})], stats)
    assert stats.invalid_measurements == {"horizontal_wind_speed_m_s|invalid_sentinel": 1}
    eng = normalize.normalize_group([mkparsed("ENG", "calibrated", "5", {"ICU_Analog_TH_HK_TM": 999999999})])
    assert eng["raw_channel_issues"] == {"ENG.calibrated": {"ICU_Analog_TH_HK_TM": "invalid_sentinel"}}
    assert eng["raw_channels"]["ENG.calibrated"]["ICU_Analog_TH_HK_TM"] == 999999999  # raw kept


def test_identical_duplicates_counted_conflicting_duplicates_reported_not_overwritten():
    a = mkparsed("WS", "derived", "5", {"HORIZONTAL_WIND_SPEED": 2.0}, file="d.csv", row=2)
    a2 = mkparsed("WS", "derived", "5", {"HORIZONTAL_WIND_SPEED": 2.0}, file="d.csv", row=2)
    stats = normalize.NormalizationStats()
    rec = normalize.normalize_group([a, a2], stats)
    assert stats.duplicates_identical == 1 and "conflicts" not in rec and len(rec["sources"]) == 2
    b = mkparsed("WS", "derived", "5", {"HORIZONTAL_WIND_SPEED": 3.0}, file="d2.csv", row=8)
    rec = normalize.normalize_group([a, b], stats)
    assert rec["measurements"]["horizontal_wind_speed_m_s"]["value"] == 2.0  # first kept, not last-write-wins
    conflict = rec["conflicts"][0]
    assert conflict["kind"] == "duplicate_measurement" and conflict["other"]["value"] == 3.0
    assert conflict["other"]["source_file"] == "d2.csv" and stats.conflicts["duplicate_measurement"] == 1


def test_conflicting_time_and_quality_metadata_are_reported():
    a = mkparsed("TIRS", "derived", "5", {"ROVER_STILL": 1}, lmst="00230M00:00:01.000", ltst="x")
    b = mkparsed("TIRS", "derived", "5", {"ROVER_STILL": 0}, lmst="00230M00:00:02.000", ltst="x", file="g.csv", row=3)
    rec = normalize.normalize_group([a, b])
    kinds = sorted(c["kind"] for c in rec["conflicts"])
    assert kinds == ["quality", "time_metadata"]
    assert rec["lmst"] == "00230M00:00:01.000" and rec["quality"]["TIRS.derived.ROVER_STILL"] == 1


def test_unknown_channels_are_preserved_and_counted():
    stats = normalize.NormalizationStats()
    rec = normalize.normalize_group([
        mkparsed("PS", "derived", "5", {"PRESSURE": 1.0, "MYSTERY_CHANNEL": 42}),
        mkparsed("ATS", "calibrated", "5", {"ATS_LOCAL_TEMP1": 1.0, "ATS_EXTRA": 3.0})], stats)
    assert rec["unmapped_channels"] == {"PS.derived": {"MYSTERY_CHANNEL": 42}}
    assert rec["raw_channels"]["ATS.calibrated"]["ATS_EXTRA"] == 3.0
    assert stats.unmapped_columns == {"PS.derived.MYSTERY_CHANNEL": 1}


def test_utc_is_never_guessed_and_sclk_is_string():
    rec = normalize.normalize_group([mkparsed("PS", "derived", "5", {"PRESSURE": 1.0}, lmst="00230M00:00:01.000")])
    assert rec["utc"] is None and rec["lmst"] == "00230M00:00:01.000" and rec["sclk_s"] == "5"
    assert list(rec)[:8] == ["sol", "sclk_s", "lmst", "ltst", "utc", "measurements", "quality", "sources"]


def test_unit_mapping_regressions_and_no_silent_conversion():
    rec = normalize.normalize_group([mkparsed("ANC", "derived", "5", {
        "ROVER_VELOCITY": 128.06, "SOLAR_LONGITUDE_ANGLE": -7.3, "SOLAR_ZENITHAL_ANGLE": 137.74,
        "ROVER_POSITION_X": -641.83})])
    m = rec["measurements"]
    assert "rover_velocity_m_s" not in m
    v = m["rover_velocity_m_per_hour"]
    assert (v["value"], v["unit"], v["source_unit"]) == (128.06, "m/h", "meter/hour")  # value untouched
    assert "solar_longitude_deg" not in m and "solar_azimuth_local_level_deg" in m
    assert m["solar_azimuth_local_level_deg"]["value"] == -7.3
    assert m["solar_azimuth_local_level_deg"]["source_channel"] == "SOLAR_LONGITUDE_ANGLE"  # original name kept
    assert m["solar_zenithal_angle_deg"]["value"] == 137.74  # not inverted / converted
    assert m["solar_zenithal_angle_deg"]["source_channel"] == "SOLAR_ZENITHAL_ANGLE"
    assert m["rover_position_x_m"]["unit"] == "m"
    assert normalize.LEGACY_CANONICAL_ALIASES["rover_velocity_m_s"] == "rover_velocity_m_per_hour"


@pytest.mark.parametrize("key,canonical,symbol,label_unit", [
    (("PS", "derived"), "pressure_pa", "Pa", "pascal"),
    (("RHS", "derived"), "volume_mixing_ratio_ppm", "ppm", "ppm"),
    (("RHS", "derived"), "humidity_sensor_temp_k", "K", "kelvin"),
    (("WS", "derived"), "horizontal_wind_speed_m_s", "m/s", "meter/second"),
    (("WS", "calibrated"), "boom2_wind_direction_deg", "deg", "degree"),
    (("TIRS", "calibrated"), "upward_sw_irradiance_w_m2", "W/m2", "watt/m2"),
    (("ATS", "calibrated"), "ats_local_temp_3_k", "K", "kelvin"),
])
def test_unit_table_entries(key, canonical, symbol, label_unit):
    spec = {s.canonical: s for s in normalize.CHANNEL_SPECS[key]}[canonical]
    assert (spec.unit, spec.label_unit, spec.unit_basis) == (symbol, label_unit, "label")


def test_relative_humidity_unit_comes_from_bundle_description_not_the_label():
    for key in (("RHS", "derived"), ("RHS", "calibrated")):
        for spec in normalize.CHANNEL_SPECS[key]:
            if "humidity" in spec.canonical and spec.canonical.startswith("relative_humidity"):
                assert spec.label_unit is None and spec.unit == "%RH" and spec.unit_basis == "bundle_description"
    # sensor temperature and humidity are separate variables
    derived = {s.canonical for s in normalize.CHANNEL_SPECS[("RHS", "derived")]}
    assert {"relative_humidity_pct", "humidity_sensor_temp_k", "volume_mixing_ratio_ppm"} <= derived


@pytest.mark.parametrize("key", sorted(H))
def test_unit_table_matches_label_snapshot(key, tmp_path):
    product, level = key
    path = write_product(tmp_path, 230, product, level, [mkrow(product, level, 1)])
    label = parser.parse_label(path.with_suffix(".xml"))
    assert normalize.verify_label_units(product, level, H[key], label) == []


def test_label_check_detects_a_wrong_unit(tmp_path):
    path = write_product(tmp_path, 230, "ANC", "derived", [mkrow("ANC", "derived", 1)],
                         label_kwargs={"unit_overrides": {"ROVER_VELOCITY": "meter/second"}})
    label = parser.parse_label(path.with_suffix(".xml"))
    issues = normalize.verify_label_units("ANC", "derived", H[("ANC", "derived")], label)
    assert any("ROVER_VELOCITY" in i and "meter/second" in i for i in issues)
    assert normalize.verify_label_units("ANC", "derived", ["SCLK"], label)  # column-count mismatch is reported


def test_normalizer_contract_violations():
    with pytest.raises(normalize.NormalizationError):
        normalize.normalize_group([])
    with pytest.raises(normalize.NormalizationError):
        normalize.normalize_group([mkparsed("PS", "derived", "5", {}), mkparsed("PS", "derived", "6", {})])
    with pytest.raises(normalize.NormalizationError):
        normalize.normalize_group([mkparsed("PS", "derived", 5.5, {})])  # float SCLK refused
    with pytest.raises(normalize.NormalizationError):
        normalize.normalize_group([{"sclk": "5", "source": {}, "raw": {}}])  # no sol


def test_legacy_rows_with_sclk_s_key_are_accepted():
    row = mkparsed("WS", "derived", "5", {"HORIZONTAL_WIND_SPEED": 4.0})
    row["sclk_s"] = row.pop("sclk")
    row["sclk_s"] = 100  # old integer style
    assert normalize.normalize_rows([row])[0]["sclk_s"] == "100"
    assert set(normalize.MAPPINGS[("WS", "derived")]) >= {"horizontal_wind_speed_m_s"}


def test_normalization_is_deterministic_and_order_independent():
    rows = [mkparsed("PS", "derived", "7", {"PRESSURE": 1.0}, file="d.csv", row=2),
            mkparsed("PS", "calibrated", "7", {"BAROCAP1_PRESSURE": 2.0}, file="c.csv", row=2),
            mkparsed("WS", "derived", "5", {"HORIZONTAL_WIND_SPEED": 3.0}, file="w.csv", row=2),
            mkparsed("WS", "derived", "5.5", {"HORIZONTAL_WIND_SPEED": 3.5}, file="w.csv", row=3)]
    first = json.dumps(normalize.normalize_rows(rows))
    again = json.dumps(normalize.normalize_rows(rows))
    shuffled = json.dumps(normalize.normalize_rows(list(reversed(rows))))
    assert first == again == shuffled
    assert [r["sclk_s"] for r in json.loads(first)] == ["5", "5.5", "7"]


# =========================================================================
# D. Streaming
# =========================================================================


def run_stream(rows, out, **kw):
    return streaming.stream_normalize(iter(rows), out, **kw)


def test_streaming_small_dataset_multiple_products(dataset, tmp_path):
    out = tmp_path / "o" / "x.jsonl"
    rows = parser.iter_archive_rows(dataset["cal"])
    rows = list(rows) + list(parser.iter_archive_rows(dataset["der"]))
    summary = run_stream(rows, out)
    assert summary.parsed_rows == dataset["rows"]
    assert summary.records == len(dataset["keys"]) == 5
    recs = read_jsonl(out)
    assert [r["sclk_s"] for r in recs] == ["1000", "1000.5", "1001", "1002", "1003"]
    first = recs[0]
    assert {"pressure_pa", "pressure_barocap1_pa", "horizontal_wind_speed_m_s", "boom1_horizontal_wind_speed_m_s",
            "ats_local_temp_5_k", "rover_velocity_m_per_hour"} <= set(first["measurements"])
    assert summary.sol_coverage() == {230: 5} and streaming.validate_jsonl(out, 5) == 5
    assert summary.rows_by_product["WS.derived"] == 2


def test_streaming_exact_matching_only_no_row_index_or_nearest_alignment(tmp_path):
    ws = [mkparsed("WS", "derived", s, {"HORIZONTAL_WIND_SPEED": float(i)}, file="w.csv", row=i + 2)
          for i, s in enumerate(["1000", "1002", "1004"])]
    ps = [mkparsed("PS", "derived", s, {"PRESSURE": float(i)}, file="p.csv", row=i + 2)
          for i, s in enumerate(["1001", "1003", "1004.001"])]
    out = tmp_path / "o.jsonl"
    run_stream(ws + ps, out)
    recs = read_jsonl(out)
    assert [r["sclk_s"] for r in recs] == ["1000", "1001", "1002", "1003", "1004", "1004.001"]
    assert all(len({s["product"] for s in r["sources"]}) == 1 for r in recs)  # 1004 vs 1004.001: no tolerance


def test_streaming_duplicate_rows_and_equivalent_sclk(tmp_path):
    a = mkparsed("PS", "derived", "100", {"PRESSURE": 1.0}, file="p.csv", row=2)
    b = mkparsed("PS", "derived", "100.0", {"PRESSURE": 1.0}, file="p.csv", row=2)  # same value, other spelling
    c = mkparsed("PS", "derived", "100", {"PRESSURE": 9.0}, file="q.csv", row=2)  # conflicting duplicate
    out = tmp_path / "o.jsonl"
    summary = run_stream([a, b, c], out)
    [rec] = read_jsonl(out)
    assert summary.records == 1 and summary.normalization.duplicates_identical == 1
    assert rec["measurements"]["pressure_pa"]["value"] == 1.0 and rec["conflicts"][0]["kind"] == "duplicate_measurement"
    assert len(rec["sources"]) == 3  # nothing dropped


def test_streaming_commit_persists_and_rollback_discards_the_whole_batch(tmp_path):
    db = tmp_path / "s.sqlite"
    stage = streaming._Stage(db)
    good = [stage.to_tuple(mkparsed("PS", "derived", str(i), {"PRESSURE": 1.0}, row=i + 2)) for i in range(3)]
    stage.insert_batch(good)
    other = sqlite3.connect(db)
    assert other.execute("SELECT COUNT(*) FROM source_rows").fetchone()[0] == 3  # committed, visible to others
    bad = [stage.to_tuple(mkparsed("PS", "derived", str(10 + i), {"PRESSURE": 1.0}, row=i)) for i in range(3)]
    bad[2] = bad[2][:2] + (None,) + bad[2][3:]  # sol NULL violates NOT NULL
    with pytest.raises(sqlite3.IntegrityError):
        stage.insert_batch(bad)
    assert other.execute("SELECT COUNT(*) FROM source_rows").fetchone()[0] == 3  # batch rolled back entirely
    other.close()
    stage.close()


def _leftovers(directory: Path):
    return sorted(p.name for p in directory.iterdir())


def _ok_rows(n=50):
    return [mkparsed("PS", "derived", str(i), {"PRESSURE": float(i)}, row=i + 2) for i in range(n)]


def test_interrupted_processing_leaves_existing_output_untouched_and_no_temp_files(tmp_path):
    out_dir = tmp_path / "out"
    work = tmp_path / "work"
    out = out_dir / "x.jsonl"
    run_stream(_ok_rows(10), out)
    before = out.read_bytes()

    def exploding():
        yield from _ok_rows(30)
        raise RuntimeError("disk on fire")

    with pytest.raises(RuntimeError):
        run_stream(exploding(), out, workdir=work, batch_size=7)
    assert out.read_bytes() == before
    assert _leftovers(out_dir) == ["x.jsonl"] and _leftovers(work) == []

    def interrupted():
        yield from _ok_rows(5)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_stream(interrupted(), out, workdir=work)
    assert out.read_bytes() == before and _leftovers(out_dir) == ["x.jsonl"] and _leftovers(work) == []


def test_failure_on_first_run_publishes_nothing(tmp_path):
    out = tmp_path / "o" / "x.jsonl"

    def boom():
        yield _ok_rows(1)[0]
        raise ValueError("bad")

    with pytest.raises(ValueError):
        run_stream(boom(), out)
    assert not out.exists() and _leftovers(out.parent) == []


def test_contract_violation_in_a_row_aborts_cleanly(tmp_path):
    out = tmp_path / "x.jsonl"
    bad = _ok_rows(3) + [{"sol": 230, "sclk": "5", "source": {"product": "PS"}, "raw": {}}]
    with pytest.raises(streaming.ContractError):
        run_stream(bad, out)
    assert not out.exists() and _leftovers(tmp_path) == []


def test_restart_after_failure_gives_same_bytes_as_clean_run(tmp_path):
    rows = _ok_rows(40)
    clean = tmp_path / "clean.jsonl"
    run_stream(rows, clean)

    def failing():
        yield from rows[:20]
        raise RuntimeError("crash")

    restarted = tmp_path / "re" / "r.jsonl"
    with pytest.raises(RuntimeError):
        run_stream(failing(), restarted)
    run_stream(rows, restarted)
    assert restarted.read_bytes() == clean.read_bytes()


def test_overwrite_flag_and_invalid_arguments(tmp_path):
    out = tmp_path / "x.jsonl"
    run_stream(_ok_rows(3), out)
    before = out.read_bytes()
    with pytest.raises(FileExistsError):
        run_stream(_ok_rows(5), out, overwrite=False)
    assert out.read_bytes() == before
    with pytest.raises(ValueError):
        run_stream(_ok_rows(1), tmp_path / "y.jsonl", batch_size=0)
    with pytest.raises(IsADirectoryError):
        run_stream(_ok_rows(1), tmp_path)


def test_batch_size_and_arrival_order_do_not_change_output(tmp_path):
    rows = _ok_rows(60) + [mkparsed("WS", "derived", str(i), {"HORIZONTAL_WIND_SPEED": 1.0}, file="w.csv", row=i + 2)
                           for i in range(0, 60, 3)]
    outs = []
    for n, (size, order) in enumerate([(1, rows), (5000, rows), (7, random.Random(3).sample(rows, len(rows)))]):
        p = tmp_path / f"o{n}.jsonl"
        run_stream(order, p, batch_size=size)
        outs.append(p.read_bytes())
    assert outs[0] == outs[1] == outs[2]


def test_streaming_consumes_rows_lazily_with_bounded_python_memory(tmp_path):
    produced = {"n": 0}

    def generator(n=60_000):
        for i in range(n):
            produced["n"] += 1
            yield mkparsed("PS", "derived", str(i), {"PRESSURE": float(i), "TRANSDUCER": 1}, row=i + 2)

    tracemalloc.start()
    run_stream(generator(), tmp_path / "big.jsonl", batch_size=2000)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert produced["n"] == 60_000
    assert peak < 30 * 1024 * 1024, f"peak python allocations {peak / 1e6:.1f} MB"
    assert streaming.validate_jsonl(tmp_path / "big.jsonl", 60_000) == 60_000


def test_validate_jsonl_rejects_bad_output(tmp_path):
    base = {"sol": 230, "sclk_s": "5", "lmst": None, "ltst": None, "utc": None,
            "measurements": {}, "quality": {}, "sources": [{"product": "PS"}]}

    def write(*records, raw=None):
        p = tmp_path / "v.jsonl"
        p.write_text(raw if raw is not None else "".join(json.dumps(r) + "\n" for r in records))
        return p

    assert streaming.validate_jsonl(write(base)) == 1
    with pytest.raises(streaming.OutputValidationError):
        streaming.validate_jsonl(write(base), expected_records=2)
    with pytest.raises(streaming.OutputValidationError):
        streaming.validate_jsonl(write(raw="{not json}\n"))
    with pytest.raises(streaming.OutputValidationError):
        streaming.validate_jsonl(write(dict(base, sclk_s=5)))  # JSON number instead of canonical string
    with pytest.raises(streaming.OutputValidationError):
        streaming.validate_jsonl(write(dict(base, sclk_s="5.10")))
    with pytest.raises(streaming.OutputValidationError):
        streaming.validate_jsonl(write(dict(base), dict(base)))  # not strictly increasing
    with pytest.raises(streaming.OutputValidationError):
        streaming.validate_jsonl(write(dict(base, utc="2021-01-01T00:00:00Z")))
    with pytest.raises(streaming.OutputValidationError):
        streaming.validate_jsonl(write(raw=json.dumps(base).replace('"sol"', '"x"') + "\n"))
    with pytest.raises(streaming.OutputValidationError):
        streaming.validate_jsonl(write(raw=json.dumps(dict(base, quality={})).replace("{}", "NaN", 1) + "\n"))


def test_backwards_compatible_wrapper_signature(tmp_path):
    parsed, records = streaming.normalize_stream_to_jsonl(iter(_ok_rows(4)), tmp_path / "w.jsonl", 2)
    assert (parsed, records) == (4, 4)


# =========================================================================
# E. CLI
# =========================================================================


def run_cli(*argv):
    return cli.main([str(a) for a in argv])


def test_cli_help(capsys):
    assert run_cli("--help") == 0
    text = capsys.readouterr().out
    assert "--calibrated" in text and "--derived" in text and "--output" in text and "exit codes" in text


def test_cli_valid_zip_inputs_success_logging_and_summary(dataset, tmp_path, capsys):
    cal_zip = zip_dir(dataset["cal"], tmp_path / "cal.zip", dataset["base"])
    der_zip = zip_dir(dataset["der"], tmp_path / "der.zip", dataset["base"])
    out = tmp_path / "res" / "out.jsonl"
    summary = tmp_path / "res" / "summary.json"
    code = run_cli("--calibrated", cal_zip, "--derived", der_zip, "--output", out,
                   "--workdir", tmp_path / "wk", "--batch-size", 3, "--summary-json", summary)
    captured = capsys.readouterr()
    assert code == ExitCode.SUCCESS == 0
    for message in ("[INFO] Starting MEDA processing", "[INFO] Calibrated archive discovered",
                    "[INFO] Derived archive discovered", "[INFO] CSV files discovered: 11",
                    "[INFO] Recognized MEDA products: 11", "[INFO] Parsing completed",
                    "[INFO] Normalization completed", "[INFO] Output validation completed",
                    "[INFO] Processing finished"):
        assert message in captured.err, message
    assert "Canonical records (exact Sol + SCLK): 5" in captured.out and "Rows rejected: 0" in captured.out
    data = json.loads(summary.read_text())
    assert data["csv_files_discovered"] == 11 and data["products_recognized"] == 11
    assert data["rows_parsed"] == dataset["rows"] and data["rows_rejected"] == 0
    assert data["canonical_records"] == 5 and data["sol_coverage"] == {"230": 5}
    assert data["label_unit_issues"] == []
    assert streaming.validate_jsonl(out, 5) == 5
    assert not [p for p in (tmp_path / "wk").iterdir()]  # extraction + staging cleaned up


def test_cli_directory_inputs_and_sol_range(dataset, tmp_path):
    write_product(dataset["cal"] / "sol_0231", 231, "PS", "calibrated", [mkrow("PS", "calibrated", 5, BAROCAP1_PRESSURE=1)])
    out = tmp_path / "o.jsonl"
    assert run_cli("--calibrated", dataset["cal"], "--derived", dataset["der"], "--output", out, "--sol-start", 231) == 0
    assert [r["sol"] for r in read_jsonl(out)] == [231]
    out2 = tmp_path / "o2.jsonl"
    assert run_cli("--calibrated", dataset["cal"], "--derived", dataset["der"], "--output", out2,
                   "--sol-start", 230, "--sol-end", 230) == 0
    assert {r["sol"] for r in read_jsonl(out2)} == {230}


def test_cli_rejects_invalid_inputs_with_exit_code_2(dataset, tmp_path, capsys):
    out = tmp_path / "o.jsonl"
    assert run_cli("--output", out) == ExitCode.INVALID_INPUT  # no inputs at all
    assert run_cli("--calibrated", tmp_path / "missing.zip", "--output", out) == 2
    fake = tmp_path / "fake.zip"
    fake.write_text("this is not a zip")
    assert run_cli("--calibrated", fake, "--output", out) == 2
    assert run_cli("--calibrated", dataset["cal"], "--output", tmp_path) == 2  # output is a directory
    blocker = tmp_path / "file.txt"
    blocker.write_text("x")
    assert run_cli("--calibrated", dataset["cal"], "--output", blocker / "sub" / "o.jsonl") == 2
    assert run_cli("--calibrated", dataset["cal"], "--output", out, "--batch-size", 0) == 2
    assert run_cli("--calibrated", dataset["cal"], "--output", out, "--sol-start", 9, "--sol-end", 1) == 2
    assert run_cli("--calibrated", dataset["cal"], "--output", out, "--workdir", blocker) == 2
    empty = tmp_path / "empty"
    empty.mkdir()
    assert run_cli("--calibrated", empty, "--output", out) == 2  # nothing recognisable
    assert run_cli("--calibrated", dataset["cal"], "--bogus-option", "--output", out) == 2  # unsupported argument
    assert not out.exists()
    err = capsys.readouterr().err
    assert "Invalid input" in err and "At least one of" in err


def test_cli_corrupt_zip_member_is_invalid_input(dataset, tmp_path):
    z = zip_dir(dataset["cal"], tmp_path / "c.zip", dataset["base"])
    raw = bytearray(z.read_bytes())
    raw[60:80] = bytes(20)  # damage compressed bytes of the first member
    z.write_bytes(bytes(raw))
    assert run_cli("--calibrated", z, "--output", tmp_path / "o.jsonl") == ExitCode.INVALID_INPUT


def test_cli_existing_output_requires_overwrite(dataset, tmp_path):
    out = tmp_path / "o.jsonl"
    out.write_text("precious")
    assert run_cli("--calibrated", dataset["cal"], "--output", out) == 2
    assert out.read_text() == "precious"
    assert run_cli("--calibrated", dataset["cal"], "--output", out, "--overwrite") == 0
    assert out.read_text() != "precious"


def test_cli_parsing_failure_exit_3_and_existing_output_untouched(dataset, tmp_path, capsys):
    out = tmp_path / "o.jsonl"
    out.write_text("old")
    bad = dataset["paths"][("PS", "derived")]
    bad.write_text("LMST,LTST,PRESSURE\nL,T,1\n")  # SCLK column missing
    assert run_cli("--derived", dataset["der"], "--output", out, "--overwrite") == ExitCode.NORMALIZATION_FAILURE
    assert out.read_text() == "old" and "Normalization failure" in capsys.readouterr().err


def test_cli_rejected_rows_are_reported_and_strict_mode_aborts(dataset, tmp_path):
    with dataset["paths"][("PS", "derived")].open("a", newline="") as fh:
        fh.write("oops,L,T,1,,1\r\n")
    out, report = tmp_path / "o.jsonl", tmp_path / "r.json"
    assert run_cli("--derived", dataset["der"], "--output", out, "--summary-json", report,
                   "--no-strict") == 0
    data = json.loads(report.read_text())
    assert data["rows_rejected"] == 1 and data["rejection_reasons"] == {"invalid_sclk": 1}
    assert data["rejection_samples"][0]["source_file"].endswith("P02.CSV")
    out2 = tmp_path / "strict.jsonl"
    assert run_cli("--derived", dataset["der"], "--output", out2, "--strict") == ExitCode.PARSING_FAILURE
    assert not out2.exists()


@pytest.mark.parametrize("exc,code", [
    (normalize.NormalizationError("n"), ExitCode.NORMALIZATION_FAILURE),
    (streaming.ContractError("c"), ExitCode.NORMALIZATION_FAILURE),
    (OSError("disk full"), ExitCode.OUTPUT_FAILURE),
    (sqlite3.OperationalError("db"), ExitCode.OUTPUT_FAILURE),
    (streaming.OutputValidationError("v"), ExitCode.VALIDATION_FAILURE),
    (ZeroDivisionError("boom"), ExitCode.UNEXPECTED),
])
def test_cli_failure_exit_codes(dataset, tmp_path, monkeypatch, exc, code):
    def fail(*a, **k):
        raise exc

    monkeypatch.setattr(cli, "stream_normalize", fail)
    assert run_cli("--calibrated", dataset["cal"], "--output", tmp_path / "o.jsonl") == code
    assert len({e.value for e in ExitCode}) == 7


def test_cli_documented_command_works_from_any_working_directory(dataset, tmp_path):
    cal_zip = zip_dir(dataset["cal"], tmp_path / "cal.zip", dataset["base"])
    der_zip = zip_dir(dataset["der"], tmp_path / "der.zip", dataset["base"])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    env = dict(os.environ, PYTHONPATH=str(PKG_PARENT))
    help_run = subprocess.run([sys.executable, "-m", "meda_weather.cli", "--help"], cwd=elsewhere, env=env,
                              capture_output=True, text=True)
    assert help_run.returncode == 0 and "--calibrated" in help_run.stdout
    out = tmp_path / "sub" / "cli.jsonl"
    run = subprocess.run([sys.executable, "-m", "meda_weather.cli", "--calibrated", str(cal_zip),
                          "--derived", str(der_zip), "--output", str(out)],
                         cwd=elsewhere, env=env, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert "[INFO] Processing finished" in run.stderr and streaming.validate_jsonl(out, 5) == 5
    bad = subprocess.run([sys.executable, "-m", "meda_weather.cli", "--output", str(out)], cwd=elsewhere, env=env,
                         capture_output=True, text=True)
    assert bad.returncode == 2


def test_importing_the_package_has_no_side_effects(tmp_path):
    code = ("import os, sys; os.chdir(sys.argv[1]); import meda_weather; "
            "print(sorted(os.listdir('.'))); print('meda_weather.cli' in sys.modules)")
    run = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True,
                         env=dict(os.environ, PYTHONPATH=str(PKG_PARENT)))
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["[]", "False"]


def test_public_api_is_importable_and_backwards_compatible():
    import meda_weather as pkg
    for name in ("iter_product_rows", "iter_archive_rows", "normalize_rows", "normalize_stream_to_jsonl"):
        assert callable(getattr(pkg, name))
    assert all(hasattr(pkg, n) for n in pkg.__all__)


# =========================================================================
# F. End-to-end on the synthetic dataset
# =========================================================================


def test_end_to_end_all_ten_products(dataset, tmp_path):
    out = tmp_path / "e2e.jsonl"
    code = run_cli("--calibrated", dataset["cal"], "--derived", dataset["der"], "--output", out)
    assert code == 0
    records = read_jsonl(out)
    by = {r["sclk_s"]: r for r in records}
    assert list(by) == ["1000", "1000.5", "1001", "1002", "1003"]  # exact keys only; fractional SCLK kept exactly

    r = by["1000"]
    products = {(s["product"], s["source_level"]) for s in r["sources"]}
    assert products == {("WS", "calibrated"), ("WS", "derived"), ("PS", "calibrated"), ("PS", "derived"),
                        ("TIRS", "calibrated"), ("TIRS", "derived"), ("ATS", "calibrated"),
                        ("ANC", "derived"), ("RDS", "calibrated"), ("ENG", "calibrated")}
    m = r["measurements"]
    assert m["pressure_pa"]["value"] == 662.25 and m["pressure_pa"]["source_level"] == "derived"
    assert m["pressure_barocap1_pa"]["value"] == 671.69 and m["pressure_barocap1_pa"]["source_level"] == "calibrated"
    assert m["boom2_horizontal_wind_speed_m_s"]["value"] == 4.7 and m["horizontal_wind_speed_m_s"]["value"] == 4.6
    assert m["rover_velocity_m_per_hour"]["value"] == 128.06 and m["rover_velocity_m_per_hour"]["unit"] == "m/h"
    assert m["downward_lw_irradiance_cal_w_m2"]["value"] == 3.77 and m["downward_lw_irradiance_derived_w_m2"]["value"] == 3.9
    assert m["downward_lw_irradiance_cal_w_m2"]["uncertainty"]["value"] == 0.3
    assert m["upward_sw_irradiance_w_m2"]["value"] == 0  # zero is data
    assert r["raw_channels"]["RDS.calibrated"]["RDS_LAT_1"] == 1.2e-7 and "ENG.calibrated" in r["raw_channels"]
    assert r["quality"]["PS.calibrated.PRESSURE_MEASUREMENT_MODE"] == "nominal"
    assert r["quality"]["WS.calibrated.BOOM1_ALG_STATE_STABLE"] == 1
    assert r["utc"] is None and r["lmst"] == "00230M00:00:01.000"

    # asynchronous measurement stays alone; sentinel never presented as a value
    assert {s["product"] for s in by["1000.5"]["sources"]} == {"WS"}
    s = by["1002"]["measurements"]["horizontal_wind_speed_m_s"]
    assert s["validity"] == "invalid_sentinel" and s["value"] is None and s["raw_value"] == 999999999
    assert by["1002"]["measurements"]["boom1_horizontal_wind_speed_m_s"]["value"] == 0

    # provenance: every measurement points at a real file/row/channel
    files = {p.name: p for p in dataset["paths"].values()}
    for record in records:
        for name, meas in record["measurements"].items():
            path = files[meas["source_file"]]
            lines = path.read_text().splitlines()
            header = lines[0].split(",")
            assert meas["source_channel"] in header
            cells = lines[meas["source_row"] - 1].split(",")
            assert parser.canonical_sclk(cells[0]) == record["sclk_s"]
            if meas["validity"] == "valid":
                assert float(cells[header.index(meas["source_channel"])]) == meas["value"]

    assert streaming.validate_jsonl(out, 5) == 5
    again = tmp_path / "again.jsonl"
    assert run_cli("--calibrated", dataset["cal"], "--derived", dataset["der"], "--output", again) == 0
    assert again.read_bytes() == out.read_bytes()  # reproducible


def test_end_to_end_row_accounting(dataset, tmp_path):
    report = tmp_path / "r.json"
    assert run_cli("--calibrated", dataset["cal"], "--derived", dataset["der"], "--output", tmp_path / "o.jsonl",
                   "--summary-json", report) == 0
    data = json.loads(report.read_text())
    assert data["rows_read"] == data["rows_parsed"] + data["rows_rejected"] == dataset["rows"]
    assert sum(data["stream"]["rows_by_product"].values()) == dataset["rows"]
    assert sum(data["stream"]["sol_coverage"].values()) == data["canonical_records"]


# =========================================================================
# G. Regression guards for previously found defects
# =========================================================================


def test_regression_rover_velocity_unit_is_meter_per_hour():
    spec = {s.canonical: s for s in normalize.CHANNEL_SPECS[("ANC", "derived")]}["rover_velocity_m_per_hour"]
    assert spec.label_unit == "meter/hour" and spec.unit == "m/h"
    assert not any("rover_velocity_m_s" in s.canonical for s in normalize.CHANNEL_SPECS[("ANC", "derived")])
    assert not any(unit == "m/s" and raw == "ROVER_VELOCITY" for (raw, unit) in normalize.MAPPINGS[("ANC", "derived")].values())


def test_regression_solar_angle_names_do_not_claim_areocentric_ls():
    names = {s.canonical for s in normalize.CHANNEL_SPECS[("ANC", "derived")]}
    assert "solar_longitude_deg" not in names and "solar_azimuth_local_level_deg" in names
    spec = {s.canonical: s for s in normalize.CHANNEL_SPECS[("ANC", "derived")]}
    assert "NOT areocentric" in spec["solar_azimuth_local_level_deg"].note
    assert "0=zenith" in spec["solar_zenithal_angle_deg"].note


def test_regression_fractional_sclk_is_not_truncated_or_rejected(tmp_path):
    p = write_product(tmp_path, 230, "PS", "derived", [mkrow("PS", "derived", "688111313.125", PRESSURE=1)])
    [row] = list(parser.iter_product_rows(p))
    assert row["sclk"] == "688111313.125" and row["source"]["original_sclk"] == "688111313.125"


def test_regression_duplicate_canonical_measurements_do_not_crash_or_overwrite():
    a = mkparsed("PS", "derived", "5", {"PRESSURE": 1.0}, file="a.csv")
    b = mkparsed("PS", "derived", "5", {"PRESSURE": 2.0}, file="b.csv")
    rec = normalize.normalize_group([a, b])  # the previous version raised ValueError here
    assert rec["measurements"]["pressure_pa"]["value"] == 1.0 and len(rec["conflicts"]) == 1


def test_regression_sentinel_is_not_applied_to_flags_or_time_and_one_is_not_good_or_bad():
    rec = normalize.normalize_group([mkparsed("TIRS", "derived", "5", {"ROVER_STILL": 1, "SUN_OUTSIDE_TIRS_FOV": 999999999})])
    assert rec["quality"] == {"TIRS.derived.ROVER_STILL": 1, "TIRS.derived.SUN_OUTSIDE_TIRS_FOV": 999999999}
    assert "validity" not in str(rec["quality"])


def _module_sources():
    return {name: (PKG_DIR / f"{name}.py").read_text(encoding="utf-8") for name in ("__init__", "cli", "normalize", "parser", "streaming")}


def test_regression_no_hardcoded_machine_paths():
    patterns = [r"[A-Za-z]:\\\\", r"[A-Za-z]:\\", r"/home/", r"/Users/", r"/mnt/", r"D:/", r"project MWI"]
    for name, source in _module_sources().items():
        for pattern in patterns:
            assert not re.search(pattern, source), f"{name}.py contains hardcoded path matching {pattern!r}"


def test_regression_no_silent_or_bare_exception_handling():
    for name, source in _module_sources().items():
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.ExceptHandler):
                continue
            assert node.type is not None, f"{name}.py: bare except at line {node.lineno}"
            broad = isinstance(node.type, ast.Name) and node.type.id in ("Exception", "BaseException")
            silent = all(isinstance(stmt, (ast.Pass, ast.Continue)) for stmt in node.body)
            assert not (broad and silent), f"{name}.py: silent broad except at line {node.lineno}"
            if broad:  # only the CLI's last-resort handler may be broad, and it must log
                assert name == "cli" and "LOG.exception" in ast.unparse(node), f"{name}.py:{node.lineno}"


def test_regression_no_import_time_work_or_circular_imports():
    code = ("import importlib, sys\n"
            "for m in ('parser','normalize','streaming','cli'):\n"
            "    for k in [k for k in sys.modules if k.startswith('meda_weather')]: del sys.modules[k]\n"
            "    importlib.import_module('meda_weather.' + m)\n"
            "print('ok')")
    run = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env=dict(os.environ, PYTHONPATH=str(PKG_PARENT)))
    assert run.stdout.strip() == "ok", run.stderr


# =========================================================================
# H. Optional audit against the real PDS4 labels (skipped by default)
# =========================================================================

_REAL = [p for p in os.environ.get("MEDA_LABEL_ROOTS", "").split(os.pathsep) if p]


@pytest.mark.skipif(not _REAL, reason="set MEDA_LABEL_ROOTS to extracted dataset directories to run")
def test_unit_table_matches_every_real_label():
    checked = 0
    for root in _REAL:
        for product in parser.discover_products(root).products:
            label = parser.parse_label(product.label_path)
            with product.path.open(encoding="utf-8-sig", newline="") as fh:
                header = [h.strip() for h in next(csv.reader(fh))]
            assert normalize.verify_label_units(product.product, product.source_level, header, label) == [], product.path.name
            checked += 1
    assert checked > 0


# =========================================================================
# Validation report regression tests
# =========================================================================

def test_validation_report_atomic_json_and_no_nonstandard_numbers(tmp_path):
    target = tmp_path / "report.json"
    cli._atomic_report(target, {"verdict": "SUCCESS", "count": 3})
    assert json.loads(target.read_text(encoding="utf-8")) == {"verdict": "SUCCESS", "count": 3}
    with pytest.raises(ValueError):
        cli._atomic_report(target, {"bad": float("nan")})
    assert json.loads(target.read_text(encoding="utf-8"))["verdict"] == "SUCCESS"


def test_report_cli_default_name_and_strict_default():
    args = cli.build_parser().parse_args(["--derived", "derived.zip", "--output", "out/meda.jsonl"])
    assert args.strict is True
    assert args.report is None


def test_report_cli_explicit_path_and_non_strict_opt_out():
    args = cli.build_parser().parse_args([
        "--derived", "derived.zip", "--output", "out/meda.jsonl",
        "--report", "out/audit.json", "--no-strict"
    ])
    assert args.report == Path("out/audit.json")
    assert args.strict is False
