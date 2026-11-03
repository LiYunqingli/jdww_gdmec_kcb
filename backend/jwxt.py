"""正方教务客户端（广东机电职业技术学院）

登录路径（实测可用）：
    1. 统一身份认证 OAuth2：home.gdmec.edu.cn -> auth.gdmec.edu.cn/sso/login（账号密码）
       -> /sso/oauth2/authorize -> /portal/sso/callback?code=...  拿到 CAS 的 TGC
    2. 教务单点：jwxt.gdmec.edu.cn/sso/ddlogin
       -> auth.gdmec.edu.cn/cas/login?service=...（TGC 免密）
       -> jwxt/sso/ddlogin?ticket=ST-... -> /jwglxt/ticketlogin -> 教务会话

httpx 的 cookie jar 是域隔离的，所以同一个 Client 可以同时持有三个域名的会话。
域名、超时、UA、重定向次数都在 config.json 里，这里只做兜底默认。
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any
from urllib.parse import urljoin

import httpx

from . import config

_http_cfg = config.get("http") or {}
_jwxt_cfg = config.get("jwxt") or {}

JWXT = str(_jwxt_cfg.get("base") or "https://jwxt.gdmec.edu.cn").rstrip("/")
PORTAL = str(_jwxt_cfg.get("portal") or "https://home.gdmec.edu.cn").rstrip("/")
SSO = str(_jwxt_cfg.get("sso") or "https://auth.gdmec.edu.cn").rstrip("/")

UA = str(_http_cfg.get("user_agent") or "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
TIMEOUT = float(_http_cfg.get("timeout") or 25)
MAX_REDIRECTS = int(_http_cfg.get("max_redirects") or 14)
MAX_REDIRECTS_LOGIN = int(_http_cfg.get("max_redirects_login") or 16)

XQM_LABEL = config.get("terms") or {"3": "第一学期", "12": "第二学期", "16": "第三学期"}

REDIRECTS = (301, 302, 303, 307, 308)

log = logging.getLogger("kcb.jwxt")


class JwxtError(Exception):
    """业务异常，message 直接给前端展示。"""


def _strip_tags(html: str) -> str:
    txt = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    return re.sub(r"\s+", " ", txt).strip()


class JwxtClient:
    def __init__(
        self,
        cookies: Any | None = None,
        cookie_header: str | None = None,
        label: str = "-",
    ):
        self.label = label or "-"
        self.username = ""
        self.http = httpx.Client(
            timeout=TIMEOUT,
            follow_redirects=False,
            headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"},
        )
        log.debug("[%s] 教务客户端初始化：timeout=%ss ua=%s…", self.label, TIMEOUT, UA[:32])
        if cookies:
            self.load_cookies(cookies)
            log.debug("[%s] 恢复 %d 条 cookie", self.label, len(self.cookie_list()))
        if cookie_header:
            self.set_cookie_header(cookie_header)

    # ---------------- 日志 ----------------
    def _log(self, level: int, msg: str, *args: Any) -> None:
        log.log(level, "[%s] " + msg, self.label, *args)

    def _debug(self, msg: str, *args: Any) -> None:
        self._log(logging.DEBUG, msg, *args)

    def _info(self, msg: str, *args: Any) -> None:
        self._log(logging.INFO, msg, *args)

    def _warning(self, msg: str, *args: Any) -> None:
        self._log(logging.WARNING, msg, *args)

    # ---------------- 基础 ----------------
    def cookie_list(self) -> list[dict[str, str]]:
        """带域名的 cookie 快照，用于持久化后精确恢复（三个域名各一套会话）。"""
        return [
            {"name": c.name, "value": c.value, "domain": c.domain or "", "path": c.path or "/"}
            for c in self.http.cookies.jar
        ]

    def cookie_dict(self) -> dict[str, str]:
        return {c.name: c.value for c in self.http.cookies.jar}

    def load_cookies(self, cookies: Any) -> None:
        """支持 [{name,value,domain,path}]（推荐）或 {name: value}（兼容旧数据）。"""
        if isinstance(cookies, dict):
            for k, v in cookies.items():
                self.http.cookies.set(k, v)
            return
        for it in cookies:
            if isinstance(it, dict) and it.get("name"):
                self.http.cookies.set(
                    it["name"], it.get("value", ""),
                    domain=it.get("domain") or None, path=it.get("path") or "/",
                )
            elif isinstance(it, (list, tuple)) and len(it) == 2:
                self.http.cookies.set(it[0], it[1])

    def close(self) -> None:
        self.http.close()
        self._debug("客户端已关闭")

    def _get(self, url: str, **kw) -> httpx.Response:
        return self.http.get(url, **kw)

    def _walk(self, url: str, max_hops: int | None = None, stop_on: str | None = None) -> tuple[str, httpx.Response]:
        """沿 30x 一路跟到底，返回最终 URL 与响应。"""
        max_hops = MAX_REDIRECTS if max_hops is None else max_hops
        resp = self._get(url)
        hops = 0
        while resp.status_code in REDIRECTS and hops < max_hops:
            loc = resp.headers.get("location")
            if not loc:
                self._debug("跳转无 location 头，停止：%s", url)
                break
            url = urljoin(url, loc)
            if stop_on and stop_on in url:
                self._debug("命中 stop_on=%s，停在 %s", stop_on, url)
                return url, resp
            resp = self._get(url)
            hops += 1
            self._debug("跳转 %d/%d → %s（HTTP %s）", hops, max_hops, url, resp.status_code)
        if hops >= max_hops and resp.status_code in REDIRECTS:
            self._warning("重定向次数超过上限 %d，停在 %s", max_hops, url)
        return url, resp

    def is_alive(self) -> bool:
        try:
            r = self.http.get(
                f"{JWXT}/jwglxt/xtgl/index_initMenu.html",
                headers={"Referer": f"{JWXT}/jwglxt/xtgl/login_slogin.html"},
            )
            ok = r.status_code == 200 and len(r.text) > 2000
            self._debug("会话探活：HTTP %s，页面 %d 字节 → %s", r.status_code, len(r.text), "有效" if ok else "无效")
            return ok
        except httpx.HTTPError as exc:
            self._warning("会话探活请求失败：%s: %s", type(exc).__name__, exc)
            return False

    # ---------------- 登录：统一身份认证 ----------------
    def login_sso(self, username: str, password: str) -> None:
        started = time.perf_counter()
        self._info("SSO 登录开始")
        self._portal_oauth(username, password)
        self._cas_into_jwxt()
        if not self.is_alive():
            raise JwxtError("统一身份认证已通过，但换取教务会话失败（ticketlogin 未生效）")
        self.username = username
        self._info("SSO 登录完成，耗时 %.1fs", time.perf_counter() - started)

    def _portal_oauth(self, username: str, password: str) -> None:
        """走门户 OAuth2 授权码流程，完成 SSO 登录。"""
        url, resp = self._walk(f"{PORTAL}/", stop_on="/sso/login")
        if "/sso/login" not in url:
            raise JwxtError(f"未进入统一身份认证登录页（当前 {url[:120]}）")
        self._debug("已进入统一身份认证登录页")

        page = self._get(url)
        html = page.text
        pid = re.search(r'name="pid"[^>]*value="([^"]*)"', html)
        if not pid:
            raise JwxtError("统一身份认证登录页结构异常：未找到 pid")

        post = self.http.post(
            url,
            data={"username": username, "password": password, "pid": pid.group(1), "source": ""},
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": url,
                "Origin": SSO,
            },
        )
        self._debug("提交账号密码：HTTP %s", post.status_code)

        if post.status_code not in REDIRECTS:
            msg = self._sso_error(post.text) or "账号或密码不正确"
            raise JwxtError(f"统一身份认证登录失败：{msg}")

        # 跟回门户
        cur = urljoin(url, post.headers.get("location", ""))
        resp = post
        for _ in range(MAX_REDIRECTS):
            resp = self._get(cur)
            loc = resp.headers.get("location")
            if resp.status_code not in REDIRECTS or not loc:
                break
            cur = urljoin(cur, loc)
        if not cur.startswith(PORTAL):
            raise JwxtError(f"授权回调未回到门户（停在 {cur[:120]}）")
        names = {c.name for c in self.http.cookies.jar}
        if not ({"LOGIN_TOKEN", "CAS_TICKET"} & names):
            raise JwxtError("统一身份认证未下发登录令牌，会话未建立")
        self._debug("门户会话建立，cookie 名：%s", ", ".join(sorted(names)) or "（空）")

    @staticmethod
    def _sso_error(html: str) -> str:
        m = re.search(r'id="errorcode"[^>]*value="([^"]+)"', html)
        if m and m.group(1).strip():
            return f"错误码 {m.group(1).strip()}"
        m = re.search(r'id="errormes"[^>]*value="([^"]+)"', html)
        if m and m.group(1).strip():
            return m.group(1).strip()
        text = _strip_tags(html)
        for kw in ("密码错误", "用户名或密码", "账号不存在", "锁定", "验证码", "过期"):
            idx = text.find(kw)
            if idx >= 0:
                return text[max(0, idx - 20): idx + 40]
        return ""

    def _cas_into_jwxt(self) -> None:
        """教务 CAS 单点：ddlogin -> cas/login -> ddlogin?ticket -> ticketlogin。"""
        url, resp = self._walk(f"{JWXT}/sso/ddlogin", max_hops=MAX_REDIRECTS_LOGIN)
        self._debug("CAS 单点结束：%s（HTTP %s）", url, resp.status_code)

    # ---------------- 登录：教务本地账号（备用） ----------------
    def login_local(self, username: str, password: str) -> None:
        from .jwxt_rsa import rsa_encrypt

        started = time.perf_counter()
        self._info("教务本地登录开始")
        page = self.http.get(f"{JWXT}/jwglxt/xtgl/login_slogin.html")
        if page.status_code != 200:
            raise JwxtError(f"无法打开教务登录页（HTTP {page.status_code}）")
        html = page.text

        token = re.search(r'id="csrftoken"[^>]*value="([^"]+)"', html)
        if not token:
            raise JwxtError("教务登录页结构异常：未找到 csrftoken")

        pk = self.http.get(
            f"{JWXT}/jwglxt/xtgl/login_getPublicKey.html",
            params={"time": int(time.time() * 1000)},
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": f"{JWXT}/jwglxt/xtgl/login_slogin.html"},
        )
        try:
            key = pk.json()
        except Exception as exc:  # noqa: BLE001
            raise JwxtError(f"获取公钥失败：{pk.text[:120]}") from exc
        self._debug("已取到 csrftoken 与公钥")

        data = _parse_hidden(html)
        data.update({
            "csrftoken": token.group(1),
            "yhm": username,
            "mm": rsa_encrypt(password, key["modulus"], key["exponent"]),
            "language": "zh_CN",
        })
        self.http.post(
            f"{JWXT}/jwglxt/xtgl/login_slogin.html",
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": f"{JWXT}/jwglxt/xtgl/login_slogin.html",
                "Origin": JWXT,
            },
        )
        if not self.is_alive():
            raise JwxtError("教务系统账号或密码不正确")
        self.username = username
        self._info("教务本地登录完成，耗时 %.1fs", time.perf_counter() - started)

    # ---------------- Cookie 注入 ----------------
    def set_cookie_header(self, cookie: str) -> None:
        cookie = re.sub(r"^Cookie:\s*", "", cookie.strip(), flags=re.I)
        count = 0
        for part in cookie.split(";"):
            if "=" not in part:
                continue
            name, _, value = part.partition("=")
            self.http.cookies.set(name.strip(), value.strip())
            count += 1
        self._debug("注入 %d 条 cookie（仅 name/value，无 domain）", count)

    def login_with_cookie(self, cookie: str) -> None:
        self.set_cookie_header(cookie)
        if not self.is_alive():
            raise JwxtError("Cookie 无效或已过期（进不去教务首页）")
        self._info("Cookie 登录完成")

    # ---------------- 学生信息 ----------------
    def student_info(self) -> dict[str, Any]:
        html = self.http.get(f"{JWXT}/jwglxt/xtgl/index_initMenu.html").text
        info: dict[str, Any] = {}
        m = re.search(r'id="sessionUserName"[^>]*>([^<]*)<', html)
        if m:
            info["name"] = m.group(1).strip()
            self._debug("读到学生姓名：%s", info["name"])
        else:
            self._warning("首页未找到 sessionUserName，学生信息为空")
        return info

    # ---------------- 学年 / 学期 ----------------
    def terms(self) -> dict[str, Any]:
        r = self.http.get(f"{JWXT}/jwglxt/kbcx/xskbcx_cxXskbcxIndex.html", params={"gnmkdm": "N2151"})
        if r.status_code in REDIRECTS:
            raise JwxtError("教务会话已失效，请重新登录")
        html = r.text

        def options(select_id: str) -> list[dict[str, Any]]:
            m = re.search(r'<select[^>]*id="%s"[\s\S]*?</select>' % select_id, html, re.I)
            if not m:
                return []
            out = []
            for om in re.finditer(r"<option[^>]*>", m.group(0)):
                tag = om.group(0)
                vm = re.search(r'value="([^"]*)"', tag)
                if not vm:
                    continue
                rest = m.group(0)[om.end():]
                label = re.split(r"</option>", rest, maxsplit=1)[0]
                out.append({
                    "value": vm.group(1),
                    "label": re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", label)).strip() or vm.group(1),
                    "selected": "selected" in tag.lower(),
                })
            return [o for o in out if o["value"].strip()]

        xnms, xqms = options("xnm"), options("xqm")
        # 学期下拉的 label 常是裸数字 "1/2/3"，转成中文
        _short = {"1": "第一学期", "2": "第二学期", "3": "第三学期"}
        for o in xqms:
            if o["label"] in _short:
                o["label"] = _short[o["label"]]
        if not xqms:
            self._warning("学期下拉为空，回落到内置的 XQM_LABEL")
            xqms = [{"value": k, "label": v, "selected": k == "3"} for k, v in XQM_LABEL.items()]
        cur_xnm = next((o["value"] for o in xnms if o["selected"]), xnms[0]["value"] if xnms else "")
        cur_xqm = next((o["value"] for o in xqms if o["selected"]), xqms[0]["value"] if xqms else "3")
        self._debug("学年 %d 项，学期 %d 项，当前 %s/%s", len(xnms), len(xqms), cur_xnm, cur_xqm)
        return {"xnm": xnms, "xqm": xqms, "current": {"xnm": cur_xnm, "xqm": cur_xqm}}

    # ---------------- 课表 ----------------
    def timetable(self, xnm: str, xqm: str) -> dict[str, Any]:
        started = time.perf_counter()
        r = self.http.post(
            f"{JWXT}/jwglxt/kbcx/xskbcx_cxXsKb.html",
            params={"gnmkdm": "N2151"},
            data={"xnm": str(xnm), "xqm": str(xqm), "kzlx": "ck"},
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Referer": f"{JWXT}/jwglxt/kbcx/xskbcx_cxXskbcxIndex.html?gnmkdm=N2151",
            },
        )
        if r.status_code in REDIRECTS:
            raise JwxtError("教务会话已失效，请重新登录")
        body = r.text.strip()
        if not body:
            raise JwxtError("课表接口返回空内容")
        try:
            data = r.json()
        except Exception as exc:  # noqa: BLE001
            self._warning("课表接口返回非 JSON：%s", body[:160])
            raise JwxtError(f"课表接口返回非 JSON：{body[:160]}") from exc
        if not isinstance(data, dict):
            raise JwxtError("课表接口返回格式异常")
        kb = data.get("kbList")
        self._debug(
            "课表接口返回：xnm=%s xqm=%s HTTP %s %d 字节 kbList=%s 条（%.0f ms）",
            xnm, xqm, r.status_code, len(body),
            len(kb) if isinstance(kb, list) else "缺失",
            (time.perf_counter() - started) * 1000,
        )
        return data


def _parse_hidden(html: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in re.finditer(r"<input[^>]*>", html, re.I):
        tag = m.group(0)
        if not re.search(r'type\s*=\s*"?hidden', tag, re.I):
            continue
        nm = re.search(r'name\s*=\s*"([^"]*)"', tag)
        if not nm:
            continue
        vm = re.search(r'value\s*=\s*(?:"([^"]*)"|([^\s>]*))', tag)
        val = (vm.group(1) if vm and vm.group(1) is not None else (vm.group(2) if vm else "")) or ""
        out[nm.group(1)] = val.strip()
    return out
