# YtoTEXT (yt2text)

**English** · [한국어](README.ko.md)

A personal YouTube → text extractor. A single-file web app that runs on your own machine or home server.

Paste a YouTube link and get the full text via **captions** (fast) or **Whisper speech-to-text** (works even when a video has no captions). Results are shown as clean paragraphs or timestamped lines, and can be saved as `.txt` / `.md` / `.srt`.

Everything runs locally with **your own IP and your own hardware** — no third-party service in the middle.

## Quick start

```bash
pip install -r requirements.txt
python yt2text.py                # options: --host, --port (default 127.0.0.1:8765)
```

Open → http://localhost:8765

## Running it as a home server (Ubuntu)

```bash
git clone https://github.com/jaebeom/YtoTEXT.git
cd YtoTEXT
./install.sh    # venv + ffmpeg + GPU libraries + systemd service, in one go
```

- Binds to `0.0.0.0:8765` by default — reachable from your LAN or Tailscale network.
  To keep it local-only: `HOST=127.0.0.1 ./install.sh`
- Manage the service: `systemctl status|restart|stop yt2text` · logs: `journalctl -u yt2text -f`
- **Keep running with the lid closed**: set `HandleLidSwitch=ignore` in `/etc/systemd/logind.conf`, then reboot
- **Access from your phone/laptop**: on the same Wi-Fi, open `http://<server-ip>:8765`.
  From anywhere, install [Tailscale](https://tailscale.com) on the server and your devices → `http://<server-name>:8765`
- Update: `git pull && sudo systemctl restart yt2text` (re-run `./install.sh` if dependencies changed)

## Features

### Caption mode (fast)
- Pulls uploader/auto-generated captions via `youtube-transcript-api` (preferred languages: Korean → English, then whatever exists)
- Click a language chip on the result to switch caption languages
- **Bot-detection avoidance**: caption requests run one at a time with a random 2–5 s gap
- **Automatic block recovery**: on a YouTube block (429) the server pauses all caption requests (cooldown starts at 90 s, doubling up to 15 min) and the UI counts down and retries automatically (up to 5×) — cancellable per row or all at once

### Whisper mode (accurate)
- Downloads audio only via `yt-dlp` and transcribes with `faster-whisper` — works for videos without captions
- Model selection (large-v3-turbo recommended), language auto-detect/pin, per-job progress, a cancel button
- **Automatic GPU use**: with an NVIDIA driver present it transcribes on CUDA. cuBLAS/cuDNN are loaded by the app itself, and if the GPU fails at any point (model load or mid-transcription) it falls back to CPU (int8) and finishes the job
- **Long-video safety**: videos over 1.5 h are split into 1-hour chunks and transcribed sequentially (prevents the memory blow-up that can kill the server)
- Transcriptions run one at a time (queued); audio downloads run 3-wide

### Batch extraction
- Paste multiple links, one per line (up to 50), with per-row progress
- **Duplicate detection**: already-extracted videos show a notice row with [Open] / [Re-extract]
- Adding new links while a batch is running appends to the queue instead of resetting it
- Videos without captions offer a one-click [Extract with Whisper] switch on the failed row

### Viewing · export
- Paragraph view / timestamped view toggle, copy (works over plain-HTTP access too), `.txt` / `.md` / `.srt` downloads
- **Copy for AI** — copies the transcript with a context header (title, channel, duration, URL, the video description, and the top comments) so you can paste it straight into Gemini/ChatGPT/Claude and the model knows exactly what it's reading
- **Top comments** — every extraction also saves the 5 most-liked top-level comments (replies excluded, pinned/creator comments marked). They appear as a collapsed block above the transcript and go straight into Copy for AI, so the model sees how viewers reacted, not just what was said. Comment fetching never blocks the transcript — if YouTube refuses, you simply get the result without comments
- The `.md` export also includes the channel name and video description
- **YouTube ↗** link on every result; with timestamps on, each timestamp is a link that opens the video at that exact moment

### History
- Stores up to 300 extractions locally — thumbnail card grid, click to reopen, per-item delete
- Slim index (`history.json`) and full transcripts (`results/`) are stored separately, with atomic writes that survive crashes
- Thumbnails are backed up locally (cards survive even if a video is taken down)
- Entries whose title couldn't be fetched (blocks, embed-disabled videos) are repaired automatically on a later history load — with a yt-dlp fallback for videos the oembed API refuses, which also backfills the top comments that block cost you

### Stability · security
- Host-header check + JSON Content-Type enforcement — stops other websites from silently driving your local server (CSRF/DNS-rebinding defense)
- Jobs interrupted by a server restart are surfaced as failed in the UI instead of spinning forever
- Finished transcription jobs are pruned from memory after 10 minutes

## Version history

| Version | Notes |
|---------|-------|
| v4.3 | Top comments (5 most-liked) saved with every extraction, shown above the transcript and included in Copy for AI |
| v4.2 | YouTube deep links & timestamp jumps, 50-video batches, caption pacing + automatic block recovery (+stop), long-video chunking, GPU auto-load with CPU fallback, Copy for AI with channel/description |
| v4.1 | Stability & security (atomic writes, split storage, job cancel, CSRF defense), Ubuntu home-server installer |
| v4 | Duplicate detection, batch extraction (queue/progress) |
| v3 | History (local storage, thumbnail cards, reopen/delete) |
| v2 | Whisper STT mode (videos without captions) |
| v1 | Caption extraction, paragraph cleanup, txt/md/srt export |

## Notes

- All data lives in the local `yt2text_data/` folder (index `history.json` · transcripts `results/` · thumbnails `thumbs/`) — never committed to git
- Whisper models download automatically on first use (large models are several GB and take a few minutes)
- Default binding is `127.0.0.1` with a Host-header check that blocks outside/cross-site access (`--host` to change; the check relaxes automatically for non-loopback binds)
- YouTube blocks are temporary — split large batches into smaller runs, and use Whisper mode for anything urgent
- The web UI is in Korean (this started as a personal tool). Everything else — install, service, exports — is language-neutral
