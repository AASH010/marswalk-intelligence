
"""Canonical variable mapping and exact-SCLK normalization."""
from __future__ import annotations

from typing import Any, Iterable


# Canonical values remain channel-aware.
# Calibrated channels are not averaged together unless a scientifically
# justified combination is documented.
#
# Scientific naming, units, and sentinel interpretation are intentionally
# unchanged pending review of the authoritative product documentation.

MAPPINGS = {
    ("PS", "derived"): {
        "pressure_pa": ("PRESSURE", "Pa"),
    },
    ("PS", "calibrated"): {
        "pressure_barocap1_pa": ("BAROCAP1_PRESSURE", "Pa"),
        "pressure_barocap2_pa": ("BAROCAP2_PRESSURE", "Pa"),
        "pressure_barocap3_pa": ("BAROCAP3_PRESSURE", "Pa"),
        "pressure_thermocap1_k": ("THERMOCAP1_TEMP", "K"),
        "pressure_thermocap2_k": ("THERMOCAP2_TEMP", "K"),
    },
    ("RHS", "derived"): {
        "relative_humidity_pct": ("LOCAL_RELATIVE_HUMIDITY", "%RH"),
        "humidity_sensor_temp_k": ("HUMIDITY_LOCAL_TEMP", "K"),
        "volume_mixing_ratio": ("VOLUME_MIXING_RATIO", None),
    },
    ("RHS", "calibrated"): {
        "relative_humidity_humicap_a_pct": ("HUMICAP_A_HUMIDITY", "%RH"),
        "relative_humidity_humicap_b_pct": ("HUMICAP_B_HUMIDITY", "%RH"),
        "humidity_sensor_temp_cal_k": ("HUMICAP_TEMP", "K"),
        "humidity_thermocap1_temp_k": ("THERMOCAP1_TEMP", "K"),
        "humidity_thermocap2_temp_k": ("THERMOCAP2_TEMP", "K"),
    },
    ("TIRS", "derived"): {
        "downward_lw_irradiance_derived_w_m2": (
            "DOWNWARD_LW_IRRADIANCE",
            "W/m2",
        ),
        "upward_lw_irradiance_derived_w_m2": (
            "UPWARD_LW_IRRADIANCE",
            "W/m2",
        ),
    },
    ("TIRS", "calibrated"): {
        "downward_lw_irradiance_cal_w_m2": (
            "DOWNWARD_LW_IRRADIANCE",
            "W/m2",
        ),
        "air_temp_k": ("AIR_TEMP", "K"),
        "upward_sw_irradiance_w_m2": ("UPWARD_SW_IRRADIANCE", "W/m2"),
        "upward_lw_irradiance_cal_w_m2": (
            "UPWARD_LW_IRRADIANCE",
            "W/m2",
        ),
        "ground_brightness_temp_k": ("GROUND_TEMP", "K"),
    },
    ("WS", "derived"): {
        "horizontal_wind_speed_m_s": ("HORIZONTAL_WIND_SPEED", "m/s"),
        "vertical_wind_speed_m_s": ("VERTICAL_WIND_SPEED", "m/s"),
        "wind_direction_deg": ("WIND_DIRECTION", "deg"),
    },
    ("WS", "calibrated"): {
        "boom1_horizontal_wind_speed_m_s": (
            "BOOM1_HORIZONTAL_WIND_SPEED",
            "m/s",
        ),
        "boom1_vertical_wind_speed_m_s": (
            "BOOM1_VERTICAL_WIND_SPEED",
            "m/s",
        ),
        "boom1_wind_direction_deg": ("BOOM1_WIND_DIRECTION", "deg"),
        "boom2_horizontal_wind_speed_m_s": (
            "BOOM2_HORIZONTAL_WIND_SPEED",
            "m/s",
        ),
        "boom2_vertical_wind_speed_m_s": (
            "BOOM2_VERTICAL_WIND_SPEED",
            "m/s",
        ),
        "boom2_wind_direction_deg": ("BOOM2_WIND_DIRECTION", "deg"),
    },
    ("ANC", "derived"): {
        "solar_longitude_deg": ("SOLAR_LONGITUDE_ANGLE", "deg"),
        "solar_zenith_angle_deg": ("SOLAR_ZENITHAL_ANGLE", "deg"),
        "rover_position_x_m": ("ROVER_POSITION_X", "m"),
        "rover_position_y_m": ("ROVER_POSITION_Y", "m"),
        "rover_position_z_m": ("ROVER_POSITION_Z", "m"),
        "rover_velocity_m_s": ("ROVER_VELOCITY", "m/s"),
        "rover_pitch_deg": ("ROVER_PITCH", "deg"),
        "rover_yaw_deg": ("ROVER_YAW", "deg"),
        "rover_roll_deg": ("ROVER_ROLL", "deg"),
        "masthead_azimuth_deg": ("MASTHEAD_AZIMUTH", "deg"),
        "masthead_elevation_deg": ("MASTHEAD_ELEVATION", "deg"),
    },
    ("ATS", "calibrated"): {
        **{
            f"ats_local_temp_{i}_k": (f"ATS_LOCAL_TEMP{i}", "K")
            for i in range(1, 6)
        }
    },
}


def _measurement(
    value: Any,
    unit: str | None,
    source: dict,
    raw_name: str,
) -> dict:
    """Build a canonical measurement with source provenance."""
    return {
        "value": value,
        "unit": unit,
        "source_product": source["product"],
        "source_level": source["source_level"],
        "source_file": source["source_file"],
        "source_row": source.get("source_row"),
        "source_channel": raw_name,
    }


def _store_quality_value(
    quality: dict,
    key: str,
    value: Any,
    source: dict,
    sol: int,
    sclk: int,
) -> None:
    """
    Store a quality value without silently accepting conflicting duplicates.

    Repeated identical values are harmless; conflicting values are not.
    """
    if key not in quality:
        quality[key] = value
        return

    if quality[key] != value:
        raise ValueError(
            f"Conflicting quality values for {key!r} at "
            f"Sol={sol}, SCLK={sclk}. "
            f"New source: {source.get('source_file')} "
            f"row {source.get('source_row')}."
        )


def normalize_rows(rows: Iterable[dict]) -> list[dict]:
    """
    Combine only records with identical (Sol, SCLK).

    Duplicate canonical measurements are rejected instead of silently
    overwriting the previous value.
    """
    grouped: dict[tuple[int, int], dict] = {}

    for row in rows:
        sol = row["sol"]
        sclk = row["sclk_s"]
        key = (sol, sclk)

        record = grouped.setdefault(
            key,
            {
                "sol": sol,
                "sclk_s": sclk,
                "lmst": row.get("lmst"),
                "ltst": row.get("ltst"),
                "utc": None,
                "measurements": {},
                "quality": {},
                "sources": [],
            },
        )

        src = row["source"]
        product = src["product"]
        level = src["source_level"]

        record["sources"].append(src)

        # Map only explicitly declared canonical measurements.
        for canonical, (raw_name, unit) in MAPPINGS.get(
            (product, level), {}
        ).items():
            value = row["raw"].get(raw_name)

            if value is None:
                continue

            if canonical in record["measurements"]:
                previous = record["measurements"][canonical]

                raise ValueError(
                    f"Duplicate canonical measurement {canonical!r} "
                    f"at Sol={sol}, SCLK={sclk}. "
                    f"First source: {previous.get('source_file')} "
                    f"row {previous.get('source_row')}; "
                    f"second source: {src.get('source_file')} "
                    f"row {src.get('source_row')}."
                )

            record["measurements"][canonical] = _measurement(
                value=value,
                unit=unit,
                source=src,
                raw_name=raw_name,
            )

        # Preserve selected quality / uncertainty fields verbatim.
        # Including source_level prevents calibrated/derived key collisions.
        for name, value in row["raw"].items():
            if value is None:
                continue

            is_quality_field = (
                "UNCERTAINTY" in name
                or name.endswith("_STABLE")
                or name
                in {
                    "ROVER_STILL",
                    "ROVER_HGA_STILL",
                    "TRANSDUCER",
                    "PRESSURE_MEASUREMENT_MODE",
                    "BOTH_BOOMS_USED_FOR_RETRIEVAL",
                    "RSM_HEAD_OUTSIDE_TIRS_UPWARD_LOOKING_FOV",
                    "WHEEL_OUTSIDE_TIRS_DOWNWARD_LOOKING_FOV",
                    "SUN_OUTSIDE_TIRS_FOV",
                    "ROVER_LOW_TILT",
                    "TIRS_GROUND_FOOTPRINT_NOT_IN_SHADOW",
                    "ROVER_HGA_OFF",
                    "SKYCAM_OFF",
                }
            )

            if is_quality_field:
                quality_key = f"{product}.{level}.{name}"

                _store_quality_value(
                    quality=record["quality"],
                    key=quality_key,
                    value=value,
                    source=src,
                    sol=sol,
                    sclk=sclk,
                )

        # Preserve complete raw channels for these products.
        # Product and source level are both included in the key so that
        # calibrated and derived sources cannot overwrite each other.
        if product in {"RDS", "ENG", "ATS"}:
            raw_channels = record.setdefault("raw_channels", {})
            raw_key = f"{product}.{level}"

            if raw_key in raw_channels:
                raise ValueError(
                    f"Duplicate raw channel source {raw_key!r} at "
                    f"Sol={sol}, SCLK={sclk}. "
                    f"New source: {src.get('source_file')} "
                    f"row {src.get('source_row')}."
                )

            raw_channels[raw_key] = row["raw"]

    return [
        grouped[key]
        for key in sorted(grouped)
    ]


def prefer_derived_measurement(
    record: dict,
    derived_key: str,
    calibrated_keys: list[str],
) -> dict | None:
    """
    Return the derived measurement when present; otherwise use the first
    available calibrated key.

    IMPORTANT:
    This is a source-preference helper only. It does not validate sentinels,
    quality flags, physical ranges, or scientific suitability.
    """
    measurements = record.get("measurements", {})

    if derived_key in measurements:
        return measurements[derived_key]

    for key in calibrated_keys:
        if key in measurements:
            return measurements[key]

    return None
