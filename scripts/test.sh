#!/usr/bin/env bash
# Run the test suite in the service image.
#
#   ./scripts/test.sh              everything
#   ./scripts/test.sh -k rubric    pytest args are passed through
#
# Needs no Ollama, no GPU and no network: the tests run against recorded pull requests
# in fixtures/prs/ and recorded model output in fixtures/model/.

set -euo pipefail
cd "$(dirname "$0")/.."

docker compose run --rm --no-deps \
  --volume "$PWD/tests:/app/tests:ro" \
  --volume "$PWD/fixtures:/app/fixtures:ro" \
  --volume "$PWD/connect:/app/connect:ro" \
  --volume "$PWD/sql:/app/sql:ro" \
  service python -m pytest tests "$@"
