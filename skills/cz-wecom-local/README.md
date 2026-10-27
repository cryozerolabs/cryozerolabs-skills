# cz-wecom-local

[![X · @cryozerolabs](https://img.shields.io/badge/X-%40cryozerolabs-000000?logo=x&logoColor=white)](https://x.com/cryozerolabs)
[![MIT License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**让 AI 读取企业微信聊天，补全链接和附件，保留原始消息。**

分析群聊时，如果只拿到卡片标题和附件名，AI 就缺少原始链接和文件内容。`cz-wecom-local` 将本机消息解密结果与企业微信官方 CLI 提供的信息整理到一起，交给后续工具使用。

## 能做什么

- 按会话和时间读取本机聊天记录。
- 提取正文片段、分享链接和附件信息。
- 复用已校验的本机附件，通过官方 CLI 获取其他普通附件。
- 统一交付消息、文件、来源和解析状态，保留原始数据供后续使用。

## 安装

```text
请读取 https://github.com/cryozerolabs/cryozerolabs-skills/blob/main/skills/cz-wecom-local/SKILL.md ，帮我安装并配置这个技能。
```

## 使用

> 读取这个群的聊天，正文、分享链接和附件都要保留。

> 找出这段聊天里的资料，取得附件后交给相应工具读取内容。
