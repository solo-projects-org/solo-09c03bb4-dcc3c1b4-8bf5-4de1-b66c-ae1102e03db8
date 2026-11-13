# 水位日报邮件收录质控库

从已配置的 IMAP 邮箱中读取**未读**邮件，把唯一的日报 CSV 附件解析为水位读数入库，
并提供查询与原始附件下载 API。已读邮件代表已收妥：只有读数、原附件字节和"待标记"
记录**一并提交成功**后，才会向邮箱写 `\Seen`；读取或校验失败不入库、也不标已读。

## 一键启动

```bash
docker compose up --build -d
```

- Swagger UI: <http://localhost:8000/docs>
- Mock 邮箱管理页: <http://localhost:8081/>（`/admin/state` 为 JSON）
- Mock IMAP: `localhost:1143`，账号 `hydro` / `hydropass`
- 数据卷 `qc-data` 保存 SQLite；`docker compose down -v` 可清空重来

Compose 内含两个服务：

| 服务 | 作用 |
| --- | --- |
| `api` | FastAPI + 后台收录 worker，SQLite 持久化 |
| `mock-imap` | 本地 IMAP4rev1 源（内存态），带重建 / 标记失败开关与样例邮件 |

## 中文列名

CSV 必须 UTF-8（可带 BOM），4 列，列顺序固定或带可识别的表头：

```
日报号,站号,UTC秒,水位
HY2026110101,S001,1793491200,1010
HY2026110101,S001,1793494800,1011
```

| 列 | 约束 |
| --- | --- |
| 日报号 | 整份附件只能有一个值 |
| 站号 | 整份附件只能有一个值 |
| UTC秒 | 整数 Unix 秒，必须真实存在，且全部落在同一个 UTC 日 |
| 水位 | 整数毫米（不接受小数） |

另会校验：同秒不得重复、行数上限、文件非空、MIME 中必须**恰好一个** CSV 附件
（`.csv` / `text/csv`；其它附件如 PDF 会被忽略）。带表头时支持中英文列名别名且可乱序。

## 幂等、重复与拒收

- 日报号首次通过校验：读数 + 原附件 + `daily_reports` 登记 + `pending_marks` 在同一
  `BEGIN IMMEDIATE` 事务中提交 → 提交成功后才 `UID STORE +\Seen`。
- **日报号相同且附件字节一致**（SHA-256 相同）：记为 `duplicate`，不重复写读数，
  照常标记已读。Message-ID 可以重复，不作为幂等键。
- **日报号相同但内容不同**：拒收，`emails.outcome=rejected`，邮件保持未读。
- 解析/校验失败、无 CSV 或多 CSV：不入库、不标已读（失败原因记入 `scan_errors` 与
  `emails.error` 便于排查），邮件保持未读。
- 标记 `\Seen` 失败：读数已入库，记录留在 `pending_marks`，按轮询间隔重试；
  进程重启后从 SQLite 继续重试。

## UIDVALIDITY 与邮箱重建

每次连接都重新 `SELECT` 并读取服务端 `UIDVALIDITY`：

- 与库中记录不同 → 判定邮箱已重建：清空（置为 `stale`）旧 UIDVALIDITY 下的待标记记录，
  **绝不用旧 UID 给新邮件标已读**，并重新扫描未读邮件。
- 同一 UIDVALIDITY 内重连可安全重试未完成的标记。
- 拉取邮件时若发现 UIDVALIDITY 中途变化，丢弃该次取回的内容并整轮重来。
- 连接/登录失败只会记录错误并按间隔重连，不影响进程与已入库数据。

注意：这里按需求在重建后**重扫未读邮件**（而非仅按 UID 增量）。重建后 UID 可重复，
因此新的扫描记录以 `(mailbox, uid_validity, uid)` 为唯一键；旧 epoch 的记录保留供审计。

## API 速览

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/readings` | 按日报号/站号/UTC 日/邮件/标记状态查询读数（含 `mark_state`） |
| GET | `/readings/{id}` | 单条读数及其标记状态 |
| GET | `/reports/{report_no}` | 日报的读数汇总、附件与相关邮件 |
| GET | `/attachments` · `/attachments/{id}` | 原附件元数据 |
| GET | `/attachments/{id}/download` | 下载原附件字节（附 `X-Attachment-SHA256` 响应头） |
| GET | `/emails` · `/emails/{id}` | 每封邮件的处理结果与标记状态（含重试次数/最后错误） |
| GET | `/mailbox/state` | 当前 UIDVALIDITY 与待重试标记 |
| GET | `/health` · `/stats` | 健康检查与行数统计 |
| GET | `/pipeline/runs` | 最近的收录轮次结果（含 UIDVALIDITY 变化与错误） |

`mark_state`：`read`（已标已读）、`pending`（已提交待标记）、`failed`（标记失败待重试）、
`stale`（邮箱重建后作废）、`rejected`（内容拒收，保持未读）、`missing`（UID 已消失）。

## 演示流程（Swagger 或 curl）

1. `docker compose up --build -d`，等 `/health` 返回 `counts.readings = 5`。
2. 打开 <http://localhost:8000/docs>：
   - `GET /emails`：UID 1 `received/read`，UID 2 `duplicate/read`，UID 3 `rejected/rejected`，
     UID 4 `received/read`，UID 5/6/7 `rejected`，UID 8 为启动前已读、未被扫描。
   - `GET /readings?report_no=HY2026110101` 查看 3 条读数与标记状态。
   - `GET /attachments/{id}/download` 下载原 CSV。
3. **模拟标记失败**：`POST http://localhost:8081/admin/fail-seen` body `{"count": 2}`，
   再向邮箱追加/重建一封新邮件（下方脚本），
   `GET /mailbox/state` 可见 `pending_marks`，`GET /emails` 中该邮件 `mark_state=failed`；
   `POST /admin/clear-fail-seen` 后下一轮自动重试为 `read`。
4. **模拟邮箱重建 + UID 重用**：
   `POST http://localhost:8081/admin/rebuild` body `{"uid_validity": 9000, "reseed": true}`。
   重建后未读邮件 UID 从 1 重新分配；`GET /mailbox/state` 显示新 UIDVALIDITY，
   旧待标记记录变 `stale`，新邮件被重新扫描并正常标记，且不会误用旧 UID。

追加一封新邮件（不重建邮箱时 UID 继续增长）：

```bash
python - <<'PY'
import base64, json, urllib.request
from email.message import EmailMessage
csv = "日报号,站号,UTC秒,水位\nHY2026110105,S005,1793577600,777\n".encode()
m = EmailMessage()
m["From"] = "logger@station.example"; m["To"] = "intake@hydro.example"
m["Subject"] = "Daily water level HY2026110105 S005"
m["Message-ID"] = "<demo-105@station.example>"
m.set_content("daily report attached")
m.add_attachment(csv, maintype="text", subtype="csv", filename="daily.csv")
body = json.dumps({"raw_base64": base64.b64encode(m.as_bytes()).decode()}).encode()
req = urllib.request.Request("http://localhost:8081/admin/append", data=body,
                             headers={"Content-Type": "application/json"})
print(urllib.request.urlopen(req).read().decode())
PY
```

重建示例（清空 mock 内存邮箱、换新 UIDVALIDITY，并重新灌入样例；重建后样例邮件
从 UID 1 重新分配、均为未读，用来演示"重扫 + 不误用旧 UID"）：

```bash
curl -s -XPOST http://localhost:8081/admin/rebuild \
  -H 'Content-Type: application/json' -d '{"uid_validity": 9000, "reseed": true}'
```

> 注意：`POST /admin/rebuild` 后 mock 会用同一批样例邮件重建邮箱，`api` 的 SQLite
> 数据卷不会清空。已收录过的日报号会按"字节一致 → duplicate"处理，只有内容不同才会拒收。
> 想从零演示请执行 `docker compose down -v`。

## 配置

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `QC_DB_PATH` | `/data/qc.sqlite3` | SQLite 路径（Compose 用数据卷） |
| `IMAP_HOST` / `IMAP_PORT` | 空 / `143` | 邮箱地址；`IMAP_USE_SSL=true` 用 TLS |
| `IMAP_USER` / `IMAP_PASSWORD` | 空 | 登录凭据 |
| `IMAP_MAILBOX` | `INBOX` | 监听的邮箱 |
| `IMAP_SSL_VERIFY` | `true` | 自签证书时可关 |
| `IMAP_POLL_INTERVAL_SECONDS` | `15` | 轮询间隔（演示用 2 秒） |
| `IMAP_TIMEOUT_SECONDS` | `30` | 网络超时 |
| `QC_WORKER_ENABLED` | 配了 `IMAP_HOST` 即开启 | 设为 `false` 仅提供查询 API |
| `QC_MAX_ATTACHMENT_BYTES` | `5000000` | 附件大小上限 |
| `QC_MAX_READINGS_PER_REPORT` | `100000` | 单份读数行数上限 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

## 本地开发与测试

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q                      # 36 个用例：解析、收录、标记重试、重建、API
python -m mock_imap --seed-dir mock_imap/mails       # IMAP 1143 / admin 8081
QC_DB_PATH=/tmp/qc.sqlite3 IMAP_HOST=127.0.0.1 IMAP_PORT=1143 \
  IMAP_USER=hydro IMAP_PASSWORD=hydropass \
  uvicorn app.main:app --reload
```

样例邮件在 `mock_imap/mails/`，可用 `python scripts/generate_samples.py mock_imap/mails`
重新生成；文件名以 `read_` 开头的会在 mock 服务启动时带 `\Seen`。

## 实现要点与边界

- 读取一律用 `BODY.PEEK[]`，读取本身不会改变 `\Seen`；标记是显式的 `UID STORE`，
  并写后回查 `FLAGS` 确认，防止服务端静默失败。
- 附件按 SHA-256 去重存储，发布时间等元数据按邮件单独记录。
- `emails` 表是处理台账（仅元数据）；拒收邮件不写读数与附件字节。
- Mock IMAP 为演示/测试用途：内存态、明文、无 IDLE/STARTTLS，请勿用于生产。
- 单实例 worker：多副本会各自扫描同一邮箱，虽然幂等但会重复尝试标记；生产部署保持 1 个副本。
