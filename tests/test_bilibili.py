import unittest

from bilibili import (
    Cooldown,
    VideoTarget,
    card_targets,
    parse_targets,
    video_template_values,
)


class ParsingTests(unittest.TestCase):
    def test_long_short_and_standalone_targets(self):
        text = (
            "https://www.bilibili.com/video/BV1ii421Q7oj?p=2，"
            " https://b23.tv/abc?p=3 AV123 BV1ii421Q7oj"
        )
        self.assertEqual(
            parse_targets(text),
            [VideoTarget("BV1ii421Q7oj", 2), VideoTarget("https://b23.tv/abc", 3),
             VideoTarget("av123")],
        )

    def test_lookalike_domains_and_embedded_ids_are_ignored(self):
        self.assertEqual(
            parse_targets("https://evil.example/b23.tv/abc abcBV1ii421Q7oj 字av123字"),
            [],
        )

    def test_card_prefers_jump_url_over_preview_and_ignores_bare_ids(self):
        card = {"meta": {"detail_1": {
            "desc": "BV1ii421Q7oj",
            "preview": "https://www.bilibili.com/video/BV1ii421Q7oj",
            "qqdocurl": "https://b23.tv/abc",
        }}}
        self.assertEqual(card_targets(card), [VideoTarget("https://b23.tv/abc")])

    def test_card_can_handle_encoded_json(self):
        self.assertEqual(
            card_targets(r'{"meta":{"detail_1":{"qqdocurl":"https:\/\/b23.tv\/abc"}}}'),
            [VideoTarget("https://b23.tv/abc")],
        )

    def test_cooldown_is_per_session_and_part_and_releases_failures(self):
        cooldown = Cooldown(180)
        video = VideoTarget("BV1ii421Q7oj")
        self.assertTrue(cooldown.reserve("room1", video, 1))
        self.assertFalse(cooldown.reserve("room1", video, 2))
        self.assertTrue(cooldown.reserve("room2", video, 2))
        self.assertTrue(cooldown.reserve("room1", VideoTarget(video.reference, 2), 2))
        cooldown.release("room1", video)
        self.assertTrue(cooldown.reserve("room1", video, 3))
        self.assertTrue(cooldown.reserve("room1", video, 183))

    def test_template_uses_real_part_and_preserves_video_page_link(self):
        data = {
            "aid": 123, "bvid": "BV1ii421Q7oj", "title": "测试",
            "desc": "一二三四", "pic": "//i0.hdslb.com/cover.jpg", "owner": {"name": "UP"},
            "stat": {"view": 15300}, "pages": [{"page": 1}, {"page": 2, "part": "第二集"}],
        }
        result = video_template_values(data, 2, "av", 3)
        self.assertEqual(result["分P"], "第二集")
        self.assertEqual(result["视频地址"], "https://www.bilibili.com/video/av123?p=2")
        self.assertEqual(result["简介"], "一二三…")
        self.assertEqual(result["观看"], "1.5万")
        self.assertEqual(result["封面"], "https://i0.hdslb.com/cover.jpg")

    def test_out_of_range_part_is_reported(self):
        with self.assertRaisesRegex(ValueError, "分 P"):
            video_template_values({
                "bvid": "BV1ii421Q7oj", "title": "测试", "pages": [{"page": 1}],
            }, 2)
