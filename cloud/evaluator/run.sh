#!/bin/bash
#
# uroad_offline_eval 运行脚本
# 用法：./run.sh [yaml 配置路径]
#       缺省配置：etc/evaluation_param.yaml
#
set -e

project_dir=$(cd "$(dirname "$0")" && pwd)
bin="${project_dir}/build/uroad_offline_eval"

if [ ! -x "${bin}" ]; then
    echo "Error: ${bin} not found. Run ./build.sh first." >&2
    exit 1
fi

config="${1:-${project_dir}/etc/evaluation_param.yaml}"
shift 2>/dev/null || true

exec "${bin}" "${config}" "$@"
