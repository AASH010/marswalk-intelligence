
from pathlib import Path
import sys

# Project root: marswalk-intelligence/
PROJECT_ROOT = Path(__file__).resolve().parents[3]

# Weather services root
WEATHER_SERVICES_ROOT = (
    PROJECT_ROOT / "backend" / "app" / "services" / "weather"
)

# Current service package roots
FILTERING_ROOT = WEATHER_SERVICES_ROOT / "filteration_service"
NORMALIZER_ROOT = WEATHER_SERVICES_ROOT / "normalization_service"

# Test directories
TESTS_ROOT = PROJECT_ROOT / "backend" / "tests" / "weather"
FILTERING_TESTS = TESTS_ROOT / "filtering_service_tests"
NORMALIZATION_TESTS = TESTS_ROOT / "normalization_service_tests"

for path in (
    PROJECT_ROOT,
    WEATHER_SERVICES_ROOT,
    FILTERING_ROOT,
    NORMALIZER_ROOT,
    FILTERING_TESTS,
    NORMALIZATION_TESTS,
):
    if path.is_dir() and str(path) not in sys.path:
        sys.path.insert(0, str(path))
