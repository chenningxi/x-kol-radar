# Contributing

感谢贡献。提交代码前请：

1. 不提交真实的推文台账、日志、截图、`.env`、Cookie、令牌或飞书群信息；
2. 在全新临时目录验证初始化流程；
3. 运行测试：

   ```bash
   python3 -m unittest discover -s src/x_kol_sync/tests -v
   ```

4. 对同步、去重、翻译状态或发送幂等逻辑增加测试；
5. 说明是否改变了 JSONL 字段或 launchd 调度行为。

项目只读 X 内容，不接受发帖、点赞、关注、转推、收藏或回复功能的贡献。
