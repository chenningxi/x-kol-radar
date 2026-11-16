# X KOL 增量监控与飞书推送

这是一个运行在 macOS 上的 X/Twitter KOL 增量监控工具。它通过 OpenCLI 和已登录的浏览器会话只读抓取公开推文，以 MySQL 保存推文、游标、审核、翻译和发送状态，使用 Redis 防止任务重入，再通过 `lark-cli` 推送到飞书群。

当前版本：`v0.3.0`。采用 MIT 许可证。

项目对 X 严格只读，不会发帖、回复、点赞、关注、转推、收藏或执行其他写操作。

面向外部读者的项目介绍、数据字典和数据使用示例，见[项目说明与数据使用指南](docs/project-and-data-guide.md)。

## 工作流

```text
每 15 分钟启动一轮（每小时 00、15、30、45 分）
        ↓
只读抓取 X，新推文写入 MySQL
        ↓
接着由 Python 直连 DeepSeek，审核和翻译后回写 MySQL
        ↓
接着发送符合条件的内容，并把回执写入 MySQL
```

| 阶段 | 入口 | 作用 |
|---|---|---|
| 自动串联 | `scripts/run_x_kol_db.py pipeline` | 在一轮中连续执行抓取、审核翻译和飞书发送 |
| 抓取 | `scripts/run_x_kol_db.py sync` | 检查数据库游标、抓取并按推文 ID/URL 去重，不发送飞书 |
| 审核与翻译 | `scripts/run_x_kol_db.py review` | 直连 DeepSeek，按仓库内策略处理数据库待办，不访问 X、不发送飞书 |
| 发送 | `scripts/run_x_kol_db.py dispatch` | 只发送当前策略版本审核完成且满足发送条件的记录 |

MySQL 是唯一运行数据源。正式流程不读取或改写 `data/*.jsonl`。项目目录不保留旧台账、迁移备份或临时测试样本，代码版本通过 Git/GitHub 管理。

每轮任务持有同一个运行锁，防止重叠。各阶段仍可单独执行：`sync` 只抓取，`review` 只审核翻译，`dispatch` 只发送。`pipeline` 不等待固定的审核或发送分钟；某阶段发生可恢复错误时，仍尝试处理数据库中其他可用内容，失败记录留到后续轮次。

## 运行条件

| 依赖 | 用途 |
|---|---|
| macOS | 使用 `launchd` 定时运行 |
| Python 3.10+ | 运行项目脚本 |
| MySQL 8+ | 保存全部运行状态和内容 |
| Redis 6+ | 任务互斥锁 |
| OpenCLI + Browser Bridge | 通过浏览器会话只读抓取 X |
| Node.js 20+ | 通过 OpenCLI Browser Bridge 读取指定博主头像 |
| Chrome/Chromium | 需已登录 X 并连接 Browser Bridge |
| lark-cli | 以飞书机器人身份发送群消息 |
| DeepSeek API Key | 审核内容并翻译非中文推文 |

## 快速开始

### 1. 初始化运行环境

```bash
git clone https://github.com/chenningxi/x-kol-radar.git x-kol-radar
cd x-kol-radar
./setup.sh
cp .env.example .env
```

`setup.sh` 创建项目虚拟环境并安装 `requirements.txt`。编辑 `.env`，至少配置 MySQL、Redis、飞书、OpenCLI 和项目路径：

```dotenv
X_KOL_FEISHU_PROFILE=your-lark-cli-profile
X_KOL_FEISHU_CHAT_ID=oc_your_chat_id
X_KOL_WORKSPACE=/absolute/path/to/x-kol-radar
X_KOL_PYTHON=/absolute/path/to/x-kol-radar/.venv/bin/python
X_KOL_OPENCLI=/absolute/path/to/opencli
X_KOL_PATH=/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin

X_KOL_MYSQL_HOST=127.0.0.1
X_KOL_MYSQL_PORT=3306
X_KOL_MYSQL_DATABASE=your_database
X_KOL_MYSQL_USER=your_user
X_KOL_MYSQL_PASSWORD=your_password
X_KOL_REDIS_HOST=127.0.0.1
X_KOL_REDIS_PORT=6379
X_KOL_REDIS_DB=0
X_KOL_REDIS_PASSWORD=
X_KOL_REDIS_PREFIX=xkol:radar
```

先在 MySQL 中创建 `.env` 指定的数据库和专用用户（替换占位数据库名、用户名与密码）：

```sql
CREATE DATABASE your_database CHARACTER SET utf8mb4;
CREATE USER 'your_user'@'localhost' IDENTIFIED BY 'your_password';
GRANT SELECT, INSERT, UPDATE, CREATE ON your_database.* TO 'your_user'@'localhost';
```

MySQL 和 Redis 需处于运行状态，MySQL 用户需能从配置的主机连接。`init` 不创建数据库，只初始化表结构：

```bash
set -a; . ./.env; set +a
.venv/bin/python scripts/run_x_kol_db.py init
```

`init` 只创建缺失的 `xkol_` 表，不清空已有表。

### 2. 配置监控账号

编辑私有文件 `data/x_profiles.md`：

```markdown
| X 账号 | 别名 | 启用 | 单次抓取上限 | 包含转推 | 备注 |
|---|---|---:|---:|---:|---|
| <https://x.com/example> | 示例账号 | 是 | 30 | 否 |  |
```

将账号配置写入 MySQL：

```bash
set -a; . ./.env; set +a
.venv/bin/python scripts/run_x_kol_db.py profiles
```

正式同步只读取 `xkol_profiles` 表。修改 Markdown 名单后需要再次运行 `profiles`；数据库中已有但从名单删除的账号不会被自动删除。

`profiles` 会读取新增或缺少头像的博主主页，下载头像，并将来源、相对文件路径和下载状态写入 `config.avatar`；已有可用头像直接复用。需要浏览器已登录 X 且 Browser Bridge 连接正常。头像读取或下载失败不阻止名单保存，输出中的 `avatars_failed` 表示本次仍未取得头像的数量；再次运行 `profiles` 或使用 `assets` 可补下载。每轮成功同步后也会自动补齐监控账号、推文作者和引用作者的头像，按账号去重下载；引用作者不会因此进入监控名单。更新名单会保留原有头像、游标及未知配置字段。

成功同步后也会读取作者自己的 X 主页身份区，保存认证状态；外层与引用作者分别处理，不根据头像、名字或其他作者推断蓝 V。未知状态留空，确认没有认证时明确保存 `verified=false`。

### 3. 验证与首次同步

```bash
opencli doctor
set -a; . ./.env; set +a
.venv/bin/python scripts/run_x_kol_db.py status
.venv/bin/python scripts/run_x_kol_db.py sync
```

首次同步抓到的内容会进入正常审核与推送流程，不创建历史基线跳过记录。只有账号本轮同步成功后才推进其数据库游标。

完成 DeepSeek 和飞书配置后，可以执行 `.venv/bin/python scripts/run_x_kol_db.py pipeline` 运行一次完整流程。此命令会向配置的飞书目标发送符合条件的记录。

## 查询博主与推文链接

先在项目目录加载数据库配置：

```bash
set -a; . ./.env; set +a
```

查看数据库里的监控博主，输出账号、别名、启用状态和已抓取推文数（包括停用或尚无推文的博主）：

```bash
.venv/bin/python scripts/query_x_kol_profiles.py
```

根据上一步的账号分页查询已抓取的推文链接；账号可写成 `example` 或 `@example`：

```bash
# 第一页，默认每页 20 条
.venv/bin/python scripts/query_x_kol_tweets.py example
# 第二页，每页 10 条
.venv/bin/python scripts/query_x_kol_tweets.py example --page 2 --page-size 10
```

推文按 ID 数值从大到小排列，即发布时间从新到旧；输出包含总条数、总页数和当前页链接。页码从 1 开始，页码和每页条数都必须为正整数；空结果或超出末页时显示本页无记录。两个脚本都支持 `--json`：博主 JSON 包含已有的 `avatar` 元数据，推文 JSON 包含分页信息、`tweet_id`、`url` 和解析后的完整 `payload`（正文、译文、媒体与引用等）。

查询复用 `.env` 中的 MySQL 配置，无需新增配置项。两个脚本使用只读事务，不获取工作流锁，不依赖 Redis、浏览器、DeepSeek 或飞书。结果覆盖数据库中已抓取的记录，包括已发送、已跳过和待处理内容；不等于 X 上的完整历史或登录账号的关注列表。相邻两次分页查询之间如有新推文入库，页内位置可能变化。

## 头像与推文配图

推文抓取、头像和认证信息读取均显式使用 OpenCLI 后台窗口模式，不主动切到前台；仍需已登录 X 的浏览器和 Browser Bridge 保持连接，后台模式不等于无头浏览器。

新推文入库时保留 OpenCLI 返回的 `media_urls`、`media_posters`，下载图片原图；视频保留原始链接并下载可用封面，不下载视频文件。直接引用对象的配图单独保存在 `quoted_tweet.media`，不会与外层作者的配图混在一起。上游只返回 `has_media` 而未返回地址时，记录 `media_status=unavailable`，不猜测媒体内容。

默认文件目录为 `data/media/avatars/` 和 `data/media/images/`，已排除 Git。可在私有 `.env` 设置 `X_KOL_MEDIA_DIR` 改用其他受控目录；相对目录以 `--workspace` 为基准。MySQL 保存元数据，图片二进制只写文件目录。使用和备份图片时，应同时保留数据库与该目录；`local_path` 相对于这个目录，不是本机绝对路径。

监控账号头像保存在 `xkol_profiles.config.avatar`。外层推文和引用对象分别使用可选的 `author_avatar` 字段保存真实作者的头像快照，包含与 `author` 对应的 `handle`、下载状态、来源和本地文件元数据；不使用外层作者的头像替代引用作者。同一作者的已保存文件可复用，字段缺失的旧记录仍兼容。推文与引用的 `media[]` 按原顺序记录 `type`（`photo` / `video` / `unknown`）、`url`、`poster_url`、`image`；`image` 包含 `status`（`saved` / `failed` / `unavailable`）、来源地址，以及成功时的 `download_url`、`local_path`、SHA-256、字节数、类型和时间。`media_status=complete` 表示当前返回的图片或封面均已保存，`partial` 表示仍有缺失；它不表示视频文件已归档。没有媒体地址的旧记录不自动补抓。

图片下载失败不阻止正文入库、游标推进或后续审核发送。每轮 `sync/pipeline` 在探测新推文前，自动按最早更新时间重试最多 20 条 `partial` 配图（包括引用中的图片或视频封面），独立使用默认 3 分钟预算；没有新推文或浏览器不可用时也会先尝试这些已知 CDN 地址。预算耗尽后，尚未尝试的记录留到下轮，不修改其修订号或更新时间。已保存图片直接复用。该重试只补媒体，保留原文、审核、翻译、回执和游标，不补造上游缺失的地址。`sync/pipeline` 输出新记录的 `images_saved`、`images_failed`，以及旧记录重试的 `asset_retry`（更新数、失败图片数、预算是否耗尽）。

需要独立补全缺失头像、重试 `partial` 配图时执行：

```bash
set -a; . ./.env; set +a
.venv/bin/python scripts/run_x_kol_db.py assets --limit 20
```

`assets` 每次最多尝试 20 位缺头像的作者（监控账号、外层作者及引用作者统一去重）和 20 条配图不完整的推文，可用 `--limit` 调整。`sync/pipeline` 在成功抓取后自动执行同样的头像补全，默认每轮最多 20 位，输出 `avatar_retry`。已保存头像会复用到同一作者的所有已存推文和引用；从未尝试的作者优先，失败作者按最近尝试时间轮换，预算耗尽的留到下轮。结果中的 `authors_attempted`、`authors_failed`、`authors_remaining` 表示本轮尝试、失败及仍缺头像的作者数量。

`assets` 和成功的 `sync/pipeline` 还会补齐或刷新最多 20 位作者的认证信息，按账号去重，使用独立的 `X_KOL_VERIFICATION_BUDGET_SECONDS`（默认 180 秒，范围 1–600）。未知作者优先，失败项轮换重试；成功快照缓存 24 小时，之后按队列刷新，不为每条旧推文重复访问 X。结果输出为 `verification_retry`，包含尝试数、失败数、待处理作者数和预算状态。认证信息保存在监控配置的 `verification`，以及外层和引用对象各自的 `author_verification`；包含 `handle`、布尔值 `verified`、`checked_at`、`source_url`，可附带读取到的 `badge_color` 和 `badge_type`（金色、灰色或纯色徽章）。它表示主页读取时的账号状态，不能还原历史发帖时的认证状态。读取失败不会把未知账号标为已认证，也不会清除上一次已确认的快照。

头像补全使用同一个工作流锁，按修订号只更新 `author_avatar` 和监控配置中的头像元数据，保留正文、引用归属、审核、译文、回执、批次和游标，不发送飞书。仍有本次下载失败时 `assets` 返回非零退出码；其余待处理内容可在后续运行继续补全。该命令不重新抓取历史推文，也不为旧的 `has_media` 标记补造地址。成功保存的头像不会定时刷新；作者更换头像后如需更新，应另行明确刷新。账号被删除、改名、受限或头像不可读取时保留失败状态，不填入其他作者的头像。

认证补全遵循相同的工作流锁和修订号校验，只更新上述认证元数据，保留其他字段与运行状态；`assets` 本次认证读取失败时也返回非零退出码。

每轮新推文图片默认可使用 5 分钟，缺图重试另有独立的 3 分钟；预算只计图片处理，不计时间线抓取等待。在私有 `.env` 中可分别调整（整数秒，范围 1–600）：

```dotenv
X_KOL_MEDIA_DOWNLOAD_BUDGET_SECONDS=300
X_KOL_MEDIA_RETRY_BUDGET_SECONDS=180
X_KOL_AVATAR_BUDGET_SECONDS=180
X_KOL_VERIFICATION_BUDGET_SECONDS=180
```

正式包装脚本每轮加载 `.env`，配置在下一轮生效。预算越大，整轮耗时可能越长；工作流锁继续防止任务重入。`profiles`、`assets` 和同步后的头像补全均使用独立的 `X_KOL_AVATAR_BUDGET_SECONDS`，默认 3 分钟。头像预算用完不会占用新推文配图预算。预算耗尽的图片以 `error_type=AssetBudgetExceeded` 区分于实际网络下载失败。单次 CDN 请求超时最多 10 秒，单张图片上限 20 MiB；只下载公开 X 图片 CDN，不携带浏览器 Cookie 或令牌。

## 与推文卡片项目配合

需要将已抓取推文制成图片时，可把独立的 `x-tweet-card` 项目放在本项目同级：

```text
workspace/
├── x-kol-radar/   # 抓取、审核翻译、MySQL 与原始媒体
└── x-tweet-card/  # 只读选帖、卡片与背景画布、图片发送
```

卡片项目默认从 `../x-kol-radar` 读取数据库配置与已保存媒体，使用本项目的 Python 环境执行只读查询；不需要复制 `.env`、推文或数据库台账。头像、认证及引用数据按各自真实作者提供，字段未知时留空。

本项目继续负责原有的 15 分钟抓取、审核翻译和文本消息推送。卡片项目的本地服务及图片发送队列独立运行；其“已有推文不补发”基线只作用于图片发送，不改变本项目的首次同步、审核或发送规则。两边的飞书目标、发送回执和去重记录分别保存。

## 从旧 JSONL 一次性迁移

仅旧安装升级时执行一次：

```bash
set -a; . ./.env; set +a
.venv/bin/python scripts/run_x_kol_db.py init
.venv/bin/python scripts/run_x_kol_db.py import
.venv/bin/python scripts/run_x_kol_db.py status
```

`import` 幂等导入旧监控名单、推文、同步历史、游标和发送/跳过回执；已有数据库译文不会被覆盖，游标只会前进不会倒退。确认推文、回执、同步历史和游标均完整迁入数据库后，删除旧 JSONL、旧游标文件、引用补全报告和备份目录。迁移完成后仅使用数据库命令。

## 配置审核与翻译

在私有 `.env` 中配置 DeepSeek：

```dotenv
DEEPSEEK_API_KEY=your_deepseek_api_key
X_KOL_DEEPSEEK_MODEL=deepseek-flash
```

程序通过 HTTPS 直接请求 `https://api.deepseek.com/chat/completions`，不使用 OpenAI 服务或 SDK。默认模型为 `deepseek-flash`，可通过环境变量指定官方 API 支持的其他文本模型；接口与模型见 [DeepSeek 官方文档](https://api-docs.deepseek.com/)。

```bash
set -a; . ./.env; set +a
.venv/bin/python scripts/run_x_kol_db.py review --dry-run
.venv/bin/python scripts/run_x_kol_db.py review
.venv/bin/python scripts/run_x_kol_db.py check
```

`review --dry-run` 只读统计待办，不调用 API 或回写；`review` 和 `pipeline` 默认每轮最多审核 20 条，可用 `--limit` 调整。每轮 API 工作最多执行约四分钟，每次请求超时 45 秒，最多尝试三次。限流和暂时故障会退避重试，凭据或余额错误会停止当前轮的审核。未完成记录留到下轮；新待办优先于旧失败记录，避免失败队列挡住新增内容。有失败或剩余待办时退出码为 1。

程序每轮加载当前审核策略、校验版本，并校验模型返回的 JSON、推文 ID、引用 ID 和翻译状态。只回写允许的审核与翻译字段，已有有效译文不重复生成。数据库、策略或修订号冲突时停止写入；失败记录不会被当作翻译成功。审核仅发送当前推文与引用内容到 DeepSeek，不发送数据库凭据或飞书配置。

自动运行使用 `launchd/com.example.x-kol-pipeline.plist` 的每 15 分钟调度。`pipeline` 抓取后立即审核翻译，随后发送已就绪记录；单条翻译失败不阻止其他已完成记录发送。已有待发送批次会先使用原幂等键重试，成功后继续发送新就绪内容。

### 可选：由 Codex 手动审核

仍可在暂停自动串联任务后使用 Codex 手动处理数据库待办，提示词如下；不要同时启用多套审核翻译定时任务：

```text
在项目目录中完整读取并严格执行 prompts/translate_and_review_db.md 和 prompts/translate_and_review.md。MySQL 是唯一主数据源；不得读取或改写 data/*.jsonl。使用数据库 export、apply、check 完成审核翻译闭环，不访问 X、不发送飞书。
```

任务流程为：

```bash
set -a; . ./.env; set +a
REVIEW_DIR=$(mktemp -d)
TEMP_EXPORT="$REVIEW_DIR/export.jsonl"
TEMP_UPDATES="$REVIEW_DIR/updates.jsonl"
trap 'rm -f "$TEMP_EXPORT" "$TEMP_UPDATES"; rmdir "$REVIEW_DIR"' EXIT
.venv/bin/python scripts/run_x_kol_db.py export > "$TEMP_EXPORT"
# 按策略生成只含允许字段、id 和 revision 的临时更新文件
.venv/bin/python scripts/run_x_kol_db.py apply --file "$TEMP_UPDATES"
.venv/bin/python scripts/run_x_kol_db.py check
```

临时导出不是台账，必须放在系统临时目录并在运行后删除。`check` 不访问 X、不发送消息；只有 `awaiting_review=0` 且 `awaiting_translation=0` 才表示本轮待办达到发送或跳过条件。

## 内容审核与引用推文

审核采用高召回原则：`substantive` 和 `uncertain` 允许发送，`promotional` 和 `low_signal` 登记为跳过。产品名、品牌名或公司名本身不是过滤依据。当前策略版本维护在 `prompts/translate_and_review.md`。

同步保留直接引用推文的作者、正文、时间和链接。审核结合作者正文与引用上下文；两段文字分别翻译并分别归属，飞书消息不会把引用作者的话算到外层作者名下。只有链接或表情的正文按无需翻译处理，引用详情不可用时不会编造内容。

MySQL 的 `xkol_tweets.payload` 保存完整记录和未知字段；`quoted_tweet` 继续作为可选嵌套对象。数据库写回使用 `revision` 做乐观并发控制，冲突时整批回滚并要求重新导出。

## 手动命令

先加载 `.env`：

```bash
set -a; . ./.env; set +a
```

| 命令 | 作用 |
|---|---|
| `.venv/bin/python scripts/run_x_kol_db.py pipeline` | 连续执行抓取、审核翻译、发送，并输出各阶段及最终队列结果 |
| `.venv/bin/python scripts/run_x_kol_db.py init` | 创建缺失的数据库表 |
| `.venv/bin/python scripts/run_x_kol_db.py profiles` | 将监控名单更新到 MySQL |
| `.venv/bin/python scripts/run_x_kol_db.py assets --limit 20` | 补下载缺失头像及已有地址但下载失败的配图，不发送消息 |
| `.venv/bin/python scripts/run_x_kol_db.py sync` | 执行一次只读 X 增量抓取 |
| `.venv/bin/python scripts/run_x_kol_db.py review` | 调用 DeepSeek 审核、翻译并回写 MySQL |
| `.venv/bin/python scripts/run_x_kol_db.py export` | 导出未完成记录的临时工作副本 |
| `.venv/bin/python scripts/run_x_kol_db.py apply --file FILE` | 原子回写允许的审核和翻译字段 |
| `.venv/bin/python scripts/run_x_kol_db.py check` | 只读检查待审核、待翻译和待发送数量 |
| `.venv/bin/python scripts/run_x_kol_db.py dispatch` | 发送已处理记录并写入数据库回执 |
| `.venv/bin/python scripts/run_x_kol_db.py status` | 查看数据库队列状态 |

`dispatch` 在联系飞书前先把完整消息批次和幂等键提交到 MySQL。失败批次会保留并使用相同幂等键重试；发送成功后才登记 `sent` 回执。

## 启用 macOS 定时任务

正式调度使用 `launchd/com.example.x-kol-pipeline.plist`：每小时 00、15、30、45 分调用 `scripts/run_x_kol_pipeline_launchd.sh`。包装脚本加载项目 `.env` 后执行一次 `pipeline`，完成后退出，由 launchd 在下一时刻再次启动。

只启用这个串联任务。若已安装原来 00/05/10 分的三条任务，先确认它们已结束，再卸载并删除相应的已安装 plist；原有 Codex 自动审核保持暂停。仓库中的单阶段模板仅供单独运行阶段时参考，不与串联调度一起安装。

替换模板中的占位路径和飞书占位值，然后加载：

```bash
plutil -lint launchd/*.plist
cp launchd/com.example.x-kol-pipeline.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.x-kol-pipeline.plist
launchctl print gui/$(id -u)/com.example.x-kol-pipeline
```

抓取需要 Mac 已登录用户会话，且 Browser Bridge 保持连接。睡眠或断网后，从 MySQL 的游标、待办和待发送批次继续处理。单轮超过 15 分钟时不会并行启动第二轮；恢复运行不保证补齐超出账号单次抓取上限的历史内容。

## 数据库表

| 表 | 用途 |
|---|---|
| `xkol_profiles` | 账号配置和持久化游标 |
| `xkol_tweets` | 推文、引用、媒体、审核、翻译和修订号 |
| `xkol_receipts` | 发送/跳过回执和 URL 去重 |
| `xkol_sync_history` | 每个账号的同步结果 |
| `xkol_batches` | 发送前持久化的消息批次和幂等键 |

Redis 只保存带命名空间的临时任务锁，不作为可恢复的数据源。运行日志仍写入私有的 `logs/`，但推文和工作流状态不写入本地 JSONL。

## 安全与隐私

项目不保存 X Cookie、请求头或访问令牌。`.env`、实际监控名单和日志均被 `.gitignore` 排除。Git/GitHub 只管理代码与占位示例，不上传真实运行数据。公开仓库前仍应检查截图、日志及其他本地产物是否含真实账号、群聊、路径、Cookie 或令牌。

GitHub 上的代码版本不能恢复 MySQL 运行数据；数据库的持久化由使用者管理。DeepSeek 密钥只放本机环境变量或 `.env`，不写入代码、plist 或日志。请遵守 X、飞书及相关工具的使用条款和访问频率限制。

## 测试

```bash
python3 -m unittest discover -s src/x_kol_sync/tests -v
python3 -m py_compile src/x_kol_sync/*.py scripts/*.py
node --check scripts/read_x_profile_avatar.mjs
git diff --check
plutil -lint launchd/*.plist
```

GitHub Actions 会在 Python 3.10、3.13 上检查代码，并使用独立的 MySQL/Redis 服务运行数据库集成测试。贡献方式见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可证

MIT License，详见 `LICENSE`。
