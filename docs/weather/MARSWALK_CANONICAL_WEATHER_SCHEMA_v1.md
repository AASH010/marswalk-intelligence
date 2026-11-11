# MarsWalk Intelligence — Canonical Weather Schema

**Version:** 1.0.0-draft  
**Status:** Design specification only; no application code or repository files modified.  
**Source collection:** MEDA calibrated + selected derived products, Sol 230–239.

## 1. Purpose

The Canonical Weather Schema is the shared contract between raw MEDA products and all downstream MarsWalk services. It standardizes variable meaning, units, provenance, timestamps, quality, and aggregation without destroying the original NASA observations.

It is not a new NASA dataset and it does not mean every source column is merged into one value.

## 2. Processing pipeline

```text
NASA calibrated CSV/XML + NASA derived CSV/XML
                    |
                    v
         Product-specific parsers
                    |
                    v
     Canonical observations + provenance
                    |
                    v
       QA / validity / unit normalization
                    |
                    v
       Per-Sol + pooled aggregation
                    |
                    v
      Unified Weather Profile / Analysis
             |             |
             v             v
       Weather API     Internal Weather Service
                           |
                  +--------+---------+
                  |                  |
                  v                  v
              A* routing       EVA & Consumables
```

## 3. Canonical observation envelope

Every normalized observation must retain:

| Field | Type | Meaning |
|---|---|---|
| `sol` | integer | Mars solar day |
| `sclk_s` | number | MEDA spacecraft clock, seconds |
| `lmst` | string/null | Original Local Mean Solar Time |
| `ltst` | string/null | Original Local True Solar Time |
| `utc` | ISO datetime/null | Only after validated SCLK-to-UTC correlation |
| `source.source_level` | enum | `calibrated` or `derived` |
| `source.product` | string | `ATS`, `PS`, `RHS`, `TIRS`, `WS`, `RDS`, `ANC` |
| `source.source_file` | string | Original filename |
| `source.source_channel` | string | Original column name |
| `source.pds_urn` | string/null | Original product URN |
| `quality.validity_status` | enum | `valid`, `missing`, `invalid_sentinel`, `flagged`, `not_assessed` |
| `quality.flags` | object | Original product flags, not flattened into one generic good/bad bit |

One canonical record represents one source observation. Do not force asynchronous product streams into a single row by row-number alignment.

## 4. Variable mapping and source policy

| Canonical variable | Preferred source | Unit | Policy |
|---|---|---|---|
| `pressure_pa` | `DER_PS.PRESSURE` | Pa | Keep CAL_PS Barocap 1/2/3 as separate channels. |
| `relative_humidity_pct` | `DER_RHS.LOCAL_RELATIVE_HUMIDITY` | %RH* | Keep CAL_RHS Humicap A/B separate. Derived RHS Sol 232 is absent; do not silently impute. |
| `humidity_sensor_temperature_k` | `DER_RHS.HUMIDITY_LOCAL_TEMP` | K | Keep sensor temperatures distinct. |
| `air_temperature_k` | `CAL_TIRS.AIR_TEMP` | K | Keep ATS 1–5 as separate local-temperature channels; do not blindly average. |
| `ground_brightness_temperature_k` | `CAL_TIRS.GROUND_TEMP` | K | Preserve the brightness-temperature interpretation. |
| `downward_lw_irradiance_cal_w_m2` | `CAL_TIRS.DOWNWARD_LW_IRRADIANCE` | W/m² | Calibrated 6.5–30 μm bandpass measurement. |
| `downward_lw_irradiance_derived_w_m2` | `DER_TIRS.DOWNWARD_LW_IRRADIANCE` | W/m² | Derived hemispherical estimate using blackbody / Stefan–Boltzmann assumptions. Keep distinct from calibrated value. |
| `upward_lw_irradiance_cal_w_m2` | `CAL_TIRS.UPWARD_LW_IRRADIANCE` | W/m² | Calibrated bandpass measurement. |
| `upward_lw_irradiance_derived_w_m2` | `DER_TIRS.UPWARD_LW_IRRADIANCE` | W/m² | Derived estimate; do not merge with calibrated value. |
| `upward_sw_irradiance_w_m2` | `CAL_TIRS.UPWARD_SW_IRRADIANCE` | W/m² | Calibrated source in supplied collection. |
| `wind_horizontal_speed_derived_m_s` | `DER_WS.HORIZONTAL_WIND_SPEED` | m/s | Preferred combined retrieval when valid; preserve retrieval flags. |
| `wind_vertical_speed_derived_m_s` | `DER_WS.VERTICAL_WIND_SPEED` | m/s | Keep separate; coverage may be sparse. |
| `wind_direction_derived_deg` | `DER_WS.WIND_DIRECTION` | degree | Circular variable; never use arithmetic mean. |
| `wind_boom1_horizontal_speed_m_s` | `CAL_WS.BOOM1_HORIZONTAL_WIND_SPEED` | m/s | Preserve Boom 1 independently. |
| `wind_boom2_horizontal_speed_m_s` | `CAL_WS.BOOM2_HORIZONTAL_WIND_SPEED` | m/s | Preserve Boom 2 independently. |
| `wind_boom1_direction_deg` | `CAL_WS.BOOM1_WIND_DIRECTION` | degree | Circular; keep Boom 1 independent. |
| `wind_boom2_direction_deg` | `CAL_WS.BOOM2_WIND_DIRECTION` | degree | Circular; keep Boom 2 independent. |
| `solar_longitude_deg` | `DER_ANCILLARY.SOLAR_LONGITUDE_ANGLE` | degree | Geometry/context, not a local weather measurement. |
| `solar_zenith_angle_deg` | `DER_ANCILLARY.SOLAR_ZENITHAL_ANGLE` | degree | Preserve PDS definition and convention. |
| `rover_position_m2020_topo_m` | `DER_ANCILLARY.ROVER_POSITION_X/Y/Z` | m | M2020_TOPO frame relative to landing site; transform before map use. |
| `rds_channels` | `CAL_RDS` | native per-channel | Keep LAT/TOP, channel number and normal/HG gain separate. No dust opacity/concentration without a validated retrieval. |

*The inspected RHS XML does not declare a unit for relative humidity. `%RH` is the intended semantic interpretation but must be verified against the MEDA instrument documentation before production calculations.

## 5. Critical non-equivalences

These fields must remain separate:

1. `CAL_TIRS` longwave irradiance is a bandpass measurement; `DER_TIRS` is a derived hemispherical estimate with model assumptions.
2. `CAL_WS` contains separate boom channels; `DER_WS` is a combined retrieval. Do not average the booms and call it the derived result.
3. `CAL_ATS` has five local temperature channels; they are not automatically interchangeable with TIRS air temperature.
4. `CAL_PS` has three Barocap channels and instrument metadata; do not blindly average channels with different transducer characteristics.
5. `CAL_RDS` normal-gain and high-gain channels are separate acquisition modes. Do not combine them without calibration/retrieval rules.
6. `DER_ANCILLARY` rover coordinates are not MarsWalk raster coordinates. Apply a validated coordinate transform.

## 6. Time alignment

- Preserve `SCLK`, `LMST`, and `LTST` exactly as supplied.
- Use `(sol, sclk_s, product)` as the within-source observation identity.
- Never join datasets by row index.
- Exact SCLK joins are allowed only when timestamps are truly aligned and semantically comparable.
- Otherwise aggregate each variable independently into explicit time bins or use a documented as-of tolerance.
- Do not derive per-row UTC from product start/stop UTC. Use a validated SPICE/PDS SCLK correlation.

## 7. Quality and missing data

- Empty strings and non-finite values become normalized `null`, with a missingness reason.
- Do not treat numeric zero as missing by default.
- Observed WS sentinel `999999999` must be mapped to invalid/null only for fields where the product documentation confirms it is a sentinel.
- Preserve original values and original flags for auditability.
- Do not discard extreme wind values solely because they look large; inspect algorithm stability, rover state, and documentation.
- Interpret each flag using its own definition. A value of `1` is not generically “good”.
- Retain uncertainty columns even when sparse; never invent uncertainty.

## 8. Aggregation

For every variable and every selected window, produce:

- `count_total`
- `count_valid`
- `count_missing`
- `missing_ratio`
- `mean`
- `median`
- `min`
- `max`
- `std`
- `first_time`
- `last_time`

Also retain per-Sol statistics. Pooled observation-weighted statistics and equal-Sol-weighted summaries answer different questions; report them separately when useful.

Wind direction requires circular statistics:
- circular mean
- resultant length
- circular standard deviation

Do not compute an arithmetic mean for degrees around the 0°/360° boundary.

## 9. Downstream use

### Weather API
- `GET /api/v1/weather/summary`: unified profile and quality-aware statistics.
- `POST /api/v1/weather/analyze`: mission/route-window weather interpretation.

### A* routing
The Weather Service can provide route/mission-level weather context. A per-cell weather cost requires an independently justified spatial exposure model. MEDA is a rover-mounted point/time-series source, not a weather raster.

### EVA & Consumables
Weather impact may modify mission estimates only through documented rules. Preserve baseline consumables and weather-adjusted consumables separately.

### Frontend
Present one unified weather profile. Per-Sol statistics remain internal QA/analysis, not a required Sol selector in the UI.

## 10. Known collection gaps and cautions

- The supplied Derived archive is missing RHS Sol 232 (CSV and XML).
- Several uncertainty / housekeeping fields are fully or mostly empty.
- No Derived RDS product is present in the selected Derived collection; use calibrated RDS channels only.
- The source archive is retained unchanged; this schema is a normalization contract, not a replacement for raw NASA data.

## 11. Implementation sequence after approval

1. Implement product-specific readers for CSV/XML.
2. Implement canonical observation models and provenance.
3. Implement validity/sentinel/quality handling.
4. Implement unit conversions only at API/presentation boundaries (canonical storage uses source SI units where defined).
5. Implement per-Sol and pooled aggregation, including circular wind statistics.
6. Expose the Weather Service and API.
7. Integrate EVA analysis, then evaluate a scientifically defensible spatial routing model.

