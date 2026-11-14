"""MEDA filtering rules: modular, variable-aware, evidence-gated.

Design principles (from the task specification)
-----------------------------------------------
* A rule is ACTIVE only when the supplied Normalizer v2.1 package (source code)
  is sufficient evidence for it.  Every rule carries its ``evidence`` text.
* No scientific threshold is invented.  Range limits, quality-flag meanings and
  sentinel semantics that need authoritative documentation are listed as
  ``PENDING_SCIENTIFIC_DOCUMENTATION`` and are never evaluated by default.
* Such policies can be switched on ONLY through an explicit rules-configuration
  file in which every policy cites its own evidence (non-empty text).  The
  package never fills that evidence in itself.
* Decisions are ``accepted`` / ``rejected`` / ``review_required``.  Only
  ``accepted`` measurements reach the Filtered Dataset.
* Rules judge ONE measurement at a time.  Source selection (several processing
  levels of one physical variable) is a separate, explicit, record-level step.
* Nothing here reads or imports the Normalizer; the Normalizer's contract was
  transcribed from its source and is enforced through the Normalized Schema.

Rule categories requested by the task
-------------------------------------
quality_flag_assessment   FILT-CFG-FLAG-*      (config only; no default policy)
missing_invalid_value     FILT-001, FILT-003, FILT-004
sentinel_handling         FILT-001, FILT-002    (the Normalizer's scoped registry is the authority)
variable_specific         FILT-005, FILT-CFG-RANGE-*
source_selection          SOURCE_SELECTION_CANDIDATES + config ``source_selection``
measurement_acceptance    evaluate_measurement()
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

__all__ = [
    "RULESET_VERSION",
    "RULES_CONFIG_VERSION",
    "Decision",
    "RuleStatus",
    "RuleResult",
    "RuleDefinition",
    "RuleSet",
    "SourcePolicy",
    "RulesConfigError",
    "SOURCE_SELECTION_CANDIDATES",
    "FLAG_WORDING",
    "build_ruleset",
    "load_rules_config",
    "evaluate_measurement",
    "select_sources",
    "get_active_rules",
    "get_all_rules",
]

RULESET_VERSION = "1.0"
RULES_CONFIG_VERSION = "1"


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


class Decision(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    REVIEW_REQUIRED = "review_required"


_SEVERITY = {Decision.ACCEPTED: 0, Decision.REVIEW_REQUIRED: 1, Decision.REJECTED: 2}


class RuleStatus(str, Enum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"  # switched off by the rules configuration (justification recorded)
    PENDING_SCIENTIFIC_DOCUMENTATION = "PENDING_SCIENTIFIC_DOCUMENTATION"


class RulesConfigError(ValueError):
    """The rules-configuration file is invalid or tries to enable an unsupported rule."""


@dataclass(frozen=True)
class RuleResult:
    rule_id: str
    decision: Decision
    reason_code: str
    detail: str


# A rule function returns (triggered, detail) or (triggered, detail, reason_code):
#   True  -> the rule fires, decision = rule.decision_on_trigger
#   False -> applicable, the measurement passes this rule
#   None  -> the rule is not applicable to this measurement
RuleFn = Callable[[str, Mapping[str, Any], Mapping[str, Any]], tuple]


@dataclass
class RuleDefinition:
    rule_id: str
    status: RuleStatus
    category: str
    description: str
    applies_to: str
    decision_on_trigger: Decision
    reason_code: str
    evidence: str
    core: bool = False                 # core rules can never be disabled
    pending_documentation: str = ""    # what is needed before a PENDING rule may be enabled
    justification: str = ""            # filled when status is DISABLED by configuration
    fn: RuleFn | None = field(default=None, repr=False)

    def evaluate(
        self, name: str, measurement: Mapping[str, Any], record: Mapping[str, Any]
    ) -> RuleResult | None:
        if self.status != RuleStatus.ACTIVE or self.fn is None:
            return None
        out = self.fn(name, measurement, record)
        triggered, detail = out[0], out[1]
        if triggered is None:
            return None
        if triggered:
            code = out[2] if len(out) > 2 and out[2] else self.reason_code
            return RuleResult(self.rule_id, self.decision_on_trigger, code, detail)
        return RuleResult(self.rule_id, Decision.ACCEPTED, "passed", detail)

    def describe(self) -> dict:
        d = {
            "rule_id": self.rule_id,
            "status": self.status.value,
            "category": self.category,
            "description": self.description,
            "applies_to": self.applies_to,
            "decision_on_trigger": self.decision_on_trigger.value,
            "reason_code": self.reason_code,
            "evidence": self.evidence,
            "core": self.core,
        }
        if self.pending_documentation:
            d["pending_documentation_required"] = self.pending_documentation
        if self.justification:
            d["disabled_justification"] = self.justification
        return d


# ---------------------------------------------------------------------------
# Core and evidence-backed rules (functions)
# ---------------------------------------------------------------------------

_VALIDITY_VOCABULARY = ("valid", "invalid_sentinel", "unverified_sentinel_candidate", "non_numeric")


def _rule_validity_invalid(name, meas, record):
    """FILT-001: the Normalizer already classified the value as unusable."""
    v = meas.get("validity")
    if v == "invalid_sentinel":
        return True, f"validity=invalid_sentinel; raw_value={meas.get('raw_value')!r}", "invalid_sentinel"
    if v == "non_numeric":
        return True, f"validity=non_numeric; raw_value={meas.get('raw_value')!r}", "non_numeric_value"
    if v == "valid":
        return False, "validity is 'valid'"
    return None, ""


def _rule_validity_review(name, meas, record):
    """FILT-002: validity states that need a human decision."""
    v = meas.get("validity")
    if v == "unverified_sentinel_candidate":
        return True, (
            f"value {meas.get('value')!r} matches an UNVERIFIED sentinel rule of the Normalizer; "
            f"validity_reason={meas.get('validity_reason')!r}"
        ), "unverified_sentinel_candidate"
    if v not in _VALIDITY_VOCABULARY:
        return True, f"validity {v!r} is outside the Normalizer v2.1 vocabulary", "unrecognised_validity"
    return None, ""


def _rule_value_usable(name, meas, record):
    """FILT-003: a 'valid' measurement must carry a finite real number (guard)."""
    if meas.get("validity") != "valid":
        return None, ""
    v = meas.get("value")
    if v is None:
        return True, "validity='valid' but value is null"
    if isinstance(v, bool) or not isinstance(v, (int, float)) or (isinstance(v, float) and not math.isfinite(v)):
        return True, f"validity='valid' but value {v!r} is not a finite number"
    return False, "value is a finite number"


def _rule_duplicate_conflict(name, meas, record):
    """FILT-004: the Normalizer kept the first of two differing duplicates."""
    for c in record.get("conflicts") or ():
        if isinstance(c, Mapping) and c.get("kind") == "duplicate_measurement" and c.get("key") == name:
            return True, (
                "the Normalizer recorded a duplicate_measurement conflict for this key; the value kept here "
                f"differs from {c.get('other')!r}"
            )
    return False, "no duplicate_measurement conflict for this key"


def _rule_unit_unverified(name, meas, record):
    """FILT-005: the unit of this measurement is not established."""
    if meas.get("unit_verified") is False or (meas.get("unit") is None and meas.get("unit_basis") == "not_stated"):
        return True, (
            f"unit is null (source_unit={meas.get('source_unit')!r}, unit_basis={meas.get('unit_basis')!r}); "
            "the Normalizer reports the unit as unverified"
        )
    return False, "unit is established by the label"


# ---------------------------------------------------------------------------
# Rule registry
# ---------------------------------------------------------------------------


def _static_rules() -> list[RuleDefinition]:
    A, P = RuleStatus.ACTIVE, RuleStatus.PENDING_SCIENTIFIC_DOCUMENTATION
    return [
        RuleDefinition(
            "FILT-001", A, "missing_invalid_value",
            "Reject a measurement whose Normalizer validity is 'invalid_sentinel' or 'non_numeric'. "
            "The Normalizer has already nulled the value; nothing usable remains.",
            "all canonical channels", Decision.REJECTED, "normalizer_validity_invalid",
            "normalize.py _build_measurement()/classify_scoped(): value is set to None for these two validity "
            "classes and the original is kept in raw_value. Sentinel scope is decided by the Normalizer's "
            "scoped registry (registry.py); this package does not re-detect or generalise sentinel values.",
            core=True, fn=_rule_validity_invalid,
        ),
        RuleDefinition(
            "FILT-002", A, "sentinel_handling",
            "Send a measurement to review when its validity is 'unverified_sentinel_candidate' (value kept by "
            "the Normalizer because the sentinel rule lacks verifying evidence) or lies outside the v2.1 vocabulary.",
            "all canonical channels", Decision.REVIEW_REQUIRED, "unverified_sentinel_candidate",
            "registry.py: UNVERIFIED rules keep the value and set validity='unverified_sentinel_candidate'; "
            "such a value is neither a confirmed sentinel nor confirmed good.",
            core=True, fn=_rule_validity_review,
        ),
        RuleDefinition(
            "FILT-003", A, "missing_invalid_value",
            "Reject a measurement that claims validity='valid' but has no finite numeric value (malformed-input guard).",
            "all canonical channels", Decision.REJECTED, "valid_but_value_unusable",
            "normalize.py _build_measurement(): a 'valid' measurement always carries the numeric value; zero is a value, "
            "null is never converted to zero.",
            core=True, fn=_rule_value_usable,
        ),
        RuleDefinition(
            "FILT-004", A, "missing_invalid_value",
            "Send a measurement to review when the Normalizer recorded a duplicate_measurement conflict for its "
            "canonical key (two source rows with the same Sol/SCLK disagreed; only the first was kept).",
            "canonical keys listed in record.conflicts[kind=duplicate_measurement]", Decision.REVIEW_REQUIRED,
            "duplicate_measurement_conflict",
            "normalize.py normalize_group(): the first value stays in measurements, nothing is overwritten silently, "
            "and the disagreement is listed under conflicts. Which duplicate is correct cannot be decided here.",
            fn=_rule_duplicate_conflict,
        ),
        RuleDefinition(
            "FILT-005", A, "variable_specific",
            "Send a measurement to review when its unit is unverified (unit null / unit_verified=false). "
            "In Normalizer v2.1 this is exactly the relative-humidity channels "
            "(relative_humidity_pct, relative_humidity_humicap_a_pct, relative_humidity_humicap_b_pct): "
            "the PDS4 label states no <unit> and the '%RH' claim was not adopted.",
            "any measurement with unit null / unit_verified=false", Decision.REVIEW_REQUIRED, "unit_unverified",
            "normalize.py _rh(): 'label has no <unit>; unit unverified, value kept exactly as given'; "
            "_build_measurement() writes unit_verified=false when ChannelSpec.unit is None. The rule is keyed on the "
            "Normalizer's own unit status, not on variable names. It may be disabled through the rules "
            "configuration once an authoritative unit statement exists (a justification is then required).",
            fn=_rule_unit_unverified,
        ),
        # -------- evidence-gated rules: documented, NEVER evaluated by default --------
        RuleDefinition(
            "FILT-P01", P, "variable_specific",
            "Physical range limits per variable (pressure, temperatures, irradiances, wind speed, humidity, ...).",
            "all numeric variables", Decision.REJECTED, "value_outside_documented_range", "NOT AVAILABLE",
            pending_documentation=(
                "An authoritative source (e.g. the MEDA RDR SIS or an instrument paper) stating, per variable, the "
                "physically valid interval and that values outside it are invalid rather than merely unusual. The "
                "supplied Normalizer package contains no such limits, and unusual values must not be discarded. "
                "Enable per variable through 'range_policies' in the rules configuration, citing that source."
            ),
        ),
        RuleDefinition(
            "FILT-P02", P, "variable_specific",
            "Domain check for angular variables (wind direction channels, solar and rover angles).",
            "angle channels", Decision.REJECTED, "angle_outside_documented_domain", "NOT AVAILABLE",
            pending_documentation=(
                "The reference frame and convention of each angle. Conventions differ per channel in the supplied "
                "package: the label defines solar_azimuth_local_level_deg over -180..180, while the wind-direction "
                "channels carry no stated range. A single 0-360 assumption is therefore not supported."
            ),
        ),
        RuleDefinition(
            "FILT-P03", P, "quality_flag_assessment",
            "Quality-flag assessment (ROVER_STILL, ROVER_HGA_STILL, BOOMn_ALG_STATE_STABLE, TIRS field-of-view flags, "
            "ROVER_LOW_TILT, TRANSDUCER, PRESSURE_MEASUREMENT_MODE, ...).",
            "variables affected by each flag", Decision.REVIEW_REQUIRED, "quality_flag_policy", "NOT AVAILABLE",
            pending_documentation=(
                "For each flag: which canonical variables it affects and whether a given flag value makes a "
                "measurement unsuitable. The package supplies only the official label wording (FLAG_WORDING), which "
                "describes states ('rover is moving'), not data quality; no single good/bad mapping may be applied. "
                "In particular the package does not say which TIRS channels are 'upward looking' or 'downward "
                "looking'. Enable per flag through 'flag_policies' in the rules configuration, citing the source."
            ),
        ),
        RuleDefinition(
            "FILT-P04", P, "sentinel_handling",
            "Confirmation of the UNVERIFIED sentinel rules (WS.derived.HORIZONTAL_WIND_SPEED and "
            "WS.derived.WIND_DIRECTION = 999999999) and of any sentinel semantics outside the Normalizer registry.",
            "WS derived channels; any other channel", Decision.REJECTED, "sentinel_confirmed", "NOT AVAILABLE",
            pending_documentation=(
                "The MEDA RDR SIS excerpt cited by the Normalizer registry (not among its inputs). Until the "
                "Normalizer is re-run with the rule promoted to VERIFIED, such values arrive as "
                "'unverified_sentinel_candidate' and are held back by FILT-002. This package never re-labels a value "
                "as a sentinel by itself, and 999999999 in any other channel stays as the Normalizer classified it."
            ),
        ),
        RuleDefinition(
            "FILT-P05", P, "variable_specific",
            "Acceptance based on measurement uncertainty (invalid uncertainty, magnitude of uncertainty).",
            "measurements carrying an uncertainty member", Decision.REVIEW_REQUIRED, "uncertainty_policy", "NOT AVAILABLE",
            pending_documentation=(
                "A documented criterion for when an uncertainty makes a measurement unsuitable. None is supplied. "
                "The uncertainty object is preserved unchanged, including its own validity, and is NOT evaluated."
            ),
        ),
        RuleDefinition(
            "FILT-P06", P, "variable_specific",
            "Cross-variable or cross-level consistency checks (e.g. derived pressure vs Barocap channels).",
            "multi-level variables", Decision.REVIEW_REQUIRED, "cross_level_consistency", "NOT AVAILABLE",
            pending_documentation=(
                "A documented equivalence statement and tolerance between processing levels. The Normalizer states "
                "that calibrated and derived values never replace one another; the package does not say they agree."
            ),
        ),
    ]


#: Official label wording of the flag columns, transcribed from normalize.FLAG_DEFINITIONS
#: (verbatim there from the PDS4 XML <description>). It says nothing about good or bad.
FLAG_WORDING: dict[str, str] = {
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


# ---------------------------------------------------------------------------
# Source selection (record-level, explicit, default = keep everything)
# ---------------------------------------------------------------------------

#: Groups of canonical names that describe the same physical quantity at different
#: processing levels or sensors. They are CANDIDATES only: the supplied package does
#: not state that members are equivalent or interchangeable (the Normalizer says they
#: never replace one another). The default policy of every group is keep_all.
SOURCE_SELECTION_CANDIDATES: dict[str, dict] = {
    "pressure": {
        "members": ["pressure_pa", "pressure_barocap1_pa", "pressure_barocap2_pa", "pressure_barocap3_pa"],
        "note": "PS derived PRESSURE vs PS calibrated per-Barocap pressures. Normalizer notes: transducer 1 is "
                "NGM-type, transducer 2 RSP2M-type; the TRANSDUCER flag is preserved in quality.",
    },
    "downward_lw_irradiance": {
        "members": ["downward_lw_irradiance_derived_w_m2", "downward_lw_irradiance_cal_w_m2"],
        "note": "Derived: hemispherical FOV (blackbody model + Stefan-Boltzmann). Calibrated: channel bandpass "
                "6.5-30 um (Normalizer ChannelSpec notes). Different quantities.",
    },
    "upward_lw_irradiance": {
        "members": ["upward_lw_irradiance_derived_w_m2", "upward_lw_irradiance_cal_w_m2"],
        "note": "Derived: hemispherical FOV, Stefan-Boltzmann emission for the ground. Calibrated: bandpass 6.5-30 um.",
    },
    "horizontal_wind_speed": {
        "members": ["horizontal_wind_speed_m_s", "boom1_horizontal_wind_speed_m_s", "boom2_horizontal_wind_speed_m_s"],
        "note": "WS derived retrieval vs per-boom calibrated channels; BOTH_BOOMS_USED_FOR_RETRIEVAL is preserved in quality.",
    },
    "vertical_wind_speed": {
        "members": ["vertical_wind_speed_m_s", "boom1_vertical_wind_speed_m_s", "boom2_vertical_wind_speed_m_s"],
        "note": "WS derived retrieval vs per-boom calibrated channels.",
    },
    "wind_direction": {
        "members": ["wind_direction_deg", "boom1_wind_direction_deg", "boom2_wind_direction_deg"],
        "note": "WS derived retrieval vs per-boom calibrated channels.",
    },
    "relative_humidity": {
        "members": ["relative_humidity_pct", "relative_humidity_humicap_a_pct", "relative_humidity_humicap_b_pct"],
        "note": "RHS derived vs Humicap A/B calibrated. No unit is stated for any of them.",
    },
    "humidity_sensor_temperature": {
        "members": ["humidity_sensor_temp_k", "humidity_sensor_temp_cal_k"],
        "note": "RHS derived HUMIDITY_LOCAL_TEMP vs RHS calibrated HUMICAP_TEMP. The relationship is not stated by the package.",
    },
}


@dataclass(frozen=True)
class SourcePolicy:
    group_id: str
    policy: str                    # "keep_all" | "priority"
    priority: tuple[str, ...] = ()
    evidence: str = ""


# ---------------------------------------------------------------------------
# Rules configuration
# ---------------------------------------------------------------------------

_TOP_KEYS = {"config_version", "description", "disabled_rules", "range_policies", "flag_policies", "source_selection"}


def _need_text(obj: Any, key: str, where: str) -> str:
    if not isinstance(obj, Mapping):
        raise RulesConfigError(f"{where}: must be a JSON object")
    v = obj.get(key)
    if not isinstance(v, str) or not v.strip():
        raise RulesConfigError(f"{where}: '{key}' must be a non-empty string (evidence/justification is mandatory)")
    return v.strip()


def _need_names(obj: Mapping, key: str, where: str) -> list[str]:
    v = obj.get(key)
    if not isinstance(v, list) or not v or any(not isinstance(x, str) or not x for x in v):
        raise RulesConfigError(f"{where}: '{key}' must be a non-empty list of canonical measurement names")
    return list(v)


def _need_decision(obj: Mapping, where: str) -> Decision:
    d = obj.get("decision")
    if d not in ("rejected", "review_required"):
        raise RulesConfigError(f"{where}: 'decision' must be 'rejected' or 'review_required'")
    return Decision(d)


def load_rules_config(path: str | Path) -> dict:
    """Read and structurally validate a rules-configuration JSON file."""
    def _reject(token: str) -> None:
        raise ValueError(f"non-finite JSON constant {token}")

    try:
        cfg = json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=_reject)
    except (OSError, ValueError) as exc:
        raise RulesConfigError(f"cannot read rules configuration {path}: {exc}") from exc
    if not isinstance(cfg, dict):
        raise RulesConfigError("rules configuration must be a JSON object")
    unknown = set(cfg) - _TOP_KEYS
    if unknown:
        raise RulesConfigError(f"unknown rules-configuration keys: {sorted(unknown)}")
    if cfg.get("config_version", RULES_CONFIG_VERSION) != RULES_CONFIG_VERSION:
        raise RulesConfigError(f"unsupported config_version {cfg.get('config_version')!r}; expected {RULES_CONFIG_VERSION!r}")
    return cfg


def _make_range_rule(i: int, p: Any) -> RuleDefinition:
    where = f"range_policies[{i}]"
    pid = _need_text(p, "policy_id", where)
    names = _need_names(p, "variables", where)
    evidence = _need_text(p, "evidence", where)
    decision = _need_decision(p, where)
    lo, hi = p.get("min"), p.get("max")
    for label, bound in (("min", lo), ("max", hi)):
        if bound is not None and (isinstance(bound, bool) or not isinstance(bound, (int, float)) or not math.isfinite(bound)):
            raise RulesConfigError(f"{where}: '{label}' must be a finite number or omitted")
    if lo is None and hi is None:
        raise RulesConfigError(f"{where}: at least one of 'min' / 'max' is required")
    if lo is not None and hi is not None and lo > hi:
        raise RulesConfigError(f"{where}: min is greater than max")
    scope = frozenset(names)

    def fn(name, meas, record):
        if name not in scope:
            return None, ""
        v = meas.get("value")
        if meas.get("validity") != "valid" or isinstance(v, bool) or not isinstance(v, (int, float)):
            return None, ""  # judged by the core rules
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            return True, f"value {v!r} outside configured interval [{lo}, {hi}] ({pid})"
        return False, f"value within configured interval [{lo}, {hi}] ({pid})"

    return RuleDefinition(
        f"FILT-CFG-RANGE-{pid}", RuleStatus.ACTIVE, "variable_specific",
        f"Configured range policy {pid}: interval [{lo}, {hi}].", ", ".join(names), decision,
        "value_outside_configured_range", f"Supplied by the operator via rules configuration: {evidence}", fn=fn,
    )


def _flag_equals(value: Any, trigger: Any) -> bool:
    """Exact comparison; numbers compare numerically, strings textually, bool never matches."""
    if isinstance(value, bool) or isinstance(trigger, bool):
        return False
    if isinstance(value, (int, float)) and isinstance(trigger, (int, float)):
        return value == trigger
    return isinstance(value, str) and isinstance(trigger, str) and value == trigger


def _make_flag_rule(i: int, p: Any) -> RuleDefinition:
    where = f"flag_policies[{i}]"
    pid = _need_text(p, "policy_id", where)
    flag = _need_text(p, "flag", where)
    names = _need_names(p, "variables", where)
    evidence = _need_text(p, "evidence", where)
    decision = _need_decision(p, where)
    tv = p.get("trigger_values")
    if not isinstance(tv, list) or not tv or any(isinstance(x, bool) or not isinstance(x, (int, float, str)) for x in tv):
        raise RulesConfigError(f"{where}: 'trigger_values' must be a non-empty list of numbers/strings")
    scope, triggers = frozenset(names), list(tv)

    def fn(name, meas, record):
        if name not in scope:
            return None, ""
        # The flag must come from the SAME product and level as the measurement.
        key = f"{meas.get('source_product')}.{meas.get('source_level')}.{flag}"
        quality = record.get("quality") or {}
        if key not in quality:
            return None, ""  # flag not recorded for this measurement: neither pass nor fail
        val = quality[key]
        if any(_flag_equals(val, t) for t in triggers):
            return True, f"quality[{key!r}]={val!r} is a configured trigger value ({pid})"
        return False, f"quality[{key!r}]={val!r} is not a configured trigger value ({pid})"

    return RuleDefinition(
        f"FILT-CFG-FLAG-{pid}", RuleStatus.ACTIVE, "quality_flag_assessment",
        f"Configured flag policy {pid}: flag {flag} in {triggers}. Official wording: {FLAG_WORDING.get(flag, 'not in FLAG_WORDING')}",
        ", ".join(names), decision, "quality_flag_policy",
        f"Supplied by the operator via rules configuration: {evidence}", fn=fn,
    )


@dataclass
class RuleSet:
    """Resolved rules for one run: what is evaluated, what is not, and why."""

    version: str
    rules: list[RuleDefinition]                 # every rule, whatever its status
    source_policies: dict[str, SourcePolicy]
    config_path: str | None = None
    config_echo: dict | None = None             # the configuration as supplied (for the report)

    @property
    def active(self) -> list[RuleDefinition]:
        return [r for r in self.rules if r.status == RuleStatus.ACTIVE]


def build_ruleset(config: Mapping[str, Any] | None = None, *, config_path: str | None = None) -> RuleSet:
    """Resolve the default rules plus an optional (already loaded) configuration."""
    rules = _static_rules()
    by_id = {r.rule_id: r for r in rules}
    policies = {g: SourcePolicy(g, "keep_all") for g in SOURCE_SELECTION_CANDIDATES}

    if config:
        for i, item in enumerate(config.get("disabled_rules", [])):
            where = f"disabled_rules[{i}]"
            rid = _need_text(item, "rule_id", where)
            just = _need_text(item, "justification", where)
            rule = by_id.get(rid)
            if rule is None:
                raise RulesConfigError(f"{where}: unknown rule_id {rid!r}")
            if rule.core:
                raise RulesConfigError(f"{where}: {rid} is a core rule and cannot be disabled")
            if rule.status != RuleStatus.ACTIVE:
                raise RulesConfigError(f"{where}: {rid} is not active; it cannot be disabled")
            rule.status, rule.justification = RuleStatus.DISABLED, just

        for i, p in enumerate(config.get("range_policies", [])):
            rules.append(_make_range_rule(i, p))
        for i, p in enumerate(config.get("flag_policies", [])):
            rules.append(_make_flag_rule(i, p))

        seen: set[str] = set()
        for r in rules:
            if r.rule_id in seen:
                raise RulesConfigError(f"duplicate rule id {r.rule_id!r} (policy_id values must be unique)")
            seen.add(r.rule_id)

        for i, s in enumerate(config.get("source_selection", [])):
            where = f"source_selection[{i}]"
            gid = _need_text(s, "group_id", where)
            if gid not in SOURCE_SELECTION_CANDIDATES:
                raise RulesConfigError(f"{where}: unknown group_id {gid!r}; known: {sorted(SOURCE_SELECTION_CANDIDATES)}")
            pol = s.get("policy")
            if pol == "keep_all":
                policies[gid] = SourcePolicy(gid, "keep_all", (), str(s.get("evidence", "")))
            elif pol == "priority":
                order = _need_names(s, "priority", where)
                members = set(SOURCE_SELECTION_CANDIDATES[gid]["members"])
                bad = [n for n in order if n not in members]
                if bad or len(set(order)) != len(order):
                    raise RulesConfigError(f"{where}: priority must list distinct members of group {gid!r}; offending: {bad}")
                policies[gid] = SourcePolicy(gid, "priority", tuple(order), _need_text(s, "evidence", where))
            else:
                raise RulesConfigError(f"{where}: 'policy' must be 'keep_all' or 'priority'")

    return RuleSet(RULESET_VERSION, rules, policies, config_path, dict(config) if config else None)


def get_all_rules() -> list[RuleDefinition]:
    return build_ruleset().rules


def get_active_rules() -> list[RuleDefinition]:
    return build_ruleset().active


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate_measurement(
    name: str,
    measurement: Mapping[str, Any],
    record: Mapping[str, Any],
    rules: Sequence[RuleDefinition],
) -> tuple[Decision, list[RuleResult]]:
    """Apply the given (active) rules to one measurement. Never mutates its inputs.

    The final decision is the most severe one (rejected > review_required > accepted).
    """
    results = [r for r in (rule.evaluate(name, measurement, record) for rule in rules) if r is not None]
    final = Decision.ACCEPTED
    for r in results:
        if _SEVERITY[r.decision] > _SEVERITY[final]:
            final = r.decision
    return final, results


def select_sources(
    accepted_names: Sequence[str], policies: Mapping[str, SourcePolicy]
) -> tuple[dict[str, tuple[str, str]], list[str]]:
    """Record-level source selection over the already ACCEPTED measurements.

    Returns ``(dropped, unresolved)``:
      dropped    {dropped_name: (group_id, kept_name)} for groups with policy 'priority'
      unresolved [group_id, ...] for groups left on 'keep_all' in which two or more
                 members were accepted in this record (an undecided choice).

    'priority' applies to the members listed in the policy only: the first listed
    member that is accepted is kept and the other listed members are dropped;
    unlisted members are untouched. Values are never merged, averaged or converted.
    """
    present = set(accepted_names)
    dropped: dict[str, tuple[str, str]] = {}
    unresolved: list[str] = []
    for gid, pol in policies.items():
        if pol.policy == "priority":
            listed = [m for m in pol.priority if m in present]
            for extra in listed[1:]:
                dropped[extra] = (gid, listed[0])
        elif sum(1 for m in SOURCE_SELECTION_CANDIDATES[gid]["members"] if m in present) >= 2:
            unresolved.append(gid)
    return dropped, unresolved
