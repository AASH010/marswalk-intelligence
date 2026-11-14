"""Scientific mapping and exact-time normalisation of MEDA parsed rows.

This module knows *what the columns mean*.  It does not discover files, read
CSV, or touch databases.  Every statement about units below was checked
against the PDS4 XML labels of the supplied Sol 230-239 archives (see
``verify_label_units`` which re-checks them against any label at run time).

INPUT CONTRACT  (``parser.py`` -> here, via ``streaming.py``)
    A parsed source row, see ``parser.py``.  Legacy rows that carry the key
    ``sclk_s`` instead of ``sclk`` are accepted; the value is converted with
    ``canonical_sclk`` (``float`` is refused).

OUTPUT CONTRACT  (canonical record, one per exact (Sol, SCLK))
    {
      "sol": int,
      "sclk_s": str,          # canonical decimal string (NOT a JSON number:
                              # a JSON number would round fractional SCLK)
      "lmst": str|None, "ltst": str|None,
      "utc": None,            # never guessed; needs SPICE SCLK+LSK kernels
      "measurements": {canonical_name: {value, unit, source_unit, unit_basis,
                        validity, source_product, source_level, source_file,
                        source_row, source_channel,
                        [raw_value, validity_reason], [uncertainty]}},
      "quality": {"<PRODUCT>.<level>.<COLUMN>": original value},
      "sources": [source dicts of every contributing row],
      optional keys (present only when non-empty):
      "raw_channels", "raw_channel_issues", "unmapped_channels",
      "missing", "conflicts"
    }

SCIENTIFIC POLICIES (all explicit, none silent)
  * Units are copied from the label (``source_unit``); no conversion is done.
    Rover velocity stays in metre/hour.
  * Calibrated and derived values never replace one another; canonical names
    differ (e.g. ``..._cal_...`` vs ``..._derived_...``) and ``source_level``
    is always recorded.  Multi-channel sensors keep every channel.
  * ``999999999`` / ``9999999999`` in a *numeric measurement channel* is
    ``invalid_sentinel`` (MEDA RDR SIS: "calibrated value could not be
    computed"; the XML labels do not state it).  The original value is kept in
    ``raw_value``; ``value`` becomes ``None``.  Flag columns and time columns
    are never interpreted as sentinels.
  * Quality / flag values are preserved verbatim.  No good/bad meaning is
    invented; the official label wording is in ``FLAG_DEFINITIONS``.
  * Empty cell -> listed under ``missing`` with reason ``empty_cell``; a
    column that is absent from the file -> ``column_absent``.  Zero is a value.
  * Two rows with identical key and identical content are counted as
    ``duplicates_identical``; differing content is kept under ``conflicts``
    (first value stays in ``measurements``; nothing is overwritten silently).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .parser import FieldDef, ProductLabel, canonical_sclk, sclk_sort_parts
from .registry import (
    DEFAULT_SENTINEL_REGISTRY,
    LEGACY_EVIDENCE,
    LEGACY_GLOBAL_SENTINEL_REGISTRY,
    REJECTED,
    UNVERIFIED,
    VERIFIED,
    ChannelDecision,
    SentinelRegistry,
)

__all__ = [
    "NORMALIZER_VERSION",
    "NormalizationError",
    "ChannelSpec",
    "NormalizationStats",
    "CHANNEL_SPECS",
    "MAPPINGS",
    "CHANNEL_UNITS",
    "FLAG_DEFINITIONS",
    "LEGACY_CANONICAL_ALIASES",
    "SENTINEL_VALUES",
    "classify_value",
    "classify_scoped",
    "normalize_group",
    "normalize_rows",
    "verify_label_units",
    "prefer_derived_measurement",
]

NORMALIZER_VERSION = "2.1.0"


class NormalizationError(ValueError):
    """Input rows violate the parser->normaliser contract."""


# --------------------------------------------------------------------------
# Units (label vocabulary -> short symbol).  Symbols only rename; no scaling.
# --------------------------------------------------------------------------

UNIT_SYMBOLS = {
    "pascal": "Pa",
    "kelvin": "K",
    "meter/second": "m/s",
    "meter/hour": "m/h",
    "meter": "m",
    "degree": "deg",
    "watt/m2": "W/m2",
    "ppm": "ppm",
    "amp": "A",
    "volt": "V",
    "ohm": "ohm",
    "second": "s",
}

# Sentinel scope: numeric measurement/uncertainty channels only.
SENTINEL_VALUES = frozenset({999999999, 9999999999})
SENTINEL_BASIS = LEGACY_EVIDENCE  # LEGACY (global) rule; the pipeline uses the scoped registry


@dataclass(frozen=True)
class ChannelSpec:
    canonical: str
    raw_name: str  # CSV column name
    label_unit: str | None  # unit string exactly as in the XML label
    unit: str | None  # short symbol reported in the output
    unit_basis: str  # "label" | "bundle_description" | "not_stated"
    uncertainty: str | None = None  # CSV column holding the 1-sigma/uncertainty
    uncertainty_label_unit: str | None = None
    note: str | None = None


def _s(
    canonical: str,
    raw: str,
    label_unit: str | None,
    unc: str | None = None,
    unc_unit: str | None = None,
    note: str | None = None,
) -> ChannelSpec:
    return ChannelSpec(
        canonical, raw, label_unit, UNIT_SYMBOLS[label_unit] if label_unit else None,
        "label" if label_unit else "not_stated", unc, unc_unit, note,
    )


def _rh(canonical: str, raw: str, unc: str) -> ChannelSpec:
    # The XML label states NO unit for relative humidity.  A "%RH" claim rested on a
    # bundle description that is not part of the inputs, so the unit is NOT adopted:
    # unit=None, unit_verified=False.  The canonical NAME is kept for schema
    # compatibility (it must not be read as proof of the unit).
    return ChannelSpec(
        canonical, raw, None, None, "not_stated", unc, None,
        "label has no <unit>; unit unverified, value kept exactly as given",
    )


_BAROCAP_NOTE = {
    1: "label: NGM-type Barocap (transducer 1) / RSP2M-type (transducer 2)",
    2: "label: RSP2M-type Barocap",
    3: "label: NGM-type Barocap (transducer 1)",
}

CHANNEL_SPECS: dict[tuple[str, str], tuple[ChannelSpec, ...]] = {
    ("PS", "derived"): (
        _s("pressure_pa", "PRESSURE", "pascal", "PRESSURE_UNCERTAINTY", None,
           "label gives <unit>kelvin</unit> for the uncertainty field, which is "
           "inconsistent with a pressure; uncertainty unit left unstated"),
    ),
    ("PS", "calibrated"): (
        *(
            _s(f"pressure_barocap{i}_pa", f"BAROCAP{i}_PRESSURE", "pascal",
               f"BAROCAP{i}_PRESSURE_UNCERTAINTY", "pascal", _BAROCAP_NOTE[i])
            for i in (1, 2, 3)
        ),
        *(
            _s(f"pressure_thermocap{i}_k", f"THERMOCAP{i}_TEMP", "kelvin",
               f"THERMOCAP{i}_TEMP_UNCERTAINTY", "kelvin")
            for i in (1, 2)
        ),
    ),
    ("RHS", "derived"): (
        _rh("relative_humidity_pct", "LOCAL_RELATIVE_HUMIDITY", "LOCAL_RELATIVE_HUMIDITY_UNCERTAINTY"),
        _s("humidity_sensor_temp_k", "HUMIDITY_LOCAL_TEMP", "kelvin",
           "HUMIDITY_LOCAL_TEMP_UNCERTAINTY", "kelvin"),
        _s("volume_mixing_ratio_ppm", "VOLUME_MIXING_RATIO", "ppm",
           "VOLUME_MIXING_RATIO_UNCERTAINTY", "ppm"),
    ),
    ("RHS", "calibrated"): (
        _rh("relative_humidity_humicap_a_pct", "HUMICAP_A_HUMIDITY", "HUMICAP_A_HUMIDITY_UNCERTAINTY"),
        _rh("relative_humidity_humicap_b_pct", "HUMICAP_B_HUMIDITY", "HUMICAP_B_HUMIDITY_UNCERTAINTY"),
        _s("humidity_sensor_temp_cal_k", "HUMICAP_TEMP", "kelvin", "HUMICAP_TEMP_UNCERTAINTY", "kelvin"),
        _s("humidity_thermocap1_temp_k", "THERMOCAP1_TEMP", "kelvin", "THERMOCAP1_TEMP_UNCERTAINTY", "kelvin"),
        _s("humidity_thermocap2_temp_k", "THERMOCAP2_TEMP", "kelvin", "THERMOCAP2_TEMP_UNCERTAINTY", "kelvin"),
    ),
    ("TIRS", "derived"): (
        _s("downward_lw_irradiance_derived_w_m2", "DOWNWARD_LW_IRRADIANCE", "watt/m2",
           "DOWNWARD_LW_IRRADIANCE_UNCERTAINTY", "watt/m2",
           "hemispherical FOV, blackbody model + Stefan-Boltzmann emission"),
        _s("upward_lw_irradiance_derived_w_m2", "UPWARD_LW_IRRADIANCE", "watt/m2",
           "UPWARD_LW_UNCERTAINTY", "watt/m2",
           "hemispherical FOV, Stefan-Boltzmann emission for the ground"),
    ),
    ("TIRS", "calibrated"): (
        _s("downward_lw_irradiance_cal_w_m2", "DOWNWARD_LW_IRRADIANCE", "watt/m2",
           "DOWNWARD_LW_IRRADIANCE_UNCERTAINTY", "watt/m2", "channel bandpass 6.5-30 um"),
        _s("air_temp_k", "AIR_TEMP", "kelvin", "AIR_TEMP_UNCERTAINTY", None,
           "label has no <unit> for AIR_TEMP_UNCERTAINTY"),
        _s("upward_sw_irradiance_w_m2", "UPWARD_SW_IRRADIANCE", "watt/m2",
           "UPWARD_SW_IRRADIANCE_UNCERTAINTY", "watt/m2", "band 0.3-3 um"),
        _s("upward_lw_irradiance_cal_w_m2", "UPWARD_LW_IRRADIANCE", "watt/m2",
           "UPWARD_LW_IRRADIANCE_UNCERTAINTY", "watt/m2", "channel bandpass 6.5-30 um"),
        _s("ground_brightness_temp_k", "GROUND_TEMP", "kelvin",
           "GROUND_TEMP_UNCERTAINTY", "kelvin", "brightness temperature"),
    ),
    ("WS", "derived"): (
        _s("horizontal_wind_speed_m_s", "HORIZONTAL_WIND_SPEED", "meter/second", "HORIZONTAL_WIND_SPEED_UNCERTAINTY"),
        _s("vertical_wind_speed_m_s", "VERTICAL_WIND_SPEED", "meter/second", "VERTICAL_WIND_SPEED_UNCERTAINTY"),
        _s("wind_direction_deg", "WIND_DIRECTION", "degree", "WIND_DIRECTION_UNCERTAINTY"),
    ),
    ("WS", "calibrated"): tuple(
        spec
        for b in (1, 2)
        for spec in (
            _s(f"boom{b}_horizontal_wind_speed_m_s", f"BOOM{b}_HORIZONTAL_WIND_SPEED", "meter/second",
               f"BOOM{b}_HORIZONTAL_WIND_SPEED_UNCERTAINTY"),
            _s(f"boom{b}_vertical_wind_speed_m_s", f"BOOM{b}_VERTICAL_WIND_SPEED", "meter/second",
               f"BOOM{b}_VERTICAL_WIND_SPEED_UNCERTAINTY"),
            _s(f"boom{b}_wind_direction_deg", f"BOOM{b}_WIND_DIRECTION", "degree",
               f"BOOM{b}_WIND_DIRECTION_UNCERTAINTY"),
        )
    ),
    ("ANC", "derived"): (
        _s("solar_azimuth_local_level_deg", "SOLAR_LONGITUDE_ANGLE", "degree", None, None,
           "source name SOLAR_LONGITUDE_ANGLE; label defines it as the angle between +X "
           "and the projection of the Sun on the XY plane of the (180-deg rotated) "
           "M2020 local-level frame, range -180..180. It is NOT areocentric Ls."),
        _s("solar_zenithal_angle_deg", "SOLAR_ZENITHAL_ANGLE", "degree", None, None,
           "label description text says 'elevation' but the definition is the angle "
           "from the frame +Z axis (0=zenith, 180=nadir); value kept exactly as given"),
        _s("rover_position_x_m", "ROVER_POSITION_X", "meter", note="M2020_TOPO frame, relative to landing site"),
        _s("rover_position_y_m", "ROVER_POSITION_Y", "meter", note="M2020_TOPO frame, relative to landing site"),
        _s("rover_position_z_m", "ROVER_POSITION_Z", "meter", note="M2020_TOPO frame, relative to landing site"),
        _s("rover_velocity_m_per_hour", "ROVER_VELOCITY", "meter/hour",
           note="label unit is meter/hour; NOT converted to m/s"),
        _s("rover_pitch_deg", "ROVER_PITCH", "degree"),
        _s("rover_yaw_deg", "ROVER_YAW", "degree"),
        _s("rover_roll_deg", "ROVER_ROLL", "degree"),
        _s("masthead_azimuth_deg", "MASTHEAD_AZIMUTH", "degree"),
        _s("masthead_elevation_deg", "MASTHEAD_ELEVATION", "degree"),
    ),
    ("ATS", "calibrated"): tuple(
        _s(f"ats_local_temp_{i}_k", f"ATS_LOCAL_TEMP{i}", "kelvin") for i in range(1, 6)
    ),
}

# Backwards-compatible view of the old table shape: {canonical: (raw, unit)}.
MAPPINGS: dict[tuple[str, str], dict[str, tuple[str, str | None]]] = {
    key: {s.canonical: (s.raw_name, s.unit) for s in specs}
    for key, specs in CHANNEL_SPECS.items()
}

#: Canonical names that changed vs. the previous package (old -> new) and why.
LEGACY_CANONICAL_ALIASES = {
    "solar_longitude_deg": "solar_azimuth_local_level_deg",  # not Ls
    "solar_zenith_angle_deg": "solar_zenithal_angle_deg",  # source name kept
    "rover_velocity_m_s": "rover_velocity_m_per_hour",  # label unit meter/hour
    "volume_mixing_ratio": "volume_mixing_ratio_ppm",  # label unit ppm
}

#: Products whose full channel set is preserved verbatim under ``raw_channels``.
RAW_CHANNEL_PRODUCTS = frozenset({"RDS", "ENG", "ATS"})

_ENG_UNITS = {
    "ICU_Analog_TH_HK_TM": "kelvin", "ICU_Analog_H_PRT_TEMP_1": "kelvin",
    "ICU_Analog_TIRS_ACQ_TEMP_TM": "kelvin", "ICU_Analog_5V_CURRENT_TM": "amp",
    "ICU_Analog_8V_CURRENT_TM": "amp", "ICU_Analog_11VN_CURRENT_TM": "amp",
    "ICU_Analog_3V3_HK_MUX": "volt", "ICU_Analog_POWER_H_SENSE": None,
    "ICU_Analog_POWER_P1": "volt", "ICU_Analog_R1_CAL_PRT": "ohm",
    "ICU_Analog_POWER_P2": "volt", "ICU_Analog_R2_CAL_PRT": "ohm",
    "ICU_Analog_8V_FRANGI_TM": "volt", "ICU_Analog_MEDA_RDS_TEMP": "kelvin",
    "ICU_Analog_B1_SEC_PWR_TLM": "volt", "ICU_Analog_CAM_PCB_PRT": "kelvin",
    "ICU_Analog_B2_SEC_PWR_TLM": "volt", "ICU_Analog_CAM_CCD_PRT": "kelvin",
    "ICU_Analog_8V_H_PRT_TM": "volt", "ICU_Analog_H_PRT_TEMP_2": "kelvin",
    "ICU_Analog_12V_HTR_CURRENT_TM": "amp", "ICU_Analog_WS1_PRT": "kelvin",
    "ICU_Analog_WS2_PRT": "kelvin",
}

#: Label units of preserved raw channels, keyed by (product, level, CSV column).
#: ``None`` = the label states no unit.  (RDS_*_1 channels are non-optical
#: temperature-corrected currents; all other photodiode channels are labelled
#: watt/m2 in the XML.)
CHANNEL_UNITS: dict[tuple[str, str, str], str | None] = {
    **{("ENG", "calibrated", k): v for k, v in _ENG_UNITS.items()},
    **{("ATS", "calibrated", f"ATS_LOCAL_TEMP{i}"): "kelvin" for i in range(1, 6)},
    **{
        ("RDS", "calibrated", f"RDS_{side}_{n}{suffix}"): ("amp" if (side == "LAT" and n == 1) else "watt/m2")
        for side in ("LAT", "TOP")
        for n in range(1, 9)
        for suffix in ("", "_HG")
    },
    ("RDS", "calibrated", "ROVER_HGA_STILL"): None,
    ("RDS", "calibrated", "ROVER_STILL"): None,
}

#: Official label wording of integer flag columns (verbatim from the XML
#: <description>).  Nothing here says "good" or "bad".
FLAG_DEFINITIONS = {
    "ROVER_STILL": "0 = Rover is moving, 1 = Rover is motionless",
    "ROVER_HGA_STILL": "0 = HGA antenna is moving, 1 = Antenna is motionless",
    "BOTH_BOOMS_USED_FOR_RETRIEVAL": "0 = only one boom was powered on and used to determine wind, 1 = both booms were used to determine wind",
    "BOOM1_ALG_STATE_STABLE": "0 = Boom 1 control algorithm is changing state, 1 = Boom 1 control algorithm not changing state",
    "BOOM2_ALG_STATE_STABLE": "0 = Boom 2 control algorithm is changing state, 1 = Boom 2 control algorithm not changing state",
    "RSM_HEAD_OUTSIDE_TIRS_UPWARD_LOOKING_FOV": "0 = RSM head obstructing the upward looking channels FoV, 1 = RSM head outside the FoV",
    "WHEEL_OUTSIDE_TIRS_DOWNWARD_LOOKING_FOV": "0 = Front right wheel obstructing the TIRS downward looking channels FoV, 1 = Rover front right wheel outside the FoV",
    "SUN_OUTSIDE_TIRS_FOV": "0 = Sun in the TIRS upward looking and UPWARD_SW_IRRADIANCE channels FoV, 1 = Sun outside the FoV",
    "ROVER_LOW_TILT": "0 = high tilt (rover tilt >= +-20 degrees), 1 = low tilt",
    "TIRS_GROUND_FOOTPRINT_NOT_IN_SHADOW": "0 = Ground focused area is shadowed by the rover, 1 = Ground focused area not shadowed by the rover",
    "ROVER_HGA_OFF": "0 = Rover HGA Antenna is on, 1 = Antenna is off",
    "SKYCAM_OFF": "0 = SkyCam is on, 1 = SkyCam is off",
    "TRANSDUCER": "Transducer used: 1 / 2",
    "PRESSURE_MEASUREMENT_MODE": "Pressure measurement mode: nominal / high resolution",
}

_FLAG_NAMES = frozenset(FLAG_DEFINITIONS)


def _is_quality_field(name: str) -> bool:
    return "UNCERTAINTY" in name or name.endswith("_STABLE") or name in _FLAG_NAMES


# --------------------------------------------------------------------------
# Value classification
# --------------------------------------------------------------------------


def classify_value(value: Any) -> tuple[str, str | None]:
    """Return ``(validity, reason)`` for a non-empty numeric channel value.

    validity is one of ``valid``, ``invalid_sentinel``, ``non_numeric``.
    ``None`` (empty cell) is the caller's concern and is not classified here.
    Zero is valid.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "non_numeric", "text found in a numeric channel"
    if value in SENTINEL_VALUES:
        return "invalid_sentinel", SENTINEL_BASIS
    return "valid", None


def classify_scoped(
    registry: SentinelRegistry, product: str, level: str, channel: str, value: Any
) -> tuple[str, str | None, Any]:
    """Scoped classification: ``(validity, reason, rule)``.

    Only a registry rule that matches product + level + channel + value can mark
    a value.  VERIFIED -> ``invalid_sentinel``; UNVERIFIED ->
    ``unverified_sentinel_candidate`` (value is kept by the caller); REJECTED or
    no rule -> ``valid``.  Text in a numeric channel is ``non_numeric``.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "non_numeric", "text found in a numeric channel", None
    rule = registry.match(product, level, channel, value)
    if rule is None or rule.status == REJECTED:
        return "valid", None, rule
    if rule.status == VERIFIED:
        return "invalid_sentinel", rule.evidence, rule
    return (
        "unverified_sentinel_candidate",
        f"UNVERIFIED sentinel rule {rule.rule_id}: value kept as given. {rule.evidence}",
        rule,
    )


# --------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------


@dataclass
class NormalizationStats:
    groups: int = 0
    rows: int = 0
    valid_measurements: Counter = field(default_factory=Counter)
    invalid_measurements: Counter = field(default_factory=Counter)  # "name|validity"
    missing: Counter = field(default_factory=Counter)  # "name|reason"
    duplicates_identical: int = 0
    conflicts: Counter = field(default_factory=Counter)  # kind
    unmapped_columns: Counter = field(default_factory=Counter)  # "PRODUCT.level.COLUMN"
    raw_channel_sentinels: Counter = field(default_factory=Counter)
    # --- accounting (every non-empty mapped cell ends in exactly one bucket) ---
    mapped_cells_seen: int = 0
    cells_empty: int = 0
    measurements_created: int = 0
    cells_duplicate_identical: int = 0
    cells_duplicate_conflict: int = 0
    cells_quarantined: int = 0
    duplicate_identity_rows: int = 0
    seen_by_file: Counter = field(default_factory=Counter)
    created_by_file: Counter = field(default_factory=Counter)
    quarantined_by_file: Counter = field(default_factory=Counter)
    dup_identical_by_file: Counter = field(default_factory=Counter)
    dup_conflict_by_file: Counter = field(default_factory=Counter)
    rows_by_file: Counter = field(default_factory=Counter)
    sentinel_hits: Counter = field(default_factory=Counter)  # "rule_id|STATUS"
    quarantined_channels: Counter = field(default_factory=Counter)  # "source_file|canonical"
    unit_unverified_measurements: Counter = field(default_factory=Counter)  # canonical

    def as_dict(self) -> dict:
        return {
            "mapped_cells_seen": self.mapped_cells_seen,
            "cells_empty": self.cells_empty,
            "measurements_created": self.measurements_created,
            "cells_duplicate_identical": self.cells_duplicate_identical,
            "cells_duplicate_conflict": self.cells_duplicate_conflict,
            "cells_quarantined": self.cells_quarantined,
            "duplicate_identity_rows": self.duplicate_identity_rows,
            "sentinel_hits": dict(sorted(self.sentinel_hits.items())),
            "unit_unverified_measurements": dict(sorted(self.unit_unverified_measurements.items())),
            "groups": self.groups,
            "rows": self.rows,
            "valid_measurements": dict(sorted(self.valid_measurements.items())),
            "invalid_measurements": dict(sorted(self.invalid_measurements.items())),
            "missing": dict(sorted(self.missing.items())),
            "duplicates_identical": self.duplicates_identical,
            "conflicts": dict(sorted(self.conflicts.items())),
            "unmapped_columns": dict(sorted(self.unmapped_columns.items())),
            "raw_channel_sentinels": dict(sorted(self.raw_channel_sentinels.items())),
        }


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------


def _row_identity(row: Mapping[str, Any]) -> tuple[int, str]:
    try:
        sol = row["sol"]
        raw_sclk = row["sclk"] if "sclk" in row else row["sclk_s"]
    except KeyError as exc:
        raise NormalizationError(f"parsed row lacks required key {exc}") from exc
    if isinstance(sol, bool) or not isinstance(sol, int):
        raise NormalizationError(f"sol must be int, got {sol!r}")
    try:
        return sol, canonical_sclk(raw_sclk)
    except (TypeError, ValueError) as exc:
        raise NormalizationError(f"bad SCLK {raw_sclk!r}: {exc}") from exc


def _row_order(row: Mapping[str, Any]) -> tuple:
    src = row.get("source") or {}
    line = src.get("source_row")
    return (
        str(src.get("source_level")),
        str(src.get("product")),
        str(src.get("source_file")),
        -1 if line is None else line,
    )


def _source_ref(src: Mapping[str, Any]) -> dict:
    return {"source_file": src.get("source_file"), "source_row": src.get("source_row")}


def _build_measurement(
    spec: ChannelSpec,
    raw: Mapping[str, Any],
    value: Any,
    src: Mapping[str, Any],
    registry: SentinelRegistry,
    stats: NormalizationStats | None = None,
) -> dict:
    product, level = src["product"], src["source_level"]
    validity, reason, rule = classify_scoped(registry, product, level, spec.raw_name, value)
    keep_value = validity in ("valid", "unverified_sentinel_candidate")
    measurement: dict[str, Any] = {
        "value": value if keep_value else None,
        "unit": spec.unit,
        "source_unit": spec.label_unit,
        "unit_basis": spec.unit_basis,
        "validity": validity,
        "source_product": product,
        "source_level": level,
        "source_file": src["source_file"],
        "source_row": src.get("source_row"),
        "source_channel": spec.raw_name,
    }
    if spec.unit is None:
        measurement["unit_verified"] = False
        if stats is not None:
            stats.unit_unverified_measurements[spec.canonical] += 1
    if validity != "valid":
        measurement["raw_value"] = value
        measurement["validity_reason"] = reason
    if rule is not None and stats is not None and validity != "valid":
        stats.sentinel_hits[f"{rule.rule_id}|{rule.status}"] += 1
    if spec.uncertainty is not None and raw.get(spec.uncertainty) is not None:
        u_value = raw[spec.uncertainty]
        u_validity, _, u_rule = classify_scoped(registry, product, level, spec.uncertainty, u_value)
        u_keep = u_validity in ("valid", "unverified_sentinel_candidate")
        measurement["uncertainty"] = {
            "value": u_value if u_keep else None,
            "unit": UNIT_SYMBOLS.get(spec.uncertainty_label_unit) if spec.uncertainty_label_unit else None,
            "source_channel": spec.uncertainty,
            "validity": u_validity,
            **({} if u_validity == "valid" else {"raw_value": u_value}),
        }
        if u_rule is not None and stats is not None and u_validity != "valid":
            stats.sentinel_hits[f"{u_rule.rule_id}|{u_rule.status}"] += 1
    return measurement


def normalize_group(
    rows: Sequence[Mapping[str, Any]],
    stats: NormalizationStats | None = None,
    *,
    sentinel_registry: SentinelRegistry | None = None,
    channel_policy: Mapping[tuple[str, str], ChannelDecision] | None = None,
) -> dict:
    """Build ONE canonical record from rows that share an exact (Sol, SCLK).

    ``sentinel_registry`` defaults to the scoped ``DEFAULT_SENTINEL_REGISTRY``
    (pass ``LEGACY_GLOBAL_SENTINEL_REGISTRY`` to reproduce the pre-2.1 global rule).
    ``channel_policy`` maps ``(source_file, source_channel)`` to a
    ``ChannelDecision``; an ``exclude`` decision moves the raw value to
    ``quarantined_channels`` instead of ``measurements`` (never silently dropped).

    Raises ``NormalizationError`` if the rows do not share that identity.
    Row order only decides which of two *conflicting* duplicates is listed
    first; callers (``streaming.py``) provide a deterministic order.
    """
    rows = list(rows)
    if not rows:
        raise NormalizationError("empty group")
    # Deterministic order independent of arrival order (stable for equal keys).
    rows.sort(key=_row_order)
    stats = stats if stats is not None else NormalizationStats()
    registry = sentinel_registry if sentinel_registry is not None else DEFAULT_SENTINEL_REGISTRY
    policy = channel_policy or {}
    quarantined_ch: dict[str, dict] = {}
    per_source = Counter()
    sol, sclk = _row_identity(rows[0])

    record: dict[str, Any] = {
        "sol": sol,
        "sclk_s": sclk,
        "lmst": None,
        "ltst": None,
        "utc": None,
        "measurements": {},
        "quality": {},
        "sources": [],
    }
    measurements = record["measurements"]
    missing: dict[str, str] = {}
    conflicts: list[dict] = []
    raw_channels: dict[str, dict] = {}
    raw_issues: dict[str, dict] = {}
    unmapped: dict[str, dict] = {}
    stats.groups += 1

    for row in rows:
        row_sol, row_sclk = _row_identity(row)
        if (row_sol, row_sclk) != (sol, sclk):
            raise NormalizationError(
                f"rows in one group must share (sol, sclk): {(sol, sclk)} vs {(row_sol, row_sclk)}"
            )
        stats.rows += 1
        src = dict(row["source"])
        product, level = src["product"], src["source_level"]
        stats.rows_by_file[src["source_file"]] += 1
        per_source[(product, level, src["source_file"])] += 1
        if per_source[(product, level, src["source_file"])] > 1:
            stats.duplicate_identity_rows += 1
        raw = row.get("raw", {})
        record["sources"].append(src)

        for fld in ("lmst", "ltst"):
            incoming = row.get(fld)
            if incoming is None:
                continue
            if record[fld] is None:
                record[fld] = incoming
            elif record[fld] != incoming:
                stats.conflicts["time_metadata"] += 1
                conflicts.append(
                    {
                        "kind": "time_metadata",
                        "field": fld,
                        "kept": record[fld],
                        "other": incoming,
                        "other_source": _source_ref(src),
                    }
                )

        specs = CHANNEL_SPECS.get((product, level), ())
        mapped_names: set[str] = set()
        for spec in specs:
            mapped_names.add(spec.raw_name)
            if spec.uncertainty:
                mapped_names.add(spec.uncertainty)
            if spec.raw_name not in raw:
                missing.setdefault(spec.canonical, "column_absent")
                continue
            value = raw[spec.raw_name]
            if value is None:
                missing.setdefault(spec.canonical, "empty_cell")
                stats.cells_empty += 1
                continue

            stats.mapped_cells_seen += 1
            stats.seen_by_file[src["source_file"]] += 1
            decision = policy.get((src["source_file"], spec.raw_name))
            if decision is not None and decision.action == "exclude":
                stats.cells_quarantined += 1
                stats.quarantined_by_file[src["source_file"]] += 1
                stats.quarantined_channels[f"{src['source_file']}|{spec.canonical}"] += 1
                quarantined_ch.setdefault(spec.canonical, {
                    "source_channel": spec.raw_name,
                    "raw_value": value,
                    "reason": decision.reason or "channel isolated by label verification",
                    "channel_status": decision.final_status,
                    **_source_ref(src),
                })
                continue

            old = measurements.get(spec.canonical)
            if old is None:
                measurements[spec.canonical] = _build_measurement(spec, raw, value, src, registry, stats)
                stats.measurements_created += 1
                stats.created_by_file[src["source_file"]] += 1
                continue
            new = _build_measurement(spec, raw, value, src, registry, None)
            same = (
                old["validity"] == new["validity"]
                and old.get("value") == new.get("value")
                and old.get("raw_value") == new.get("raw_value")
            )
            if same:
                stats.duplicates_identical += 1
                stats.cells_duplicate_identical += 1
                stats.dup_identical_by_file[src["source_file"]] += 1
            else:
                stats.conflicts["duplicate_measurement"] += 1
                stats.cells_duplicate_conflict += 1
                stats.dup_conflict_by_file[src["source_file"]] += 1
                conflicts.append(
                    {
                        "kind": "duplicate_measurement",
                        "key": spec.canonical,
                        "kept": {"value": old.get("raw_value", old["value"]), **_source_ref(old)},
                        "other": {"value": value, **_source_ref(src)},
                    }
                )

        # Original flags / uncertainties, verbatim, per product+level.
        for name, value in raw.items():
            if value is None or not _is_quality_field(name):
                continue
            key = f"{product}.{level}.{name}"
            if key not in record["quality"]:
                record["quality"][key] = value
            elif record["quality"][key] != value:
                stats.conflicts["quality"] += 1
                conflicts.append(
                    {
                        "kind": "quality",
                        "key": key,
                        "kept": record["quality"][key],
                        "other": value,
                        "other_source": _source_ref(src),
                    }
                )

        pl_key = f"{product}.{level}"
        if product in RAW_CHANNEL_PRODUCTS:
            if pl_key in raw_channels:
                if raw_channels[pl_key] != dict(raw):
                    stats.conflicts["raw_channels"] += 1
                    conflicts.append(
                        {
                            "kind": "raw_channels",
                            "key": pl_key,
                            "other_source": _source_ref(src),
                        }
                    )
                else:
                    stats.duplicates_identical += 1
            else:
                raw_channels[pl_key] = dict(raw)
                for name, value in raw.items():
                    if value is not None and not _is_quality_field(name):
                        validity, _, _r = classify_scoped(registry, product, level, name, value)
                        if validity != "valid":
                            raw_issues.setdefault(pl_key, {})[name] = validity
                            stats.raw_channel_sentinels[f"{pl_key}.{name}|{validity}"] += 1
        else:
            for name, value in raw.items():
                if name in mapped_names or _is_quality_field(name) or value is None:
                    continue
                unmapped.setdefault(pl_key, {})[name] = value
                stats.unmapped_columns[f"{pl_key}.{name}"] += 1

    for name, meas in measurements.items():
        if meas["validity"] == "valid":
            stats.valid_measurements[name] += 1
        else:
            stats.invalid_measurements[f"{name}|{meas['validity']}"] += 1
    for name, reason in missing.items():
        if name not in measurements:
            stats.missing[f"{name}|{reason}"] += 1
    record_missing = {k: v for k, v in missing.items() if k not in measurements}

    if raw_channels:
        record["raw_channels"] = raw_channels
    if raw_issues:
        record["raw_channel_issues"] = raw_issues
    if unmapped:
        record["unmapped_channels"] = unmapped
    if record_missing:
        record["missing"] = record_missing
    if conflicts:
        record["conflicts"] = conflicts
    if quarantined_ch:
        record["quarantined_channels"] = quarantined_ch
    return record


def normalize_rows(
    rows: Iterable[Mapping[str, Any]],
    stats: NormalizationStats | None = None,
) -> list[dict]:
    """Group in memory by exact (Sol, canonical SCLK) and normalise.

    Convenience for small inputs/tests.  The pipeline itself uses
    ``streaming.stream_normalize`` which groups on disk.
    """
    grouped: dict[tuple[int, str], list[Mapping[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(_row_identity(row), []).append(row)
    ordered = sorted(grouped, key=lambda k: (k[0], sclk_sort_parts(k[1])))
    stats = stats if stats is not None else NormalizationStats()
    return [normalize_group(grouped[key], stats) for key in ordered]


# --------------------------------------------------------------------------
# Run-time check of the unit table against real labels
# --------------------------------------------------------------------------


def verify_label_units(
    product: str,
    level: str,
    csv_columns: Sequence[str],
    label: ProductLabel,
) -> list[str]:
    """Compare this module's unit table with one product's XML label.

    Returns human-readable discrepancies (empty list = fully consistent).
    Columns are matched to label fields by position.
    """
    issues: list[str] = []
    by_col: Mapping[str, FieldDef] = label.fields_for_columns(csv_columns)
    if not by_col:
        return [f"{product}.{level}: column count differs from label ({label.path.name})"]

    for spec in CHANNEL_SPECS.get((product, level), ()):
        field_def = by_col.get(spec.raw_name)
        if field_def is None:
            continue
        if field_def.unit != spec.label_unit:
            issues.append(
                f"{product}.{level}.{spec.raw_name}: label unit {field_def.unit!r} "
                f"!= mapping {spec.label_unit!r}"
            )
        if field_def.data_type not in ("ASCII_Real", "ASCII_Integer"):
            issues.append(f"{product}.{level}.{spec.raw_name}: non-numeric label type {field_def.data_type}")
        if spec.uncertainty and spec.uncertainty_label_unit is not None:
            unc_def = by_col.get(spec.uncertainty)
            if unc_def is not None and unc_def.unit != spec.uncertainty_label_unit:
                issues.append(
                    f"{product}.{level}.{spec.uncertainty}: label unit {unc_def.unit!r} "
                    f"!= mapping {spec.uncertainty_label_unit!r}"
                )
    for column, field_def in by_col.items():
        key = (product, level, column)
        if key in CHANNEL_UNITS and field_def.unit != CHANNEL_UNITS[key]:
            issues.append(
                f"{product}.{level}.{column}: label unit {field_def.unit!r} "
                f"!= table {CHANNEL_UNITS[key]!r}"
            )
    return issues


# --------------------------------------------------------------------------
# Explicit, caller-controlled preference helper (not used by the pipeline)
# --------------------------------------------------------------------------


def prefer_derived_measurement(
    record: Mapping[str, Any],
    derived_key: str,
    calibrated_keys: Sequence[str],
) -> dict | None:
    """Return the first *valid* measurement in the order given by the caller.

    Order: ``derived_key`` first, then ``calibrated_keys``.  Nothing is
    modified, nothing is averaged, and invalid (sentinel / non-numeric)
    measurements are skipped.  This is a *selection policy only*: it does not
    claim derived is scientifically better, and the original measurements
    remain in ``record["measurements"]``.
    """
    measurements = record.get("measurements", {})
    for key in (derived_key, *calibrated_keys):
        candidate = measurements.get(key)
        if candidate is not None and candidate.get("validity", "valid") == "valid":
            return candidate
    return None
