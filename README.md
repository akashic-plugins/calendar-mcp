# calendar-mcp

Google Calendar 的 Akashic 插件，使用 Plugin API v3 提供日历 MCP、受管 API
进程和主动提醒事件源。

```text
calendar-mcp
├─ akashic.plugin.toml
├─ plugin.py
└─ mcp
   ├─ run_mcp.py
   ├─ run_server.py
   └─ src
```

`plugin.py` 只向 Core 声明运行时能力，不启动进程、不访问 Google API，也不写
插件数据。Core 从静态 manifest 准备独立 Python runtime，并在正式 generation
中启动 `calendar_api` 与 `calendar` MCP；候选验证只允许调用只读的
`get_proactive_events`。

插件代码安装到 `~/.akashic-plugin/cache/<marketplace>/calendar/<version>/`，运行数据保存在 `~/.akashic-plugin/data/calendar-<marketplace>/`：

- `.env`：Google OAuth 客户端配置
- `.gcp-saved-tokens.json`：OAuth Token
- `proactive_alerts.json`：主动提醒配置
- `calendar_alerts.sqlite3`：提醒确认状态
- 运行日志：只写 stdout/stderr，由 Core 的 bounded log ring 收集

从 v2 升级时，先停止占用该 workspace 的 Akashic，再从带 Core 依赖的环境执行：

```bash
python scripts/migrate_v2_data.py \
  --workspace /path/to/workspace \
  --marketplace github
```

迁移命令持有 workspace 独占锁，保留 `mcp/calendar-mcp` 原数据，用 SQLite
在线备份复制状态，拒绝覆盖不同内容，并在
`plugin-data/calendar-<marketplace>/.calendar-v2-migration.json` 写最终 receipt。
进程内失败会回滚本次新增文件；进程崩溃后重跑会核对并收束已发布的同内容文件。
正式发布 Gate 必须在启动 v3 generation 前验证 receipt 和前四项持久数据。

v3 `apply()` 不会自动复制或覆盖数据，以免候选验证改写正式 OAuth 和确认状态。
候选 MCP 使用无凭证、无外网的 recording backend；正式 runtime 只从自己的
plugin-data `.env` 读取 OAuth 配置。刷新 OAuth Token 后会立即写回正式数据目录。

主动拉取返回明确的 `empty/items` 结果；只有全部 event ID 持久确认后
才返回 `committed`。异常和部分确认会 fail-loud，不伪装成成功。
