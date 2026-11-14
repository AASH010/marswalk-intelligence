"""Sentinel registry and per-channel scientific verification.

Nothing here invents science.  A statement is VERIFIED only when evidence that
can be re-checked *at run time* exists (the product's own PDS4 XML label).
Anything that rests on a document that is not part of the inputs (e.g. the
MEDA RDR SIS) is UNVERIFIED until a registry file supplies that evidence.

Sentinel rules are scoped by (product, level, source channel, value).  Policy:

* VERIFIED   -> ``value`` becomes ``None``, ``validity = invalid_sentinel``,
                the original goes to ``raw_value`` (reversible, counted).
* UNVERIFIED -> value is KEPT unchanged, ``validity =
                unverified_sentinel_candidate``, ``raw_value`` repeated,
                a warning is counted.  Never treated as a confirmed sentinel.
* REJECTED   -> ordinary value (rule explicitly disproved).

A JSON file (``--sentinel-registry``) can replace the default rules, e.g. to
promote a rule to VERIFIED once the SIS excerpt is attached as evidence.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "SentinelRule",
    "SentinelRegistry",
    "DEFAULT_SENTINEL_REGISTRY",
    "LEGACY_GLOBAL_SENTINEL_REGISTRY",
    "VERIFIED",
    "UNVERIFIED",
    "REJECTED",
    "ChannelDecision",
    "verify_channels",
    "verify_flag_definitions",
]

VERIFIED, UNVERIFIED, REJECTED = "VERIFIED", "UNVERIFIED", "REJECTED"
_STATUSES = (VERIFIED, UNVERIFIED, REJECTED)

LEGACY_EVIDENCE = (
    "MEDA RDR SIS: a value of 999999999 means the calibrated value could not be computed "
    "(not stated in the XML labels; the PDS readme quotes 9999999999)"
)


@dataclass(frozen=True)
class SentinelRule:
    product: str
    level: str
    channel: str
    value: float
    status: str
    evidence: str
    scope: str = ""
    policy: str = ""

    def __post_init__(self) -> None:
        if self.status not in _STATUSES:
            raise ValueError(f"sentinel rule status must be one of {_STATUSES}, got {self.status!r}")

    @property
    def rule_id(self) -> str:
        v = int(self.value) if float(self.value).is_integer() else self.value
        return f"{self.product}.{self.level}.{self.channel}={v}"

    @property
    def effective_policy(self) -> str:
        return {
            VERIFIED: "value->null, validity=invalid_sentinel, raw_value kept",
            UNVERIFIED: "value kept, validity=unverified_sentinel_candidate, warning counted",
            REJECTED: "treated as an ordinary value",
        }[self.status]

    def as_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "product_type": self.product,
            "processing_level": self.level,
            "source_channel": self.channel,
            "sentinel_value": self.value,
            "verification_status": self.status,
            "evidence_reference": self.evidence,
            "rule_scope": self.scope or f"{self.product}/{self.level}/{self.channel} only",
            "effective_policy": self.policy or self.effective_policy,
        }


class SentinelRegistry:
    """Scoped lookup.  ``legacy_global`` reproduces the pre-2.1 behaviour."""

    LEGACY_VALUES = frozenset({999999999, 9999999999})

    def __init__(self, rules: Iterable[SentinelRule] = (), *, legacy_global: bool = False) -> None:
        self.rules = tuple(rules)
        self.legacy_global = legacy_global
        self._index: dict[tuple[str, str, str, float], SentinelRule] = {
            (r.product, r.level, r.channel, float(r.value)): r for r in self.rules
        }
        self._legacy_rule = SentinelRule(
            "*", "*", "*", 999999999, VERIFIED,
            LEGACY_EVIDENCE,
            scope="every numeric channel (legacy)",
        )

    def match(self, product: str, level: str, channel: str, value: Any) -> SentinelRule | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        rule = self._index.get((product, level, channel, float(value)))
        if rule is not None:
            return rule
        if self.legacy_global and value in self.LEGACY_VALUES:
            return self._legacy_rule
        return None

    @classmethod
    def from_json(cls, path: str | Path) -> "SentinelRegistry":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        items = data["rules"] if isinstance(data, dict) else data
        rules = []
        for item in items:
            missing = [k for k in ("product", "level", "channel", "value", "status", "evidence") if k not in item]
            if missing:
                raise ValueError(f"sentinel rule lacks {missing}: {item!r}")
            rules.append(SentinelRule(item["product"], item["level"], item["channel"], item["value"],
                                      item["status"], item["evidence"], item.get("scope", ""), item.get("policy", "")))
        return cls(rules)

    def as_list(self) -> list[dict]:
        return [r.as_dict() for r in self.rules] or (
            [self._legacy_rule.as_dict()] if self.legacy_global else []
        )


_EV = (
    "Observed value only in this channel of the Sol 230-239 derived WS product; the XML labels contain "
    "no sentinel/special-constant statement; the previous package cited the MEDA RDR SIS "
    "(\"calibrated value could not be computed\") but that document is not among the inputs and "
    "could not be re-checked. Attach the SIS excerpt and set status VERIFIED via --sentinel-registry."
)

DEFAULT_SENTINEL_REGISTRY = SentinelRegistry(
    [
        SentinelRule("WS", "derived", "HORIZONTAL_WIND_SPEED", 999999999, UNVERIFIED, _EV),
        SentinelRule("WS", "derived", "WIND_DIRECTION", 999999999, UNVERIFIED, _EV),
    ]
)
LEGACY_GLOBAL_SENTINEL_REGISTRY = SentinelRegistry((), legacy_global=True)


# --------------------------------------------------------------------------
# Channel verification against the product's own XML label
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ChannelDecision:
    """Run-time decision for one (source file, source channel)."""

    action: str  # "accept" | "exclude"
    reason: str = ""
    final_status: str = "SCIENTIFICALLY_VERIFIED"


#: Documented label inconsistencies that do NOT change the meaning but must be shown.
_MEANING_NOTES = {
    "SOLAR_ZENITHAL_ANGLE": (
        "label text begins 'Solar elevation angle' but defines the value as the angle between the Sun "
        "direction and +Z (0 = zenith, 180 = nadir); the explicit definition is kept, the wording "
        "discrepancy is reported"
    ),
    "SOLAR_LONGITUDE_ANGLE": (
        "label defines a solar AZIMUTH angle in the local-level frame (range -180..180); it is NOT "
        "areocentric solar longitude Ls"
    ),
    "PRESSURE_UNCERTAINTY": "label gives unit kelvin for this uncertainty field, inconsistent with pressure; unit not adopted",
}


def verify_channels(
    product: str,
    level: str,
    csv_columns: Sequence[str],
    label: Any,
    specs: Sequence[Any],
    *,
    label_ref: str,
) -> list[dict]:
    """Verify every mapped channel of one product file against its label.

    Returns one dict per channel present in the CSV with the fields required by
    the validation report.  ``decision`` is a ``ChannelDecision``.
    """
    by_col = label.fields_for_columns(csv_columns)
    out: list[dict] = []
    for spec in specs:
        if spec.raw_name not in csv_columns:
            continue
        fld = by_col.get(spec.raw_name) if by_col else None
        meaning, unit_status, dtype_status = "UNVERIFIED", "UNVERIFIED", "UNVERIFIED"
        notes: list[str] = []
        evidence = f"{label_ref}"
        if fld is None:
            meaning = unit_status = dtype_status = "INVALID"
            notes.append("column has no field definition at the same position in the label")
        else:
            evidence = f"{label_ref} field_number={fld.position} name={fld.name}"
            meaning = "LABEL_DEFINED" if fld.description else "UNVERIFIED"
            dtype_status = "VERIFIED" if fld.data_type in ("ASCII_Real", "ASCII_Integer") else "INVALID"
            if spec.label_unit is None:
                # Mapping asserts no unit.  A label that also has none leaves the unit unproven.
                unit_status = "UNVERIFIED" if fld.unit is None else "UNMAPPED_UNIT_IN_LABEL"
                if fld.unit is None:
                    notes.append("label states no <unit>; unit not adopted")
            else:
                unit_status = "VERIFIED" if fld.unit == spec.label_unit else "INVALID"
                if unit_status == "INVALID":
                    notes.append(f"label unit {fld.unit!r} != mapping {spec.label_unit!r}")
        note = _MEANING_NOTES.get(spec.raw_name)
        if note:
            notes.append(note)
        if unit_status == "INVALID" or dtype_status == "INVALID" or meaning == "INVALID":
            final, action = "INVALID", "exclude"
        elif unit_status in ("UNVERIFIED", "UNMAPPED_UNIT_IN_LABEL"):
            final, action = "UNVERIFIED", "accept"
        elif meaning == "UNVERIFIED":
            final, action = "NUMERICALLY_VALID", "accept"
        else:
            final, action = "SCIENTIFICALLY_VERIFIED", "accept"
        out.append(
            {
                "source_channel_name": spec.raw_name,
                "canonical_channel_name": spec.canonical,
                "product_type": product,
                "processing_level": level,
                "source_data_type": fld.data_type if fld else None,
                "source_unit": fld.unit if fld else None,
                "canonical_unit": spec.unit if final != "INVALID" else None,
                "scientific_definition": (re.sub(r"\s+", " ", fld.description)[:400] if fld else None),
                "xml_label_reference": evidence,
                "scientific_definition_status": meaning,
                "unit_compatibility_status": unit_status,
                "data_type_status": dtype_status,
                "transformation_applied": "none (identity; unit symbol rename only)",
                "transformation_evidence": "no scaling or offset exists in the mapping table",
                "notes": notes,
                "static_status": final,
                "decision": ChannelDecision(action, "; ".join(notes), final),
            }
        )
    return out


def verify_flag_definitions(label: Any, csv_columns: Sequence[str], definitions: Mapping[str, str]) -> list[dict]:
    """Compare the official flag wording kept in the package with the label text.

    No meaning is derived from values; only whether the *documented wording*
    still equals the product's own label description.
    """
    by_col = label.fields_for_columns(csv_columns)
    results = []
    for column, expected in definitions.items():
        fld = by_col.get(column) if by_col else None
        if fld is None:
            continue
        norm = lambda s: re.sub(r"\s+", " ", s or "").strip().lower().rstrip(".")  # noqa: E731
        ok = norm(expected) in norm(fld.description) or norm(fld.description) in norm(expected)
        results.append({"flag": column, "label_ref": f"{label.path.name} field_number={fld.position}",
                        "definition_status": VERIFIED if ok else UNVERIFIED,
                        "label_text": re.sub(r"\s+", " ", fld.description)[:200]})
    return results
