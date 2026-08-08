# WeFlow 本地 API 兼容契约

本项目只兼容用户已经合法持有、自行配置并在本机运行的 WeFlow 历史或兼容 API 快照。开发时曾在本机验证 WeFlow 5.1.0，但这不代表当前上游版本仍提供相同能力。

[WeFlow 当前官方仓库](https://github.com/hicccc77/WeFlow)已经说明移除了旧有的密钥获取和数据库解密能力。本项目不分发旧版 WeFlow，不提供密钥提取、数据库解密、权限绕过或规避平台限制的指导。无法合法取得兼容本地 API 时，仍可使用 `demo.py` 查看纯合成界面，或直接运行 `ranker.py --input ...` 分析你有权处理的 Markdown 文本。

## 安全边界

- API 必须使用 `http://127.0.0.1`、`http://localhost` 或其他 loopback 地址。
- 本项目会拒绝连接局域网地址和公网地址。
- 如果 API 配置了 token，请通过 WeFlow 本地配置或 `WEFLOW_API_TOKEN` 环境变量提供；不要写进仓库、截图或 issue。
- 下列示例只描述数据形状，不是真实聊天数据，也不是完整的 WeFlow API 文档。

## 所需端点

### `GET /health`

用于判断本地服务是否启动。成功时应返回可解析的 JSON；本项目不依赖固定的健康字段。

### `GET /api/v1/sessions?limit=1000`

响应需包含 `sessions` 数组。每个群会话至少需要：

```json
{
  "sessions": [
    {
      "username": "<group-id>",
      "displayName": "合成家教群",
      "type": 2,
      "lastTimestamp": 1760000000
    }
  ]
}
```

群标识通常以 `@chatroom` 结尾；`type: 2` 也会被视为群会话。`lastTimestamp` 使用 Unix 秒时间戳。

### `GET /api/v1/messages`

查询参数：

| 参数 | 含义 |
| --- | --- |
| `talker` | 群会话标识 |
| `start` | 起始 Unix 秒时间戳 |
| `limit` | 本轮最大返回条数 |

响应需包含 `messages` 数组。每条消息至少应提供时间、正文和稳定标识中的可用部分：

```json
{
  "messages": [
    {
      "serverId": "<message-id>",
      "localId": "<local-id>",
      "createTime": 1760000000,
      "senderUsername": "<member-id>",
      "content": "这是纯合成消息正文"
    }
  ]
}
```

正文兼容字段还包括 `displayContent`、`text` 和 `parsedContent`。`serverId` 或 `localId` 均缺失时，本项目会用群标识、时间和正文生成本地哈希键。

### `GET /api/v1/group-members?chatroomId=<group-id>`

该端点用于把发送者标识转换为本地显示名。不可用时不会阻止拆单，只会保留原发送者标识。

```json
{
  "members": [
    {
      "wxid": "<member-id>",
      "groupNickname": "合成昵称",
      "displayName": "合成显示名"
    }
  ]
}
```

显示名会按 `groupNickname`、`displayName`、`remark`、`nickname`、`alias` 的顺序选择第一个非空值。

## 配置发现

默认从 `%APPDATA%\weflow\WeFlow-config.json` 读取：

- `httpApiHost`
- `httpApiPort`
- `httpApiToken`

非默认安装可使用：

| 环境变量 | 作用 |
| --- | --- |
| `WEFLOW_CONFIG_PATH` | WeFlow 本地配置文件路径 |
| `WEFLOW_EXE_PATH` | 可选的 WeFlow 可执行文件路径 |
| `WEFLOW_API_BASE` | 本机 API 根地址，只接受 loopback HTTP |
| `WEFLOW_API_TOKEN` | Bearer token，敏感信息 |

## 兼容性自检

```powershell
.\check-setup.ps1
```

自检会验证配置、sessions 响应以及首个已匹配群的 messages 响应结构，不会把 token 打印到终端。若没有任何匹配群，`message_schema_probe_ok` 会显示 `null`；此时先运行 `.\check-setup.ps1 -ListGroups` 并调整群筛选。

API 版本没有统一协商机制。升级或更换 WeFlow 后，应重新运行自检，再手动确认报告中的消息数量、日期和群范围；健康接口成功不等于消息读取一定兼容。
