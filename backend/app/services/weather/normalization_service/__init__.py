"""MarsWalk MEDA weather pipeline (Mars 2020 Perseverance, calibrated + derived).

Importing this package has **no side effects**: it reads no files, opens no
databases and starts no processing.  ``cli`` is deliberately not imported here
(run it with ``python -m normalization_service.cli``).

Public API
----------
Parsing (``parser``)
    ``iter_product_rows(path)``      parsed source rows of one CSV file
    ``iter_archive_rows(root)``      parsed source rows of every product under a root
    ``discover_products(root)``      recursive discovery with explicit skip reasons
    ``identify_file(path)``          ``(sol, product, level)`` or ``None``
    ``parse_label(path)``            PDS4 XML label -> ``ProductLabel``
    ``canonical_sclk / parse_sclk / sclk_sort_parts``   the one exact SCLK representation
    ``safe_extract_zip(zip, dest)``  guarded ZIP extraction
Normalisation (``normalize``)
    ``normalize_rows(rows)``         in-memory exact-time normalisation (small inputs)
    ``normalize_group(rows)``        one exact (Sol, SCLK) group -> canonical record
    ``verify_label_units(...)``      compare the unit table with a real label
Streaming (``streaming``)
    ``stream_normalize(rows, out)``  disk-backed pipeline -> validated JSONL (+ summary)
    ``normalize_stream_to_jsonl``    backwards-compatible ``(parsed, records)`` wrapper
    ``validate_jsonl(path)``         structural validation of a produced JSONL

Backwards compatibility: ``iter_product_rows``, ``iter_archive_rows``,
``normalize_rows`` and ``normalize_stream_to_jsonl`` keep their previous names
and call shapes.  Behavioural changes are listed in the delivery notes
(SCLK is a canonical decimal *string*, ``rover_velocity`` is metre/hour, ...).
"""
from .normalize import (
    CHANNEL_SPECS,
    FLAG_DEFINITIONS,
    LEGACY_CANONICAL_ALIASES,
    NORMALIZER_VERSION,
    NormalizationError,
    NormalizationStats,
    normalize_group,
    normalize_rows,
    prefer_derived_measurement,
    verify_label_units,
)
from .parser import (
    ArchiveError,
    DataTypeError,
    DiscoveryResult,
    InvalidSclkError,
    InvalidSolError,
    MalformedCsvError,
    MedaParseError,
    MissingColumnError,
    ParseStats,
    ProductFile,
    ProductLabel,
    UnsupportedProductError,
    XmlLabelError,
    canonical_sclk,
    discover_products,
    identify_file,
    iter_archive_rows,
    iter_product_rows,
    parse_label,
    parse_sclk,
    safe_extract_zip,
    sclk_sort_parts,
)
from .streaming import (
    ContractError,
    OutputValidationError,
    StreamingError,
    StreamSummary,
    normalize_stream_to_jsonl,
    stream_normalize,
    validate_jsonl,
)

__version__ = NORMALIZER_VERSION

__all__ = [
    # backwards-compatible core
    "iter_product_rows",
    "iter_archive_rows",
    "normalize_rows",
    "normalize_stream_to_jsonl",
    # parser
    "discover_products",
    "identify_file",
    "parse_label",
    "parse_sclk",
    "canonical_sclk",
    "sclk_sort_parts",
    "safe_extract_zip",
    "ParseStats",
    "ProductFile",
    "ProductLabel",
    "DiscoveryResult",
    # normalize
    "normalize_group",
    "NormalizationStats",
    "verify_label_units",
    "prefer_derived_measurement",
    "CHANNEL_SPECS",
    "FLAG_DEFINITIONS",
    "LEGACY_CANONICAL_ALIASES",
    # streaming
    "stream_normalize",
    "validate_jsonl",
    "StreamSummary",
    # errors
    "MedaParseError",
    "UnsupportedProductError",
    "MalformedCsvError",
    "MissingColumnError",
    "InvalidSclkError",
    "InvalidSolError",
    "XmlLabelError",
    "DataTypeError",
    "ArchiveError",
    "NormalizationError",
    "StreamingError",
    "ContractError",
    "OutputValidationError",
]
