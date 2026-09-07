import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# 测试文件直接运行时，也能从仓库根目录导入 app 包。
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.services import subtitle


class TestSubtitleService(unittest.TestCase):
    def test_file_to_subtitles_returns_empty_for_missing_input(self):
        """空路径和不存在的文件都应安全返回空列表。"""
        self.assertEqual(subtitle.file_to_subtitles(""), [])
        with tempfile.TemporaryDirectory() as tmp_dir:
            missing_file = Path(tmp_dir) / "missing.srt"
            self.assertEqual(subtitle.file_to_subtitles(str(missing_file)), [])

    def test_levenshtein_distance_and_similarity_cover_common_boundaries(self):
        """
        字幕校正依赖编辑距离选择是否继续合并相邻字幕，因此覆盖空字符串、
        参数交换、大小写忽略和明显不相似四种边界，防止算法调整后误合并。
        """
        self.assertEqual(subtitle.levenshtein_distance("kitten", "sitting"), 3)
        self.assertEqual(subtitle.levenshtein_distance("a", "longer"), 6)
        self.assertEqual(subtitle.levenshtein_distance("hello", ""), 5)
        self.assertEqual(subtitle.similarity("Hello", "hello"), 1.0)
        self.assertLess(subtitle.similarity("hello", "world"), 0.5)

    def test_create_returns_empty_when_whisper_is_unavailable(self):
        """可选 Whisper 依赖未安装时应跳过，而不是在任务线程中抛异常。"""
        with patch.object(subtitle, "WhisperModel", None):
            self.assertEqual(subtitle.create("audio.mp3"), "")

    def test_create_returns_none_when_whisper_model_cannot_load(self):
        """模型下载或初始化失败时必须返回失败结果，并允许任务层更新状态。"""
        with patch.object(subtitle, "model", None), patch.object(
            subtitle,
            "WhisperModel",
            side_effect=RuntimeError("model unavailable"),
        ):
            self.assertIsNone(subtitle.create("audio.mp3"))

    def test_create_writes_punctuated_and_trailing_segments(self):
        """
        使用假的 Whisper 模型覆盖逐词时间戳处理，不访问网络也不加载真实模型。
        一个 segment 同时包含标点断句和末尾无标点文本，可验证两条关键写入路径。
        """

        class _FakeWhisperModel:
            def __init__(self, **kwargs):
                self.init_kwargs = kwargs

            def transcribe(self, audio_file, **kwargs):
                words = [
                    SimpleNamespace(start=0.0, end=0.4, word="Hello"),
                    SimpleNamespace(start=0.4, end=0.9, word=" world."),
                    SimpleNamespace(start=1.0, end=1.5, word="Again"),
                ]
                segment = SimpleNamespace(
                    start=0.0,
                    end=1.8,
                    words=words,
                )
                info = SimpleNamespace(language="en", language_probability=0.99)
                return [segment], info

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "generated.srt"
            with patch.object(subtitle, "model", None), patch.object(
                subtitle,
                "WhisperModel",
                _FakeWhisperModel,
            ):
                subtitle.create("audio.mp3", str(subtitle_file))

            items = subtitle.file_to_subtitles(str(subtitle_file))

        self.assertEqual([item[2] for item in items], ["Hello world", "Again"])

    def test_correct_ignores_markdown_separator_lines(self):
        """
        Whisper fallback 校正阶段也必须忽略 `---` 这类不可发声脚本行。

        如果这里继续保留 Markdown 分隔符，`correct()` 会认为脚本行数多于
        字幕行数，并补出 `00:00:00,000 --> 00:00:00,000`，剪辑软件会把
        生成的 SRT 判定为不可导入。
        """
        original_srt = (
            "1\n"
            "00:00:00,100 --> 00:00:01,000\n"
            "第一段\n\n"
            "2\n"
            "00:00:01,100 --> 00:00:02,000\n"
            "第二段\n\n"
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            subtitle_file.write_text(original_srt, encoding="utf-8")

            subtitle.correct(
                subtitle_file=str(subtitle_file),
                video_script="第一段\n---\n第二段",
            )

            corrected_srt = subtitle_file.read_text(encoding="utf-8")

        self.assertIn("第一段", corrected_srt)
        self.assertIn("第二段", corrected_srt)
        self.assertNotIn("---", corrected_srt)
        self.assertNotIn("00:00:00,000 --> 00:00:00,000", corrected_srt)

    def test_correct_merges_adjacent_subtitles_for_one_script_sentence(self):
        """
        Whisper 可能把一句文案拆成多个时间块。校正逻辑应合并时间范围并恢复
        原始脚本文本，避免最终字幕出现不必要的碎片。
        """
        original_srt = (
            "1\n00:00:00,100 --> 00:00:01,000\nHello\n\n"
            "2\n00:00:01,000 --> 00:00:02,000\nworld\n\n"
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            subtitle_file.write_text(original_srt, encoding="utf-8")

            subtitle.correct(str(subtitle_file), "Hello world")
            items = subtitle.file_to_subtitles(str(subtitle_file))

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0][1], "00:00:00,100 --> 00:00:02,000")
        self.assertEqual(items[0][2], "Hello world")

    def test_correct_replaces_mismatch_and_appends_missing_script_line(self):
        """
        转写结果与脚本完全不一致时仍应以脚本为准；脚本多出的句子没有可复用
        时间轴时使用明确的零时间占位，避免丢失文本且保持现有兼容行为。
        """
        original_srt = "1\n00:00:00,100 --> 00:00:01,000\nWrong text\n\n"

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            subtitle_file.write_text(original_srt, encoding="utf-8")

            subtitle.correct(str(subtitle_file), "Expected sentence. Extra sentence.")
            items = subtitle.file_to_subtitles(str(subtitle_file))

        self.assertEqual(
            [item[2] for item in items],
            ["Expected sentence", "Extra sentence"],
        )
        self.assertEqual(items[1][1], "00:00:00,000 --> 00:00:00,000")

    def test_file_to_subtitles_keeps_last_block_without_trailing_newline(self):
        """
        The final subtitle must be parsed even when the SRT file does not end
        with a trailing blank line. Many tools omit it, and previously the last
        block was silently dropped because only a blank line flushed a block.
        """
        srt_without_trailing_blank = (
            "1\n"
            "00:00:00,000 --> 00:00:01,000\n"
            "Hello\n\n"
            "2\n"
            "00:00:01,000 --> 00:00:02,000\n"
            "World"
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            subtitle_file.write_text(srt_without_trailing_blank, encoding="utf-8")

            items = subtitle.file_to_subtitles(str(subtitle_file))

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0][2], "Hello")
        self.assertEqual(items[1][2], "World")

    def test_file_to_subtitles_parses_blocks_with_trailing_newline(self):
        """A normal SRT ending in a blank line still parses all blocks."""
        srt_with_trailing_blank = (
            "1\n"
            "00:00:00,000 --> 00:00:01,000\n"
            "Hello\n\n"
            "2\n"
            "00:00:01,000 --> 00:00:02,000\n"
            "World\n\n"
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            subtitle_file.write_text(srt_with_trailing_blank, encoding="utf-8")

            items = subtitle.file_to_subtitles(str(subtitle_file))

        self.assertEqual([item[2] for item in items], ["Hello", "World"])

    def test_build_karaoke_subtitles_chunks_and_highlights_words(self):
        """Karaoke 模式按指定词数切块，并逐词给当前词添加高亮颜色标签。"""
        words = [
            subtitle.SubtitleWord("O", 0.0, 0.4),
            subtitle.SubtitleWord("futuro", 0.4, 0.9),
            subtitle.SubtitleWord("começa", 0.9, 1.5),
            subtitle.SubtitleWord("hoje", 1.5, 2.0),
        ]
        subs, srt_content = subtitle.build_karaoke_subtitles(
            words, highlight_color="#FFDD00", max_words=3
        )
        self.assertEqual(len(subs), 4)
        # 第一块前 3 个词
        self.assertIn('<font color="#FFDD00">O</font> futuro começa', subs[0]["msg"])
        self.assertIn('O <font color="#FFDD00">futuro</font> começa', subs[1]["msg"])
        self.assertIn('O futuro <font color="#FFDD00">começa</font>', subs[2]["msg"])
        # 第二块第 4 个词
        self.assertIn('<font color="#FFDD00">hoje</font>', subs[3]["msg"])
        self.assertIn("00:00:00,000 --> 00:00:00,400", srt_content)

    def test_build_karaoke_subtitles_handles_punctuation_break(self):
        """遇到标点符号时提前切块，保证断句节奏符合语意。"""
        words = [
            subtitle.SubtitleWord("Olá,", 0.0, 0.5),
            subtitle.SubtitleWord("mundo", 0.5, 1.0),
            subtitle.SubtitleWord("digital", 1.0, 1.6),
        ]
        subs, _ = subtitle.build_karaoke_subtitles(
            words, highlight_color="#00FFFF", max_words=3
        )
        self.assertEqual(len(subs), 3)
        self.assertEqual(subs[0]["msg"], '<font color="#00FFFF">Olá,</font>')
        self.assertEqual(subs[1]["msg"], '<font color="#00FFFF">mundo</font> digital')
        self.assertEqual(subs[2]["msg"], 'mundo <font color="#00FFFF">digital</font>')

    def test_build_karaoke_subtitles_handles_empty_input(self):
        """空单词列表应安全返回空列表与空字符串。"""
        subs, srt = subtitle.build_karaoke_subtitles([])
        self.assertEqual(subs, [])
        self.assertEqual(srt, "")

    def test_write_ass_subtitles_generates_valid_ass_file(self):
        """ASS 导出应生成包含 Karaoke 样式和 \\k 标签的标准字幕文件。"""
        words = [
            subtitle.SubtitleWord("Hello", 0.0, 0.5),
            subtitle.SubtitleWord("world", 0.5, 1.2),
        ]
        with tempfile.TemporaryDirectory() as tmp_dir:
            ass_path = Path(tmp_dir) / "sub.ass"
            subtitle.write_ass_subtitles(
                words=words,
                ass_file=str(ass_path),
                font_name="Arial",
                font_size=60,
                primary_color="#FFFFFF",
                highlight_color="#FFDD00",
            )
            self.assertTrue(ass_path.exists())
            content = ass_path.read_text(encoding="utf-8")
            self.assertIn("[Script Info]", content)
            self.assertIn("Style: Karaoke", content)
            self.assertIn(r"{\k50}Hello", content)
            self.assertIn(r"{\k70}world", content)

    def test_create_with_karaoke_style_generates_both_srt_and_ass(self):
        """create 传入 subtitle_style='karaoke' 时应同时生成带有高亮标签的 .srt 与 .ass 文件。"""
        class _FakeWhisperModel:
            def __init__(self, *args, **kwargs):
                pass

            def transcribe(self, audio_file, **kwargs):
                words = [
                    SimpleNamespace(start=0.0, end=0.5, word="Smart"),
                    SimpleNamespace(start=0.5, end=1.0, word="video"),
                ]
                segment = SimpleNamespace(start=0.0, end=1.0, words=words)
                info = SimpleNamespace(language="en", language_probability=0.99)
                return [segment], info

        with tempfile.TemporaryDirectory() as tmp_dir:
            srt_path = Path(tmp_dir) / "output.srt"
            with patch.object(subtitle, "model", None), patch.object(
                subtitle, "WhisperModel", _FakeWhisperModel
            ):
                subtitle.create(
                    audio_file="audio.mp3",
                    subtitle_file=str(srt_path),
                    subtitle_style="karaoke",
                    karaoke_highlight_color="#FFDD00",
                )

            self.assertTrue(srt_path.exists())
            ass_path = Path(tmp_dir) / "output.ass"
            self.assertTrue(ass_path.exists())
            srt_text = srt_path.read_text(encoding="utf-8")
            self.assertIn('<font color="#FFDD00">Smart</font>', srt_text)


if __name__ == "__main__":
    unittest.main()
