import json
import threading
import unittest
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import tiktok

FIXTURE = json.loads((Path(__file__).parent / "fixtures/tiktok_api.json").read_text())


class Handler(BaseHTTPRequestHandler):
    calls = []
    responses = {}
    received_test_cookie = False

    def log_message(self, *args):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/api/user/detail/":
            self.__class__.received_test_cookie = "test_session=local-only" in (self.headers.get("Cookie") or "")
        self.calls.append((u.path, q))
        response_key = (u.path, q.get("cursor", ["0"])[0])
        if u.path == "/rate-limited" and sum(path == u.path for path, _ in self.calls) == 1:
            self.send_response(429)
            self.send_header("Retry-After", "7")
            self.end_headers()
            self.wfile.write(b"Too many requests")
            return
        if u.path == "/forbidden":
            self.send_response(403)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b'<html><title>Access Denied</title><body>Request blocked by gateway.</body></html>')
            return
        if response_key in self.responses:
            response = self.responses[response_key]
            if callable(response):
                response = response(q)
            body = response if isinstance(response, bytes) else json.dumps(response).encode()
        elif u.path == "/":
            body = b"<html><body>Local browser API test</body></html>"
        elif u.path == "/api/post/item_list/":
            cursor = q.get("cursor", ["0"])[0]
            pages = {"0": 0, "1792166648103": 1, "1791940315639": 2}
            body = json.dumps(FIXTURE["posts"][pages[cursor]]).encode()
        elif u.path == "/api/comment/list/":
            body = json.dumps(FIXTURE["comments"][0 if q["cursor"][0] == "0" else 1]).encode()
        elif u.path == "/api/comment/list/reply/":
            body = json.dumps(FIXTURE["replies"]).encode()
        elif u.path == "/api/user/detail/":
            info = deepcopy(FIXTURE["profile"])
            info["user"].update(id="10", uniqueId=q["uniqueId"][0], nickname="测试用户")
            body = json.dumps({"status_code": 0, "userInfo": info}).encode()
        elif u.path == "/empty":
            body = b""
        elif u.path == "/recover":
            attempts = sum(path == u.path and query.get("cursor") == q.get("cursor")
                           for path, query in self.calls)
            body = json.dumps({"status_code": 5 if attempts == 1 else 0}).encode()
        else:
            self.send_error(501)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json" if u.path != "/" else "text/html")
        self.end_headers()
        self.wfile.write(body)


class ParsingTests(unittest.TestCase):
    def test_retry_after_parsing_and_error_classification(self):
        self.assertEqual(tiktok.retry_after_seconds("7"), 7)
        for value in (None, "invalid", "nan", "inf", "-1"):
            self.assertEqual(tiktok.retry_after_seconds(value), 0)
        self.assertEqual(tiktok.retry_after_seconds("Wed, 01 Jan 2020 00:00:00 GMT"), 0)
        self.assertFalse(tiktok.is_transient_error(ValueError("Failed to fetch")))
        self.assertFalse(tiktok.is_transient_error(tiktok.BusinessError("failure", {"status_code": "5"})))

    def test_root_echo_requires_exact_work_comment_and_no_reply_target(self):
        raw = {"cid": "200", "aweme_id": "100", "reply_id": "100", "reply_to_reply_id": "0"}
        self.assertTrue(tiktok.is_root_comment_echo(raw, "100", "200"))
        self.assertFalse(tiktok.is_root_comment_echo(raw, "100", None))
        for key, value in (("cid", "201"), ("aweme_id", "101"), ("reply_id", "201"),
                           ("reply_to_reply_id", "300")):
            self.assertFalse(tiktok.is_root_comment_echo({**raw, key: value}, "100", "200"))

    def test_cli_defaults_to_unlimited_and_keeps_explicit_limit(self):
        limits = []

        async def capture(args):
            limits.append((args.limit, args.max_pages))
            return 0

        base = ["tiktok.py", "-link", "https://www.tiktok.com/@melimakesfaces"]
        with patch("tiktok.run", new=capture):
            with patch("sys.argv", base):
                self.assertEqual(tiktok.main(), 0)
            with patch("sys.argv", base + ["--limit", "5", "--max-pages", "2"]):
                self.assertEqual(tiktok.main(), 0)
        self.assertEqual(limits, [(None, None), (5, 2)])

    def test_link_validation(self):
        self.assertEqual(tiktok.parse_link("https://www.tiktok.com/@melimakesfaces"), "melimakesfaces")
        for link in ["https://evil.test/@x", "https://www.tiktok.com/@../", "https://www.tiktok.com/@x/video/123"]:
            with self.assertRaises(ValueError):
                tiktok.parse_link(link)

    def test_unknown_reply_counts_remain_unknown_not_zero(self):
        state = {"是否完成": True}
        tiktok.record_reply_counts({"回复数": None, "回复列表": []}, state)
        self.assertTrue(state["回复分页是否完成"])
        self.assertIsNone(state["顶层评论回复计数"])
        self.assertIsNone(state["回复接口总计数"])
        self.assertIsNone(state["数量是否一致"])
        self.assertNotIn("返回数量差异", state)

    def test_profile_and_comment_timestamp(self):
        author = tiktok.profile_info(FIXTURE["profile"], expected_name="melimakesfaces")
        self.assertEqual(author["粉丝数"], 2397)
        c = FIXTURE["comments"][0]["comments"][0]
        result = tiktok.comment_info(c, c["aweme_id"])
        self.assertEqual(result["评论发布时间"], c["create_time"])
        self.assertEqual(result["评论者信息"]["用户ID"], c["user"]["uid"])
        with self.assertRaises(tiktok.CollectionError):
            tiktok.comment_info(c, "wrong")

    def test_empty_and_business_error(self):
        for data in ["", '{"status_code":10201}', '{"hello":"world"}']:
            with self.assertRaises(tiktok.CollectionError):
                tiktok.decode_response({"status": 200, "text": data})

    def test_failed_exchange_logs_empty_body_without_tokens(self):
        params = {"uniqueId": "someone", "secUid": "", "aid": "1988", "msToken": "SECRET"}
        with patch("builtins.print") as output:
            tiktok.log_failed_exchange("/api/user/detail/", params,
                                       "https://www.tiktok.com/@author?token=PRIVATE",
                                       {"status": 200, "text": ""}, tiktok.CollectionError("空响应"))
        text = "\n".join(str(call.args[0]) for call in output.call_args_list)
        for expected in ("失败请求", "失败响应", "someone", "1988", "<空正文>", '"HTTP状态": 200'):
            self.assertIn(expected, text)
        self.assertNotIn("SECRET", text)
        self.assertNotIn("PRIVATE", text)

    def test_failed_exchange_logs_business_code_not_sensitive_response_fields(self):
        body = json.dumps({"status_code": 5, "status_msg": "", "sessionid": "SECRET",
                           "data": {"msToken": "PRIVATE"}})
        with patch("builtins.print") as output:
            tiktok.log_failed_exchange("/api/comment/list/", {"aweme_id": "123", "cursor": "0"},
                                       "https://www.tiktok.com/@author", {"status": 200, "text": body},
                                       tiktok.CollectionError("业务失败"))
        text = "\n".join(str(call.args[0]) for call in output.call_args_list)
        self.assertIn('"status_code": 5', text)
        self.assertIn('"status_msg": ""', text)
        self.assertNotIn("SECRET", text)
        self.assertNotIn("PRIVATE", text)

    def test_error_html_excerpt_retains_reason_and_redacts_sensitive_content(self):
        body = '''<html><title>403 Forbidden</title><body>Access denied.
        <script>script_secret = "HIDDEN";</script><!-- private_comment -->
        <a href="https://example.test/?token=URL_SECRET">Details</a>
        sessionid=SESSION_SECRET; msToken="TOKEN_SECRET"; Authorization: Bearer AUTH_SECRET
        https://example.test/?token=VISIBLE_URL_SECRET
        </body></html>'''
        text = tiktok.response_excerpt(body)
        self.assertIn("403 Forbidden", text)
        self.assertIn("Access denied", text)
        for secret in ("HIDDEN", "private_comment", "SESSION_SECRET", "TOKEN_SECRET", "AUTH_SECRET",
                       "URL_SECRET", "VISIBLE_URL_SECRET"):
            self.assertNotIn(secret, text)
        self.assertIn("已截断", tiktok.response_excerpt("reason " * 1000))



class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright
        Handler.calls = []
        Handler.responses = {}
        Handler.received_test_cookie = False
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(channel="chrome", headless=True)
        self.page = await self.browser.new_page()
        await self.page.goto(f"http://127.0.0.1:{self.server.server_port}/")
        self.client = tiktok.BrowserClient(self.page, delay=0, retry_delay=0)

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    async def test_default_50_posts_and_exact_limit(self):
        author = tiktok.profile_info(FIXTURE["profile"])
        state = {}
        posts = await tiktok.get_posts(self.client, author, 50, state, 10)
        self.assertEqual(len(posts), 50)
        self.assertEqual(len({p["id"] for p in posts}), 50)
        self.assertTrue(state["是否完成"])
        self.assertEqual(state["已请求页数"], 3)
        state2 = {}
        limited = await tiktok.get_posts(self.client, author, 5, state2, 10)
        self.assertEqual(len(limited), 5)
        self.assertEqual(sum(x.get("isPinnedItem") is True for x in limited), 3)

    def prepare_unlimited_posts(self):
        cursors = ["0", "1792166648103", "1791940315639"]
        for cursor, original in zip(cursors, FIXTURE["posts"]):
            page = deepcopy(original)
            for item in page["itemList"]:
                item["stats"]["commentCount"] = 0
            if cursor == cursors[-1]:
                # 样本原有50条，再增加2条，验证不会在50条截断。
                for number in range(2):
                    item = deepcopy(page["itemList"][0])
                    item["id"] = str(9000000000000000000 + number)
                    page["itemList"].append(item)
                page["hasMore"] = False
            Handler.responses[("/api/post/item_list/", cursor)] = page

    async def test_no_limit_collects_more_than_fifty_until_api_end(self):
        self.prepare_unlimited_posts()
        author = tiktok.profile_info(FIXTURE["profile"])
        with TemporaryDirectory() as folder:
            index = await tiktok.collect_account(self.client, author, Path(folder))
            self.assertIsNone(index["作品数量上限"])
            self.assertEqual(index["作品数量限制说明"], "不限制作品数量")
            self.assertEqual(index["实际作品数"], 52)
            self.assertTrue(index["是否完成"])
            state = index["作品列表采集状态"]
            self.assertIsNone(state["请求上限"])
            self.assertEqual(state["结束原因"], "作品列表分页结束")
            self.assertEqual(state["已请求页数"], 3)
            self.assertEqual(len(list((Path(folder) / author["用户名"]).glob("*.json"))), 53)

    async def test_unlimited_posts_still_report_page_guard_as_incomplete(self):
        self.prepare_unlimited_posts()
        state = {}
        author = tiktok.profile_info(FIXTURE["profile"])
        posts = await tiktok.get_posts(self.client, author, None, state, max_pages=1)
        self.assertTrue(posts)
        self.assertFalse(state["是否完成"])
        self.assertIn("达到作品分页上限", state["未完成原因"])

    async def test_unlimited_posts_pass_one_thousand_pages(self):
        author = tiktok.profile_info(FIXTURE["profile"])
        for cursor in range(1001):
            item = deepcopy(FIXTURE["posts"][0]["itemList"][0])
            item["id"] = str(9000000000000000000 + cursor)
            Handler.responses[("/api/post/item_list/", str(cursor))] = {
                "status_code": 0, "itemList": [item], "cursor": cursor + 1, "hasMore": cursor < 1000}
        state = {}
        posts = await tiktok.get_posts(self.client, author, None, state, max_pages=None)
        self.assertEqual(len(posts), 1001)
        self.assertEqual(state["已请求页数"], 1001)
        self.assertTrue(state["是否完成"])
        self.assertEqual(state["结束原因"], "作品列表分页结束")

    async def test_comments_and_replies_accept_no_page_limit(self):
        raw = deepcopy(FIXTURE["comments"][0]["comments"][0])
        work = raw["aweme_id"]
        raw.update(reply_id="0", reply_comment=[])
        second = deepcopy(raw)
        second["cid"] = "9000000000000000001"
        Handler.responses[("/api/comment/list/", "0")] = {
            "status_code": 0, "comments": [raw], "cursor": 20, "has_more": 1, "total": 2}
        Handler.responses[("/api/comment/list/", "20")] = {
            "status_code": 0, "comments": [second], "cursor": 40, "has_more": 0, "total": 2}
        state = {}
        comments = await tiktok.get_comment_pages(self.client, work, state, max_pages=None)
        self.assertEqual(len(comments), 2)
        self.assertTrue(state["是否完成"])
        parent = "7696223715234874143"
        reply_state = {}
        replies = await tiktok.get_comment_pages(self.client, "7692246277479812383", reply_state,
                                                max_pages=None, parent=parent)
        self.assertEqual(len(replies), 3)
        self.assertTrue(reply_state["是否完成"])

    async def test_comments_paging_and_reply_links(self):
        state = {}
        comments = await tiktok.get_comment_pages(self.client, "7692246277479812383", state, 2)
        self.assertEqual(len(comments), 40)
        self.assertFalse(state["是否完成"])
        self.assertIn("分页上限", state["未完成原因"])
        parent = "7696223715234874143"
        replies = await tiktok.get_comment_pages(self.client, "7692246277479812383", {}, 2, parent)
        self.assertEqual(len(replies), 3)
        self.assertEqual(next(iter(replies.values()))["父评论ID"], parent)
        self.assertEqual(next(iter(replies.values()))["直接回复目标ID"], "7696223792041280286")

    def prepare_preview_thread(self, reply_count=2):
        """使用用户确认的评论关系构造无认证信息的本地 HTTP 样本。"""
        author = tiktok.profile_info(FIXTURE["profile"])
        item = deepcopy(FIXTURE["posts"][0]["itemList"][0])
        item["stats"]["commentCount"] = 3
        doc = tiktok.make_document(item, author)
        root = deepcopy(FIXTURE["comments"][0]["comments"][0])
        root.update(aweme_id=item["id"], cid="7698152800593068830",
                    reply_id="0", reply_comment_total=reply_count)
        preview = deepcopy(FIXTURE["replies"]["comments"][0])
        preview.update(aweme_id=item["id"], cid="7698158871751934751",
                       reply_id=root["cid"], reply_to_reply_id="0",
                       text="Between 240-260 calories!", create_time=1792368000)
        added = deepcopy(preview)
        added.update(cid="7698166162283021086", text="Nice. Thanks for answering!",
                     reply_to_reply_id=preview["cid"], create_time=1792369012)
        root["reply_comment"] = [preview]
        Handler.responses[("/api/comment/list/", "0")] = {
            "status_code": 0, "comments": [root], "cursor": 20, "has_more": 0, "total": 3}
        Handler.responses[("/api/comment/list/reply/", "0")] = {
            "status_code": 0, "comments": [added], "cursor": 2, "has_more": 0, "total": 2}
        return doc, root, preview, added

    async def test_preview_plus_expanded_reply_matches_total(self):
        doc, root, preview, added = self.prepare_preview_thread()
        await tiktok.collect_comments(self.client, doc, 5)
        comment = doc["评论列表"][0]
        replies = comment["回复列表"]
        self.assertEqual([r["评论ID"] for r in replies], [preview["cid"], added["cid"]])
        self.assertEqual(replies[1]["直接回复目标ID"], preview["cid"])
        self.assertEqual(replies[1]["评论发布时间"], 1792369012)
        self.assertTrue(doc["采集状态"]["评论是否采集完成"])
        calls = [q for path, q in Handler.calls if path == "/api/comment/list/reply/"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["cursor"], ["0"])
        self.assertEqual(calls[0]["count"], ["3"])
        self.assertEqual(calls[0]["comment_id"], [root["cid"]])
        state = comment["回复采集状态"]
        self.assertEqual(state["预览回复ID列表"], [preview["cid"]])
        self.assertEqual(state["各页响应核对"][0]["返回评论ID列表"], [added["cid"]])
        self.assertEqual(state["合并后回复ID列表"], [preview["cid"], added["cid"]])
        self.assertEqual(state["未取得的直接回复目标ID列表"], [])

    async def test_reply_uses_returned_cursor_and_deduplicates_preview(self):
        doc, root, preview, added = self.prepare_preview_thread(reply_count=3)
        first = Handler.responses[("/api/comment/list/reply/", "0")]
        first.update(comments=[preview, added], cursor=9, has_more=1, total=3)
        last = deepcopy(added)
        last.update(cid="7698166162283021087", text="Third reply")
        Handler.responses[("/api/comment/list/reply/", "9")] = {
            "status_code": 0, "comments": [added, last], "cursor": 10, "has_more": 0, "total": 3}
        await tiktok.collect_comments(self.client, doc, 5)
        self.assertEqual(len(doc["评论列表"][0]["回复列表"]), 3)
        self.assertTrue(doc["采集状态"]["评论是否采集完成"])
        calls = [q for path, q in Handler.calls if path == "/api/comment/list/reply/"]
        self.assertEqual([q["cursor"][0] for q in calls], ["0", "9"])
        self.assertTrue(all(q["count"] == ["3"] for q in calls))
        pages = doc["评论列表"][0]["回复采集状态"]["各页响应核对"]
        self.assertEqual(pages[0]["返回评论ID列表"], [preview["cid"], added["cid"]])
        self.assertEqual(pages[0]["合并后去重数量"], 2)
        self.assertEqual(pages[1]["返回评论ID列表"], [added["cid"], last["cid"]])
        self.assertEqual(pages[1]["合并后去重数量"], 3)

    async def test_reply_trace_identifies_missing_direct_target_without_inventing_reply(self):
        doc, root, preview, added = self.prepare_preview_thread(reply_count=3)
        added["reply_to_reply_id"] = "7690397631290311437"
        response = Handler.responses[("/api/comment/list/reply/", "0")]
        response["total"] = 3
        await tiktok.collect_comments(self.client, doc, 5)
        comment = doc["评论列表"][0]
        state = comment["回复采集状态"]
        self.assertTrue(state["是否完成"])
        self.assertTrue(state["回复分页是否完成"])
        self.assertFalse(state["数量是否一致"])
        self.assertTrue(doc["采集状态"]["评论是否采集完成"])
        self.assertTrue(doc["采集状态"]["存在回复计数差异"])
        self.assertEqual(state["预览回复ID列表"], [preview["cid"]])
        self.assertEqual(state["各页响应核对"][0]["返回评论ID列表"], [added["cid"]])
        self.assertEqual(state["未取得的直接回复目标ID列表"], ["7690397631290311437"])
        self.assertEqual(len(comment["回复列表"]), 2)

    async def test_reply_count_difference_is_reported_but_account_flow_completes(self):
        # 用户已验证：页面只显示3条，根评论计数4，展开接口计数5且has_more=0。
        doc, root, preview, added = self.prepare_preview_thread(reply_count=4)
        root["reply_comment"] = []
        third = deepcopy(added)
        third["cid"] = "7696479769948799774"
        Handler.responses[("/api/comment/list/reply/", "0")] = {
            "status_code": 0, "comments": [preview, added, third], "cursor": 3, "has_more": 0, "total": 5}
        posts = deepcopy(FIXTURE["posts"][0])
        posts["itemList"] = [posts["itemList"][0]]
        posts["itemList"][0]["stats"]["commentCount"] = 4
        Handler.responses[("/api/post/item_list/", "0")] = posts
        author = doc["作者信息"]
        with TemporaryDirectory() as folder:
            output = Path(folder)
            index = await tiktok.collect_account(self.client, author, output, limit=1)
            self.assertTrue(index["是否完成"])
            self.assertEqual(index["回复计数差异作品数"], 1)
            record = index["作品文件"][0]
            self.assertTrue(record["是否完成"])
            self.assertTrue(record["存在回复计数差异"])
            saved = json.loads((output / author["用户名"] / record["文件名"]).read_text())
        state = saved["评论列表"][0]["回复采集状态"]
        self.assertTrue(state["是否完成"])
        self.assertTrue(state["回复分页是否完成"])
        self.assertEqual(state["已采集回复数"], 3)
        self.assertEqual(state["顶层评论回复计数"], 4)
        self.assertEqual(state["回复接口总计数"], 5)
        self.assertFalse(state["数量是否一致"])
        self.assertNotIn("未完成原因", state)

    async def test_reply_has_more_at_page_limit_stays_incomplete(self):
        doc, _, _, _ = self.prepare_preview_thread(reply_count=5)
        response = Handler.responses[("/api/comment/list/reply/", "0")]
        response.update(has_more=1, cursor=3, total=5)
        await tiktok.collect_comments(self.client, doc, max_pages=1)
        state = doc["评论列表"][0]["回复采集状态"]
        self.assertFalse(state["是否完成"])
        self.assertFalse(state["回复分页是否完成"])
        self.assertFalse(doc["采集状态"]["评论是否采集完成"])
        self.assertIn("达到评论分页上限", state["未完成原因"])

    async def test_reply_request_failure_stays_incomplete_despite_count_warning(self):
        doc, _, _, _ = self.prepare_preview_thread(reply_count=4)
        Handler.responses[("/api/comment/list/reply/", "0")] = b""
        await tiktok.collect_comments(self.client, doc, max_pages=2)
        state = doc["评论列表"][0]["回复采集状态"]
        self.assertFalse(state["是否完成"])
        self.assertFalse(state["回复分页是否完成"])
        self.assertFalse(state["数量是否一致"])
        self.assertIn("响应为空", state["未完成原因"])
        self.assertFalse(doc["采集状态"]["评论是否采集完成"])

    async def test_repeated_reply_cursor_stays_incomplete_despite_count_warning(self):
        doc, _, _, _ = self.prepare_preview_thread(reply_count=4)
        response = Handler.responses[("/api/comment/list/reply/", "0")]
        response.update(has_more=1, cursor=0, total=5)
        await tiktok.collect_comments(self.client, doc, max_pages=5)
        state = doc["评论列表"][0]["回复采集状态"]
        self.assertFalse(state["是否完成"])
        self.assertFalse(state["回复分页是否完成"])
        self.assertIn("游标缺失或重复", state["未完成原因"])
        self.assertFalse(doc["采集状态"]["评论是否采集完成"])

    async def test_preview_count_must_not_skip_first_reply_in_reordered_thread(self):
        # 用户对照：预览 A；cursor=1 返回 B,A；cursor=0 返回 C,B,A。
        doc, root, preview, added = self.prepare_preview_thread(reply_count=3)
        root["cid"] = "7689322928077570846"
        preview.update(cid="7689358022053413663", reply_id=root["cid"])
        added.update(cid="7690402533755650847", reply_id=root["cid"],
                     reply_to_reply_id="7690397631290311437")
        missing = deepcopy(preview)
        missing.update(cid="7690397631290311437", text="Missing reply")
        Handler.responses[("/api/comment/list/reply/", "1")] = {
            "status_code": 0, "comments": [added, preview], "cursor": 3, "has_more": 0, "total": 3}
        Handler.responses[("/api/comment/list/reply/", "0")] = {
            "status_code": 0, "comments": [missing, added, preview], "cursor": 3, "has_more": 0, "total": 3}
        await tiktok.collect_comments(self.client, doc, 5)
        comment = doc["评论列表"][0]
        self.assertEqual(len(comment["回复列表"]), 3)
        self.assertIn(missing["cid"], {r["评论ID"] for r in comment["回复列表"]})
        self.assertTrue(doc["采集状态"]["评论是否采集完成"])
        state = comment["回复采集状态"]
        self.assertEqual(state["起始游标"], "0")
        self.assertEqual(state["未取得的直接回复目标ID列表"], [])
        self.assertEqual(state["各页响应核对"][0]["返回评论ID列表"],
                         [missing["cid"], added["cid"], preview["cid"]])

    async def test_all_replies_in_preview_need_no_expansion(self):
        doc, _, _, _ = self.prepare_preview_thread(reply_count=1)
        await tiktok.collect_comments(self.client, doc, 5)
        self.assertTrue(doc["采集状态"]["评论是否采集完成"])
        self.assertEqual(len(doc["评论列表"][0]["回复列表"]), 1)
        self.assertFalse(any(path == "/api/comment/list/reply/" for path, _ in Handler.calls))

    async def test_reply_page_root_echo_is_excluded_without_creating_self_reply(self):
        doc, root, _, _ = self.prepare_preview_thread(reply_count=1)
        root["reply_comment"] = []
        echo = deepcopy(root)
        echo.update(reply_id=doc["视频信息"]["作品ID"], reply_to_reply_id="0")
        Handler.responses[("/api/comment/list/reply/", "0")] = {
            "status_code": 0, "comments": [echo], "cursor": 1, "has_more": 0, "total": 1}
        await tiktok.collect_comments(self.client, doc, 5)
        comment = doc["评论列表"][0]
        self.assertEqual(comment["评论ID"], root["cid"])
        self.assertEqual(comment["回复列表"], [])
        state = comment["回复采集状态"]
        self.assertTrue(state["回复分页是否完成"])
        self.assertTrue(doc["采集状态"]["评论是否采集完成"])
        self.assertFalse(state["数量是否一致"])
        self.assertEqual(state["排除的根评论回显"][0]["评论ID"], root["cid"])
        self.assertEqual(state["各页响应核对"][0]["返回评论ID列表"], [root["cid"]])

    async def test_reply_page_root_echo_and_real_reply_are_not_mixed(self):
        doc, root, _, added = self.prepare_preview_thread(reply_count=2)
        root["reply_comment"] = []
        echo = deepcopy(root)
        echo.update(reply_id=doc["视频信息"]["作品ID"], reply_to_reply_id="0")
        Handler.responses[("/api/comment/list/reply/", "0")] = {
            "status_code": 0, "comments": [echo, added], "cursor": 2, "has_more": 0, "total": 2}
        await tiktok.collect_comments(self.client, doc, 5)
        replies = doc["评论列表"][0]["回复列表"]
        self.assertEqual([r["评论ID"] for r in replies], [added["cid"]])
        self.assertEqual(replies[0]["父评论ID"], root["cid"])

    async def test_foreign_parent_is_still_rejected_not_treated_as_echo(self):
        doc, root, _, added = self.prepare_preview_thread(reply_count=2)
        root["reply_comment"] = []
        added["reply_id"] = "9999999999999999999"
        Handler.responses[("/api/comment/list/reply/", "0")] = {
            "status_code": 0, "comments": [added], "cursor": 1, "has_more": 0, "total": 1}
        await tiktok.collect_comments(self.client, doc, 5)
        state = doc["评论列表"][0]["回复采集状态"]
        self.assertFalse(state["是否完成"])
        self.assertIn("回复根评论 ID 与请求不符", state["未完成原因"])
        self.assertNotIn("排除的根评论回显", state)

    async def test_retry_incomplete_only_updates_pending_work_and_index(self):
        pending, root, _, _ = self.prepare_preview_thread(reply_count=1)
        root["reply_comment"] = []
        pending["评论列表"] = [tiktok.comment_info(root, pending["视频信息"]["作品ID"])]
        echo = deepcopy(root)
        echo.update(reply_id=pending["视频信息"]["作品ID"], reply_to_reply_id="0")
        Handler.responses[("/api/comment/list/reply/", "0")] = {
            "status_code": 0, "comments": [echo], "cursor": 1, "has_more": 0, "total": 1}
        author = pending["作者信息"]
        completed = tiktok.make_document(FIXTURE["posts"][0]["itemList"][1], author)
        completed["采集状态"]["评论是否采集完成"] = True
        with TemporaryDirectory() as folder:
            output = Path(folder)
            directory = output / author["用户名"]
            records = []
            for doc, done in [(completed, True), (pending, False)]:
                work = doc["视频信息"]["作品ID"]
                tiktok.save_json(directory / f"{work}.json", doc)
                records.append({"作品ID": work, "文件名": f"{work}.json", "是否完成": done})
            finished_path = directory / records[0]["文件名"]
            unchanged = finished_path.read_bytes()
            index_path = directory / "index.json"
            tiktok.save_json(index_path, {"作者信息": author, "作品文件": records, "实际作品数": 2,
                                          "作品列表采集状态": {"是否完成": True}, "是否完成": False})
            index = await tiktok.retry_incomplete_account(self.client, author, output)
            self.assertTrue(index["是否完成"])
            self.assertEqual(index["本次补采作品数"], 1)
            self.assertEqual(index["实际作品数"], 2)
            self.assertEqual(index["回复计数差异作品数"], 1)
            self.assertEqual(finished_path.read_bytes(), unchanged)
            before = index_path.read_bytes()
            calls_before = len(Handler.calls)
            await tiktok.retry_incomplete_account(self.client, author, output)
            self.assertEqual(index_path.read_bytes(), before)
            self.assertEqual(len(Handler.calls), calls_before)
        self.assertFalse(any(path == "/api/post/item_list/" for path, _ in Handler.calls))
        self.assertEqual(sum(path == "/api/comment/list/" for path, _ in Handler.calls), 1)

    async def test_retry_incomplete_rejects_path_traversal_before_network(self):
        author = tiktok.profile_info(FIXTURE["profile"])
        with TemporaryDirectory() as folder:
            output = Path(folder)
            tiktok.save_json(output / author["用户名"] / "index.json", {
                "作者信息": author, "作品文件": [{"作品ID": "123", "文件名": "../secret.json", "是否完成": False}]})
            before = len(Handler.calls)
            with self.assertRaises(tiktok.CollectionError):
                await tiktok.retry_incomplete_account(self.client, author, output)
            self.assertEqual(len(Handler.calls), before)

    async def test_no_preview_reply_starts_at_zero_with_count_three(self):
        doc, root, preview, added = self.prepare_preview_thread()
        root["reply_comment"] = []
        Handler.responses[("/api/comment/list/reply/", "0")] = {
            "status_code": 0, "comments": [preview, added], "cursor": 2, "has_more": 0, "total": 2}
        await tiktok.collect_comments(self.client, doc, 5)
        self.assertTrue(doc["采集状态"]["评论是否采集完成"])
        calls = [q for path, q in Handler.calls if path == "/api/comment/list/reply/"]
        self.assertEqual(calls[0]["cursor"], ["0"])
        self.assertEqual(calls[0]["count"], ["3"])

    async def test_cursor_zero_check_compares_ids_once_without_overwriting_work(self):
        doc, root, preview, added = self.prepare_preview_thread(reply_count=3)
        work = doc["视频信息"]["作品ID"]
        added["reply_to_reply_id"] = "7690397631290311437"
        comment = tiktok.comment_info(root, work)
        comment["预览回复ID列表"] = [preview["cid"]]
        comment["回复列表"] = [tiktok.comment_info(r, work, root["cid"]) for r in (preview, added)]
        doc["评论列表"] = [comment]
        missing = deepcopy(preview)
        missing["cid"] = "7690397631290311437"
        Handler.responses[("/api/comment/list/reply/", "0")] = {
            "status_code": 0, "comments": [preview, missing, added], "cursor": 3, "has_more": 0, "total": 3}
        author = doc["作者信息"]
        with TemporaryDirectory() as folder:
            output = Path(folder)
            path = output / author["用户名"] / f"{work}.json"
            tiktok.save_json(path, doc)
            before = path.read_bytes()
            index = path.parent / "index.json"
            index.write_text('{"保持不变":true}')
            with patch("builtins.input") as confirm:
                report = await tiktok.check_reply_cursor_zero(self.client, author, output, work, root["cid"])
            confirm.assert_not_called()
            self.assertTrue(report["是否核对成功"])
            self.assertEqual(report["新增回复ID列表"], [missing["cid"]])
            self.assertEqual(report["补回的直接回复目标ID列表"], [missing["cid"]])
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(index.read_text(), '{"保持不变":true}')
            self.assertTrue((output / "_diagnostics" / f"{root['cid']}-cursor-zero-control.json").exists())
        requests = [(path, query) for path, query in Handler.calls if path.startswith("/api/")]
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0][1]["cursor"], ["0"])
        self.assertEqual(requests[0][1]["count"], ["3"])

    async def test_cursor_zero_check_failure_does_not_prompt_or_retry(self):
        doc, root, _, _ = self.prepare_preview_thread()
        work = doc["视频信息"]["作品ID"]
        doc["评论列表"] = [tiktok.comment_info(root, work)]
        Handler.responses[("/api/comment/list/reply/", "0")] = b""
        self.client.manual = True
        author = doc["作者信息"]
        with TemporaryDirectory() as folder:
            output = Path(folder)
            tiktok.save_json(output / author["用户名"] / f"{work}.json", doc)
            with patch("builtins.input") as confirm:
                report = await tiktok.check_reply_cursor_zero(self.client, author, output, work, root["cid"])
            self.assertFalse(report["是否核对成功"])
            self.assertIn("响应为空", report["未完成原因"])
            confirm.assert_not_called()
        self.assertEqual(sum(path == "/api/comment/list/reply/" for path, _ in Handler.calls), 1)

    async def test_reply_parent_mismatch_preserves_safe_raw_relationship_evidence(self):
        doc, root, _, added = self.prepare_preview_thread()
        work = doc["视频信息"]["作品ID"]
        doc["评论列表"] = [tiktok.comment_info(root, work)]
        added["reply_id"] = 0
        Handler.responses[("/api/comment/list/reply/", "0")] = {
            "status_code": 0, "comments": [added], "cursor": 1, "has_more": 0, "total": 1}
        author = doc["作者信息"]
        with TemporaryDirectory() as folder:
            output = Path(folder)
            path = output / author["用户名"] / f"{work}.json"
            tiktok.save_json(path, doc)
            original = path.read_bytes()
            report = await tiktok.check_reply_cursor_zero(self.client, author, output, work, root["cid"])
            self.assertFalse(report["是否核对成功"])
            self.assertIn("回复根评论 ID 与请求不符", report["未完成原因"])
            evidence = report["原始回复关联核对"][0]
            self.assertEqual(evidence["reply_id"], "0")
            self.assertEqual(evidence["reply_id原始类型"], "int")
            self.assertEqual(evidence["aweme_id"], work)
            self.assertEqual(report["接口计数"], 1)
            self.assertEqual(path.read_bytes(), original)

    async def test_cursor_zero_count_control_changes_only_count_and_preserves_baseline(self):
        doc, root, preview, added = self.prepare_preview_thread(reply_count=5)
        work = doc["视频信息"]["作品ID"]
        comment = tiktok.comment_info(root, work)
        comment["回复列表"] = [tiktok.comment_info(added, work, root["cid"])]
        doc["评论列表"] = [comment]
        replies = [deepcopy(added) for _ in range(5)]
        for number, reply in enumerate(replies):
            reply["cid"] = str(9000000000000000000 + number)
        Handler.responses[("/api/comment/list/reply/", "0")] = {
            "status_code": 0, "comments": replies, "cursor": 5, "has_more": 0, "total": 5}
        author = doc["作者信息"]
        with TemporaryDirectory() as folder:
            output = Path(folder)
            path = output / author["用户名"] / f"{work}.json"
            tiktok.save_json(path, doc)
            original = path.read_bytes()
            baseline = output / "_diagnostics" / f"{root['cid']}-cursor-zero-control.json"
            tiktok.save_json(baseline, {"count": 3})
            report = await tiktok.check_reply_cursor_zero(self.client, author, output, work, root["cid"], count=20)
            self.assertTrue(report["是否核对成功"])
            self.assertEqual(report["实际去重条数"], 5)
            self.assertEqual(report["对照请求业务参数"]["count"], 20)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(json.loads(baseline.read_text()), {"count": 3})
            self.assertTrue((output / "_diagnostics" / f"{root['cid']}-cursor-zero-count20-control.json").exists())
        calls = [q for path, q in Handler.calls if path == "/api/comment/list/reply/"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["cursor"], ["0"])
        self.assertEqual(calls[0]["count"], ["20"])

    async def test_cursor_zero_check_rejects_unbounded_count_before_network(self):
        author = tiktok.profile_info(FIXTURE["profile"])
        before = len(Handler.calls)
        for count in (0, 101):
            with self.assertRaises(tiktok.CollectionError):
                await tiktok.check_reply_cursor_zero(self.client, author, Path("/unused"), "123", "456", count=count)
        self.assertEqual(len(Handler.calls), before)

    async def test_separate_work_files_and_index(self):
        author = tiktok.profile_info(FIXTURE["profile"])
        with TemporaryDirectory() as folder:
            # 前两条置顶作品样本不执行评论请求，验证落盘流程及独立结构。
            original = deepcopy(FIXTURE["posts"][0])
            try:
                for item in FIXTURE["posts"][0]["itemList"]:
                    item["stats"]["commentCount"] = 0
                index = await tiktok.collect_account(self.client, author, Path(folder), limit=2)
            finally:
                FIXTURE["posts"][0] = original
            directory = Path(folder) / "melimakesfaces"
            self.assertTrue(index["是否完成"])
            self.assertEqual(len(list(directory.glob("*.json"))), 3)
            for record in index["作品文件"]:
                doc = json.loads((directory / record["文件名"]).read_text())
                self.assertEqual(doc["视频信息"]["作品ID"], record["作品ID"])
                self.assertIn("作者信息", doc)

    async def test_transient_empty_response_recovers_after_five_seconds(self):
        Handler.responses[("/temporary", "0")] = lambda q: (
            b"" if sum(path == "/temporary" for path, _ in Handler.calls) == 1
            else {"status_code": 0})
        self.client.retry_delay = 5
        real_sleep = tiktok.asyncio.sleep
        waits = []

        async def sleep(seconds):
            waits.append(seconds)
            await real_sleep(0)

        with patch("tiktok.asyncio.sleep", side_effect=sleep), patch("builtins.input") as confirm:
            result = await self.client.request("/temporary", {"cursor": "0"})
        self.assertEqual(result["status_code"], 0)
        self.assertEqual([value for value in waits if value > 0], [5])
        self.assertFalse(self.client.stopped)
        confirm.assert_not_called()
        queries = [q for path, q in Handler.calls if path == "/temporary"]
        self.assertEqual(len(queries), 2)
        self.assertEqual(queries[0], queries[1])

    async def test_rate_limit_respects_longer_retry_after(self):
        Handler.responses[("/rate-limited", "0")] = {"status_code": 0}
        self.client.retry_delay = 5
        real_sleep = tiktok.asyncio.sleep
        waits = []

        async def sleep(seconds):
            waits.append(seconds)
            await real_sleep(0)

        with patch("tiktok.asyncio.sleep", side_effect=sleep):
            result = await self.client.request("/rate-limited", {})
        self.assertEqual(result["status_code"], 0)
        self.assertEqual([value for value in waits if value > 0], [7])

    async def test_transport_failure_recovers_without_manual_input(self):
        Handler.responses[("/temporary", "0")] = {"status_code": 0}
        attempts = []

        async def route_request(route):
            attempts.append(route.request.url)
            if len(attempts) == 1:
                await route.abort("failed")
            else:
                await route.continue_()

        await self.page.route("**/temporary?*", route_request)
        with patch("builtins.input") as confirm:
            result = await self.client.request("/temporary", {})
        self.assertEqual(result["status_code"], 0)
        self.assertEqual(len(attempts), 2)
        confirm.assert_not_called()

    async def test_passive_empty_first_page_is_not_automatically_replayed(self):
        self.client.require_initial_capture = True
        self.client.initial_posts["author"] = {"status": 200, "text": ""}
        with self.assertRaises(tiktok.CollectionError):
            await self.client.request("/api/post/item_list/", {"secUid": "author", "cursor": "0"})
        self.assertFalse(any(path == "/api/post/item_list/" for path, _ in Handler.calls))

    async def test_empty_response_stops_after_one_automatic_retry(self):
        with self.assertRaises(tiktok.CollectionError):
            await self.client.request("/empty", {})
        before = len(Handler.calls)
        with self.assertRaises(tiktok.CollectionError):
            await self.client.request("/empty", {})
        self.assertEqual(len(Handler.calls), before)

    async def test_http_403_logs_html_reason_and_content_type_without_retry(self):
        with patch("builtins.print") as output:
            with self.assertRaises(tiktok.CollectionError):
                await self.client.request("/forbidden", {})
        text = "\n".join(str(call.args[0]) for call in output.call_args_list)
        for expected in ('"HTTP状态": 403', "text/html", "Access Denied", "blocked by gateway"):
            self.assertIn(expected, text)
        self.assertEqual(sum(path == "/forbidden" for path, _ in Handler.calls), 1)
        self.assertTrue(self.client.stopped)

    async def test_manual_pause_retries_current_request_once(self):
        self.client.manual = True
        with patch("builtins.input", return_value="") as pause, patch("builtins.print") as output:
            result = await self.client.request("/recover", {"cursor": "20"})
        pause.assert_called_once()
        messages = "\n".join(str(call.args[0]) for call in output.call_args_list)
        self.assertIn("不代表页面已出现验证码", messages)
        self.assertNotIn("请在当前浏览器中手动完成验证码", messages)
        self.assertEqual(result["status_code"], 0)
        self.assertFalse(self.client.stopped)
        calls = [query for path, query in Handler.calls if path == "/recover"]
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(query["cursor"] == ["20"] for query in calls))

    async def test_manual_retry_failure_stops_and_does_not_loop(self):
        self.client.manual = True
        with patch("builtins.input", return_value="") as pause:
            with self.assertRaises(tiktok.CollectionError):
                await self.client.request("/empty", {})
            with self.assertRaises(tiktok.CollectionError):
                await self.client.request("/empty", {})
        pause.assert_called_once()
        self.assertTrue(self.client.stopped)
        self.assertEqual(sum(path == "/empty" for path, _ in Handler.calls), 3)

    async def test_multiple_requests_can_each_wait_for_terminal_confirmation(self):
        self.client.manual = True
        with patch("builtins.input", return_value="") as confirm:
            for cursor in ("0", "20"):
                result = await self.client.request("/recover", {"cursor": cursor})
                self.assertEqual(result["status_code"], 0)
        self.assertEqual(confirm.call_count, 2)
        self.assertEqual(self.client.manual_pauses, 2)
        self.assertFalse(self.client.stopped)

    async def test_terminal_eof_does_not_retry(self):
        self.client.manual = True
        with patch("builtins.input", side_effect=EOFError):
            with self.assertRaises(EOFError):
                await self.client.request("/empty", {})
        self.assertTrue(self.client.stopped)
        self.assertEqual(sum(path == "/empty" for path, _ in Handler.calls), 2)

    async def test_reply_count_difference_preserves_original_failure(self):
        author = tiktok.profile_info(FIXTURE["profile"])
        doc = tiktok.make_document(FIXTURE["posts"][0]["itemList"][0], author)
        raw = deepcopy(FIXTURE["comments"][0]["comments"][0])
        raw["aweme_id"] = doc["视频信息"]["作品ID"]
        comment = tiktok.comment_info(raw, raw["aweme_id"])
        comment["回复数"] = 3
        doc["评论列表"] = [comment]
        doc["互动数据"]["评论数"] = 1
        self.client.stopped = True
        await tiktok.collect_comments(self.client, doc, 1)
        state = doc["评论列表"][0]["回复采集状态"]
        self.assertIn("此前请求失败", state["未完成原因"])
        self.assertEqual(state["失败接口"], "/api/comment/list/reply/")
        self.assertEqual(state["返回数量差异"]["接口计数"], 3)

    async def test_manual_pause_budget_zero_disables_pause(self):
        self.client.manual = True
        self.client.max_manual_pauses = 0
        with patch("builtins.input", return_value="") as pause:
            with self.assertRaises(tiktok.CollectionError):
                await self.client.request("/empty", {})
        pause.assert_not_called()
        self.assertEqual(sum(path == "/empty" for path, _ in Handler.calls), 2)

    async def test_user_profile_cache_shared_between_works(self):
        comment = {"评论者信息": {"用户ID": "10", "用户名": "someone"}, "回复列表": []}
        cache = {}
        for _ in range(2):
            doc = {"评论列表": [deepcopy(comment), deepcopy(comment)], "采集状态": {}}
            await tiktok.enrich_users(self.client, doc, cache)
            self.assertEqual(doc["评论列表"][0]["评论者信息"]["粉丝数"], 2397)
        self.assertEqual(sum(path == "/api/user/detail/" for path, _ in Handler.calls), 1)

    async def test_profile_empty_response_is_scoped_and_comments_continue(self):
        Handler.responses[("/api/user/detail/", "0")] = b""
        entry = {"评论者信息": {"用户ID": "10", "用户名": "someone"}, "回复列表": []}
        doc = {"评论列表": [deepcopy(entry)], "采集状态": {}}
        await tiktok.enrich_users(self.client, doc, {})
        self.assertFalse(self.client.stopped)
        self.assertFalse(doc["采集状态"]["评论区用户资料是否完成"])
        self.assertTrue(doc["采集状态"]["用户资料接口状态"]["是否暂停"])
        state = doc["评论列表"][0]["用户资料采集状态"]
        self.assertEqual(state["请求诊断"]["响应字符数"], 0)
        self.assertEqual(state["请求诊断"]["HTTP状态"], 200)
        self.assertEqual(state["请求诊断"]["用户名"], "someone")
        self.assertEqual(doc["评论列表"][0]["评论者信息"], entry["评论者信息"])
        with self.assertRaises(tiktok.CollectionError):
            await self.client.request("/api/user/detail/", {"uniqueId": "another"})
        comments = await tiktok.get_comment_pages(self.client, "7692246277479812383", {}, 1)
        self.assertTrue(comments)
        self.assertEqual(sum(path == "/api/user/detail/" for path, _ in Handler.calls), 1)

    async def test_profile_uses_direct_http_with_shared_cookies_and_no_page_fetch(self):
        await self.page.context.add_cookies([{"name": "test_session", "value": "local-only",
                                             "domain": "127.0.0.1", "path": "/"}])
        original_url = self.page.url
        with patch.object(self.page, "evaluate", side_effect=AssertionError("不应调用页面 fetch")), \
                patch.object(self.page, "goto", side_effect=AssertionError("不应跳转页面")):
            data = await self.client.request("/api/user/detail/", {"uniqueId": "someone", "secUid": ""})
        self.assertEqual(data["userInfo"]["user"]["uniqueId"], "someone")
        self.assertEqual(self.page.url, original_url)
        self.assertTrue(Handler.received_test_cookie)
        self.assertEqual(self.client.diagnostics["/api/user/detail/"]["传输方式"], "直接 HTTP 请求")
        self.assertFalse(any(path.startswith("/@") for path, _ in Handler.calls))

    async def test_empty_detail_reads_html_over_http_without_navigation(self):
        Handler.responses[("/api/user/detail/", "0")] = b""
        info = deepcopy(FIXTURE["profile"])
        info["user"].update(id="10", uniqueId="someone")
        payload = {"__DEFAULT_SCOPE__": {"webapp.user-detail": {"userInfo": info}}}
        html = '<html><script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'
        Handler.responses[("/@someone", "0")] = (html + json.dumps(payload) + '</script></html>').encode()
        original_url = self.page.url
        entry = {"评论者信息": {"用户ID": "10", "用户名": "someone"}, "回复列表": []}
        doc = {"评论列表": [entry], "采集状态": {}}
        with patch.object(self.page, "evaluate", side_effect=AssertionError("不应调用页面 fetch")), \
                patch.object(self.page, "goto", side_effect=AssertionError("不应跳转页面")):
            await tiktok.enrich_users(self.client, doc, {})
        self.assertTrue(doc["采集状态"]["评论区用户资料是否完成"])
        self.assertEqual(entry["评论者信息"]["粉丝数"], 2397)
        self.assertEqual(self.page.url, original_url)
        self.assertIsNone(self.client.profile_failure)
        diagnostic = self.client.diagnostics["/api/user/detail/"]
        self.assertEqual(diagnostic["资料来源"], "HTTP 主页内嵌资料")
        self.assertEqual(diagnostic["HTTP主页补充"]["HTTP状态"], 200)
        self.assertEqual(sum(path == "/@someone" for path, _ in Handler.calls), 1)

    async def test_business_error_does_not_fallback_to_html(self):
        Handler.responses[("/api/user/detail/", "0")] = {"status_code": 5, "status_msg": ""}
        with self.assertRaises(tiktok.CollectionError):
            await self.client.request("/api/user/detail/", {"uniqueId": "someone", "secUid": ""})
        self.assertFalse(any(path.startswith("/@") for path, _ in Handler.calls))

    async def test_http_html_wrong_username_is_not_merged(self):
        Handler.responses[("/api/user/detail/", "0")] = b""
        info = deepcopy(FIXTURE["profile"])
        payload = {"__DEFAULT_SCOPE__": {"webapp.user-detail": {"userInfo": info}}}
        Handler.responses[("/@someone", "0")] = (
            '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">' + json.dumps(payload) + '</script>').encode()
        summary = {"用户ID": "10", "用户名": "someone", "昵称": "原昵称"}
        doc = {"评论列表": [{"评论者信息": deepcopy(summary), "回复列表": []}], "采集状态": {}}
        await tiktok.enrich_users(self.client, doc, {})
        self.assertEqual(doc["评论列表"][0]["评论者信息"], summary)
        self.assertFalse(doc["采集状态"]["评论区用户资料是否完成"])

    async def test_manual_profile_retry_failure_prints_and_only_pauses_profiles(self):
        Handler.responses[("/api/user/detail/", "0")] = b""
        self.client.manual = True
        with patch("builtins.input", return_value="") as confirm, patch("builtins.print") as output:
            with self.assertRaises(tiktok.CollectionError):
                await self.client.request("/api/user/detail/", {"uniqueId": "someone", "secUid": ""})
        confirm.assert_called_once()
        text = "\n".join(str(call.args[0]) for call in output.call_args_list)
        self.assertIn("重试失败 [/api/user/detail/]", text)
        self.assertIn("继续其他作品的评论采集", text)
        self.assertFalse(self.client.stopped)
        self.assertEqual(sum(path == "/api/user/detail/" for path, _ in Handler.calls), 2)

    async def test_paused_profile_endpoint_still_reuses_successful_cache(self):
        self.client.profile_failure = {"失败用户名": "failed", "失败原因": "空响应", "请求诊断": {}}
        author = tiktok.profile_info(FIXTURE["profile"])
        entry = {"评论者信息": {"用户ID": author["用户ID"], "用户名": author["用户名"]}, "回复列表": []}
        doc = {"评论列表": [entry], "采集状态": {}}
        await tiktok.enrich_users(self.client, doc, {author["用户ID"]: (author, None)})
        self.assertTrue(doc["采集状态"]["评论区用户资料是否完成"])
        self.assertEqual(entry["评论者信息"]["粉丝数"], author["粉丝数"])
        self.assertFalse(any(path == "/api/user/detail/" for path, _ in Handler.calls))

    async def test_profile_identity_mismatch_is_not_merged_or_global_stop(self):
        summary = {"用户ID": "999", "用户名": "someone", "昵称": "原评论昵称"}
        doc = {"评论列表": [{"评论者信息": deepcopy(summary), "回复列表": []}], "采集状态": {}}
        await tiktok.enrich_users(self.client, doc, {})
        entry = doc["评论列表"][0]
        self.assertEqual(entry["评论者信息"], summary)
        self.assertFalse(entry["用户资料采集状态"]["是否完成"])
        self.assertIn("用户资料 ID", entry["用户资料采集状态"]["未完成原因"])
        self.assertFalse(self.client.stopped)
        self.assertIsNone(self.client.profile_failure)

    async def test_account_collects_comments_without_requesting_commenter_profiles(self):
        page = deepcopy(FIXTURE["posts"][0])
        page["itemList"] = page["itemList"][:2]
        for item in page["itemList"]:
            item["stats"]["commentCount"] = 1
        Handler.responses[("/api/post/item_list/", "0")] = page

        def comments_for_work(query):
            raw = deepcopy(FIXTURE["comments"][0]["comments"][0])
            raw.update(aweme_id=query["aweme_id"][0], reply_comment_total=0, reply_comment=[])
            raw["user"].update(uid=query["aweme_id"][0], unique_id="someone")
            return {"status_code": 0, "comments": [raw], "total": 1, "cursor": 20, "has_more": 0}

        Handler.responses[("/api/comment/list/", "0")] = comments_for_work
        Handler.responses[("/api/user/detail/", "0")] = b""
        author = tiktok.profile_info(FIXTURE["profile"])
        with TemporaryDirectory() as folder:
            index = await tiktok.collect_account(self.client, author, Path(folder), limit=2)
            self.assertTrue(index["是否完成"])
            self.assertFalse(index["评论者主页资料采集"]["是否启用"])
            self.assertNotIn("用户资料接口状态", index)
            for row in index["作品文件"]:
                doc = json.loads((Path(folder) / author["用户名"] / row["文件名"]).read_text())
                self.assertTrue(doc["采集状态"]["评论是否采集完成"])
                self.assertFalse(doc["采集状态"]["评论者主页资料采集"]["是否启用"])
                self.assertNotIn("评论区用户资料是否完成", doc["采集状态"])
                self.assertEqual(len(doc["评论列表"]), 1)
                self.assertIsNotNone(doc["评论列表"][0]["评论发布时间"])
                self.assertEqual(doc["评论列表"][0]["评论者信息"]["用户名"], "someone")
                self.assertNotIn("用户资料采集状态", doc["评论列表"][0])
                self.assertEqual(doc["作者信息"]["用户ID"], author["用户ID"])
        self.assertEqual(sum(path == "/api/comment/list/" for path, _ in Handler.calls), 2)
        self.assertFalse(any(path == "/api/user/detail/" or path.startswith("/@")
                             for path, _ in Handler.calls))
        self.assertFalse(self.client.stopped)

    async def test_unrelated_api_cannot_pollute_app_params_or_leak_secrets(self):
        self.client.observe(SimpleNamespace(url="https://www.tiktok.com/api/comment/list/?aid=1988&app_language=zh-Hans"))
        self.client.observe(SimpleNamespace(url="https://www.tiktok.com/api/unrelated/?aid=1284&app_language=ja"))
        self.assertEqual(self.client.common["aid"], "1988")
        self.assertEqual(self.client.common["app_language"], "zh-Hans")
        self.client.observe(SimpleNamespace(url="https://www.tiktok.com/api/user/detail/?aid=1988&app_language=en&msToken=SECRET"))
        self.client.observe(SimpleNamespace(url="https://www.tiktok.com/api/comment/list/?aid=1284&app_language=zh-Hans"))
        await self.client.request("/api/user/detail/", {"uniqueId": "someone", "secUid": ""})
        calls = [q for path, q in Handler.calls if path == "/api/user/detail/"]
        self.assertEqual(calls[0]["aid"], ["1988"])
        self.assertEqual(calls[0]["app_language"], ["en"])
        self.assertNotIn("msToken", calls[0])
        diagnostic = json.dumps(self.client.diagnostics)
        self.assertNotIn("SECRET", diagnostic)
        self.assertNotIn("msToken", diagnostic)

    async def test_initial_response_is_captured_without_duplicate_request(self):
        author = tiktok.profile_info(FIXTURE["profile"])
        self.client.require_initial_capture = True
        self.page.on("response", self.client.observe_response)
        await self.page.evaluate("""async secUid => {
            const url = new URL('/api/post/item_list/', location.origin);
            url.searchParams.set('secUid', secUid);
            url.searchParams.set('cursor', '0');
            await fetch(url.toString());
        }""", author["用户secUid"])
        self.assertTrue(await self.client.wait_initial_posts(author["用户secUid"], timeout=2))
        before = len(Handler.calls)
        state = {}
        posts = await tiktok.get_posts(self.client, author, 5, state, 10)
        self.assertEqual(len(posts), 5)
        self.assertEqual(len(Handler.calls), before)
        self.assertEqual(state["各页数据来源"], ["主页自然响应，未重复请求"])

    async def test_missing_initial_capture_does_not_send_request(self):
        self.client.require_initial_capture = True
        before = len(Handler.calls)
        author = tiktok.profile_info(FIXTURE["profile"])
        state = {}
        posts = await tiktok.get_posts(self.client, author, 5, state, 10)
        self.assertEqual(posts, [])
        self.assertEqual(len(Handler.calls), before)
        self.assertIn("未重复发送首批请求", state["未完成原因"])


if __name__ == "__main__":
    unittest.main()
