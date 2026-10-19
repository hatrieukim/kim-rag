# kim-rag: a personal lifelog you can ask questions

kim-rag records your day and turns it into memory an AI agent can search. An iPhone app records conversations
all day, a Mac app records online meetings and what you do on the Mac, a Chrome extension keeps the pages you
actually read, and a Zalo sensor keeps your chats. A server on your own Mac turns all of it into transcripts with
speaker names, a searchable verbatim index, a temporal knowledge graph and daily and weekly journals. Agents such
as Claude Code reach it through an MCP server.

It is built for one person, on one Mac, with Vietnamese as the main spoken language (the language is a setting).
The design notes are in [docs/idea.md](docs/idea.md).

> **Privacy.** Everything you record stays on your machine: audio, transcripts, journals, the databases and the
> logs live in `data/` and in two Docker volumes, all ignored by git. Text is sent only to the speech-to-text,
> embedding and LLM services you configure (Google Cloud and/or OpenAI) to be processed. Recording other people
> may require their consent where you live; check your local law before you turn this on.

## Architecture

```
 iPhone app ──┐ audio chunks (POST /chunks)
 Mac app ─────┤ meetings, room audio, app/window/clipboard/terminal events, calendar, Wi-Fi, sound
 Chrome ext. ─┤ pages read, selections (POST /events)
 Zalo sensor ─┤ chat messages, contacts (POST /events)
 Claude Code ─┘ session files read from ~/.claude/projects
                 │
                 ▼
        FastAPI server (`lifelog serve`, 127.0.0.1:8000)
                 │  raw files in data/raw, one row per chunk/event in Postgres
                 ▼
        Worker (`lifelog worker`, launchd every 5 min)
          VAD (Silero) → episodes → diarization (pyannote, local) → speech-to-text (Gemini / OpenAI / Chirp)
          → speaker identity → verbatim index (pgvector) → Graphiti facts (FalkorDB) → day/week journals
                 │
                 ▼
        MCP server (`lifelog mcp`): search, summary, people, timeline
```

Postgres is the source of truth; the vector index and the graph can always be rebuilt from it
(`lifelog reprocess`). Each stage is recorded per episode, so a failure is retried from where it stopped.

## Components

| Folder | What | Language |
| --- | --- | --- |
| [src/lifelog/](src/lifelog/) | Server, worker, CLI and MCP server (`lifelog` command) | Python 3.12, uv |
| [ios/](ios/) | iPhone recorder: all-day recording, background upload, Live Activity | Swift, SwiftUI |
| [mac/](mac/) | Mac menu bar app: activity, calendar, meetings, room mic, main window | Swift (SwiftPM) |
| [chrome/](chrome/) | Chrome extension: pages read, selections | JavaScript (MV3) |
| [zalo/](zalo/) | Zalo sensor (listen only) and Zalo PC backup importer | Node.js 18+ |
| [ops/](ops/) | LaunchAgent templates and the iPhone app renewal script | plist, zsh |
| [tests/](tests/) | Python test suite | pytest |

## Requirements

- macOS on Apple silicon (the Mac app needs macOS 14.4+ for meeting capture).
- [Docker](https://www.docker.com/) for Postgres (pgvector) and FalkorDB.
- [uv](https://docs.astral.sh/uv/) and `ffmpeg` (`brew install ffmpeg`).
- At least one speech-to-text service: a Google Cloud project (Gemini / Chirp, also used for embeddings and the
  graph) or an OpenAI API key.
- A Hugging Face token for pyannote speaker diarization (optional; without it every turn is `SPEAKER_01`).
- Optional: Xcode for the iPhone app, Node.js 18+ for the Zalo sensor, Chrome for the extension.

## Setup

```bash
docker compose up -d
uv sync
cp .env.example .env
```

Open `.env` and fill in at least:

1. **`HF_TOKEN`** for speaker diarization. Create a read token at huggingface.co/settings/tokens and accept the
   terms on the model page `pyannote/speaker-diarization-community-1`.
2. **`GOOGLE_CLOUD_PROJECT`** for `gemini-3.5-transcribe`, `chirp_3`, embeddings and the graph. Enable Agent
   Platform (Vertex AI) and Cloud Speech-to-Text in that project, then run `gcloud auth application-default login`.
3. **`OPENAI_API_KEY`** if you want `gpt-transcribe`.
4. **`API_TOKEN`** before the server is reachable from anything other than this Mac (the iPhone app).

Check that everything is in place:

```bash
uv run lifelog doctor
```

## Configuration

All server settings are environment variables, read from `.env`; [.env.example](.env.example) documents each
one. The clients keep their own settings:

| Client | Settings |
| --- | --- |
| Mac app | `~/Library/Application Support/kim-rag/mac.json` (server, token, ignore lists, features), opened from the menu |
| Zalo sensor | `~/Library/Application Support/kim-rag/zalo.json` (groups kept, blocked chats); server and token come from `mac.json` |
| Chrome extension | The extension's options page (server, token, blocked domains) |
| iPhone app | Settings screen in the app; the default server and your Apple team come from `ios/Config/Local.xcconfig` |

## Daily use

Transcribe one file and wait for the result:

```bash
uv run lifelog transcribe ~/Desktop/meeting.m4a
```

Or run the worker and drop files into `data/inbox/`; finished files move to `data/inbox/processed/`.

```bash
uv run lifelog worker
```

Results are in `data/episodes/DAY/TIME_ID.md`; the original audio in `data/raw/YYYY/MM/DD/`.

| Command | What it does |
| --- | --- |
| `lifelog status` | How many items are at each stage, which are stuck and why |
| `lifelog show ID` | Print the transcript of an episode |
| `lifelog compare ID` | Run the same turns through all three speech-to-text services and write a comparison |
| `lifelog reprocess --from transcribed --since 2026-10-01 --backend chirp_3` | Switch service and redo only speech-to-text |
| `lifelog reprocess --from diarize --episode ID` | Redo diarization |
| `lifelog retry` | Retry stuck items, e.g. after adding a key to `.env` |
| `lifelog search "question" --since 2026-10-01` | Search both memory stores; prints exactly what the agent gets from `search` |
| `lifelog reprocess --from ingest --all` | Rebuild the whole graph from Postgres |
| `lifelog reprocess --from index --all` | Re-embed, e.g. after changing `EMBED_MODEL` |
| `lifelog mcp` | MCP server for agents, over stdio |
| `lifelog journal 2026-10-13` | Rewrite the work journal of a day (today when omitted) |
| `lifelog summary 2026-10-13 [--week] [--detail]` | Print what the agent gets from the `summary` tool. Defaults to yesterday |
| `lifelog people "Tuấn"` | Print what the agent gets from the `people` tool |
| `lifelog timeline "Redis" --since 2026-10-01` | Print what the agent gets from the `timeline` tool |
| `lifelog serve` | HTTP server receiving `POST /chunks` and `POST /events` |

### Choosing a speech-to-text service

Set `ASR_BACKEND` in `.env` to `gpt-transcribe`, `gemini-3.5-transcribe` or `chirp_3`. To choose, run a few real
files and compare:

```bash
uv run lifelog compare --since 2026-10-13
```

`ASR_KEYWORDS` takes names and terms that come up often and helps `gpt-transcribe` and Gemini hear them right.
`GEMINI_MODEL` is the model id sent to Google; a new project may only get `gemini-3.5-transcribe-preview`. If Gemini
answers 403 or 404, try that value. Without `HF_TOKEN`, set `DIARIZER=none` to start right away; once you have a
token, set `DIARIZER=pyannote` and run `lifelog reprocess --from diarize --all`.

## Memory

After speech-to-text, each episode goes into two stores:

- **Verbatim, in Postgres.** The transcript is cut into windows of a few consecutive turns, each embedded with
  `gemini-embedding-2` (1,536 dimensions) and stored in the `windows` table with pgvector. This answers "who said
  exactly what, and when".
- **Facts, in Graphiti on FalkorDB.** `gemini-3.8-flash` reads the transcript and extracts people, places, tasks and
  facts with validity periods. The `graph_parts` table records each part ingested, the tokens spent, and the way
  back from a fact to the exact turns in Postgres.

The graph can be rebuilt: delete the FalkorDB volume and run `lifelog reprocess --from ingest --all`. Browse it at
http://127.0.0.1:3001 (graph `lifelog`). Unnamed speakers appear as e.g. `Người nói 1 (bản ghi ab12cd34)`
("speaker 1 (recording ab12cd34)"): a `SPEAKER_01` label only means something inside one episode, so the episode id
keeps Graphiti from merging two different people. Set `GRAPH=none` to turn Graphiti off; episodes then stop at
`indexed`.

### Connecting Claude Code

`.mcp.json` in this folder declares the `lifelog` server, so Claude Code opened here offers to enable it. To use
it from any folder:

```bash
claude mcp add --scope user lifelog -- uv run --directory /path/to/kim-rag lifelog mcp
```

| Tool | Arguments | Returns |
| --- | --- | --- |
| `search` | `query`, optional `since`, `until`, `speakers`, `limit` | The closest verbatim passages and Graphiti facts, each with time, episode id and turn numbers |
| `summary` | optional `date`, `period` (`day` or `week`), `detail` | The day journal, or the week summary, of that date. Defaults to yesterday |
| `people` | `name` | Everything known about one person: recordings, who else was there, calendar, chats, facts |
| `timeline` | `topic`, optional `since`, `until`, `limit` | Facts and passages about a topic, oldest first, including facts later replaced |

`search` is semantic, not exact-keyword. Passages more than 0.15 below the best one are dropped and overlapping
passages of one episode are merged. `since` and `until` filter by when something was said. If one store cannot be
reached, the other still answers, with a note. The tools are declared in
[src/lifelog/mcp_server.py](src/lifelog/mcp_server.py); the work is in [src/lifelog/search.py](src/lifelog/search.py).
Reconnect the server in Claude Code after changing the code.

## Work journal (Claude Code sessions)

Claude Code writes every session as JSONL in `~/.claude/projects/`. The worker reads those files; no hook or extra
process is needed. List the project folder names it may read in `.env`:

```
CLAUDE_CODE_PROJECTS=my-app,my-other-app
```

Empty means nothing is read; a project not in the list is never opened. From each session only three things are
kept: what you typed, what Claude answered, and what Claude did (files edited, commands run, by description). Tool
results are not kept: they are long, noisy and may contain keys or `.env` contents. Strings that look like keys or
passwords are masked before storage.

Turns of one session less than an hour apart form one `code` episode; an open session is grouped after an hour of
silence. Gemini Flash writes a summary of each episode; the full dialogue and the summary go into the verbatim
store, and only the summary goes into Graphiti. The journal of a day (`data/journal/DAY.md`) is written once the
day is over and all its sessions are summarized, and rewritten if more sessions of that day arrive. Session
content of the selected projects is sent to Google (your Cloud project) to be summarized, embedded and turned into
facts.

### Conversations, weeks, people and timelines

The day journal also has a conversations section: one heading per recorded conversation or online meeting (time,
where it was recorded, who spoke, episode id) and a few sentences written by Gemini Flash. A recording under 300
characters is quoted instead. Naming a voice clears the summary, so the conversation and its day are written again
with the name.

When a week (Monday to Sunday) is over and all its days are written, the worker writes
`data/journal/week-MONDAY.md` from the day journals: main work, results, people met, open items, then each day's
overview. The `summary` tool returns the saved journal when it is up to date and writes it on the spot otherwise;
today and the current week are written from the data so far and not saved.

`people` finds a named voice by exact name or by whole words ("Tuấn" finds "anh Tuấn", not "Tuấnh") and adds
Graphiti's own summary of the person and the newest facts about them. `timeline` asks Graphiti for 60 facts, keeps
those within 0.15 of the closest, then asks Gemini Flash which facts and passages are really about the topic; if
the LLM cannot be reached, facts within 0.1 are kept and the answer says so.

Other sources can send events with `POST /events`, an array of `{uid, source, type, ts, payload}`. Sending the same
`uid` again does not create a duplicate.

## Mac app

`KimRag Mac` ([mac/](mac/), Swift) runs in the background from login, with a menu bar icon. It records:

- the frontmost app and its window title (needs Accessibility permission),
- when you are away: no mouse or keyboard for 5 minutes, screen locked, Mac asleep,
- text you copy,
- terminal commands, through the zsh hook [mac/kim-rag.zsh](mac/kim-rag.zsh) (commands starting with a space are not
  recorded),
- calendar events, the Wi-Fi network name, sound the Mac plays out loud, and online meetings (below).

It never records keystrokes, never takes screenshots and never reads message contents. All filters run on the Mac
before anything is sent: ignored apps (password managers) record nothing, titles matching hidden patterns (private
tabs, banking) are dropped, copies from password managers are dropped, and keys, passwords and card numbers are
masked. ⌃⌥⌘P pauses and resumes everything. Settings are in `~/Library/Application Support/kim-rag/mac.json`.

Events wait in a local queue and are sent in batches to `POST /events`, so nothing is lost while the server is
down. The worker groups raw events into time spans (`mac_spans`): same app and same context (project in VS Code,
site in Chrome, connection in Royal TSX) are merged, glances under 30 seconds are dropped, time away is not counted.
App rules are the `RULES` table in [src/lifelog/mac_log.py](src/lifelog/mac_log.py). After changing them:

```bash
uv run lifelog mac --rebuild
uv run lifelog mac 2026-10-14      # one day, today by default
```

Commits of the selected projects are read with `git log` from `GIT_SINCE`. The day journal gets a Mac activity
section: time per app and context, commits, notable commands (ssh, docker, deploy, failed commands), computed by
code; the LLM only reads it to write the overview.

Install, or reinstall after changing the Swift code:

```bash
mac/install.sh
echo 'source "$HOME/Library/Application Support/kim-rag/kim-rag.zsh"' >> ~/.zshrc
```

On first start macOS asks for Accessibility (System Settings › Privacy & Security › Accessibility). `install.sh`
signs with your Apple Development certificate when there is one (override with `KIMRAG_SIGN_IDENTITY`) so
permissions survive rebuilds; an ad-hoc signature resets them on every build.

### Main window

Click the Dock icon, the open button in the menu bar panel, or ⌘0. Closing the window keeps the app recording; only
Quit stops it. It reads the server's `/app/*` routes ([src/lifelog/views.py](src/lifelog/views.py)) with the token
from `mac.json`, so the API server must be running. Sections: Today (counts, time per app, iPhone recording,
calendar, day journal or week summary), Conversations (every episode of the day, turn by turn, with playback), Zalo,
Search (or the timeline of a topic), People (name voices, read a person's page) and System (`lifelog doctor`, the
queue, failures). The icon is drawn by [mac/icon/make-icon.swift](mac/icon/make-icon.swift).

### Calendar

The Mac app reads the macOS calendar (every 15 minutes and on change), the last 3 days and the next 7: title, time,
location, calendar name and attendees; notes and meeting links are never read. `ignoreTitles` and
`ignoreCalendars` (birthdays and holidays by default) filter it; `"calendar": false` turns it off. Attendees of a
meeting during a recording are offered as names on the speakers page and given to the LLM as a hint. Put your own
addresses in `MY_EMAILS` so invites sent to another of your addresses still count as you.

### Sound the Mac plays

A video playing out loud on the Mac reaches the iPhone microphone. The Mac app checks Core Audio every 2 seconds and
sends a `sound` event naming the apps playing on a device the room can hear (not muted, not at zero volume, not
headphones; apps using the mic are left out, since a call is a meeting). The server then leaves out the iPhone
speech during that sound and keeps the original in the chunk metadata (`speech_before_mac_sound`). Sounds under 8
seconds are ignored. See [src/lifelog/mac_sound.py](src/lifelog/mac_sound.py); `headphones`, `soundIgnoreApps` and
`"sound": false` in `mac.json` tune it.

### Online meetings

When a meeting app uses the microphone, the Mac app records two channels: the mic that app uses (you) and the app's
own sound through a Core Audio process tap (everyone else), so it works with headphones too. A meeting ends 30
seconds after the app stops using the mic. Meeting apps are the bundle ids in `meetingApps` (Teams, Zoom, Chrome,
Edge, Arc, Slack, FaceTime, Webex, Zalo, Discord by default); for a browser the whole browser's sound is recorded.
`"meeting": false` turns it off.

Audio is written to `~/Library/Application Support/kim-rag/meetings/`, cut every 5 minutes, encoded to stereo AAC
and uploaded to `POST /chunks` with `source` `mac-meeting`; files are deleted once the server has them. One meeting
is one episode. The iPhone's speech during those minutes is cut so the meeting is transcribed once. The voice mostly
on the mic channel is linked to your enrolled voice. See [src/lifelog/meeting.py](src/lifelog/meeting.py). macOS asks
for Microphone and "System Audio Recording Only" the first time.

## Chrome extension

The extension in [chrome/](chrome/) records the pages you actually read: the visible tab of the focused window,
with mouse or keyboard activity in the last 3 minutes. It sends time spent per page (per minute), the main text of
pages read for more than 30 seconds (extracted with [Readability](https://github.com/mozilla/readability) 0.6.0,
vendored in `chrome/vendor/` under its own license), and text you select and copy.

It never records private windows, form fields, pages with a password field, machines on private networks, or the
blocked domains: mail, banks, password managers and sign-in pages by default. **Add your own internal systems
(your company intranet, for example) on the extension's options page.** Keys, passwords and card numbers are masked
before sending. Each article becomes a `read` episode, embedded for `search`; articles do not go into Graphiti and
are not summarized by an LLM.

Install: open `chrome://extensions`, turn on Developer mode, click "Load unpacked" and choose the `chrome/` folder.
Set the server and token on the options page.

## Zalo messages

The sensor in [zalo/](zalo/) listens to your Zalo account with [zca-js](https://github.com/RFS-ADRENO/zca-js), an
unofficial library that acts like a Zalo Web session. It only listens: it never sends a message, a reaction or a
"seen". It sends every message of direct chats (yours included), every message of the groups you allow, and for
other groups only that a message happened (type, length, whether you were mentioned), never who wrote it or what it
said. Photos, files and voice messages become short labels such as `[file: quote.pdf]`; nothing is downloaded.
Secrets are masked with the same rules as the Chrome extension.

Two things to know: Zalo allows one web session per account, so opening Zalo in a browser closes the sensor (use
the Zalo desktop app instead). And Zalo may lock accounts that use unofficial clients; the sensor behaves like a
normal Zalo Web tab to lower that risk but cannot remove it. **Use it at your own risk.**

```bash
zalo/install.sh                 # npm install, then start the LaunchAgent
cd zalo && npm run login        # scan the QR code with the Zalo app once
uv run lifelog zalo --threads   # what came in, per chat, with the ids for zalo.json
```

`groups` in `zalo.json` lists the groups kept in full and `blocked` the chats never recorded, by id or exact name.
The login session, queue and state live in `~/Library/Application Support/kim-rag/zalo/`, readable only by you.
Restart after editing: `launchctl kickstart -k gui/$(id -u)/com.kim.kimrag.zalo`. Run `zalo/install.sh` again after
upgrading Node.

Direct chats become one episode per chat per day, searchable and, from `GRAPH_CHAT_SINCE`, ingested into Graphiti.
Link a Zalo person to a named voice with `lifelog zalo-link` so `people` sees one person.

### Loading older messages from a Zalo PC backup

The sensor only hears messages from the moment it logs in. Older messages come from a Zalo PC backup: its message
file ends in `_zmessage.zdb`, one message per line ([docs/giai-ma-backup-zalo.md](docs/giai-ma-backup-zalo.md)
explains how to decrypt your own backup). `zalo/import.js` sends every message through the same rules as the
sensor; running it twice is safe.

```bash
cd zalo && node import.js /path/to/<account id>_zmessage.zdb --since 2026-07-15 --dry-run
cd zalo && node import.js /path/to/<account id>_zmessage.zdb --since 2026-07-15
```

Without `--since` it loads the last 90 days. The backup holds all your messages in plain text: keep it somewhere
safe, outside this repository, or delete it once loaded.

## iPhone recorder

The app in [ios/](ios/) records all day with the iPhone's microphone and uploads to your server.

- 16 kHz mono AAC in ADTS files, cut every 5 minutes without a gap; ADTS stays readable if iOS kills the app.
- Runs in the background next to music and other audio; resumes by itself after calls, Siri or a mic change.
- Each chunk carries metadata: why it ended, microphone, battery, rough location, motion, calls, "mark" presses.
- Uploads run in a background URLSession to `POST /chunks`; a chunk is deleted only after the server accepts it.
- The lock screen shows a quiet Live Activity with a bookmark button; the orange mic dot is shown by iOS.

The server stays on your Mac. The phone can reach it through [Tailscale](https://tailscale.com/):
`tailscale serve --bg 8000` publishes the API at `https://<mac>.<tailnet>.ts.net`, reachable only from your tailnet.
Set `API_TOKEN` in `.env` and the same token in the app.

Build with a free Apple ID (Personal Team) or a paid account:

```bash
cp ios/Config/Local.xcconfig.example ios/Config/Local.xcconfig   # then set DEVELOPMENT_TEAM and KIMRAG_SERVER
open ios/KimRagRecorder.xcodeproj                                 # choose your iPhone and Run
```

From the command line:

```bash
cd ios && xcodebuild -project KimRagRecorder.xcodeproj -scheme KimRagRecorder -destination 'generic/platform=iOS' \
  -configuration Release -derivedDataPath build -allowProvisioningUpdates build
xcrun devicectl device install app --device <UDID> ios/build/Build/Products/Release-iphoneos/KimRagRecorder.app
```

Trust the developer the first time in Settings › General › VPN & Device Management. The bundle ids
(`com.kim.kimrag.recorder`) must be unique to your team; change `PRODUCT_BUNDLE_IDENTIFIER` in Xcode if signing
complains.

A free profile expires after 7 days, so the Mac can renew it: [ops/ios-renew.sh](ops/ios-renew.sh) runs every 30
minutes and, once the copy on the phone has under 2 days left, rebuilds with a fresh profile, tells the app to close
its current chunk, reinstalls over Wi-Fi and relaunches it. It reads `DEVELOPMENT_TEAM` and `IPHONE_UDID` from
`ios/Config/Local.xcconfig`. The phone must be unlocked and on the same network; with under a day left the Mac shows
a reminder.

```bash
ops/install-agent.sh com.kim.kimrag.ios-renew
ops/ios-renew.sh --force      # renew now
lifelog recorder              # today: hours recorded, gaps and why
```

## Who is speaking

Diarization only labels voices `SPEAKER_01`, `SPEAKER_02` inside one recording. kim-rag links those voices to people
across recordings. Enrol your own voice once from 30+ seconds of you talking:

```bash
lifelog speakers enroll path/to/my-voice.m4a --me
```

Each new recording is then matched against known voices before indexing: a close enough voice (cosine ≥
`SPEAKER_MATCH`) gets that name, an unmatched one becomes an unknown voice that is recognised again on later days,
and the LLM suggests a name when someone is called by name, plus a role label. Open
http://127.0.0.1:8000/speakers to name voices worth naming (heard on 2+ days, or 1+ minute in one conversation):
listen to a 6-second clip, pick or type a name, or mark it not important. Naming a voice relabels and re-indexes
every recording with it.

Named people keep their voiceprints and a 10-second sample; unnamed voices are deleted after `STRANGER_KEEP_DAYS`
without being heard.

```bash
lifelog speakers                  # people, voices waiting for a name, share of speech that has a name
lifelog speakers rescan           # match recordings indexed before this feature existed
lifelog speakers name 8613 "Tuấn"
lifelog speakers ignore 8613
```

## Running in the background

The worker runs from launchd at login and every 5 minutes, does all pending work and exits; a run still going is
never overlapped. The API server runs permanently. The plists in [ops/](ops/) are templates; `ops/install-agent.sh`
fills in the paths for your checkout, installs them in `~/Library/LaunchAgents` and starts them:

```bash
ops/install-agent.sh com.kim.lifelog.worker
ops/install-agent.sh com.kim.lifelog.api
launchctl print gui/$(id -u)/com.kim.lifelog.worker | grep -E "state|last exit"
launchctl kickstart gui/$(id -u)/com.kim.lifelog.worker      # run now
launchctl bootout gui/$(id -u)/com.kim.lifelog.worker        # remove
```

Logs are in `data/logs/`. Docker must be running (turn on "Start Docker Desktop when you sign in"); if it is not,
the run logs an error and tries again 5 minutes later. Several workers at once are fine: each job is taken by one
worker, and Graphiti ingestion is always done by one.

## Sending audio over HTTP

```bash
curl -F audio=@meeting.m4a -F 'metadata={"started_at":"2026-10-13T09:00:00+07:00","client_id":"iphone-0001"}' \
  http://127.0.0.1:8000/chunks
```

Sending the same file or the same `client_id` again returns the existing id with `"duplicate": true`.

## Inside the pipeline

1. `received`: the file is stored and recorded.
2. `vad`: Silero removes silence; files without speech stop at `no_speech`.
3. `grouped`: files with speech less than 10 minutes apart form one episode.
4. `diarized`: pyannote separates speakers locally and keeps voice embeddings.
5. `transcribed`: each turn (up to 50 seconds) goes to the speech-to-text API; markdown is written.
6. `indexed`: windows are embedded into Postgres.
7. `ingested`: parts are ingested into Graphiti, each part saved as it completes.

Reprocessing from a stage drops the memory built from the old version: vectors are deleted and old parts are
removed from the graph first. `code` episodes have no audio and start at `transcribed`. Known limit: a single file
always stays within one episode, even with more than 10 minutes of silence inside it.

## Tests

```bash
docker compose up -d     # the tests use a separate database, lifelog_test, in the same Postgres
uv run pytest
node --test chrome/scrub.test.js
cd zalo && npm test
cd mac && swift test
```

The Python tests use fake speech-to-text, embedding, graph and LLM services, generate audio with the macOS `say`
command, and create Claude Code sessions in a temporary folder. They call no real API, do not touch FalkorDB and
do not read `~/.claude`.

## License

[MIT](LICENSE), Copyright (c) 2026 hatrieukim. `chrome/vendor/Readability.js` is Mozilla's Readability under the
Apache License 2.0 ([chrome/vendor/Readability-LICENSE.md](chrome/vendor/Readability-LICENSE.md)).
