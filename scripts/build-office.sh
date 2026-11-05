#!/usr/bin/env bash
# Build the forked AIOffice editors (docs/slides/sheets) served at /office.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/office"
if [[ ! -d node_modules ]]; then
  npm install --ignore-scripts --no-audit --no-fund
fi
npm run build
