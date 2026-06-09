#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

RUN_ROOT_TEST_PDF_ACCEPTANCE_GATE=1 \
  .venv/bin/python -m pytest \
  services/api/tests/test_local_pipeline.py::test_root_test_pdf_first_four_pages_deterministic_acceptance_gate \
  -q
