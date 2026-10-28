# 云邮教学空间 HTTP 接口参考

> 全部结论来自对平台前端 bundle（`/uclass/static/js/*.js`）的静态分析 +
> 真实账号的联调验证（2026-10）。接口属于平台自身前端使用，非官方开放 API。

## 1. 基础信息

| 项目 | 值 |
|---|---|
| 网页 | `https://ucloud.bupt.edu.cn`（`/uclass/index.html`、`/uclass/course.html`） |
| 接口 | `https://apiucloud.bupt.edu.cn` |
| 认证 | `https://auth.bupt.edu.cn/authserver`（Apereo CAS） |
| 请求头 | `Blade-Auth: <token>`、`Tenant-Id: 000000`、`Authorization: Basic <client>` |
| 客户端凭据 | `sword:sword_secret` → `Basic c3dvcmQ6c3dvcmRfc2VjcmV0`（业务接口）<br>`portal:portal_secret` → `Basic cG9ydGFsOnBvcnRhbF9zZWNyZXQ=`（票据登录） |
| 列表分页 | blade 约定：入参 `current`/`size`，出参 `{records, total, size, current}` |
| 返回包装 | `{"code":200,"success":true,"data":...}`；`code != 200` 即失败 |

## 2. 登录

### 2.1 CAS 取票（两步）

```http
POST https://auth.bupt.edu.cn/authserver/v1/tickets
Content-Type: application/x-www-form-urlencoded

username=2024000000&password=***
→ 201, Location: http://auth.bupt.edu.cn/authserver/v1/tickets/TGT-9488-...
```

```http
GET https://auth.bupt.edu.cn/authserver/login?service=https://ucloud.bupt.edu.cn
Cookie: CASTGC=TGT-9488-...
→ 302, Location: https://ucloud.bupt.edu.cn?ticket=ST-75-v53x-...
```

注意：
- **不要**用 `POST /v1/tickets/{TGT}` 换 ST，该实例返回
  `400 "Request from ... is not from a licensed host."`；
- TGT 换来的 ST 一次性使用，取到后立即换 token。

### 2.2 票据换 token（portal 客户端）

```http
POST https://apiucloud.bupt.edu.cn/ykt-basics/oauth/token
Authorization: Basic cG9ydGFsOnBvcnRhbF9zZWNyZXQ=
Tenant-Id: 000000
Content-Type: application/x-www-form-urlencoded

grant_type=third&ticket=ST-75-v53x-...
```

返回：

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "refresh_token": "eyJ...",
  "user_id": "1820000000000000000",
  "account": "2024000000",
  "real_name": "张三",
  "avatar": "", "loginId": "...", "expires_in": 2591999
}
```

**坑**：同一请求若用 `sword` 客户端会返回
`400 {"error":"invalid_grant","error_description":"ticket获取用户账号失败"}`。

### 2.3 刷新

```http
POST /ykt-basics/oauth/token
Authorization: Basic cG9ydGFsOnBvcnRhbF9zZWNyZXQ=

grant_type=refresh_token&refresh_token=<refresh_token>
```

token 是 JWT：`access_token` 的 `exp` 约 **2 小时**，`refresh_token` 约 30 天
（登录响应里的 `expires_in` 2591999 秒对应 refresh_token）。
页面端/客户端在收到 401 时自动刷新一次并重放请求；刷新会返回新的 refresh_token，
必须落盘，否则轮换后旧 refresh_token 可能失效。

## 3. 列表类接口（最常用）

### 3.1 学生课程列表

```http
GET /ykt-site/site/list/student/current?size=999&current=1&userId=<userId>&siteRoleCode=2
```

```json
{"code":200,"data":{"records":[{
  "id":"2091774263396257793","siteName":"操作系统","termName":"2026秋季",
  "termId":"...","baseCourseId":"...","teachers":[{"name":"..."}],
  "courseCode":"...","departmentName":"..."
}],"total":11,"size":999,"current":1}}
```

`siteRoleCode=2` 表示学生视角；`history` 版本把 `current` 换成 `history` 即为历史课程。

### 3.2 作业列表（课程内 / 全部课程）

```http
POST /ykt-site/work/student/list
Content-Type: application/json

{"siteId":"2091774263396257793","current":1,"size":50}
```

- **不传 `siteId`**：返回该生所有课程的作业（可用于全局检索，实测 243 条）；
- 可选 `assignmentTitle` 做标题过滤；
- 返回记录关键字段：

```json
{"id":"2109236609324797954","assignmentTitle":"操作系统第一次编程作业",
 "assignmentStatus":0,"status":3,
 "assignmentEndTime":"2026-10-18 23:59","submitTime":"2026-10-18 23:00",
 "fullMark":10,"assignmentType":0}
```

状态语义（实测）：`status=2` 待提交、`status=3` 已提交；`submitTime` 为空即未交。
`assignmentStatus` 是业务状态位（0 正常）。

### 3.3 未完成事项（跨课程）

```http
GET /ykt-site/site/student/undone?userId=<userId>
```

```json
{"data":{"siteNum":11,"undoneNum":8,"undoneList":[
  {"siteId":-1,"activityName":"实验一：知识蒸馏","activityId":"2109553729338830849",
   "type":3,"endTime":"2026-12-27 23:59:59","assignmentType":-1}]}}
```

`type=3` 为作业类活动。

### 3.4 作业详情 / 我的提交

```http
GET /ykt-site/work/detail?assignmentId=2109236609324797954
GET /ykt-site/work/submit-view?assignmentId=2109236609324797954
```

`detail` 含 `assignmentTitle/assignmentContent(HTML)/assignmentEndTime/fullMark/
assignmentResource[]/status`。

`submit-view` 即"我的作业"：

```json
{"data":{"attachmentIds":["2111834866789269506"],
 "assignmentContent":"姓名：张三...",
 "assignmentId":"2109236609324797954","status":2,
 "score":-1,"evaluation":""}}
```

### 3.5 附件元数据

```http
GET /blade-source/resource/list/byId?resourceIds=2111834866789269506
```

```json
[{"id":"2111834866789269506","name":"xxx.zip","fileSize":1806567,"fileSizeUnit":"1.72MB",
  "ext":"zip","mimeType":"application/x-zip-compressed","bizType":3,
  "createTime":"2026-10-18 23:00:43","author":"1820000000000000000"}]
```

## 4. 附件上传 / 下载

### 4.1 上传

```http
POST /blade-source/resource/upload/biz
Content-Type: multipart/form-data

file=<binary>; bizType=3
```

```json
{"code":200,"success":true,"data":"2111834866789268418"}
```

字段实测结论（同一 token 下逐项尝试）：

| 字段 | 是否必填 | 说明 |
|---|---|---|
| `file` | **必填** | 文件二进制，multipart；**没有“只传路径/URL”的入口** |
| `bizType` | **必填** | 作业提交=3；只传 file 或 file+userId 会 400 `缺少必要的请求参数` |
| `userId` | 可选 | 可省略；传错也被忽略——服务端以 `Blade-Auth` 判定归属，返回资源 `author` 即 token 用户 |

- `data` 直接就是 `resourceId`（字符串），要放进 `submit` 的 `attachmentIds`；
- `bizType`：作业提交=3（实测元数据一致），公告=6，帖子=5，题库=8；
- 其他上传入口：`/blade-source/resource/upload`（网盘，字段 `file`+`groupId`/`folderId`）、
  `/blade-source/resource/upload/link`（名字带 link，实际同样是 multipart `file`）。

### 4.2 删除上传的资源

```http
POST /blade-source/resource/remove?ids=2111862271239868418
```

### 4.3 下载

```http
GET /blade-source/resource/download?resourceId=<id>        # 302 到对象存储
GET /blade-source/resource/download-url?resourceId=<id>    # 返回直链
GET /blade-source/resource/preview-url?resourceId=<id>     # 预览链接
```

## 5. 提交作业

```http
POST /ykt-site/work/submit
Content-Type: application/json

{
  "attachmentIds": ["2111834866789269506"],
  "assignmentContent": "姓名：张三　学号：2024000000\n...",
  "assignmentId": "2109236609324797954",
  "assignmentType": 0,
  "userId": "1820000000000000000",
  "groupId": "",
  "commitId": ""
}
```

| 字段 | 必填性 | 取值 | 说明 |
|---|---|---|---|
| `assignmentId` | **必填** | 字符串 | 作业 id |
| `assignmentType` | **必填** | `0`/`1` | 0 单人、1 小组 |
| `assignmentContent` | **与附件二选一** | ≤2000 字 | 前端会 `replace(/[\s\n\t]+$/g,"")` 去尾部空白 |
| `attachmentIds` | **与正文二选一** | `resourceId` 数组 | 来自 `upload/biz` 的返回值 |
| `userId` | 前端必发，服务端以 token 为准 | 字符串 | 传错被忽略（同上传接口） |
| `groupId` | 小组作业必填 | 字符串 | 单人传 `""` |
| `commitId` | 可选 | 字符串 | 仅重新提交时带 `submit-view` 中的历史值 |

- 正文与附件同时为空时接口**不会报错**，会形成“空提交”，因此客户端必须自己拦截；
- 成功返回 `{"code":200,...}`，前端提示"作业提交成功！"并把页签切回"作业信息"。

相关接口：
- `GET /ykt-site/work/check/assignment/size?assignmentId=` 附件大小限制；
- `POST /ykt-site/work/evaluation-list?<query>` 互评列表；
- `POST /ykt-site/work/evaluation-submit?<query>` 提交互评。

## 6. 其它有用接口

| 用途 | 方法与路径 |
|---|---|
| 课程详情 | `GET /ykt-site/site/detail?id=<siteId>` |
| 课程资源树 | `GET /ykt-site/site-resource/tree/perspective/student?siteId=` |
| 章节列表 | `GET /ykt-site/site-class/list?size=100&siteId=` |
| 优秀作业 | `GET /ykt-site/work/excellence` / `POST /ykt-site/work/all-assignment`（`excellence:1`） |
| 当前学期 | `GET /ykt-site/base-term/current` |
| 用户信息 | `GET /ykt-basics/info` |
| 退出 | `POST /ykt-basics/oauth/logout`（body `token`+`loginId`，portal 客户端） |

## 7. 失败与排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `invalid_grant` / `ticket获取用户账号失败` | OAuth 用了 sword 客户端，或 ST 已用过 | 用 portal 客户端，重新取票 |
| `not from a licensed host` | 用 `/v1/tickets/{TGT}` 换 ST | 改走 `/login?service=` 的 302 |
| 401 + `您长时间未操作系统` | access_token 过期（TTL 约 2h） | `grant_type=refresh_token` 刷新并重放 |
| 上传 400 `缺少必要的请求参数` | 只传了 `file` 或 `file`+`userId` | 补 `bizType`（作业提交=3） |
| 上传返回 code!=200 | 超出大小限制/类型限制 | 查 `work/check/assignment/size`，压缩或换格式 |
| 提交后 `submit-view` 无变化 | `assignmentId` 或 `userId` 不对 | 用 `assignments` 核对 id |

## 8. 前端实现线索（便于后续扩展）

- axios 实例：`baseURL=https://apiucloud.bupt.edu.cn`，请求拦截器加
  `Authorization`（默认 sword，`fromPortal` 时 portal）、`Tenant-Id: 000000`、
  `Blade-Auth=<cookie token>`、`identity=<cookie identity>`；
- 响应拦截器：401 时用 `refresh_token` 刷新并重放队列中的请求；
- 路由守卫：无 `token` 且 URL 带 `?ticket=` 时，取 `ticket`（截到最后一个 `#` 之前）
  调 `{ticket, grant_type:'third'}` 换 token，并把 `uuid/token/refresh_token/login-meth/
  expert-account/real_name/avatar/loginId/role_name/identity/user-role` 写进 cookie
  （统一带 `iClass-` 前缀）；否则跳 CAS 登录页。
- SPA cookie 名：`iClass-token`、`iClass-user-info`（URL 编码 JSON）、`iClass-refresh_token` 等。
