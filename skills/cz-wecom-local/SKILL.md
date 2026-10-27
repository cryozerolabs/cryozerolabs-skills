---
name: cz-wecom-local
description: 按会话和时间读取企业微信本机聊天消息，通过只读副本解密并结合官方 wecom-cli 补全正文、链接和附件，统一交付来源、解析状态和原始数据引用。适用于完整读取群聊、恢复分享卡片链接、取得聊天附件、读取本地缓存历史，或向其他 Skill 提供消息与文件。
metadata:
  author: CryoZeroLabs
  version: 0.1.0
---

# cz-wecom-local

读取并交付通用消息数据。根据用户任务选择下游工具，保留每种来源的原始记录和未解释字段。

## 安装与本机准备

安装来源：[cryozerolabs-skills/skills/cz-wecom-local](https://github.com/cryozerolabs/cryozerolabs-skills/tree/main/skills/cz-wecom-local)。先取得完整技能目录；下列相对路径均以该目录为起点。

1. 在当前 Agent 的 Skills 目录安装独立副本：从本技能目录执行 `python3 scripts/install_local.py`；其他 Agent 通过 `--destination "$SKILLS_DIR/cz-wecom-local"` 指定目录。更新时使用 `--replace`，旧副本备份到 Skills 目录之外。
2. 准备 Python 3.10+ 及 `requirements.txt` 中的依赖，优先使用已有环境；需要新环境时放在本机数据目录的 `venv/`，与发布源码分开。
3. 在本机配置消息库路径与密钥文件。优先复用已经验证的本机密钥；通过 `keys --import-file "$KEY_FILE"` 导入独立副本。已有取钥工具可接入 `key_provider.argv`，执行 `keys` 时才调用。配置和命令详见 [references/integration.md](references/integration.md)。密钥未通过验证时保留明确错误，不把仅有官方接口结果报告成完整解密读取。
4. 使用官方补全或附件下载时，检查 `wecom-cli --version` 不低于 1.2.1，以及 `wecom-cli auth show --status` 为 `authorized`；仅在缺少工具或授权时按官方安装、授权流程准备。
5. 从安装副本执行 `python3 scripts/wecom_local.py doctor`。复用本机配置，不把密钥、数据库或账号路径写进发布源码。

## 读取消息

```bash
python3 scripts/wecom_local.py groups --name "$GROUP_KEYWORD"
python3 scripts/wecom_local.py read --group "$GROUP_NAME" --since "$START_TIME" --until "$END_TIME" --output "$OUTPUT_DIR"
```

- `--source both` 为默认值：读取本机副本，并按官方接口可提供的时间范围补充数据。`local` 只读本机，`official` 只走官方接口。
- 时间可以使用带时区的 ISO 格式或配置时区下的 `YYYY-MM-DD HH:MM:SS`；开始时间包含，结束时间不包含。不指定时间时读取目标会话的本机缓存，并查询官方接口当前可返回的近期消息。
- 会话名称需要唯一匹配；多账号时使用本机配置或 `--db` 选择消息库。先确认目标，避免把不同会话的数据混在一起。
- 采用用户指定的输出目录；未指定时在系统下载目录创建本次任务的目录。每次读取使用新目录，保留此前快照。
- 客户端关闭但本机消息库和已验证密钥仍在时，可直接读取缓存。数据库操作在私有副本上进行，只合并已经提交的 WAL 数据，临时明文数据库在读取结束后删除。

“最近”不表示“字段完整”。需要完整消息时检查 `coverage`、卡片链接、附件状态和未解析项；正文相同可复用，卡片缺少 URL 时保留本地补充字段。源之间只按唯一匹配证据关联；无法确认对应关系时保留各自记录，不猜测合并。

## 链接、附件和复杂消息

- 文本、分享卡片、小程序中的 URL 按来源记录到 `links`，保留标题和原始消息引用。获取链接不表示已经读取目标网页；需要网页正文时再调用合适的工具。
- 附件首先提供消息中的文件信息和获取状态。本机文件需要通过大小、校验值及真实路径验证；发送者设备路径不能当作当前电脑的文件路径。
- 用户要求读取附件内容或任务需要基于附件作答时，取得实际文件后交给相应的文档、图片或媒体解析工具。只有文件名或下载引用不能算已读附件。

```bash
python3 scripts/wecom_local.py fetch --bundle "$BUNDLE_JSON" --attachment-ref "$ATTACHMENT_REF"
python3 scripts/wecom_local.py fetch --bundle "$BUNDLE_JSON" --all
```

根据用户需要选择一个附件或整批。脚本优先复制已验证的本机文件，其他普通附件通过官方 `message files get` 取得；逐个保存结果。官方拒绝访问、文件受保护或取回内容不匹配时记录错误，不改用其他网络接口绕过。

`file_ready` 只表示实际文件已取得；脚本不会把它标成正文已解析。需要总结附件时，继续使用下游文件解析工具，并在最终报告中说明实际读到的内容及未读取项。

重复 protobuf 字段、嵌套消息和未知类型均保留。`partial` 或 `raw_only` 项可通过 `wire_fields_ref` 和 `raw_refs` 按需检查；没有实际解析的内容不编造摘要。

## 交付和下游衔接

- `messages.json`：统一消息、来源、会话、时间、正文片段、链接、附件状态和覆盖范围。
- `links.json`：所有已提取链接及其来源消息。
- `attachments.json`：附件信息、获取状态、文件路径和校验结果。
- `raw/local.jsonl`：选中会话的原始数据库行，二进制内容以 Base64 保留。
- `raw/wire.jsonl`：保留顺序、重复字段和嵌套结构的 protobuf 字段树。
- `raw/official.json`：该次官方接口返回的消息。
- `attachments/`：按需取得并校验后的文件。

把 `messages.json` 作为主要交接入口，相关附件通过 `messages[].attachments[].file.path` 提供，保留 `raw_refs`、`parse_status` 与 `coverage`。结构定义见 [references/integration.md](references/integration.md)。原始 JSON 中的机器标识用于接口关联，向用户说明结果时使用会话名、文件名和可读链接。

最终报告说明来源覆盖、消息与附件数量、未解析类型、未取到或未读取的附件，以及交付文件位置。退出码 `0` 为读取完成，`1` 为部分来源或附件失败但成功数据已保留，`2` 为参数或环境需要调整。
