# jdww_gdmec_kcb

广东机电职业技术学院 · 课表代理。

原版教务系统只给一个又丑又难用的总课表，查某个学期、某一周的课要来回翻。
这个项目把教务系统的 SSO 登录和数据接口重做了一遍，前面套一层自己的 Web 界面：按学年 / 学期 / 周查看，点开课程看详情。

## 功能

- 用**学校统一身份认证**（`auth.gdmec.edu.cn`）的账号密码登录，不需要教务独立密码
- 学年 × 学期切换，自动定位到当前学期
- 两种视图：**周视图**（七列网格，一屏看完一周）和**日视图**（左侧时间轴 + 日程，手机默认用这个）
- 周次条直接点选，`←` `→` 换周 / 换天，`T` 回本周
- 每个星期几都带真实日期；今天那一列高亮，正在上的课带标记，顶部还会提示「正在进行 / 下一节」
- 课程按课名自动分色（七色调色盘，同一门课整学期同色）
- 课程详情：教师、地点、校区、上课时间、周次、课程号、教学班、备注
- 开学日期可自定义（存在浏览器本地，用于计算"当前第几周"和每天日期）
- 表尾连着的空节次会自动收起，不占一屏空白
- 深浅色可跟随系统，也可以在设置里固定浅色 / 深色
- 会话落盘，重启服务后旧 token 依然可用；**不保存密码**

## 作息时间

课表上的上下课时间由 `backend/timetable.py` 顶部的常量按规则算出来，不是写死的时间表：

```python
PERIOD_MIN = 45             # 一节课 45 分钟
BREAK_BETWEEN_PERIODS = 10  # 同一大节内两小节之间
BREAK_BETWEEN_PAIRS = 25    # 每上完两节
BLOCKS = [(1, 4, "08:30"), (5, 8, "14:10"), (9, 12, "19:00")]
```

上午 4 节 08:30 起，下午 4 节 14:10 起，晚上 19:00 起。大节之间的午饭、晚饭时长不写死，由下一个大节的开始时间决定。学校调整作息时只改这几个常量即可，前端演示数据 `MOCK_SLOTS`（`frontend/app.js`）记得同步。


## 快速开始

需要 Python 3.10+ 且已安装好（勾选 Add to PATH）。

```bat
:: 双击即可，也可以在命令行执行
start.bat

:: 手机同网段访问
start.bat lan
```

脚本会自动找到系统里的 Python、检查依赖、挑一个空闲端口（8000-8010）、打开浏览器。
不需要建虚拟环境。

手动跑也可以：

```bash
pip install -r requirements.txt
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

浏览器打开 `http://127.0.0.1:8000/`，用统一身份认证的账号密码登录。
想先看界面效果可以加 `?mock=1` 用演示数据。

## 目录结构

```
backend/
  app.py          FastAPI 接口层：登录、学年学期、课表、会话持久化
  jwxt.py         教务系统客户端：SSO 全链路 + 课表拉取
  jwxt_rsa.py     正方教务本地登录用的 RSA(PKCS#1 v1.5) 加密（保留备用）
  timetable.py    把 kbList 原始数据规整成前端可直接渲染的结构
frontend/
  index.html      单页界面（内联 SVG 图标，无图标库依赖）
  app.js          无框架，原生 JS
  style.css       设计令牌（配色 / 字体 / 行高）+ 深浅色 + 响应式
data/             会话快照（运行时生成，已 gitignore）
start.bat         Windows 一键启动
```

界面字体是 IBM Plex Sans / IBM Plex Mono，从 jsDelivr 取；取不到会自动退回系统字体
（PingFang SC / 微软雅黑等），只是观感差异，不影响功能。纯前端无构建步骤。

## 登录链路

教务系统没有开放密码登录入口，必须绕学校门户走一遍 CAS：

```
GET  home.gdmec.edu.cn            → OAuth2 authorize
POST auth.gdmec.edu.cn/sso/login  → 账号密码
GET  /sso/oauth2/authorize        → code
GET  home.gdmec.edu.cn/portal/sso/callback?code=...   → 拿到 TGC
GET  jwxt.gdmec.edu.cn/sso/ddlogin                    → CAS 下发 ST
GET  /jwglxt/ticketlogin?uid=...&verify=...           → 教务会话建立
```

教务登录页上那个"统一身份验证登录"按钮对应的 `authJwglxtLoginURL` 是空的，
学校没给它配地址，所以只能从门户进，直接访问教务登录页是进不去的。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/login` | `{username, password}` → `{token}` |
| POST | `/api/login-cookie` | Cookie 注入（SSO 挂掉时的备用通道） |
| GET | `/api/terms` | 学年 / 学期选项 + 当前默认值 |
| GET | `/api/timetable?xnm=&xqm=` | 规整后的课表 |
| GET | `/api/raw?xnm=&xqm=` | 原始 JSON，排查字段差异用 |
| GET | `/api/me` | 学生信息 |
| GET | `/api/health` | 健康检查 |

除 `/api/login`、`/api/login-cookie`、`/api/health` 外都要带 `Authorization: Bearer <token>`。

## 踩过的坑

1. **`jcs` 是整段跨度**（`1-4`），`jc` 才是分段（`1-2节` / `3-4节`）。同一门课在 `kbList` 里会重复出现，
   直接渲染会重叠。按 `(星期, 起止, 课程, 地点, 周次)` 去重。
2. **`zcd` 的单双周按段生效**。`11-13周(单),14-17周` 表示第一段单周、第二段全周。
   把整串当成"单周"处理会错误地干掉 14-17 周。
3. **`jcma` 是位掩码**（15 = 1-4 节，240 = 5-8 节），作为 `jcs` 缺失时的兜底。
4. **Cookie 持久化必须带 domain**。只存 name/value，重启后 httpx 不知道往哪个域发，会话直接失效。
5. **`start.bat` 必须是 GBK + CRLF**。UTF-8 + LF 会让 cmd 逐字节解析错乱，
   报一堆"不是内部或外部命令"。

## 免责声明

个人自用项目，只读教务系统的公开数据，不写库、不改成绩、不绕过任何权限校验。
登录凭据仅在登录瞬间用于换取会话票据，服务端不落盘密码。

## License

MIT
