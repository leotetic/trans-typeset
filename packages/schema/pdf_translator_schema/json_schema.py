from __future__ import annotations

import json
from pathlib import Path

from .models import (
    AssetIR,
    DocumentIR,
    InputSource,
    LayoutIntentPlan,
    TranslationChunk,
    TranslationLayoutPlan,
    UserIntent,
    WorkflowRun,
)


SCHEMA_URI = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_VERSION = "0.1"


def _with_metadata(schema: dict) -> dict:
    return {
        **schema,
        "$schema": SCHEMA_URI,
        "x-schema-version": SCHEMA_VERSION,
    }


def export_schema(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    schemas = {
        "document-ir.schema.json": _with_metadata(DocumentIR.model_json_schema()),
        "input-source.schema.json": _with_metadata(InputSource.model_json_schema()),
        "asset-ir.schema.json": _with_metadata(AssetIR.model_json_schema()),
        "user-intent.schema.json": _with_metadata(UserIntent.model_json_schema()),
        "workflow-run.schema.json": _with_metadata(WorkflowRun.model_json_schema()),
        "layout-intent-plan.schema.json": _with_metadata(
            LayoutIntentPlan.model_json_schema()
        ),
        "translation-chunk.schema.json": _with_metadata(TranslationChunk.model_json_schema()),
        "translation-layout-plan.schema.json": _with_metadata(
            TranslationLayoutPlan.model_json_schema()
        ),
    }
    for filename, schema in schemas.items():
        (output_dir / filename).write_text(
            json.dumps(schema, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    export_schema(Path(__file__).resolve().parents[2] / "json-schema")
