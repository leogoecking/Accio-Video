import hashlib
import os
import random
import subprocess
import time
import urllib.parse
from typing import Literal

import requests
from loguru import logger

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect
from app.utils import utils


def _get_stock_api_key(cfg_key: str) -> str:
    """安全获取配置中的库存图库 API Key，兼容字符串和列表类型。"""
    keys = config.app.get(cfg_key)
    if isinstance(keys, str):
        return keys.strip()
    if isinstance(keys, (list, tuple)) and keys:
        first = keys[0]
        if isinstance(first, str):
            return first.strip()
    return ""


def _generate_with_gemini(
    clean_prompt: str,
    width: int,
    height: int,
    image_path: str,
) -> bool:
    """尝试通过 Google Gemini 图像模型生成图片，返回是否成功。"""
    api_key = config.app.get("gemini_api_key", "").strip()
    if not api_key:
        return False

    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        aspect_ratio = "9:16" if height > width else ("16:9" if width > height else "1:1")
        
        # 尝试 Gemini Flash Image / Imagen
        for model in ["gemini-2.5-flash-image", "gemini-3.1-flash-image"]:
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=f"Generate an image: {clean_prompt}, aspect ratio {aspect_ratio}, high quality photorealistic 8k",
                )
                if response and response.candidates:
                    for part in response.candidates[0].content.parts:
                        if hasattr(part, "inline_data") and part.inline_data and part.inline_data.data:
                            os.makedirs(os.path.dirname(image_path), exist_ok=True)
                            with open(image_path, "wb") as f:
                                f.write(part.inline_data.data)
                            logger.success(f"Gemini image generated successfully ({model}): {image_path}")
                            return True
            except Exception as e:
                logger.debug(f"Gemini {model} generation error: {e}")
    except Exception as exc:
        logger.warning(f"Google Gemini image generation unavailable: {exc}")

    return False


def _enrich_prompt(prompt: str) -> str:
    """自动增强提示词以产出高质感电影级图像。"""
    clean = prompt.strip()
    if not clean:
        return clean
    qualifiers = "cinematic lighting, photorealistic, 8k, highly detailed, sharp focus"
    if any(q in clean.lower() for q in ["cinematic", "photorealistic", "8k", "detailed"]):
        return clean
    return f"{clean}, {qualifiers}"


def _fetch_stock_photo_bytes(query: str, aspect: VideoAspect) -> bytes | None:
    """尝试从 Pexels 或 Pixabay 照片 API 获取高质量匹配静态图片作为坚实保底。"""
    # 1. 尝试 Pexels Photos
    pexels_key = _get_stock_api_key("pexels_api_keys")
    if pexels_key:
        try:
            orientation = "portrait" if aspect == VideoAspect.portrait else ("landscape" if aspect == VideoAspect.landscape else "square")
            headers = {
                "Authorization": pexels_key,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            }
            params = {"query": query, "per_page": 5, "orientation": orientation}
            r = requests.get(
                "https://api.pexels.com/v1/search",
                headers=headers,
                params=params,
                timeout=10,
            )
            if r.status_code == 200:
                data = r.json()
                photos = data.get("photos", [])
                if photos:
                    img_url = photos[0]["src"].get("large2x") or photos[0]["src"].get("original") or photos[0]["src"].get("large")
                    if img_url:
                        img_res = requests.get(img_url, timeout=15)
                        if img_res.status_code == 200 and len(img_res.content) > 1024:
                            logger.success(f"retrieved matching stock photo from Pexels: query={query!r}")
                            return img_res.content
        except Exception as e:
            logger.debug(f"Pexels photo search failed for {query!r}: {e}")

    # 2. 尝试 Pixabay Photos
    pixabay_key = _get_stock_api_key("pixabay_api_keys")
    if pixabay_key:
        try:
            orientation = "vertical" if aspect == VideoAspect.portrait else ("horizontal" if aspect == VideoAspect.landscape else "all")
            params = {
                "key": pixabay_key,
                "q": query,
                "image_type": "photo",
                "orientation": orientation,
                "per_page": 5,
            }
            r = requests.get("https://pixabay.com/api/", params=params, timeout=10)
            if r.status_code == 200:
                data = r.json()
                hits = data.get("hits", [])
                if hits:
                    img_url = hits[0].get("largeImageURL") or hits[0].get("webformatURL")
                    if img_url:
                        img_res = requests.get(img_url, timeout=15)
                        if img_res.status_code == 200 and len(img_res.content) > 1024:
                            logger.success(f"retrieved matching stock photo from Pixabay: query={query!r}")
                            return img_res.content
        except Exception as e:
            logger.debug(f"Pixabay photo search failed for {query!r}: {e}")

    return None


def _fetch_pollinations_image_bytes(
    clean_prompt: str,
    width: int,
    height: int,
    timeout: int = 15,
) -> bytes | None:
    """通过 Pollinations AI 获取图片，支持模型轮询与 429 退避重试。"""
    enriched_prompt = _enrich_prompt(clean_prompt)
    encoded_prompt = urllib.parse.quote(enriched_prompt)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }

    # 优先使用生成速度极快且队列轻量的 turbo，再回退到 flux 和默认模型
    models_to_try = ["turbo", "flux", ""]
    for model in models_to_try:
        seed = random.randint(1000, 999999)
        model_param = f"&model={model}" if model else ""
        query_url = (
            f"https://image.pollinations.ai/prompt/{encoded_prompt}"
            f"?width={width}&height={height}&nologo=true&seed={seed}{model_param}"
        )
        for retry in range(2):
            try:
                logger.info(
                    f"generating AI image via Pollinations ({model or 'default'}): {clean_prompt!r}"
                )
                response = requests.get(
                    query_url,
                    headers=headers,
                    proxies=config.proxy,
                    verify=config.app.get("tls_verify", True),
                    timeout=timeout,
                )
                if response.status_code == 200 and len(response.content) > 1024:
                    return response.content
                elif response.status_code == 429:
                    logger.debug(f"Pollinations 429 rate limit, backing off (retry {retry+1})...")
                    time.sleep(1.5)
                else:
                    break
            except Exception as exc:
                logger.debug(f"Pollinations request attempt failed ({model}): {exc}")
                break

    return None


def generate_ai_image(
    prompt: str,
    video_aspect: VideoAspect = VideoAspect.portrait,
    provider: str = "pollinations",
    save_dir: str | None = None,
    timeout: int = 20,
) -> str | None:
    """
    使用 AI 生成与指定提示词和宽高比匹配的高清静态图像。
    
    默认使用免 API Key 的 Pollinations AI（基于 Flux/SDXL 引擎），
    支持 Google Gemini 图像模型，当主力模型受限时自动平滑回退到高清摄影保底源。
    """
    if not prompt or not isinstance(prompt, str) or not prompt.strip():
        return None

    clean_prompt = prompt.strip()
    aspect = VideoAspect(video_aspect)
    width, height = aspect.to_resolution()

    chosen_provider = provider or config.app.get("ai_image_provider", "pollinations")
    prompt_hash = hashlib.md5(f"{clean_prompt}_{width}_{height}_{chosen_provider}".encode("utf-8")).hexdigest()
    
    target_dir = save_dir or utils.storage_dir("cache_videos", create=True)
    os.makedirs(target_dir, exist_ok=True)
    image_path = os.path.join(target_dir, f"ai-img-{prompt_hash}.jpg")

    if os.path.exists(image_path) and os.path.getsize(image_path) > 1024:
        logger.debug(f"reusing cached AI image: {image_path}")
        return image_path

    # 1. 若选择了 Gemini，优先尝试 Gemini
    if chosen_provider in ("gemini", "google_imagen"):
        if _generate_with_gemini(clean_prompt, width, height, image_path):
            return image_path
        logger.info(f"falling back to Pollinations AI for query: {clean_prompt!r}")

    # 2. 尝试 Pollinations AI
    img_bytes = _fetch_pollinations_image_bytes(clean_prompt, width, height, timeout=timeout)
    if img_bytes:
        with open(image_path, "wb") as f:
            f.write(img_bytes)
        logger.success(f"AI image generated and saved: {image_path}")
        return image_path

    # 3. 若 AI 图像生成遇到网络/队列限制，自动回退到高清摄影图库保底
    logger.info(f"AI image providers busy, fetching matching studio photo for {clean_prompt!r}")
    stock_bytes = _fetch_stock_photo_bytes(clean_prompt, aspect)
    if stock_bytes:
        with open(image_path, "wb") as f:
            f.write(stock_bytes)
        logger.success(f"matching photo retrieved and saved for Ken Burns: {image_path}")
        return image_path

    return None


def render_image_to_ken_burns_clip(
    image_path: str,
    output_path: str,
    duration: float,
    video_aspect: VideoAspect = VideoAspect.portrait,
    motion: Literal["zoom_in", "zoom_out", "random"] = "random",
    fps: int = 30,
) -> str | None:
    """
    使用 FFmpeg zoompan 滤镜将静态图片转换为带有电影级 Ken Burns 运动效果的 MP4 视频片段。
    """
    if not image_path or not os.path.exists(image_path):
        return None

    aspect = VideoAspect(video_aspect)
    target_width, target_height = aspect.to_resolution()
    duration = max(0.5, float(duration))
    total_frames = max(1, int(round(fps * duration)))

    chosen_motion = motion
    if chosen_motion == "random":
        chosen_motion = random.choice(["zoom_in", "zoom_out"])

    canvas_w = target_width * 2
    canvas_h = target_height * 2

    if chosen_motion == "zoom_out":
        vf_zoom = (
            f"scale={canvas_w}:{canvas_h}:force_original_aspect_ratio=increase,"
            f"crop={canvas_w}:{canvas_h},"
            f"zoompan=z='max(1.25-0.25*(in/{total_frames}),1.0)':"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"d={total_frames}:s={target_width}x{target_height}:fps={fps}"
        )
    else:
        vf_zoom = (
            f"scale={canvas_w}:{canvas_h}:force_original_aspect_ratio=increase,"
            f"crop={canvas_w}:{canvas_h},"
            f"zoompan=z='min(1.0+0.25*(in/{total_frames}),1.25)':"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"d={total_frames}:s={target_width}x{target_height}:fps={fps}"
        )

    vf_full = f"{vf_zoom},format=yuv420p"
    ffmpeg_bin = utils.get_ffmpeg_binary()
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    cmd = [
        ffmpeg_bin,
        "-y",
        "-i", image_path,
        "-vf", vf_full,
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-r", str(fps),
        "-t", f"{duration:.3f}",
        output_path,
    ]

    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if res.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            logger.info(f"Ken Burns video clip created successfully: {output_path}")
            return output_path
        else:
            logger.warning(f"FFmpeg Ken Burns render failed: {res.stderr}")
    except Exception as exc:
        logger.error(f"error rendering Ken Burns video from {image_path}: {exc}")

    return None


def generate_ai_video_clip(
    prompt: str,
    duration: float = 4.0,
    video_aspect: VideoAspect = VideoAspect.portrait,
    provider: str = "pollinations",
    save_dir: str | None = None,
    motion: Literal["zoom_in", "zoom_out", "random"] = "random",
) -> MaterialInfo | None:
    """
    高阶合成方法：输入提示词，直接生成一张 AI 图像并转换为带 Ken Burns 动效的视频片段，
    返回标准的 MaterialInfo 对象，可直接无缝接入 Accio Video 素材时间线。
    """
    image_path = generate_ai_image(
        prompt=prompt,
        video_aspect=video_aspect,
        provider=provider,
        save_dir=save_dir,
    )
    if not image_path:
        return None

    target_dir = save_dir or utils.storage_dir("cache_videos", create=True)
    prompt_hash = hashlib.md5(f"{prompt}_{duration}_{video_aspect}_{provider}_{motion}".encode("utf-8")).hexdigest()
    output_clip_path = os.path.join(target_dir, f"ai-clip-{prompt_hash}.mp4")

    if os.path.exists(output_clip_path) and os.path.getsize(output_clip_path) > 1024:
        logger.debug(f"reusing cached AI video clip: {output_clip_path}")
        clip_path = output_clip_path
    else:
        clip_path = render_image_to_ken_burns_clip(
            image_path=image_path,
            output_path=output_clip_path,
            duration=duration,
            video_aspect=video_aspect,
            motion=motion,
        )

    if not clip_path:
        return None

    aspect = VideoAspect(video_aspect)
    width, height = aspect.to_resolution()

    item = MaterialInfo()
    item.provider = "ai_image"
    item.url = clip_path
    item.duration = duration
    item.source_info = {
        "provider": "ai_image",
        "search_term": prompt,
        "asset_id": f"ai_{prompt_hash}",
        "source_page": None,
        "creator": {"name": f"AI Image ({provider})", "profile_page": None},
        "rendition": {
            "id": "ken_burns",
            "width": width,
            "height": height,
        },
    }
    return item
