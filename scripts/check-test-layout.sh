#!/usr/bin/env bash
#
# PublishHub — test file layout check.
#
# Enforces one rule: test files live in a dedicated test directory, never
# beside the production source they exercise.
#
#   TypeScript   *.test.ts / *.test.tsx   must sit in a `testing/` or
#                                         `__tests__/` directory
#   Python       test_*.py / *_test.py    must sit under a `tests/` directory
#
# Both conventions already exist in this repo and neither needs a test-runner
# config change: apps/api/vitest.config.ts includes `src/**/*.test.ts` (any
# depth) and apps/worker/pytest.ini sets `testpaths = tests`.
#
# Usage:
#   scripts/check-test-layout.sh                # scan the whole repository
#   scripts/check-test-layout.sh FILE [FILE..]  # check specific files only
#
# Exit codes:
#   0  no misplaced test files
#   2  at least one misplaced test file (stderr names each one and the fix)
#
# Exit code 2 is deliberate: Kiro hooks treat 2 as "block this action" and
# forward stderr, so the same script works as a hook and as a manual check.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 1

# Directory names that count as a dedicated test location.
TS_TEST_DIR_RE='(^|/)(testing|__tests__)/'
PY_TEST_DIR_RE='(^|/)tests/'

violations=()

is_ignored_path() {
	case "$1" in
	*/node_modules/* | node_modules/*) return 0 ;;
	*/.venv/* | .venv/*) return 0 ;;
	*/dist/* | dist/*) return 0 ;;
	*/build/* | build/*) return 0 ;;
	*/.git/* | .git/*) return 0 ;;
	*/.pytest_cache/* | .pytest_cache/*) return 0 ;;
	*/__pycache__/* | __pycache__/*) return 0 ;;
	esac
	return 1
}

check_path() {
	local path="${1#./}"
	local dir base

	is_ignored_path "$path" && return 0

	dir="$(dirname "$path")"
	base="$(basename "$path")"

	case "$base" in
	*.test.ts | *.test.tsx)
		if [[ ! "$path" =~ $TS_TEST_DIR_RE ]]; then
			violations+=("$path"$'\t'"move to ${dir}/testing/${base}")
		fi
		;;
	test_*.py | *_test.py)
		if [[ ! "$path" =~ $PY_TEST_DIR_RE ]]; then
			violations+=("$path"$'\t'"move under the package's tests/ directory")
		fi
		;;
	esac
}

if [ "$#" -gt 0 ]; then
	for arg in "$@"; do
		# Accept absolute paths from hook payloads by making them repo-relative.
		check_path "${arg#"$REPO_ROOT"/}"
	done
else
	while IFS= read -r found; do
		check_path "$found"
	done < <(
		find . \
			\( -name node_modules -o -name .venv -o -name dist -o -name build \
			-o -name .git -o -name .pytest_cache -o -name __pycache__ \) -prune \
			-o -type f \
			\( -name '*.test.ts' -o -name '*.test.tsx' \
			-o -name 'test_*.py' -o -name '*_test.py' \) -print
	)
fi

if [ "${#violations[@]}" -eq 0 ]; then
	printf 'test layout OK: every test file is in a dedicated test directory\n'
	exit 0
fi

{
	printf 'Misplaced test files (%d) — tests must not sit beside production source:\n\n' "${#violations[@]}"
	printf '%s\n' "${violations[@]}" | sort | awk -F'\t' '{ printf "  %s\n      -> %s\n", $1, $2 }'
	printf '\nConvention: TypeScript tests belong in a testing/ (or __tests__/)\n'
	printf 'directory; Python tests belong under tests/. Move the file and update\n'
	printf 'its relative imports.\n'
} >&2

exit 2
