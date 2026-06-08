#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

bash scripts/visual-regression.sh
.venv/bin/python -m pytest
.venv/bin/python -m compileall packages services
npm run typecheck:web
npm run build:web
