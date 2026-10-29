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
- **Duplicate detection**: already-extracted videos show a notice row with [Open] / [Re-extract]. If the existing record was pulled the other way, the row says so and the button offers that method by name ([caption으로 추출] / [whisper로 추출]) — captions and Whisper are kept as separate records for the same video. Detection searches the whole history, not the folder you happen to be viewing, so a video filed in another folder (or left unfiled) is still caught — otherwise an out-of-sight record would send a whole Whisper run through again. [Open] opens it wherever it lives, and the card's 🗂 will file it where you want it
- Adding new links while a batch is running appends to the queue instead of resetting it
- Videos without captions offer a one-click [Extract with Whisper] switch on the failed row

### Viewing · export
- Paragraph view / timestamped view toggle, copy (works over plain-HTTP access too), `.txt` / `.md` / `.srt` downloads
- **Copy for AI** — copies the transcript with a context header (title, channel, duration, URL, the video description, and the top comments) so you can paste it straight into Gemini/ChatGPT/Claude and the model knows exactly what it's reading
- **Top comments** — every extraction also saves 5 top-level comments, picked by like count out of the candidates YouTube ranks highest (up to 30 scanned, replies excluded, pinned/creator comments marked). They appear as a collapsed block above the transcript and go straight into Copy for AI, so the model sees how viewers reacted, not just what was said. Comment fetching never holds up the transcript: it runs on a 10-second response budget with retries capped, and a refusal or timeout just yields the result without comments
- The `.md` export also includes the channel name and video description
- **YouTube ↗** link on every result; with timestamps on, each timestamp is a link that opens the video at that exact moment

### History
- Stores up to 300 extractions per person (counted per account/employee, so someone else's pulls never push yours out) — thumbnail card grid, click to reopen, per-item delete
- **Folders** — a sidebar beside the grid. Create folders by theme and file videos into them by dragging a card onto a folder or picking one from the card's 🗂 button. Clicking a folder filters the grid, and the folder you were last in is remembered across reloads. A folder belongs to the video, not to one extraction: pull the same video again — even by a different method — and it stays where you filed it. Extract a new video while a folder is open and it lands in that folder, so a fresh pull never disappears behind the filter. Deleting a folder never deletes videos — they simply go back to *미분류* (unfiled)
- **Re-extract from a card** — the ↻ button re-runs that video exactly the way it was pulled the first time (captions, or the same Whisper model), queues it like any other job, and replaces that history entry while keeping its folder
- **Sorting** — newest / oldest, by title (Korean collation: Hangul first, then Latin) or reversed, and by video length either way. It applies within the folder you're viewing and is remembered across reloads
- Slim index (`history.json`) and full transcripts (`results/`) are stored separately, with atomic writes that survive crashes
- Thumbnails are backed up locally (cards survive even if a video is taken down)
- Entries whose title couldn't be fetched (blocks, embed-disabled videos) are repaired automatically on a later history load — cheap oembed lookup first, with a yt-dlp fallback for videos it refuses. Repair is deliberately frugal: 20 entries per pass, a pause after every attempt, and a cooldown (5 min, doubling to 1 h) after a failed pass so a still-blocked host isn't hammered on every history reload

### Sharing with a team (optional)

- **Two accounts** — an owner (`YT2TEXT_OWNER_PW`) and a shared team login (`YT2TEXT_TEAM_PW`). Set either environment variable and the login screen turns on; set neither and the app behaves exactly as before
- **Nothing gets mixed** — history and folders are fully separated per account. Two people can extract the same video without overwriting each other, and neither can open or delete the other's records. Entries without a `user` field (everything from before) belong to the owner
- **Team records expire after 30 days** — anything extracted on the team login is deleted, transcript file included, once it is 30 days old (swept every 6 hours). Owner records never expire
- **Tailscale only** — with login on, requests are accepted only from the Tailscale ranges (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`) and from the machine itself. Set `YT2TEXT_ALLOW_LAN=1` to also allow your LAN
- Login attempts are capped at 10 per IP per 5 minutes; sessions last 30 days (the signing key lives in `yt2text_data/secret.key`, so a restart doesn't log everyone out)

Keep passwords out of the repository — use a systemd drop-in:

```bash
sudo mkdir -p /etc/systemd/system/yt2text.service.d
sudo tee /etc/systemd/system/yt2text.service.d/auth.conf >/dev/null <<'EOF'
[Service]
Environment=YT2TEXT_OWNER_PW=your-owner-pw
Environment=YT2TEXT_TEAM_PW=your-team-pw
EOF
sudo chmod 600 /etc/systemd/system/yt2text.service.d/auth.conf
sudo systemctl daemon-reload && sudo systemctl restart yt2text
```

Adding `tailscale serve --bg 8765` puts it behind HTTPS at `https://<machine>.<tailnet>.ts.net`, which also makes the clipboard API work properly.

### Running behind the company intranet (trusted proxy mode, optional)

The company intranet (romanty-intranet) checks its own login and forwards requests here. It is switched on by an environment variable; without it, everything above (solo use, team accounts) works exactly as before.

| Name | Kind | Meaning |
|------|------|---------|
| `INTRANET_PROXY_SECRET_FILE` | env var | Path to a secret shared only by the intranet and this server (32+ chars). Setting it turns the mode on. If the file can't be read or is too short the server refuses to start |
| `X-Intranet-Proxy-Secret` | request header | The secret, added by the intranet — compared in constant time against the file |
| `X-Intranet-Employee-Id` | request header | Intranet employee id (positive integer). Trusted only when the secret matches; becomes user `emp:<id>` |
| `YT2TEXT_BASE_PATH` | env var | Path prefix (e.g. `/apps/ytot`). Requests outside it get 404; every path and redirect in the UI carries it |
| `YT2TEXT_SAVE_PATH` | env var | Intranet page opened by the "save as intranet document" link (default `/ytot-save`, plus `?key=<entry key>`) |
| `YT2TEXT_KIT_PATH` | env var | Intranet design-kit CSS path (default `/apps/_kit/tool.css`, outside the prefix — served by the intranet) |
| `?theme=light\|dark\|system` | page query | The intranet frame page passes the initial theme on the iframe URL (`system` follows the OS). Later changes arrive as `postMessage({type:"intranet-theme"})` from the frame |
| `YT2TEXT_DATA_DIR` | env var | Data folder (default `yt2text_data/` next to the code) |

- The header and variable names deliberately leave out the tool's name — other tools attached to the intranet use the same contract
- In this mode password login, `/login` and the app's own session cookie are off (those paths return 404), and the 30-day team expiry is disabled — each member owns their records and deletes them themselves
- A request without the right secret header has no identity and gets 401
- The result view shows a "사내망 문서로 저장" (save as intranet document) link, only in this mode. It opens outside the frame (`target="_top"`)
- **Intranet UI mode**: for requests with a valid secret header the page hides its own title bar (the intranet frame draws the title) and loads the intranet design kit plus `intranet.css` to follow the intranet font, colors and radius. Light/dark/system comes from `?theme=` at first and then from the frame's `postMessage` (same origin, parent window and the three known values only); at phone widths (360–430px) nothing scrolls sideways, touch devices hide the shortcut hint and get finger-sized card buttons. The inline style hash is unchanged; the script hash changes because of the message listener (re-print with `--print-csp-hashes` and update the intranet tool registry). Screenshots: `docs/intranet-mode/`
- The UI has no inline event attributes (`onclick=` etc.), so the intranet proxy can send a `script-src` with hashes and no `'unsafe-inline'`. Print the hashes with `python yt2text.py --print-csp-hashes` (they don't change with the prefix or other settings)
- Container: the `Dockerfile` runs `gunicorn 'yt2text:wsgi_app()'` as a non-root user on `127.0.0.1:8765` with one worker and 8 threads (job state lives in memory, so never add workers). Data lives in `/data`; the Whisper model cache goes to a separate `/cache` volume (several GB, kept out of backups)

**Moving records over (one-off):** when switching to this mode, this command hands the owner's records to an intranet employee and deletes the team account's records. Run it with the server stopped.

```bash
python yt2text.py --migrate-to-employee <employee id>          # dry run — counts only
python yt2text.py --migrate-to-employee <employee id> --apply --backup-dir <where>  # copies the whole data folder first, then moves
```

Owner entries and folders get `user: "emp:<id>"` and transcript files are renamed to `emp__<id>__<key>.json`; team entries, folders and transcript files (including orphaned `team__*.json`) are deleted. It prints how many were moved and deleted, and where the backup is. `--backup-dir` must be outside the data folder (on a volume when run in a container). If a target name already exists, the dry run reports it and `--apply` stops before changing anything.

### Stability · security
- Host-header check + JSON Content-Type enforcement — stops other websites from silently driving your local server (CSRF/DNS-rebinding defense)
- Jobs interrupted by a server restart are surfaced as failed in the UI instead of spinning forever
- Finished transcription jobs are pruned from memory after 10 minutes
- Whisper jobs never pass the submitted link to yt-dlp — only `https://www.youtube.com/watch?v=<id>` rebuilt from the extracted video id (no arbitrary-URL fetches) — and the model must be one of the four offered in the UI
- Only the person who started a transcription job can poll or cancel it

## Version history

| Version | Notes |
|---------|-------|
| v4.7 | Intranet trusted proxy mode (secret header + employee id), path prefix, CSP-ready UI (no inline event attributes), record migration command, per-person history cap, Whisper URL/model restrictions, job-owner checks, Dockerfile (gunicorn, non-root) |
| v4.6.1 | Folders follow the video across extraction methods; new pulls land in the folder you're viewing |
| v4.6 | Two-account login (owner / team), per-account separation, 30-day expiry for team records, Tailscale-only access |
| v4.5 | History sorting (date / title / video length) |
| v4.4 | History folders (sidebar, drag-and-drop filing, per-folder filtering) and a re-extract button on every card |
| v4.3 | Top comments (5 picked by likes from YouTube's top-ranked candidates) saved with every extraction, shown above the transcript and included in Copy for AI |
| v4.2 | YouTube deep links & timestamp jumps, 50-video batches, caption pacing + automatic block recovery (+stop), long-video chunking, GPU auto-load with CPU fallback, Copy for AI with channel/description |
| v4.1 | Stability & security (atomic writes, split storage, job cancel, CSRF defense), Ubuntu home-server installer |
| v4 | Duplicate detection, batch extraction (queue/progress) |
| v3 | History (local storage, thumbnail cards, reopen/delete) |
| v2 | Whisper STT mode (videos without captions) |
| v1 | Caption extraction, paragraph cleanup, txt/md/srt export |

## Notes

- All data lives in the local `yt2text_data/` folder (index `history.json` · folders `folders.json` · transcripts `results/` · thumbnails `thumbs/` · session key `secret.key`) — never committed to git
- Whisper models download automatically on first use (large models are several GB and take a few minutes)
- Default binding is `127.0.0.1` with a Host-header check that blocks outside/cross-site access (`--host` to change; the check relaxes automatically for non-loopback binds)
- YouTube blocks are temporary — split large batches into smaller runs, and use Whisper mode for anything urgent
- The web UI is in Korean (this started as a personal tool). Everything else — install, service, exports — is language-neutral
