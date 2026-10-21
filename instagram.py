"""通过系统 Chrome 的网页 fetch 采集 Instagram 单作品及可访问的评论。"""

import argparse
import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
COMMENTS = "xdt_api__v1__media__media_id__comments__connection"
REPLIES = "xdt_api__v1__media__media_id__comments__parent_comment_id__child_comments__connection"
QUERIES = {
    "profile": ("PolarisProfilePageContentQuery", "28649589871350527"),
    "initial": ("PolarisPostCommentsContainerQuery", "28832055503095837"),
    "comments": ("PolarisPostCommentsPaginationQuery", "28692551520410323"),
    "replies": ("PolarisPostChildCommentsQuery", "28723871317239557"),
}

OUTPUT_NAMES = {
    "source_url": "作品链接", "collected_at": "采集开始时间", "finished_at": "采集结束时间",
    "author": "作者信息", "video": "视频信息", "interactions": "互动数据",
    "plays": "播放量数据", "comments": "评论列表", "collection": "采集状态",
    "pk": "数字ID", "id": "接口ID", "code": "作品短码", "username": "用户名",
    "full_name": "昵称", "biography": "简介", "is_verified": "是否认证",
    "is_private": "是否私密账号", "profile_pic_url": "头像地址",
    "profile_pic_url_hd": "高清头像地址", "follower_count": "粉丝数",
    "following_count": "关注数", "media_count": "作品数",
    "taken_at": "视频发布时间", "product_type": "作品类型", "media_type": "媒体类型编码",
    "video_duration": "视频时长（秒）", "original_width": "原始宽度（像素）",
    "original_height": "原始高度（像素）", "has_audio": "是否包含音频",
    "caption": "作品文案", "video_versions": "视频资源列表", "image_versions2": "封面资源",
    "like_count": "点赞数", "comment_count": "评论数", "media_repost_count": "再发布数",
    "play_count": "播放量", "ig_play_count": "Instagram播放计数",
    "view_count": "浏览计数（view_count）", "source": "来源接口",
    "text": "评论内容", "created_at": "评论发布时间", "comment_like_count": "评论点赞数",
    "child_comment_count": "回复数", "parent_comment_id": "父评论ID",
    "is_edited": "是否编辑", "is_pinned": "是否置顶", "has_translation": "是否有翻译",
    "replies": "回复列表", "replies_collection": "回复采集状态",
    "scope": "采集范围", "comments_complete": "评论是否采集完成",
    "fetched_top_level": "已采集顶层评论数", "fetched_replies": "已采集回复数",
    "top_level": "顶层评论采集状态", "complete": "是否完成", "pages": "已采集页数",
    "fetched": "已采集条数", "next_cursor": "下一页游标", "reason": "状态说明",
    "page_info": "分页信息", "end_cursor": "结束游标", "start_cursor": "起始游标",
    "has_next_page": "是否有下一页", "has_previous_page": "是否有上一页",
    "errors": "未完成原因", "stage": "处理阶段", "parent_id": "父评论ID",
    "message": "原因说明", "url": "资源地址", "type": "资源类型编码",
    "width": "宽度（像素）", "height": "高度（像素）", "bandwidth": "带宽",
    "candidates": "候选资源列表", "additional_candidates": "其他候选资源",
    "first_frame": "首帧图", "igtv_first_frame": "IGTV首帧图", "smart_frame": "智能选帧图",
    "default": "默认项", "fallback": "备用项", "file_size_kb": "文件大小（KB）",
    "estimated_scans_sizes": "预估扫描大小列表", "scans_profile": "扫描配置",
    "progressive_identifier": "渐进式资源标识", "is_spatial_image": "是否空间图像",
    "rendered_width": "渲染宽度（像素）", "url_expiration_timestamp_us": "地址过期时间戳（微秒）",
    "scrubber_spritesheet_info_candidates": "进度条预览拼图候选信息",
    "sprite_height": "拼图高度（像素）", "sprite_width": "拼图宽度（像素）",
    "sprite_urls": "拼图地址列表", "thumbnail_height": "缩略图高度（像素）",
    "thumbnail_width": "缩略图宽度（像素）", "thumbnail_duration": "缩略图时间间隔",
    "thumbnails_per_row": "每行缩略图数", "total_thumbnail_num_per_sprite": "每张拼图缩略图总数",
    "max_thumbnails_per_sprite": "每张拼图最多缩略图数", "video_length": "接口视频长度",
    "author_profile": "作者资料采集状态", "query_name": "请求名称",
    "profile_url": "主页链接", "external_url": "外部链接", "category": "账号类别",
    "is_business": "是否商业账号", "is_professional_account": "是否专业账号",
    "total_clips_count": "Reels作品数",
    "commenter_profiles": "评论区用户资料采集状态", "profile_collection": "用户资料采集状态",
    "unique_users": "去重用户数", "fetched_users": "已补齐用户数",
    "failed_users": "未补齐用户数", "user_id": "用户ID",
}


# 所有网络采集在网页内执行；不使用 requests、httpx 或 context.request。
FETCH = r"""async ({path, query, variables}) => {
    const readModule = (name) => {
        try { return window.require ? window.require(name) : null; }
        catch { return null; }
    };
    const csrf = document.cookie.split('; ').find(x => x.startsWith('csrftoken='));
    const headers = {
        'x-ig-app-id': '936619743392459',
        'x-requested-with': 'XMLHttpRequest'
    };
    if (csrf) headers['x-csrftoken'] = decodeURIComponent(csrf.slice(10));
    const options = {credentials: 'same-origin', headers};
    if (query) {
        const dtsg = readModule('DTSGInitialData')?.token
            || document.querySelector('input[name="fb_dtsg"]')?.value;
        const lsd = readModule('LSD')?.token;
        const body = new URLSearchParams({
            __a: '1', __d: 'www', __user: '0', __comet_req: '7',
            fb_api_caller_class: 'RelayModern',
            fb_api_req_friendly_name: query[0], doc_id: query[1],
            server_timestamps: 'true', variables: JSON.stringify(variables)
        });
        if (dtsg) {
            body.set('fb_dtsg', dtsg);
            body.set('jazoest', '2' + [...dtsg].reduce((s, c) => s + c.charCodeAt(0), 0));
        }
        if (lsd) { body.set('lsd', lsd); headers['x-fb-lsd'] = lsd; }
        headers['content-type'] = 'application/x-www-form-urlencoded';
        headers['x-fb-friendly-name'] = query[0];
        options.method = 'POST';
        options.body = body.toString();
    }
    const controller = new AbortController();
    options.signal = controller.signal;
    const timer = setTimeout(() => controller.abort(), 30000);
    try {
        const response = await fetch(path, options);
        const text = await response.text();
        // 不将响应原文带回错误信息，避免输出页面中的会话凭据。
        return {status: response.status, redirected: response.redirected, text};
    } finally { clearTimeout(timer); }
}"""


class CollectionError(RuntimeError):
    def __init__(self, message, http_status=None):
        super().__init__(message)
        self.http_status = http_status


def parse_link(link):
    parsed = urlparse(link)
    match = re.fullmatch(r"/(?:reel|reels|p)/([A-Za-z0-9_-]+)/?", parsed.path)
    if (parsed.scheme != "https" or parsed.netloc not in
            ("www.instagram.com", "instagram.com") or not match):
        raise ValueError("请输入有效的 https://www.instagram.com/reel/短码/ 链接")
    code = match.group(1)
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    media_id = 0
    for char in code:
        media_id = media_id * 64 + alphabet.index(char)
    if media_id == 0 or media_id > 2**64 - 1:
        raise ValueError("作品短码超出有效范围")
    return code, str(media_id)


def decode_response(result):
    if result["status"] != 200 or result.get("redirected"):
        status = result["status"]
        hint = "请求频率受限，已停止继续补充用户资料" if status == 429 else "请求未成功或发生跳转"
        raise CollectionError(f"接口返回 HTTP {status}：{hint}", http_status=status)
    text = re.sub(r"^\s*for\s*\(;;\);", "", result["text"])
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        raise CollectionError("接口未返回 JSON，请检查页面登录状态") from None
    if not isinstance(data, dict):
        raise CollectionError("接口响应不是 JSON 对象")
    if data.get("errors") or data.get("status") == "fail" or data.get("error"):
        raise CollectionError("接口返回业务错误；未输出可能包含凭据的响应原文")
    return data


def select_media(data, media_id, code):
    for media in data.get("items", []):
        if str(media.get("pk")) == media_id and media.get("code") == code:
            return media
    raise CollectionError("详情响应没有匹配目标作品 ID 和短码")


def pick(value, fields):
    return {key: value.get(key) for key in fields}


def author_info(user):
    return pick(user or {}, (
        "pk", "id", "username", "full_name", "biography", "is_verified",
        "is_private", "profile_pic_url", "profile_pic_url_hd", "follower_count",
        "following_count", "media_count",
    ))


def merge_author(summary, profile, expected_id):
    """只补入匹配作者的有效资料；0、False、空简介均为有效值。"""
    if str(profile.get("pk") or profile.get("id")) != expected_id:
        raise CollectionError("用户资料 ID 与请求的目标用户不一致，未合并")
    merged = dict(summary or {})
    merged.update({key: value for key, value in profile.items() if value is not None})
    if not merged.get("profile_pic_url_hd"):
        merged["profile_pic_url_hd"] = (profile.get("hd_profile_pic_url_info") or {}).get("url")
    result = author_info(merged)
    result.update(pick(merged, ("external_url", "category", "is_business",
                                "is_professional_account", "total_clips_count")))
    username = merged.get("username")
    result["profile_url"] = f"https://www.instagram.com/{username}/" if username else None
    return result


def comment_info(node):
    if not node.get("pk"):
        raise CollectionError("评论条目缺少 ID，无法可靠去重")
    result = pick(node, (
        "pk", "text", "created_at", "comment_like_count", "child_comment_count",
        "parent_comment_id", "is_edited", "is_pinned", "has_translation",
    ))
    result["pk"] = str(node["pk"])
    result["author"] = author_info(node.get("user"))
    return result


class BrowserClient:
    def __init__(self, page, delay=0.5):
        self.page = page
        self.delay = delay

    async def request(self, path, query=None, variables=None):
        await asyncio.sleep(self.delay)
        result = await self.page.evaluate(FETCH, {
            "path": path, "query": query, "variables": variables,
        })
        return decode_response(result)

    async def profile(self, user_id):
        variables = {
            "id": user_id, "enable_integrity_filters": True,
            "__relay_internal__pv__PolarisCannesGuardianExperienceEnabledrelayprovider": True,
            "__relay_internal__pv__PolarisCASB976ProfileEnabledrelayprovider": False,
            "__relay_internal__pv__PolarisWebSchoolsEnabledrelayprovider": False,
            "__relay_internal__pv__PolarisProfileRingsGradientEnabledrelayprovider": False,
            "__relay_internal__pv__PolarisRepostsConsumptionEnabledrelayprovider": True,
            "__relay_internal__pv__PolarisShortDramaEnabledrelayprovider": True,
        }
        return await self.request("/api/graphql", QUERIES["profile"], variables)

    async def comments(self, media_id, cursor=None):
        variables = {
            "media_id": media_id,
            "__relay_internal__pv__PolarisIsLoggedInrelayprovider": True,
            "__relay_internal__pv__PolarisThreadsStyleCommentsEnabledrelayprovider": False,
        }
        if cursor is not None:
            variables.update(after=cursor, before=None, first=10, last=None,
                             sort_order="popular")
        return await self.request("/api/graphql", QUERIES[
            "initial" if cursor is None else "comments"], variables)

    async def replies(self, media_id, parent_id, cursor=None):
        variables = {
            "media_id": media_id, "parent_comment_id": parent_id,
            "after": cursor, "before": None, "first": None, "last": None,
            "is_chronological": None,
            "__relay_internal__pv__PolarisIsLoggedInrelayprovider": True,
            "__relay_internal__pv__PolarisThreadsStyleCommentsEnabledrelayprovider": False,
        }
        return await self.request("/api/graphql", QUERIES["replies"], variables)


async def collect_pages(fetch_page, root, output, state, max_pages, parent_id=None):
    """保留去重结果与分页状态；异常时由调用方保存部分结果。"""
    known = {item["pk"]: item for item in output}
    seen_cursors = set()
    cursor = None
    state.update(complete=False, pages=0, next_cursor=None)
    for _ in range(max_pages):
        data = await fetch_page(cursor)
        connection = data.get("data", {}).get(root)
        if not isinstance(connection, dict) or not isinstance(connection.get("edges"), list):
            raise CollectionError("评论响应缺少预期列表节点")
        for edge in connection["edges"]:
            comment = comment_info(edge["node"])
            returned_parent = comment["parent_comment_id"]
            if parent_id is not None and str(returned_parent) != parent_id:
                raise CollectionError("回复的父评论 ID 与请求不一致")
            if parent_id is None and returned_parent is not None:
                raise CollectionError("顶层评论响应混入未关联的子评论")
            if comment["pk"] not in known:
                output.append(comment)
                known[comment["pk"]] = comment
            else:
                known[comment["pk"]].update(comment)
        info = connection.get("page_info", {})
        state.update(pages=state["pages"] + 1, page_info=info,
                     next_cursor=info.get("end_cursor"), fetched=len(output))
        if info.get("has_previous_page") is True:
            raise CollectionError("响应指示存在前向回复页，当前实现不能声明回复完整")
        if info.get("has_next_page") is False:
            state["complete"] = True
            return
        if info.get("has_next_page") is not True:
            raise CollectionError("缺少明确的分页结束状态")
        cursor = info.get("end_cursor")
        if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
            raise CollectionError("分页游标缺失或重复，已停止并保留部分结果")
        seen_cursors.add(cursor)
    raise CollectionError(f"达到分页上限 {max_pages}，未声明采集完整")


async def collect_comments(client, media, result, max_pages):
    media_id = str(media["pk"])
    state = result["collection"]
    if media.get("comment_count") == 0:
        state["top_level"] = {"complete": True, "pages": 0, "fetched": 0,
                              "reason": "作品详情返回 comment_count=0"}
    else:
        try:
            await collect_pages(lambda c: client.comments(media_id, c), COMMENTS,
                                result["comments"], state["top_level"], max_pages)
        except Exception as error:
            state["errors"].append({"stage": "comments", "message": safe_error(error)})
    for comment in result["comments"]:
        comment["replies"] = []
        comment["replies_collection"] = {"complete": False}
        if comment["child_comment_count"] == 0:
            comment["replies_collection"].update(complete=True, pages=0, fetched=0)
            continue
        try:
            await collect_pages(
                lambda c: client.replies(media_id, comment["pk"], c), REPLIES,
                comment["replies"], comment["replies_collection"], max_pages,
                parent_id=comment["pk"],
            )
        except Exception as error:
            state["errors"].append({"stage": "replies", "parent_id": comment["pk"],
                                    "message": safe_error(error)})
    state["comments_complete"] = state["top_level"].get("complete", False) and all(
        c["replies_collection"]["complete"] for c in result["comments"])
    state["fetched_top_level"] = len(result["comments"])
    state["fetched_replies"] = sum(len(c["replies"]) for c in result["comments"])


async def collect_author(client, media, result):
    state = {"complete": False, "source": "/api/graphql",
             "query_name": QUERIES["profile"][0]}
    result["collection"]["author_profile"] = state
    summary = media.get("user") or {}
    user_id = summary.get("pk") or summary.get("id")
    try:
        if not user_id:
            raise CollectionError("作品详情缺少作者用户 ID")
        data = await client.profile(str(user_id))
        profile = (data.get("data") or {}).get("user")
        if not isinstance(profile, dict):
            raise CollectionError("作者资料响应缺少 data.user")
        result["author"] = merge_author(summary, profile, str(user_id))
        state["complete"] = True
    except Exception as error:
        result["collection"]["errors"].append({"stage": "author_profile",
                                                "message": safe_error(error)})


async def collect_commenter_profiles(client, result):
    """补齐所有已采集评论及回复的用户资料，每个用户最多请求一次。"""
    groups = {}
    entries = [entry for comment in result["comments"]
               for entry in [comment, *comment.get("replies", [])]]
    for entry in entries:
        user = entry.get("author") or {}
        user_id = user.get("pk") or user.get("id")
        if user_id:
            groups.setdefault(str(user_id), []).append(entry)
        else:
            entry["profile_collection"] = {"complete": False, "reason": "缺少用户ID"}
    state = {"complete": False, "unique_users": len(groups),
             "fetched_users": 0, "failed_users": 0}
    result["collection"]["commenter_profiles"] = state
    owner = result["author"]
    owner_id = str(owner.get("pk") or owner.get("id"))
    owner_ready = result["collection"].get("author_profile", {}).get("complete", False)
    stopped = False
    print(f"补充评论区用户资料：共 {len(groups)} 个去重用户")
    for user_id, occurrences in groups.items():
        status = {"complete": False, "source": "/api/graphql",
                  "query_name": QUERIES["profile"][0]}
        profile = None
        try:
            if user_id == owner_id and owner_ready:
                profile = owner
            elif stopped:
                status["reason"] = "此前请求受限或登录异常，未继续请求"
            else:
                data = await client.profile(user_id)
                profile = (data.get("data") or {}).get("user")
                if not isinstance(profile, dict):
                    raise CollectionError("用户资料响应缺少 data.user")
                profile = merge_author(occurrences[0]["author"], profile, user_id)
            if profile is not None:
                for entry in occurrences:
                    entry["author"] = merge_author(entry["author"], profile, user_id)
                status["complete"] = True
                state["fetched_users"] += 1
        except Exception as error:
            status["reason"] = safe_error(error)
            result["collection"]["errors"].append({"stage": "commenter_profiles",
                                                    "user_id": user_id,
                                                    "message": safe_error(error)})
            if isinstance(error, CollectionError) and error.http_status in (401, 403, 429):
                stopped = True
        if not status["complete"]:
            state["failed_users"] += 1
        for entry in occurrences:
            entry["profile_collection"] = dict(status)
    state["complete"] = all(entry["profile_collection"]["complete"] for entry in entries)
    if any(not (entry.get("author") or {}).get("pk") and
           not (entry.get("author") or {}).get("id") for entry in entries):
        result["collection"]["errors"].append({"stage": "commenter_profiles",
                                                "message": "部分评论或回复缺少用户ID"})


def safe_error(error):
    if isinstance(error, CollectionError):
        return str(error)
    return f"{type(error).__name__}：浏览器操作或数据结构异常，未输出原文"


def make_result(media, code):
    return {
        "source_url": f"https://www.instagram.com/reel/{code}/",
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "author": author_info(media.get("user")),
        "video": {
            **pick(media, ("pk", "id", "code", "taken_at", "product_type",
                           "media_type", "video_duration", "original_width",
                           "original_height", "has_audio", "video_versions", "image_versions2")),
            "caption": (media.get("caption") or {}).get("text"),
        },
        "interactions": pick(media, ("like_count", "comment_count", "media_repost_count")),
        "plays": {**pick(media, ("play_count", "ig_play_count", "view_count")),
                  "source": f"/api/v1/media/{media['pk']}/info/"},
        "comments": [],
        "collection": {"top_level": {}, "comments_complete": False, "errors": [],
                       "scope": "当前会话可访问的评论与回复，不保证包含隐藏或已删除内容"},
    }


def chinese_output(value, section=""):
    """只转换落盘字段名，保留内部接口字段、所有数值及时间戳。"""
    if isinstance(value, list):
        return [chinese_output(item, section) for item in value]
    if not isinstance(value, dict):
        return value
    document = {}
    for key, item in value.items():
        name = OUTPUT_NAMES.get(key, f"接口字段（{key}）")
        if key == "pk":
            name = {"video": "作品ID", "author": "用户ID",
                    "comments": "评论ID", "replies": "回复ID"}.get(section, name)
        if key == "author" and section in ("comments", "replies"):
            name = "评论者信息" if section == "comments" else "回复者信息"
        if key == "created_at" and section == "replies":
            name = "回复发布时间"
        if name in document:
            raise CollectionError("中文字段名冲突，停止写入以免丢失数据")
        document[name] = chinese_output(item, key)
    return document


def save_result(result, output_dir, code):
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / f"{code}.json"
    temporary = destination.with_suffix(".json.tmp")
    document = chinese_output(result)
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=4), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(destination)
    return destination


async def run(args):
    from playwright.async_api import async_playwright

    code, media_id = parse_link(args.link)
    cookies = json.loads(args.cookies.read_text(encoding="utf-8"))
    if not isinstance(cookies, list) or not any(
            c.get("name") == "sessionid" and c.get("value") for c in cookies):
        raise CollectionError("Cookie 文件缺少 sessionid，请先运行 instargram.js 登录保存")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chrome", headless=args.headless)
        try:
            context = await browser.new_context()
            await context.add_cookies(cookies)
            page = await context.new_page()
            await page.goto("https://www.instagram.com/", wait_until="domcontentloaded",
                            timeout=60000)
            client = BrowserClient(page)
            print(f"请求作品详情：{code}（{media_id}）")
            detail = await client.request(f"/api/v1/media/{media_id}/info/")
            media = select_media(detail, media_id, code)
            result = make_result(media, code)
            # 先保存详情，评论中途失败仍有可用结果。
            save_result(result, args.output, code)
            print("请求作者主页资料")
            await collect_author(client, media, result)
            save_result(result, args.output, code)
            await collect_comments(client, media, result, args.max_pages)
            save_result(result, args.output, code)
            await collect_commenter_profiles(client, result)
            result["finished_at"] = datetime.now(timezone.utc).isoformat()
            destination = save_result(result, args.output, code)
            state = result["collection"]
            print(f"已保存：{destination}")
            print(f"顶层评论 {state['fetched_top_level']} 条，回复 {state['fetched_replies']} 条")
            if not all((state["comments_complete"], state["author_profile"]["complete"],
                        state["commenter_profiles"]["complete"])):
                print("数据未完整取得，具体阶段和原因见 JSON 中的“采集状态”。")
                return 2
            print("当前可访问评论分页采集完成。")
            return 0
        finally:
            await browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-link", "--link", required=True, help="Instagram 作品链接")
    parser.add_argument("--cookies", type=Path, default=ROOT / "account/instargram.cookies.json")
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "instagram")
    parser.add_argument("--headless", action="store_true", help="不显示 Chrome 窗口")
    parser.add_argument("--max-pages", type=int, default=1000, help="每个评论列表的分页上限")
    args = parser.parse_args()
    if args.max_pages < 1:
        parser.error("--max-pages 必须大于零")
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("已中断；如已取得作品详情，输出目录保留先前保存结果。")
        return 130
    except (ValueError, OSError, CollectionError) as error:
        # 文件错误不含 Cookie 内容；JSON 解码失败也不输出输入原文。
        message = str(error) if not isinstance(error, ValueError) else "链接或 Cookie JSON 格式不正确"
        print(f"采集失败：{message}")
        return 1
    except Exception as error:
        print(f"采集失败：{safe_error(error)}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
