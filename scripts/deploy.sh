#!/usr/bin/env bash
# Run from a release bundle. Only the ATRI service is replaced; NapCat stays up.
set -Eeuo pipefail
umask 077

die() { printf '%s\n' "$*" >&2; exit 1; }
if [[ $# != 1 || ${#1} -gt 80 || ! $1 =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z][0-9A-Za-z.-]*)?$ ]]; then
    die "用法：./deploy.sh 0.1.1（填写明确版本，不接受 latest、路径或镜像地址）"
fi
version=$1
image="ghcr.io/keepingrunning/atri:$version"
deploy_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if [[ ! -f "$deploy_dir/compose.yaml" && -f "$deploy_dir/../compose.yaml" ]]; then
    deploy_dir=$(cd -- "$deploy_dir/.." && pwd)
fi
cd -- "$deploy_dir"
[[ -f compose.yaml && -f .env && ! -L .env ]] || die "请先在部署目录准备 compose.yaml 和普通文件 .env。"
command -v docker >/dev/null || die "找不到 docker。"
command -v python3 >/dev/null || die "找不到 python3；它用于安全更新 .env，不执行其中的内容。"
mkdir .deploy.lock 2>/dev/null || die "另一次部署尚未结束（.deploy.lock）；确认没有部署进程后再清理此目录。"
temporary=''
changed=0
cleanup() {
    if [[ -n "$temporary" ]]; then rm -rf -- "$temporary"; fi
    rmdir .deploy.lock 2>/dev/null || true
}
trap cleanup EXIT
temporary=$(mktemp -d "$deploy_dir/.deploy.XXXXXX")

compose() {
    docker compose --project-directory "$deploy_dir" --env-file "$deploy_dir/.env" \
        -f "$deploy_dir/compose.yaml" "$@"
}

wait_healthy() {
    local container status attempt
    for ((attempt=0; attempt<60; attempt++)); do
        container=$(compose ps -q atri) || return 1
        if [[ -n "$container" ]]; then
            status=$(docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "$container") || return 1
            case "$status" in
                'running healthy') return 0 ;;
                *' missing') printf '%s\n' '容器没有 healthcheck，无法确认升级。' >&2; return 1 ;;
                'exited '*|'dead '*) return 1 ;;
            esac
        fi
        sleep 2
    done
    printf '%s\n' '等待 ATRI 本地健康检查超时（120 秒）。' >&2
    return 1
}

failed() {
    local exit_code=$?
    trap - ERR INT TERM
    if (( changed )); then
        printf '%s\n' '升级失败，正在恢复升级前的 ATRI 镜像。' >&2
        if compose -f "$temporary/rollback.yaml" up -d --no-deps --pull never --force-recreate atri && wait_healthy; then
            printf '%s\n' '已恢复旧镜像；.env 未更改。请查看 ATRI 日志后再尝试升级。' >&2
        else
            printf '%s\n' '自动回滚未恢复健康，请按 .deploy-last.json 中的旧镜像 ID 手动恢复，并核查数据备份。' >&2
        fi
    else
        printf '%s\n' '更新失败，尚未替换 ATRI 容器。' >&2
    fi
    if (( exit_code == 0 )); then exit_code=1; fi
    exit "$exit_code"
}
trap failed ERR INT TERM

compose config --quiet
old_container=$(compose ps -aq atri)
[[ "$old_container" =~ ^[0-9a-f]{12,64}$ ]] || die "没有唯一的 ATRI 容器；首次安装请运行 docker compose up -d。"
old_image=$(docker inspect --format '{{.Image}}' "$old_container")
[[ "$old_image" =~ ^sha256:[0-9a-f]{64}$ ]] || die "无法取得旧镜像 ID，未开始更新。"
printf 'services:\n  atri:\n    image: "%s"\n' "$old_image" > "$temporary/rollback.yaml"
printf '{"previous_image_id":"%s","target_image":"%s","started_at":"%s"}\n' \
    "$old_image" "$image" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$temporary/record.json"
mv -- "$temporary/record.json" .deploy-last.json

printf '拉取 %s\n' "$image"
ATRI_VERSION="$version" compose pull atri
changed=1
ATRI_VERSION="$version" compose up -d --no-deps --pull never --force-recreate atri
wait_healthy

# Keep unrelated values byte-for-byte, remove duplicate version keys, and replace
# atomically. In particular, never source .env or expand its values in a shell.
python3 - "$deploy_dir/.env" "$version" <<'PY'
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

path, version = Path(sys.argv[1]), sys.argv[2]
original = path.read_bytes()
replacement = f"ATRI_VERSION={version}\n".encode()
pattern = re.compile(rb"^\s*(?:export\s+)?ATRI_VERSION\s*=")
lines, replaced = [], False
for line in original.splitlines(keepends=True):
    if pattern.match(line):
        if not replaced:
            lines.append(replacement)
            replaced = True
    else:
        lines.append(line)
if not replaced:
    if lines and not lines[-1].endswith(b"\n"):
        lines.append(b"\n")
    lines.append(replacement)
fd, temporary = tempfile.mkstemp(prefix=".env.", dir=path.parent)
try:
    with os.fdopen(fd, "wb") as stream:
        os.fchmod(stream.fileno(), stat.S_IMODE(path.stat().st_mode))
        stream.write(b"".join(lines))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
changed=0
trap - ERR INT TERM
printf 'ATRI 已更新到 %s，本地健康检查通过；NapCat 未重建。\n' "$version"
printf '%s\n' '请另行检查 /healthz 的 connected 字段；此检查不验证模型 API 或 MCP。'
