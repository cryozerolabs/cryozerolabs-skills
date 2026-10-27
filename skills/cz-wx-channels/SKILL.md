---
name: cz-wx-channels
description: 接收单条或批量视频号分享链接，复用本地 wx_channels_download 自动解析、解密并下载经过验证的 MP4，返回本地文件与来源元数据。适用于企微视频号链接、sph 分享链接、链接列表和聊天记录 JSON；企微取链通过独立适配器接入。
metadata:
  author: CryoZeroLabs
  version: 0.1.0
---

# cz-wx-channels

把分享链接交付为经过验证的 MP4 和 `manifest.json`。复用已运行的本地下载器，通过详情接口和 `/play` 完成批量解析与媒体解密。

## 安装与环境准备

用户提出安装或配置请求时，由 AI Agent 完成以下操作：

1. 找到当前 AI Agent 使用的 Skills 目录，检查已有的同名技能。已有安装可直接复用；用户要求更新时使用安装脚本的 `--replace`，现有副本会备份到独立目录。
2. 从本技能目录调用 `scripts/install_local.py` 安装独立副本。Codex 使用脚本默认位置，其他位置通过 `--destination` 指定。后续命令从安装副本执行。
3. 检查 Python 3.10+、FFmpeg/ffprobe 和本地 `wx_channels_download`。优先复用已有工具；缺少时使用当前系统的安装方式准备依赖，下载器从[官方仓库](https://github.com/ltaoo/wx_channels_download)的 Release 获取，并按上游文档完成初始化。
4. 读取已有本机配置，复用下载器地址、工具路径和启动器。配置字段与默认路径见[集成说明](references/integration.md)。需要启动服务时，优先执行已配置的 `backend_launcher`。
5. 运行 `doctor`，根据返回字段检查服务与文件校验工具。详情缓存可直接使用；遇到 `client_not_connected` 时，通过当前可用的应用工具完成一次视频号首页连接，再继续该批任务。
6. 向用户报告安装和运行准备结果，并给出直接使用的自然语言示例。

```bash
python3 scripts/install_local.py
python3 scripts/install_local.py --destination "$SKILLS_DIR/cz-wx-channels"
```

以上两条对应默认位置和指定位置，按当前环境选择一条执行。环境准备和下载操作由 AI Agent 执行，沿用当前会话已有配置和授权。

## 执行入口

在已安装的 skill 目录执行命令。采用用户指定的输出目录；用户未指定时，在系统下载目录创建本次任务的输出目录。运行产物放在源码目录之外。

```bash
python3 scripts/channels.py doctor
python3 scripts/channels.py download --url "$VIDEO_URL" --output "$OUTPUT_DIR"
python3 scripts/channels.py download --input "$LINKS_JSON" --output "$OUTPUT_DIR"
```

默认执行 `download`；用户明确只要媒体地址时执行 `resolve`。`--url`、`--input` 均可重复，支持 TXT、JSON、JSONL 和 `--input -` 标准输入。输入会按视频分享标识去重。

## 运行流程

1. 已有链接时直接执行核心入口。需要从企微取链接时，使用当前可用的企微 MCP、CLI 或独立适配器，得到标准链接列表后再调用核心入口。
2. 执行 `doctor` 检查服务、首页连接和 ffprobe，按“安装与环境准备”处理需要准备的项目。`reachable` 表示服务是否可达，`connected` 表示首页当前是否连接；已有缓存可继续解析和下载。
3. 单条链接通过 `--url` 传入，多条链接可重复使用 `--url` 或写入 TXT、JSON、JSONL 后通过 `--input` 传入。`--input -` 接收标准输入。需要示例结构时读取[输入示例](examples/links.json)。详情 API 复用已有缓存，新视频通过首页连接查询，整批由接口执行。
4. 调用 `download` 保存 MP4。脚本自动解密、检查文件头、校验视频流、计算 SHA-256，并逐条保存结果。默认对可重试错误重试一次。
5. 重跑同一输入和输出目录时，已交付文件通过校验后直接复用，失败条目继续处理。使用 `--refresh` 可重新下载。

## 企微取链适配器

```bash
python3 adapters/wecom/export_links.py --input "$MESSAGES_JSON" --output "$LINKS_JSON"
python3 adapters/wecom/export_links.py --group "$GROUP_NAME" --output "$LINKS_JSON"
```

第一种读取聊天记录或已有提链结果；第二种调用本机配置中的 `wecom_exporter`。现有数据库解密提链器作为上游运行，核心始终接收分享链接。

## 交付

- `videos/*.mp4`：下载后的完整文件，含视频流校验、实际编码、分辨率、时长和 SHA-256。
- `manifest.json`：逐条状态、文件信息、标题、作者、分享链接、来源上下文及错误详情。
- `mp4-links.txt`：通过验证的本机播放地址，便于后续程序读取。

给后续 skill 或 MCP 的主要交接字段为 `items[].file.path` 和 `items[].source_url`，同时传递相关标题、作者与上下文。只解析时交接 `items[].media.playback_url`；该地址由当前本机下载器提供解密服务。

最终报告列出完成数量、文件和 manifest 路径；部分失败时列出条目与对应错误码。继续复用用户正在使用的下载器，按当前任务约定管理其生命周期。
