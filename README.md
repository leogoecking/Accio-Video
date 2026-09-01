<div align="center">

# Accio Video 🪄🎬

### Gerador Completo de Vídeos Curtos com IA (All-in-One AI Video Generator)

Basta fornecer um **tema** ou **palavra-chave**, e o **Accio Video** gera automaticamente o roteiro, busca os materiais de vídeo em alta definição, cria legendas sincronizadas, adiciona trilha sonora de fundo e renderiza um vídeo pronto para publicação.

[![Python](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![GitHub Repo](https://img.shields.io/badge/GitHub-Accio--Video-181717?logo=github)](https://github.com/leogoecking/Accio-Video)

[Português](README.md) | [English](README-en.md) | [Releases](https://github.com/leogoecking/Accio-Video/releases) | [Issues](https://github.com/leogoecking/Accio-Video/issues)

</div>

---

## 🖥️ Interface e Demonstração

<h4 align="center">WebUI</h4>

![](docs/webui-en.jpg)

<h4 align="center">API REST</h4>

![](docs/api.jpg)

---

## 🎯 Principais Funcionalidades

- [x] **Renderização Otimizada com FFmpeg Direct Filtergraph**: Pipeline acelerado em C++ para corte, redimensionamento com letterbox, transições dinâmicas e mixagem com áudio ducking.
- [x] **Múltiplas Interfaces**: Suporte completo via **WebUI (Streamlit)**, **API REST (FastAPI)**, **CLI (Linha de comando)** e **AI Agent Skills**.
- [x] **Roteiros Automáticos com LLMs**: Compatível com OpenAI (GPT-4o, GPT-5), Google Gemini, Claude, DeepSeek, Qwen, Ollama, Groq e modelos locais.
- [x] **Formatos e Resoluções**:
  - Vertical (9:16) - `1080x1920` (Reels, TikTok, YouTube Shorts)
  - Horizontal (16:9) - `1920x1080` (YouTube padrão)
- [x] **Síntese de Voz (TTS)**: Edge TTS gratuito, Azure Speech, SiliconFlow, Google Gemini TTS, ElevenLabs, Fish Audio e Chatterbox com prévia em tempo real.
- [x] **Legendas Personalizadas**: Geração com Faster-Whisper, fontes customizadas, posições, cores, bordas, sombras e fundo destacado.
- [x] **Banco de Mídia e IA**: Integração com Pexels, Pixabay, Coverr, upload de vídeos locais e geração de imagens/vídeos via IA.
- [x] **Trilha Sonora Inteligente**: Música de fundo aleatória ou personalizada, controle de volume e *Auto-Ducking* (reduz o volume do BGM quando a voz fala).
- [x] **Publicação Direta**: Suporte para exportação rápida e postagem em redes sociais.

---

## 📦 Requisitos de Sistema

- **Python**: 3.11 ou superior
- **FFmpeg**: Incluído automaticamente ou detectado no sistema
- **Sistema Operacional**: Windows 10+, macOS 11+, ou Linux (Ubuntu/Debian, etc.)

| Recurso | Mínimo | Recomendado |
| :--- | :--- | :--- |
| **CPU** | 4 núcleos | 6 a 8 núcleos |
| **RAM** | 4 GB | 8 GB a 16 GB |
| **GPU** | Opcional | Recomendado para transcrever com Whisper ou aceleração NVENC |

---

## 🚀 Instalação e Execução

### 1. Clonar o Repositório

```bash
git clone https://github.com/leogoecking/Accio-Video.git
cd Accio-Video
```

### 2. Configurar o Ambiente Python

Utilizando o gerenciador **uv** (recomendado):

```bash
uv sync --frozen
```

Ou utilizando `venv` tradicional:

```bash
python3.11 -m venv .venv
source .venv/bin/activate  # No Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Iniciar o Accio Video

#### Interface Gráfica (WebUI):
```bash
./webui.sh  # No Windows: webui.bat
```
Abra no navegador em: `http://127.0.0.1:8501`

#### Servidor de API REST:
```bash
python main.py
```
Documentação interativa disponível em: `http://127.0.0.1:8080/docs`

---

## 🐳 Execução via Docker

```bash
docker compose up -d
```

---

## 📄 Licença

Este projeto é distribuído sob a licença [MIT](LICENSE).
