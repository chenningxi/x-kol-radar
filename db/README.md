# 数据库存储

正式运行入口使用 MySQL 作为唯一数据源，Redis 用于任务互斥。`data/*.jsonl` 只支持旧安装的一次性迁移，不参与同步、审核翻译或发送调度。

## 配置和初始化

Python 3.10+，MySQL 8+，Redis 6+。安装依赖并在 `.env` 填写 `.env.example` 中的数据库连接参数。`.env` 需要显式加载：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
set -a
. ./.env
set +a
.venv/bin/python scripts/run_x_kol_db.py init
.venv/bin/python scripts/run_x_kol_db.py status
```

`init` 只创建缺失的 `xkol_` 表，不清空已有表。配置变更后运行 `profiles` 将 Markdown 名单更新到数据库；数据库中其他账号不会自动删除。

旧安装升级时可在切换调度前执行一次 `.venv/bin/python scripts/run_x_kol_db.py import`。它导入监控名单、推文、同步历史、最新游标及发送/跳过回执；重复导入保留数据库中已有推文和译文，不覆盖它们，游标不会倒退。迁移完成后不再读取或改写 JSONL。

核对迁移完整性后删除旧 JSONL、旧游标及历史备份；项目目录不保留这些副本。代码版本使用 Git/GitHub 管理，真实运行数据只存于 MySQL，不提交数据库内容或凭据。

## 表结构

| 表 | 职责 |
|---|---|
| xkol_profiles | 账号配置和持久化游标 |
| xkol_tweets | 唯一推文 ID/URL、全文 JSON、翻译状态、审核版本、修订号 |
| xkol_receipts | 历史发送/跳过事件，防止补发 |
| xkol_sync_history | 每个账号采集结果 |
| xkol_batches | 发送前持久化的消息批次和幂等键 |

原始推文、引用推文、媒体和未知字段完整保存在 `payload` JSON 中。引用对象继续使用 `quoted_tweet`，避免迁移丢失既有内容。索引列便于后续扩展查询。数据库时间使用 UTC。

## 三个阶段

正式自动调度每 15 分钟执行一次 `.venv/bin/python scripts/run_x_kol_db.py pipeline`，同一轮中依次完成下述三个阶段，不再分别按 00/05/10 分调度。以下命令也可用于单独运维。

只读采集：

```bash
.venv/bin/python scripts/run_x_kol_db.py sync
```

每个账号先读取 1 条探针，游标无变化则跳过；首次或变化后按该账号上限读取。只更新该账号成功提交的游标；外部读取失败后停止当前轮次。OpenCLI 仍通过原有只读封装访问 X。零条返回只表示未取得数据，不代表账号没有发文。

DeepSeek 审核翻译：在 `.env` 配置 `DEEPSEEK_API_KEY` 和可选的 `X_KOL_DEEPSEEK_MODEL`（默认 `deepseek-flash`），加载环境后执行：

```bash
.venv/bin/python scripts/run_x_kol_db.py review --dry-run
.venv/bin/python scripts/run_x_kol_db.py review
.venv/bin/python scripts/run_x_kol_db.py check
```

`review` 直连 DeepSeek 官方 API，加载 `prompts/translate_and_review.md`，复用 `apply` 的字段白名单和修订号校验，按条提交，不落地导出文件。作者与引用正文分别翻译；无需翻译的文本、已有译文、失败重试和发送条件保持原策略。

如需 Codex 手动审核翻译，提示词使用 `prompts/translate_and_review_db.md`，流程如下：

```bash
REVIEW_DIR=$(mktemp -d)
trap 'rm -f "$REVIEW_DIR/export.jsonl" "$REVIEW_DIR/updates.jsonl"; rmdir "$REVIEW_DIR"' EXIT
.venv/bin/python scripts/run_x_kol_db.py export > "$REVIEW_DIR/export.jsonl"
# Codex 根据 prompts/translate_and_review.md 审核并翻译，生成更新 JSONL
.venv/bin/python scripts/run_x_kol_db.py apply --file "$REVIEW_DIR/updates.jsonl"
.venv/bin/python scripts/run_x_kol_db.py check
```

`export` 输出尚未发送/跳过的记录并附带 `revision`。输出是工作副本，不是主账本。更新文件每行必须保留 `id`、`revision`，可以只包含需要更新的审核和翻译字段；`quoted_tweet` 更新需保留引用 `id`。例如：

```json
{"id":"123456789","revision":1,"translation_status":"translated","translation_zh":"中文译文","source_language":"en","content_classification":"substantive","content_policy_version":"v2","content_reason":"有实质信息"}
```

`apply` 只允许审核和翻译字段修改，保留原文、URL 和未知字段；修订号冲突时整批回滚，重新导出后再处理。仍需遵循当前提示词的引用独立翻译、审核版本等要求。`check` 不访问 X、不发消息，输出各状态数量。

程序发送：

```bash
.venv/bin/python scripts/run_x_kol_db.py dispatch
```

复用既有审核策略、引用翻译检查和消息格式。发送前先将完整批次和幂等键提交到 MySQL；失败保留批次，下次使用同一幂等键重试。发送完成才登记成功回执。幂等保障仍取决于飞书对幂等键的支持和有效期，超长中断后须核对回执。

## 调度

正式包装脚本 `scripts/run_x_kol_pipeline_launchd.sh` 加载 `.env`，调用数据库 `pipeline`。对应的 `launchd/com.example.x-kol-pipeline.plist` 在每小时 00、15、30、45 分启动一轮。仅保留这一条自动调度；卸载旧三阶段调度，原有 Codex 定时审核保持暂停。

整轮持有同一个运行锁，防止并发。同轮抓取、审核的成功记录分别提交；部分失败后仍尝试处理其他待办并发送已就绪内容。失败记录和待发送批次从 MySQL 恢复。默认每轮最多审核 20 条，最多约四分钟，可用 `pipeline --limit N` 调整条数；有剩余待办或阶段失败时退出码非零，不代表此前已提交的进度被撤回。

Redis 只使用配置前缀下的锁，不清空其他 key。MySQL 会话锁同时防止 Redis 租约过期后的重入；Redis 不可用时任务明确失败。持久化游标始终在 MySQL，不依赖缓存恢复。`status/check/export` 使用只读查询（会短暂获取运行锁）。
