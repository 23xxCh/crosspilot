# Amazon 采集表任务 API v1

供公司内部系统、ERP、RPA 或固定合作方提交 Amazon JSON 采集表。接口采用
异步任务模式：提交成功只表示文件已安全进入队列，不表示回填表已经完成。

## 连接信息

- Base URL：`http://127.0.0.1:8765/api/v1`
- 鉴权请求头：`X-API-Key: <调用方密钥>`
- 提交格式：`Content-Type: application/json`
- 单文件上限：20 MB
- 单表上限：10,000 行
- 提交限流：每个密钥每分钟 10 次

远程调用时由服务器管理员提供 HTTPS 或 VPN 地址。调用方不应获得
DeepSeek、Agnes、GPT Image 密钥，也不能在请求中指定模型、Prompt 或 Endpoint。

## 1. 提交采集表

```http
POST /api/v1/jobs
X-API-Key: <key>
Content-Type: application/json

{
  "商品id": ["10001"],
  "产品站点": ["US"],
  "产品标题": ["..."],
  "产品描述": ["..."],
  "产品图片链接": [["https://example.com/1.jpg"]],
  "变种图片链接": [[]]
}
```

首次接收返回 `201 Created`；完全相同的文件再次提交返回 `200 OK` 和同一个
任务 ID，不会重复处理。

```json
{
  "data": {
    "id": "<64位SHA256>",
    "status": "queued",
    "message": "任务已进入处理队列",
    "row_count": 1,
    "attempt": 0,
    "stage": "queued",
    "progress": {"completed": 0, "total": 0},
    "queue_position": 1,
    "isolated_count": 0,
    "isolated_product_ids": [],
    "blocker_reason": "",
    "links": {
      "self": "/api/v1/jobs/<id>",
      "result": "/api/v1/jobs/<id>/result",
      "review": "/api/v1/jobs/<id>/review"
    }
  }
}
```

## 2. 查询任务

```http
GET /api/v1/jobs/{id}
X-API-Key: <key>
```

状态含义：

- `queued`：已进入队列。
- `running`：正在处理。
- `retry_wait`：临时网络或 Provider 异常，系统会自动续跑。
- `blocked`：鉴权或额度异常，等待服务器恢复。
- `invalid_input`：输入结构或源数据不合格。
- `pending_review`：需要人工审核，没有覆盖旧正式表。
- `failed`：自动重试后仍未完成。
- `published`：正式回填表已生成。
- `published_with_warnings`：正式表已生成，部分商品被自动隔离。

`stage`、`progress` 和 `queue_position` 用于显示当前阶段、完成数/总数和队列
位置；`isolated_product_ids` 是本批未进入正式表的异常商品。图片或模型质量失败
不会写入回填表的 `有问题的产品id`。`blocker_reason` 只返回可安全展示的本机处理
原因，不包含 Provider 密钥或完整日志。

建议调用方每 15–30 秒查询一次，不要高频轮询。

## 3. 下载正式回填表

```http
GET /api/v1/jobs/{id}/result
X-API-Key: <key>
```

- 已发布：`200 OK`，响应体为回填表 JSON。
- 尚未完成：`202 Accepted`，并返回 `Retry-After`。
- 该任务没有正式结果：`409 Conflict`。

## 4. 下载终审包

```http
GET /api/v1/jobs/{id}/review
X-API-Key: <key>
```

成功时返回可离线查看的 HTML 文件。

## 5. 健康检查

```http
GET /api/v1/health
X-API-Key: <key>
```

API 和 Worker 都正常时返回 `200`；API 正常但 Worker 心跳异常时返回 `503`，
响应中仍会分别给出 `api` 和 `worker` 状态。

## 标准错误

```json
{
  "error": {
    "code": "invalid_contract",
    "message": "采集表字段不符合输入契约"
  }
}
```

常用状态码：`400` JSON 损坏、`401` 密钥错误、`413` 文件过大、`415`
Content-Type 错误、`422` 采集表契约错误、`429` 请求过多、`503` Worker 降级。

## PowerShell 示例

```powershell
$base = 'http://127.0.0.1:8765/api/v1'
$headers = @{ 'X-API-Key' = '<调用方密钥>' }
$job = Invoke-RestMethod -Method Post -Uri "$base/jobs" `
  -Headers $headers -ContentType 'application/json' `
  -InFile '.\跨境电商自动化采集表.json'
$id = $job.data.id
Invoke-RestMethod -Uri "$base/jobs/$id" -Headers $headers
Invoke-WebRequest -Uri "$base/jobs/$id/result" -Headers $headers `
  -OutFile '.\跨境电商自动化回填表.json'
```
