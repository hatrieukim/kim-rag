# Personal lifelog: 24/7 audio + Mac activity → Graphiti

Oct 13, 2026 · the original design notes. Some choices changed during implementation (for example speech-to-text
runs through cloud APIs rather than WhisperX, and Redis was never needed); the README describes what was built.

## Goal and decisions

Goal: capture almost every daily interaction (conversations + activity on the Mac), process it into structured
data, and open it to other agents through MCP.

Decisions made:

| Area | Decision | Main reason |
| --- | --- | --- |
| Voice capture device | A self-written iOS app on the iPhone already in use, installed with Xcode and a free Apple ID | No cost, data goes to your own server; Omi is already the cheapest option ($89), Limitless was shut down after Meta bought it |
| Second source | Mac daemon + Chrome extension + Claude Code watcher | Most of the working day happens on the Mac; richer than voice |
| RAG layer | Graphiti (Zep's open source), self-hosted on FalkorDB | Episode + fact model with validity windows over time, hybrid search, an official MCP server |
| Output | A thin MCP server of our own wrapping Graphiti | Agents do not depend on Graphiti directly; swapping the engine does not affect them |
| Source of truth | Raw audio + original episodes (transcript + metadata), independent of Graphiti | Every layer above can be recomputed |

Every source sends the same simple contract; Postgres holds the source of truth, Graphiti is a recomputable layer,
and agents depend only on our thin MCP server.

## Data volume for one year

About 15–30 million tokens a year, much less than it first feels. Assumptions: 2–4 hours of speech a day, 130–150
words a minute, Vietnamese costs more tokens than English.

| Quantity | Estimate |
| --- | --- |
| Words per day | 20–35 thousand |
| Words per year | 7–13 million |
| Tokens per year | 15–30 million (about 100–150 books) |
| Plain text | 50–100 MB |
| Opus-compressed audio | 10–25 GB |
| 500-token chunks | ~50 thousand |
| Postgres/graph after processing | under 2 GB |

Two consequences: scale is not the problem; the problem is dirty, diluted data (ASR errors, overlapping speakers,
small talk making up most of it). And an LLM can read everything at ingest time, since 30M tokens through a cheap
model costs a few to a few tens of USD a year, so the effort should go into processing at write time.

## Recording device

Conclusion: build one or use a phone. No AI pendant is cheaper than Omi while fitting 24/7 recording, Vietnamese
and your own server.

### Off the shelf

| Device | Price | Notes |
| --- | --- | --- |
| [Omi](https://www.omi.me/pages/pendant) | ~$89, subscription optional | Open source, cheapest AI pendant; the free plan has only 1,200 cloud minutes a month, not enough for 24/7 |
| Bee (Amazon) | ~$49.99 | Cheapest, but owned by Amazon since 7/2025, reportedly US-only; Vietnamese support unclear |
| Limitless Pendant | — | [Bought by Meta 12/2025, no longer sold](https://9to5mac.com/2025/12/05/rewind-limitless-meta-acquisition/); lesson: a device dies when its vendor shuts the service |
| Plaud NotePin | ~$169 + subscription | Press to record meetings, not always-on |
| Fieldy 3 | $129.99–$179 | Only 150 transcription minutes a month included |
| Anker soundcore Work | $159 | 8-hour battery, no voice activation; a meeting recorder |
| Voice-activated recorder (local marketplace) | roughly $12–28 | 20–30 hour battery, records on sound; manual file transfer; cheapest way to get real data now (price unverified) |

### Build your own

| Option | Estimated cost | Pros / cons |
| --- | --- | --- |
| Seeed XIAO ESP32S3 Sense + 800–1000 mAh battery + microSD | $25–35 | Built-in mic, Wi-Fi/BLE, same family as the Omi dev kit so there is open firmware to learn from; ESP32 is power-hungry with Wi-Fi on, 12–16 hours per charge in practice |
| M5Stack StickS3 | ~$21.50 | Case, screen, mic, 250 mAh battery, no soldering; good for prototyping firmware, battery lasts only a few hours |
| XIAO nRF52840 Sense | — | Base of the Omi dev kit, very low power; BLE only, must sync through a phone |
| ESP32 + separate INMP441 mic | $6–8 | Cheapest, free choice of mic and placement; needs soldering and a case |
| Raspberry Pi Zero 2W + USB mic + power bank | — | Runs Linux, VAD + Whisper tiny on device; bulky, power-hungry |

Common difficulties: battery, noise from the mic rubbing on clothes (the most common failure), and the most work
is server-side software, not hardware.

### A dedicated Android phone

If going with a separate phone: an old Pixel 4a is the most sensible (stock Android does not kill background apps,
3.5 mm jack for a lavalier mic, small; needs a new battery and should be single-purpose since it no longer gets
security updates). A Samsung A0x for a new phone with a big battery. Avoid Xiaomi/Oppo/Realme/Tecno ROMs, which
often kill background apps. Unihertz Jelly Star ($219.99, 3-inch screen) only if size matters most; its 2000 mAh
battery is weak.

Final decision: use the existing iPhone and write the app (next section).

## iOS voice capture app

Installable without the Apple Developer Program: Xcode + a free Apple ID (Personal Team). Limits per
[Apple](https://developer.apple.com/support/compare-memberships/): profiles expire after 7 days, at most 3 apps per
device, some advanced capabilities unavailable. Background audio still works since it is only an Info.plist
declaration.

### Installing and keeping it alive

1. Xcode → Settings → Accounts → add the Apple ID; in the target's Signing choose "Your Name (Personal Team)".
2. Turn on Developer Mode on the iPhone; after the first build, trust the certificate in Settings → General → VPN &
   Device Management.
3. Re-run from the Mac every week, or use SideStore: a computer once (iloader), then refresh on the iPhone through
   LocalDevVPN, automatable with Shortcuts. SideStore is also limited to 3 apps, itself included.

### iOS limits the code must handle

- Recording must start while the app is open; if it is killed or the phone restarts, recording stops for good.
- Calls, Siri and other apps taking the mic cause interruptions → catch them and resume.
- Plugging or unplugging the charger does not stop recording. Cases to watch: rebuilding from Xcode kills the app;
  mic adapters with pass-through charging cause route changes; CarPlay may take the audio session; a hot phone only
  slows down.
- The orange dot is always on; the main phone's battery drains faster; a mic in a pocket sounds muffled → a wired
  lavalier mic helps.

### App architecture (keep the app as dumb as possible)

1. AVAudioEngine, mono 16 kHz, AAC, a new file every 5 minutes.
2. A local upload queue; a background URLSession uploads on Wi-Fi; files are deleted only after the server ACKs.
3. One button to stop recording.
4. No ASR and no complex VAD on device; every improvement happens on the server.

### Metadata sent with each chunk (JSON sidecar)

| Group | Field | Used for |
| --- | --- | --- |
| Required | Start/end time + timezone | The main axis of all data |
| Required | Coordinates + accuracy | Reverse-geocode into "home", "office", "café X" |
| Required | Mic in use, battery, charging, why the chunk ended | Debugging audio quality, explaining gaps |
| High value | Motion state (CoreMotion) | Telling a seated meeting from talking while walking |
| High value | Connected Wi-Fi name | Indoor location, steadier than GPS |
| High value | Current calendar event (EventKit): title, attendees | The best source for naming speakers |
| High value | Whether a call is in progress (CallKit, state only) | Context |
| Manual | A "mark" button for important moments; quick tags | Extraction priority |

Not collected: messages, email, photos, web history (iOS does not allow it and it is out of scope). Apple Watch
heart rate is a later step.

## Second source: activity on the Mac

Voice tells what was said to whom; the Mac tells what you were doing, reading and writing. The main work happens in
Chrome, Claude Code, Zalo and Royal TSX, so full-screen capture is not needed; each app has a better source than
OCR.

| App / source | How | What | Not collected |
| --- | --- | --- | --- |
| Chrome | A self-written extension, installed unpacked | Active tab changes (URL, title, time); main text of pages read for over 30 s (Readability, URL hash to avoid repeats); selected/copied text | Form contents; domains on the ignore list; private tabs |
| Claude Code | Watcher reading the JSONL in `~/.claude/projects/` + hooks (Stop, PostToolUse) | The whole conversation: repo, problem, decisions, files edited | — (the highest quality source, nearly free) |
| Zalo | OS level: active app, window title | Who you are chatting with and for how long; content only when you copy it | Message content (of other people); per-conversation OCR only if ever needed |
| Royal TSX | Window/tab title | Which machine you connected to, when, how long | Pixels inside RDP; SSH commands are logged by the shell on the target server |
| Online meetings (Teams/Meet) | The app's audio via ScreenCaptureKit + mic, only while a meeting app is open and the mic is on | Meeting audio → same ASR pipeline, iPhone recording paused meanwhile | — |
| General | Swift daemon: NSWorkspace + Accessibility; clipboard; shell/git hooks | Active app, window title, clipboard (regex-filtered for tokens/cards), terminal commands, commits | Full keylogging |

A screenshot + OCR layer (Screenpipe) is only worth considering later, for a clear need: 1–2 GB of images a day if
kept, a lot of noise, the highest privacy risk.

Every source posts to the same `/events` endpoint with the schema `{ts, source, type, payload}`. On the server the
Mac is a stream parallel to voice, on the same time axis; when building an episode, events of the same period are
added as context. A daily work journal can be produced from events alone, without voice.

Privacy filters from day one: an ignore list of apps/domains (banking, password managers), regexes blocking
tokens/API keys/card numbers, and one shortcut that pauses all recording on the Mac.

Order by value over effort: (1) Claude Code watcher, (2) OS-level daemon + clipboard, (3) Chrome extension, (4)
online meeting audio, (5) Zalo OCR later.

## Server: ingest and processing pipeline

Principle: keep raw data untouched; everything above it can be recomputed, because ASR models, embeddings and
prompts will change in the first year.

### Ingest

FastAPI with `POST /chunks` (audio + metadata) and `POST /events`. Files are stored in `raw/YYYY/MM/DD/`, a
`chunks` row is written with status `received`, and a job is queued. Idempotent: the same chunk sent twice does not
create a duplicate, since mobile uploads retry often.

### Worker steps

1. VAD (Silero): drop silence, cutting 60–80% of what needs transcribing.
2. Episode grouping: adjacent chunks with speech less than 10 minutes of silence apart → one conversation. The
   episode is the base unit.
3. Diarization (pyannote) before ASR, since ASR on single-speaker segments is cleaner.
4. ASR: WhisperX (ASR + word timing + pyannote bundled) or PhoWhisper for Vietnamese. Without a GPU, run batches at
   night; a typical VPS has no GPU and ASR may take 10–20× real time.
5. Speaker matching across time (below).
6. Ingest the episode into Graphiti with `add_episode`: the transcript with speakers + context (time, place,
   calendar event, open apps). Mac events also go in through `add_episode` as JSON.

Each episode has a `stage` column (received → diarized → transcribed → speakers → ingested). The worker only takes
work at the next stage; a failure is retried from there. `reprocess --from <stage> --since <date>` reruns
selectively when a model or prompt changes.

### Diarization and speaker identity

Standard diarization (VAD → ECAPA/WeSpeaker voice embeddings over 1–2 s windows → clustering → merge with ASR)
only works within one file: today's `SPEAKER_01` and tomorrow's are unrelated. The cross-time identity layer has to
be built:

- A `speakers` table: each person keeps several embeddings (not one centroid, since the voice changes with a cold
  or over the phone). After each episode, compare each cluster's centroid with known speakers by cosine; above a
  threshold (~0.7, to be tuned) assign it, below it create a new speaker.
- Recognise "me" first with a few minutes of enrollment. That step alone separates "I said" from "someone else
  said".
- Naming other people: an LLM suggests names from calendar attendees, names used in the conversation and familiar
  places; you confirm in a small UI with a 5-second clip. 10–20 confirmations in the first week, mostly automatic
  after that.
- When a name is confirmed, rescan old episodes and assign it; cheap since embeddings are stored.
- Keep a short audio sample per speaker forever, separate from the audio deletion policy.

What breaks results: overlapping speech (10–15% of natural conversation), a mic in a pocket merging distant
speakers, utterances under 1 s are nearly impossible to assign, meetings of 6–8 people get worse quickly (pass
`num_speakers` from the calendar when known). Expect about 10–15% of duration wrong in a quiet meeting room, 25–30%
in a café.

Cost: pyannote on an 8-core CPU handles 3–4 hours of audio in about an hour; a few minutes on a GPU.

### Storage

| Data | Where |
| --- | --- |
| Raw audio | Disk or MinIO, by day |
| chunks, events, episodes, segments (speaker, start, end, text, confidence), speakers | Postgres |
| Graph: entities, facts with valid_from/valid_to, episodes, communities | Graphiti on FalkorDB |
| Embeddings | In Graphiti; store the model name + version next to every vector |

Suggested embedding model: bge-m3 locally, fine for Vietnamese. Extraction LLM: Claude or Gemini; a cheap model for
ingest, roughly 100–300k input tokens a day, a few USD a month.

## RAG layer: Graphiti

[Graphiti](https://github.com/getzep/graphiti) (Zep's open source), self-hosted on FalkorDB, was chosen because it
matches the design almost point by point: it takes data as episodes; each fact has a validity window, and old facts
are invalidated rather than overwritten; the graph updates incrementally; retrieval is hybrid semantic + BM25 +
graph traversal with no LLM call at query time; and there is an official MCP server.
[Thoughtworks Radar, April 2026](https://www.thoughtworks.com/en-us/radar/platforms/graphiti) puts it at Trial.
Neo4j is the main backend; FalkorDB is lighter and enough for one user.

### Why not something else

| Option | Why not |
| --- | --- |
| Zep Cloud | Same engine, but your whole life sits on their servers |
| Ragie, Vectara, OpenAI file search | Document RAG with no notion of time or speakers; cloud; Vectara starts at $100,000/year |
| Mem0 | Good for chatbots remembering preferences, shallow for questions over time |
| Cognee | Same graph + vector + MCP class, strong at ingesting many document types; Graphiti is strong on time, the main axis here; the article ranking Cognee first is their own blog |
| LlamaIndex, LangChain | Frameworks to assemble yourself, no savings in effort |

### What still has to be built

- Before Graphiti: ingest, VAD, ASR, diarization, episode grouping, calling `add_episode`.
- After Graphiti: our own thin MCP server with 5–6 tools: `search(query, from, to, people)`, `get_episode(id)`,
  `summary(date | week)`, `query_facts(filter)`, `people(name)`, `timeline(topic)`, calling Graphiti inside. Daily
  and weekly summaries written separately if community summaries are not enough.

The `search` tool should: extract time/person/place filters from the question with a small LLM; run vector and BM25
in parallel on the filtered set; rerank (bge-reranker) to the top 5–8; return a few segments before and after each
result.

### Test early before committing

- Vietnamese: entity extraction and embeddings depend on the configured models, not Graphiti. Try 10 real episodes
  with Claude/Gemini + bge-m3.
- LLM cost at ingest: several calls per episode to extract facts and reconcile them with old ones.

## How easy is it to swap a component

Rule: components that only talk through a simple contract are easy to swap; components holding accumulated state
are hard. The cost of a swap is recomputation time from the original episodes, not data loss.

| Component | Difficulty | What a swap takes |
| --- | --- | --- |
| Capture sources (iOS app, Mac daemon, extension, watcher) | Easy | They only know `/chunks` or `/events` and `{ts, source, type, payload}`; the server does not care |
| ASR | Easy | Same output shape; `reprocess --from transcribed` for old data |
| LLM inside Graphiti | Easy | One line of config |
| Diarization model | Easy | See speaker memory below |
| Embedding model | Medium | Re-embed every node and edge, a few hours; never mix two models; store the model name with the data |
| Graph DB (FalkorDB ↔ Neo4j) | Medium | No ready migration; re-ingest every episode, paying the extraction LLM cost again |
| Speaker memory | Hard | A new voice-embedding model makes old centroids meaningless; recompute from the original audio and re-match manual labels; lost if the audio was deleted → keep a sample per speaker forever |
| Graphiti | Hardest | The fact/entity schema is its own; the graph is thousands of accumulated LLM calls; swapping means re-ingesting from scratch |

Two cheap ways to lower the Graphiti risk: agents only call our thin MCP, never Graphiti's MCP directly; and the
original episodes (transcript + metadata) are the source of truth, kept in Postgres/files independent of Graphiti,
re-ingestable into any engine.

Consequence for the audio policy: to keep the option of swapping ASR and diarization later, keep audio longer than
90 days.

## Privacy, audio retention, rollout order

### Privacy

- Recording other people requires their consent. Vietnam's Personal Data Protection Law takes effect in 2026; this
  kind of data is extremely sensitive.
- Process locally or on your own server, encrypt at rest, and support deletion by person or by time range.
- A stop button on the iPhone, a pause shortcut on the Mac, app/domain/regex filters before sending.
- Audio retention: lean towards 90 days then delete, except marked segments and each speaker's sample; consider
  keeping longer to preserve the option of swapping ASR/diarization later.

### Rollout order

- [ ] Ingest server + pipeline (VAD, diarization, ASR, episode grouping), tested with Voice Memos files. No app
  needed yet.
- [ ] docker-compose: FalkorDB + Graphiti + MCP server; try one Vietnamese transcript with 10 episodes.
- [ ] Our thin MCP with `search` and `get_episode`; check that RAG answers real questions.
- [ ] Claude Code JSONL watcher → `/events`.
- [ ] OS-level Mac daemon + clipboard + filters.
- [ ] iOS app: background recording, resume after interruptions/route changes, file splitting, upload, sidecar
  metadata.
- [ ] Chrome extension.
- [ ] Online meeting audio on the Mac.
- [ ] Cross-time speaker identity + name confirmation UI.
- [ ] Daily/weekly summaries, the remaining MCP tools (`summary`, `query_facts`, `people`, `timeline`).
- [ ] Later: Zalo OCR, Screenpipe, a DIY pendant (XIAO ESP32S3 Sense), Apple Watch heart rate.

Open question: does the server run on a lab machine or a VPS, and with a GPU or not; this decides ASR speed.
