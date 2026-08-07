# KiraAI-Plus-One · 群聊 +1 复读插件

让你的 AI 也能愉快地在群聊里 +1！

一个运行在 [KiraAI](https://github.com/xxynet/KiraAI) 上的插件：自动检测群聊中的「复读」，达到阈值后提醒 AI 加入，和群友一起快乐地刷屏。

## ✨ 功能特性

- **自动检测复读**：同一会话内连续出现相同纯文本消息，达到阈值即触发
- **阈值可配**：`random` 模式在最小/最大条数间随机；`fixed` 模式固定为最小条数
- **AI 可加入复读**：检测到复读后向 AI 发送提示，AI 输出 `<msg><plus1>Yes</plus1></msg>` 即可跟上复读
- **可选：打断复读**：开启后 AI 可输出 `<msg><plus1>No</plus1></msg>`，向群里发送自定义内容打断复读（默认关闭）
- **黑名单过滤**：内容包含黑名单词的消息不参与复读检测，避免敏感内容被复读扩散
- **会话隔离**：每个会话独立计数，可对指定会话关闭复读

## 📦 安装

1. 将本插件复制到 KiraAI 的 `data/plugins/plus_one/` 目录
2. 重启 KiraAI（或刷新 WebUI 插件页）
3. 在 WebUI 插件设置中按需配置

## ⚙️ 配置项

| 配置键 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `min_nums` | integer | `2` | 最小 +1 条数 |
| `max_nums` | integer | `5` | 最大 +1 条数（需大于最小条数） |
| `mode` | enum | `random` | `random`: 随机阈值；`fixed`: 固定为最小条数 |
| `disallowed_sessions` | list | `[]` | 禁止复读的会话 ID，每行一个（留空不限制） |
| `blacklist` | list | `[]` | 复读黑名单词，命中内容不参与复读检测 |
| `enable_interrupt` | switch | `false` | 是否允许 AI 输出 `No` 打断复读 |
| `interrupt_message` | string | `打断！` | AI 打断复读时发送到群里的内容 |

## 🔄 工作原理

1. 插件按会话维护计数器，记录上一条收到的纯文本消息
2. 相同消息连续出现 → 计数 +1；出现不同消息 → 计数重置
3. 计数达到阈值 → 向 AI 发送系统提示「群友正在复读…」
4. AI 输出 `<plus1>Yes</plus1>` → 插件把被复读的内容原样发送到群里
5. （可选）AI 输出 `<plus1>No</plus1>` → 插件发送自定义内容打断复读

> 图片、回复、转发等非纯文本消息不会参与复读；包含黑名单词的消息同样被跳过。

## 🔗 相关链接

- [KiraAI](https://github.com/xxynet/KiraAI) — 插件运行的主项目
- [KiraAI 文档](https://docs.kira-ai.top) — 插件开发与使用文档
