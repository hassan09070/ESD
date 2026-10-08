#!/usr/bin/env python3
"""Write the OpenAPI document FastAPI derives from app/main.py to openapi.json (A2 Part A).

    uv run python scripts/export_openapi.py

The committed file is the spec we hand in; tests/test_api_practices.py checks it still matches
the running app, so re-run this after changing a route or a model.
"""
import json
import os
import pathlib
import sys

os.environ.setdefault("CANTEEN_DB", "/tmp/canteen-openapi.db")      # the app opens its store at import; any path works
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.main import app  # noqa: E402

out = ROOT / "openapi.json"
out.write_text(json.dumps(app.openapi(), indent=2) + "\n")
print(f"wrote {out} ({len(app.openapi()['paths'])} paths)")
