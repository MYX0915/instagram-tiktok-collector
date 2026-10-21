"""TikTok 账号采集：默认获取全部可访问普通作品，每个作品独立保存。"""

import argparse
import asyncio
import json
import math
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

ROOT = Path(__file__).resolve().parent
COMMON_PARAMS = {
    "WebIdLastTime", "aid", "app_language", "app_name", "browser_language",
    "browser_name", "browser_online", "browser_platform", "browser_version",
    "channel", "cookie_enabled", "device_id", "device_platform", "odinId", "os",
    "priority_region", "region", "root_referer", "screen_height", "screen_width",
    "tz_name", "user_is_login", "verifyFp", "webcast_language", "language",
}
API_PATHS = {"/api/post/item_list/", "/api/comment/list/",
             "/api/comment/list/reply/", "/api/user/detail/"}
WEB_APP_PARAMS = {"aid": "1988", "app_name": "tiktok_web",
                  "device_platform": "web_pc", "channel": "tiktok_web"}
FETCH = """async ({path, params}) => {
    const url = new URL(path, location.origin);
    const defaults = {
        aid: '1988', app_name: 'tiktok_web', device_platform: 'web_pc',
        channel: 'tiktok_web', app_language: 'en',
        browser_language: navigator.language, browser_online: String(navigator.onLine),
        cookie_enabled: String(navigator.cookieEnabled),
        screen_width: String(screen.width), screen_height: String(screen.height),
        tz_name: Intl.DateTimeFormat().resolvedOptions().timeZone
    };
    for (const [key, value] of Object.entries({...defaults, ...params})) {
        url.searchParams.set(key, String(value));
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 30000);
    try {
        // 正常调用当前网页 fetch，不自行生成签名或绕过验证。
        const response = await fetch(url.toString(), {
            credentials: 'same-origin', signal: controller.signal
        });
        return {status: response.status, redirected: response.redirected,
                contentType: response.headers.get('content-type'),
                retryAfter: response.headers.get('retry-after'),
                text: await response.text()};
    } finally { clearTimeout(timer); }
}"""


class CollectionError(RuntimeError):
    pass


class TransientRequestError(CollectionError):
    def __init__(self, message, retry_after=0):
        super().__init__(message)
        self.retry_after = retry_after


def retry_after_seconds(value):
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return 0
    return max(0, seconds) if math.isfinite(seconds) else 0


def is_transient_error(error):
    if isinstance(error, TransientRequestError):
        return True
    if not type(error).__module__.startswith("playwright."):
        return False
    return type(error).__name__ == "TimeoutError" or any(
        marker in str(error) for marker in ("net::ERR_", "Failed to fetch", "NetworkError", "AbortError"))


class BusinessError(CollectionError):
    def __init__(self, message, codes):
        super().__init__(message)
        self.codes = codes


def now():
    return datetime.now(timezone.utc).isoformat()


class ProfileScriptParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.fragments = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("id") == "__UNIVERSAL_DATA_FOR_REHYDRATION__":
            self.active = True

    def handle_data(self, data):
        if self.active:
            self.fragments.append(data)

    def handle_endtag(self, tag):
        if tag == "script":
            self.active = False


def profile_from_html(text, expected_name):
    parser = ProfileScriptParser()
    parser.feed(text)
    try:
        payload = json.loads("".join(parser.fragments))
        info = payload.get("__DEFAULT_SCOPE__", {}).get("webapp.user-detail", {}).get("userInfo")
    except (ValueError, AttributeError):
        raise CollectionError("HTTP 主页未包含可解析的用户资料节点") from None
    if not isinstance(info, dict):
        raise CollectionError("HTTP 主页缺少 userInfo，可能不可访问或需要人工验证")
    profile_info(info, expected_name=expected_name)
    return info


def parse_link(link):
    parsed = urlparse(link)
    match = re.fullmatch(r"/@([A-Za-z0-9_.]{1,32})/?", parsed.path)
    if (parsed.scheme != "https" or parsed.netloc not in ("www.tiktok.com", "tiktok.com")
            or not match or match.group(1) in (".", "..")):
        raise ValueError("请输入 https://www.tiktok.com/@用户名 形式的账号主页链接")
    return match.group(1)


def decode_response(result):
    if not result.get("redirected") and result["status"] in (429, 500, 502, 503, 504):
        raise TransientRequestError(f"HTTP {result['status']}，未取得有效数据",
                                    retry_after_seconds(result.get("retryAfter")))
    if result["status"] != 200 or result.get("redirected"):
        raise CollectionError(f"HTTP {result['status']} 或请求跳转，未继续请求")
    if not result["text"].strip():
        raise TransientRequestError("HTTP 200 但响应为空；未取得有效数据")
    try:
        data = json.loads(result["text"])
    except ValueError:
        raise CollectionError("接口未返回 JSON；未输出可能含凭据的原文") from None
    if not isinstance(data, dict):
        raise CollectionError("接口响应不是 JSON 对象")
    codes = [data[k] for k in ("statusCode", "status_code") if k in data]
    if not codes or any(str(code) != "0" for code in codes):
        # 仅记录数字状态码，绝不输出接口原文、令牌或任意服务端文本。
        values = {k: str(data[k]) if re.fullmatch(r"-?\d{1,12}", str(data[k])) else "非数字"
                  for k in ("statusCode", "status_code") if k in data}
        raise BusinessError(f"接口业务状态失败：{values or '缺少状态字段'}，未继续请求", values)
    return data


def profile_info(info, expected_id=None, expected_name=None):
    user = info.get("user") or {}
    uid = str(user.get("id") or user.get("uid") or "")
    name = user.get("uniqueId") or user.get("unique_id")
    if not uid or (expected_id and uid != str(expected_id)):
        raise CollectionError("用户资料 ID 与目标不符或缺失")
    if expected_name and (not name or name.casefold() != expected_name.casefold()):
        raise CollectionError("用户资料用户名与目标不符")
    stats = info.get("stats") or {}
    avatar = user.get("avatarThumb") or user.get("avatar_thumb")
    if isinstance(avatar, dict):
        avatar = (avatar.get("url_list") or [None])[0]
    return {
        "用户ID": uid, "用户名": name, "用户secUid": user.get("secUid") or user.get("sec_uid"),
        "昵称": user.get("nickname"), "简介": user.get("signature"),
        "是否认证": user.get("verified"), "是否私密账号": user.get("privateAccount"),
        "头像地址": avatar, "大头像地址": user.get("avatarLarger"),
        "中头像地址": user.get("avatarMedium"), "主页链接": f"https://www.tiktok.com/@{name}" if name else None,
        "粉丝数": stats.get("followerCount"), "关注数": stats.get("followingCount"),
        "获赞数": stats.get("heartCount"), "作品数": stats.get("videoCount"),
        "好友计数": stats.get("friendCount"),
    }


def is_root_comment_echo(node, work_id, parent):
    """回复页回显当前根评论时排除自身，不放宽其他回复的归属校验。"""
    return (parent is not None and str(node.get("cid")) == parent
            and str(node.get("aweme_id")) == work_id
            and str(node.get("reply_id")) == work_id
            and node.get("reply_to_reply_id") in (None, "0", 0, ""))


def comment_info(node, work_id, parent=None):
    if str(node.get("aweme_id")) != work_id or not node.get("cid"):
        raise CollectionError("评论作品 ID 不匹配或评论 ID 缺失")
    root = str(node.get("reply_id", "0"))
    if parent is not None and root != parent:
        raise CollectionError("回复根评论 ID 与请求不符")
    if parent is None and root not in ("0", "None", ""):
        raise CollectionError("顶层列表包含未关联的回复")
    target = node.get("reply_to_reply_id")
    return {
        "评论ID": str(node["cid"]), "作品ID": work_id, "评论内容": node.get("text"),
        "评论发布时间": node.get("create_time"), "评论点赞数": node.get("digg_count"),
        "回复数": node.get("reply_comment_total"), "父评论ID": parent,
        "直接回复目标ID": str(target) if target not in (None, "0", 0, "") else None,
        "是否作者点赞": node.get("is_author_digged"), "是否置顶": node.get("author_pin"),
        "评论者信息": profile_info({"user": node.get("user") or {}}),
        "回复列表": [],
    }


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8")
    temp.chmod(0o600)
    temp.replace(path)


def safe_error(error):
    return str(error) if isinstance(error, CollectionError) else f"{type(error).__name__}：请求或解析失败，未输出原文"


def response_excerpt(body, limit=1200):
    """提取错误页可读文本；不执行 HTML，脱敏后再截断。"""
    text = unescape(body)
    text = re.sub(r"(?is)<(script|style)\b.*?(?:</\1\s*>|$)", " ", text)
    text = re.sub(r"(?s)<!--.*?(?:-->|$)", " ", text)
    text = re.sub(r"<[^>]*>", " ", text)
    text = re.sub(r"https?://[^\s<>\"']+", "<URL已脱敏>", text, flags=re.I)
    text = re.sub(r"(?i)\b(Bearer|Basic)\s+[^\s,;]+", r"\1 <已脱敏>", text)
    keys = (r"cookie|set-cookie|authorization|sessionid(?:_ss)?|sid_tt|csrftoken|"
            r"mstoken|x-bogus|x-gnarly|x-dynosaur|access_token|token|signature|verifyfp|password")
    text = re.sub(rf"(?i)\b({keys})\b[\"']?\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s;,]+)",
                  r"\1=<已脱敏>", text)
    text = re.sub(r"[A-Za-z0-9_+/=-]{40,}", "<长标识已脱敏>", text)
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    text = " ".join(text.split())
    return text[:limit] + ("…<已截断>" if len(text) > limit else "")


def log_failed_exchange(path, params, page_url, result, error):
    """失败即打印可核对的请求与响应，白名单之外的值不进入终端。"""
    allowed = {"aid", "app_name", "device_platform", "channel", "uniqueId", "secUid",
               "aweme_id", "item_id", "comment_id", "cursor", "count", "from_page"}
    request = {"方法": "GET", "接口路径": path, "页面路径": urlparse(page_url).path,
               "业务参数": {k: v for k, v in params.items() if k in allowed},
               "凭据": "由浏览器同源发送，不输出 Cookie、令牌和签名"}
    print("失败请求：" + json.dumps(request, ensure_ascii=False))
    if result is None:
        print("失败响应：未取得响应；" + safe_error(error))
        return
    body = result["text"]
    summary = {"HTTP状态": result["status"], "响应字符数": len(body),
               "是否跳转": bool(result.get("redirected"))}
    summary["内容类型"] = response_excerpt(result.get("contentType") or "<未提供>", 200)
    if not body.strip():
        summary["响应正文"] = "<空正文>"
    else:
        try:
            data = json.loads(body)
        except ValueError:
            data = None
        if isinstance(data, dict):
            fields = {}
            for key in ("status_code", "statusCode", "status_msg"):
                if key in data:
                    value = str(data[key])
                    fields[key] = data[key] if not value or re.fullmatch(r"-?\d{1,12}", value) else "<非数字文本已省略>"
            summary["响应JSON摘要"] = fields
            summary["说明"] = "仅显示业务状态字段，其他字段未输出"
        else:
            summary["响应正文摘要"] = response_excerpt(body) or "<无可读文本>"
            summary["说明"] = "非 JSON 对象；HTML 已转为可读文本并脱敏，最多显示1200字符"
    print("失败响应：" + json.dumps(summary, ensure_ascii=False))


class BrowserClient:
    def __init__(self, page, delay=1.0, manual=False, max_manual_pauses=None, retry_delay=5.0):
        self.page, self.delay = page, delay
        self.retry_delay = retry_delay
        self.common = {}
        self.endpoint_common = {}
        self.stopped = False
        self.profile_failure = None
        self.diagnostics = {}
        self.initial_posts = {}
        self.initial_response = asyncio.Event()
        self.require_initial_capture = False
        self.last_source = None
        self.manual = manual
        self.max_manual_pauses = max_manual_pauses
        self.manual_pauses = 0
        self.checkpoint = None

    def observe(self, request):
        parsed = urlparse(request.url)
        if parsed.hostname == "www.tiktok.com" and parsed.path in API_PATHS:
            values = {k: v for k, v in parse_qsl(parsed.query) if k in COMMON_PARAMS}
            self.common.update(values)
            self.endpoint_common[parsed.path] = values

    async def observe_response(self, response):
        """只缓存当前页面自然产生的普通作品首批响应，不重放签名请求。"""
        parsed = urlparse(response.url)
        origin = urlparse(self.page.url)
        if (parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc):
            return
        if parsed.path != "/api/post/item_list/":
            return
        query = dict(parse_qsl(parsed.query))
        if query.get("cursor", "0") != "0" or not query.get("secUid"):
            return
        key = query["secUid"]
        try:
            text = await response.text()
            self.initial_posts[key] = {"status": response.status,
                                       "redirected": response.request.redirected_from is not None,
                                       "contentType": response.headers.get("content-type"),
                                       "retryAfter": response.headers.get("retry-after"),
                                       "text": text}
        except Exception:
            self.initial_posts[key] = {"status": response.status, "text": ""}
        finally:
            self.initial_response.set()

    async def wait_initial_posts(self, sec_uid, timeout=15):
        if sec_uid in self.initial_posts:
            return True
        try:
            await asyncio.wait_for(self.initial_response.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return sec_uid in self.initial_posts

    async def request(self, path, params):
        if self.stopped:
            raise CollectionError("此前请求失败，已停止网络采集；保留已有结果")
        if path == "/api/user/detail/" and self.profile_failure:
            raise CollectionError("本次用户资料接口已暂停；保留评论者摘要，其他评论采集继续")
        try:
            return await self.request_with_backoff(path, params)
        except Exception as error:
            exhausted = (self.max_manual_pauses is not None
                         and self.manual_pauses >= self.max_manual_pauses)
            if not self.manual or exhausted:
                self.record_failure(path, params, error)
                raise
            self.manual_pauses += 1
            print(f"接口异常 [{path}]：{safe_error(error)}")
            if isinstance(error, BusinessError) and "5" in error.codes.values():
                print("接口返回业务状态码 5，原因尚未确认；此状态码不代表页面已出现验证码。")
                print("若页面显示验证提示，请手动处理；没有提示时请检查对应作品的评论及回复请求。")
            else:
                print("请手动检查当前浏览器中的验证或登录问题。")
            if self.checkpoint:
                self.checkpoint()
            try:
                # 在线程中等待终端输入，让浏览器事件循环继续处理人工验证。
                await asyncio.to_thread(input, "采集已暂停；检查后按回车仅重试当前请求一次，或按 Ctrl+C 结束：")
            except Exception:
                self.stopped = True
                raise
            try:
                result = await self.request_once(path, params)
            except Exception as retry_error:
                print(f"重试失败 [{path}]：{safe_error(retry_error)}")
                self.record_failure(path, params, retry_error)
                raise
            print(f"重试成功 [{path}]，继续采集。")
            return result

    async def request_with_backoff(self, path, params):
        try:
            return await self.request_once(path, params)
        except Exception as error:
            passive_first_page = (self.require_initial_capture and path == "/api/post/item_list/"
                                  and str(params.get("cursor", "0")) == "0")
            if passive_first_page or not is_transient_error(error):
                raise
            delay = max(self.retry_delay, getattr(error, "retry_after", 0))
            print(f"临时请求异常 [{path}]：{safe_error(error)}；等待 {delay:g} 秒后自动重试一次。")
            if self.checkpoint:
                self.checkpoint()
            await asyncio.sleep(delay)
            try:
                result = await self.request_once(path, params)
            except Exception:
                print(f"自动重试失败 [{path}]，转入原有失败处理流程。")
                raise
            print(f"自动重试成功 [{path}]，继续采集。")
            return result

    def record_failure(self, path, params, error):
        if path == "/api/user/detail/":
            self.profile_failure = {
                "失败用户名": params.get("uniqueId"), "失败原因": safe_error(error),
                "请求诊断": dict(self.diagnostics.get(path, {}))}
            print("用户资料接口已暂停：本次不再请求其他用户详情；保留摘要并继续其他作品的评论采集。")
        else:
            self.stopped = True
            print(f"网络采集已停止 [{path}]：{safe_error(error)}")

    async def request_once(self, path, params):
        if path == "/api/post/item_list/" and str(params.get("cursor", "0")) == "0":
            sec_uid = params.get("secUid")
            if sec_uid in self.initial_posts:
                self.last_source = "主页自然响应，未重复请求"
                result = self.initial_posts.pop(sec_uid)
                try:
                    return decode_response(result)
                except Exception as error:
                    log_failed_exchange(path, params, self.page.url, result, error)
                    raise
            if self.require_initial_capture:
                raise CollectionError("未捕获目标作者的首批作品响应；未重复发送首批请求")
        self.last_source = "浏览器主动请求"
        effective = {**self.common, **self.endpoint_common.get(path, {}),
                     **params, **WEB_APP_PARAMS}
        diagnostic = {"接口路径": path, "页面路径": urlparse(self.page.url).path,
                      "用户名": params.get("uniqueId"), "应用ID": effective["aid"],
                      "请求参数名": sorted(effective)}
        self.diagnostics[path] = diagnostic
        await asyncio.sleep(self.delay)
        if path == "/api/user/detail/":
            self.last_source = "直接 HTTP 请求"
            diagnostic["传输方式"] = self.last_source
            return await self.http_user_profile(effective, diagnostic)
        try:
            result = await self.page.evaluate(FETCH, {"path": path, "params": effective})
        except Exception as error:
            log_failed_exchange(path, effective, self.page.url, None, error)
            raise
        diagnostic.update({"HTTP状态": result["status"], "响应字符数": len(result["text"]),
                           "是否跳转": bool(result.get("redirected"))})
        try:
            return decode_response(result)
        except CollectionError as error:
            if isinstance(error, BusinessError):
                diagnostic["业务状态"] = error.codes
            log_failed_exchange(path, effective, self.page.url, result, error)
            raise

    async def http_get(self, path, params=None):
        origin = urlparse(self.page.url)
        url = f"{origin.scheme}://{origin.netloc}{path}"
        response = await self.page.context.request.get(
            url, params=params, timeout=30000, max_redirects=0, max_retries=0)
        try:
            return {"status": response.status, "redirected": 300 <= response.status < 400,
                    "contentType": response.headers.get("content-type"),
                    "retryAfter": response.headers.get("retry-after"),
                    "text": await response.text()}
        finally:
            await response.dispose()

    async def http_user_profile(self, params, diagnostic):
        path = "/api/user/detail/"
        result = None
        try:
            result = await self.http_get(path, params)
            diagnostic.update({"HTTP状态": result["status"], "响应字符数": len(result["text"]),
                               "是否跳转": result["redirected"]})
            if result["status"] != 200 or result["text"].strip():
                data = decode_response(result)
                diagnostic["资料来源"] = "HTTP 用户详情接口"
                return data
            log_failed_exchange(path, params, self.page.url, result, CollectionError("空响应"))
        except Exception as error:
            if isinstance(error, BusinessError):
                diagnostic["业务状态"] = error.codes
            log_failed_exchange(path, params, self.page.url, result, error)
            raise
        # 只对 HTTP 200 空正文使用已确认的公开 HTML 资料路径，不切换浏览器页面。
        name = params.get("uniqueId", "")
        if not re.fullmatch(r"[A-Za-z0-9_.]{1,32}", name) or name in (".", ".."):
            raise CollectionError("用户名格式无效，未请求 HTTP 主页")
        html_path = f"/@{name}"
        print(f"详情接口为空，改用直接 HTTP 读取公开主页：{html_path}（不跳转浏览器）")
        result = None
        fallback = {"接口路径": html_path, "传输方式": "直接 HTTP 请求"}
        diagnostic["HTTP主页补充"] = fallback
        try:
            result = await self.http_get(html_path)
            fallback.update({"HTTP状态": result["status"], "响应字符数": len(result["text"]),
                             "是否跳转": result["redirected"]})
            if result["status"] != 200 or result["redirected"]:
                raise CollectionError(f"HTTP 主页返回 {result['status']} 或跳转，未继续")
            info = profile_from_html(result["text"], name)
            diagnostic["资料来源"] = "HTTP 主页内嵌资料"
            return {"userInfo": info}
        except Exception as error:
            log_failed_exchange(html_path, {}, self.page.url, result, error)
            raise


async def get_posts(client, author, limit, state, max_pages):
    works, seen, cursors = [], set(), {"0"}
    cursor = "0"
    state.update({"是否完成": False, "请求上限": limit, "已请求页数": 0})
    try:
        while max_pages is None or state["已请求页数"] < max_pages:
            params = {"secUid": author["用户secUid"], "cursor": cursor, "count": 16,
                      "coverFormat": 2, "from_page": "user", "post_item_list_request_type": 0}
            if cursor != "0":
                params["needPinnedItemIds"] = "true"
            data = await client.request("/api/post/item_list/", params)
            state.setdefault("各页数据来源", []).append(client.last_source)
            if not isinstance(data.get("itemList"), list):
                raise CollectionError("作品响应缺少 itemList")
            state["已请求页数"] += 1
            for item in data["itemList"]:
                uid = str((item.get("author") or {}).get("id") or "")
                if uid != author["用户ID"] or not re.fullmatch(r"\d+", str(item.get("id", ""))):
                    raise CollectionError("作品作者不匹配或作品 ID 无效")
                item_id = str(item["id"])
                if item_id not in seen:
                    seen.add(item_id)
                    works.append({**item, "id": item_id})
                if limit is not None and len(works) >= limit:
                    state.update({"是否完成": True, "结束原因": "达到作品数量上限"})
                    return works
            state["后续游标"] = data.get("cursor")
            if data.get("hasMore") is False:
                state.update({"是否完成": True, "结束原因": "作品列表分页结束"})
                return works
            if data.get("hasMore") is not True:
                raise CollectionError("作品响应缺少明确的 hasMore 状态")
            cursor = str(data.get("cursor") or "")
            if not cursor or cursor in cursors:
                raise CollectionError("作品分页游标缺失或重复")
            cursors.add(cursor)
        raise CollectionError("达到作品分页上限，未取得完整目标列表")
    except Exception as error:
        state["未完成原因"] = safe_error(error)
        state["失败数据来源"] = client.last_source
        return works


async def get_comment_pages(client, work_id, state, max_pages, parent=None, seed=None,
                            progress=None, start_cursor="0"):
    comments = dict(seed or {})
    cursor = str(start_cursor)
    cursors = {cursor}
    state.update({"是否完成": False, "已请求页数": 0, "起始游标": cursor})
    try:
        while max_pages is None or state["已请求页数"] < max_pages:
            path = "/api/comment/list/reply/" if parent else "/api/comment/list/"
            params = {"cursor": cursor, "count": 3 if parent else 20, "from_page": "video"}
            params.update({"item_id": work_id, "comment_id": parent} if parent else {"aweme_id": work_id})
            data = await client.request(path, params)
            if not isinstance(data.get("comments"), list):
                raise CollectionError("评论响应缺少 comments 列表")
            state["已请求页数"] += 1
            for raw in data["comments"]:
                if is_root_comment_echo(raw, work_id, parent):
                    state.setdefault("排除的根评论回显", []).append({
                        "评论ID": parent, "作品ID": work_id, "原始reply_id": str(raw["reply_id"]),
                        "请求游标": cursor, "说明": "返回的是当前根评论自身，不作为子回复保存"})
                    continue
                comment = comment_info(raw, work_id, parent)
                old = comments.get(comment["评论ID"])
                if old:
                    if old["评论者信息"]["用户ID"] != comment["评论者信息"]["用户ID"]:
                        raise CollectionError("同一评论 ID 的用户身份不一致，未合并")
                    comment["评论者信息"] = {**old["评论者信息"], **{
                        k: v for k, v in comment["评论者信息"].items() if v is not None}}
                    if "用户资料采集状态" in old:
                        comment["用户资料采集状态"] = old["用户资料采集状态"]
                # 预览回复先合并，后续重复顶层评论不覆盖已取得的预览。
                previews = {r["评论ID"]: r for r in (old or {}).get("回复列表", [])}
                for reply in raw.get("reply_comment") or []:
                    normalized = comment_info(reply, work_id, comment["评论ID"])
                    previews[normalized["评论ID"]] = normalized
                comment["回复列表"] = list(previews.values())
                if parent is None:
                    comment["预览回复ID列表"] = [str(r["cid"]) for r in raw.get("reply_comment") or []]
                comments[comment["评论ID"]] = comment
            state.setdefault("各页响应核对", []).append({
                "请求游标": cursor, "请求条数": params["count"],
                "返回评论ID列表": [str(raw["cid"]) for raw in data["comments"]],
                "响应游标": data.get("cursor"), "是否还有下一页": data.get("has_more"),
                "接口计数": data.get("total"), "合并后去重数量": len(comments)})
            state.update({"接口总计数": data.get("total"), "后续游标": data.get("cursor")})
            if progress:
                progress(comments)
            if data.get("has_more") in (0, False):
                state["是否完成"] = True
                return comments
            if data.get("has_more") not in (1, True):
                raise CollectionError("评论缺少明确的 has_more 状态")
            cursor = str(data.get("cursor") if data.get("cursor") is not None else "")
            if not cursor or cursor in cursors:
                raise CollectionError("评论分页游标缺失或重复")
            cursors.add(cursor)
        raise CollectionError("达到评论分页上限")
    except Exception as error:
        state["未完成原因"] = safe_error(error)
        state["失败接口"] = "/api/comment/list/reply/" if parent else "/api/comment/list/"
        state["失败请求游标"] = cursor
        return comments


def record_reply_counts(comment, state):
    """分页完成与数量一致性独立；计数差异不改变请求/分页的完成状态。"""
    actual = len(comment["回复列表"])
    top_count, api_count = comment.get("回复数"), state.get("接口总计数")
    counts = [int(value) for value in (top_count, api_count)
              if (isinstance(value, int) and not isinstance(value, bool))
              or (isinstance(value, str) and value.isdigit())]
    consistent = all(actual == count for count in counts) if counts else None
    state.update({"回复分页是否完成": bool(state["是否完成"]), "已采集回复数": actual,
                  "顶层评论回复计数": top_count, "回复接口总计数": api_count,
                  "数量是否一致": consistent})
    if consistent is False:
        state["返回数量差异"] = {"已保存回复数": actual, "接口计数": max(counts),
                                   "顶层评论回复计数": top_count, "回复接口总计数": api_count}
        state["说明"] = ("当前接口采集流程已结束，已保存全部返回条目；计数存在差异，不保证包含不可见回复"
                         if state["是否完成"] else "回复采集未完成；数量差异不替代原始失败原因")


async def collect_comments(client, document, max_pages, checkpoint=None):
    work_id = document["视频信息"]["作品ID"]
    state = document["采集状态"]
    if state.get("评论是否采集完成"):
        return
    if document["互动数据"]["评论数"] == 0:
        state["顶层评论"] = {"是否完成": True, "结束原因": "作品返回评论数为零"}
        state["评论是否采集完成"] = True
        state["存在回复计数差异"] = False
        return
    state["顶层评论"] = {}
    seeds = {c["评论ID"]: c for c in document["评论列表"]}

    def update_comments(comments):
        document["评论列表"] = list(comments.values())
        if checkpoint:
            checkpoint()

    comments = await get_comment_pages(client, work_id, state["顶层评论"], max_pages,
                                       seed=seeds, progress=update_comments)
    update_comments(comments)
    for parent, comment in comments.items():
        reply_state = {"是否完成": True, "已请求页数": 0}
        comment["回复采集状态"] = reply_state
        if comment["回复数"] != 0 or comment["回复列表"]:
            seeds = {r["评论ID"]: r for r in comment["回复列表"]}
            reply_state.update({"预览回复ID列表": comment.get("预览回复ID列表", list(seeds)),
                                "合并前已保存回复ID列表": list(seeds), "合并后回复ID列表": list(seeds)})
            # 预览不保证是展开列表的前缀，不能用预览条数计算游标。
            if comment["回复数"] is not None and len(seeds) >= int(comment["回复数"]):
                reply_state["结束原因"] = "已保存预览回复覆盖接口回复计数"
                record_reply_counts(comment, reply_state)
                continue
            def update_replies(replies):
                comment["回复列表"] = list(replies.values())
                if checkpoint:
                    checkpoint()

            replies = await get_comment_pages(client, work_id, reply_state, max_pages,
                                              parent, seeds, progress=update_replies,
                                              start_cursor="0")
            comment["回复列表"] = list(replies.values())
            reply_state["合并后回复ID列表"] = list(replies)
            reply_state["未取得的直接回复目标ID列表"] = sorted({
                r["直接回复目标ID"] for r in replies.values()
                if r["直接回复目标ID"] and r["直接回复目标ID"] != parent
                and r["直接回复目标ID"] not in replies})
        record_reply_counts(comment, reply_state)
    document["评论列表"] = list(comments.values())
    state["评论是否采集完成"] = state["顶层评论"]["是否完成"] and all(
        c["回复采集状态"]["是否完成"] for c in comments.values())
    state["存在回复计数差异"] = any(
        c["回复采集状态"].get("数量是否一致") is False for c in comments.values())


async def enrich_users(client, document, cache):
    entries = [entry for c in document["评论列表"] for entry in [c, *c["回复列表"]]]
    complete = True
    for entry in entries:
        summary = entry["评论者信息"]
        uid = summary["用户ID"]
        if uid not in cache:
            try:
                if not summary["用户名"]:
                    raise CollectionError("评论者缺少用户名")
                if not client.profile_failure and not client.stopped:
                    print(f"补充用户资料：@{summary['用户名']}（{uid}）")
                data = await client.request("/api/user/detail/", {"uniqueId": summary["用户名"], "secUid": "", "from_page": "user"})
                cache[uid] = (profile_info(data.get("userInfo") or {}, uid, summary["用户名"]), None)
            except Exception as error:
                cache[uid] = (None, safe_error(error))
        profile, error = cache[uid]
        entry["用户资料采集状态"] = {"是否完成": profile is not None}
        if profile is not None:
            entry["评论者信息"] = {**summary, **{k: v for k, v in profile.items() if v is not None}}
        else:
            entry["用户资料采集状态"]["未完成原因"] = error
            failure = client.profile_failure
            if failure and failure["失败用户名"] == summary.get("用户名"):
                entry["用户资料采集状态"]["请求诊断"] = failure["请求诊断"]
            complete = False
        if client.checkpoint:
            client.checkpoint()
    document["采集状态"]["评论区用户资料是否完成"] = complete
    if client.profile_failure:
        document["采集状态"]["用户资料接口状态"] = {"是否暂停": True, **client.profile_failure}


def make_document(item, author):
    stats = item.get("stats") or {}
    v2 = item.get("statsV2") or {}
    video = item.get("video") or {}
    return {
        "作品链接": f"https://www.tiktok.com/@{author['用户名']}/{'photo' if item.get('imagePost') else 'video'}/{item['id']}",
        "采集时间": now(), "作者信息": author,
        "视频信息": {"作品ID": str(item["id"]), "作品文案": item.get("desc"),
                 "作品发布时间": item.get("createTime"), "是否置顶": item.get("isPinnedItem"),
                 "视频时长（秒）": video.get("duration"), "宽度": video.get("width"),
                 "高度": video.get("height"), "封面地址": video.get("cover"),
                 "原始封面地址": video.get("originCover"), "播放地址": video.get("playAddr"),
                 "下载地址": video.get("downloadAddr"), "是否图文作品": bool(item.get("imagePost"))},
        "互动数据": {"点赞数": stats.get("diggCount"), "评论数": stats.get("commentCount"),
                 "分享数": stats.get("shareCount"), "收藏数": stats.get("collectCount"),
                 "再发布计数": v2.get("repostCount")},
        "播放量": stats.get("playCount"), "评论列表": [],
        "采集状态": {"评论是否采集完成": False,
                 "评论者主页资料采集": {"是否启用": False, "说明": "仅保存评论接口返回的用户摘要"}},
    }


async def collect_account(client, author, output, limit=None, max_pages=None):
    index = {"账号主页": author["主页链接"], "作者信息": author, "采集开始时间": now(),
             "作品数量上限": limit, "作品列表采集状态": {}, "作品文件": [], "是否完成": False,
             "作品数量限制说明": "不限制作品数量" if limit is None else f"最多{limit}个去重作品",
             "评论者主页资料采集": {"是否启用": False, "说明": "仅保存评论接口返回的用户摘要"}}
    directory = output / author["用户名"]
    save_json(directory / "index.json", index)
    works = await get_posts(client, author, limit, index["作品列表采集状态"], max_pages)
    for number, item in enumerate(works, 1):
        document = make_document(item, author)
        filename = f"{item['id']}.json"
        save_json(directory / filename, document)
        stage = "采集评论及回复，保留评论者摘要"
        if client.stopped:
            stage = "仅保存已有详情，网络采集已停止"
        print(f"[{number}/{len(works)}] 作品 {item['id']}：{stage}")
        client.checkpoint = lambda: save_json(directory / filename, document)
        await collect_comments(client, document, max_pages, client.checkpoint)
        document["采集结束时间"] = now()
        save_json(directory / filename, document)
        index["作品文件"].append({"作品ID": str(item["id"]), "文件名": filename,
                                "存在回复计数差异": document["采集状态"].get("存在回复计数差异", False),
                                "是否完成": document["采集状态"]["评论是否采集完成"]})
        save_json(directory / "index.json", index)
        client.checkpoint = None
    index.update({"采集结束时间": now(), "实际作品数": len(works),
                  "回复计数差异作品数": sum(x["存在回复计数差异"] for x in index["作品文件"]),
                  "是否完成": index["作品列表采集状态"]["是否完成"] and all(
                      x["是否完成"] for x in index["作品文件"])})
    save_json(directory / "index.json", index)
    return index


async def retry_incomplete_account(client, author, output, max_pages=None):
    """仅补采索引内未完成的作品，不重取作品列表或覆盖完成作品。"""
    directory = output / author["用户名"]
    index = json.loads((directory / "index.json").read_text(encoding="utf-8"))
    if index.get("作者信息", {}).get("用户ID") != author["用户ID"]:
        raise CollectionError("索引作者与当前账号不一致，未补采")
    pending = []
    for record in index["作品文件"]:
        if record.get("是否完成"):
            continue
        work_id = str(record.get("作品ID", ""))
        if not re.fullmatch(r"\d+", work_id) or record.get("文件名") != f"{work_id}.json":
            raise CollectionError("未完成作品的文件名或 ID 无效，未补采")
        path = directory / record["文件名"]
        document = json.loads(path.read_text(encoding="utf-8"))
        if (document["视频信息"]["作品ID"] != work_id
                or document["作者信息"]["用户ID"] != author["用户ID"]):
            raise CollectionError("未完成作品文件与索引不匹配，未补采")
        pending.append((record, path, document))
    if not pending:
        print("索引中没有待补采的作品文件；未改写已有数据。")
        return index
    print(f"仅补采 {len(pending)} 个未完成作品，不重新请求作品列表。")
    attempted = 0
    for number, (record, path, document) in enumerate(pending, 1):
        print(f"[补采 {number}/{len(pending)}] 作品 {record['作品ID']}：评论及回复")
        client.checkpoint = lambda: save_json(path, document)
        try:
            await collect_comments(client, document, max_pages, client.checkpoint)
            document["采集结束时间"] = now()
            save_json(path, document)
        finally:
            client.checkpoint = None
        record.update({"是否完成": document["采集状态"]["评论是否采集完成"],
                       "存在回复计数差异": document["采集状态"].get("存在回复计数差异", False)})
        attempted += 1
        index.update({"最近补采时间": now(), "本次补采作品数": attempted,
                      "回复计数差异作品数": sum(bool(r.get("存在回复计数差异")) for r in index["作品文件"]),
                      "是否完成": bool(index["作品列表采集状态"].get("是否完成"))
                          and all(r.get("是否完成") for r in index["作品文件"])})
        save_json(directory / "index.json", index)
        if client.stopped:
            print("网络采集已停止；其余未完成作品保持原文件，未继续补采。")
            break
    return index


async def check_reply_cursor_zero(client, author, output, work_id, parent, count=3):
    """只读已有作品并请求一页回复，不重采作品、不自动重试。"""
    if not all(re.fullmatch(r"\d+", value) for value in (work_id, parent)):
        raise CollectionError("核对用的作品 ID 和评论 ID 必须为数字")
    if not isinstance(count, int) or not 1 <= count <= 100:
        raise CollectionError("核对请求条数必须为1至100的整数")
    directory = output / author["用户名"]
    document = json.loads((directory / f"{work_id}.json").read_text(encoding="utf-8"))
    if (document["视频信息"]["作品ID"] != work_id
            or document["作者信息"]["用户ID"] != author["用户ID"]):
        raise CollectionError("已有作品与当前目标作者不匹配，未发送核对请求")
    root = next((c for c in document["评论列表"] if c["评论ID"] == parent), None)
    if root is None:
        raise CollectionError("已有作品中未找到目标顶层评论，未发送核对请求")
    old_ids = {r["评论ID"] for r in root["回复列表"]}
    report = {"核对时间": now(), "作品ID": work_id, "顶层评论ID": parent, "是否核对成功": False,
              "预览回复ID列表": root.get("预览回复ID列表", []),
              "已有展开响应核对": root.get("回复采集状态", {}).get("各页响应核对", []),
              "已有合并回复ID列表": sorted(old_ids)}
    params = {"item_id": work_id, "comment_id": parent, "cursor": "0", "count": count, "from_page": "video"}
    report["对照请求业务参数"] = params
    try:
        data = await client.request_once("/api/comment/list/reply/", params)
        if not isinstance(data.get("comments"), list):
            raise CollectionError("对照响应缺少 comments 列表")
        # 关联字段在严格校验之前留证，避免拒绝合并后无法判断实际返回结构。
        links = []
        for raw in data["comments"]:
            fields = {}
            for key in ("cid", "aweme_id", "reply_id", "reply_to_reply_id"):
                value = raw.get(key)
                fields[key] = str(value) if re.fullmatch(r"\d+", str(value)) else None
                fields[key + "原始类型"] = type(value).__name__
            links.append(fields)
        report["原始回复关联核对"] = links
        report.update({"响应游标": data.get("cursor"), "是否还有下一页": data.get("has_more"),
                       "接口计数": data.get("total")})
        echoes = [raw for raw in data["comments"] if is_root_comment_echo(raw, work_id, parent)]
        report["排除的根评论回显ID列表"] = [str(raw["cid"]) for raw in echoes]
        replies = [comment_info(raw, work_id, parent) for raw in data["comments"]
                   if not is_root_comment_echo(raw, work_id, parent)]
        ids = {r["评论ID"] for r in replies}
        missing = {r["直接回复目标ID"] for r in root["回复列表"]
                   if r["直接回复目标ID"] and r["直接回复目标ID"] != parent
                   and r["直接回复目标ID"] not in old_ids}
        report.update({"是否核对成功": True, "返回回复ID列表": [r["评论ID"] for r in replies],
                       "响应游标": data.get("cursor"), "是否还有下一页": data.get("has_more"),
                       "接口计数": data.get("total"), "新增回复ID列表": sorted(ids - old_ids),
                       "实际返回条数": len(replies), "实际去重条数": len(ids),
                       "补回的直接回复目标ID列表": sorted(ids & missing),
                       "尚未取得的直接回复目标ID列表": sorted(missing - ids)})
    except Exception as error:
        report["未完成原因"] = safe_error(error)
    report["请求诊断"] = client.diagnostics.get("/api/comment/list/reply/", {})
    suffix = "" if count == 3 else f"-count{count}"
    path = output / "_diagnostics" / f"{parent}-cursor-zero{suffix}-control.json"
    save_json(path, report)
    print("对照结果：" + json.dumps(report, ensure_ascii=False))
    print(f"核对文件：{path}；原作品文件未改写。")
    return report


async def run(args):
    from playwright.async_api import async_playwright
    name = parse_link(args.link)
    cookies = json.loads(args.cookies.read_text(encoding="utf-8"))
    if not isinstance(cookies, list) or not cookies:
        raise CollectionError("TikTok Cookie 文件不是非空 Playwright Cookie 数组")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chrome", headless=False if args.debug else args.headless)
        try:
            context = await browser.new_context()
            await context.add_cookies(cookies)
            page = await context.new_page()
            client = BrowserClient(page, manual=args.debug or not args.headless,
                                   max_manual_pauses=args.manual_pauses)
            client.require_initial_capture = True
            page.on("request", client.observe)
            page.on("response", client.observe_response)
            await page.goto(f"https://www.tiktok.com/@{name}", wait_until="domcontentloaded", timeout=60000)
            if args.debug:
                print("已进入有头暂停调试模式：请手动检查页面；点击 Playwright Inspector 的 Resume 后继续。")
                await page.pause()
            text = await page.locator("#__UNIVERSAL_DATA_FOR_REHYDRATION__").text_content(timeout=15000)
            data = json.loads(text)
            info = data.get("__DEFAULT_SCOPE__", {}).get("webapp.user-detail", {}).get("userInfo")
            if not isinstance(info, dict):
                raise CollectionError("主页缺少目标作者资料，未继续请求")
            author = profile_info(info, expected_name=name)
            if not author["用户secUid"]:
                raise CollectionError("作者资料缺少 secUid")
            # 等待首批响应正文读完；捕获失败不再重发首批请求。
            await client.wait_initial_posts(author["用户secUid"])
            if args.check_reply:
                result = await check_reply_cursor_zero(client, author, args.output, *args.check_reply,
                                                       count=args.check_reply_count)
                return 0 if result["是否核对成功"] else 2
            if args.retry_incomplete:
                index = await retry_incomplete_account(client, author, args.output, args.max_pages)
            else:
                index = await collect_account(client, author, args.output, args.limit, args.max_pages)
            print(f"保存目录：{args.output / name}；实际作品数：{index['实际作品数']}")
            if not index["是否完成"]:
                print("未完整采集，原因见 index.json 及各作品的采集状态。")
                return 2
            if index["回复计数差异作品数"]:
                print(f"当前可访问列表采集流程已完成；{index['回复计数差异作品数']} 个作品存在回复计数差异，详见 JSON。")
            return 0
        finally:
            await browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-link", "--link", required=True, help="TikTok 账号主页")
    parser.add_argument("--limit", "-limit", type=int, default=None, help="可选的去重作品数量上限，默认不限制")
    parser.add_argument("--cookies", type=Path, default=ROOT / "account/tiktok.cookies.json")
    parser.add_argument("--output", type=Path, default=ROOT / "data/tiktok")
    parser.add_argument("--max-pages", type=int, default=None, help="可选的每个列表分页上限，默认不限制")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--debug", action="store_true", help="强制有头模式，打开主页后 page.pause() 等待手动调试")
    parser.add_argument("--manual-pauses", type=int, default=None,
                        help="人工暂停次数上限，默认不限，0禁用；终端回车后仅重试当前请求一次")
    parser.add_argument("--check-reply", nargs=2, metavar=("作品ID", "评论ID"),
                        help="只对已有评论请求一次 cursor=0 的回复页，不重采或覆盖作品")
    parser.add_argument("--check-reply-count", type=int, default=3,
                        help="核对模式的 count，默认3，允许1至100；不改变正常采集参数")
    parser.add_argument("--retry-incomplete", action="store_true", help="仅补采已有索引中的未完成作品")
    args = parser.parse_args()
    if ((args.limit is not None and args.limit < 1)
            or (args.max_pages is not None and args.max_pages < 1)):
        parser.error("指定 --limit 或 --max-pages 时必须大于零")
    if args.manual_pauses is not None and args.manual_pauses < 0:
        parser.error("--manual-pauses 不能为负数")
    if args.check_reply and not all(re.fullmatch(r"\d+", value) for value in args.check_reply):
        parser.error("--check-reply 的作品ID和评论ID必须为数字")
    if not 1 <= args.check_reply_count <= 100:
        parser.error("--check-reply-count 必须为1至100")
    if not args.check_reply and args.check_reply_count != 3:
        parser.error("--check-reply-count 必须配合 --check-reply 使用")
    if args.retry_incomplete and (args.check_reply or args.limit is not None):
        parser.error("--retry-incomplete 不与 --check-reply 或 --limit 同时使用")
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("已中断，保留已经落盘的数据；未完成项不视为完整采集。")
        return 130
    except (OSError, ValueError) as error:
        print(f"采集失败：{type(error).__name__}，请检查链接、文件和 JSON 格式")
        return 1
    except Exception as error:
        print(f"采集失败：{safe_error(error)}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
