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
- 第一次登录后自动走一遍聚光灯引导，依次指向周次条、视图切换、课程块和设置；`←` `→` 翻，`Esc` 跳过，设置里可以「重看新手引导」
- 会话落盘，重启服务后旧 token 依然可用；**不保存密码**
- **一个账号同时只有一个会话**：同一学号在另一处登录，旧令牌立刻失效，旧页面会提示「该账号已在另一处登录」，不会出现两个标签页各自读写同一份教务会话
- **`/admin` 数据看板**：谁在什么时候用什么设备登录、接口耗时分布、错误率、设备/浏览器构成、日志尾部，全部实时聚合（ECharts 图表，深浅色自适应）

## 作息时间

课表上的上下课时间由 `config.json` 的 `timetable` 段按规则算出来，不是写死的时间表：

```json
"timetable": {
  "period_minutes": 40,
  "break_between_periods": 10,
  "break_between_pairs": 25,
  "blocks": [[1, 4, "08:30"], [5, 8, "14:10"], [9, 12, "19:00"]]
}
```

上午 4 节 08:30 起（08:30-09:10 / 09:20-10:00 / 10:25-11:05 / 11:15-11:55），下午 4 节 14:10 起，晚上 19:00 起。大节之间的午饭、晚饭时长不写死，由下一个大节的开始时间决定。学校调整作息时只改 `config.json` 里这几个值、重启即可，前端演示数据 `MOCK_SLOTS`（`frontend/app.js`）记得同步。


## 配置与日志

所有可调项都在项目根目录的 **`config.json`**，改完重启生效。代码里不再有散落的设置常量。

`config.json` **里存着管理后台密码，已 gitignore、不入库**；仓库里那份是模板 `config.example.json`。
新环境先复制一份再改：

```bash
cp config.example.json config.json     # Windows: copy config.example.json config.json
```

没有 `config.json` 也能启动：会退回读 `config.example.json` 并在启动日志里提示一句。想让配置放在别处，
用 `KCB_CONFIG=/path/to/conf.json` 指过去。

| 段 | 管什么 |
| --- | --- |
| `server` | 标题、版本、默认 host / port、CORS 白名单 |
| `logging` | 日志级别、格式、控制台与文件两路输出、第三方库降噪 |
| `paths` | `data/`、会话文件名、前端目录 |
| `session` | `single`：同一账号是否只保留最新会话（默认 `true`）；`idle_timeout_hours`：空闲多久算过期，默认 168 小时，填 0 永不过期 |
| `http` | 请求超时、重定向上限、User-Agent、可信反向代理 |
| `jwxt` | 教务 / 门户 / 统一认证三个域名 |
| `terms` | 学期代码 → 中文名（`3` / `12` / `16`） |
| `timetable` | 作息：单节时长、课间、各大节打铃时间 |
| `admin` | 管理后台账号密码、会话时长、失败限流 |
| `cache` | `ttl_seconds`：学年学期 / 课表的缓存秒数，默认 60，填 0 关掉 |
| `audit` | 事件文件路径、轮转大小、内存保留条数、后台刷盘间隔（`flush_interval_ms`，默认 400） |

日志级别只写一个 `logging.level` 就够：

```json
"logging": {
  "level": "DEBUG",
  "console": { "enabled": true, "level": "INFO" },
  "file":    { "enabled": true, "path": "logs/app.log", "level": "DEBUG", "max_bytes": 2097152, "backup_count": 5 }
}
```

- `logging.level` 是**总闸**：设成 `WARNING` 就只剩警告和错误，两路输出一起安静；设成 `DEBUG` 才有最全的细节。
- `console.level` / `file.level` 各自只能比总闸更严，用来让"文件记全量、控制台只看重点"。默认总闸 DEBUG、控制台 INFO、文件 DEBUG——终端安静，日志文件里留着排查用的完整记录。
- 控制台输出重定向时自动去掉颜色；文件固定 UTF-8、按 2 MB 轮转、留 5 份。
- 临时改级别不用动文件：启动命令前加 `KCB_LOG_LEVEL=WARNING`（或 `DEBUG`），环境变量优先级最高。
- 第三方库（`httpx` / `httpcore` / `uvicorn.access`）默认压到 WARNING，需要时在 `logging.quiet` 里调。

配置写坏了不会让服务起不来：文件缺失或 JSON 语法错误就整体回落到内置默认值，单项类型写错只忽略那一项，两种情况都会在启动日志里打一条 WARNING 说明。

日志内容：启动 / 停止、每个 `/api` 请求的耗时与状态码、登录结果（**学号 / 账号完整记录**，不记录密码和 cookie 值）、教务接口往返与课表规整统计（去重了几条、跳过了几条）。异常不再被 `except: pass` 静默吞掉。

日志目录 `logs/` 下有两个文件，分工不同：

| 文件 | 用途 |
| --- | --- |
| `app.log` | 给人看：带时间戳和对齐，按 2 MB 轮转，排查问题时直接翻 |
| `events.jsonl` | 给程序看：一行一个 JSON 事件，`/admin` 看板的数据源，按 5 MB 轮转、留 3 份 |

事件写在 `logs/events.jsonl`（`audit` 段可改路径和容量），只有三种：`request`（每个 `/api` 请求）、`login`（每次登录尝试）、`logout`。管理后台自己的轮询请求不进统计，避免仪表盘把真实流量盖掉。

事件不是同步写盘的：请求线程只把这一行塞进内存队列（约 8 µs），后台线程按 `flush_interval_ms` 攒一批一起写，一次 `open` 写几百行。关服务时（`Ctrl+C` / 关窗口）会把剩下的写完。这样即使在机械盘或同步盘上，接口响应也不会被日志 IO 拖住。

会话文件 `data/sessions.json` 同理——只有 cookie 集合真的变了、或者 `ts` 已经 5 分钟没存过，才打脏标记，后台线程合并写一次。所以正常浏览时这个文件几乎不动。

> 学号是完整写进日志的，这是 `/admin` 能按人统计的前提。`logs/` 已 gitignore，别把日志文件发出去。

> `start.bat` 会自动探测 8000-8010 的空闲端口，所以走它启动时端口以探测结果为准（`server.port` 只是 `python -m backend.app` 的默认值）；host / port 都能用 `KCB_HOST` / `KCB_PORT` 覆盖。

## 快速开始

需要 Python 3.10+ 且已安装好（勾选 Add to PATH）。

```bat
:: 双击即可，也可以在命令行执行
start.bat

:: 手机同网段访问（会先弹一个确认：这条通道是明文 HTTP，见下面「安全边界」）
start.bat lan
```

脚本会自动找到系统里的 Python、检查依赖、挑一个空闲端口（8000-8010）、打开浏览器。
不需要建虚拟环境。

手动跑也可以：

```bash
pip install -r requirements.txt
python -m backend.app                  # 读 config.json 的 host / port
# 或指定端口：
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

浏览器打开 `http://127.0.0.1:8000/`，用统一身份认证的账号密码登录。
想先看界面效果可以加 `?mock=1` 用演示数据。

## 目录结构

```
config.example.json 配置模板（入库），复制成 config.json 再改
config.json       本机的真实配置，含管理后台密码，已 gitignore
backend/
  config.py       读 config.json，深合并默认值，配置写坏也不崩
  logging_setup.py 日志初始化：控制台 + 轮转文件，级别来自配置
  audit.py        事件采集与聚合：JSONL 落盘 + 内存统计，看板的数据源
  admin.py        /api/admin/*：后台登录、限流、统计、日志尾部
  requestinfo.py  从请求里取 IP 与设备信息
  useragent.py    UA 解析：浏览器 / 系统 / 设备类型
  app.py          FastAPI 接口层：登录、学年学期、课表、会话持久化
  jwxt.py         教务系统客户端：SSO 全链路 + 课表拉取
  jwxt_rsa.py     正方教务本地登录用的 RSA(PKCS#1 v1.5) 加密（保留备用）
  timetable.py    把 kbList 原始数据规整成前端可直接渲染的结构
frontend/
  index.html      单页界面（内联 SVG 图标，无图标库依赖）
  app.js          无框架，原生 JS
  style.css       设计令牌（配色 / 字体 / 行高）+ 深浅色 + 响应式
  admin.html      数据看板（指标卡 + ECharts + 明细表 + 日志）
  vendor/         echarts.min.js（1 MB，本地放着，断网也能开看板）
data/             会话快照（运行时生成，已 gitignore）
logs/             运行日志 app.log + 事件 events.jsonl（运行时生成，已 gitignore）
start.bat         Windows 一键启动
```

界面字体是 IBM Plex Sans / IBM Plex Mono，从 jsDelivr 取；取不到会自动退回系统字体
（PingFang SC / 微软雅黑等），只是观感差异，不影响功能。纯前端无构建步骤。

## 管理后台

打开 `http://127.0.0.1:8000/admin`（跟着聊天窗口里那个端口走）。账号密码在 `config.json`：

```json
"admin": {
  "enabled": true,
  "username": "admin",
  "password": "kcb-admin",
  "session_hours": 12,
  "max_login_fails": 5,
  "fail_window_minutes": 10
}
```

`/admin.html` 会 302 跳到这里；`enabled` 关掉时两者都返回 404——静态目录不会绕过这个开关。

**默认密码 `kcb-admin`（模板里是 `change-me`）一定要改**——还在用这两个值时，启动日志会打一条 WARNING 提醒。改完重启生效。
密码明文存在 `config.json` 里，而 `config.json` 现在**不进版本库**，改完不会被顺手提交上去。

看板上有什么：

- **指标卡**：在线会话、累计/今日登录用户、登录成功率、今日请求与错误率、平均与 P95 耗时、今日峰值时段、管理后台登录数
- **图表**：近 7 天请求/错误/登录走势、今日 24 小时分布、接口平均耗时 Top、设备类型、浏览器、响应状态码、登录成功率仪表盘
- **表格**：登录过的用户（学号、成功/失败次数、最后登录、设备、浏览器/系统、IP）、当前在线账号、最近登录明细、最近请求、最慢的请求
- **日志尾部**：直接看 `app.log` 或 `events.jsonl`，随时切换

数据来源是 `logs/events.jsonl`，重启服务不会丢历史（启动时读回最近 `audit.memory_events` 条）。页面默认 15 秒自动刷新，可以关掉。

几点实现上的取舍：

- 管理后台的 token 和学生的登录 token **分开存**，互相不能顶替；token 只放内存，重启即失效
- 同一 IP 连续失败 `max_login_fails` 次后按 `fail_window_minutes` 内限流（实测第 6 次返回 429）
- 这里的"IP"默认取 **TCP 对端地址**。`X-Forwarded-For` / `X-Real-IP` 是客户端能随便写的头，
  默认**不信**——否则伪造一个随机头就能绕过上面的限流。前面真的挂了 nginx 之类的反代时，
  把它的地址填进 `http.trusted_proxies`（支持 `10.0.0.0/8` 这种网段），才会采信转发头
- 密码用 `hmac.compare_digest` 比对，避免计时攻击；明文存在 `config.json` 里，这是个人自用项目的取舍
- 看板要读全量事件做聚合，事件量大了（> 5000 条）就只统计内存里保留的那部分，避免一次请求扫几万行

## 会话策略

学生端一个学号只保留**最新一个**会话，这是 `config.json` 的 `session.single`（默认开）：

- 新登录成功时，先把这个学号名下的其它 token 从内存和 `data/sessions.json` 里摘掉，旧客户端的连接池一并关闭
- **会话会因空闲而过期**：`session.idle_timeout_hours`（默认 168 小时）内一次都没访问过就作废，
  过期时同样会关掉连接池、从内存和磁盘摘掉。每次请求都会刷新"最后活动时间"，所以常用的人不会被踢。
  填 `0` 恢复成"永不过期"的老行为
- 旧标签页下次请求会拿到 401 和一句明确的话：「该账号已在另一处登录，此会话已失效」，而不是含糊的"登录已过期"
- 启动时也会按同样规则清洗历史快照，把超出的一条并成 ts 最新的那条（清理条数写进日志）
- 被顶下线的 token 记在内存里（上限 200 条），只为给出准确提示，重启即清空
- Cookie 登录不强制带学号，用的是占位账号 `cookie-user`，同名占位之间**不互相顶**，否则两个用 Cookie 的人会互相踢下线。
  请求里带的 `username` 只当日志标签，**不参与身份判定**——否则谁都能填一个别人的学号把对方顶掉

想恢复成"允许多端同时在线"，把 `session.single` 改成 `false` 即可。管理后台是只读看板，多终端同时看着是正常需求，所以没套这条规则。

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
| POST | `/api/admin/login` | `{username, password}` → 管理后台 token |
| POST | `/api/admin/logout` | 注销管理后台 token |
| GET | `/api/admin/stats` | 看板全量聚合数据 |
| GET | `/api/admin/logs?source=app\|events&lines=` | 日志尾部 |

学生接口除 `/api/login`、`/api/login-cookie`、`/api/health` 外都要带 `Authorization: Bearer <token>`；
`/api/admin/*` 用自己那套 token，两套不通用。

## 踩过的坑

1. **`jcs` 是整段跨度**（`1-4`），`jc` 才是分段（`1-2节` / `3-4节`）。同一门课在 `kbList` 里会重复出现，
   直接渲染会重叠。按 `(星期, 起止, 课程, 地点, 周次)` 去重。
2. **`zcd` 的单双周按段生效**。`11-13周(单),14-17周` 表示第一段单周、第二段全周。
   把整串当成"单周"处理会错误地干掉 14-17 周。
3. **`jcma` 是位掩码**（15 = 1-4 节，240 = 5-8 节），作为 `jcs` 缺失时的兜底。
4. **Cookie 持久化必须带 domain**。只存 name/value，重启后 httpx 不知道往哪个域发，会话直接失效。
5. **`start.bat` 必须是 GBK + CRLF**。UTF-8 + LF 会让 cmd 逐字节解析错乱，
   报一堆"不是内部或外部命令"。
## 安全边界

这个服务是给**一个人在自己电脑上用**的，默认只监听 `127.0.0.1`。有三条线不要越：

1. **`start.bat lan` 是明文 HTTP。** 学号和统一身份认证密码会以明文穿过局域网，同一网段里任何设备
   抓包就能拿到；拿到你机器上的 token 也能直接看课表。所以脚本在 `lan` 模式下会先打印警告、要你敲
   `Y` 确认。**只在宿舍 / 家里这类可信网络里开，公共 WiFi、教学楼开放网络不要开。**
2. **管理后台密码不要留默认值。** `kcb-admin` / `change-me` 都会在启动日志里触发 WARNING。
3. **`config.json` 和 `data/` 都别提交。** 前者是管理后台密码，后者是带 Cookie 的会话快照
   （等价于你的教务登录态）。两者都已在 `.gitignore` 里。

要真正对公网开放，得自己在前面套一层 HTTPS 反代，并把反代地址写进 `http.trusted_proxies`，
否则限流拿到的 IP 全是反代地址。

## 免责声明

个人自用项目，只读教务系统的公开数据，不写库、不改成绩、不绕过任何权限校验。
登录凭据仅在登录瞬间用于换取会话票据，服务端不落盘密码。

## License

MIT
