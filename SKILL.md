---
name: ultimate-video-processor
description: Get the spoken transcript or full voiceover script from a Douyin or other supported video URL. Use this first when the user provides a video link and asks for the words spoken in it.
---

# Ultimate Video Processor

This Skill extracts complete spoken transcripts from video URLs. When the user asks for the words spoken in a video, prioritize a complete transcript over a summary.

## Default task: return the complete spoken script

When the user provides a video URL and asks for its script, transcript, or spoken copy:

1. Run this Skill's `video_processor.py` with `--mode script`. Put output under `<current workspace>/video-transcripts/` using `--output-dir`.
2. Read the whole generated Markdown file. Return the complete transcript in the conversation. Do not stop at the saved-file message, summarize it, or invent missing speech.
3. Check the recorded source and matching coverage against the video's duration. Timed captions should reach the end of the spoken section; Whisper output should report the full audio duration processed. A non-empty transcript alone does not prove full coverage.
4. If the file marks coverage as unknown or partial, continue with the Douyin recovery steps below before replying.

Example command (replace `<skill-dir>` with this Skill's directory):

```sh
python3 "<skill-dir>/video_processor.py" "<video-url>" --mode script --output-dir "$PWD/video-transcripts"
```

## Douyin recovery steps

Try the built-in methods first. They resolve short links, read the share-page metadata, extract any caption track exposed by that page, and then try the selected yt-dlp runtime with browser cookies against the actual video URL. Whisper is the audio-recognition fallback when the audio can be fetched.

If the script reports `403`, `Fresh cookies`, or no transcript:

1. Do not repeat login, export cookies, or keep retrying the same blocked endpoint. A confirmed browser login can still play the page while Douyin's downloader endpoint rejects requests.
2. Check the connected Chrome session for the exact video. If it is not already open, open the supplied URL in a new tab. Confirm its `video/<id>` matches the supplied video.
3. Inspect for an actual transcript or selectable subtitle track. Douyin's “章节要点” / chapter summary is an AI summary and is not the spoken script; do not present it as a transcript.
4. If the video plays but the detail/download API is blocked, run the processor again with `--wait-for-browser-media-url` and the visible title/author. It prints a one-use loopback endpoint and waits for the active player to send its media address. Follow [references/douyin-browser-fallback.md](references/douyin-browser-fallback.md) to relay the URL from Chrome DevTools Console. Do not print, copy, save, or send the signed media URL anywhere else. The processor accepts only HTTPS addresses from Douyin media hosts, downloads with certificate verification and the Douyin referer, extracts audio locally, and runs local Whisper. It keeps the signed URL in memory and removes the media when the run exits.
5. If the active player does not expose its media address, use visible captions only when they cover the entire spoken section. Inspect the timeline in short overlapping intervals, join captions split across frames, and remove only exact repeated text. If any stretch is unreadable or unchecked, say which portion remains unverified.
6. Treat Whisper output as machine transcription: correct clear names and terms from the video, but mark genuinely unclear phrases instead of guessing. Confirm audio/caption coverage reaches the end of the spoken section.

If the exact video is mirrored elsewhere with a usable transcript, it can be used only after verifying that the creator, title, and content match the supplied Douyin video. Identify the transcript source to the user.

## Runtime and output rules

- The script selects the newest usable yt-dlp between the PATH executable and the Python module, and warns when it is older than 2026.08.19. Update the environment that runs this Skill if needed; do not silently use a stale binary.
- Local Whisper defaults to the `small` model with beam search and voice activity detection for more reliable full-script Chinese transcription. The first run may download model files.
- The script saves a unique Markdown file under `--output-dir`. Read that file before responding.
- If extraction fails, return the sanitized error and describe the recovery path tried. Do not say the Skill retrieved a complete transcript unless the whole spoken section is covered.
- The default `--mode summary` returns metadata, extracted text, and available keyframes as JSON; it does not generate a natural-language summary. Use `--mode script` for this Skill's main task.
- Browser cookies remain on the machine. Temporary cookies are permission-restricted and removed at process exit. Never commit credentials, cookies, browser profiles, cache files, or screenshots.
