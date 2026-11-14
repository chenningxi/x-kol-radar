# AGENTS.md

## 项目定位

这是一个 macOS X/Twitter KOL 增量监控项目：通过 OpenCLI 和 Browser Bridge 只读抓取公开推文，使用 MySQL 保存推文和工作流状态、Redis 提供任务锁，再通过 `lark-cli` 推送到飞书群。

## 必须保持的行为

- X 端严格只读。不得添加发帖、回复、点赞、关注、转推、收藏或其他写操作。
- 正式调度每 15 分钟启动一次 `pipeline`（每小时 00、15、30、45 分），在同一轮中连续执行抓取、DeepSeek 审核翻译、飞书发送，三个阶段之间不再等待固定分钟。不得同时启用旧 00/05/10 分调度；Codex 仅作为手动审核备选。
- 正式入口为 `scripts/run_x_kol_db.py`；`sync` 只能同步，成功后直接退出，不能顺带发送飞书。
- 数据库 `dispatch` 只发送翻译完成或无需翻译的记录。
- 数据库 `check` 必须保持只读，不发送消息或登记跳过记录；审核回写后使用它验证当前版本审核和翻译条件。`pipeline` 输出同样的队列统计。
- 每轮的内容审核与翻译必须遵循 `prompts/translate_and_review.md` 中的当前策略版本；产品名或品牌名本身不得作为过滤依据。
- 整轮 `pipeline` 仅获取一次工作流运行锁，阶段之间不释放。抓取或审核阶段的可恢复失败不阻止已就绪记录发送；发送失败保留持久化批次，下轮使用原幂等键重试。
- 内容审核采用高召回原则：`substantive` 和 `uncertain` 允许发送，`promotional` 和 `low_signal` 登记为跳过；缺少当前版本审核结果的记录不得提前发送。
- 抓取时保留 `quoted_tweet` 的作者、正文、时间和链接；审核结合引用上下文，翻译分别写入外层与引用对象，飞书明确区分两个作者。只有链接或表情的正文不应视为翻译失败。
- 首次同步抓取到的内容默认进入翻译和推送流程；项目不提供历史基线跳过命令。
- 同步按推文 URL/ID 去重；发送按 URL 台账和幂等键去重。完整同步成功后才能推进游标。
- 正式工作流不得读取或写入 `data/*.jsonl`、旧 JSON 游标或项目内备份。API 审核翻译使用数据库 `review/check` 并复用 `apply` 修订号校验；手动审核继续使用 `export/apply/check`。保留未知字段，通过修订号防止并发覆盖；临时工作文件用后清理。
- DeepSeek API Key 只保存在私有 `.env` 或环境变量，不写入代码、plist、日志或公开示例。
- 项目目录不保留历史台账、备份或临时测试样本；代码版本通过 Git/GitHub 管理，真实运行数据不提交。

## 配置与隐私

- `data/x_profiles.md`、`.env`、`data/*.jsonl`、`data/x_cursors.json` 和 `logs/` 都是本地运行数据，不得提交真实内容。
- 不要把真实 X 账号、飞书 profile、群名、`chat_id`、本机绝对路径、Cookie、令牌或聊天截图写入公开文件。
- 对外示例只使用占位值，并同步维护 `.env.example`、`data/x_profiles.example.md` 和 `launchd/com.example.*.plist`。
- 不要清空、重建或批量修改现有运行台账，除非用户明确要求；这可能导致重复或大量补发。

## 代码入口

- 同步核心：`src/x_kol_sync/sync_x_kols.py`
- OpenCLI 读取封装：`src/x_kol_sync/fetch_tweet_links.py`
- 游标探测：`scripts/run_x_kol_cursor_sync.py`
- 正式同步/审核回写/发送入口：`scripts/run_x_kol_db.py`
- 正式 launchd 包装脚本：`scripts/run_x_kol_pipeline_launchd.sh`；单阶段包装脚本只用于独立运维，不与串联调度同时启用。
- 用户说明：`README.md`
- 旧 JSONL 工具（含 `scripts/backfill_x_kol_quotes.py`、`scripts/run_x_kol_dispatch.py`）只保留兼容代码，不得用于正式调度或重新生成已删除的旧台账。

## 修改要求

- 优先做小而明确的改动，不改变已有运行数据格式和兼容状态，除非需求明确要求迁移。
- 修改命令、环境变量、调度时间或数据字段时，同时更新 README、示例配置和相关测试。
- README 面向第一次下载项目的用户，只描述当前使用方式，不写变更历史，也不解释作者个人环境。
- 未经用户要求，不安装、卸载或改动本机 launchd/Codex 定时任务，也不向真实飞书群发送测试消息。

## 验证

改动 Python 或工作流后至少运行：

```bash
python3 -m unittest discover -s src/x_kol_sync/tests -v
python3 -m py_compile src/x_kol_sync/*.py scripts/*.py
git diff --check
```

修改 launchd 模板后再运行：

```bash
plutil -lint launchd/*.plist
```
