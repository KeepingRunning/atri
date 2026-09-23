# 服务器部署与更新

部署包将 ATRI 和 NapCat 放在同一 Docker Compose 网络里。首次部署需要登录 QQ、填写模型密钥；后续更新只替换 ATRI 镜像，配置、人设、聊天存档和 NapCat 登录资料均留在服务器。当前没有已配置的远程服务器，发布流程不会自动修改任何服务器。

## 准备服务器与部署包

需要 Linux `amd64` 或 `arm64` 服务器、Docker Engine、Docker Compose v2 插件，以及 Bash、Python 3。两项容器镜像均提供这两种 Linux 架构；由 Docker 自动选择，不要把本机 macOS 的 Python 环境复制过去。操作账号需要 Docker 权限，它等同于较高的宿主机权限。

从 [v0.1.0 Release](https://github.com/KeepingRunning/atri/releases/tag/v0.1.0) 下载 `atri-0.1.0-deploy.tar.gz` 与 `SHA256SUMS`，放在同一目录：

```sh
sha256sum -c SHA256SUMS
tar -xzf atri-0.1.0-deploy.tar.gz
cd atri-0.1.0
cp .env.example .env
cp config.toml.template config.toml
chmod 600 .env config.toml
mkdir -p data napcat/qq napcat/config
```

部署目录包括 `compose.yaml`、`.env.example`、`config.toml.template`、`personal_info.txt`、`deploy.sh` 与本文的副本 `DEPLOYMENT.md`。镜像中的代码和日程素材独立于服务器运行数据；`/opt/atri-mcp` 已安装所需 MCP 包，不在启动时运行 npm 安装。

`.env` 中固定 `ATRI_VERSION=0.1.0`；NapCat 固定 `v4.18.28`，不要改成 `latest`。NapCat 默认 `NAPCAT_UID=1000`、`NAPCAT_GID=1000`，使用已有目录时确保这两个 ID 可以读写 `napcat/qq` 和 `napcat/config`。ATRI 当前以容器 root 运行，需要读配置并写 `data`；不会给容器配置 privileged 或宿主网络。

如果 GHCR 包仍是私有，先执行 `docker login ghcr.io -u YOUR_GITHUB_USERNAME`，在交互提示中输入有 `read:packages` 权限的 GitHub Token。也可以由仓库所有者把该 package 设为公开后匿名拉取。不要把 GitHub Token 填进 ATRI 的模型配置。

## 配置、登录与第一次启动

编辑 `config.toml`，至少填写：

- `[bot]`：`self_id` 为机器人 QQ 号，`allowed_groups` 为允许接入的群。
- `[onebot]`：保留容器监听 `host="0.0.0.0"`、`port=28080` 和路径；`token` 填一个自行生成的非空随机值。
- `[llm]`：模型服务的 `base_url`、`model`、`api_key`，须支持当前 Planner 使用的原生工具调用。

部署模板的链接工具和 MCP 已启用；图片理解与百炼 ASR 默认关闭。需要时填写对应配置再启用，ASR 的地域与 Key 必须一致。容器内模型地址不能用宿主机的 `127.0.0.1`。网络代理、DoH 不会自动沿用本机配置；保留模板默认值，按服务器实际网络调整。修改 `personal_info.txt` 可更新角色设定。

```sh
docker compose pull
docker compose run --rm --no-deps atri check
docker compose up -d
docker compose ps
```

`atri check` 只验证配置，成功不代表模型、MCP 或 QQ 已连通。

NapCat 管理页只绑定服务器 `127.0.0.1:6099`。在自己的电脑建立 SSH 隧道后，打开 `http://127.0.0.1:6099`：

```sh
ssh -N -L 6099:127.0.0.1:6099 YOUR_USER@YOUR_SERVER
```

初次 WebUI 登录入口/口令按 `docker compose logs --tail 100 napcat` 中的提示获取，扫码登录机器人 QQ。WebUI 口令和 OneBot access token 是两回事。在 NapCat 的网络配置中新建并启用 **OneBot v11 WebSocket 客户端／反向 WebSocket（Universal）**：

- 地址：`ws://atri:28080/onebot/v11/ws`。
- access token：与 `config.toml` 的 `[onebot].token` 完全一致。
- 使用已登录的机器人账号，保存配置并确认连接生效。

这里的 `atri` 是 Compose 服务名，不能改成容器内的 `127.0.0.1`。ATRI 的服务端口不需要公开到互联网；WebUI 日志可能包含管理凭据，不要直接贴到公开聊天或 issue。

## 查看状态与维护

```sh
docker compose ps
docker compose logs -f --tail 100 atri
docker compose logs -f --tail 100 napcat
docker compose exec -T atri python -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:28080/healthz").read().decode())'
```

Docker 的 `healthy` 检查本地 `/healthz` 是否返回 HTTP 200；它不要求 QQ 已连接。再看 JSON 的 `connected=true` 才能确认 NapCat 已接入。两者都不探测模型 API 或 MCP；允许的群里也可发送 `/health` 查看本地状态，不产生模型调用。

| 操作 | 命令 |
| --- | --- |
| 修改配置或人设后生效 | `docker compose restart atri` |
| 暂停 ATRI，保留 QQ 登录 | `docker compose stop atri` |
| 恢复 ATRI | `docker compose up -d atri` |
| 停止两项服务 | `docker compose stop` |
| 移除容器但保留宿主文件 | `docker compose down` |

挂载关系如下。`config.toml`、`personal_info.txt` 必须已是文件，Compose 不会替你创建空目录。

| 宿主部署目录 | 容器路径 | 内容 |
| --- | --- | --- |
| `config.toml` | `/app/config.toml`，只读 | QQ 与模型配置、密钥 |
| `personal_info.txt` | `/app/personal_info.txt`，只读 | 角色人设 |
| `data/` | `/app/data/`，可写 | 聊天存档、去重记录、资料缓存与日程状态 |
| `napcat/qq/` | `/app/.config/QQ/`，可写 | QQ 登录资料 |
| `napcat/config/` | `/app/napcat/config/`，可写 | NapCat 与 OneBot 配置 |

## 迁移本机数据与备份

同一份 `data` 只允许一个 ATRI 使用。迁移时先停止旧 ATRI；新旧环境不要同时用同一个机器人账号回复。随后停止旧 NapCat，再复制它的 QQ 与配置目录，保持备份时没有进程写入。如果原来用 Docker named volume，应复制其对应 `/app/.config/QQ` 与 `/app/napcat/config` 的内容到新部署的两个目录；不需要重建旧容器来导出文件。

旧项目的整个 `data/` 可复制到新部署目录，保留 `groups`、去重记录、文档缓存等；新镜像使用 `/opt/atri-mcp`，旧 `data/mcp` 下的 npm 依赖不用迁移。用 SSH/SCP/rsync 传输备份。QQ 登录资料可一并迁移，但新机器上仍可能需要重新扫码。

启用表情包时，也要保留整个 `data/sticker_review/`，包含 `catalog.json` 及其相对图片目录。表情以图片内容交给 NapCat，不需要额外的跨容器图片挂载；发行包不包含本地挑选的素材。功能配置见 [表情包发送](stickers.md)。

启用语音辅助表达时，保留 `data/voice_review/catalog.json`、`selection.json` 和 `audio/*.mp3`，并配置 `[voices]`。音频内容交给 NapCat 转码，不需要两个容器共享音频路径。原始 Opus 和 WAV 审阅副本不属于机器人发送所必需的文件。共享频率及超时见 [辅助表达](supplements.md)。

`personal_info.txt` 可以原样迁移。配置应以服务器模板为基础填写原有值，尤其保留容器的监听地址、MCP 路径和数据路径；不要直接拿本机绝对路径覆盖服务器模板。NapCat 迁移后还要把反向 WebSocket 地址改为前面的 `ws://atri:28080/onebot/v11/ws`。

服务器已在使用时，可在每次升级前做一份包含登录资料的停机备份：

```sh
umask 077
docker compose stop
tar -czf "../atri-backup-$(date +%Y%m%d-%H%M%S).tar.gz" \
  .env config.toml personal_info.txt data napcat
docker compose up -d
```

备份包含 API Key、聊天与 QQ 登录资料，保存在仅管理员可访问的位置。先确认新服务器 `connected=true` 且实际收发正常，再处理旧环境；不要恢复旧 bot 与新 bot 并行运行。

## 升级与回滚

首次启动后，在部署目录运行更新脚本，参数为已发布的明确版本号：

```sh
./deploy.sh 0.1.1
```

`0.1.1` 是后续发布版本的示例，必须先确认对应 Release 和镜像存在。脚本不接受 `latest`、任意镜像地址或 shell 表达式；只拉取 `ghcr.io/keepingrunning/atri:<版本>`，只重建 `atri`，不停止或升级 NapCat。

它先把正在使用的不可变镜像 ID 记录到 `.deploy-last.json`，拉取目标版本，再检查容器本地健康状态，最多等待 120 秒。启动/健康检查失败时用旧 ID 自动重建 ATRI，并保持 `.env` 不变；成功才原子更新 `.env` 中的 `ATRI_VERSION`，不执行或打印其他变量。部署期间不要手动重建容器或清理旧镜像。`.deploy.lock` 用于防止同时运行两次脚本；强制中断遗留时，确认没有部署进程后再删除此锁目录。

需要主动退回已发布的旧版本时，同样执行明确版本：

```sh
./deploy.sh 0.1.0
```

如果自动恢复也失败，查看 ATRI 日志以及 `.deploy-last.json` 的 `previous_image_id`，在部署目录生成仅替换镜像的临时覆盖文件后手动恢复：

```sh
python3 - <<'PY'
import json
from pathlib import Path
record = json.loads(Path('.deploy-last.json').read_text())
Path('rollback.yaml').write_text('services:\n  atri:\n    image: ' + json.dumps(record['previous_image_id']) + '\n')
PY
docker compose -f compose.yaml -f rollback.yaml up -d --no-deps --pull never --force-recreate atri
```

这使用本地旧镜像 ID，要求旧镜像尚未被清理。恢复后再核查健康状态与 QQ 连接；后续正常操作不要带这个临时覆盖文件。

**镜像回滚不会回滚数据格式或已经发送的消息。** 若新版本变更数据结构，先按该版本迁移说明备份，恢复旧镜像时可能还需恢复相匹配的数据备份。本脚本不修改 `config.toml` 或 `personal_info.txt`；新版本增加配置项时，先比较新部署包模板与 Release 说明，保留自己的密钥和人设，不直接覆盖。

仓库推送版本标签后会构建并发布镜像与部署包。当前版本由操作者在服务器执行脚本；后续服务器准备好后再配置 SSH 和 GitHub 手动部署流程。
