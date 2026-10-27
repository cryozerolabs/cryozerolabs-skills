# 本机配置和通用消息结构

## 配置位置与密钥接入

配置按 `--config`、`CZ_WECOM_LOCAL_CONFIG`、默认路径选择。macOS 默认配置为 `~/Library/Application Support/cz-wecom-local/config.json`；其他系统使用 `$XDG_DATA_HOME/cz-wecom-local/config.json`，未设置时为 `~/.local/share/cz-wecom-local/config.json`。

```json
{
  "database": "/path/to/profile/Messages1/Info.db",
  "key_file": "/path/to/private/state/keys.json",
  "data_dir": "/path/to/private/runtime",
  "cache_roots": ["/path/to/optional/download-cache"],
  "wecom_cli": "wecom-cli",
  "timezone": "Asia/Shanghai",
  "cli_interval_seconds": 8
}
```

不指定 `database` 时，读取器在 macOS 本机 Profiles 目录中，用已验证密钥匹配唯一消息库；可用 `profile_roots` 配置其他目录。原数据库不写入，临时快照位于 `data_dir/tmp/`，读取后删除。

密钥文件为消息库名称到密钥条目的 JSON 映射，`Info.db` 条目使用 `enc_key` 字段保存 16 字节原始密钥的十六进制编码。不要把实际值写进文档、终端输出或发布源码。使用已存在的本机密钥文件：

```bash
python3 scripts/wecom_local.py keys --db "$DATABASE" --import-file "$EXISTING_KEY_FILE"
```

导入先校验数据页，再把独立副本写入本技能的私有密钥目录。可配置既有取钥工具：

```json
{
  "key_provider": {
    "argv": ["python3", "/path/to/local/key-provider.py", "--db", "{db}", "--output", "{output}"],
    "timeout": 180
  }
}
```

取钥器接收真实消息库路径和私有临时输出路径，产出上述密钥 JSON。`keys` 显式调用该工具；标准输出、错误输出不传给消息消费方，取出的密钥经验证后才保存。刷新失败时现有密钥保持不变。配置文件与密钥文件使用权限 600，私有数据目录使用 700。取钥工具的系统授权沿用其正常流程。

解密器实现 wxSQLite3 AES-128-CBC 的 4096 字节页格式，首个数据页验证头部片段，再使用 SQLite 检查消息表和会话表。格式验证错误会明确返回；保留原数据库与旧密钥。

## 来源范围与合并

`coverage.local` 记录本机缓存条数、首末时间和数据库完整性检查。缓存范围不等于完整服务器历史。

`coverage.official` 记录官方实际查询时间窗口、条数和是否裁剪。官方聊天接口目前提供最近七天，分页失败时保留已取得的页，标记 `partial`。一个来源暂不可用时仍交付另一来源，同时报告缺口。

两源只在同一会话和发送时间下按正文、卡片标题、附件名等证据唯一匹配。发送者映射只从唯一一致的正文样本建立；图片、语音、视频的关联需要此映射。重复候选或多个本地记录对应同一官方记录时不合并。原始变体始终保留，`summary.message_records` 不被宣称为绝对去重后的真实消息数。

官方会话目录可能把同一会话的不同时间段分别返回。相同会话引用的目录项先合并；名称相同而引用不同的会话继续保留为多候选，不默认选择。

## 消息字段

`schema_version` 为 `1.0`，顶层为 `conversation`、`query`、`coverage`、`summary`、`messages`。

| 字段 | 含义 |
| --- | --- |
| `message_ref` | 本次交付内的机器引用，用于下游关联 |
| `conversation` / `sent_at` | 会话名和带时区时间 |
| `sender` | 已确认的发送者名称及来源内关联引用 |
| `kind` | text、link_card、miniapp、file、image、voice、video、mixed、message_bundle 或 unknown |
| `parts` / `text` | 按顺序保存的文本片段和可提取正文；未知片段单独标记 |
| `links` | URL、提取来源及可取得的标题等元数据 |
| `attachments` | 文件信息、取得状态及下载引用 |
| `sources` / `official_variant` | 信息来源和官方接口表现形式 |
| `parse_status` | parsed：已识别主要内容；partial：仅部分语义已提取；raw_only：保留原始记录待解释 |
| `raw_refs` | 原始 JSONL 行或 JSON Pointer 引用 |
| `wire_fields_ref` | 本地二进制消息的字段树引用；顺序和重复字段均保留 |
| `merge_status` | matched 或 ambiguous；没有充分匹配证据时保留各来源记录 |

所有原始消息字段按原值保存，二进制内容以 Base64 表达。字段树是辅助解释，不替代原始字节；达到解析深度限制时保留对应二进制内容。

## 附件状态

- `metadata_only`：只有附件记录，尚未确认本机文件或官方引用。
- `download_reference_available`：存在唯一关联的官方下载引用，尚未取得文件。
- `file_ready`：实际文件已经取得，`file` 包含路径、字节数、SHA-256 和与消息记录的校验结果。
- `content_status: not_read`：文件尚未交给下游解析。文件取得和正文读取是两个独立状态。
- `error`：该附件最近一次获取失败的明确原因；其他成功文件仍然保留。

本机普通文件附件通过消息中记录的大小和 MD5 与缓存匹配。发送者设备路径只作为原始字段保留，不作为本机路径。官方引用只能来自该会话的消息返回，不能从本地下载凭据、URL 或数值自行拼造。

`fetch` 使用官方 `message files get`，校验取得文件后落地。PDF 和 Office 文件额外检查文件头，随后仍由合适的下游工具读取具体内容。官方受保护或无权限的文件保留错误，不采用其他网络接口替代。

配置、密钥、数据库、原始消息及运行产物都位于仓库之外；安装器复制代码，更新前备份旧安装，不创建符号链接或同步关系。
