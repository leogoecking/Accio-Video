<div align="center">

# Accio Video 🪄🎬

### An All-in-One AI Short Video Generator

Simply provide a video **topic** or **keyword**, and **Accio Video** will automatically generate the script, match HD footage, generate synchronized subtitles, add background music, and render an HD short video.

[![Python](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![GitHub Repo](https://img.shields.io/badge/GitHub-Accio--Video-181717?logo=github)](https://github.com/leogoecking/Accio-Video)

[Português](README.md) | [English](README-en.md) | [Releases](https://github.com/leogoecking/Accio-Video/releases) | [Issues](https://github.com/leogoecking/Accio-Video/issues)

</div>

---

## 🖥️ Screenshots & Interface

<h4 align="center">WebUI</h4>

![](docs/webui-en.jpg)

<h4 align="center">REST API</h4>

![](docs/api.jpg)

---

## 🎯 Key Features

- [x] **FFmpeg Direct Filtergraph Accelerated Rendering**: Fast subclip trimming, letterboxing, dynamic transitions, and sidechain auto-ducking audio mixing.
- [x] **Multiple Workflow Interfaces**: **WebUI (Streamlit)**, **REST API (FastAPI)**, **CLI**, and **AI Agent Skills**.
- [x] **AI Script Generation**: Compatible with OpenAI (GPT-4o, GPT-5), Google Gemini, Anthropic Claude, DeepSeek, Qwen, Ollama, Groq, and local LLMs.
- [x] **Multiple Formats & Aspect Ratios**:
  - Portrait (9:16) - `1080x1920` (Reels, TikTok, Shorts)
  - Landscape (16:9) - `1920x1080` (Standard video)
- [x] **Speech Synthesis (TTS)**: Free Edge TTS, Azure Speech, SiliconFlow, Google Gemini TTS, ElevenLabs, Fish Audio, and Chatterbox with live voice preview.
- [x] **Custom Subtitles**: Auto-transcription with Faster-Whisper, customizable fonts, colors, stroke borders, and background banners.
- [x] **Footage Integration**: Built-in support for Pexels, Pixabay, Coverr, local video collections, and AI-generated footage.
- [x] **Smart Audio Mixing**: Random or custom BGM with auto-ducking to smooth volume during speech.
- [x] **Social Publishing**: Export and schedule uploads to TikTok, Instagram, and YouTube Shorts.

---

## 📦 System Requirements

- **Python**: 3.11+
- **FFmpeg**: Bundled or system installation
- **Operating System**: Windows 10+, macOS 11+, or Linux

| Resource | Minimum | Recommended |
| :--- | :--- | :--- |
| **CPU** | 4 cores | 6 to 8 cores |
| **RAM** | 4 GB | 8 GB to 16 GB |
| **GPU** | Optional | Recommended for local Whisper transcription or NVENC acceleration |

---

## 🚀 Quick Start

### 1. Clone the Repository

```bash
git clone https://github.com/leogoecking/Accio-Video.git
cd Accio-Video
```

### 2. Setup Python Environment

Using **uv** (recommended):

```bash
uv sync --frozen
```

Or using standard `venv`:

```bash
python3.11 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Launch Accio Video

#### Graphical Interface (WebUI):
```bash
./webui.sh  # On Windows: webui.bat
```
Open in browser at: `http://127.0.0.1:8501`

#### REST API Server:
```bash
python main.py
```
API Documentation at: `http://127.0.0.1:8080/docs`

---

## 🐳 Docker Deployment

```bash
docker compose up -d
```

---

## 📄 License

This project is licensed under the [MIT](LICENSE) License.
