import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs

import instagram


FIXTURE = json.loads((Path(__file__).parent / "fixtures/browser_api.json").read_text())


class Handler(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *args):
        pass  # 本地测试服务器不记录请求头或表单令牌。

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/v1/media/"):
            self.calls.append(("info", self.path))
            self.send_json(FIXTURE["info"])
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b'<html><body><input name="fb_dtsg" value="local-test-token"></body></html>')

    def do_POST(self):
        form = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
        name = form["fb_api_req_friendly_name"][0]
        variables = json.loads(form["variables"][0])
        self.calls.append((name, variables))
        if name == instagram.QUERIES["profile"][0]:
            user_id = variables["id"]
            if user_id == "999":
                self.send_json({"status": "fail", "message": "PRIVATE_RESPONSE"}, status=429)
                return
            response = {"data": {"user": {
                "pk": user_id, "id": user_id,
                "username": "quangphamvs660" if user_id == "11792660243" else "local_user",
                "full_name": "本地用户昵称",
                "biography": "本地作者资料样本", "follower_count": 3764,
                "following_count": 2, "media_count": 346,
                "hd_profile_pic_url_info": {"url": "https://example.test/avatar.jpg"},
            }}}
        elif name == instagram.QUERIES["initial"][0]:
            response = FIXTURE["pages"][0]
        elif name == instagram.QUERIES["comments"][0]:
            response = FIXTURE["pages"][1]
        elif name == instagram.QUERIES["replies"][0]:
            response = FIXTURE["replies"]
        else:
            self.send_error(501)
            return
        self.send_json(response)


class ParsingTests(unittest.TestCase):
    def test_shortcode_known_samples(self):
        self.assertEqual(instagram.parse_link("https://www.instagram.com/reel/DdO6y3MSSGk/")[1],
                         "3985381302202868132")
        self.assertEqual(instagram.parse_link("https://www.instagram.com/reel/DeeC-GcJumf/?x=1")[1],
                         "4007653782021663135")

    def test_reject_foreign_links(self):
        for link in ("https://evil.test/reel/abc/", "https://www.instagram.com@evil.test/reel/abc/",
                     "https://www.instagram.com/user/", "http://instagram.com/reel/abc/"):
            with self.assertRaises(ValueError):
                instagram.parse_link(link)

    def test_response_prefix_and_error_redaction(self):
        self.assertEqual(instagram.decode_response({"status": 200, "text": 'for (;;);{"status":"ok"}'}),
                         {"status": "ok"})
        with self.assertRaises(instagram.CollectionError) as error:
            instagram.decode_response({"status": 200, "text": '{"errors":[{"message":"SECRET"}]} '})
        self.assertNotIn("SECRET", str(error.exception))

    def test_wrong_media_rejected(self):
        with self.assertRaises(instagram.CollectionError):
            instagram.select_media(FIXTURE["info"], "3985381302202868132", "DdO6y3MSSGk")

    def test_zero_and_null_preserved(self):
        value = instagram.comment_info({"pk": "1", "comment_like_count": 0, "is_edited": False})
        self.assertEqual(value["comment_like_count"], 0)
        self.assertIs(value["is_edited"], False)
        self.assertIsNone(value["parent_comment_id"])

    def test_comment_timestamp_is_preserved(self):
        comment = instagram.comment_info({"pk": "1", "created_at": 1791006576})
        reply = instagram.comment_info({"pk": "2", "created_at": 1791009783,
                                        "parent_comment_id": "1"})
        self.assertEqual(comment["created_at"], 1791006576)
        self.assertEqual(reply["created_at"], 1791009783)

    def test_chinese_output_preserves_nested_data(self):
        source = {
            "video": {"pk": "3985381302202868132", "taken_at": 1789314558},
            "plays": {"play_count": 0, "view_count": None},
            "comments": [{"pk": "1", "created_at": 1791006576,
                          "author": {"pk": "10", "username": "test"},
                          "replies": [{"pk": "2", "created_at": 1791009783,
                                       "author": {"pk": "11"}, "parent_comment_id": "1"}]}],
            "collection": {"comments_complete": False},
            "new_metric": 42,
        }
        result = instagram.chinese_output(source)
        self.assertEqual(result["视频信息"]["作品ID"], "3985381302202868132")
        self.assertEqual(result["视频信息"]["视频发布时间"], 1789314558)
        self.assertEqual(result["播放量数据"]["播放量"], 0)
        self.assertIsNone(result["播放量数据"]["浏览计数（view_count）"])
        comment = result["评论列表"][0]
        self.assertEqual(comment["评论发布时间"], 1791006576)
        self.assertEqual(comment["评论者信息"]["用户ID"], "10")
        self.assertEqual(comment["回复列表"][0]["回复发布时间"], 1791009783)
        self.assertEqual(comment["回复列表"][0]["回复者信息"]["用户ID"], "11")
        self.assertIs(result["采集状态"]["评论是否采集完成"], False)
        self.assertEqual(result["接口字段（new_metric）"], 42)
        self.assertIn("comments", source)

    def test_profile_merge_checks_identity_and_preserves_values(self):
        summary = {"pk": "10", "username": "owner", "full_name": "原昵称",
                   "profile_pic_url": "original.jpg"}
        profile = {"id": "10", "biography": "", "follower_count": 0,
                   "following_count": 0, "is_business": False, "full_name": None,
                   "hd_profile_pic_url_info": {"url": "hd.jpg"}}
        merged = instagram.merge_author(summary, profile, "10")
        self.assertEqual(merged["full_name"], "原昵称")
        self.assertEqual(merged["biography"], "")
        self.assertEqual(merged["follower_count"], 0)
        self.assertIs(merged["is_business"], False)
        self.assertEqual(merged["profile_pic_url_hd"], "hd.jpg")
        with self.assertRaises(instagram.CollectionError):
            instagram.merge_author(summary, {"id": "11"}, "10")



class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright
        Handler.calls = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(channel="chrome", headless=True)
        self.page = await self.browser.new_page()
        await self.page.goto(f"http://127.0.0.1:{self.server.server_port}/")
        self.client = instagram.BrowserClient(self.page, delay=0)

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    async def test_real_browser_fetch_pagination_and_replies(self):
        media_id, code = "4007653782021663135", "DeeC-GcJumf"
        data = await self.client.request(f"/api/v1/media/{media_id}/info/")
        media = instagram.select_media(data, media_id, code)
        result = instagram.make_result(media, code)
        await instagram.collect_author(self.client, media, result)
        self.assertEqual(result["author"]["follower_count"], 3764)
        self.assertEqual(result["author"]["media_count"], 346)
        self.assertTrue(result["collection"]["author_profile"]["complete"])
        await instagram.collect_comments(self.client, media, result, max_pages=5)
        self.assertEqual(result["plays"]["play_count"], 716)
        self.assertEqual(len(result["comments"]), 30)
        self.assertEqual(len(result["comments"][0]["replies"]), 7)
        self.assertTrue(result["collection"]["comments_complete"])
        calls = dict(Handler.calls)
        self.assertEqual(calls[instagram.QUERIES["comments"][0]]["after"], "mock-next")
        self.assertEqual(calls[instagram.QUERIES["replies"][0]]["parent_comment_id"],
                         "18114126323326624")
        with TemporaryDirectory() as directory:
            path = instagram.save_result(result, Path(directory), code)
            saved = json.loads(path.read_text())
            self.assertEqual(saved["视频信息"]["作品短码"], code)
            self.assertEqual(len(saved["评论列表"][0]["回复列表"]), 7)
            self.assertIn("评论发布时间", saved["评论列表"][0])
            self.assertEqual(saved["作者信息"]["粉丝数"], 3764)
            self.assertEqual(calls[instagram.QUERIES["profile"][0]]["id"], "11792660243")

    async def test_page_limit_keeps_partial_and_marks_incomplete(self):
        media = FIXTURE["info"]["items"][0]
        result = instagram.make_result(media, media["code"])
        await instagram.collect_comments(self.client, media, result, max_pages=1)
        self.assertEqual(len(result["comments"]), 15)
        self.assertFalse(result["collection"]["comments_complete"])
        self.assertEqual(result["collection"]["errors"][0]["stage"], "comments")

    async def test_commenter_and_reply_profiles_are_deduplicated(self):
        media = FIXTURE["info"]["items"][0]
        result = instagram.make_result(media, media["code"])
        await instagram.collect_author(self.client, media, result)
        result["comments"] = [
            {"author": {"pk": "10"}, "created_at": 123, "replies": [
                {"author": {"pk": "10"}, "created_at": 124},
                {"author": {"pk": "11792660243"}, "created_at": 125}]},
            {"author": {"pk": "11"}, "created_at": 126, "replies": []},
        ]
        await instagram.collect_commenter_profiles(self.client, result)
        requested = [v["id"] for name, v in Handler.calls
                     if name == instagram.QUERIES["profile"][0]]
        self.assertEqual(requested, ["11792660243", "10", "11"])
        self.assertTrue(result["collection"]["commenter_profiles"]["complete"])
        self.assertEqual(result["collection"]["commenter_profiles"]["fetched_users"], 3)
        comment = result["comments"][0]
        self.assertEqual(comment["author"]["follower_count"], 3764)
        self.assertEqual(comment["replies"][0]["author"]["full_name"], "本地用户昵称")
        self.assertEqual(comment["created_at"], 123)
        self.assertEqual(comment["replies"][0]["created_at"], 124)
        saved = instagram.chinese_output(result)
        self.assertTrue(saved["评论列表"][0]["用户资料采集状态"]["是否完成"])
        self.assertEqual(saved["评论列表"][0]["回复列表"][0]["回复者信息"]["粉丝数"], 3764)

    async def test_rate_limit_preserves_comments_and_stops_profile_requests(self):
        media = FIXTURE["info"]["items"][0]
        result = instagram.make_result(media, media["code"])
        result["comments"] = [
            {"text": "保留评论", "author": {"pk": "999", "username": "original"}},
            {"text": "后续评论", "author": {"pk": "10"}},
        ]
        await instagram.collect_commenter_profiles(self.client, result)
        requested = [v["id"] for name, v in Handler.calls
                     if name == instagram.QUERIES["profile"][0]]
        self.assertEqual(requested, ["999"])
        self.assertEqual(result["comments"][0]["text"], "保留评论")
        self.assertEqual(result["comments"][0]["author"]["username"], "original")
        self.assertFalse(result["collection"]["commenter_profiles"]["complete"])
        self.assertEqual(result["collection"]["commenter_profiles"]["failed_users"], 2)
        self.assertNotIn("PRIVATE_RESPONSE", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
