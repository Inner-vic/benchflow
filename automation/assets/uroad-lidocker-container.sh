#!/usr/bin/env bash
set -euo pipefail

IMAGE="registry.example/bench/build-environment:example"
NAME_PREFIX="bench-eval-lidocker-build-"

die() {
  echo "ERROR: $*" >&2
  exit 2
}

validate_name() {
  [[ "$1" =~ ^${NAME_PREFIX}[A-Za-z0-9._-]+$ ]] || die "invalid container name"
}

start_container() {
  [[ $# -eq 2 ]] || die "usage: $0 start SOURCE_DIR CONTAINER_NAME"
  local source_dir container_name docker_config conan_data auth decoded username password env_file container_id
  source_dir="$(realpath "$1")"
  container_name="$2"
  validate_name "$container_name"
  [[ "$source_dir" == "$HOME"/* ]] || die "source must be below the current user's home directory"
  [[ -f "$source_dir/build.sh" && -f "$source_dir/conanfile.py" ]] || die "source is not a uroad build tree"
  [[ ! -e "$source_dir/build" && ! -e "$source_dir/install" ]] || die "source must be a fresh build tree without build/ or install/"
  docker container inspect "$container_name" >/dev/null 2>&1 && die "container already exists: $container_name"

  docker_config="$HOME/.docker/config.json"
  conan_data="$HOME/.lidocker/.conan/data"
  [[ -f "$docker_config" ]] || die "Docker login state is missing: $docker_config"
  [[ -d "$conan_data" ]] || die "LiDocker Conan cache is missing: $conan_data"
  command -v yq >/dev/null || die "yq is required"

  auth="$(yq -r '.auths."registry.example".auth // ""' "$docker_config")"
  [[ -n "$auth" ]] || die "Artifactory Docker authentication is not initialized"
  decoded="$(printf '%s' "$auth" | base64 -d)" || die "cannot decode Docker authentication"
  username="${decoded%%:*}"
  password="${decoded#*:}"
  [[ -n "$username" && "$password" != "$decoded" ]] || die "invalid Docker authentication record"
  printf '%s' "$password" | docker login registry.example --username "$username" --password-stdin >/dev/null

  umask 077
  env_file="$(mktemp /tmp/bench-eval-lidocker-env.XXXXXX)"
  trap 'rm -f "${env_file:-}"; unset auth decoded username password' EXIT
  {
    printf 'CONAN_LOGIN_USERNAME=%s\n' "$username"
    printf 'CONAN_PASSWORD=%s\n' "$password"
    printf 'ARTIFACTORY_USER=%s\n' "$username"
    printf 'ARTIFACTORY_PASS=%s\n' "$password"
  } >"$env_file"

  container_id="$(docker run -d --pull=never \
    --name "$container_name" \
    --privileged \
    --net=host \
    --shm-size=300M \
    --workdir /root/workspace/apps \
    --env-file "$env_file" \
    --volume "$source_dir:/root/workspace/apps" \
    --volume "$conan_data:/root/.conan/data" \
    "$IMAGE" tail -f /dev/null)"
  docker exec "$container_name" git config --global --add safe.directory /root/workspace/apps
  printf 'container=%s\nimage=%s\nimage_id=%s\n' \
    "$container_name" "$IMAGE" "$(docker image inspect "$IMAGE" --format '{{.Id}}')"
  [[ -n "$container_id" ]] || die "container did not start"
}

stop_container() {
  [[ $# -eq 1 ]] || die "usage: $0 stop CONTAINER_NAME"
  validate_name "$1"
  docker container inspect "$1" >/dev/null 2>&1 || die "container does not exist: $1"
  docker rm -f "$1" >/dev/null
  printf 'removed=%s\n' "$1"
}

[[ $# -ge 1 ]] || die "usage: $0 {start SOURCE_DIR CONTAINER_NAME|stop CONTAINER_NAME}"
action="$1"
shift
case "$action" in
  start) start_container "$@" ;;
  stop) stop_container "$@" ;;
  *) die "unknown action: $action" ;;
esac
