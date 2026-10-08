"""Test bootstrap: point the shared store at a throw-away SQLite file before app.main or the
gRPC server is imported (both read CANTEEN_DB at import time), and keep the rate limit at its
default so the limit test is deterministic."""
import os
import tempfile

os.environ.setdefault("CANTEEN_DB", os.path.join(tempfile.mkdtemp(prefix="canteen-test-"), "canteen.db"))
os.environ.setdefault("CANTEEN_RATE_LIMIT", "60")
os.environ.setdefault("CANTEEN_RATE_WINDOW_S", "60")
