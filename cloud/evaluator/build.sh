#!/bin/bash
#
# uroad_offline_eval 编译脚本
# 用法：
#   ./build.sh            默认 Release 编译
#   ./build.sh -b debug   调试编译
#   ./build.sh -c         清理 build 目录
#
# 依赖（apt）：libyaml-cpp-dev libeigen3-dev
#
set -e

type=Release
project_dir=$(cd "$(dirname "$0")" && pwd)
build_dir=${project_dir}/build

usage() {
  echo "Usage: $0 [-b debug|release] [-c] [-h]"
  echo "  -b  build type (default: release)"
  echo "  -c  clean ./build, then exit"
  echo "  -h  show this help"
  exit 0
}

while getopts ":b:hc" opt; do
  case $opt in
    b)
      case $OPTARG in
        Release|release) type=Release ;;
        Debug|debug)     type=Debug ;;
        *) echo "Unknown build type: $OPTARG"; usage ;;
      esac
      ;;
    c)
      rm -rf "${build_dir}"
      echo "Cleaned ./build"
      exit 0
      ;;
    h) usage ;;
    *) echo "Invalid option: -$OPTARG"; usage ;;
  esac
done

echo "=========================================="
echo " uroad_offline_eval build (${type})"
echo "=========================================="

cmake -S "${project_dir}" -B "${build_dir}" -GNinja \
    -DCMAKE_BUILD_TYPE=${type}

ninja -C "${build_dir}" -j$(nproc) uroad_offline_eval

echo "=========================================="
echo " Build succeeded: ${build_dir}/uroad_offline_eval"
echo "=========================================="
