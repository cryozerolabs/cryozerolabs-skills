# 输入、配置与结果交接

## 输入

核心支持 `https://weixin.qq.com/sph/...` 和 `https://channels.weixin.qq.com/finder-preview/pages/feed?eid=...`。规范化后的分享标识用于去重，输入中的群名、分享时间等信息作为来源元数据保留。

```json
{
  "items": [
    {
      "url": "https://weixin.qq.com/sph/EXAMPLE",
      "title": "示例视频",
      "author": "示例作者",
      "context": {"group": "示例群", "shared_at": "2026-01-01T10:00:00+08:00"}
    }
  ]
}
```

企微适配器能从嵌套聊天记录 JSON 的文本或链接字段中提取上述 URL，处理 XML 中的 `&amp;` 和 JSON 转义斜线，输出相同的 `items` 格式。

## 本机配置

配置路径按 `--config`、`CRYOZEROLABS_WECOM_VIDEO_CONFIG`、默认路径的顺序选择。

- macOS：`~/Library/Application Support/cryozerolabs-wecom-video/config.json`
- 其他系统：`$XDG_CONFIG_HOME/cryozerolabs-wecom-video/config.json`，默认位于 `~/.config`。

核心配置示例见 [config.example.json](../examples/config.example.json)。`backend_url` 默认为 `http://127.0.0.1:2022`，`ffprobe` 默认从 PATH 查找。

企微群模式读取本机配置中的导出器：

```json
{
  "backend_url": "http://127.0.0.1:2022",
  "ffprobe": "ffprobe",
  "wecom_exporter": {
    "argv": ["python3", "/path/to/your/exporter.py", "--group", "{group}"],
    "result_file": "/path/to/your/links.json",
    "timeout": 180
  }
}
```

`{group}` 替换为 `--group` 的值，命令逐项传入进程。适配器读取该次更新的 `result_file`，再导出标准链接列表。数据库解密、密钥缓存和群消息选择由现有提链器负责；其路径和配置保存在本机配置文件。

启动器可作为本机配置中的 `backend_launcher` 保存 `argv`、`cwd`，供 agent 在服务未运行时复用。`doctor` 检查服务就绪状态。

## 调用本地下载器

1. `GET /api/channels/status` 检查 `data.available`。
2. `GET /api/channels/feed/profile?eid=...` 处理企微链接，或 `?url=...` 处理 sph 分享链接。
3. 从 `object.objectDesc.media[]` 取得 `url`、`urlToken`、`decodeKey`。
4. `GET /play?url=...&key=...` 读取解密后的 MP4。解析模式验证前 4096 字节；下载模式保存完整文件后执行 ffprobe。

调用始终复用本机 HTTP 服务。播放地址与媒体有效时间记录在 manifest；已经下载的文件可以独立交给后续工具。

## 结果

manifest 的 `schema_version` 为 `1.0`。顶层含输入数量、去重数量、执行时间、`summary` 与 `items`。

每条任务状态为 `resolved`、`downloaded`、`reused` 或 `failed`。成功文件放在 `file` 对象中：

```json
{
  "path": "/absolute/path/video.mp4",
  "size_bytes": 123456,
  "sha256": "文件内容的 SHA-256",
  "duration_seconds": 24.845,
  "width": 720,
  "height": 1280,
  "video_codec": "h264",
  "audio_codec": "aac",
  "verified": true
}
```

`source_url`、标题、作者和 `contexts` 与文件保存在同一条记录中。`media` 包含上游媒体地址、本机播放地址、解密方式与有效时间（接口提供时）。manifest 和链接清单使用文件权限 600。

## 重试与继续执行

默认每条最多执行两次；`--retries` 设置额外重试次数。每处理一条就写入 manifest，下载先写入临时文件，校验后交付为 `.mp4`。重新运行原命令时，成功文件经视频流与 SHA-256 校验后复用，其他条目继续执行。

退出码 `0` 表示本批完成，`1` 表示存在失败条目，`2` 表示输入或运行环境需要调整。结构化错误包含 `code`、`message` 和 `retryable`。`client_not_connected` 对应首页连接，`backend_unavailable` 对应本机服务，`profile_error` 对应详情接口，`incomplete_download` 对应文件传输。
