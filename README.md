# bupt-ucloud-homework

北邮「云邮教学空间」（`ucloud.bupt.edu.cn`）作业接口的 DSH 技能：不用浏览器、不碰瑞数反爬，
纯 HTTP 完成 CAS 登录、列课程 / 列作业、查提交记录、上传附件、提交作业、下载附件。

## 平台地址

| 用途 | 地址 |
|---|---|
| 站点 | https://ucloud.bupt.edu.cn |
| 业务接口 | https://apiucloud.bupt.edu.cn |
| CAS 统一认证 | https://auth.bupt.edu.cn/authserver |

## 目录

- `SKILL.md` — 技能说明、凭据优先级、human-in-the-loop 流程
- `scripts/ucloud.py` — 客户端实现（登录、课程、作业、上传、提交、下载）
- `reference/api.md` — 接口逆向记录（CAS 票据 → oauth/token → 业务接口）
- `.env.example` — 凭据模板

## 快速开始

```bash
python scripts/ucloud.py env set -u <学号> --password-stdin
python scripts/ucloud.py login
python scripts/ucloud.py courses
```

## 凭据

学号与统一认证密码写入本目录 `.env`（`chmod 600`），该文件已被 `.gitignore` 忽略。
登录态缓存在 `~/.ucloud_bupt_session.json`，同样不入库。

## 说明

- 示例中的学号、姓名与平台内部 ID 均已替换为占位符（`2024000000` / `张三`）。
- 本项目只封装本人账号有权访问的页面动作，请勿用于绕过权限或批量抓取他人数据。
