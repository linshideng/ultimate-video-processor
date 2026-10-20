# Douyin browser media fallback

Use this only after the ordinary Skill run fails with a Douyin detail/API 403, and only when the exact video is already open in the user's authenticated Chrome session and its player can play.

## Start the one-use local receiver

Run the Skill again with the one-use receiver enabled. Use the title and author shown on the verified video page:

```sh
python3 "<skill-dir>/video_processor.py" "<video-url>" \
  --mode script --output-dir "$PWD/video-transcripts" \
  --wait-for-browser-media-url --title "<video title>" --author "<creator>"
```

Leave the process waiting. It prints a short-lived endpoint on `127.0.0.1`; use that exact endpoint as `relayEndpoint` in the code below. Do not send the URL to a non-local endpoint.

## Relay the player's media URL

In the exact Douyin video tab, open Chrome DevTools Console. Set `expectedId` to the numeric video ID and `relayEndpoint` to the one-use local endpoint printed by the Skill. Paste and run:

```js
(async () => {
  const expectedId = "<numeric video id>";
  const relayEndpoint = "<one-use 127.0.0.1 endpoint>";
  if (!location.pathname.includes(`/video/${expectedId}`)) {
    throw new Error("The active tab is not the requested Douyin video.");
  }

  let element = document.querySelector("video");
  while (element && !Object.keys(element).some(key => key.startsWith("__reactFiber$"))) {
    element = element.parentElement;
  }
  if (!element) throw new Error("The player data is not available on this page.");

  const fiberKey = Object.keys(element).find(key => key.startsWith("__reactFiber$"));
  let fiber = element[fiberKey];
  let item;
  for (let depth = 0; fiber && depth < 12; depth++, fiber = fiber.return) {
    const candidate = fiber.memoizedProps?.awemeInfo;
    if (candidate && String(candidate.awemeId || candidate.groupId) === expectedId) {
      item = candidate;
      break;
    }
  }
  if (!item) throw new Error("The page player did not match the requested video ID.");

  const urls = [];
  const seen = new WeakSet();
  function collect(value, depth = 0) {
    if (typeof value === "string") {
      if (value.startsWith("https://")) urls.push(value);
      return;
    }
    if (!value || typeof value !== "object" || depth > 7 || seen.has(value)) return;
    seen.add(value);
    for (const child of Object.values(value)) collect(child, depth + 1);
  }
  collect(item.video?.playAddr);
  collect(item.video?.playAddrH265);
  collect(item.video?.downloadAddr);

  const mediaUrl = urls.find(value => {
    try {
      const host = new URL(value).hostname;
      return ["douyinvod.com", "bytevod.com", "bytecdn.cn", "douyin.com"]
        .some(domain => host === domain || host.endsWith(`.${domain}`));
    } catch { return false; }
  });
  if (!mediaUrl) throw new Error("No supported media address was exposed by the player.");

  const response = await fetch(relayEndpoint, {
    method: "POST",
    headers: { "Content-Type": "text/plain" },
    body: mediaUrl,
  });
  console.log("Local media handoff status:", response.status);
})();
```

The console should show status `200`; the waiting Skill process then downloads and transcribes the audio. Do not expand or print the `mediaUrl` value. The processor validates the URL host again, does not persist or log the signed URL, and removes temporary media after the run.

If the page has no matching player data or the relay does not return `200`, stop and report the exact limitation. Do not retry authentication, export cookies, or infer speech from chapter summaries.
