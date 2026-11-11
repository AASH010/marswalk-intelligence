from pathlib import Path
from meda_weather.parser import identify_file, _to_value
from meda_weather.normalize import normalize_rows

def test_identify_calibrated():
    assert identify_file(Path("resources/MEDA_Weather_ds/sol_0230/WE__0230_CAL_WS.CSV")) == (230, "WS", "calibrated")

def test_identify_derived_ancillary():
    assert identify_file(Path("derived/WE__0230_DER_ANCILLARY.CSV")) == (230, "ANC", "derived")

def test_values():
    assert _to_value("") is None
    assert _to_value("12.5") == 12.5
    assert _to_value("nominal") == "nominal"

def test_exact_sclk_join_and_source_preference():
    rows = [
      {"sol":230,"sclk_s":100,"lmst":"x","ltst":"y",
       "source":{"product":"WS","source_level":"calibrated","source_file":"cal.csv"},
       "raw":{"BOOM1_HORIZONTAL_WIND_SPEED":5.0}},
      {"sol":230,"sclk_s":100,"lmst":"x","ltst":"y",
       "source":{"product":"WS","source_level":"derived","source_file":"der.csv"},
       "raw":{"HORIZONTAL_WIND_SPEED":4.0}},
      {"sol":230,"sclk_s":101,"lmst":"z","ltst":"w",
       "source":{"product":"WS","source_level":"derived","source_file":"der.csv"},
       "raw":{"HORIZONTAL_WIND_SPEED":3.0}},
    ]
    result=normalize_rows(rows)
    assert len(result)==2
    assert result[0]["measurements"]["horizontal_wind_speed_m_s"]["value"] == 4.0
    assert result[0]["measurements"]["boom1_horizontal_wind_speed_m_s"]["value"] == 5.0
