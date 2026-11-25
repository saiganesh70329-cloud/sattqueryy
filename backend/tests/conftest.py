"""Keep backend tests isolated from the developer's local SATQUERY database."""

import os
import tempfile
from pathlib import Path

_test_db_dir = Path(tempfile.mkdtemp(prefix="satquery-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_test_db_dir / 'test.db').as_posix()}"
os.environ["SATQUERY_SKIP_SCHEMA_CREATE"] = "1"
