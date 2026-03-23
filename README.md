# WeChat Auto-Reply (OpenClaw)

本项目是一个运行在 macOS 本地的微信自动回复 agent，基于 UI 自动化 + OCR，不依赖微信官方 API。

当前实现重点是“可控、可诊断、可恢复”：

- 强约束触发逻辑
- FIFO 待发送队列
- 明确的日志事件
- Gateway 命令可直接观测状态

## 目录结构

```text
apps/
  gateway/         # Gateway 控制端（on/off/status/queue/diagnose/restart）
  runner/          # Runner 主循环入口
docs/
  ARCHITECTURE.md
  OPERATIONS.md
tools/
  wechat_row_badges.swift   # 聊天列表红点检测
  wechat_bubble_roles.swift # 聊天气泡角色（入站/出站）辅助识别
wechat_autoreply/
  orchestrator.py   # 核心状态机
  wechat_ui.py      # 微信窗口探测、点击、OCR提取
  vision.py         # 状态栏微信图标+数字检测
  idle.py           # 全局空闲时长（Quartz）
runtime/
  config.json
  state.json
  events.jsonl
  captures/
```

## 兼容入口

- `main.py` -> `apps.runner.cli`
- `gateway_control.py` -> `apps.gateway.cli`

## 当前核心逻辑（已落地）

### 1) 状态栏检测门控（Idle First）

- 只有在 `idle >= 30s` 时才会检测状态栏数字。
- `idle < 30s` 时不采样状态栏，并清空临时 menu signal，避免“旧数字缓存”导致延迟误触发。

### 2) 自动打开微信只允许两种原因

- `claim_scan`：认领流程（状态栏检测到有数字后触发）。
- `pending_send_due`：队列中某条草稿到达 5 分钟发送时间。

除此之外不允许出现其他“自动开微信”业务路径。

### 3) 认领流程（claim_scan）

触发条件：本轮状态栏检测拿到可执行数字（`signal` 为正整数）。

打开微信后只走两类业务分支：

1. 白名单联系人且有未读红点：
   - 打开该会话
   - 读取入站文本
   - 生成草稿
   - 写入 `pending_queue`
2. 非白名单联系人但有未读红点：
   - 依次点开以清除红点（不入队）

如果状态栏有数字，但打开后既没有白名单未读也没有非白名单未读，会记事件：

- `claim_logic_bug`
- reason: `非正常状态栏数字和红点`

### 4) 待发送队列（pending_queue）

- 队列是 FIFO；发送时总是先处理队头。
- 每条 pending 包含：`contact`、`inbound_text`、`inbound_fingerprint`、`draft_text`、`created_at`、`due_at`。
- 默认延时发送：`300s`（5分钟）。
- 超时垃圾回收：`pending_stale_ttl_seconds`（默认 86400 秒）。

### 5) 到点发送流程（pending_send_due）

触发条件：

- 队列非空
- 当前 idle 满足阈值
- 队头 `due_at` 已到

发送逻辑：

1. 打开微信并选中队头联系人
2. 再次读取当前会话，做安全校验
3. 若确认可发，粘贴草稿并发送
4. 二次确认发送结果，成功则出队
5. 若未确认，进入重试（默认最多 2 次）

### 6) “已手动回复”保护（避免误发）

以下任一满足会取消对应 pending：

- 最新气泡检测为我方出站
- 会话 preview 与我方最近出站文本匹配
- 发现我方最新文本变化（非自动草稿回显）

这保证“你已经在别处回过”的消息不会再次自动发送。

### 7) Barrys 相关修复（已包含）

已加入两类防误判：

- 聊天列表红点检测收紧 ROI 与形态阈值，减少把头像红色误当红点。
- 预览文本匹配我方出站时，不再把 OCR 抖动入站（如 `8~`）认领进队列。

## 识别链路简述

- 状态栏：`vision.py` 检测微信图标右侧数字信号。
- 列表红点：`tools/wechat_row_badges.swift` 对每行头像区域做红色连通域判断。
- 聊天气泡角色：`tools/wechat_bubble_roles.swift` + `wechat_ui.py` 辅助区分 inbound/outbound。
- 文本提取：OCR + 行聚合（防碎片化）。

## Gateway 常用命令

```bash
./wechat_env/bin/python gateway_control.py on
./wechat_env/bin/python gateway_control.py off
./wechat_env/bin/python gateway_control.py status
./wechat_env/bin/python gateway_control.py queue
./wechat_env/bin/python gateway_control.py diagnose
./wechat_env/bin/python gateway_control.py reset
./wechat_env/bin/python gateway_control.py restart
./wechat_env/bin/python gateway_control.py style-show
./wechat_env/bin/python gateway_control.py style-set "自然、简短、口语化，不要句号"
./wechat_env/bin/python gateway_control.py command
./wechat_env/bin/python gateway_control.py /command
```

说明：

- `on`：开启微信自动回复 runner。
- `off`：关闭微信自动回复 runner（不再认领和发送）。
- `status`：查看当前开关状态与最近关键记录。
- `queue`：查看当前待发送队列（联系人、剩余时间、入站、草稿）。
- `diagnose`：输出详细诊断（最近事件、状态栏信号、错误、队列）。
- `reset`：清空 runtime state 并重启 runner（等价于快速回到干净状态）。
- `restart`：清空 runtime state 并重启 runner（用于回到“干净聆听状态”）。
- `style-show`：查看当前微信自动回复语气规则。
- `style-set "<文本>"`：更新微信自动回复语气规则（写入 `reply_style_instructions`）。
- `command` / `/command`：查看全部 Gateway 指令说明。

## 回复语气配置（微信自动回复专用）

- 配置文件：`runtime/config.json`
- 字段：`reply_style_instructions`
- 当前规则包含：回复句尾不加句号（`Omit sentence-final periods in each reply.`）
- Emoji 相关字段：
  - `emoji_pack_zip_path`（默认 `/Users/<你的用户名>/Downloads/wechat-emoji-main.zip`）
  - `reply_emoji_enabled`（默认 `true`）
  - `reply_emoji_min_count`（默认 `1`）
  - `reply_emoji_max_count`（默认 `2`）
- 程序会优先读取 `emoji_pack_zip_path` 里的微信默认表情代码名（例如 `[微笑]`、`[捂脸]`、`[旺柴]`），用于提示词与自动补表情。
- 修改该字段后执行 `./wechat_env/bin/python gateway_control.py restart` 使配置立即生效
- 也可直接用 Gateway：
  - `./wechat_env/bin/python gateway_control.py style-show`
  - `./wechat_env/bin/python gateway_control.py style-set "你的语气规则"`

## 关键日志事件

- `menu_bar_checked`
- `wechat_window_action` (`reason=claim_scan|pending_send_due`)
- `claim_candidates`
- `draft_saved_locally`
- `pending_refreshed_latest`
- `auto_sent`
- `pending_cancelled`
- `non_whitelist_unread_cleared`
- `claim_logic_bug`（在 Gateway 中显示为“非正常状态栏数字和红点”）

## 运行与维护建议

1. 首次运行确认终端具备 macOS 权限：
   - Accessibility
   - Screen Recording
2. 测试前可先 `restart`，确保 queue/state 干净。
3. 若发现“状态栏有数字但微信内无红点”，优先看 `diagnose` 里的“非正常状态栏数字和红点”事件。
4. 变更核心逻辑后至少执行：
   - `./wechat_env/bin/python -m py_compile wechat_autoreply/orchestrator.py`
   - `./wechat_env/bin/python gateway_control.py diagnose`
