# MEDA Scientific Validation & Quality Flag Mapping — V1

- **Project:** MarsWalk Intelligence (MWI)
- **Stage:** MEDA Scientific Validation & Quality Flag Mapping
- **Report date:** 2026-11-11
- **Status:** In progress; source-backed definitions captured; transformation/filter policies not yet applied
- **Data scope:** Sols 230–239
- **Products reviewed:** CAL_WS, CAL_RDS, CAL_RHS, CAL_TIRS, CAL_PS, CAL_ATS, CAL_ENG; DER_WS, DER_RHS, DER_TIRS, DER_PS, DER_ANCILLARY

## 1. Purpose

Establish source-backed meanings for MEDA variables and quality/context flags before changing normalized data or building aggregation rules. This report records what the original PDS product labels say, what was observed in the supplied CSV files, and what remains unresolved.

## 2. Inputs and provenance

| Input | Scope / role | SHA-256 |
|---|---|---|
| `MEDA_Weather_ds.zip` | Calibrated product CSVs and detached PDS4 XML labels, Sols 230–239 | `f9a4043002bfb345b3819912392b30e2b9b0e351897c05949a5ec17890166e19` |
| `derived.zip` | Derived product CSVs and detached PDS4 XML labels, Sols 230–239 | `95407346ead23de19fc88210112d450a7fc13941dbbe32522315c797d24a76f9` |
| `meda_schema_report.json` | Existing normalized-schema discovery report (471,708 records) | `04e0ceef191895363c1ded8572824aeaa6899015fc33ccd9a8eef713b52f0799` |

The source CSV and XML pairs were inspected directly. Counts in this report were recomputed by streaming the supplied CSV files. The normalized JSONL itself was not part of this upload, so no rewrite or record-by-record normalized-vs-source reconciliation was performed in this checkpoint.

## 3. Source-backed product interpretation

The PDS MEDA documentation distinguishes calibrated and derived products:

- Calibrated products provide calibrated environmental magnitudes and, for subsystems with multiple detectors, retain per-detector values.
- Derived products are processed from calibrated products using assumptions or models. They may combine multiple detectors into a single value and are not interchangeable with the individual calibrated channels.
- The official MEDA overview describes a general convention for product flags: `1` means the stated good condition is present and `0` means the stated bad condition is present. The exact meaning still comes from each field's own description.

**Project decision D-001 (approved):** retain calibrated detector-level channels and derived product outputs as distinct source-level observations/provenance. Do not collapse them into one field merely because they represent the same physical quantity.

## 4. Quality flag dictionary — source-backed semantics

### 4.1 Wind Sensor — calibrated `CAL_WS`

The CSV header and PDS XML field descriptions identify:

| CSV field | Source meaning of `0` | Source meaning of `1` | Supplied-data counts | Project interpretation |
|---|---|---|---:|---|
| `BOOM1_ALG_STATE_STABLE` | Boom 1 control algorithm is changing state | Boom 1 control algorithm is not changing state | 4,843 zero; 356,486 one | Algorithm-state context for Boom 1 measurements |
| `BOOM2_ALG_STATE_STABLE` | Boom 2 control algorithm is changing state | Boom 2 control algorithm is not changing state | 4,602 zero; 356,727 one | Algorithm-state context for Boom 2 measurements |
| `ROVER_STILL` | Rover is moving | Rover is motionless | 625 zero; 360,704 one | Rover-motion context; not a direct wind-quality verdict |

**Label/header discrepancy:** the CAL_WS XML labels call fields 16 and 17 `BOOM1_ALG_IN_STABLE_STATE` and `BOOM2_ALG_IN_STABLE_STATE`, while the actual CSV headers are `BOOM1_ALG_STATE_STABLE` and `BOOM2_ALG_STATE_STABLE`. Field positions (16/17) and descriptions align. The project mapping follows the CSV header but preserves this discrepancy in the audit log; it is not silently treated as a perfectly matching label.

### 4.2 Radiation and Dust Sensor — calibrated `CAL_RDS`

| CSV field | Source meaning of `0` | Source meaning of `1` | Supplied-data counts | Project interpretation |
|---|---|---|---:|---|
| `ROVER_HGA_STILL` | HGA antenna is moving | HGA antenna is motionless | 469,915 blank | Defined by source, but unavailable in these supplied rows; blank is **unknown**, not `0` |
| `ROVER_STILL` | Rover is moving | Rover is motionless | 625 zero; 469,290 one | Rover-motion context |

### 4.3 Thermal Infrared Sensor — calibrated and derived `TIRS`

The same flag definitions and value distributions occur in the supplied `CAL_TIRS` and `DER_TIRS` products (471,471 rows each):

| CSV field | Source meaning of `0` | Source meaning of `1` | Supplied-data counts | Scope caution |
|---|---|---|---:|---|
| `RSM_HEAD_OUTSIDE_TIRS_UPWARD_LOOKING_FOV` | RSM head obstructs the upward-looking channels' FOV | RSM head is outside that FOV | 471,471 zero; 0 one | Relevant to affected upward-looking channels; all-zero distribution requires investigation before use as a filter |
| `WHEEL_OUTSIDE_TIRS_DOWNWARD_LOOKING_FOV` | Front-right wheel obstructs downward-looking channels' FOV | Wheel is outside that FOV | 471,471 blank | Unknown/unavailable in this sample; do not coerce to zero |
| `SUN_OUTSIDE_TIRS_FOV` | Sun is inside the upward-looking / upward-SW channels' FOV | Sun is outside that FOV | 38,288 zero; 433,183 one | Apply only to affected channels/interpretations, not every TIRS variable |
| `ROVER_LOW_TILT` | High rover tilt (at/above the stated ±20° criterion) | Low rover tilt (at/below the stated ±20° criterion) | 471,471 one; 0 zero | Source label says low tilt gives complete ground/sky coverage of corresponding FOVs; all-one distribution should be reviewed |
| `TIRS_GROUND_FOOTPRINT_NOT_IN_SHADOW` | Ground footprint is shadowed by the rover | Ground footprint is not shadowed by the rover | 64,722 zero; 406,749 one | Relevant to ground-looking measurements, not a global TIRS rejection rule |
| `ROVER_HGA_OFF` | Rover HGA antenna is on | Rover HGA antenna is off | 471,471 blank | Unknown/unavailable in this sample |
| `SKYCAM_OFF` | SkyCam is on | SkyCam is off | 471,471 blank | Unknown/unavailable in this sample |
| `ROVER_STILL` | Rover is moving | Rover is motionless | 707 zero; 470,764 one | Rover-motion context |

### 4.4 Derived Wind Sensor — `DER_WS`

| CSV field | Source meaning of `0` | Source meaning of `1` | Supplied-data counts | Project interpretation |
|---|---|---|---:|---|
| `BOTH_BOOMS_USED_FOR_RETRIEVAL` | Only one boom was powered on and used to determine wind | Both booms were used to determine wind | 361,534 blank | Defined by source but not populated in the supplied files; keep as unknown |
| `ROVER_STILL` | Rover is moving | Rover is motionless | 625 zero; 360,909 one | Rover-motion context |

### 4.5 Metadata fields that are not binary quality flags

- `CAL_PS.PRESSURE_MEASUREMENT_MODE`: categorical mode (`nominal` / `high resolution`).
- `CAL_PS.TRANSDUCER` and `DER_PS.TRANSDUCER`: transducer identifier (`1` / `2`).
- Uncertainty columns are uncertainty estimates, not boolean flags. Preserve them as uncertainty metadata.

## 5. Decisions recorded before application

| ID | Decision | Status | Reason |
|---|---|---|---|
| D-001 | Keep calibrated detector-level values distinct from derived/modelled values | Approved | PDS product-level descriptions distinguish the processing levels and purpose |
| D-002 | Interpret documented binary flag values according to each XML field description; `1`/`0` semantics are not inferred from names alone | Approved | Explicit PDS field descriptions |
| D-003 | Blank flag values remain `unknown/unavailable`; never coerce blank to `0` or `1` | Approved | Blank CSV cells are not source-defined boolean values |
| D-004 | Do not automatically discard measurements when a context/quality flag is `0`; first scope the flag to the variables it affects and validate its downstream effect | Approved | Several flags describe geometry, rover state, or algorithm state rather than universal invalidity |
| D-005 | Keep `BOOM*_ALG_STATE_STABLE` flags linked to their corresponding boom channels | Approved | Field descriptions explicitly distinguish Boom 1 and Boom 2 |
| D-006 | Do not use `RSM_HEAD...`, `ROVER_LOW_TILT`, or `TIRS_GROUND_FOOTPRINT_NOT_IN_SHADOW` as a global TIRS filter | Approved | Their descriptions refer to specific FOV/ground-footprint geometry |
| D-007 | Treat `999999999` in derived wind speed/direction as a candidate sentinel, not a confirmed PDS special constant | Pending source-SIS confirmation | The supplied detached XML labels do not declare a special-constant definition |
| D-008 | Correct the normalized `solar_longitude_deg` semantic mapping: source `SOLAR_LONGITUDE_ANGLE` is local solar azimuth in the rover frame, range −180° to +180°, not Mars heliocentric solar longitude (Ls) | Approved as a schema correction; implementation pending | DER_ANCILLARY XML field 4 explicitly defines a local-frame solar azimuth angle |
| D-009 | Correct `rover_velocity_m_s`: source `ROVER_VELOCITY` unit is `meter/hour`; conversion to m/s requires division by 3600, or retain the source unit/name | Approved as a schema correction; implementation pending | DER_ANCILLARY XML field 9 explicitly declares `meter/hour`; current schema name claims m/s |
| D-010 | Preserve wind direction as a circular variable; do not use arithmetic mean for its summary | Approved | Angular wraparound makes ordinary arithmetic mean misleading |

No flag-based filtering, sentinel replacement, unit conversion, variable renaming, or aggregation has been applied to the source or normalized data in this checkpoint.

## 6. Additional scientific-validation findings

### 6.1 Candidate sentinel in derived wind

`DER_WS` contains `999999999` in `HORIZONTAL_WIND_SPEED` and `WIND_DIRECTION` for 9,676 rows each. The supplied product XML does not declare this number in a special-constants section. The value is excluded from descriptive-statistic calculations in the preliminary counts only; its production-data handling remains pending until the RDR SIS or another authoritative product definition confirms the convention. Do not generalize this sentinel to unrelated variables/products.

### 6.2 Solar angle naming and semantics

The `DER_ANCILLARY` XML field `SOLAR_LONGITUDE_ANGLE` is explicitly described as the Sun's **azimuth angle relative to the M2020 local-level frame**, measured in the rover frame with a range of −180° to +180°. It is not the heliocentric solar longitude (Ls). The existing normalized key `solar_longitude_deg` is therefore scientifically misleading and should be renamed to a local solar-azimuth name after the mapping code is reviewed.

### 6.3 Rover velocity unit

The `DER_ANCILLARY` XML declares `ROVER_VELOCITY` in `meter/hour`. The existing normalized schema field is named `rover_velocity_m_s`; the previously reported maximum of 128.06 matches the source scale and indicates that the value may have been carried across without conversion. Before any use, verify the normalization code and either:

- rename/store as `rover_velocity_m_per_hour`, or
- convert using `m/s = (m/hour) / 3600` and document the conversion.

### 6.4 Derived-vs-calibrated distinctions to preserve

- Wind: `CAL_WS` provides separate boom measurements; `DER_WS` provides a derived combined wind product.
- Pressure: `CAL_PS` provides multiple Barocap transducers; `DER_PS` provides a derived pressure value and transducer identifier.
- Humidity: `CAL_RHS` provides Humicap A/B channels; `DER_RHS` provides local relative humidity and volume mixing ratio.
- TIRS: derived radiative products use assumptions described in the product overview; they must retain their derived provenance.

### 6.5 Product-label/header inconsistencies found

The supplied XML labels and CSV headers have these differences:

| Product | Field position | XML label name | CSV header | XML description alignment |
|---|---:|---|---|---|
| CAL_WS | 16 | `BOOM1_ALG_IN_STABLE_STATE` | `BOOM1_ALG_STATE_STABLE` | Describes Boom 1 algorithm changing/not changing state |
| CAL_WS | 17 | `BOOM2_ALG_IN_STABLE_STATE` | `BOOM2_ALG_STATE_STABLE` | Describes Boom 2 algorithm changing/not changing state |
| CAL_RHS | 13 | `THERMOCAP1_TEMP_UNCERTAINTY` | `THERMOCAP2_TEMP_UNCERTAINTY` | XML description says Thermocap 2 uncertainty |
| DER_RHS | 7 | `LOCAL_TEMP_UNCERTAINTY` | `HUMIDITY_LOCAL_TEMP_UNCERTAINTY` | Description says humidity local temperature uncertainty |
| DER_RHS | 9 | `VOLUME_MIXING_RATION_UNCERTAINTY` | `VOLUME_MIXING_RATIO_UNCERTAINTY` | Typographical difference (`RATION` vs `RATIO`) |
| DER_PS | 5 | `PRESSURE UNCERTAINTY` | `PRESSURE_UNCERTAINTY` | Same meaning; whitespace/underscore normalization |

Mapping should use the CSV header for the actual column identifier and preserve XML field number/description as provenance. The discrepancies should remain visible in the source-audit record.

## 7. Source-data counts (Sols 230–239)

| Product | Rows | Notes |
|---|---:|---|
| CAL_WS | 361,329 | 10 Sol files |
| CAL_RDS | 469,915 | `ROVER_HGA_STILL` blank in all rows |
| CAL_TIRS | 471,471 | Several geometry flags have constant or blank distributions |
| DER_WS | 361,534 | 9,676 candidate sentinel values in speed and direction |
| DER_TIRS | 471,471 | Flag counts match corresponding CAL_TIRS fields |
| DER_ANCILLARY | 471,708 | Rover velocity source unit is m/hour |
| DER_RHS | 382,316 | Sol 232 file is absent in supplied derived archive; this is not a zero-reading Sol |

Counts are scoped to the supplied archives and Sol range only; they are not mission-wide totals.

## 8. Open issues / next validation actions

1. Obtain/read the authoritative MEDA RDR Data Product SIS and relevant release notes to confirm the `999999999` convention, wind derivation, and any sensor-specific quality logic.
2. Reconcile the normalized mapping code against the confirmed PDS field names, especially solar azimuth and rover velocity units.
3. Validate all source-to-normalized join keys and per-product sampling alignment using `(Sol, SCLK)`; do not assume all products share identical sampling.
4. Investigate all-zero `RSM_HEAD_OUTSIDE_TIRS_UPWARD_LOOKING_FOV` and all-one `ROVER_LOW_TILT` distributions before using them operationally.
5. Determine whether blank fields represent unreported/unsupported channels in these products or a product/version-specific behavior; preserve unknown until proven.
6. Review Humicap A/B and derived RH disagreement/negative values using the RHS SIS and uncertainty information.
7. Review wind direction using circular statistics and source uncertainty; do not use arithmetic mean.
8. Only after decisions above are closed, implement a versioned quality policy and rerun variable QA against the normalized data.

## 9. Exit criteria for this stage

- [x] Original calibrated and derived XML labels inspected for the supplied products.
- [x] Binary flag meanings captured from field descriptions.
- [x] Blank vs `0`/`1` states distinguished.
- [x] Raw CSV flag distributions recomputed for Sols 230–239.
- [x] Product-level label/header mismatches documented.
- [x] High-priority unit and semantic mapping issues recorded.
- [ ] RDR SIS and release-note review completed.
- [ ] Candidate sentinel policy confirmed by authoritative documentation.
- [ ] Normalization mapping reconciled and approved.
- [ ] Quality policies applied and regression QA passed.

## 10. References

1. NASA PDS Atmospheres Node, MEDA instrument and data overview: https://pds-atmospheres.nmsu.edu/data_and_services/atmospheres_data/PERSEVERANCE/meda.html
2. MEDA RDR Software Interface Specification (SIS), official PDS-hosted document: https://pds-atmospheres.nmsu.edu/data_and_services/atmospheres_data/PERSEVERANCE/logs/MEDA_RDR_SIS_issue1.docx (listed in the official MEDA document index; detailed review remains an open action).
3. NASA PDS MEDA bundle information: https://pds.nasa.gov/ds-view/pds/viewBundle.jsp?identifier=urn%3Anasa%3Apds%3Amars2020_meda&version=1.0
4. MEDA PDS4 detached XML labels paired with the supplied source CSV files (primary field-level evidence), especially `CAL_WS`, `CAL_RDS`, `CAL_RHS`, `CAL_TIRS`, `CAL_PS`, `DER_WS`, `DER_RHS`, `DER_TIRS`, `DER_PS`, and `DER_ANCILLARY`.

---

**Implementation boundary:** This report documents source-backed meanings and recommended schema corrections. It does not claim that the normalized dataset has been modified or that all scientific validation is complete.
