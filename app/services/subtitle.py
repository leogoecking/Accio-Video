from dataclasses import dataclass
import json
import os.path
import re
from timeit import default_timer as timer

try:
    from faster_whisper import WhisperModel
except ImportError:
    WhisperModel = None
from loguru import logger

from app.config import config
from app.utils import utils

model_size = config.whisper.get("model_size", "large-v3")
device = config.whisper.get("device", "cpu")
compute_type = config.whisper.get("compute_type", "int8")
initial_prompt = config.whisper.get("initial_prompt", "") or None
model = None


@dataclass
class SubtitleWord:
    word: str
    start: float
    end: float


def hex_to_ass_color(color: str, alpha: int = 0) -> str:
    """Convert hex color (#RRGGBB) to ASS color (&HAABBGGRR)."""
    if isinstance(color, str) and color.startswith("#") and len(color) == 7:
        try:
            r = int(color[1:3], 16)
            g = int(color[3:5], 16)
            b = int(color[5:7], 16)
            return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}&"
        except ValueError:
            pass
    return f"&H{alpha:02X}FFFFFF&"


def format_ass_timestamp(seconds: float) -> str:
    """Format seconds into ASS timestamp format: H:MM:SS.cs"""
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int(round((seconds - int(seconds)) * 100))
    if cs >= 100:
        cs = 0
        s += 1
        if s >= 60:
            s = 0
            m += 1
            if m >= 60:
                m = 0
                h += 1
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def build_karaoke_subtitles(
    words: list[SubtitleWord],
    highlight_color: str = "#FFDD00",
    max_words: int = 3,
    is_cjk_lang: bool = False,
) -> tuple[list[dict], str]:
    """
    Groups words into short visual chunks and emits dynamic karaoke SRT cues.
    Each cue illuminates the active spoken word with `<font color="{highlight_color}">`.
    """
    cleaned_words = [
        w for w in words
        if w.word and w.word.strip() and w.end > w.start
    ]
    if not cleaned_words:
        return [], ""

    max_words = max(1, int(max_words or 3))
    chunks: list[list[SubtitleWord]] = []
    current_chunk: list[SubtitleWord] = []

    for i, w in enumerate(cleaned_words):
        current_chunk.append(w)
        is_last = (i == len(cleaned_words) - 1)
        has_punct = utils.str_contains_punctuation(w.word)
        has_gap = False if is_last else (cleaned_words[i + 1].start - w.end > 0.8)

        if len(current_chunk) >= max_words or has_punct or has_gap or is_last:
            chunks.append(current_chunk)
            current_chunk = []

    subtitles = []
    lines = []
    idx = 1
    separator = "" if is_cjk_lang else " "

    for chunk in chunks:
        n = len(chunk)
        for i in range(n):
            cue_start = chunk[i].start
            cue_end = (
                chunk[i + 1].start
                if (i < n - 1 and chunk[i + 1].start > chunk[i].start)
                else chunk[i].end
            )
            if cue_end <= cue_start:
                cue_end = cue_start + 0.1

            formatted = []
            for j, cw in enumerate(chunk):
                word_text = cw.word.strip()
                if j == i:
                    formatted.append(f'<font color="{highlight_color}">{word_text}</font>')
                else:
                    formatted.append(word_text)

            cue_text = separator.join(formatted)
            subtitles.append({"msg": cue_text, "start_time": cue_start, "end_time": cue_end})
            lines.append(utils.text_to_srt(idx, cue_text, cue_start, cue_end))
            idx += 1

    srt_content = "\n".join(lines) + "\n"
    return subtitles, srt_content


def write_ass_subtitles(
    words: list[SubtitleWord],
    ass_file: str,
    font_name: str = "Arial",
    font_size: int = 60,
    primary_color: str = "#FFFFFF",
    highlight_color: str = "#FFDD00",
    stroke_color: str = "#000000",
    stroke_width: float = 2.0,
    max_words: int = 3,
    is_cjk_lang: bool = False,
):
    """Writes Advanced SubStation Alpha (.ass) file with native \\k karaoke tags."""
    cleaned_words = [
        w for w in words
        if w.word and w.word.strip() and w.end > w.start
    ]
    if not cleaned_words:
        return

    max_words = max(1, int(max_words or 3))
    pri_ass = hex_to_ass_color(primary_color)
    sec_ass = hex_to_ass_color(highlight_color)
    out_ass = hex_to_ass_color(stroke_color)

    header = f"""[Script Info]
Title: Accio Video Karaoke Subtitles
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Karaoke,{font_name},{font_size},{pri_ass},{sec_ass},{out_ass},&H80000000,-1,0,0,0,100,100,0,0,1,{int(stroke_width)},2,2,40,40,140,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    chunks = []
    current_chunk = []
    for i, w in enumerate(cleaned_words):
        current_chunk.append(w)
        is_last = (i == len(cleaned_words) - 1)
        has_punct = utils.str_contains_punctuation(w.word)
        has_gap = False if is_last else (cleaned_words[i + 1].start - w.end > 0.8)
        if len(current_chunk) >= max_words or has_punct or has_gap or is_last:
            chunks.append(current_chunk)
            current_chunk = []

    dialogues = []
    separator = "" if is_cjk_lang else " "
    for chunk in chunks:
        chunk_start = chunk[0].start
        chunk_end = chunk[-1].end
        start_str = format_ass_timestamp(chunk_start)
        end_str = format_ass_timestamp(chunk_end)

        parts = []
        for w in chunk:
            dur_cs = max(1, int(round((w.end - w.start) * 100)))
            parts.append(f"{{\\k{dur_cs}}}{w.word.strip()}")

        dialogue_text = separator.join(parts)
        dialogues.append(f"Dialogue: 0,{start_str},{end_str},Karaoke,,0,0,0,,{dialogue_text}")

    content = header + "\n".join(dialogues) + "\n"
    with open(ass_file, "w", encoding="utf-8") as f:
        f.write(content)


def create(
    audio_file,
    subtitle_file: str = "",
    subtitle_style: str = "classic",
    karaoke_highlight_color: str = "#FFDD00",
    karaoke_max_words: int = 3,
):
    global model
    if WhisperModel is None:
        logger.warning("faster_whisper not available, skipping whisper subtitle generation")
        return ""
    if not model:
        model_path = f"{utils.root_dir()}/models/whisper-{model_size}"
        model_bin_file = f"{model_path}/model.bin"
        if not os.path.isdir(model_path) or not os.path.isfile(model_bin_file):
            model_path = model_size

        logger.info(
            f"loading model: {model_path}, device: {device}, compute_type: {compute_type}"
        )
        try:
            model = WhisperModel(
                model_size_or_path=model_path, device=device, compute_type=compute_type
            )
        except Exception as e:
            logger.error(
                f"failed to load model: {e} \n\n"
                f"********************************************\n"
                f"this may be caused by network issue. \n"
                f"please download the model manually and put it in the 'models' folder. \n"
                f"see [README.md FAQ](https://github.com/leogoecking/Accio-Video) for more details.\n"
                f"********************************************\n\n"
            )
            return None

    logger.info(f"start, output file: {subtitle_file}, style: {subtitle_style}")
    if not subtitle_file:
        subtitle_file = f"{audio_file}.srt"

    segments, info = model.transcribe(
        audio_file,
        beam_size=5,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500),
        **({"initial_prompt": initial_prompt} if initial_prompt else {}),
    )

    detected_lang = getattr(info, "language", "") or "en"
    logger.info(
        f"detected language: '{detected_lang}', probability: {getattr(info, 'language_probability', 0.0):.2f}"
    )

    start = timer()
    is_cjk = detected_lang in ("zh", "ja", "ko")

    if subtitle_style == "karaoke":
        words: list[SubtitleWord] = []
        for segment in segments:
            if getattr(segment, "words", None):
                for w in segment.words:
                    clean_w = w.word.strip()
                    if clean_w:
                        words.append(
                            SubtitleWord(
                                word=clean_w,
                                start=float(w.start),
                                end=float(w.end),
                            )
                        )
        _, srt_content = build_karaoke_subtitles(
            words=words,
            highlight_color=karaoke_highlight_color,
            max_words=karaoke_max_words,
            is_cjk_lang=is_cjk,
        )
        with open(subtitle_file, "w", encoding="utf-8") as f:
            f.write(srt_content)
        try:
            ass_file = os.path.splitext(subtitle_file)[0] + ".ass"
            write_ass_subtitles(
                words=words,
                ass_file=ass_file,
                highlight_color=karaoke_highlight_color,
                max_words=karaoke_max_words,
                is_cjk_lang=is_cjk,
            )
        except Exception as ass_err:
            logger.debug(f"failed to export ASS subtitles: {ass_err}")

        diff = timer() - start
        logger.info(f"complete karaoke subtitles, elapsed: {diff:.2f} s")
        return subtitle_file

    subtitles = []

    def recognized(seg_text, seg_start, seg_end):
        seg_text = seg_text.strip()
        if not seg_text:
            return

        msg = "[%.2fs -> %.2fs] %s" % (seg_start, seg_end, seg_text)
        logger.debug(msg)

        subtitles.append(
            {"msg": seg_text, "start_time": seg_start, "end_time": seg_end}
        )

    for segment in segments:
        words_idx = 0
        words_len = len(segment.words) if getattr(segment, "words", None) else 0

        seg_start = 0
        seg_end = 0
        seg_text = ""

        if getattr(segment, "words", None):
            is_segmented = False
            for word in segment.words:
                if not is_segmented:
                    seg_start = word.start
                    is_segmented = True

                seg_end = word.end
                # If it contains punctuation, then break the sentence.
                seg_text += word.word

                if utils.str_contains_punctuation(word.word):
                    # remove last char
                    seg_text = seg_text[:-1]
                    if not seg_text:
                        continue

                    recognized(seg_text, seg_start, seg_end)

                    is_segmented = False
                    seg_text = ""

                if words_idx == 0 and segment.start < word.start:
                    seg_start = word.start
                if words_idx == (words_len - 1) and segment.end > word.end:
                    seg_end = word.end
                words_idx += 1

        if not seg_text:
            continue

        recognized(seg_text, seg_start, seg_end)

    end = timer()
    diff = end - start
    logger.info(f"complete, elapsed: {diff:.2f} s")

    idx = 1
    lines = []
    for subtitle_item in subtitles:
        text = subtitle_item.get("msg")
        if text:
            lines.append(
                utils.text_to_srt(
                    idx, text, subtitle_item.get("start_time"), subtitle_item.get("end_time")
                )
            )
            idx += 1

    sub = "\n".join(lines) + "\n"
    with open(subtitle_file, "w", encoding="utf-8") as f:
        f.write(sub)
    logger.info(f"subtitle file created: {subtitle_file}")
    return subtitle_file


def file_to_subtitles(filename):
    if not filename or not os.path.isfile(filename):
        return []

    times_texts = []
    current_times = None
    current_text = ""
    index = 0
    with open(filename, "r", encoding="utf-8") as f:
        for line in f:
            times = re.findall("([0-9]*:[0-9]*:[0-9]*,[0-9]*)", line)
            if times:
                current_times = line
            elif line.strip() == "" and current_times:
                index += 1
                times_texts.append((index, current_times.strip(), current_text.strip()))
                current_times, current_text = None, ""
            elif current_times:
                current_text += line

    # Flush the final block. SRT files whose last subtitle is not followed by a
    # trailing blank line never hit the blank-line branch above, so without this
    # the last subtitle would be silently dropped.
    if current_times:
        index += 1
        times_texts.append((index, current_times.strip(), current_text.strip()))
    return times_texts


def levenshtein_distance(s1, s2):
    if len(s1) < len(s2):
        return levenshtein_distance(s2, s1)

    if len(s2) == 0:
        return len(s1)

    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    return previous_row[-1]


def similarity(a, b):
    distance = levenshtein_distance(a.lower(), b.lower())
    max_length = max(len(a), len(b))
    return 1 - (distance / max_length)


def correct(subtitle_file, video_script):
    subtitle_items = file_to_subtitles(subtitle_file)
    normalized_script = utils.normalize_script_for_subtitle_matching(video_script)
    script_lines = utils.split_string_by_punctuations(normalized_script)

    corrected = False
    new_subtitle_items = []
    script_index = 0
    subtitle_index = 0

    while script_index < len(script_lines) and subtitle_index < len(subtitle_items):
        script_line = script_lines[script_index].strip()
        subtitle_line = subtitle_items[subtitle_index][2].strip()

        if script_line == subtitle_line:
            new_subtitle_items.append(subtitle_items[subtitle_index])
            script_index += 1
            subtitle_index += 1
        else:
            combined_subtitle = subtitle_line
            start_time = subtitle_items[subtitle_index][1].split(" --> ")[0]
            end_time = subtitle_items[subtitle_index][1].split(" --> ")[1]
            next_subtitle_index = subtitle_index + 1

            while next_subtitle_index < len(subtitle_items):
                next_subtitle = subtitle_items[next_subtitle_index][2].strip()
                if similarity(
                    script_line, combined_subtitle + " " + next_subtitle
                ) > similarity(script_line, combined_subtitle):
                    combined_subtitle += " " + next_subtitle
                    end_time = subtitle_items[next_subtitle_index][1].split(" --> ")[1]
                    next_subtitle_index += 1
                else:
                    break

            if similarity(script_line, combined_subtitle) > 0.8:
                logger.warning(
                    f"Merged/Corrected - Script: {script_line}, Subtitle: {combined_subtitle}"
                )
                new_subtitle_items.append(
                    (
                        len(new_subtitle_items) + 1,
                        f"{start_time} --> {end_time}",
                        script_line,
                    )
                )
                corrected = True
            else:
                logger.warning(
                    f"Mismatch - Script: {script_line}, Subtitle: {combined_subtitle}"
                )
                new_subtitle_items.append(
                    (
                        len(new_subtitle_items) + 1,
                        f"{start_time} --> {end_time}",
                        script_line,
                    )
                )
                corrected = True

            script_index += 1
            subtitle_index = next_subtitle_index

    # Process the remaining lines of the script.
    while script_index < len(script_lines):
        logger.warning(f"Extra script line: {script_lines[script_index]}")
        if subtitle_index < len(subtitle_items):
            new_subtitle_items.append(
                (
                    len(new_subtitle_items) + 1,
                    subtitle_items[subtitle_index][1],
                    script_lines[script_index],
                )
            )
            subtitle_index += 1
        else:
            new_subtitle_items.append(
                (
                    len(new_subtitle_items) + 1,
                    "00:00:00,000 --> 00:00:00,000",
                    script_lines[script_index],
                )
            )
        script_index += 1
        corrected = True

    if corrected:
        with open(subtitle_file, "w", encoding="utf-8") as fd:
            for i, item in enumerate(new_subtitle_items):
                fd.write(f"{i + 1}\n{item[1]}\n{item[2]}\n\n")
        logger.info("Subtitle corrected")
    else:
        logger.success("Subtitle is correct")


if __name__ == "__main__":
    task_id = "c12fd1e6-4b0a-4d65-a075-c87abe35a072"
    task_dir = utils.task_dir(task_id)
    subtitle_file = f"{task_dir}/subtitle.srt"
    audio_file = f"{task_dir}/audio.mp3"

    subtitles = file_to_subtitles(subtitle_file)
    print(subtitles)

    script_file = f"{task_dir}/script.json"
    with open(script_file, "r") as f:
        script_content = f.read()
    s = json.loads(script_content)
    script = s.get("script")

    correct(subtitle_file, script)

    subtitle_file = f"{task_dir}/subtitle-test.srt"
    create(audio_file, subtitle_file)
