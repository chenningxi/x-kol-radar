# X KOL Radar：项目说明与数据使用指南

> 面向希望了解、部署或使用项目数据的读者。本文依据当前仓库实现编写；账号、推文和消息示例均为虚构占位，不包含实际监控名单、数据库记录或群聊信息。

## 1. 项目解决什么问题

X KOL Radar 是一个运行在 macOS 上的 X/Twitter 公开推文增量监控项目。使用者指定需要关注的账号，项目定时采集公开内容，将原文、引用上下文、审核结果、中文翻译和发送回执保存到 MySQL，并把符合条件的内容推送到飞书群。

它将分散在账号时间线中的信息整理成可追溯、可查询的数据，减少反复查看账号、阅读外语内容和人工转发的工作。适用于行业观察、产品与技术动态跟踪、研究资料积累，以及团队信息共享。

项目当前版本为 `v0.1.0`，代码采用 MIT 许可证。正式运行入口为 `scripts/run_x_kol_db.py`。

### 当前能力与适用边界

| 能力 | 当前实现 |
|---|---|
| 指定账号监控 | 配置账号、别名、启停、单次抓取上限及是否包含转推 |
| 增量采集 | 先探测最新推文，必要时按账号配置抓取，按 ID/URL 去重 |
| 内容审核 | 采用高召回策略，过滤明确的低信息量推广和低信号内容 |
| 中文翻译 | Python 直连 DeepSeek API 处理非中文正文；作者与被引用作者的内容分别翻译 |
| 引用上下文 | 保留直接引用的一层作者、正文、时间和链接 |
| 持久化与追溯 | MySQL 保存内容、游标、审核翻译状态、同步历史及发送回执 |
| 群消息分发 | 使用 `lark-cli` 推送，持久化批次及幂等键以支持失败重试 |
| 数据二次使用 | 可通过 MySQL 只读查询接入自建分析、检索或报告流程 |

当前项目不提供现成的 Web 看板、业务查询 API、自动日报、知识库或语义搜索。这些可以基于数据库进一步开发。项目监控的是指定账号，不是 X 全站搜索或全量历史归档；X 端严格只读，不执行发帖、回复、点赞、关注、转推、收藏等操作。

## 2. 整体架构与运行节奏

```text
私有监控名单 ── profiles ──→ MySQL 账号配置
                                  │
每 15 分钟启动一轮                 ↓
X 公开推文 → 已登录浏览器 → OpenCLI / Browser Bridge
                                  │ sync
                                  ↓
                         MySQL 推文与同步状态
                                  │ review
抓取后接着执行                     ↓
                     DeepSeek 按当前策略审核、翻译
                                  │ 修订号校验回写 → check
                                  ↓
                         MySQL 审核与翻译结果
                                  │ dispatch
审核后接着执行                     ↓
                      lark-cli → 飞书群 → MySQL 回执

Redis 临时锁 + MySQL 会话锁：防止工作流任务重入
```

正式调度每小时 00、15、30、45 分启动 `pipeline`，同一轮中连续执行抓取、DeepSeek 审核翻译和飞书发送。三个阶段之间不等待固定分钟，整轮使用同一个运行锁。单阶段命令继续保持独立：`sync` 成功后退出，不会顺带发送；`review` 不访问 X 或外部链接、不发送飞书；`dispatch` 负责发送及登记跳过回执。

抓取失败后仍可处理数据库里已有的待办，部分翻译失败也不阻止其他就绪内容发送。成功进度逐阶段持久化，失败记录和发送批次留到下一轮重试。限额、网络及运行耗时会影响完成时间，不能保证每条推文都在一轮内送达。

MySQL 是唯一运行数据源。Redis 只保存临时任务锁，不能用于恢复推文或发送记录。正式流程不使用 `data/*.jsonl` 或旧 JSON 游标；审核导出文件只是系统临时目录中的工作副本，用后删除。

### 一条记录如何流转

1. `sync` 将新推文写入数据库。首次抓到的内容也进入正常审核与推送流程。
2. `review` 从 MySQL 读取尚无 `sent` 或 `skipped` 回执、仍需审核或翻译的记录，并保留修订号 `revision`。
3. Python 直连 DeepSeek 官方 API，按当前策略审核，需要时分别翻译正文与引用正文。
4. `review` 复用 `apply` 的字段白名单与修订号校验，逐条提交，保留原文及未知字段；冲突时停止并回滚当前记录。手动 Codex 审核仍可使用 `export/apply`。
5. `check` 只读检查队列，不发送消息、不登记跳过记录。
6. `dispatch` 为符合条件的内容持久化发送批次，再联系飞书；成功后写入 `sent` 回执。应跳过的内容写入 `skipped` 回执，原推文仍保留在数据库中。

同步游标按账号在本轮成功提交后推进；外部读取失败时停止当前轮次。发送同时使用 URL 回执与批次幂等键去重。飞书侧幂等支持及有效期仍影响重试保障，不能将其解释为无限期、绝对不重复的交付承诺。

## 3. 数据审核与翻译口径

内容审核的唯一策略来源为 [prompts/translate_and_review.md](../prompts/translate_and_review.md)，当前策略版本为 `v2`。数据库审核流程见 [prompts/translate_and_review_db.md](../prompts/translate_and_review_db.md)。版本变更后以仓库策略及发送脚本为准。

| 分类 | 含义 | 后续处理 |
|---|---|---|
| `substantive` | 有独立新闻、业务、技术或观点价值 | 正文及有效引用正文翻译就绪后允许发送 |
| `uncertain` | 缺少上下文，无法可靠判断重要性 | 保留发送机会；翻译就绪后允许发送 |
| `promotional` | 明确的低信息量推广，没有重要新增事实 | 不要求翻译，由发送阶段登记跳过 |
| `low_signal` | 缺少独立信息的反应或简短内容 | 不要求翻译，由发送阶段登记跳过 |

审核采用高召回原则：品牌名、产品名或公司名本身不能作为过滤依据。带有购买或试用号召的新产品首次发布，仍可能属于实质信息。`uncertain` 表示上下文或判断存在不确定性，不是事实已经验证。

包含引用时，审核结合外层正文与直接引用正文判断，分类写在外层记录。作者只发表情，但引用中有重要新增事实，也可以保留。引用不代表外层作者认可全部内容，二次分析时必须分清发言归属。

翻译状态的含义如下：

| 状态 | 含义 |
|---|---|
| `pending` 或未完成状态 | 仍需处理 |
| `translated` | 已有中文译文 `translation_zh` |
| `no_translation_needed` | 原文为中文，或只有链接、表情、没有可翻译文字 |
| `not_needed` | 兼容的无需翻译状态 |
| `failed` | 翻译失败，需结合 `translation_error` 排查和重试 |

作者与引用正文分别保存翻译状态。引用存在文字时，两部分都须达到就绪条件才可发送；引用详情不可用时保留已有内容和链接，不编造正文。当前策略版本缺失或不匹配的未终结记录，不能提前发送。

审核与翻译属于内容加工，不构成事实核验。将数据用于报告或决策时，应保留原文链接，并对关键事实进行独立核实。

## 4. 数据落在哪里、包含什么

### 4.1 数据表

| 表 | 主要字段 | 用途 |
|---|---|---|
| `xkol_profiles` | `handle`、`config`、`cursor_id`、`updated_at` | 账号配置与持久化游标 |
| `xkol_tweets` | `tweet_id`、`url`、`profile_handle`、`payload`、`translation_status`、`content_policy_version`、`revision`、`first_seen_at`、`updated_at` | 推文内容与处理状态 |
| `xkol_receipts` | `event_key`、`tweet_url`、`status`、`payload`、`recorded_at` | 发送及跳过回执，按 URL 判断是否已终结 |
| `xkol_sync_history` | `event_key`、`payload`、`recorded_at` | 每个账号的采集结果、数量和错误 |
| `xkol_batches` | `batch_key`、`payload`、`status`、`updated_at` | 已持久化的消息批次和重试信息 |

完整定义见 [db/schema.sql](../db/schema.sql)，存储说明见 [db/README.md](../db/README.md)。`payload` 为 JSON；业务内容和扩展字段主要保存在其中，索引列便于查询。

### 4.2 推文字段

以下路径均相对于 `xkol_tweets.payload`，除特别注明外。

| 字段 | 含义与使用注意 |
|---|---|
| `id`、`url` | 推文身份与规范化原文链接；ID 应按字符串处理 |
| `profile` | 本次采集来源账号主页 |
| `author`、`name` | 实际发言作者和可选显示名；包含转推时，来源账号与作者可能不同 |
| `created_at` | 上游返回的原始发布时间，可能为空；不是数据库入库时间 |
| `text` | 原文正文，审核翻译不覆盖它 |
| `likes`、`retweets`、`replies`、`views` | 采集时返回的互动数；上游未返回时当前封装可能填 `0` |
| `has_media` | 可选媒体存在标记，不等于已下载媒体或完成图片、视频理解 |
| `quote_status` | 检测到引用时的可用状态；缺失不能直接推断有无引用 |
| `quoted_tweet` | 可选直接引用对象，含独立的 `id`、`author`、`name`、`text`、`created_at`、`url`、`has_media` 及翻译字段 |
| `content_classification`、`content_reason` | 内容分类与简短中文理由；不是主题标签或情绪标签 |
| `content_policy_version`、`content_reviewed_at` | 审核策略版本和审核时间 |
| `translation_status`、`translation_zh`、`source_language` | 翻译状态、简体中文译文和语言标记 |
| `translation_updated_at`、`translation_error` | 翻译更新时间和失败信息 |
| `first_seen_at` | 正式同步补充的首次发现时间；数据库同名列记录入库时间，统计应明确采用哪个口径 |
| 表列 `revision` | 并发控制修订号；审核导出时附加在记录顶层，不是推文内容版本 |

数据库连接将会话时区设置为 UTC；审核和翻译时间使用 UTC ISO 8601。`created_at` 保留上游格式，分析前应单独解析。飞书消息对可识别的发布时间转换为北京时间，无法解析时保留原值。

### 4.3 虚构数据示意

以下为一条记录的部分字段，省略互动数等信息；不是可直接 `apply` 的更新文件，也不代表真实运行结果。

```json
{
  "id": "1000000000000000001",
  "url": "https://x.com/example/status/1000000000000000001",
  "profile": "https://x.com/example",
  "author": "example",
  "created_at": "Mon Jun 01 01:00:00 +0000 2026",
  "text": "A new version is available with offline support.",
  "content_classification": "substantive",
  "content_reason": "包含新增离线能力的版本发布信息",
  "content_policy_version": "v2",
  "content_reviewed_at": "2026-06-01T01:05:00+00:00",
  "translation_status": "translated",
  "source_language": "en",
  "translation_zh": "新版本已发布，支持离线使用。",
  "translation_updated_at": "2026-06-01T01:05:00+00:00"
}
```

### 4.4 不能从这些字段直接得出的结论

- 互动数是采集快照，现有同步不会持续刷新已入库推文，不能直接用于互动增长曲线；`0` 也可能来自上游字段缺失。
- 入库数量只代表本项目实际取得的数据，不代表账号全部发文量或整个行业的信息量。
- 空结果只代表本次未取得数据，不能证明账号没有发布推文。单次抓取上限、浏览器状态及访问限制均会影响覆盖率。
- 相同事件由不同作者发布时，仍会保留多条推文；ID/URL 去重不等于事件级去重。
- 引用只展开直接的一层，未提供完整讨论串或递归引用链；媒体标记不提供完整视觉上下文。
- 正文入库后不会因原推文后续编辑或删除自动刷新，不能将数据理解为来源页面的实时镜像。
- 飞书展示会压缩空白并对每段正文截取前 500 个字符后加省略号；完整已采集正文应从数据库读取。

## 5. 如何使用落地数据

### 5.1 直接阅读飞书消息

飞书是团队消费增量信息的入口。每条内容包含作者、发布时间、可用中文内容和原文链接；存在引用时另列引用作者、时间、正文及链接。需要研究或引用长文时，结合数据库正文和来源页面查看。

### 5.2 对接自建分析或报告

推荐由部署方为分析使用者提供单独的 MySQL 只读账号，按用途开放必要的表或脱敏视图。分析查询不修改工作流表，也不通过重置回执来获取历史内容。数据库中可能包含监控名单、别名、群目标和消息信息，对外共享应选择必要字段。

下列查询以 MySQL 8 为目标，使用 `SELECT` 读取业务数据；示例 A 另用 `SET SESSION` 将当前连接设为 UTC，不修改表中数据。应在自己的数据库连接中运行，结果保存到仓库外的受控位置。它们是数据使用示例，不会执行审核、推进状态或发送消息。

**示例 A：读取最近 24 小时实际发送过的内容，用作团队日报素材。**

```sql
SET SESSION time_zone = '+00:00';

SELECT
  t.tweet_id,
  t.url,
  JSON_UNQUOTE(JSON_EXTRACT(t.payload, '$.author')) AS author,
  JSON_UNQUOTE(JSON_EXTRACT(t.payload, '$.created_at')) AS published_at_raw,
  JSON_UNQUOTE(JSON_EXTRACT(t.payload, '$.text')) AS original_text,
  JSON_UNQUOTE(JSON_EXTRACT(t.payload, '$.translation_zh')) AS translation_zh,
  JSON_UNQUOTE(JSON_EXTRACT(t.payload, '$.content_classification')) AS classification,
  JSON_EXTRACT(t.payload, '$.quoted_tweet') AS quoted_tweet
FROM xkol_tweets AS t
WHERE EXISTS (
  SELECT 1
  FROM xkol_receipts AS r
  WHERE r.tweet_url = t.url
    AND r.status = 'sent'
    AND r.recorded_at >= UTC_TIMESTAMP() - INTERVAL 1 DAY
)
ORDER BY t.first_seen_at DESC, t.tweet_id DESC;
```

这里按发送回执登记时间选择素材，不等同于“最近 24 小时发布的推文”。`EXISTS` 避免同一 URL 的多条回执放大结果。生成日报时可再按事件聚合、人工核实，并保留各作者与原文链接；这些加工步骤需自行实现。

**示例 B：在当前策略保留的记录中检索正文、译文和引用。**

```sql
SELECT
  tweet_id,
  url,
  profile_handle,
  translation_status,
  JSON_UNQUOTE(JSON_EXTRACT(payload, '$.text')) AS original_text,
  JSON_UNQUOTE(JSON_EXTRACT(payload, '$.translation_zh')) AS translation_zh,
  JSON_EXTRACT(payload, '$.quoted_tweet') AS quoted_tweet
FROM xkol_tweets
WHERE content_policy_version = 'v2'
  AND JSON_UNQUOTE(JSON_EXTRACT(payload, '$.content_classification'))
      IN ('substantive', 'uncertain')
  AND (
    JSON_UNQUOTE(JSON_EXTRACT(payload, '$.text')) LIKE '%offline%'
    OR JSON_UNQUOTE(JSON_EXTRACT(payload, '$.translation_zh')) LIKE '%离线%'
    OR JSON_UNQUOTE(JSON_EXTRACT(payload, '$.quoted_tweet.text')) LIKE '%offline%'
    OR JSON_UNQUOTE(JSON_EXTRACT(payload, '$.quoted_tweet.translation_zh')) LIKE '%离线%'
  )
ORDER BY first_seen_at DESC
LIMIT 100;
```

`v2` 应随当前审核策略版本同步调整。这里的关键词是虚构示例；接入应用时应使用参数化查询。结果可能包含尚未完成翻译或尚未发送的记录，不能据此认定已推送。`LIKE` 是简单文本匹配，数据量增大后需另行设计检索索引。

### 5.3 常见落地方向

以下为基于现有数据可以开发的应用，均不属于当前内置功能。

| 方向 | 使用的数据 | 需要补充的处理 |
|---|---|---|
| 行业日报、周报 | 正文、译文、来源链接、分类、发送回执 | 时间窗口、事件聚合、摘要与人工核实 |
| 产品及技术动态跟踪 | 账号来源、正文、引用、发布时间 | 产品实体识别、主题标签和事件时间线 |
| 研究资料库 | 原文、译文、作者、链接、引用关系 | 检索索引、权限、引用展示及来源复核 |
| 团队信息看板 | 推文、同步历史、处理状态、回执 | 只读查询服务和前端展示 |
| 工作流质量分析 | 分类、失败信息、审核版本、同步历史 | 统计口径、人工抽样标注与质量指标 |

内容库应以 `tweet_id` 或规范化 `url` 作为来源键；事件聚合另建业务标识。引用对象保持作者归属，避免把引用内容误记成外层作者观点。新增主题、摘要或实体等分析结果，优先放在独立分析表或下游系统，避免直接更改原文与现有工作流状态。

## 6. 如何部署和运行

### 运行条件

| 依赖 | 职责 |
|---|---|
| macOS 与可用用户会话 | 运行浏览器及 `launchd` 调度 |
| Python 3.10+ | 执行项目脚本 |
| MySQL 8+、Redis 6+ | 持久化状态与任务锁 |
| OpenCLI、Browser Bridge、Chrome/Chromium | 使用已登录 X 的浏览器会话读取公开内容 |
| 已配置的 `lark-cli` | 以机器人身份向目标飞书群发送消息 |
| DeepSeek API Key | 每轮抓取后审核及翻译；密钥仅保存于私有 `.env` |

`setup.sh` 创建 Python 虚拟环境、安装 Python 依赖并在缺失时复制示例名单；其他工具、数据库服务和登录授权需要使用者配置。详细命令见 [README.md](../README.md)。

### 落地顺序

1. 获取代码，运行 `./setup.sh`，复制 `.env.example` 为私有 `.env` 并配置环境变量。
2. 创建专用 MySQL 数据库与用户，启动 MySQL 和 Redis；加载 `.env` 后执行 `init` 初始化缺失的表。
3. 编辑私有 `data/x_profiles.md`，通过 `profiles` 将账号配置写入数据库。正式同步读取数据库；从 Markdown 删除账号不会自动删除数据库账号，停用时应明确设置禁用后重新导入配置。
4. 确认浏览器已登录 X 且 Browser Bridge 已连接，运行 `opencli doctor`，再执行 `sync`。
5. 配置 `DEEPSEEK_API_KEY` 和模型名，先用 `review --dry-run` 检查队列，再运行 `review → check` 验证审核翻译。
6. 准备好接收目标后执行 `dispatch`，核对飞书显示和数据库回执。
7. 需要自动运行时，按 README 仅启用 `com.example.x-kol-pipeline.plist`，每 15 分钟连续执行一次完整流程。卸载旧三阶段调度，原有 Codex 自动审核保持暂停。

配置项以 [.env.example](../.env.example) 为准，监控名单格式见 [data/x_profiles.example.md](../data/x_profiles.example.md)。首次同步内容会进入推送流程，开始前应按实际需要确定账号及抓取上限。

### 常用命令与副作用

加载私有 `.env` 后，使用项目虚拟环境中的 Python：

| 命令后缀（位于 `scripts/run_x_kol_db.py` 后） | 用途 | 是否写业务状态 / 发送消息 |
|---|---|---|
| `pipeline` | 一轮连续抓取、审核翻译和发送 | 写内容、处理结果和回执 / 发送 |
| `init` | 创建缺失的数据库表，不创建数据库 | 建表 / 不发送 |
| `profiles` | 更新数据库中的名单配置 | 写配置 / 不发送 |
| `sync` | 只读抓取 X 并保存增量 | 写内容、游标、同步历史 / 不发送 |
| `review` | 直连 DeepSeek，审核翻译并校验后回写 | 写审核和译文 / 不发送 |
| `review --dry-run` | 只读统计待处理数量，不调用 API | 不写入 / 不发送 |
| `export` | 导出尚未终结记录及 `revision` | 只读查询 / 不发送 |
| `apply --file FILE` | 原子回写允许的审核及翻译字段 | 写内容处理结果 / 不发送 |
| `check`、`status` | 查询当前队列状态 | 只读查询 / 不发送 |
| `dispatch` | 发送就绪内容、登记跳过、重试待发送批次 | 写批次及回执 / 发送 |

只读命令仍会短暂获取运行锁，并要求 Redis 可用。用于分析的独立 SQL 查询无需走工作流入口。旧 JSONL 工具及兼容迁移入口不参与新部署和正式调度。

## 7. 如何判断项目已正常运行

运行以下命令检查队列：

```bash
set -a
. ./.env
set +a
.venv/bin/python scripts/run_x_kol_db.py status
.venv/bin/python scripts/run_x_kol_db.py check
```

| 输出项 | 解释 |
|---|---|
| `content_policy_version` | 当前发送代码要求的策略版本，应与审核提示词一致 |
| `tweets` | 数据库推文总记录数 |
| `terminal_urls` | 已有 `sent` 或 `skipped` 回执的不同 URL 数；不是发送成功数 |
| `send_ready` | 当前满足审核和翻译条件的待发送数 |
| `skip_ready` | 当前应由发送阶段登记为跳过的数量 |
| `awaiting_review` | 缺少当前版本有效审核的待办数 |
| `awaiting_translation` | 当前允许发送但正文或引用翻译未就绪的待办数 |

`awaiting_review=0` 且 `awaiting_translation=0` 表示当前待办已达到发送或跳过条件，`check` 此时返回成功；只要任一非零就返回非零退出码。没有新记录时也可能为零，仍需结合账号启用状态及 `xkol_sync_history` 判断采集是否正常。

`send_ready` 不代表发送成功；最终以 `xkol_receipts.status='sent'` 为依据。`skip_ready` 也不代表已经跳过，须由 `dispatch` 登记。持续存在 `pending` 发送批次时，应先核对日志、飞书响应及回执，再按原批次重试，不清空台账或重新制造一批内容。

## 8. 数据管理与对外共享

代码仓库只管理代码、文档和占位配置。真实 `.env`、监控名单、日志、数据库内容和临时工作文件不提交 Git，也不把真实账号、飞书 profile、群名、群 ID、本机路径、Cookie、令牌或聊天截图写入公开文档。

对外提供数据时，应由数据管理者确认共享范围，按用途选择必要的正文、译文、作者及原文链接，排除私有备注、群目标和凭据。代码的 MIT 许可证不授予对第三方推文和媒体的额外使用权；数据使用应遵守来源平台及相关权利要求。

数据库持久化、备份、恢复和留存周期由部署方管理，代码仓库不能恢复运行数据。不要通过清空、重建或批量修改运行台账来重新开始，这可能触发重复或大量补发；分析导出与备份应保存在项目目录外。

## 9. 阅读入口

- [README：部署与日常使用](../README.md)
- [数据库说明](../db/README.md)与[表结构](../db/schema.sql)
- [当前内容审核与翻译策略](../prompts/translate_and_review.md)
- [Codex 数据库审核操作流程](../prompts/translate_and_review_db.md)
- [环境配置示例](../.env.example)与[监控名单示例](../data/x_profiles.example.md)
- [每 15 分钟串联调度模板](../launchd/com.example.x-kol-pipeline.plist)
