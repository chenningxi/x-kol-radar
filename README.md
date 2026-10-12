# X KOL 增量监控与翻译发送

一个面向本地 macOS 的 X/Twitter KOL 监控示例：通过 OpenCLI Browser Bridge 抓取公开推文，使用本地 JSONL 台账去重，再通过 `lark-cli` 将已处理内容发送到飞书群。非中文推文的翻译由使用者自行配置 Codex 定时任务，本项目不绑定任何特定 Codex 账号、任务或窗口。

## 特性

- 多账号监控，支持别名和单账号抓取上限；
- URL/推文 ID 去重和增量游标；
- 原文与中文翻译同时保留；
- 翻译和飞书发送解耦，未完成翻译的内容不会发送；
- 发送台账支持幂等，历史内容可以建立基线而不补发；
- 不包含发帖、点赞、关注、转推、收藏或回复操作。

## 工作流

```text
整点 launchd：抓取 X → tweet_ledger.jsonl
每小时 05 分 Codex：翻译待处理记录并回写台账
每小时 10 分 launchd：发送 translated / no_translation_needed 记录到飞书
```

## 外部依赖与职责

本项目本身负责本地数据流转、增量去重、翻译状态判断和飞书发送；以下能力由使用者自行安装和配置：

| 依赖 | 用途 | 配置方式 |
|---|---|---|
| OpenCLI | 调用 X 推文读取命令 | 安装 `opencli` 并确保它在 `PATH` 中 |
| Browser Bridge 扩展 | 让 OpenCLI 使用浏览器会话 | 在 Chrome/Chromium 中安装并启用扩展 |
| X 登录会话 | 访问 X 的公开推文 | 在同一个 Chrome/Chromium profile 中登录 X |
| Python 3.10+ | 运行本项目脚本 | `python3` 或 `X_KOL_PYTHON` |
| lark-cli | 向飞书群发送消息 | 配置自己的 profile、机器人权限和群 ID |
| Codex 定时任务 | 翻译非中文推文 | 使用者自行创建，目标是本地项目的 `data/tweet_ledger.jsonl` |

项目不会自动安装扩展、登录 X、创建飞书机器人，也不会自动创建 Codex 任务。

## 安装前提

- macOS；
- 已登录 X 的 Chrome/Chromium；
- OpenCLI 和 Browser Bridge 扩展；
- Python 3.10+；
- 已配置 `lark-cli`（仅在需要飞书发送时）。

## 初始化

```bash
cp data/x_profiles.example.md data/x_profiles.md
cp .env.example .env
```

编辑 `data/x_profiles.md` 添加监控账号。不要把真实 `.env`、台账、日志、截图或浏览器凭据提交到公开仓库。

添加账号：

```bash
python3 src/x_kol_sync/sync_x_kols.py add-profile \
  --workspace "$PWD" \
  'https://x.com/example' \
  --alias '示例账号' \
  --limit 30
```

## 手动运行

先检查 Browser Bridge：

```bash
opencli doctor
```

只抓取：

```bash
X_KOL_WORKSPACE="$PWD" python3 scripts/run_x_kol_dispatch.py sync
```

只发送已完成翻译的内容：

```bash
set -a; . ./.env; set +a
X_KOL_WORKSPACE="$PWD" python3 scripts/run_x_kol_dispatch.py dispatch
```

## Codex 翻译任务（由使用者自行配置）

建议用户创建一个独立的 Codex 定时任务，按自己的账号、模型和调度偏好运行。任务只处理 `data/tweet_ledger.jsonl`，将非中文推文的译文写回同一条记录，至少维护：

- `translation_status`: `translated`、`no_translation_needed` 或 `failed`；
- `translation_zh`: 中文译文；
- `translation_updated_at`；
- `source_language`。

Codex 不访问 X、不使用 Browser Bridge、不发送飞书，也不修改监控名单。

可直接作为 Codex 任务提示词的模板：

```text
在项目 <WORKSPACE> 中，只处理 data/tweet_ledger.jsonl 里的待翻译推文。
保留原文，按推文 URL/ID 关联，将非中文推文翻译成中文并写回同一条 JSONL 记录。
中文推文标记为 no_translation_needed；成功翻译标记为 translated，并写入 translation_zh；失败标记为 failed 并记录 translation_error。
不要访问 X，不要使用 Browser Bridge，不要发送飞书，不要修改监控名单，不要执行任何 X 互动操作。
``` 

## macOS launchd

项目中的 `scripts/` 提供入口脚本，同步核心代码位于 `src/x_kol_sync/`，不依赖 Codex Skill 目录。公开仓库提供了 `launchd/` 下的通用 plist 模板，不包含个人路径、真实飞书群 ID 或本机解释器路径。使用者从 GitHub 下载项目后，先替换模板中的占位符：

- `launchd/com.example.x-kol-sync.plist`：每小时整点抓取；
- `launchd/com.example.x-kol-dispatch.plist`：每小时 10 分发送。

云端项目安装示例：

```bash
git clone <公开仓库地址> ~/x-kol-monitor
cd ~/x-kol-monitor
./setup.sh

# 将 launchd/*.plist 中的 /ABSOLUTE/PATH/TO/x-kol-monitor
# 替换为实际项目路径，并填写 Python、OpenCLI 和飞书配置。
cp launchd/com.example.x-kol-sync.plist ~/Library/LaunchAgents/
cp launchd/com.example.x-kol-dispatch.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.x-kol-sync.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.x-kol-dispatch.plist
```

安装前先确认：

```bash
opencli doctor
python3 src/x_kol_sync/sync_x_kols.py init --workspace "$PWD"
```

加载示例：

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.x-kol-sync.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.x-kol-dispatch.plist
```

整点抓取任务需要设置 `X_KOL_WORKSPACE`、`X_KOL_PYTHON`、`X_KOL_OPENCLI`；发送任务另外需要设置 `X_KOL_FEISHU_PROFILE`、`X_KOL_FEISHU_CHAT_ID`。两个任务都应使用 `StartCalendarInterval`，不要使用 `StartInterval`，这样才是固定整点/固定分钟运行。

## 数据文件

| 文件 | 用途 |
|---|---|
| `data/x_profiles.md` | 监控名单和别名 |
| `data/tweet_ledger.jsonl` | 推文原文、翻译状态和译文 |
| `data/x_cursors.json` | 每个账号的增量游标 |
| `data/sync_history.jsonl` | 每次抓取结果 |
| `data/feishu_send_ledger.jsonl` | 飞书发送/跳过台账 |
| `logs/` | launchd 标准输出和错误日志 |

## 安全与隐私

本项目不会保存 Cookie、请求头或令牌。公开发布前请确认：

- 删除真实推文台账和日志；
- 删除聊天截图；
- 替换飞书群 ID、CLI profile 和本机路径；
- 不提交 `.env` 或任何密钥；
- 检查推文正文是否包含不适合公开的内容。

## 开源发布前检查清单

- 确认 `src/x_kol_sync/` 中的同步和抓取代码已作为仓库内代码提交；
- 添加通用的 launchd plist 模板，并确认模板不含个人路径、群 ID 或账号名；
- 添加一个初始化/依赖检查命令，自动创建 `data/`、检查 `opencli`、`lark-cli` 和配置项；
- 在全新目录中完成一次从安装、登录、抓取、翻译到飞书发送的验证；
- 明确 OpenCLI、Browser Bridge、X 登录、lark-cli 和 Codex 均由用户自行安装或配置；
- 检查并删除真实台账、日志、截图、缓存、个人账号和聊天信息；
- 选择并添加许可证；
- 如果希望非 macOS 用户使用，再提供 Linux/Windows 的调度替代方案。

当前项目已经完成脱敏配置化和说明文档初稿，但完成上述检查前，不应宣传为“一键开箱即用”。

## 许可证

发布前请根据维护者意愿补充许可证，例如 MIT、Apache-2.0 或 GPL-3.0。
