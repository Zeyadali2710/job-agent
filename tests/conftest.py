"""Make the repo root importable so `tests.mock_forms` resolves.

pytest prepends a test file's own directory to sys.path, which makes
`tests.unit.*` importable but not `tests.*`. The mock form server lives beside
the tests that use it rather than in src/, because it is a test fixture and
shipping it inside the application image would be wrong.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
