#!/usr/bin/env bash
set -euo pipefail

if ! command -v pip-audit >/dev/null 2>&1; then
  python3 -m pip install --quiet pip-audit
fi

for req in "$@"; do
  echo "pip-audit: $req"
  pip-audit -r "$req"
done
