#!/usr/bin/env bash
# Example client for the firmware static-library trimming API (POST /trim).
#
# Usage:
#   ./scripts/example_trim.sh <service-base-url> <main.o> <hooks> <lib1.a> [lib2.a ... lib6.a]
# e.g.
#   ./scripts/example_trim.sh http://localhost:8000 build/main.o \
#       board_hook,board_early_hook libplat.a libboard.a liblegacy.a
#
# The libraries MUST be passed in the exact order of the original link
# command (at most six). The response JSON lists the shortest ordered library
# list that still links while keeping every hook provided by its original
# object file, plus the provider of each hook.
set -euo pipefail

BASE=${1:?base url required}
MAIN=${2:?main .o required}
HOOKS=${3:?comma-separated hook names required}
shift 3

if [ "$#" -lt 1 ] || [ "$#" -gt 6 ]; then
  echo "error: between 1 and 6 .a libraries required (got $#)" >&2
  exit 2
fi

lib_args=()
for lib in "$@"; do
  lib_args+=(-F "libraries=@${lib}")
done

echo "POST ${BASE}/trim  main=${MAIN}  hooks=${HOOKS}"
echo "libraries (original order): $*"
curl -sS -X POST "${BASE}/trim" \
  -F "main_object=@${MAIN}" \
  -F "hooks=${HOOKS}" \
  "${lib_args[@]}" | python3 -m json.tool
