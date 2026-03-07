# Ultimate Video Processor

An AI agent skill to extract subtitles, generate summaries with keyframes, or get full video scripts from various platforms.

This tool combines two main functionalities, selectable via a `--mode` flag:
1.  **Summary Mode (default):** Extracts subtitles, generates keyframe screenshots, and outputs a structured JSON summary.
2.  **Script Mode:** Extracts the full, clean transcript and saves it as a well-formatted Markdown file.

## Supported Platforms

- Bilibili (B站)
- YouTube
- Douyin (抖音)
- Xiaohongshu (小红书)
- TikTok
- 1800+ other sites supported by `yt-dlp`.

## Features

- **Dual-Mode Operation**: Get a quick summary or a full script with a simple command-line flag.
- **Multi-Platform Support**: Works seamlessly across coscienza video sites.
- **Automatic Fallbacks**: Uses platform APIs, then `yt-dlp`, and finally Whisper ASR for transcription, ensuring the best possible result.
- **Robust Error Handling**: Includes a fix for common SSL certificate verification issues.
- **Local Caching**: Caches results to speed up repeated requests for the same video.

## Installation

1.  **Clone the repository:**
    ```bash
    git clone <your-github-repo-url>
    ```

2.  **Install Dependencies:**
    This skill requires Python 3.8+. All dependencies can be installed via pip.

    ```bash
    # Install all dependencies at once
    pip install youtube-transcript-api yt-dlp faster-whisper openai pydub Pillow
    ```
    You also need `ffmpeg` for keyframe extraction.
    - **macOS:** `brew install ffmpeg`
    - **Windows:** `winget install ffmpeg`
    - **Linux (Debian/Ubuntu):** `sudo apt install ffmpeg`

## Usage

The script is controlled via the command line.

- **To get a full video script (saved to a .md file):**
  ```bash
  python video_processor.py <VIDEO_URL> --mode script
  ```

- **To get a JSON summary with keyframes (printed to console):**
  ```bash
  python video_processor.py <VIDEO_URL> --mode summary
  ```
  *(This is the default mode if `--mode` is not specified.)*

- **To clear the cache:**
  ```bash
  python video_processor.py --clear-cache
  ```

## Configuration

You can edit `config.json` to customize the behavior, such as setting the Whisper model size, language hints, or disabling keyframe extraction.
