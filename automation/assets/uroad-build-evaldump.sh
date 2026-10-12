#!/usr/bin/env bash
set -euo pipefail

BASELOCK_REF="${BASELOCK_REF:-example_toolchain/1.0@example/stable}"

echo "[build] source=$(pwd) baselock=${BASELOCK_REF}"
./build.sh \
  -t schu \
  -b release \
  -o platform=schu \
  -v "${BASELOCK_REF}" \
  2>&1 | tee build-evaldump.log

echo '[verify]'
grep -E '^WITH_EVAL_DUMP(:BOOL)?=(ON|TRUE|True|1)$' build/CMakeCache.txt
test -f install/lib/liburoad_m100.so
file install/lib/liburoad_m100.so
sha256sum install/lib/liburoad_m100.so
readelf -h install/lib/liburoad_m100.so | grep -E 'Class:|Machine:'
