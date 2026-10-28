#!/usr/bin/env python3
"""ucloud.py — 北邮「云邮教学空间」(ucloud.bupt.edu.cn) 作业接口客户端（无浏览器）。

设计目标：把作业提交从「Playwright 点界面」降级为「HTTP 直连」，一条命令完成
登录、列课程、列作业、查提交、上传附件、提交作业。

凭据解析顺序（前者优先）：
    1. 命令行参数        -u / -p / --password-file / --password-stdin
    2. 进程环境变量      UCLOUD_USER / UCLOUD_PASSWORD
    3. 技能目录下的 .env  UCLOUD_USER / UCLOUD_PASSWORD（0600）
    4. 缓存登录态        ~/.ucloud_bupt_session.json（token + refresh_token）

全部缺失时以退出码 3 结束，并在 stdout 输出一行
    NEED_CREDENTIALS {"reason": ..., "env_file": ...}
供调用方（agent）据此走 human-in-the-loop：向用户索要学号/密码后再 `env set`。

典型用法：
    python ucloud.py env set -u 2024000000 --password-stdin
    python ucloud.py auth status
    python ucloud.py courses
    python ucloud.py assignments --todo
    python ucloud.py show 2109236609324797954
    python ucloud.py submit 2109236609324797954 -f 作业.zip -t "姓名：张三"
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import re
import sys
import time
import urllib.parse

import requests

API_BASE = "https://apiucloud.bupt.edu.cn"
CAS_BASE = "https://auth.bupt.edu.cn/authserver"
SERVICE = "https://ucloud.bupt.edu.cn"

# 平台前端写死的 OAuth 客户端凭据（公开存在于 app.js 中）
# sword:sword_secret  —— 业务接口默认客户端
# portal:portal_secret —— 门户/票据登录（grant_type=third）专用
BASIC_AUTH = "Basic c3dvcmQ6c3dvcmRfc2VjcmV0"
BASIC_AUTH_PORTAL = "Basic cG9ydGFsOnBvcnRhbF9zZWNyZXQ="
TENANT_ID = "000000"

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = os.environ.get("UCLOUD_ENV_FILE") or os.path.join(SKILL_DIR, ".env")
DEFAULT_STATE = (os.environ.get("UCLOUD_STATE")
                 or os.path.join(os.path.expanduser("~"), ".ucloud_bupt_session.json"))

# 作业状态：2=未提交/进行中，3=已提交
STATUS_TEXT = {0: "未开始", 1: "进行中", 2: "待提交", 3: "已提交"}

EXIT_NEED_CREDENTIALS = 3


class UCloudError(RuntimeError):
    """接口返回非 200 或业务失败。"""


class AuthExpired(UCloudError):
    """401：token 过期，需要刷新后重试。"""


class CredentialsRequired(UCloudError):
    """没有任何可用凭据，需要向用户索取（human-in-the-loop）。"""


# ------------------------------------------------------------------ .env
def read_env(path: str = ENV_FILE) -> dict:
    """读取简单的 KEY=VALUE 文件（支持 # 注释、export 前缀、引号）。"""
    data: dict[str, str] = {}
    if not os.path.exists(path):
        return data
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            key, val = key.strip(), val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            data[key] = val
    return data


def write_env(values: dict, path: str = ENV_FILE) -> None:
    """原子写入 .env（权限 0600）。只保留 UCLOUD_* 键，避免误存其它变量。"""
    lines = ["# 云邮教学空间凭据（由 ucloud.py env set 生成，请勿提交到版本库）"]
    for key in ("UCLOUD_USER", "UCLOUD_PASSWORD"):
        if values.get(key):
            lines.append("%s=%s" % (key, values[key]))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def credential_sources(args=None) -> dict:
    """按优先级合并凭据，返回 {user, password, source}。"""
    env_file = read_env()
    out = {"user": None, "password": None, "source": None}

    for key, attr in (("UCLOUD_USER", "user"), ("UCLOUD_PASSWORD", "password")):
        if os.environ.get(key):
            out[attr] = os.environ[key]
            out["source"] = out["source"] or "process-env"
        elif env_file.get(key):
            out[attr] = env_file[key]
            out["source"] = out["source"] or "dotenv"

    if args is not None:
        if getattr(args, "user", None):
            out["user"] = args.user
            out["source"] = "args"
        pw = getattr(args, "password", None)
        if getattr(args, "password_file", None):
            with open(args.password_file, encoding="utf-8") as f:
                lines = [ln.rstrip("\r\n") for ln in f if ln.strip()]
            if lines:
                pw = lines[-1]
        if getattr(args, "password_stdin", False):
            pw = sys.stdin.readline().rstrip("\r\n")
        if pw:
            out["password"] = pw
            out["source"] = "args"
    return out


class UCloudClient:
    """云邮教学空间 API 客户端。"""

    def __init__(self, token: str | None = None, refresh_token: str | None = None,
                 user: dict | None = None, timeout: int = 60,
                 state_path: str | None = None):
        self.token = token
        self.refresh_token = refresh_token
        self.user = user or {}
        self.timeout = timeout
        self.state_path = state_path
        self.http = requests.Session()
        self.http.trust_env = True

    # ------------------------------------------------------------------ 鉴权
    @staticmethod
    def cas_ticket(username: str, password: str) -> str:
        """通过 CAS REST 创建 TGT，再用登录重定向换 Service Ticket。

        注意：不能用 /v1/tickets/{TGT} 直接取 ST（该实例会以
        "not from a licensed host" 拒绝），必须走 /login?service= 的 302。
        """
        s = requests.Session()
        r = s.post(CAS_BASE + "/v1/tickets",
                   data={"username": username, "password": password},
                   timeout=30, allow_redirects=False)
        if r.status_code != 201:
            raise UCloudError("CAS 认证失败（用户名或密码错误）：HTTP %s %s"
                              % (r.status_code, r.text[:200]))
        tgt = r.headers["Location"].rstrip("/").rsplit("/", 1)[-1]
        s.cookies.set("CASTGC", tgt, domain="auth.bupt.edu.cn", path="/authserver/")
        r = s.get(CAS_BASE + "/login", params={"service": SERVICE},
                  timeout=30, allow_redirects=False)
        loc = r.headers.get("Location", "")
        ticket = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query).get("ticket", [None])[0]
        if not ticket:
            raise UCloudError("未能取得 CAS Service Ticket：%s" % (loc or r.status_code))
        return ticket

    def _oauth(self, payload: dict) -> dict:
        # 票据登录（grant_type=third）必须用 portal 客户端，否则
        # 返回 invalid_grant: ticket获取用户账号失败。
        r = self.http.post(API_BASE + "/ykt-basics/oauth/token", data=payload,
                           headers={"Authorization": BASIC_AUTH_PORTAL,
                                    "Tenant-Id": TENANT_ID,
                                    "Referer": "https://ucloud.bupt.edu.cn/",
                                    "Origin": "https://ucloud.bupt.edu.cn"},
                           timeout=30)
        if r.status_code != 200:
            raise UCloudError("OAuth 失败：HTTP %s %s" % (r.status_code, r.text[:300]))
        data = r.json()
        if "access_token" not in data:
            raise UCloudError("OAuth 返回异常：%s" % str(data)[:300])
        self.token = data["access_token"]
        self.refresh_token = data.get("refresh_token")
        self.user = {
            "id": str(data.get("user_id", "")),
            "account": data.get("account", ""),
            "name": data.get("real_name", ""),
        }
        return data

    @classmethod
    def login(cls, username: str, password: str,
              state_path: str | None = None) -> "UCloudClient":
        """账号密码登录：CAS -> ticket -> OAuth token。"""
        client = cls(state_path=state_path)
        ticket = cls.cas_ticket(username, password)
        client._oauth({"grant_type": "third", "ticket": ticket})
        return client

    @classmethod
    def from_ticket(cls, ticket: str, state_path: str | None = None) -> "UCloudClient":
        client = cls(state_path=state_path)
        client._oauth({"grant_type": "third", "ticket": ticket})
        return client

    def refresh_session(self) -> "UCloudClient":
        if not self.refresh_token:
            raise UCloudError("没有 refresh_token，请重新登录")
        self._oauth({"grant_type": "refresh_token", "refresh_token": self.refresh_token})
        if self.state_path:
            self.save(self.state_path)   # 刷新后的 token 必须落盘，防止 refresh 轮换后失配
        return self

    # -------------------------------------------------------------- 持久化
    def save(self, path: str | None = None) -> None:
        path = path or self.state_path or DEFAULT_STATE
        self.state_path = path
        data = {"token": self.token, "refresh_token": self.refresh_token,
                "user": self.user, "saved_at": int(time.time())}
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)

    @classmethod
    def load(cls, path: str = DEFAULT_STATE) -> "UCloudClient":
        if not os.path.exists(path):
            raise CredentialsRequired("没有缓存的登录态")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return cls(token=data.get("token"), refresh_token=data.get("refresh_token"),
                   user=data.get("user") or {}, state_path=path)

    @staticmethod
    def token_exp(token: str | None) -> int | None:
        """解析 JWT 的 exp（unix 秒），失败返回 None。"""
        if not token or token.count(".") < 2:
            return None
        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            return int(json.loads(base64.urlsafe_b64decode(payload)).get("exp"))
        except Exception:
            return None

    # -------------------------------------------------------------- HTTP 层
    def _headers(self, extra: dict | None = None) -> dict:
        h = {
            "Authorization": BASIC_AUTH,
            "Tenant-Id": TENANT_ID,
            "Blade-Auth": self.token or "",
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://ucloud.bupt.edu.cn",
            "Referer": "https://ucloud.bupt.edu.cn/",
        }
        if extra:
            h.update(extra)
        return h

    def get(self, path: str, **params) -> dict:
        return self._with_retry(
            lambda: self.http.get(API_BASE + path, params=params or None,
                                  headers=self._headers(), timeout=self.timeout),
            path)

    def post(self, path: str, json_body=None, data=None, files=None) -> dict:
        headers = self._headers()
        if json_body is not None:
            headers["Content-Type"] = "application/json;charset=UTF-8"
        return self._with_retry(
            lambda: self.http.post(API_BASE + path, json=json_body, data=data,
                                   files=files, headers=headers,
                                   timeout=self.timeout),
            path)

    def _with_retry(self, call, path: str) -> dict:
        """token 过期（401）时刷新一次并重放请求。"""
        try:
            return self._unwrap(call(), path)
        except AuthExpired:
            if not self.refresh_token:
                raise
            self.refresh_session()
            return self._unwrap(call(), path)

    @staticmethod
    def _unwrap(r: requests.Response, path: str) -> dict:
        try:
            body = r.json()
        except ValueError:
            raise UCloudError("%s 返回非 JSON：HTTP %s %s"
                              % (path, r.status_code, r.text[:200]))
        if r.status_code == 401:
            raise AuthExpired("%s 鉴权失效（401）" % path)
        if r.status_code != 200 or body.get("code") not in (200, None):
            raise UCloudError("%s 调用失败：HTTP %s %s"
                              % (path, r.status_code, str(body)[:300]))
        return body

    # ---------------------------------------------------------------- 列表
    def list_courses(self, size: int = 999) -> list[dict]:
        """学生当前学期课程列表（blade 分页，records 为课程）。"""
        body = self.get("/ykt-site/site/list/student/current",
                        size=size, current=1, userId=self.user.get("id", ""),
                        siteRoleCode=2)
        return (body.get("data") or {}).get("records", [])

    def list_assignments(self, site_id: str | None = None, current: int = 1,
                         size: int = 50, keyword: str | None = None) -> dict:
        """作业列表。不带 site_id 时为全部课程的作业（跨课程）。"""
        payload: dict = {"current": current, "size": size}
        if site_id:
            payload["siteId"] = site_id
        if keyword:
            payload["assignmentTitle"] = keyword
        body = self.post("/ykt-site/work/student/list", json_body=payload)
        return body.get("data") or {}

    def list_undone(self) -> dict:
        """未完成活动（含作业）。"""
        body = self.get("/ykt-site/site/student/undone", userId=self.user.get("id", ""))
        return body.get("data") or {}

    def get_assignment(self, assignment_id: str) -> dict:
        body = self.get("/ykt-site/work/detail", assignmentId=assignment_id)
        return body.get("data") or {}

    def get_submission(self, assignment_id: str) -> dict:
        """我的提交记录（attachmentIds / assignmentContent / status）。"""
        body = self.get("/ykt-site/work/submit-view", assignmentId=assignment_id)
        return body.get("data") or {}

    def list_attachments(self, resource_ids: list[str]) -> list[dict]:
        body = self.get("/blade-source/resource/list/byId",
                        resourceIds=",".join(resource_ids))
        return body.get("data") or []

    def whoami(self) -> dict:
        body = self.get("/ykt-basics/info")
        return body.get("data") or {}

    # ---------------------------------------------------------------- 文件
    def upload(self, path: str, biz_type: int = 3) -> str:
        """上传附件，返回 resourceId。

        服务端要求 multipart 三个字段：file（二进制）、userId、bizType；
        只给 file（或只给文件路径/URL）不会被接受——附件必须先上传换 resourceId，
        再把 resourceId 放进 submit 的 attachmentIds。
        """
        name = os.path.basename(path)
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            files = {"file": (name, f, "application/octet-stream")}
            data = {"userId": self.user.get("id", ""), "bizType": str(biz_type)}
            body = self.post("/blade-source/resource/upload/biz",
                             data=data, files=files)
        rid = body.get("data")
        if not rid:
            raise UCloudError("上传失败：%s" % str(body)[:300])
        return str(rid)

    def delete_resource(self, resource_ids: list[str]) -> dict:
        ids = ",".join(resource_ids)
        return self.post("/blade-source/resource/remove?ids=" + ids)

    def download(self, resource_id: str, dest: str) -> str:
        """下载附件到本地（走 download 接口的 302 到对象存储）。"""
        r = self.http.get(API_BASE + "/blade-source/resource/download",
                          params={"resourceId": resource_id},
                          headers=self._headers(), timeout=self.timeout,
                          allow_redirects=True, stream=True)
        if r.status_code != 200:
            raise UCloudError("下载失败：HTTP %s" % r.status_code)
        with open(dest, "wb") as f:
            for chunk in r.iter_content(65536):
                f.write(chunk)
        return dest

    # ---------------------------------------------------------------- 提交
    @staticmethod
    def validate_submission(assignment_id: str, content: str,
                            attachment_ids: list[str] | None,
                            assignment_type: int, group_id: str) -> None:
        """提交前的必填校验，避免把空内容/空附件交上去。"""
        if not str(assignment_id or "").strip():
            raise UCloudError("assignmentId 必填")
        if not (content or "").strip() and not attachment_ids:
            raise UCloudError("assignmentContent 与 attachmentIds 至少要有一个非空，"
                              "否则会提交一条空作业")
        if len(content or "") > 2000:
            raise UCloudError("assignmentContent 超过 2000 字上限（当前 %d 字）"
                              % len(content))
        if assignment_type not in (0, 1):
            raise UCloudError("assignmentType 只能是 0（单人）或 1（小组）")
        if assignment_type == 1 and not str(group_id or "").strip():
            raise UCloudError("小组作业（assignmentType=1）必须提供 groupId")

    def submit(self, assignment_id: str, content: str = "",
               attachment_ids: list[str] | None = None,
               assignment_type: int = 0, group_id: str = "",
               commit_id: str | None = None) -> dict:
        """提交作业。

        必填：assignmentId、assignmentType，以及 assignmentContent / attachmentIds
        至少一个非空；userId 由 token 决定（前端也会带）；groupId 仅小组作业需要；
        commitId 仅“重新提交”时带历史值。
        """
        self.validate_submission(assignment_id, content, attachment_ids,
                                 assignment_type, group_id)
        payload = {
            "attachmentIds": attachment_ids or [],
            "assignmentContent": re.sub(r"[\s\n\t]+$", "", content or ""),
            "assignmentId": str(assignment_id),
            "assignmentType": assignment_type,
            "userId": self.user.get("id", ""),
            "groupId": group_id if assignment_type == 1 else "",
            "commitId": commit_id or "",
        }
        body = self.post("/ykt-site/work/submit", json_body=payload)
        return body.get("data") or body

    # ---------------------------------------------------------------- 工具
    def find_course(self, keyword: str) -> dict:
        for c in self.list_courses():
            if keyword in (c.get("siteName") or ""):
                return c
        raise UCloudError("未找到包含 %r 的课程" % keyword)


# ------------------------------------------------------------------ CLI 辅助
def ensure_client(args) -> UCloudClient:
    """取一个可用客户端：缓存 token 优先，其次用 env/参数里的账号密码登录。"""
    state = getattr(args, "state", None) or DEFAULT_STATE
    if os.path.exists(state):
        return UCloudClient.load(state)

    creds = credential_sources(args)
    if creds["user"] and creds["password"]:
        c = UCloudClient.login(creds["user"], creds["password"], state_path=state)
        c.save(state)
        return c
    raise CredentialsRequired("缺少凭据：既没有缓存登录态，也没有可用的账号密码")


def need_credentials_payload(reason: str) -> dict:
    creds = credential_sources()
    return {
        "reason": reason,
        "env_file": ENV_FILE,
        "has_user": bool(creds["user"]),
        "has_password": bool(creds["password"]),
        "state_file": DEFAULT_STATE,
        "how": ("请向用户索取「学号 + 统一认证密码」，然后执行："
                "python ucloud.py env set -u <学号> --password-stdin"),
    }


def cmd_env(args) -> int:
    action = args.action
    if action == "path":
        print(ENV_FILE)
        return 0

    if action == "show":
        data = read_env()
        user = data.get("UCLOUD_USER") or os.environ.get("UCLOUD_USER") or ""
        pw = data.get("UCLOUD_PASSWORD") or os.environ.get("UCLOUD_PASSWORD") or ""
        print("env 文件：%s（%s）" % (ENV_FILE, "存在" if os.path.exists(ENV_FILE) else "不存在"))
        print("UCLOUD_USER：%s" % (user or "(未设置)"))
        print("UCLOUD_PASSWORD：%s" % ("*" * min(len(pw), 8) + "（已设置）" if pw else "(未设置)"))
        return 0

    if action == "clear":
        if os.path.exists(ENV_FILE):
            os.remove(ENV_FILE)
            print("已删除 %s" % ENV_FILE)
        else:
            print("env 文件不存在，无需删除")
        return 0

    if action == "set":
        creds = credential_sources(args)
        if not creds["user"]:
            raise CredentialsRequired("缺少 UCLOUD_USER")
        if not creds["password"]:
            raise CredentialsRequired("缺少 UCLOUD_PASSWORD")
        write_env({"UCLOUD_USER": creds["user"], "UCLOUD_PASSWORD": creds["password"]})
        print("已写入 %s（0600，含 UCLOUD_USER / UCLOUD_PASSWORD）" % ENV_FILE)
        return 0

    raise UCloudError("未知 env 动作：%s" % action)


def cmd_auth(args) -> int:
    if args.action == "logout":
        for path in {DEFAULT_STATE, args.state}:
            if path and os.path.exists(path):
                os.remove(path)
                print("已删除登录态 %s" % path)
        return 0

    # status
    info = {
        "env_file": ENV_FILE,
        "env_exists": os.path.exists(ENV_FILE),
        "state_file": DEFAULT_STATE,
        "state_exists": os.path.exists(DEFAULT_STATE),
    }
    creds = credential_sources()
    info["has_user"] = bool(creds["user"])
    info["has_password"] = bool(creds["password"])
    info["credential_source"] = creds["source"]

    ok = False
    if info["state_exists"]:
        try:
            c = UCloudClient.load(DEFAULT_STATE)
            c.list_courses(size=1)     # 触发 401 自动刷新，验证可用性
            ok = True
            exp = UCloudClient.token_exp(c.token)   # 刷新之后再读取有效期
            info["token_exp"] = exp
            info["token_remain_days"] = round((exp - time.time()) / 86400, 1) if exp else None
        except Exception as e:                      # noqa: BLE001
            info["error"] = str(e)[:200]

    info["authenticated"] = ok
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=1))
    else:
        print("env 文件：%s（%s）" % (ENV_FILE, "存在" if info["env_exists"] else "不存在"))
        print("凭据来源：%s" % (creds["source"] or "无"))
        print("登录态：%s" % ("存在" if info["state_exists"] else "不存在"))
        rem = info.get("token_remain_days")
        if rem is not None:
            if rem < 1:
                print("token 剩余：约 %.0f 分钟" % (rem * 24 * 60))
            else:
                print("token 剩余：%.1f 天" % rem)
        print("接口可用：%s" % ("是" if ok else "否"))

    if ok:
        return 0
    if info["has_user"] and info["has_password"]:
        return 1        # 有凭据但尚未登录：调用 login 即可
    return EXIT_NEED_CREDENTIALS


def main(argv=None):
    ap = argparse.ArgumentParser(description="云邮教学空间作业接口客户端")
    ap.add_argument("--state", default=DEFAULT_STATE, help="登录态缓存文件")
    sub = ap.add_subparsers(dest="cmd", required=True)

    # ---- env
    p = sub.add_parser("env", help="管理凭据文件 .env")
    p.add_argument("action", choices=["set", "show", "clear", "path"])
    p.add_argument("-u", "--user")
    p.add_argument("-p", "--password")
    p.add_argument("--password-stdin", action="store_true")
    p.add_argument("--password-file")

    # ---- auth
    p = sub.add_parser("auth", help="登录态检查/登出")
    p.add_argument("action", choices=["status", "logout"])
    p.add_argument("--json", action="store_true")

    # ---- login
    p = sub.add_parser("login", help="账号密码登录并缓存登录态")
    p.add_argument("-u", "--user")
    p.add_argument("-p", "--password")
    p.add_argument("--password-stdin", action="store_true")
    p.add_argument("--password-file", help="从文件读取密码（取最后一行非空内容）")

    p = sub.add_parser("ticket", help="用已有 CAS ticket 登录")
    p.add_argument("ticket")

    sub.add_parser("refresh", help="刷新 token")

    p = sub.add_parser("courses", help="列出课程")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("assignments", help="列出作业")
    p.add_argument("-c", "--course", help="课程名关键字")
    p.add_argument("--site-id")
    p.add_argument("--keyword", help="作业标题关键字")
    p.add_argument("--todo", action="store_true", help="只看未提交的")
    p.add_argument("--current", type=int, default=1)
    p.add_argument("--size", type=int, default=50)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("show", help="查看某个作业及其提交")
    p.add_argument("assignment_id")

    sub.add_parser("undone", help="列出未完成事项")

    p = sub.add_parser("submit", help="提交作业")
    p.add_argument("assignment_id")
    p.add_argument("-f", "--file", action="append", default=[], help="附件，可多次")
    p.add_argument("--attach-id", action="append", default=[],
                   help="复用已上传的 resourceId（不再重复上传）")
    p.add_argument("-t", "--content", default="", help="作业内容文本")
    p.add_argument("--content-file", help="从文件读取作业内容")
    p.add_argument("--assignment-type", type=int, default=0)
    p.add_argument("--group-id", default="")
    p.add_argument("--commit-id", default="")
    p.add_argument("--dry-run", action="store_true", help="只打印请求体，不真正提交")

    p = sub.add_parser("download", help="下载提交的附件")
    p.add_argument("assignment_id")
    p.add_argument("-o", "--out", required=True)

    p = sub.add_parser("upload", help="上传附件，返回 resourceId")
    p.add_argument("file")
    p.add_argument("--biz-type", type=int, default=3,
                   help="业务类型，作业提交为 3")

    p = sub.add_parser("rm", help="删除（回收）资源")
    p.add_argument("resource_ids", nargs="+")

    args = ap.parse_args(argv)

    try:
        if args.cmd == "env":
            return cmd_env(args)

        if args.cmd == "auth":
            return cmd_auth(args)

        if args.cmd == "login":
            creds = credential_sources(args)
            if not creds["user"]:
                raise CredentialsRequired("缺少 UCLOUD_USER")
            pw = creds["password"]
            if not pw:
                if sys.stdin.isatty():
                    pw = getpass.getpass("CAS 密码：")
                else:
                    raise CredentialsRequired("缺少 UCLOUD_PASSWORD")
            c = UCloudClient.login(creds["user"], pw, state_path=args.state)
            c.save(args.state)
            print("登录成功：%s（%s）" % (c.user.get("name"), c.user.get("account")))
            return 0

        if args.cmd == "ticket":
            c = UCloudClient.from_ticket(args.ticket, state_path=args.state)
            c.save(args.state)
            print("登录成功：%s" % c.user.get("name"))
            return 0

        c = ensure_client(args)

        if args.cmd == "refresh":
            c.refresh_session()
            c.save(args.state)
            mins = (UCloudClient.token_exp(c.token) - time.time()) / 60
            print("已刷新 token（剩余约 %.0f 分钟）" % mins)
            return 0

        if args.cmd == "courses":
            courses = c.list_courses()
            if args.json:
                print(json.dumps(courses, ensure_ascii=False, indent=1))
            else:
                print("%-20s %-8s %s" % ("siteId", "学期", "课程"))
                for x in courses:
                    print("%-20s %-8s %s" % (x.get("id"), x.get("termName", ""),
                                             x.get("siteName", "")))
            return 0

        if args.cmd == "assignments":
            site_id = args.site_id
            if not site_id and args.course:
                site_id = c.find_course(args.course).get("id")
            data = c.list_assignments(site_id, args.current, args.size, args.keyword)
            rows = data.get("records", [])
            if args.todo:
                rows = [x for x in rows if x.get("status") != 3]
            if args.json:
                print(json.dumps({"total": data.get("total"), "records": rows},
                                 ensure_ascii=False, indent=1))
                return 0
            print("共 %s 条（显示 %d）" % (data.get("total"), len(rows)))
            print("%-20s %-8s %-16s %-12s %s" %
                  ("assignmentId", "状态", "截止时间", "提交时间", "标题"))
            for x in rows:
                print("%-20s %-8s %-16s %-12s %s" %
                      (x.get("id"),
                       STATUS_TEXT.get(x.get("status"), str(x.get("status"))),
                       x.get("assignmentEndTime", ""),
                       x.get("submitTime") or "-",
                       x.get("assignmentTitle", "")))
            return 0

        if args.cmd == "show":
            a = c.get_assignment(args.assignment_id)
            s = c.get_submission(args.assignment_id)
            print("标题：%s" % a.get("assignmentTitle"))
            print("截止：%s  总分：%s" % (a.get("assignmentEndTime"), a.get("fullMark")))
            print("状态：%s" % STATUS_TEXT.get(a.get("status"), a.get("status")))
            print("提交内容：%s" % (s.get("assignmentContent") or "(空)"))
            ids = s.get("attachmentIds") or []
            print("附件：%d 个" % len(ids))
            if ids:
                for meta in c.list_attachments(ids):
                    print("  - %s  %s  %s" % (meta.get("name"), meta.get("fileSizeUnit"),
                                              meta.get("createTime")))
            return 0

        if args.cmd == "undone":
            d = c.list_undone()
            print("课程数 %s，未完成 %s 项" % (d.get("siteNum"), d.get("undoneNum")))
            for x in d.get("undoneList", []):
                print("  %-12s %s  (截止 %s)" % (x.get("type"), x.get("activityName"),
                                                 x.get("endTime")))
            return 0

        if args.cmd == "submit":
            content = args.content
            if args.content_file:
                with open(args.content_file, encoding="utf-8") as f:
                    content = f.read()

            ids = list(args.attach_id)
            planned = ["<upload:%s>" % os.path.basename(p) for p in args.file]
            # 提交前统一校验（--dry-run 也校验，避免演练时看不出必填项缺失）
            UCloudClient.validate_submission(args.assignment_id, content,
                                             ids + planned, args.assignment_type,
                                             args.group_id)

            if args.dry_run:
                print("[dry-run] 不会上传/提交，请求体预览：")
                print(json.dumps({
                    "attachmentIds": ids + planned,
                    "assignmentContent": re.sub(r"[\s\n\t]+$", "", content),
                    "assignmentId": args.assignment_id,
                    "assignmentType": args.assignment_type,
                    "userId": c.user.get("id"), "groupId": args.group_id,
                    "commitId": args.commit_id}, ensure_ascii=False, indent=1))
                return 0

            for path in args.file:
                rid = c.upload(path)
                ids.append(rid)
                print("已上传 %s -> resourceId=%s" % (os.path.basename(path), rid))
            c.submit(args.assignment_id, content, ids, args.assignment_type,
                     args.group_id, args.commit_id or None)
            print("提交成功")
            return 0

        if args.cmd == "download":
            s = c.get_submission(args.assignment_id)
            ids = s.get("attachmentIds") or []
            if not ids:
                print("没有附件")
                return 1
            c.download(ids[0], args.out)
            print("已下载到", args.out)
            return 0

        if args.cmd == "upload":
            rid = c.upload(args.file, args.biz_type)
            print(rid)
            return 0

        if args.cmd == "rm":
            c.delete_resource(list(args.resource_ids))
            print("已删除：%s" % ", ".join(args.resource_ids))
            return 0

    except CredentialsRequired as e:
        print("NEED_CREDENTIALS " + json.dumps(
            need_credentials_payload(str(e)), ensure_ascii=False))
        return EXIT_NEED_CREDENTIALS
    except UCloudError as e:
        print("错误：%s" % e, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
