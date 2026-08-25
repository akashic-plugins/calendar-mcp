# calendar-mcp

Google Calendar 的 Akashic 插件，使用 Plugin API v3 提供日历 MCP、受管 API
进程和普通 Content source。

```text
calendar-mcp
├─ akashic.plugin.toml
├─ plugin.py
└─ mcp
   ├─ run_mcp.py
   ├─ run_server.py
   └─ src
```

`plugin.py` 组合 Core 已有的 Managed Process、MCP、Timer、Content 与 lifecycle
能力。Core 在正式 generation 中启动 `calendar_api` 与 `calendar` MCP；只有正式
`RUNTIME_STARTED` 才登记一个 Timer。候选 generation 不登记 Timer，不调用
Calendar HTTP/Google，也不写正式 Content 或 Calendar 数据。

插件代码安装到 `~/.akashic-plugin/cache/<marketplace>/calendar/<version>/`，运行数据保存在 `~/.akashic-plugin/data/calendar-<marketplace>/`：

- `.env`：Google OAuth 客户端配置
- `.gcp-saved-tokens.json`：OAuth Token
- `content.json`：Content 日历来源配置
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

迁移会把旧 `proactive_alerts.json` 一次性改名为 `content.json`，并删除旧的
`enabled` 字段。运行时不读取旧文件，也不保留 `proactive.*` 双字段兼容层。

v3 `apply()` 不会自动复制或覆盖数据，以免候选验证改写正式 OAuth 和确认状态。
候选 MCP 使用无凭证、无外网的 recording backend；正式 runtime 只从自己的
plugin-data `.env` 读取 OAuth 配置。刷新 OAuth Token 后会立即写回正式数据目录。

每次 Timer 到点后，Calendar API 先冻结稳定 batch，插件调用 `Content.submit`，
成功后才 CAS 推进 Calendar cursor。交付完成后，插件先幂等确认 Calendar event，
再调用 `Content.ack`。任一步崩溃都会用同一 batch、item revision 或 event ID 重放。
没有新 item 时 API 明确返回 `no_batch`，不会调用 Content、创建 batch 或推进 cursor。
旧 `calendar_alerts.sqlite3` 的 pending/ack 事实会保留；第一次 schema 升级前会留下
`calendar_alerts.sqlite3.pre-content-v1.bak` 恢复副本。

```text
Timer ──▶ Calendar poll
              ├── no_batch ──▶ 等下一次 Timer
              └── batch ─────▶ Content.submit ──▶ cursor commit

Content.unsettled ──▶ Calendar ACK ──▶ Content.ack
```

已提交的 batch 和 Calendar ACK 历史全量保留，不用诊断日志代替真实 handoff
记录。运行日志仍由 Core 的 bounded log ring 固定容量轮转。新事件保存完整 payload；
旧 schema 未保存的 URL 等字段无法恢复，迁移行以 `legacy_fallback` 明确标记，并从
迁移后的 `content.json` 恢复 source 配置。
