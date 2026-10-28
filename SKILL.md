---
name: bupt-ucloud-homework
description: 北邮「云邮教学空间」(ucloud.bupt.edu.cn) 作业接口操作：CAS 登录、列课程/列作业、查提交记录、上传附件、提交作业、下载附件。当需要查看或提交北邮教学云平台的课程作业，或需要绕过该平台的 CAS 反爬页面、直接用 HTTP 接口操作时使用。
whenToUse: 用户提到云邮教学空间 / ucloud.bupt.edu.cn / 北邮作业提交 / 教学云平台，或要求在 ucloud 上查作业、交作业、下载附件时。
---

# 云邮教学空间作业接口（ucloud.bupt.edu.cn）

平台的网页（CAS 统一认证 + Vue SPA）有瑞数反爬，浏览器自动化易白屏；但它本身是标准的
Blade 前后端分离应用，**所有页面动作都有对应的 HTTP 接口**。本 skill 直接用接口完成任务，
无浏览器、无 Playwright，一条命令完成登录/列表/上传/提交。

脚本位置（Windows）：`%USERPROFILE%\.dsh-beta\skills\bupt-ucloud-homework\scripts\ucloud.py`
（即 `~/.dsh-beta/skills/bupt-ucloud-homework/scripts/ucloud.py`）。

## 凭据：env 文件 + human-in-the-loop

凭据按以下顺序查找，**前面的优先**：

| 优先级 | 来源 | 说明 |
|---|---|---|
| 1 | 命令行参数 | `-u` / `-p` / `--password-file` / `--password-stdin` |
| 2 | 进程环境变量 | `UCLOUD_USER` / `UCLOUD_PASSWORD` |
| 3 | **技能目录 `.env`** | `%USERPROFILE%\.dsh-beta\skills\bupt-ucloud-homework\.env`（0600） |
| 4 | 缓存登录态 | `~/.ucloud_bupt_session.json`（access_token + refresh_token） |

.env 由 `env` 子命令管理，格式就是两行 `UCLOUD_USER=...` / `UCLOUD_PASSWORD=...`：

```bash
python scripts/ucloud.py env set -u 2024000000 --password-stdin   # 从 stdin 读密码
python scripts/ucloud.py env set -u 2024000000 -p '***'          # 或直接给（会进 shell 历史）
python scripts/ucloud.py env show      # 只显示掩码，不会回显密码
python scripts/ucloud.py env clear     # 删除 .env
python scripts/ucloud.py env path      # 打印 .env 路径
```

### 缺凭据时：退出码 3 + `NEED_CREDENTIALS`

任何需要登录的命令在**既无缓存登录态、又无可用账号密码**时，不会静默失败，而是以 **退出码 3**
结束，并在 stdout 打一行机器可读提示：

```
NEED_CREDENTIALS {"reason": "...", "env_file": "...", "has_user": false,
                  "has_password": false, "how": "请向用户索取「学号 + 统一认证密码」..."}
```

**agent 的标准处理流程（human-in-the-loop）**：

1. 先跑 `python scripts/ucloud.py auth status --json`：
   - 退出码 0 → 已登录，直接干活；
   - 退出码 1 → 有账号密码但没登录态，跑 `login` 即可；
   - 退出码 3 → 需要用户提供学号/密码。
2. 退出码 3 时**用 `ask_user_question` 向用户要「学号 + 统一认证密码」**，说明会写进本机
   `.env`（0600，仅本机使用）。不要自行猜测密码、不要翻找浏览器保存的密码代替用户授权。
3. 拿到后立刻 `env set`（密码走 stdin，避免出现在命令行/日志里），再 `login`。
4. 不要把密码写进回复、命令输出或任何提交物；任务结束如需清理，`env clear`。

## 快速开始

```bash
# 1) 首次：写入凭据并登录（之后有 refresh_token，通常无需再登录）
python scripts/ucloud.py env set -u 2024000000 --password-stdin
python scripts/ucloud.py login
python scripts/ucloud.py auth status          # 确认登录态与剩余有效期

# 2) 列课程 → 列出该课作业（含“已提交/截止时间/提交时间”）
python scripts/ucloud.py courses
python scripts/ucloud.py assignments -c 操作系统
python scripts/ucloud.py assignments --todo          # 全课程未提交的作业（一次请求拉到全部）
python scripts/ucloud.py assignments --keyword 实验   # 按标题过滤

# 3) 查某个作业的提交状态（内容 + 附件元数据）
python scripts/ucloud.py show 2109236609324797954

# 4) 提交（可多次 -f 传附件；先 --dry-run 看请求体，不会上传）
python scripts/ucloud.py submit 2109236609324797954 \
    -f 2024000000-张三-操作系统第一次作业.zip \
    -t "姓名：张三　学号：2024000000" --dry-run
python scripts/ucloud.py submit 2109236609324797954 -f ... -t "..."

# 5) 其它
python scripts/ucloud.py undone            # 未完成事项（跨课程）
python scripts/ucloud.py upload a.zip      # 只上传，打印 resourceId
python scripts/ucloud.py submit <id> --attach-id <resourceId>   # 复用已上传附件
python scripts/ucloud.py download <id> -o out.zip
python scripts/ucloud.py refresh           # 手动续期
```

参数化调用见 `scripts/ucloud.py` 的 `UCloudClient`：`list_courses()`、
`list_assignments(site_id)`、`get_assignment(id)`、`get_submission(id)`、`upload(path)`、
`submit(id, content, attachment_ids)`、`download(rid, dest)`。

## 附件到底传什么（易错点）

**不是传“附件地址/路径”，而是传文件本身。** 平台没有“给个 URL 就能挂上去”的接口：

1. `POST /blade-source/resource/upload/biz`，`multipart/form-data`，字段：
   - `file`：**必填**，文件二进制；
   - `bizType`：**必填**，作业提交固定 `3`；
   - `userId`：**可省**（实测只传 `file`+`bizType` 即可；传错也会被忽略，服务端以
     `Blade-Auth` token 里的用户为准，返回资源的 `author` 就是 token 用户）。
   - 只传 `file` 或 `file`+`userId` 会 400 `缺少必要的请求参数`。
2. 返回的 `data` 就是 `resourceId`（字符串）；
3. 把 `resourceId` 放进 `POST /ykt-site/work/submit` 的 `attachmentIds` 数组才完成挂载。

因此正确顺序是「**先上传拿 id，再提交带 id**」；已上传过的文件可用 `--attach-id` 复用，
不必重复上传。注意 `/blade-source/resource/upload/link` 虽然名字带 link，同样要求 multipart
`file`，并不是“传链接”。

## 提交接口的必填参数（POST /ykt-site/work/submit）

前端固定发送 7 个字段，服务端要求如下：

| 字段 | 必填性 | 取值 | 说明 |
|---|---|---|---|
| `assignmentId` | **必填** | 字符串 | 作业 id，来自 `assignments` 列表 |
| `assignmentType` | **必填** | `0` / `1` | 0 单人模式（绝大多数作业）、1 小组作业 |
| `assignmentContent` | **与附件二选一** | 文本，≤ 2000 字 | 会去掉尾部空白；不能和空 `attachmentIds` 同时为空，否则就是交空作业 |
| `attachmentIds` | **与正文二选一** | `resourceId` 字符串数组 | 必须先用 `upload/biz` 上传换 id（见上节） |
| `userId` | 前端必发，服务端以 token 为准 | 字符串 | 与上传一致：传错会被忽略，归属由 `Blade-Auth` 决定 |
| `groupId` | **小组作业必填** | 字符串 | 单人模式传 `""`；小组作业从 `submit-view`/选组接口取 |
| `commitId` | 可选 | 字符串 | 仅“重新提交”时带 `submit-view` 里的历史 `commitId`，首次提交留空 |

最小可用请求体（单人 + 一个附件）：

```json
{
  "attachmentIds": ["2111834866789269506"],
  "assignmentContent": "姓名：张三　学号：2024000000",
  "assignmentId": "2109236609324797954",
  "assignmentType": 0,
  "userId": "1820000000000000000",
  "groupId": "",
  "commitId": ""
}
```

客户端在发请求前会强制校验以上必填项（`assignmentId` 非空、正文与附件至少一个非空、
正文 ≤ 2000 字、`assignmentType ∈ {0,1}`、小组作业必须有 `groupId`），不满足直接报错；
`--dry-run` 同样会校验并打印请求体，便于提交前确认。

提交前必须满足的三个前提：

1. `auth status` 退出码为 0（已登录；否则先按上面 HITL 流程处理凭据）；
2. `assignmentId` 来自 `assignments` 列表且状态允许提交（`status=2` 待提交，或允许重交）；
3. 需要交附件时，附件已经通过 `upload/biz` 换成 `resourceId`（不能只给文件路径）。

## 关键约定（务必先读）

- **Base URL**：`https://apiucloud.bupt.edu.cn`（页面在 `https://ucloud.bupt.edu.cn`，两者不同域）。
- **鉴权头**：`Blade-Auth: <access_token>` + `Authorization: Basic c3dvcmQ6c3dvcmRfc2VjcmV0` + `Tenant-Id: 000000`。
- **票据登录用另一个客户端**：`grant_type=third` 的 `/ykt-basics/oauth/token` 必须用
  `Authorization: Basic cG9ydGFsOnBvcnRhbF9zZWNyZXQ=`（portal 客户端），用 sword 会返回
  `invalid_grant: ticket获取用户账号失败`。这是最容易踩的坑。
- **CAS 取票**：`POST https://auth.bupt.edu.cn/authserver/v1/tickets` 建 TGT，然后
  `GET /authserver/login?service=https://ucloud.bupt.edu.cn` 读 302 的 `ticket=`。
  不要用 `/v1/tickets/{TGT}` 换 ST——该实例会报 `not from a licensed host`。
- **列表都是 blade 分页**：入参 `current` / `size`，返回 `{records, total, size, current}`；
  作业列表 `POST /ykt-site/work/student/list` 的 body 里 `siteId` 可选——**不传就是全部课程的作业**。
- **作业状态**：`status` 2=待提交、3=已提交（`submitTime` 为空表示没交）。
- **提交体**：字段与必填性见上一节「提交接口的必填参数」；
  `assignmentType=0` 单人、`1` 小组，`groupId` 仅小组作业需要，`commitId` 仅重新提交需要。
- **票据是一次性的**：取到 ST 后立刻换 token；重复使用会失败。
- **有效期**：`access_token` 只有 **约 2 小时**（JWT `exp`），`refresh_token` 约 30 天。
  客户端收到 401 会自动刷新、重放请求**并把新 token 落盘**；两者都过期时，若 `.env` 里有
  账号密码会自动重新登录，所以正常使用无需手工 `login`。
- **效率要点**：`assignments` 不传 `-c/--site-id` 时，一次请求返回全部课程作业
  （实测 243 条），配合 `--todo`/`--keyword` 可在本地过滤，不必逐个课程翻。

## 推荐工作流（提交作业）

1. `assignments -c <课程名>` 找到 `assignmentId`，确认 `status=2`（待提交）且未过截止时间；
2. `show <assignmentId>` 看当前是否已有旧提交（避免误覆盖）；
3. 本地打包并核对文件名规范（如 `学号-姓名-课程第N次作业.zip`）；
4. `submit ... --dry-run` 检查请求体 → 去掉 `--dry-run` 正式提交；
5. 再跑一次 `show <assignmentId>` 验证：`status` 变 3、`submitTime` 有值、附件大小与本地一致。

## 安全

- `.env` 与 `~/.ucloud_bupt_session.json` 都是 0600；前者存明文密码、后者存 JWT，
  不要提交到版本库、不要贴进对话；
- 密码优先用 `--password-stdin` 或 `env set` 传入，避免出现在 shell 历史/进程列表；
- 不要在回复里回显密码或完整 token；任务结束如需清理：`env clear` + `auth logout`；
- 该接口是平台自身前端使用的公开接口，仅用于本人账号的正当操作。

## 不要做的事

- 不要为了登录去自动化 CAS 页面（瑞数反爬会让页面白屏）；
- 不要用浏览器 profile 里的 Cookie 复制方案（Chrome/Edge 主配置的 Cookies 文件被独占锁定，
  且新版是 App-Bound 加密，v20 无法离线解密）；
- 不要在未确认 `assignmentId` 归属时调用 `submit`（提交不可逆地覆盖旧记录）；
- 不要在缺凭据时自行猜测/爆破密码——按上面的 HITL 流程向用户索取。

更多接口细节（全部 endpoint、字段、返回示例）见 `reference/api.md`。
