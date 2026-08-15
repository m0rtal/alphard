#!/usr/bin/env bash
# Local Phase 1.2 gate runner — replaces GitHub Actions for environments
# where Actions infrastructure is unavailable. Runs:
#   1. pytest with coverage threshold 80% (integration tests skipped without DSN)
#   2. black format check
#   3. flake8 lint
#   4. mypy type check
#
# Usage:
#   ./scripts/ci_local.sh              # full gate
#   PG_DSN=... ./scripts/ci_local.sh  # with integration tests
#
# Exit code: 0 = green, non-zero = failure.

set -uo pipefail

cd "$(dirname "$0")/.."

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PASSED=0
FAILED=0

run_step() {
    local name="$1"
    shift
    echo
    echo -e "${YELLOW}━━━ $name ━━━${NC}"
    if "$@"; then
        echo -e "${GREEN}✓ $name PASSED${NC}"
        PASSED=$((PASSED + 1))
        return 0
    else
        echo -e "${RED}✗ $name FAILED${NC}"
        FAILED=$((FAILED + 1))
        return 1
    fi
}

# Step 1: pytest with coverage
run_step "pytest + coverage" python3 -m pytest tests/ \
    --cov=src \
    --cov-fail-under=80 \
    --cov-report=term-missing \
    -q

# Step 2: black format check
run_step "black format check" python3 -m black --check src/ tests/

# Step 3: flake8
run_step "flake8 lint" python3 -m flake8 src/ tests/ \
    --max-line-length=120 \
    --extend-ignore=E203,W503

# Step 4: mypy
run_step "mypy type check" python3 -m mypy src/ --ignore-missing-imports

echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
if [ "$FAILED" -eq 0 ]; then
    echo -e "${GREEN}✓ ALL $PASSED GATES PASSED${NC}"
    exit 0
else
    echo -e "${RED}✗ $FAILED of $((PASSED + FAILED)) GATES FAILED${NC}"
    exit 1
fi
