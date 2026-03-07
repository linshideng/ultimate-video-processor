This skill allows you to process videos from various online platforms.

**Tool: `video_processor.py`**

This script can perform two main actions based on the `--mode` parameter:

1.  **Summarize Video (`--mode summary`)**
    - **Use Case:** When the user asks for a summary, overview, or key points of a video.
    - **Action:** Run the script with the video URL. The output will be a JSON object containing the title, author, duration, a text summary, and a list of keyframe images.
    - **Example Command:** `python video_processor.py "<video_url>"`

2.  **Get Full Script (`--mode script`)**
    - **Use Case:** When the user asks for the full transcript, text, or complete script of a video, often for content creation or analysis.
    - **Action:** Run the script with the video URL and the `--mode script` flag. The output will be a confirmation message indicating that the script has been saved to a `.md` file.
    - **Example Command:** `python video_processor.py "<video_url>" --mode script`

**URL Detection:**

The script automatically detects URLs from platforms like YouTube, Bilibili, Douyin, etc., when they are present in the user's prompt.
