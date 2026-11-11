# Dataset audit used for parser validation

| Archive | Product | CSV files | Rows |
|---|---:|---:|---:|
| Calibrated | ATS | 10 | 471,482 |
| Calibrated | ENG | 10 | 471,549 |
| Calibrated | PS | 10 | 468,980 |
| Calibrated | RDS | 10 | 469,915 |
| Calibrated | RHS | 10 | 430,089 |
| Calibrated | TIRS | 10 | 471,471 |
| Calibrated | WS | 10 | 361,329 |
| Derived | ANC | 10 | 471,708 |
| Derived | PS | 10 | 468,980 |
| Derived | RHS | 9 | 382,316 |
| Derived | TIRS | 10 | 471,471 |
| Derived | WS | 10 | 361,534 |

Total CSV row counts: Calibrated 3,145,815; Derived 2,156,009.

The Derived RHS coverage has no file for Sol 232 in the supplied archive. This is recorded as missing coverage, not filled from invented or interpolated data. The parser was tested on the package's 4 unit tests and a synthetic exact-SCLK merge. The archives were independently scanned to verify file identification and row counts; a full end-to-end normalization of all rows has not yet been run.
