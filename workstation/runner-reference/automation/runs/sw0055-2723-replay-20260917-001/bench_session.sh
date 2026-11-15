#!/bin/sh
set -eu

RUN_ID=sw0055-2723-replay-20260917-001
RUN_ROOT=/app/livc-test/$RUN_ID
CANDIDATE=$RUN_ROOT/candidate/livc
SOURCE_ARCHIVE=/userdata/bench-eval-stage/sw0055-2723-deploy-20260917-001/incoming/candidate.app.tar.gz
SOURCE_ARCHIVE_SHA=dfc4431656893db3522280a266d306838023a32007f74325e5dfa99fa1b600ff
SCENE=2026-07-07_10-06-47_HLX33B166R1997378
SCENE_ROOT=$RUN_ROOT/eval_replay_scenes
OUTPUT_ROOT=$RUN_ROOT/outputs/eval_pred_result
FORMAL=/app/livc
BACKUP=$RUN_ROOT/formal-backup/livc
SMCTL=/framework/bin/liware.tool.smctl
FORMAL_SERVICE=infer_expert_fusion
TEST_SERVICE=infer_expert_pcm_replay
EVAL_LIB_SHA=${EVAL_LIB_SHA:-85fb3387e399213b39a0f6c1105d62c718210e94150463bd941578911d3bab14}
EVAL_LIB_SOURCE=${EVAL_LIB_SOURCE:-/tmp/sw0055-2723-liburoad-eval.so}
XCAMERA_LIB_SHA=${XCAMERA_LIB_SHA:-2c8c1318cd2e3bcd4bb989e7055153fffaef42dfbc617bb47bd3897401f04a88}
XCAMERA_LIB_SOURCE=${XCAMERA_LIB_SOURCE:-/tmp/sw0055-2723-libxcamera.so}
XLIDAR_LIB_SHA=${XLIDAR_LIB_SHA:-139089feac395540f62991effc36caa5d33c403e455b0d58017edcde01b102ad}
XLIDAR_LIB_SOURCE=${XLIDAR_LIB_SOURCE:-/tmp/sw0055-2723-libxlidar.so}
LIDAR_PORT=${LIDAR_PORT:-50054}
export LD_LIBRARY_PATH=/framework/lib:/usr/lib

is_active() {
  test "$("$SMCTL" is-active "$1" 2>/dev/null || true)" = active
}

wait_active() {
  name=$1
  limit=$2
  i=0
  while test "$i" -lt "$limit"; do
    is_active "$name" && return 0
    sleep 2
    i=$((i + 1))
  done
  return 1
}

wait_inactive() {
  name=$1
  limit=$2
  i=0
  while test "$i" -lt "$limit"; do
    ! is_active "$name" && return 0
    sleep 1
    i=$((i + 1))
  done
  return 1
}

restore() {
  "$SMCTL" unloadsdf "$TEST_SERVICE" >/dev/null 2>&1 || true
  wait_inactive "$TEST_SERVICE" 30 || true
  old_selinux=$(getenforce)
  trap 'test "$old_selinux" = Enforcing && setenforce 1 >/dev/null 2>&1 || true' EXIT HUP INT TERM
  test "$old_selinux" = Enforcing && setenforce 0
  if test -L "$FORMAL"; then
    test "$(readlink "$FORMAL")" = "$CANDIDATE"
    unlink "$FORMAL"
  fi
  if test -d "$BACKUP"; then
    test ! -e "$FORMAL"
    mv "$BACKUP" "$FORMAL"
  fi
  chmod 0700 /app/livc-test 2>/dev/null || true
  restorecon -RF "$FORMAL" >/dev/null 2>&1 || true
  test "$old_selinux" = Enforcing && setenforce 1
  trap - EXIT HUP INT TERM
  if ! is_active "$FORMAL_SERVICE"; then
    "$SMCTL" loadsdf /app/livc/sdf/infer_expert_fusion.sdf
  fi
  wait_active "$FORMAL_SERVICE" 90
  "$SMCTL" status "$FORMAL_SERVICE"
}

step=${1:-}
case "$step" in
  prepare)
    test ! -e "$RUN_ROOT"
    test -f "$SOURCE_ARCHIVE"
    printf '%s  %s\n' "$SOURCE_ARCHIVE_SHA" "$SOURCE_ARCHIVE" | sha256sum -c -
    test -f /tmp/sw0055-2723-prepare-config.py
    test -f /tmp/sw0055-2723-replay.sdf
    test -f /tmp/sw0055-2723-run-candidate.sh
    old_selinux=$(getenforce)
    trap 'test "$old_selinux" = Enforcing && setenforce 1 >/dev/null 2>&1 || true' EXIT HUP INT TERM
    test "$old_selinux" = Enforcing && setenforce 0
    umask 022
    mkdir -p "$CANDIDATE" "$RUN_ROOT/config-source" "$SCENE_ROOT/$SCENE" "$OUTPUT_ROOT" "$RUN_ROOT/scripts" "$RUN_ROOT/logs" "$RUN_ROOT/candidate"
    tar -xzf "$SOURCE_ARCHIVE" -C "$RUN_ROOT/candidate"
    test -x "$CANDIDATE/bin/infer_expert_pcm"
    mv "$CANDIDATE/etc/uroad/config/common_param.yaml" "$RUN_ROOT/config-source/common_param.yaml"
    python3 /tmp/sw0055-2723-prepare-config.py \
      --source "$RUN_ROOT/config-source/common_param.yaml" \
      --output "$CANDIDATE/etc/uroad/config/common_param.yaml" \
      --scene-root "$SCENE_ROOT" \
      --output-dir "$OUTPUT_ROOT" \
      --eval-mode preserve
    cp /tmp/sw0055-2723-replay.sdf "$RUN_ROOT/candidate/infer_expert_pcm_replay.sdf"
    cp /tmp/sw0055-2723-run-candidate.sh "$RUN_ROOT/scripts/run-candidate.sh"
    chmod 0755 "$RUN_ROOT/scripts/run-candidate.sh"
    chown -R root:root "$CANDIDATE" "$RUN_ROOT/candidate/infer_expert_pcm_replay.sdf" "$RUN_ROOT/scripts"
    chown infer_expert:infer_expert "$OUTPUT_ROOT" "$RUN_ROOT/logs"
    chmod 0755 "$RUN_ROOT" "$RUN_ROOT/candidate" "$CANDIDATE" "$RUN_ROOT/scripts" "$RUN_ROOT/logs" "$SCENE_ROOT" "$SCENE_ROOT/$SCENE" "$OUTPUT_ROOT"
    restorecon -RF "$RUN_ROOT" >/dev/null 2>&1 || true
    test "$old_selinux" = Enforcing && setenforce 1
    trap - EXIT HUP INT TERM
    printf '%s  %s\n' e4c48e137273525d43ca7da67eed4f03707a06220c512607c8783ede4237bee7 "$CANDIDATE/bin/infer_expert_pcm" | sha256sum -c -
    printf '%s  %s\n' 6f667207f4d3e3e85832b5f6067fecc56f8d9d6534fb6ec88904e32cd374d52b "$CANDIDATE/lib/libxcamera.so" | sha256sum -c -
    printf '%s  %s\n' bb00041ca9692299aada9da71bfc3281fd82215bd8e33d4875a07c1b1c9c43f5 "$CANDIDATE/lib/libxlidar.so" | sha256sum -c -
    printf '%s  %s\n' dbe0bcad1452da8cdf51cb68bf3472f193b102f84e753724e8a6ba0d2732885a "$CANDIDATE/lib/liburoad_m100.so" | sha256sum -c -
    grep -A2 '^replay:' "$CANDIDATE/etc/uroad/config/common_param.yaml"
    grep -A3 '^eval_dump:' "$CANDIDATE/etc/uroad/config/common_param.yaml"
    echo PREPARE_OK
    ;;
  install_replay_libs)
    test -d "$CANDIDATE"
    printf '%s  %s\n' "$XCAMERA_LIB_SHA" "$XCAMERA_LIB_SOURCE" | sha256sum -c -
    printf '%s  %s\n' "$XLIDAR_LIB_SHA" "$XLIDAR_LIB_SOURCE" | sha256sum -c -
    old_selinux=$(getenforce)
    trap 'test "$old_selinux" = Enforcing && setenforce 1 >/dev/null 2>&1 || true' EXIT HUP INT TERM
    test "$old_selinux" = Enforcing && setenforce 0
    if test ! -d "$RUN_ROOT/package-libs"; then
      mkdir -p "$RUN_ROOT/package-libs"
      cp -a "$CANDIDATE/lib/libxcamera.so" "$RUN_ROOT/package-libs/libxcamera.so"
      cp -a "$CANDIDATE/lib/libxlidar.so" "$RUN_ROOT/package-libs/libxlidar.so"
    fi
    cp "$XCAMERA_LIB_SOURCE" "$CANDIDATE/lib/libxcamera.so.partial"
    cp "$XLIDAR_LIB_SOURCE" "$CANDIDATE/lib/libxlidar.so.partial"
    chown root:root "$CANDIDATE/lib/libxcamera.so.partial" "$CANDIDATE/lib/libxlidar.so.partial"
    chmod 0644 "$CANDIDATE/lib/libxcamera.so.partial" "$CANDIDATE/lib/libxlidar.so.partial"
    mv "$CANDIDATE/lib/libxcamera.so.partial" "$CANDIDATE/lib/libxcamera.so"
    mv "$CANDIDATE/lib/libxlidar.so.partial" "$CANDIDATE/lib/libxlidar.so"
    restorecon "$CANDIDATE/lib/libxcamera.so" "$CANDIDATE/lib/libxlidar.so" >/dev/null 2>&1 || true
    test "$old_selinux" = Enforcing && setenforce 1
    trap - EXIT HUP INT TERM
    printf '%s  %s\n' "$XCAMERA_LIB_SHA" "$CANDIDATE/lib/libxcamera.so" | sha256sum -c -
    printf '%s  %s\n' "$XLIDAR_LIB_SHA" "$CANDIDATE/lib/libxlidar.so" | sha256sum -c -
    if LD_LIBRARY_PATH="/usr/lib:/framework/lib:/app/eds/lib:$CANDIDATE/lib:/app/ad3/lib" ldd "$CANDIDATE/bin/infer_expert_pcm" 2>&1 | grep -q 'not found'; then
      echo UNRESOLVED_DEPENDENCIES >&2
      exit 30
    fi
    echo REPLAY_LIBS_INSTALLED
    ;;
  install_eval_lib)
    test -d "$CANDIDATE"
    printf '%s  %s\n' "$EVAL_LIB_SHA" "$EVAL_LIB_SOURCE" | sha256sum -c -
    old_selinux=$(getenforce)
    trap 'test "$old_selinux" = Enforcing && setenforce 1 >/dev/null 2>&1 || true' EXIT HUP INT TERM
    test "$old_selinux" = Enforcing && setenforce 0
    mkdir -p "$RUN_ROOT/package-libs"
    if test ! -f "$RUN_ROOT/package-libs/liburoad_m100.so"; then
      cp -a "$CANDIDATE/lib/liburoad_m100.so" "$RUN_ROOT/package-libs/liburoad_m100.so"
    fi
    cp "$EVAL_LIB_SOURCE" "$CANDIDATE/lib/liburoad_m100.so.partial"
    chown root:root "$CANDIDATE/lib/liburoad_m100.so.partial"
    chmod 0644 "$CANDIDATE/lib/liburoad_m100.so.partial"
    mv "$CANDIDATE/lib/liburoad_m100.so.partial" "$CANDIDATE/lib/liburoad_m100.so"
    find "$OUTPUT_ROOT" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
    rm -f "$RUN_ROOT/outputs/diag-events.log"
    rm -f "$RUN_ROOT/logs/candidate.stdout.log"
    chown infer_expert:infer_expert "$RUN_ROOT/outputs" "$OUTPUT_ROOT"
    chmod 0755 "$RUN_ROOT/outputs" "$OUTPUT_ROOT"
    restorecon "$CANDIDATE/lib/liburoad_m100.so" "$OUTPUT_ROOT" >/dev/null 2>&1 || true
    test "$old_selinux" = Enforcing && setenforce 1
    trap - EXIT HUP INT TERM
    printf '%s  %s\n' "$EVAL_LIB_SHA" "$CANDIDATE/lib/liburoad_m100.so" | sha256sum -c -
    if LD_LIBRARY_PATH="/usr/lib:/framework/lib:/app/eds/lib:$CANDIDATE/lib:/app/ad3/lib" ldd "$CANDIDATE/bin/infer_expert_pcm" 2>&1 | grep -q 'not found'; then
      echo UNRESOLVED_DEPENDENCIES >&2
      exit 31
    fi
    echo EVAL_LIB_INSTALLED
    ;;
  configure_offline)
    test -d "$CANDIDATE"
    cfg="$CANDIDATE/etc/uroad/config/common_param.yaml"
    test -f "$cfg"
    test -f /tmp/sw0055-2723-run-candidate.sh
    old_selinux=$(getenforce)
    trap 'test "$old_selinux" = Enforcing && setenforce 1 >/dev/null 2>&1 || true' EXIT HUP INT TERM
    test "$old_selinux" = Enforcing && setenforce 0
    if test ! -f "$RUN_ROOT/config-source/common_param.before-offline-gates.yaml"; then
      cp -a "$cfg" "$RUN_ROOT/config-source/common_param.before-offline-gates.yaml"
    fi
    cp /tmp/sw0055-2723-run-candidate.sh "$RUN_ROOT/scripts/run-candidate.sh"
    chown root:root "$RUN_ROOT/scripts/run-candidate.sh"
    chmod 0755 "$RUN_ROOT/scripts/run-candidate.sh"
    sed -i '/^bench_mode:/,/^[^[:space:]#]/{s/^[[:space:]]*enable: False/  enable: True/}' "$cfg"
    sed -i '/^cfgwd:/,/^[^[:space:]#]/{s/^[[:space:]]*enable: ON/  enable: OFF/}' "$cfg"
    restorecon "$cfg" >/dev/null 2>&1 || true
    test "$old_selinux" = Enforcing && setenforce 1
    trap - EXIT HUP INT TERM
    sed -n '/^bench_mode:/,/^[^[:space:]#]/p' "$cfg" | grep -q 'enable: True'
    sed -n '/^cfgwd:/,/^[^[:space:]#]/p' "$cfg" | grep -q 'enable: OFF'
    echo OFFLINE_CONFIGURED
    ;;
  activate)
    test -d "$CANDIDATE"
    test ! -e "$BACKUP"
    test -z "$(who 2>/dev/null || true)"
    is_active "$FORMAL_SERVICE"
    ! is_active "$TEST_SERVICE"
    printf '%s  %s\n' "$XCAMERA_LIB_SHA" "$CANDIDATE/lib/libxcamera.so" | sha256sum -c -
    printf '%s  %s\n' "$XLIDAR_LIB_SHA" "$CANDIDATE/lib/libxlidar.so" | sha256sum -c -
    printf '%s  %s\n' "$EVAL_LIB_SHA" "$CANDIDATE/lib/liburoad_m100.so" | sha256sum -c -
    grep -A2 '^replay:' "$CANDIDATE/etc/uroad/config/common_param.yaml" | grep -q 'enable: ON'
    grep -A3 '^eval_dump:' "$CANDIDATE/etc/uroad/config/common_param.yaml" | grep -q 'enable: ON'
    old_selinux=$(getenforce)
    trap 'test "$old_selinux" = Enforcing && setenforce 1 >/dev/null 2>&1 || true' EXIT HUP INT TERM
    test "$old_selinux" = Enforcing && setenforce 0
    mkdir -p "$RUN_ROOT/formal-backup"
    cp /tmp/sw0055-2723-replay.sdf "$RUN_ROOT/candidate/infer_expert_pcm_replay.sdf"
    chmod 0600 "$RUN_ROOT/candidate/infer_expert_pcm_replay.sdf"
    sha256sum "$FORMAL/bin/infer_expert_fusion" "$FORMAL/etc/uroad/config/common_param.yaml" "$FORMAL/lib/liburoad_m100.so" > "$RUN_ROOT/formal-baseline.sha256"
    test "$old_selinux" = Enforcing && setenforce 1
    trap - EXIT HUP INT TERM
    "$SMCTL" unloadsdf "$FORMAL_SERVICE"
    wait_inactive "$FORMAL_SERVICE" 30
    old_selinux=$(getenforce)
    trap 'test "$old_selinux" = Enforcing && setenforce 1 >/dev/null 2>&1 || true' EXIT HUP INT TERM
    test "$old_selinux" = Enforcing && setenforce 0
    chmod 0711 /app/livc-test
    mv "$FORMAL" "$BACKUP"
    ln -s "$CANDIDATE" "$FORMAL"
    chcon --reference="$BACKUP/bin/infer_expert_pcm" "$CANDIDATE/bin/infer_expert_pcm"
    test "$old_selinux" = Enforcing && setenforce 1
    trap - EXIT HUP INT TERM
    "$SMCTL" loadsdf "$RUN_ROOT/candidate/infer_expert_pcm_replay.sdf"
    if ! wait_active "$TEST_SERVICE" 90; then
      tail -200 "$RUN_ROOT/logs/candidate.stdout.log" 2>/dev/null || true
      restore
      exit 20
    fi
    i=0
    while test "$i" -lt 90; do
      if ss -lnt 2>/dev/null | grep -q ':50051 ' && ss -lnt 2>/dev/null | grep -q ":$LIDAR_PORT "; then
        "$SMCTL" status "$TEST_SERVICE"
        ss -lnt 2>/dev/null | grep -E ":(50051|$LIDAR_PORT) "
        echo ACTIVATE_OK
        exit 0
      fi
      sleep 2
      i=$((i + 1))
    done
    echo REPLAY_PORTS_NOT_READY
    tail -300 "$RUN_ROOT/logs/candidate.stdout.log" 2>/dev/null || true
    restore
    exit 21
    ;;
  inspect)
    "$SMCTL" status "$TEST_SERVICE" || true
    echo OUTPUT_FILES=$(find "$OUTPUT_ROOT" -type f 2>/dev/null | wc -l)
    echo PRED_TRAJ_FILES=$(find "$OUTPUT_ROOT" -type f -name 'pred_traj_*.npy' 2>/dev/null | wc -l)
    echo TRAJ_XY_FILES=$(find "$OUTPUT_ROOT" -type f -name 'traj_xy_*.npy' 2>/dev/null | wc -l)
    echo PRED_IMG_FILES=$(find "$OUTPUT_ROOT" -type f -name 'pred_img_*.json' 2>/dev/null | wc -l)
    find "$OUTPUT_ROOT" -type f 2>/dev/null | head -20
    echo LOG_TAIL
    tail -300 "$RUN_ROOT/logs/candidate.stdout.log" 2>/dev/null || true
    ;;
  restore)
    restore
    ;;
  verify_restore)
    test -d "$FORMAL"
    test ! -L "$FORMAL"
    test ! -d "$BACKUP"
    sha256sum -c "$RUN_ROOT/formal-baseline.sha256"
    is_active "$FORMAL_SERVICE"
    ! is_active "$TEST_SERVICE"
    test "$(getenforce)" = Enforcing
    "$SMCTL" status "$FORMAL_SERVICE"
    echo RESTORE_OK
    ;;
  *)
    echo 'usage: bench_session.sh {prepare|install_replay_libs|install_eval_lib|configure_offline|activate|inspect|restore|verify_restore}' >&2
    exit 2
    ;;
esac
