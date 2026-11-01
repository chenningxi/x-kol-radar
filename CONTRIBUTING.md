# Contributing

感谢贡献。提交代码前请：

1. 不提交真实的推文台账、日志、截图、`.env`、Cookie、令牌或飞书群信息；
2. 在全新临时目录验证安装流程，使用独立的测试数据库，不接触真实群聊或运行数据；
3. 运行测试：

   ```bash
   python3 -m unittest discover -s src/x_kol_sync/tests -v
   python3 -m py_compile src/x_kol_sync/*.py scripts/*.py
   git diff --check
   plutil -lint launchd/*.plist
   ```

4. 对同步、去重、翻译状态或发送幂等逻辑增加测试；
5. 说明是否改变数据库字段、审核策略、兼容迁移或 launchd 调度行为。

数据库集成测试设置 `X_KOL_RUN_DB_TESTS=1` 后启用，使用当前连接中的临时表隔离测试数据，不修改正式表。GitHub Actions 自动运行单元测试和 MySQL/Redis 集成测试。

项目只读 X 内容，不接受发帖、点赞、关注、转推、收藏或回复功能的贡献。
