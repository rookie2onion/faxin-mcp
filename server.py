#!/usr/bin/env python3
"""
法信 (faxin.cn) MCP Server —— 公开版（REST 化，法信官网账号登录）

技术要点：检索与详情全部为带登录 cookie 的纯 HTTP 调用
（Playwright 仅作 cookie 容器，不渲染检索页），单次检索延迟约 2-8 秒。

主要接口（均为法信平台公开网页端实际调用的接口）：
  - 法规检索：  POST /alllibsearch/GetFileInfoForZyfl.ashx （中央法规, lib=zyfl）
                POST /alllibsearch/GetFileInfoForDffl.ashx （地方法规, lib=dffl）
                返回 JSON {"FirstHtml": "<li>..."} ；时效筛选 shixiao_id_left：01现行有效/02失效/03已被修改/04尚未生效
  - 裁判规则：  POST /alllibsearch/GetFileInfoForAlyz.ashx （isAlyz=1, lib= 留空）
                注意：/search/GeneralCaseSearch.aspx 直检页会忽略关键词返回全库默认，本服务不使用该页
  - 分库计数：  POST /alllibsearch/GetLibSearchResultNum.ashx → {"lib":..,"searchnum":..}
  - 法条直达：  GET  /alllibsearch/GetZyflTiaoInfo.ashx?k=法规名+中文条号连写&t=1
  - 法信大纲：  POST /alllibsearch/GetKeyWordList.ashx（概念检索）
                GET  /keyword/KeyWordDetail.aspx?id=（概念聚合）
  - 类案检索：  POST https://wenshu.faxin.cn/faxin/search/search（JSON 查询 DSL，
                token 从主站首页提取后作为 faxintoken 请求头）
  - 详情页：    服务端直出，纯 GET
                A* gid → /lib/zyfl/zyflcontent.aspx?gid=
                B* gid → /lib/dffl/DfflContent.aspx?gid=
                C* gid → /lib/cpal/AlyzContent.aspx?isAlyz=1&gid=

合规说明（请务必阅读）：
  - 本服务仅以使用者本人已通过法信官网正常登录的会话，访问其账号权限内的内容；
    不含任何破解、绕过验证、模拟登录或验证码识别功能；
  - 不使用任何隐藏自动化特征 / 规避检测的启动参数；
  - 仅供个人研究学习使用，请低频按需检索，不批量抓取、不建本地镜像库，
    并遵守法信平台用户协议。

登录：首次使用或会话过期时运行 login.py，在弹出的浏览器中用本人法信账号登录。
"""

import asyncio
import html as _html
import json
import logging
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("faxin-mcp")

# ── 配置 ──────────────────────────────────────────────
FAXIN_BASE = os.environ.get("FAXIN_BASE_URL", "https://www.faxin.cn")
USER_DATA_DIR = os.environ.get(
    "FAXIN_USER_DATA_DIR",
    str(Path.home() / ".faxin-mcp" / "browser-data"),
)
HEADLESS = os.environ.get("FAXIN_HEADLESS", "true").lower() == "true"
MAX_RESULTS = int(os.environ.get("FAXIN_MAX_RESULTS", "20"))
HTTP_TIMEOUT = 30000
LOGIN_CACHE_TTL = 1800  # 登录态判定缓存 30 分钟

app = Server("faxin-mcp")

# ── 浏览器上下文（仅作 cookie 容器）────────────────────
_context = None
_ctx_lock = asyncio.Lock()


async def get_context():
    """获取或创建持久化 context。刻意无反检测参数（不需要）。"""
    global _context
    async with _ctx_lock:
        if _context:
            try:
                _ = _context.pages  # 探测 context 是否仍可用
                return _context
            except Exception:
                _context = None
        from playwright.async_api import async_playwright

        pw = await async_playwright().start()
        Path(USER_DATA_DIR).mkdir(parents=True, exist_ok=True)
        _context = await pw.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            headless=HEADLESS,
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
            # 合规声明：不使用 --disable-blink-features=AutomationControlled 等
            # 任何隐藏自动化特征的参数。
        )
        return _context


class SessionExpired(Exception):
    """法信会话已失效（接口被重定向到登录页或返回登录表单）。"""
    pass


_LOGIN_HINT = (
    "❌ 法信会话已过期或未登录，无法检索。请重新登录：\n"
    "1. 终端运行 login.py（弹出浏览器，用本人法信官网账号登录）；\n"
    "2. 登录成功后按回车保存即可，无需重启本服务。\n"
    "（会话有效期有限，过期属正常现象）"
)

# 登录态判定缓存
_login_ok_at = 0.0


def _looks_like_login_page(url: str, body: str) -> bool:
    if "login" in url.lower():
        return True
    head = body[:3000]
    return ("请输入账号" in head) or ("请先登录" in head) or ("登录法信" in head and "退出" not in head)


async def _http_post(ctx, path: str, form: dict) -> str:
    """带会话 POST，返回响应文本；被重定向到登录页则抛 SessionExpired。超时自动重试一次。"""
    from playwright.async_api import TimeoutError as PwTimeout
    resp = None
    for attempt in range(2):
        try:
            resp = await ctx.request.post(
                FAXIN_BASE + path,
                form=form,
                headers={"X-Requested-With": "XMLHttpRequest"},
                timeout=HTTP_TIMEOUT,
            )
            break
        except PwTimeout:
            if attempt == 1:
                raise
            logger.info(f"POST {path} 超时，重试一次")
            await asyncio.sleep(1)
    body = await resp.text()
    if _looks_like_login_page(resp.url, body):
        raise SessionExpired(f"POST {path} 被重定向至登录页")
    return body


async def _http_get(ctx, path_or_url: str) -> tuple[str, str]:
    """带会话 GET，返回 (最终URL, 响应文本)。"""
    url = path_or_url if path_or_url.startswith("http") else FAXIN_BASE + path_or_url
    resp = await ctx.request.get(url, timeout=HTTP_TIMEOUT)
    body = await resp.text()
    if _looks_like_login_page(resp.url, body):
        raise SessionExpired(f"GET {url} 被重定向至登录页")
    return resp.url, body


async def ensure_login(ctx, force: bool = False) -> tuple[bool, str]:
    """轻量登录态判定：调分库计数接口，返回 JSON 即已登录。结果缓存 30 分钟。"""
    global _login_ok_at
    if not force and (time.time() - _login_ok_at) < LOGIN_CACHE_TTL:
        return True, "登录态缓存有效（30分钟内已验证）"
    try:
        body = await _http_post(
            ctx,
            "/alllibsearch/GetLibSearchResultNum.ashx",
            {"k": "test", "IsInResult": "0", "keyword_inResult": "test",
             "usersearchtype": "1", "lib": "zyfl"},
        )
        data = json.loads(body)
        if "searchnum" in data:
            _login_ok_at = time.time()
            return True, "会话有效"
        return False, f"计数接口返回异常: {body[:100]}"
    except SessionExpired as e:
        return False, str(e)
    except Exception as e:
        return False, f"检测异常: {e}"


# ── FirstHtml 解析 ────────────────────────────────────
def _strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def parse_first_html(first_html: str) -> list[dict]:
    """解析 GetFileInfoFor*.ashx 返回的 FirstHtml，提取结果条目。

    条目结构：<li ...><div class="div-title"><a href='/lib/...gid=X'>标题</a></div>
    ...公布/施行日期等文本...</li>；分组头 <li class='allmore'> 含库名与组内命中数。
    """
    results = []
    group = ""
    for li in re.findall(r"<li[^>]*>(.*?)</li>", first_html, re.S | re.I):
        # 分组头：<li class='allmore'>司法解释（33）...
        if "allmore" in li[:80]:
            g = _strip_tags(li)
            g = re.sub(r"更多.*$", "", g).strip()
            if g:
                group = g
            continue
        m = re.search(r"<a[^>]+href=['\"]([^'\"]*gid=[A-Z0-9]+[^'\"]*)['\"][^>]*>(.*?)</a>", li, re.S | re.I)
        if not m:
            continue
        href, title = m.group(1), _strip_tags(m.group(2))
        if not title or len(title) < 2:
            continue
        gid_m = re.search(r"gid=([A-Z0-9]+)", href)
        gid = gid_m.group(1) if gid_m else ""
        ctx_text = _strip_tags(li)
        ctx_text = ctx_text.replace(title, "", 1).strip()
        # 抽取公布/施行日期（法规结果自带）
        dates = {}
        dm = re.search(r"(\d{4}\.\d{2}\.\d{2})\s*公布", ctx_text)
        if dm:
            dates["公布"] = dm.group(1)
        dm = re.search(r"(\d{4}\.\d{2}\.\d{2})\s*施行", ctx_text)
        if dm:
            dates["施行"] = dm.group(1)
        results.append({
            "title": title,
            "gid": gid,
            "url": href if href.startswith("http") else FAXIN_BASE + href,
            "group": group,
            "snippet": ctx_text[:200],
            **dates,
        })
    # 跨分组去重
    seen, out = set(), []
    for r in results:
        if r["gid"] and r["gid"] in seen:
            continue
        seen.add(r["gid"])
        out.append(r)
    return out


def _is_no_content(body: str) -> bool:
    """判断检索响应是否为「暂无内容」（平台权限门或无命中的统一空态）。

    权限门特征：接口返回 200 + AllCount>0（按关键词计数正常）但结果区为
    「暂时没有找到您想要的内容」空态（no_content 容器），且无任何 gid 链接。
    """
    return ("no_content" in body) or ("暂时没有找到您想要的内容" in body)


async def get_lib_counts(ctx, keyword: str, libs: list[str]) -> dict:
    """分库命中数。失败不阻塞主流程。注意：form 会自动编码，keyword 必须传原文。"""
    counts = {}
    async def one(lib):
        try:
            body = await _http_post(
                ctx, "/alllibsearch/GetLibSearchResultNum.ashx",
                {"k": keyword, "IsInResult": "0", "keyword_inResult": keyword,
                 "usersearchtype": "1", "lib": lib})
            data = json.loads(body)
            counts[lib] = int(data.get("searchnum", 0))
        except Exception:
            pass
    await asyncio.gather(*[one(l) for l in libs])
    return counts


# ── 检索实现 ──────────────────────────────────────────
# 时效筛选映射（对应法信检索页时效筛选控件的 value 值）
TIMELINESS_MAP = {
    "current": "01",   # 现行有效
    "invalid": "02",   # 失效
    "modified": "03",  # 已被修改
    "pending": "04",   # 尚未生效
}
TIMELINESS_LABEL = {"01": "现行有效", "02": "失效", "03": "已被修改", "04": "尚未生效"}


async def search_law(ctx, keyword: str, timeliness: str = "", lib: str = "all",
                     max_results: int = MAX_RESULTS) -> dict:
    """法规检索。timeliness: current/invalid/modified/pending 或 01-04；lib: central/local/all。

    中央法规并行双发——smlib=lfjs（法律本体组；民法典/刑法本体只能从这里拿到，
    smlib=other 混合组中法律本体易被司法解释挤占）+ smlib=other（其余分组）。
    lfjs 组只匹配法律标题，对非法规名关键词返回空，合并无噪声。
    """
    shixiao = TIMELINESS_MAP.get(timeliness, timeliness if timeliness in TIMELINESS_LABEL else "")
    libs = {"central": ["zyfl"], "local": ["dffl"], "all": ["zyfl", "dffl"]}.get(lib, ["zyfl", "dffl"])

    async def fetch(lb: str, smlib: str = "other", group_label: str = "") -> list[dict]:
        handler = "GetFileInfoForZyfl.ashx" if lb == "zyfl" else "GetFileInfoForDffl.ashx"
        body = await _http_post(
            ctx, f"/alllibsearch/{handler}",
            {"keyTitle": keyword, "lib": lb, "smlib": smlib,
             "firstPage": "1", "listnum": str(min(max_results, 50)),
             "usersearchtype": "1", "libsearchtype": "1",
             "sort_field": "", "sort_id_left": "0", "xiaoli_id_left": "0",
             "shixiao_id_left": shixiao or "0", "fdep_id_left": "0"},
        )
        data = json.loads(body)
        items = parse_first_html(data.get("FirstHtml", ""))
        for it in items:
            it["lib"] = "中央法规" if lb == "zyfl" else "地方法规"
            if group_label:
                it["group"] = group_label
        return items

    # 中央法规：法律本体组（置顶）+ 原混合组并行
    fetch_jobs = []
    for lb in libs:
        if lb == "zyfl":
            fetch_jobs.append(fetch("zyfl", "lfjs", "法律（含法典本体）"))
            fetch_jobs.append(fetch("zyfl", "other"))
        else:
            fetch_jobs.append(fetch(lb))

    results_lists, counts = await asyncio.gather(
        asyncio.gather(*fetch_jobs),
        get_lib_counts(ctx, keyword, libs),
    )
    results = [x for lst in results_lists for x in lst]
    # 双发可能带回重复（同一部法律同时出现在两组），按 gid 去重，lfjs 组优先
    seen, deduped = set(), []
    for r in results:
        if r["gid"] and r["gid"] in seen:
            continue
        seen.add(r["gid"])
        deduped.append(r)
    return {
        "results": deduped[:max_results],
        "raw_count": len(deduped),
        "counts": counts,
        "timeliness_applied": TIMELINESS_LABEL.get(shixiao, ""),
    }


async def search_cases(ctx, keyword: str, max_results: int = MAX_RESULTS) -> dict:
    """裁判规则检索。走 GetFileInfoForAlyz.ashx（真正按关键词过滤）。

    注意：不使用 /search/GeneralCaseSearch.aspx —— 该直检页忽略关键词返回全库默认。
    """
    body = await _http_post(
        ctx, "/alllibsearch/GetFileInfoForAlyz.ashx",
        {"keyTitle": keyword, "isAlyz": "1", "lib": "",
         # Alyz 库大响应慢，高频调用会触发平台临时限速——条数上限压到 30，请低频使用
         "firstPage": "1", "listnum": str(min(max_results, 30)),
         "usersearchtype": "1", "libsearchtype": "1",
         "sort_field": "", "sort_id_left": "0", "fcourt_id_left": "",
         "xfys_id_left": "", "cpsxz_id_left": "", "slcx_id_left": "",
         "result_id_left": "0", "comefrom_id_left": ""},
    )
    data = json.loads(body)
    results = parse_first_html(data.get("FirstHtml", ""))
    for it in results:
        it["lib"] = "裁判规则"
    counts = await get_lib_counts(ctx, keyword, ["cpws_al"])

    # 权限门检测：结果为空但接口按关键词计数正常（AllCount>0）且为平台空态
    # → 账号对裁判规则库无内容权限，显式告知而非返回空列表
    if not results:
        all_count = 0
        try:
            all_count = int(data.get("AllCount", 0) or 0)
        except (TypeError, ValueError):
            all_count = 0
        if _is_no_content(body) and all_count > 0:
            return {
                "results": [],
                "raw_count": 0,
                "counts": counts,
                "permission_denied": True,
                "lib_name": "裁判规则",
                "message": (
                    "当前账号对「裁判规则库」无内容访问权限（接口可按关键词计数 "
                    f"AllCount={all_count}，但结果列表被平台锁定）。"
                    "裁判规则库为法信付费库，请在法信官网为该账号开通相应权限后重试。"
                ),
            }
    return {
        "results": results[:max_results],
        "raw_count": len(results),
        "counts": counts,
    }


# ── 专题库检索 ────────────────────────────────────────
# 库代码 → 句柄/名称/usersearchtype。句柄大小写不敏感（IIS）。
EXTRA_LIBS = {
    "lf":   {"name": "立法资料", "handler": "GetFileInfoForlf.ashx",   "ust": "1"},
    "sf":   {"name": "司法文件", "handler": "GetFileInfoForsf.ashx",   "ust": "1"},
    "gjty": {"name": "国家条约", "handler": "GetFileInfoForgjty.ashx", "ust": "1"},
    "flqk": {"name": "法学期刊", "handler": "GetFileInfoForflqk.ashx", "ust": "1"},
    "flsy": {"name": "法律释义", "handler": "GetFileInfoForflsy.ashx", "ust": "1"},
    "amk":  {"name": "澳门法律", "handler": "GetFileInfoForamk.ashx",  "ust": "1"},
    "xgk":  {"name": "香港法律", "handler": "GetFileInfoForxgk.ashx",  "ust": "4"},
}


async def search_lib(ctx, keyword: str, lib: str, max_results: int = MAX_RESULTS) -> dict:
    """专题库检索（立法资料/司法文件/国家条约/期刊/释义/澳门/香港）。"""
    cfg = EXTRA_LIBS[lib]
    form = {
        "keyTitle": keyword, "lib": lib,
        "firstPage": "1", "listnum": str(min(max_results, 30)),
        "usersearchtype": cfg["ust"], "libsearchtype": cfg["ust"],
        "sort_field": "", "fdep_id_left": "",
    }
    if lib == "xgk":
        form["notitle"] = "1"
    body = await _http_post(ctx, f"/alllibsearch/{cfg['handler']}", form)
    data = json.loads(body)
    results = parse_first_html(data.get("FirstHtml", ""))
    for it in results:
        it["lib"] = cfg["name"]
    # 权限门检测：结果为空但接口按关键词计数正常且为平台空态 → 账号无该库权限
    if not results:
        all_count = 0
        try:
            all_count = int(data.get("AllCount", 0) or 0)
        except (TypeError, ValueError):
            all_count = 0
        if _is_no_content(body) and all_count > 0:
            return {"results": [], "raw_count": 0, "counts": {}, "lib_name": cfg["name"],
                    "permission_denied": True,
                    "message": (f"当前账号对「{cfg['name']}」库无内容访问权限（接口可按关键词计数 "
                                f"AllCount={all_count}，但结果列表被平台锁定）。"
                                "该库为法信付费库，请在法信官网为该账号开通相应权限后重试。")}
    return {"results": results[:max_results], "raw_count": len(results),
            "counts": {}, "lib_name": cfg["name"]}


# ── 类案检索（wenshu 子站）──────────────────────────
# 机制：主站首页 HTML 提取 laIndex 链接的 token → 子站 API 认证头 faxintoken
# 检索：POST https://wenshu.faxin.cn/faxin/search/search（JSON 查询 DSL）
#   条件形态：{"type":2,"field":"<字段>","value":"<值>"}；组合 {"type":1,"and":[...]}
#   支持字段：title（标题）、casetype（民事/刑事/行政/执行）、casecause（案由）
#   筛选项（来自 aggs 端点）：referencetype/features/focus/province/courtlevel/judgeyear/procedure/doctype/judgeresult
#   索引：fxal=权威案例库（指导/公报/公布案例），wenshu=普通案例
#   注意：当前仅支持标题/字段筛选，未实现全文检索条件
# 详情：docs 的 uniqid 即 C* gid，走现有 get_detail 路由（AlyzContent 兼容）

WENSHU_API = "https://wenshu.faxin.cn/faxin"
_lalk_token = ""
_lalk_token_at = 0.0


async def _get_lalk_token(ctx, force: bool = False) -> str:
    """从主站首页提取类案检索子站 token（缓存 30 分钟）。"""
    global _lalk_token, _lalk_token_at
    if not force and _lalk_token and (time.time() - _lalk_token_at) < 1800:
        return _lalk_token
    _, body = await _http_get(ctx, FAXIN_BASE + "/")
    m = re.search(r'href="https://(?:[a-z0-9-]+\.)?wenshu\.faxin\.cn/wenshu/page/laIndex\.html\?[^"]*?token=([^&"]+)', body)
    token_val = m.group(1) if m else ""
    if not token_val:
        raise SessionExpired(
            "未能从首页提取类案检索 token。注意：法信主站（www.faxin.cn）首页 laIndex "
            "链接的 token 为空、类案 token 生成机制与律协版不同——类案检索在主站暂不可用"
            "（本连接器在律协版 sfb-vip.faxin.cn 下类案检索正常）。"
        )
    _lalk_token = token_val
    _lalk_token_at = time.time()
    return _lalk_token


async def search_lalei(ctx, keyword: str = "", casetype: str = "", casecause: str = "",
                       referencetype: str = "", province: str = "", courtlevel: str = "",
                       judgeyear: str = "", procedure: str = "",
                       size: int = 10, index: str = "fxal") -> dict:
    """类案检索（权威案例库）。条件 AND 组合；keyword 走标题匹配。"""
    conds = []
    if keyword:
        conds.append({"type": 2, "field": "title", "value": keyword})
    for field, val in [("casetype", casetype), ("casecause", casecause),
                       ("referencetype", referencetype), ("province", province),
                       ("courtlevel", courtlevel), ("judgeyear", judgeyear),
                       ("procedure", procedure)]:
        if val:
            conds.append({"type": 2, "field": field, "value": val})
    if not conds:
        raise ValueError("至少提供一个检索条件（keyword 或任一筛选字段）")

    token = await _get_lalk_token(ctx)
    payload = {
        "query": {"type": 1, "mode": "default", "and": conds},
        "params": {"from": 0, "size": min(size, 30),
                   "sort": ["_score desc", "fit_score desc"], "index": index},
    }
    resp = await ctx.request.post(
        f"{WENSHU_API}/search/search",
        data=json.dumps(payload),
        headers={"faxintoken": token, "Content-Type": "application/json"},
        timeout=HTTP_TIMEOUT,
    )
    body = await resp.text()
    if resp.status in (400, 401, 403):
        # token 失效：清缓存重试一次
        token = await _get_lalk_token(ctx, force=True)
        resp = await ctx.request.post(
            f"{WENSHU_API}/search/search",
            data=json.dumps(payload),
            headers={"faxintoken": token, "Content-Type": "application/json"},
            timeout=HTTP_TIMEOUT,
        )
        body = await resp.text()
    data = json.loads(body)
    if data.get("status") == 400:
        raise ValueError(f"类案检索请求被拒：{body[:150]}")
    result = data.get("data") or {}
    docs = []
    for d in (result.get("docs") or []):
        docs.append({
            "uniqid": d.get("uniqid", ""),
            "title": _strip_tags(d.get("title", "")),
            "casetype": d.get("casetype", ""),
            "casecause": d.get("casecause", ""),
            "procedure": d.get("procedure", ""),
            "referencetype": d.get("referencetype", ""),
            "court": d.get("pcourt", ""),
            "courtlevel": d.get("courtlevel", ""),
            "judgeyear": d.get("judgeyear", ""),
            "tuijiandu": d.get("tuijiandu", ""),
        })
    return {"total": result.get("total", 0), "docs": docs}


# ── 法条推荐直达（GetZyflTiaoInfo）────────────────────
# POST /alllibsearch/GetZyflTiaoInfo.ashx  k=法规名+中文条号连写（民法典第六百八十条），t=1
# 返回 HTML：法规名+gid+#条号锚点 / 文号+公布施行日期 / 条号+条文主旨+条文全文
# 注意：k 必须是「法规名+中文条号」连写形态；k 与条号分传（k=民法典&t=680）返回空


async def get_tiao(ctx, law: str, tiao: str) -> dict:
    """一句话取法条：法规名（可简称）+ 条号（阿拉伯/中文）→ 条文全文+效力信息。"""
    law = law.strip().strip("《》")
    tiao_s = str(tiao).strip().lstrip("第")
    # 条号统一转中文数字
    m = re.match(r"^([一二三四五六七八九十百千零0-9]+)条?(之[一二三四五六七八九十]+)?$", tiao_s)
    if not m:
        raise ValueError(f"条号格式无法识别：{tiao}（示例：577 / 第五百七十七条 / 第五百七十七条之一）")
    num = cn_to_int(m.group(1))
    if num is None:
        raise ValueError(f"条号无法解析：{tiao}")
    cn = int_to_cn(num) + (m.group(2) or "")

    body = await _http_post(ctx, "/alllibsearch/GetZyflTiaoInfo.ashx",
                            {"k": f"{law}第{cn}条", "t": "1"})
    if not body.strip():
        raise ValueError(f"未命中：{law}第{cn}条。请核对法规名与条号（条号需为中文或阿拉伯整数）")

    out = {"law": "", "gid": "", "anchor_url": "", "doc_no_dates": "", "tiao": "", "content": ""}
    tm = re.search(r'<a href="(/lib/Zyfl/ZyflContent\.aspx\?gid=([A-Z0-9]+)#(\d+))"[^>]*>([^<]+)</a><br\s*/?><span>([^<]*)</span>', body)
    if tm:
        out["anchor_url"] = FAXIN_BASE + tm.group(1)
        out["gid"] = tm.group(2)
        out["law"] = tm.group(4).strip()
        out["doc_no_dates"] = tm.group(5).strip()
    cm = re.search(r'<div class="zyfl_tiao_tiaoinfo">(.*?)</div>', body, re.S)
    if cm:
        seg = cm.group(1)
        seg = re.sub(r'<span id="t_\d+"[^>]*></span>', "", seg)
        text = _strip_tags(seg.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n"))
        out["content"] = text.strip()
        tm2 = re.match(r"^(第[一二三四五六七八九十百千零]+条(?:之[一二三四五六七八九十]+)?)", text.strip())
        if tm2:
            out["tiao"] = tm2.group(1)
    if not out["content"]:
        raise ValueError(f"命中法规但条文解析失败：{law}第{cn}条（响应结构可能已变化）")
    return out


# ── 法信大纲（概念知识树）─────────────────────────────
# 概念检索：POST /alllibsearch/GetKeyWordList.ashx → JSON {"keywordhtml": "..."}
#   注意：keywordhtml 里属性引号用 $ 占位（href=$...$），解析前需还原
# 概念详情：GET /keyword/KeyWordDetail.aspx?id=<id>（服务端直出）
#   结构：概念名+法信码+完整路径；分库计数 tab（法律/裁判规则/观点/图书/期刊）；
#   默认「法律」版块列出相关法条摘录（法规名+条号+摘录+文号日期+概念路径标签）


async def search_outline(ctx, keyword: str, max_results: int = 10) -> list[dict]:
    """检索法信大纲概念（法信码）。返回概念名、法信码、完整路径、概念id。"""
    body = await _http_post(
        ctx, "/alllibsearch/GetKeyWordList.ashx",
        {"keyword": keyword, "IsInResult": "0", "keyword_inResult": keyword,
         "usersearchtype": "1", "libsearchtype": ""},
    )
    data = json.loads(body)
    kh = (data.get("keywordhtml") or "").replace("$", '"')
    results = []
    for m in re.finditer(
        r'href="(/keyword/KeyWordDetail\.aspx\?id=(\d+)[^"]*)"\s+title="([^"]+)"[^>]*>'
        r'.*?<span class="fl num"\s*>([^<]+)</span>(.*?)</a>',
        kh, re.S,
    ):
        path, cid, code, name = m.group(3), m.group(2), m.group(4).strip(), _strip_tags(m.group(5))
        results.append({
            "id": cid,
            "code": code,
            "name": name,
            "path": re.sub(r"\s*>\s*", " > ", path).strip(),
            "url": f"{FAXIN_BASE}/keyword/KeyWordDetail.aspx?id={cid}",
        })
    # 去重（同 id 可能出现多次）
    seen, out = set(), []
    for r in results:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        out.append(r)
    return out[:max_results]


_OUTLINE_EXTRACT_JS = """() => {
    const out = {name: '', code: '', path: '', counts: {}, laws: []};
    // 概念名+法信码：.right_navation 内的 label，文本形如 "A2.E6买卖合同纠纷"
    const nav = document.querySelector('.right_navation');
    const labelEl = nav ? nav.querySelector('label') : null;
    const labelTxt = (labelEl ? labelEl.textContent : '').trim();
    const cm = labelTxt.match(/^([A-Z]\\d+(?:\\.[A-Z]+\\d+)*)([\\s\\S]*)$/);
    if (cm) { out.code = cm[1]; out.name = cm[2].trim(); }
    // 大纲路径：nav 区域内含「>」的文本行
    if (nav) {
        const lines = (nav.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
        const pathLine = lines.filter(l => l.includes('>') && !l.includes('[')).pop() || '';
        out.path = pathLine.replace(/\\s*·+\\s*>\\s*·+\\s*/g, ' > ').replace(/\\s*>\\s*/g, ' > ').trim();
    }
    // 分库计数：li.navation_item，文本形如「法 律 99+」「裁判规则 99+」
    document.querySelectorAll('li.navation_item').forEach(li => {
        const m = (li.textContent || '').trim().match(/^([\\S\\s]*?)\\s*([0-9]+\\+?)$/);
        if (m) {
            const key = m[1].replace(/\\s+/g, '');
            if (key) out.counts[key] = m[2];
        }
    });
    // 法条摘录条目
    document.querySelectorAll('.content_item').forEach(item => {
        const a = item.querySelector('a[href*="/lib/"]');
        const txt = (item.innerText || '').trim();
        if (!txt) return;
        const lines = txt.split('\\n').map(s => s.trim()).filter(Boolean);
        out.laws.push({
            law: a ? a.innerText.trim() : (lines[0] || ''),
            url: a ? a.href : '',
            excerpt: lines.slice(1).join(' ').slice(0, 300),
        });
    });
    return out;
}"""


async def get_outline(ctx, concept_id: str) -> dict:
    """取概念详情：名称/法信码/路径/分库计数/相关法条摘录。"""
    cid = re.sub(r"\D", "", str(concept_id))
    if not cid:
        raise ValueError("概念 id 必须是数字（由 faxin_search_outline 返回）")
    _, body = await _http_get(ctx, f"{FAXIN_BASE}/keyword/KeyWordDetail.aspx?id={cid}")
    page = await _get_parse_page(ctx)
    await page.set_content(body, wait_until="domcontentloaded")
    data = await page.evaluate(_OUTLINE_EXTRACT_JS)
    data["id"] = cid
    data["url"] = f"{FAXIN_BASE}/keyword/KeyWordDetail.aspx?id={cid}"
    return data


# ── 详情实现 ──────────────────────────────────────────
# gid 前缀 → 详情页路由
GID_ROUTES = {
    "A": "/lib/zyfl/zyflcontent.aspx?gid=",          # 中央法规
    "B": "/lib/dffl/DfflContent.aspx?gid=",          # 地方法规
    "C": "/lib/cpal/AlyzContent.aspx?isAlyz=1&gid=", # 裁判规则
    "D": "/lib/syyl/SyylContent.aspx?gid=",          # 法律释义
    "F": "/lib/flwx/FlqkContent.aspx?gid=",          # 法学期刊
    "G": "/lib/lfsf/LfContent.aspx?gid=",            # 立法资料
    "H": "/lib/lfsf/sfContent.aspx?gid=",            # 司法文件
    "J": "/lib/xgk/HKflContent.aspx?gid=",           # 香港法律
    "K": "/lib/amk/MacflContent.aspx?gid=",          # 澳门法律
    "M": "/lib/gjty/GjtyContent.aspx?gid=",          # 国家条约
}


def gid_to_detail_path(gid_or_url: str) -> str:
    """gid 前缀路由到详情页路径（见 GID_ROUTES）。"""
    s = gid_or_url.strip()
    if s.startswith("http"):
        return s
    if s.startswith("/lib"):
        return FAXIN_BASE + s
    prefix = s[0].upper() if s else ""
    route = GID_ROUTES.get(prefix)
    if route:
        return FAXIN_BASE + route + s
    # 未知前缀默认按中央法规试
    return f"{FAXIN_BASE}/lib/zyfl/zyflcontent.aspx?gid={s}"


# ── 详情解析（set_content + DOM 查询，不导航）─────────
_parse_page = None


async def _get_parse_page(ctx):
    """复用一个空白页做 DOM 解析（set_content 本地解析，无网络导航）。"""
    global _parse_page
    if _parse_page and not _parse_page.is_closed():
        return _parse_page
    _parse_page = await ctx.new_page()
    return _parse_page


_DETAIL_EXTRACT_JS = """() => {
    const out = {title: '', meta: {}, content: ''};
    // 标题：<title> 去掉「 - 法信 - ...」后缀
    out.title = (document.title || '').replace(/\\s*-\\s*法信.*$/, '').trim();
    // 正文：法规 .box.fulltext；裁判规则 .content；退化 body
    const cands = ['.box.fulltext', '.content', '#content', '.article-content'];
    for (const s of cands) {
        const el = document.querySelector(s);
        if (el && el.innerText.trim().length > 100) {
            out.content = el.innerText.trim();
            break;
        }
    }
    if (!out.content) {
        let t = document.body ? document.body.innerText : '';
        const stop = t.indexOf('法信简介');
        out.content = (stop > 100 ? t.slice(0, stop) : t).trim();
    }
    // meta：法规详情页的文档属性在「属性标签」弹框 .js-sxTab 内（默认隐藏但服务端直出）。
    // 结构为键值相邻行：制定机关/最高人民法院/文号/法释〔2025〕1号/效力等级/…/时效性/现行有效
    const KEYS = ['制定机关','文号','发文字号','效力等级','效力级别','公布日期','施行日期','时效性','主题分类','法律类别'];
    const tab = document.querySelector('.js-sxTab');
    if (tab) {
        // 弹框默认 display:none，innerText 取不到隐藏内容，必须用 textContent
        const raw = (tab.textContent || '').replace(/[ \\t\\r]+/g, ' ');
        const lines = raw.split('\\n').map(s => s.trim()).filter(Boolean);
        for (let i = 0; i < lines.length - 1; i++) {
            if (KEYS.includes(lines[i]) && !out.meta[lines[i]]) {
                const v = lines[i + 1];
                if (!KEYS.includes(v)) out.meta[lines[i]] = v;
            }
        }
    }
    return out;
}"""


async def parse_detail_html(ctx, body: str) -> dict:
    """把服务端直出的详情页 HTML 装进空白页做 DOM 解析。"""
    page = await _get_parse_page(ctx)
    await page.set_content(body, wait_until="domcontentloaded")
    data = await page.evaluate(_DETAIL_EXTRACT_JS)
    if not data.get("title") and data.get("content"):
        data["title"] = data["content"].split("\n", 1)[0][:60]
    return {
        "title": data.get("title") or "(无标题)",
        "meta": data.get("meta") or {},
        "content": (data.get("content") or "").strip(),
    }


# 中文数字转换（支持到 9999，覆盖民法典 1260 条）
_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def cn_to_int(s: str) -> int | None:
    """中文数字转整数。标准位值累加：十/百/千 只乘当前 number 后累加进 section。

    二百三十二 = 200+30+2 = 232；一千二百六十 = 1000+200+60 = 1260。
    """
    s = s.strip()
    if s.isdigit():
        return int(s)
    if not s:
        return None
    section, number = 0, 0
    for ch in s:
        if ch in _CN_DIGIT:
            number = _CN_DIGIT[ch]
        elif ch == "十":
            section += (number or 1) * 10
            number = 0
        elif ch == "百":
            section += number * 100
            number = 0
        elif ch == "千":
            section += number * 1000
            number = 0
        else:
            return None
    return section + number


_TIAO_RE = re.compile(r"第([一二三四五六七八九十百千零0-9]+)条(之[一二三四五六七八九十]+)?")
_CN_UNIT = [(1000, "千"), (100, "百"), (10, "十")]
_CN_NUM = "零一二三四五六七八九"


def int_to_cn(n: int) -> str:
    """整数转中文数字（1-9999，覆盖法条号场景）：577→五百七十七，1260→一千二百六十"""
    if n <= 10:
        return _CN_NUM[n] if n < 10 else "十"
    parts, rem = [], n
    for unit, name in _CN_UNIT:
        if rem >= unit:
            d, rem = divmod(rem, unit)
            parts.append(_CN_NUM[d] + name)
    if rem:
        parts.append(_CN_NUM[rem])
    s = "".join(parts)
    # 口语化规则：1X 开头读「十X」而非「一十X」
    if s.startswith("一十"):
        s = s[1:]
    return s


def extract_tiao(content: str, tiao: str) -> str | None:
    """从法条全文中定位「第X条」（支持 577 / 第五百七十七条 / 之一）。

    法条正文使用中文数字，阿拉伯输入统一转中文后再匹配。
    """
    tiao = tiao.strip().lstrip("第")
    m = re.match(r"^([一二三四五六七八九十百千零0-9]+)条?(之[一二三四五六七八九十]+)?$", tiao)
    if not m:
        return None
    num = cn_to_int(m.group(1))
    if num is None:
        return None
    suffix = m.group(2) or ""
    cn_num = int_to_cn(num)
    # 同时尝试中文数字形与原始输入形（个别文件条文号用阿拉伯数字）
    forms = {cn_num, m.group(1)}
    for form in forms:
        target_re = re.compile(r"第" + form + r"条" + (suffix if suffix else r"(?!之)"))
        match = target_re.search(content)
        if match:
            start = match.start()
            nxt = _TIAO_RE.search(content, match.end())
            end = nxt.start() if nxt else min(len(content), start + 2000)
            return content[start:end].strip()
    return None


async def get_detail(ctx, gid: str, tiao: str = "") -> dict:
    url = gid_to_detail_path(gid)
    final_url, body = await _http_get(ctx, url)
    if "页面不存在" in body[:2000]:
        raise ValueError(f"详情页不存在或 gid 路由错误：{url}")
    parsed = await parse_detail_html(ctx, body)
    content = parsed["content"]
    # 清理裁判规则页的面包屑导航（「首页 > 裁判规则 > … > 正文」前缀）
    if content.startswith("首页"):
        lines = content.split("\n")
        for i, ln in enumerate(lines[:8]):
            if ln.strip() == "正文":
                content = "\n".join(lines[i + 1:]).strip()
                break
    truncated = False
    tiao_note = ""
    if tiao:
        seg = extract_tiao(content, tiao)
        if seg:
            content = seg
            _t = tiao.strip().lstrip("第").rstrip("条")
            tiao_note = f"已定位「第{_t}条」"
        else:
            tiao_note = f"⚠️ 未能在正文中定位「第{tiao}条」，返回全文开头（请人工核对条号写法）"
            content = content[:8000]
            truncated = len(parsed["content"]) > 8000
    else:
        if len(content) > 20000:
            content = content[:20000]
            truncated = True
    return {
        "title": parsed["title"],
        "url": final_url,
        "meta": parsed["meta"],
        "content": content,
        "truncated": truncated,
        "tiao_note": tiao_note,
    }


# ── 输出格式化 ────────────────────────────────────────
def format_results(keyword: str, kind: str, data: dict) -> str:
    results = data["results"]
    counts = data.get("counts", {})
    out = f"## 法信{kind}检索：{keyword}\n\n"
    if counts:
        label = {"zyfl": "中央法规", "dffl": "地方法规", "cpws_al": "裁判规则"}
        out += "分库命中：" + "、".join(f"{label.get(k, k)} {v} 条" for k, v in counts.items()) + "\n\n"
    if data.get("timeliness_applied"):
        out += f"时效筛选：{data['timeliness_applied']}\n\n"
    if data.get("permission_denied"):
        out += f"🔒 **{data.get('lib_name', kind)}库访问受限**\n\n{data['message']}\n"
        return out
    if not results:
        out += ("未找到相关结果。建议：①换用案由词/法条原文表述（如「买卖合同」而非「违约」）；"
                "②确认关键词无错别字；③放宽时效筛选。\n")
        return out
    if data["raw_count"] > len(results):
        out += f"共抓取 {data['raw_count']} 条，以下为前 {len(results)} 条\n\n"
    for i, r in enumerate(results, 1):
        out += f"### {i}. {r['title']}\n"
        out += f"gid：{r['gid']}（get_detail 用）\n"
        if r.get("lib"):
            out += f"库别：{r['lib']}"
            if r.get("group"):
                out += f"｜分组：{r['group']}"
            out += "\n"
        date_info = "｜".join(f"{k}：{v}" for k, v in (("公布", r.get("公布")), ("施行", r.get("施行"))) if v)
        if date_info:
            out += f"{date_info}\n"
        if r.get("snippet"):
            out += f"摘要：{r['snippet']}\n"
        out += "\n"
    return out


# ── MCP 工具定义 ──────────────────────────────────────
@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="faxin_check_login",
            description="检查法信登录态。结果缓存30分钟，force=true 强制重检。",
            inputSchema={
                "type": "object",
                "properties": {
                    "force": {"type": "boolean", "description": "强制重新检测（默认 false，用30分钟缓存）"},
                },
            },
        ),
        Tool(
            name="faxin_search_law",
            description=(
                "在法信检索法律法规（中央+地方），返回标题、gid、公布/施行日期、分库命中数。"
                "【关键词策略】优先用法条原文表述或法规名（如「盗窃公私财物」「民法典」），"
                "避免抽象法律概念（如「盗窃罪」「违约责任」命中率低）。"
                "【效力审计】查现行法务必传 timeliness=current，并核对返回的施行日期。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "检索关键词（法规名/条文原文表述）"},
                    "timeliness": {
                        "type": "string",
                        "enum": ["", "current", "invalid", "modified", "pending"],
                        "description": "时效筛选：current=现行有效 invalid=失效 modified=已被修改 pending=尚未生效；默认不过滤",
                    },
                    "lib": {
                        "type": "string",
                        "enum": ["all", "central", "local"],
                        "description": "库别：central=中央法规 local=地方法规 all=两者（默认）",
                    },
                    "max_results": {"type": "integer", "description": "返回条数上限（默认20，最大50）"},
                },
                "required": ["keyword"],
            },
        ),
        Tool(
            name="faxin_search_case",
            description=(
                "在法信检索裁判规则（案例要旨），返回标题、gid、摘要。"
                "【关键词策略】优先用案由词（如「买卖合同纠纷」「股权转让纠纷」），"
                "避免抽象法律概念。"
                "【注意】本工具走 Alyz 接口，真正按关键词过滤；"
                "平台另有 GeneralCaseSearch 直检页会忽略关键词返回全库默认，本工具已规避。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "检索关键词（案由词优先）"},
                    "max_results": {"type": "integer", "description": "返回条数上限（默认20，最大30——裁判规则库大，响应较慢，低频使用）"},
                },
                "required": ["keyword"],
            },
        ),
        Tool(
            name="faxin_search_lib",
            description=(
                "检索法信专题库。lib 取值："
                "gjty=国家条约（海牙公约等，涉外必备）、"
                "xgk=香港法律（条例原文，如《仲裁条例》）、"
                "amk=澳门法律（终审法院统一司法见解等）、"
                "flqk=法学期刊（学术论文）、"
                "flsy=法律释义（法条理解问答）、"
                "lf=立法资料（草案/示范文本/征求意见稿）、"
                "sf=司法文件（法院发布的典型案例/文件）。"
                "关键词策略同 search_law：原文表述/案由词优先。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "检索关键词"},
                    "lib": {
                        "type": "string",
                        "enum": ["gjty", "xgk", "amk", "flqk", "flsy", "lf", "sf"],
                        "description": "专题库代码",
                    },
                    "max_results": {"type": "integer", "description": "返回条数上限（默认20，最大30）"},
                },
                "required": ["keyword", "lib"],
            },
        ),
        Tool(
            name="faxin_search_lalei",
            description=(
                "类案检索（法信权威案例库 fxal：指导性案例/公报案例/公布案例，按推荐度排序）。"
                "keyword 走案例标题匹配（案由词精度最高，如「买卖合同纠纷」）；"
                "筛选项：casetype（民事/刑事/行政/执行）、casecause（案由）、referencetype"
                "（最高人民法院指导性案例/公报案例等）、province、courtlevel、judgeyear、procedure。"
                "返回 total + 案例列表（标题/案由/程序/法院/年份/三维推荐度/uniqid）。"
                "uniqid 可直接传给 faxin_get_detail 取详情。"
                "【与 search_case 的分工】search_case 查裁判规则要旨（编辑提炼），本工具查权威案例本体（可溯源到具体案件）。"
                "【主站限制】法信主站（www.faxin.cn）类案 token 机制不同，本工具在主站暂不可用。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "案例标题关键词（案由词优先，如「买卖合同纠纷」）"},
                    "casetype": {"type": "string", "description": "案件类型：民事/刑事/行政/执行"},
                    "casecause": {"type": "string", "description": "案由（如「房屋买卖合同纠纷」）"},
                    "referencetype": {"type": "string", "description": "参考类型：如「最高人民法院指导性案例」「最高人民法院公报案例」"},
                    "province": {"type": "string", "description": "省份（如「上海市」）"},
                    "courtlevel": {"type": "string", "description": "法院级别（如「最高人民法院」「高级法院」）"},
                    "judgeyear": {"type": "string", "description": "裁判年份（如「2023」）"},
                    "procedure": {"type": "string", "description": "审理程序（如「二审」「再审」）"},
                    "size": {"type": "integer", "description": "返回条数（默认10，最大30）"},
                },
            },
        ),
        Tool(
            name="faxin_get_tiao",
            description=(
                "【取法条最快通道】法规名+条号直达条文全文，无需先检索。"
                "law 支持简称（如「民法典」「刑法」「劳动合同法」），tiao 支持阿拉伯或中文数字"
                "（如 680 / 第六百八十条 / 第五百七十七条之一）。"
                "返回法规全称、文号、公布/施行日期、条文主旨、条文全文、gid+锚点链接。"
                "已知法规名和条号时一律用它，不要用 search_law 翻。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "law": {"type": "string", "description": "法规名（可简称，如「民法典」「刑法」「劳动合同法」）"},
                    "tiao": {"type": "string", "description": "条号（如 \"680\"、\"第六百八十条\"、\"第五百七十七条之一\"）"},
                },
                "required": ["law", "tiao"],
            },
        ),
        Tool(
            name="faxin_search_outline",
            description=(
                "检索法信大纲概念（法信码知识树）。输入法律概念或业务场景词，"
                "返回规范概念名、法信码、大纲路径（上下位概念）、概念id。"
                "【适用场景】不知道用什么关键词检索时先做概念发现；"
                "或需要概念的上下位体系（如「买卖合同纠纷」的上位是「合同」，下位含「房屋买卖合同纠纷」）。"
                "拿到 id 后用 faxin_get_outline 取该概念聚合的法条/裁判规则/观点/图书/期刊。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "法律概念或场景词，如「买卖合同」「违约金」「人格权」"},
                    "max_results": {"type": "integer", "description": "返回概念数上限（默认10）"},
                },
                "required": ["keyword"],
            },
        ),
        Tool(
            name="faxin_get_outline",
            description=(
                "取法信大纲概念详情（知识地图）：概念名、法信码、完整路径、"
                "各库聚合数量（法律/裁判规则/观点/图书/期刊）、相关法条摘录（法规名+条号+条文）。"
                "id 由 faxin_search_outline 返回。"
                "【工作流】概念发现 → 读本概念的法条摘录 → 需要深挖的库再用 search_law/search_case/search_lib 带概念词检索。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "概念 id（数字，由 faxin_search_outline 返回）"},
                },
                "required": ["id"],
            },
        ),
        Tool(
            name="faxin_get_detail",
            description=(
                "获取法规/裁判规则/专题库文档详情全文。传入 gid 或完整URL。"
                "gid 前缀路由：A=中央法规 B=地方法规 C=裁判规则 D=法律释义 F=法学期刊 "
                "G=立法资料 H=司法文件 J=香港法律 K=澳门法律 M=国家条约。"
                "法规可传 tiao 参数精确取条（如 tiao=\"577\" 或 \"第五百七十七条\"）。"
                "返回含时效性/效力位阶等 meta——引用法条前必须核对 meta 中的时效性。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "gid": {"type": "string", "description": "文档 gid（前缀自动路由）或详情页URL"},
                    "tiao": {"type": "string", "description": "可选：只取第X条（如 \"577\"、\"第五百七十七条\"、\"第五百七十七条之一\"）"},
                },
                "required": ["gid"],
            },
        ),
    ]


async def _guarded(ctx):
    """登录守卫：返回 None 表示已登录；否则返回错误内容列表。"""
    ok, diag = await ensure_login(ctx)
    if not ok:
        logger.warning(f"登录态校验未通过：{diag}")
        return [TextContent(type="text", text=f"{_LOGIN_HINT}\n（诊断：{diag}）")]
    return None


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    logger.info(f"调用工具: {name}, 参数: {arguments}")
    global _login_ok_at
    try:
        ctx = await get_context()

        if name == "faxin_check_login":
            ok, diag = await ensure_login(ctx, force=bool(arguments.get("force")))
            if ok:
                return [TextContent(type="text", text=f"✅ 法信已登录，可以正常检索。（{diag}）")]
            return [TextContent(type="text", text=f"{_LOGIN_HINT}\n（诊断：{diag}）")]

        elif name == "faxin_search_law":
            keyword = (arguments.get("keyword") or "").strip()
            if not keyword:
                return [TextContent(type="text", text="⚠️ 请提供 keyword 参数。")]
            err = await _guarded(ctx)
            if err:
                return err
            data = await search_law(
                ctx, keyword,
                timeliness=arguments.get("timeliness", ""),
                lib=arguments.get("lib", "all"),
                max_results=min(int(arguments.get("max_results") or MAX_RESULTS), 50),
            )
            return [TextContent(type="text", text=format_results(keyword, "法规", data))]

        elif name == "faxin_search_case":
            keyword = (arguments.get("keyword") or "").strip()
            if not keyword:
                return [TextContent(type="text", text="⚠️ 请提供 keyword 参数。")]
            err = await _guarded(ctx)
            if err:
                return err
            data = await search_cases(
                ctx, keyword,
                max_results=min(int(arguments.get("max_results") or MAX_RESULTS), 30),
            )
            return [TextContent(type="text", text=format_results(keyword, "裁判规则", data))]

        elif name == "faxin_search_lib":
            keyword = (arguments.get("keyword") or "").strip()
            lib = (arguments.get("lib") or "").strip()
            if not keyword or lib not in EXTRA_LIBS:
                return [TextContent(type="text", text=f"⚠️ 请提供 keyword 和合法的 lib（{ '/'.join(EXTRA_LIBS) }）。")]
            err = await _guarded(ctx)
            if err:
                return err
            data = await search_lib(
                ctx, keyword, lib,
                max_results=min(int(arguments.get("max_results") or MAX_RESULTS), 30),
            )
            return [TextContent(type="text", text=format_results(keyword, data["lib_name"], data))]

        elif name == "faxin_search_lalei":
            err = await _guarded(ctx)
            if err:
                return err
            data = await search_lalei(
                ctx,
                keyword=arguments.get("keyword", ""),
                casetype=arguments.get("casetype", ""),
                casecause=arguments.get("casecause", ""),
                referencetype=arguments.get("referencetype", ""),
                province=arguments.get("province", ""),
                courtlevel=arguments.get("courtlevel", ""),
                judgeyear=arguments.get("judgeyear", ""),
                procedure=arguments.get("procedure", ""),
                size=min(int(arguments.get("size") or 10), 30),
            )
            out = f"## 类案检索"
            kw = arguments.get("keyword", "")
            if kw:
                out += f"：{kw}"
            out += f"\n\n共 {data['total']} 条（按推荐度排序，以下为前 {len(data['docs'])} 条）\n\n"
            if not data["docs"]:
                out += "未找到。建议：①换案由词；②放宽筛选；③检查关键词是否过窄。\n"
            for i, d in enumerate(data["docs"], 1):
                out += f"### {i}. {d['title']}\n"
                meta = "｜".join(x for x in [
                    d["referencetype"], d["court"],
                    f"{d['judgeyear']}年" if d["judgeyear"] else "",
                    d["procedure"], d["casecause"]] if x)
                out += f"{meta}\n"
                out += f"推荐度：{d['tuijiandu']}｜uniqid：{d['uniqid']}（get_detail 取详情）\n\n"
            return [TextContent(type="text", text=out)]

        elif name == "faxin_get_tiao":
            law = (arguments.get("law") or "").strip()
            tiao = (arguments.get("tiao") or "").strip()
            if not law or not tiao:
                return [TextContent(type="text", text="⚠️ 请提供 law（法规名）和 tiao（条号）参数。")]
            err = await _guarded(ctx)
            if err:
                return err
            d = await get_tiao(ctx, law, tiao)
            out = f"## {d['law']} {d['tiao']}\n\n"
            if d["doc_no_dates"]:
                out += f"{d['doc_no_dates']}\n\n"
            out += f"{d['content']}\n\n"
            if d["anchor_url"]:
                out += f"来源：{d['anchor_url']}\n"
            out += "_引用前如需核验时效性，可用 gid 调 faxin_get_detail 查看完整 meta。_\n"
            return [TextContent(type="text", text=out)]

        elif name == "faxin_search_outline":
            keyword = (arguments.get("keyword") or "").strip()
            if not keyword:
                return [TextContent(type="text", text="⚠️ 请提供 keyword 参数。")]
            err = await _guarded(ctx)
            if err:
                return err
            concepts = await search_outline(
                ctx, keyword, max_results=int(arguments.get("max_results") or 10))
            if not concepts:
                return [TextContent(type="text", text=(
                    f"大纲中未找到「{keyword}」相关概念。建议换更上位的概念词"
                    "（如「合同」而非「二手设备买卖」），或直接用 search_law/search_case 检索。"))]
            out = f"## 法信大纲概念：{keyword}\n\n共 {len(concepts)} 个相关概念\n\n"
            for i, c in enumerate(concepts, 1):
                out += f"### {i}. {c['name']}（法信码 {c['code']}）\n"
                out += f"路径：{c['path']}\n"
                out += f"概念id：{c['id']}（get_outline 用）\n\n"
            return [TextContent(type="text", text=out)]

        elif name == "faxin_get_outline":
            cid = (arguments.get("id") or "").strip()
            if not cid:
                return [TextContent(type="text", text="⚠️ 请提供 id 参数（由 faxin_search_outline 返回）。")]
            err = await _guarded(ctx)
            if err:
                return err
            d = await get_outline(ctx, cid)
            out = f"## {d['name']}（法信码 {d['code']}）\n\n"
            if d.get("path"):
                out += f"大纲路径：{d['path']}\n\n"
            if d.get("counts"):
                out += "聚合资源：" + "、".join(f"{k} {v}" for k, v in d["counts"].items()) + "\n\n"
            if d.get("laws"):
                out += f"### 相关法条（前 {len(d['laws'])} 条摘录）\n\n"
                for i, law in enumerate(d["laws"], 1):
                    out += f"{i}. **{law['law']}**\n   {law['excerpt']}\n\n"
            out += ("_提示：需深挖某类资源时，用概念名作为关键词调 "
                    "search_law / search_case / search_lib 继续检索。_\n")
            return [TextContent(type="text", text=out)]

        elif name == "faxin_get_detail":
            gid = (arguments.get("gid") or "").strip()
            if not gid:
                return [TextContent(type="text", text="⚠️ 请提供 gid 参数。")]
            err = await _guarded(ctx)
            if err:
                return err
            d = await get_detail(ctx, gid, tiao=arguments.get("tiao", ""))
            out = f"## {d['title']}\n\n链接：{d['url']}\n\n"
            if d["meta"]:
                out += "### 基本信息（引用前请核对时效性）\n"
                for k, v in d["meta"].items():
                    out += f"- {k}：{v}\n"
                out += "\n"
            if d["tiao_note"]:
                out += f"_{d['tiao_note']}_\n\n"
            out += f"### 正文\n\n{d['content']}\n"
            if d["truncated"]:
                out += "\n_（正文过长已截断；法规类建议用 tiao 参数按条取）_\n"
            return [TextContent(type="text", text=out)]

        return [TextContent(type="text", text=f"未知工具：{name}")]

    except SessionExpired as e:
        _login_ok_at = 0  # 会话失效，清缓存强制下次重检
        logger.warning(f"会话失效：{e}")
        return [TextContent(type="text", text=f"{_LOGIN_HINT}\n（诊断：{e}）")]
    except Exception as e:
        logger.error(f"工具调用失败: {e}", exc_info=True)
        return [TextContent(type="text", text=f"执行失败：{str(e)}")]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
