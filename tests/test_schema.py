from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from kilog.diffing import build_event
from tests.helpers import item, snapshot


def test_generated_event_matches_bundled_schema():
    before = snapshot()
    after = snapshot(item("track-1", "track", width=250000))
    event = build_event(before, after, 1, "11111111-1111-4111-8111-111111111111")
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "plugin"
        / "kilog"
        / "schemas"
        / "operation-log-v1.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(event)
