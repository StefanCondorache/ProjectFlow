#!/usr/bin/env bash
set -euo pipefail
# nightly batch
LOG_LEVEL=info python3 -u -m shop.worker --batch 50
