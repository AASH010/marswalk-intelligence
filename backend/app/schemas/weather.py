"""
MarsWalk Intelligence — MEDA Canonical Weather Schema
=====================================================

Schema contract for normalized MEDA weather records.

Baseline used to prepare this schema:
- Dataset: meda_normalized_test.jsonl
- QA report: qa_test_reports/meda_variable_qa_report.json
- Records: 471,708
- Sol coverage: 230–239
- Discovered measurement variables: 41

Important:
- This module describes the normalized data contract and observed source metadata.
- QA min/max/mean values are NOT physical validity limits.
- Missing measurements are permitted because source products/channels have
  different coverage.
- 999999999 is retained as a candidate sentinel only for the two variables
  identified by the approved QA report. It must not be treated as definitive
  until confirmed against the authoritative MEDA product documentation.
- A null unit means the approved QA report did not provide unit metadata.
"""

from __future__ import annotations

from typing import Any


SCHEMA_NAME = "MarsWalk Canonical MEDA Weather Schema"
SCHEMA_VERSION = "1.1.0"
SCHEMA_STATUS = "baseline-aligned; scientific review items explicitly flagged"

DATASET_BASELINE = {
    "dataset_file": "meda_normalized_test.jsonl",
    "qa_report": "qa_test_reports/meda_variable_qa_report.json",
    "record_count": 471_708,
    "sol_start": 230,
    "sol_end": 239,
    "sol_count": 10,
    "measurement_variable_count": 41,
    "invalid_json_lines": 0,
    "non_object_records": 0,
    "missing_sol_records": 0,
    "missing_measurements_object": 0,
    "invalid_measurements_object": 0,
}

# The normalized record envelope. `sol`, `sclk`, `measurements`, and `quality`
# are the canonical concepts used by this project. Additional provenance keys
# may be retained by the normalizer without changing measurement semantics.
RECORD_SCHEMA = {
    "sol": {
        "type": "integer",
        "required": True,
        "description": "Mars solar day (Sol).",
    },
    "sclk": {
        "type": "number_or_string",
        "required": True,
        "description": "Spacecraft clock value used to identify the observation time.",
    },
    "measurements": {
        "type": "object",
        "required": True,
        "description": "Canonical measurement-name to measurement-object mapping.",
    },
    "quality": {
        "type": "object",
        "required": False,
        "description": (
            "Preserved source quality/uncertainty values. Values are not "
            "interpreted as good/bad by this schema."
        ),
    },
}

# Each measurement entry records the observed source metadata from the
# approved QA report. `description` is intentionally conservative and does
# not replace the authoritative PDS/MEDA channel definition.
#
# Fields:
#   unit          : unit string observed in normalized metadata, or None
#   source_level  : calibrated / derived
#   source_product: MEDA product family
#   source_channel: original MEDA channel identifier
#   value_type    : normalized numeric value
#   nullable      : whether explicit null was observed in the QA baseline
#   sentinel      : candidate sentinel policy, when detected by QA
#   review        : scientific interpretation requiring source-document review

MEASUREMENT_SCHEMA = {
    "air_temp_k": {
        "unit": "K", "source_level": "calibrated", "source_product": "TIRS",
        "source_channel": "AIR_TEMP", "value_type": "number", "nullable": False,
    },
    "ats_local_temp_1_k": {
        "unit": "K", "source_level": "calibrated", "source_product": "ATS",
        "source_channel": "ATS_LOCAL_TEMP1", "value_type": "number", "nullable": False,
    },
    "ats_local_temp_2_k": {
        "unit": "K", "source_level": "calibrated", "source_product": "ATS",
        "source_channel": "ATS_LOCAL_TEMP2", "value_type": "number", "nullable": False,
    },
    "ats_local_temp_3_k": {
        "unit": "K", "source_level": "calibrated", "source_product": "ATS",
        "source_channel": "ATS_LOCAL_TEMP3", "value_type": "number", "nullable": False,
    },
    "ats_local_temp_4_k": {
        "unit": "K", "source_level": "calibrated", "source_product": "ATS",
        "source_channel": "ATS_LOCAL_TEMP4", "value_type": "number", "nullable": False,
    },
    "ats_local_temp_5_k": {
        "unit": "K", "source_level": "calibrated", "source_product": "ATS",
        "source_channel": "ATS_LOCAL_TEMP5", "value_type": "number", "nullable": False,
    },
    "boom1_horizontal_wind_speed_m_s": {
        "unit": "m/s", "source_level": "calibrated", "source_product": "WS",
        "source_channel": "BOOM1_HORIZONTAL_WIND_SPEED",
        "value_type": "number", "nullable": False,
    },
    "boom1_wind_direction_deg": {
        "unit": "deg", "source_level": "calibrated", "source_product": "WS",
        "source_channel": "BOOM1_WIND_DIRECTION",
        "value_type": "number", "nullable": False,
    },
    "boom2_horizontal_wind_speed_m_s": {
        "unit": "m/s", "source_level": "calibrated", "source_product": "WS",
        "source_channel": "BOOM2_HORIZONTAL_WIND_SPEED",
        "value_type": "number", "nullable": False,
    },
    "boom2_wind_direction_deg": {
        "unit": "deg", "source_level": "calibrated", "source_product": "WS",
        "source_channel": "BOOM2_WIND_DIRECTION",
        "value_type": "number", "nullable": False,
    },
    "downward_lw_irradiance_cal_w_m2": {
        "unit": "W/m2", "source_level": "calibrated", "source_product": "TIRS",
        "source_channel": "DOWNWARD_LW_IRRADIANCE",
        "value_type": "number", "nullable": False,
    },
    "downward_lw_irradiance_derived_w_m2": {
        "unit": "W/m2", "source_level": "derived", "source_product": "TIRS",
        "source_channel": "DOWNWARD_LW_IRRADIANCE",
        "value_type": "number", "nullable": False,
    },
    "ground_brightness_temp_k": {
        "unit": "K", "source_level": "calibrated", "source_product": "TIRS",
        "source_channel": "GROUND_TEMP", "value_type": "number", "nullable": False,
    },
    "horizontal_wind_speed_m_s": {
        "unit": "m/s", "source_level": "derived", "source_product": "WS",
        "source_channel": "HORIZONTAL_WIND_SPEED", "value_type": "number",
        "nullable": False,
        "sentinel": {
            "value": 999999999,
            "status": "candidate",
            "qa_count": 9676,
            "handling": "exclude from numeric statistics; preserve provenance",
        },
    },
    "humidity_sensor_temp_cal_k": {
        "unit": "K", "source_level": "calibrated", "source_product": "RHS",
        "source_channel": "HUMICAP_TEMP", "value_type": "number", "nullable": False,
    },
    "humidity_sensor_temp_k": {
        "unit": "K", "source_level": "derived", "source_product": "RHS",
        "source_channel": "HUMIDITY_LOCAL_TEMP",
        "value_type": "number", "nullable": False,
    },
    "humidity_thermocap1_temp_k": {
        "unit": "K", "source_level": "calibrated", "source_product": "RHS",
        "source_channel": "THERMOCAP1_TEMP", "value_type": "number", "nullable": False,
    },
    "humidity_thermocap2_temp_k": {
        "unit": "K", "source_level": "calibrated", "source_product": "RHS",
        "source_channel": "THERMOCAP2_TEMP", "value_type": "number", "nullable": False,
    },
    "pressure_barocap1_pa": {
        "unit": "Pa", "source_level": "calibrated", "source_product": "PS",
        "source_channel": "BAROCAP1_PRESSURE", "value_type": "number", "nullable": False,
    },
    "pressure_barocap2_pa": {
        "unit": "Pa", "source_level": "calibrated", "source_product": "PS",
        "source_channel": "BAROCAP2_PRESSURE", "value_type": "number", "nullable": False,
    },
    "pressure_barocap3_pa": {
        "unit": "Pa", "source_level": "calibrated", "source_product": "PS",
        "source_channel": "BAROCAP3_PRESSURE", "value_type": "number", "nullable": False,
    },
    "pressure_pa": {
        "unit": "Pa", "source_level": "derived", "source_product": "PS",
        "source_channel": "PRESSURE", "value_type": "number", "nullable": False,
    },
    "pressure_thermocap1_k": {
        "unit": "K", "source_level": "calibrated", "source_product": "PS",
        "source_channel": "THERMOCAP1_TEMP", "value_type": "number", "nullable": False,
    },
    "pressure_thermocap2_k": {
        "unit": "K", "source_level": "calibrated", "source_product": "PS",
        "source_channel": "THERMOCAP2_TEMP", "value_type": "number", "nullable": False,
    },
    "relative_humidity_humicap_a_pct": {
        "unit": "%RH", "source_level": "calibrated", "source_product": "RHS",
        "source_channel": "HUMICAP_A_HUMIDITY",
        "value_type": "number", "nullable": False,
    },
    "relative_humidity_humicap_b_pct": {
        "unit": "%RH", "source_level": "calibrated", "source_product": "RHS",
        "source_channel": "HUMICAP_B_HUMIDITY",
        "value_type": "number", "nullable": False,
    },
    "relative_humidity_pct": {
        "unit": "%RH", "source_level": "derived", "source_product": "RHS",
        "source_channel": "LOCAL_RELATIVE_HUMIDITY",
        "value_type": "number", "nullable": False,
    },
    "rover_pitch_deg": {
        "unit": "deg", "source_level": "derived", "source_product": "ANC",
        "source_channel": "ROVER_PITCH", "value_type": "number", "nullable": False,
    },
    "rover_position_x_m": {
        "unit": "m", "source_level": "derived", "source_product": "ANC",
        "source_channel": "ROVER_POSITION_X", "value_type": "number", "nullable": False,
    },
    "rover_position_y_m": {
        "unit": "m", "source_level": "derived", "source_product": "ANC",
        "source_channel": "ROVER_POSITION_Y", "value_type": "number", "nullable": False,
    },
    "rover_position_z_m": {
        "unit": "m", "source_level": "derived", "source_product": "ANC",
        "source_channel": "ROVER_POSITION_Z", "value_type": "number", "nullable": False,
    },
    "rover_roll_deg": {
        "unit": "deg", "source_level": "derived", "source_product": "ANC",
        "source_channel": "ROVER_ROLL", "value_type": "number", "nullable": False,
    },
    "rover_velocity_m_s": {
        "unit": "m/s", "source_level": "derived", "source_product": "ANC",
        "source_channel": "ROVER_VELOCITY", "value_type": "number", "nullable": False,
        "review": (
            "QA reports the source unit as m/s, but the observed maximum "
            "128.06 is suspicious for rover translational velocity. Confirm "
            "the source XML definition/conversion before scientific use."
        ),
    },
    "rover_yaw_deg": {
        "unit": "deg", "source_level": "derived", "source_product": "ANC",
        "source_channel": "ROVER_YAW", "value_type": "number", "nullable": False,
    },
    "solar_longitude_deg": {
        "unit": "deg", "source_level": "derived", "source_product": "ANC",
        "source_channel": "SOLAR_LONGITUDE_ANGLE",
        "value_type": "number", "nullable": False,
        "review": (
            "Channel identity and observed range (-77.27 to 77.30) require "
            "verification against the authoritative ANC definition. Do not "
            "assume this is conventional areocentric solar longitude Ls."
        ),
    },
    "solar_zenith_angle_deg": {
        "unit": "deg", "source_level": "derived", "source_product": "ANC",
        "source_channel": "SOLAR_ZENITHAL_ANGLE",
        "value_type": "number", "nullable": False,
    },
    "upward_lw_irradiance_cal_w_m2": {
        "unit": "W/m2", "source_level": "calibrated", "source_product": "TIRS",
        "source_channel": "UPWARD_LW_IRRADIANCE",
        "value_type": "number", "nullable": False,
    },
    "upward_lw_irradiance_derived_w_m2": {
        "unit": "W/m2", "source_level": "derived", "source_product": "TIRS",
        "source_channel": "UPWARD_LW_IRRADIANCE",
        "value_type": "number", "nullable": False,
    },
    "upward_sw_irradiance_w_m2": {
        "unit": "W/m2", "source_level": "calibrated", "source_product": "TIRS",
        "source_channel": "UPWARD_SW_IRRADIANCE",
        "value_type": "number", "nullable": False,
    },
    "volume_mixing_ratio": {
        "unit": None, "source_level": "derived", "source_product": "RHS",
        "source_channel": "VOLUME_MIXING_RATIO",
        "value_type": "number", "nullable": False,
        "review": (
            "The approved QA report found no unit metadata for this variable. "
            "Do not assign a unit until confirmed from the source product label/XML."
        ),
    },
    "wind_direction_deg": {
        "unit": "deg", "source_level": "derived", "source_product": "WS",
        "source_channel": "WIND_DIRECTION", "value_type": "number",
        "nullable": False,
        "sentinel": {
            "value": 999999999,
            "status": "candidate",
            "qa_count": 9676,
            "handling": "exclude from numeric statistics; preserve provenance",
        },
    },
}

# Dataset coverage observed by the approved QA report. These counts describe
# the baseline only; they are not schema-level minimum coverage requirements.
SOL_RECORD_COUNTS = {
    230: 48026,
    231: 46396,
    232: 47990,
    233: 46432,
    234: 48007,
    235: 46434,
    236: 41654,
    237: 48459,
    238: 47581,
    239: 50729,
}

QA_POLICY = {
    "missing_measurements": "allowed; preserve as missing, do not impute in normalization",
    "explicit_null": "not observed in the approved QA baseline",
    "non_numeric_measurements": "not observed in the approved QA baseline",
    "non_finite_measurements": "not observed in the approved QA baseline",
    "candidate_sentinel_value": 999999999,
    "candidate_sentinel_variables": (
        "horizontal_wind_speed_m_s",
        "wind_direction_deg",
    ),
    "sentinel_confirmation": "required against authoritative MEDA source documentation",
    "quality_values": "preserve as supplied; do not infer good/bad meaning",
    "physical_range_validation": "not defined by this schema",
    "statistics": "QA descriptive statistics are not physical validity thresholds",
}

SCIENTIFIC_REVIEW_ITEMS = {
    "rover_velocity_m_s": (
        "Verify source unit and conversion; QA observed max is 128.06."
    ),
    "solar_longitude_deg": (
        "Verify semantic definition; source channel is SOLAR_LONGITUDE_ANGLE "
        "and observed values span -77.27 to 77.30."
    ),
    "volume_mixing_ratio": (
        "Verify unit from authoritative source metadata; normalized QA unit is absent."
    ),
    "sentinel_999999999": (
        "Confirm sentinel semantics from source documentation before treating "
        "the candidate marker as definitive."
    ),
}

def get_measurement_spec(name: str) -> dict[str, Any] | None:
    """Return the canonical specification for a measurement, if defined."""
    return MEASUREMENT_SCHEMA.get(name)


def validate_record_structure(record: Any) -> list[str]:
    """
    Validate only the normalized record envelope.

    This intentionally does not apply physical range limits or interpret
    quality flags. Missing individual measurements are allowed.
    """
    errors: list[str] = []

    if not isinstance(record, dict):
        return ["record must be a JSON object"]

    if not isinstance(record.get("sol"), int):
        errors.append("'sol' must be an integer")

    if "sclk" not in record or record["sclk"] is None:
        errors.append("'sclk' is required and must not be null")

    if not isinstance(record.get("measurements"), dict):
        errors.append("'measurements' must be an object")

    quality = record.get("quality")
    if quality is not None and not isinstance(quality, dict):
        errors.append("'quality' must be an object when present")

    return errors


def unknown_measurement_names(record: dict[str, Any]) -> set[str]:
    """Return measurement keys not declared in this schema."""
    measurements = record.get("measurements", {})
    if not isinstance(measurements, dict):
        return set()
    return set(measurements) - set(MEASUREMENT_SCHEMA)


if len(MEASUREMENT_SCHEMA) != DATASET_BASELINE["measurement_variable_count"]:
    raise RuntimeError(
        "Canonical schema variable count does not match approved QA baseline: "
        f"{len(MEASUREMENT_SCHEMA)} != "
        f"{DATASET_BASELINE['measurement_variable_count']}"
    )


if __name__ == "__main__":
    print(f"{SCHEMA_NAME} v{SCHEMA_VERSION}")
    print(f"Measurement variables: {len(MEASUREMENT_SCHEMA)}")
    print(
        "Baseline: "
        f"{DATASET_BASELINE['record_count']:,} records, "
        f"Sols {DATASET_BASELINE['sol_start']}–{DATASET_BASELINE['sol_end']}"
    )
    print(f"Scientific review items: {len(SCIENTIFIC_REVIEW_ITEMS)}")
