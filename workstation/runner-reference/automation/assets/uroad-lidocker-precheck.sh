#!/usr/bin/env bash
set -euo pipefail

echo '[toolchain]'
conan --version
cmake --version | head -n 1
ninja --version

echo '[conan-profiles]'
conan profile list

echo '[source]'
git rev-parse HEAD
git diff --check
grep -n 'WITH_EVAL_DUMP' conanfile.py CMakeLists.txt
