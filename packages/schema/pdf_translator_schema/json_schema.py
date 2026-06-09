from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import (
    AssetIR,
    DocumentIR,
    FormulaRecognitionResult,
    InputSource,
    LayoutIntentPlan,
    OCRRecognitionResult,
    SemanticLayoutAnalysis,
    TranslationChunk,
    TranslationLayoutPlan,
    UserIntent,
    WorkflowRun,
)


SCHEMA_URI = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_VERSION = "0.1"

SCHEMA_MODELS = {
    "document-ir": DocumentIR,
    "input-source": InputSource,
    "asset-ir": AssetIR,
    "formula-recognition": FormulaRecognitionResult,
    "ocr-recognition": OCRRecognitionResult,
    "user-intent": UserIntent,
    "workflow-run": WorkflowRun,
    "layout-intent-plan": LayoutIntentPlan,
    "semantic-layout-analysis": SemanticLayoutAnalysis,
    "translation-chunk": TranslationChunk,
    "translation-layout-plan": TranslationLayoutPlan,
}


def _with_metadata(schema: dict) -> dict:
    return {
        **schema,
        "$schema": SCHEMA_URI,
        "x-schema-version": SCHEMA_VERSION,
    }


def schema_for(name: str) -> dict[str, Any]:
    try:
        model = SCHEMA_MODELS[name]
    except KeyError as exc:
        available = ", ".join(sorted(SCHEMA_MODELS))
        raise ValueError(f"unknown schema {name!r}; available schemas: {available}") from exc
    return _with_metadata(model.model_json_schema())


def all_schemas() -> dict[str, dict[str, Any]]:
    return {
        f"{name}.schema.json": schema_for(name)
        for name in SCHEMA_MODELS
    }


def export_schema(output_dir: Path) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    exported: dict[str, Path] = {}
    for filename, schema in all_schemas().items():
        output_path = output_dir / filename
        output_path.write_text(
            json.dumps(schema, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        exported[filename] = output_path
    return exported


if __name__ == "__main__":
    export_schema(Path(__file__).resolve().parents[2] / "json-schema")
