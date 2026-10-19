-- Sổ của lifelog. Chạy lại nhiều lần không hỏng (idempotent).
CREATE EXTENSION IF NOT EXISTS vector;

-- Một cuộc hội thoại. Bước xử lý: open -> grouped -> diarized -> transcribed -> indexed -> ingested.
CREATE TABLE IF NOT EXISTS episodes (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    started_at      timestamptz NOT NULL,          -- tiếng nói đầu tiên
    ended_at        timestamptz NOT NULL,          -- tiếng nói cuối cùng
    stage           text NOT NULL DEFAULT 'open'
                    CHECK (stage IN ('open', 'grouped', 'diarized', 'transcribed', 'indexed', 'ingested')),
    close_requested boolean NOT NULL DEFAULT false,
    timeline        jsonb,                         -- [{chunk_id, offset_s, duration_s, started_at}]
    duration_s      double precision,
    speech_s        double precision,
    diarizer        text,
    asr_backend     text,
    export_path     text,
    attempts        int NOT NULL DEFAULT 0,
    error           text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS episodes_stage_idx ON episodes (stage, started_at);
CREATE INDEX IF NOT EXISTS episodes_started_idx ON episodes (started_at);

-- Một file audio đã nhận. Bước xử lý: received -> vad -> grouped, hoặc no_speech.
CREATE TABLE IF NOT EXISTS chunks (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    sha256          text NOT NULL UNIQUE,          -- chống gửi trùng theo nội dung file
    client_id       text UNIQUE,                   -- chống gửi trùng theo mã của thiết bị
    source          text NOT NULL DEFAULT 'upload',
    original_name   text,
    path            text NOT NULL,                 -- tương đối so với data_dir
    started_at      timestamptz NOT NULL,
    ended_at        timestamptz NOT NULL,
    duration_s      double precision NOT NULL,
    metadata        jsonb NOT NULL DEFAULT '{}',
    close_episode   boolean NOT NULL DEFAULT false,
    stage           text NOT NULL DEFAULT 'received'
                    CHECK (stage IN ('received', 'vad', 'grouped', 'no_speech')),
    speech          jsonb,                         -- [[start_s, end_s], ...] trong file
    speech_s        double precision,
    episode_id      uuid REFERENCES episodes (id) ON DELETE SET NULL,
    attempts        int NOT NULL DEFAULT 0,
    error           text,
    received_at     timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chunks_stage_idx ON chunks (stage, started_at);
CREATE INDEX IF NOT EXISTS chunks_episode_idx ON chunks (episode_id);

-- Một lượt nói trong episode. Thời gian tính bằng giây từ đầu audio của episode.
CREATE TABLE IF NOT EXISTS segments (
    id          bigserial PRIMARY KEY,
    episode_id  uuid NOT NULL REFERENCES episodes (id) ON DELETE CASCADE,
    idx         int NOT NULL,
    speaker     text NOT NULL,
    start_s     double precision NOT NULL,
    end_s       double precision NOT NULL,
    started_at  timestamptz NOT NULL,
    UNIQUE (episode_id, idx)
);

-- Chữ của một lượt nói, mỗi dịch vụ chuyển chữ một dòng để so sánh được.
CREATE TABLE IF NOT EXISTS segment_texts (
    segment_id  bigint NOT NULL REFERENCES segments (id) ON DELETE CASCADE,
    backend     text NOT NULL,
    model       text NOT NULL,
    text        text NOT NULL,
    confidence  real,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (segment_id, backend)
);

-- Người đã biết. Sản phẩm 6 mới dùng tới.
CREATE TABLE IF NOT EXISTS speakers (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name        text NOT NULL,
    is_me       boolean NOT NULL DEFAULT false,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Dấu giọng của từng người nói trong từng episode, lưu sẵn cho Sản phẩm 6.
CREATE TABLE IF NOT EXISTS episode_speakers (
    episode_id      uuid NOT NULL REFERENCES episodes (id) ON DELETE CASCADE,
    label           text NOT NULL,
    speech_s        double precision NOT NULL DEFAULT 0,
    embedding       vector,
    embedding_model text,
    speaker_id      uuid REFERENCES speakers (id) ON DELETE SET NULL,
    PRIMARY KEY (episode_id, label)
);

-- Sổ tạo từ Sản phẩm 1 chỉ cho phép tới bước transcribed. Nới ra một lần; đã nới rồi thì không đụng
-- tới bảng nữa, vì ALTER TABLE phải chờ thợ đang giữ khóa một episode.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'episodes'::regclass AND conname = 'episodes_stage_check'
          AND pg_get_constraintdef(oid) LIKE '%ingested%'
    ) THEN
        ALTER TABLE episodes DROP CONSTRAINT IF EXISTS episodes_stage_check;
        ALTER TABLE episodes ADD CONSTRAINT episodes_stage_check
            CHECK (stage IN ('open', 'grouped', 'diarized', 'transcribed', 'indexed', 'ingested'));
    END IF;
END $$;

-- Chỉ mục nguyên văn (Sản phẩm 2): vài lượt nói liền nhau thành một cửa sổ, mỗi cửa sổ một vector.
-- Trả lời câu "ai nói chính xác gì, lúc nào". Tính lại được từ segments.
CREATE TABLE IF NOT EXISTS windows (
    id              bigserial PRIMARY KEY,
    episode_id      uuid NOT NULL REFERENCES episodes (id) ON DELETE CASCADE,
    idx             int NOT NULL,
    first_idx       int NOT NULL,                  -- segments.idx của lượt nói đầu
    last_idx        int NOT NULL,                  -- và lượt nói cuối trong cửa sổ
    started_at      timestamptz NOT NULL,
    ended_at        timestamptz NOT NULL,
    speakers        text[] NOT NULL,
    text            text NOT NULL,
    embedding       vector(1536) NOT NULL,
    embedding_model text NOT NULL,                 -- đổi model thì embed lại từ segments
    UNIQUE (episode_id, idx)
);
CREATE INDEX IF NOT EXISTS windows_embedding_idx ON windows USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS windows_started_idx ON windows (started_at);

-- Mỗi dòng là một phần của episode đã nạp vào Graphiti. `uuid` là mã episode bên Graphiti, dùng để
-- lần từ một fact về đúng các lượt nói trong Postgres. Dòng có episode_id NULL là phần đã lỗi thời,
-- thợ sẽ gỡ nó khỏi Graphiti rồi xóa dòng.
CREATE TABLE IF NOT EXISTS graph_parts (
    uuid            uuid PRIMARY KEY,
    episode_id      uuid REFERENCES episodes (id) ON DELETE SET NULL,
    part            int NOT NULL,
    first_idx       int NOT NULL,
    last_idx        int NOT NULL,
    started_at      timestamptz NOT NULL,
    model           text NOT NULL,
    nodes           int NOT NULL DEFAULT 0,        -- số thực thể Graphiti rút ra từ phần này
    edges           int NOT NULL DEFAULT 0,        -- số fact
    llm_calls       int NOT NULL DEFAULT 0,
    input_tokens    bigint NOT NULL DEFAULT 0,
    output_tokens   bigint NOT NULL DEFAULT 0,     -- gồm cả token suy nghĩ, vì Google tính tiền cả hai
    seconds         real NOT NULL DEFAULT 0,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS graph_parts_episode_idx ON graph_parts (episode_id, part);

-- Sản phẩm 3. Episode giờ có hai loại: 'voice' dựng từ audio, 'code' dựng từ phiên Claude Code.
-- Thêm cột theo kiểu có kiểm tra trước, cùng lý do với đoạn nới ràng buộc ở trên.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'episodes' AND column_name = 'kind') THEN
        ALTER TABLE episodes ADD COLUMN kind text NOT NULL DEFAULT 'voice' CHECK (kind IN ('voice', 'code'));
        ALTER TABLE episodes ADD COLUMN meta jsonb NOT NULL DEFAULT '{}';   -- code: project, session, branch, files
        ALTER TABLE episodes ADD COLUMN summary text;                        -- bản tóm tắt do LLM viết
        ALTER TABLE episodes ADD COLUMN summary_model text;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'windows' AND column_name = 'kind') THEN
        -- 'turns' là vài lượt nói liền nhau, 'summary' là bản tóm tắt của cả episode.
        ALTER TABLE windows ADD COLUMN kind text NOT NULL DEFAULT 'turns' CHECK (kind IN ('turns', 'summary'));
    END IF;
END $$;

-- Sự kiện từ mọi nguồn không phải audio, theo đúng hợp đồng {ts, source, type, payload}.
-- Bước xử lý: received -> grouped (đã vào một episode), hoặc ignored (cụm không có nội dung đáng giữ).
CREATE TABLE IF NOT EXISTS events (
    id          bigserial PRIMARY KEY,
    uid         text NOT NULL UNIQUE,              -- mã do nguồn đặt, chống gửi trùng
    source      text NOT NULL,                     -- 'claude-code', 'mac', 'git'
    type        text NOT NULL,                     -- claude-code: prompt | reply | tool; mac: focus | away | back | tick | clipboard | shell…; git: commit
    ts          timestamptz NOT NULL,
    payload     jsonb NOT NULL DEFAULT '{}',
    stage       text NOT NULL DEFAULT 'received' CHECK (stage IN ('received', 'grouped', 'ignored')),
    episode_id  uuid REFERENCES episodes (id) ON DELETE SET NULL,
    received_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS events_stage_idx ON events (source, stage, ts);
CREATE INDEX IF NOT EXISTS events_episode_idx ON events (episode_id);

-- Người thợ đã đọc mỗi file nguồn tới đâu, để lần sau chỉ đọc phần mới.
CREATE TABLE IF NOT EXISTS event_files (
    path        text PRIMARY KEY,
    source      text NOT NULL,
    read_bytes  bigint NOT NULL DEFAULT 0,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Nhật ký ngày, mỗi ngày một file markdown.
CREATE TABLE IF NOT EXISTS journals (
    day          date PRIMARY KEY,
    path         text NOT NULL,                    -- tương đối so với data_dir
    episodes     int NOT NULL,
    model        text,
    generated_at timestamptz NOT NULL DEFAULT now()
);

-- Tên phiên Claude Code. Người dùng đặt tên (custom) thì ưu tiên hơn tên Claude Code tự đặt (ai).
-- Tên có thể đổi bất cứ lúc nào nên giữ riêng, rồi chép vào episodes.meta->>'title'.
CREATE TABLE IF NOT EXISTS code_sessions (
    session      text PRIMARY KEY,
    project      text,
    custom_title text,
    ai_title     text,
    updated_at   timestamptz NOT NULL DEFAULT now()
);

-- Hoạt động trên Mac (Sản phẩm 4): các khoảng liền mạch ở một app, tính ra từ sự kiện thô source 'mac'.
-- Chỉ là kết quả tính lại được: đổi luật thì xóa theo ngày và tính lại.
CREATE TABLE IF NOT EXISTS mac_spans (
    id          bigserial PRIMARY KEY,
    day         date NOT NULL,                     -- ngày theo TIMEZONE
    started_at  timestamptz NOT NULL,
    ended_at    timestamptz NOT NULL,
    seconds     int NOT NULL,                      -- thời gian thật ở app này, không tính lần liếc sang app khác
    app         text NOT NULL,
    bundle      text,
    kind        text,                              -- project | site | chat | host | db
    context     text,                              -- tên dự án, trang web, người chat, máy, kết nối
    titles      jsonb NOT NULL DEFAULT '[]'        -- vài tiêu đề cửa sổ tiêu biểu
);
CREATE INDEX IF NOT EXISTS mac_spans_day_idx ON mac_spans (day, started_at);

-- Nhật ký viết lại khi dấu này đổi. Để trống ở nhật ký cũ, coi như bằng số phiên.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'journals' AND column_name = 'signature') THEN
        ALTER TABLE journals ADD COLUMN signature text;
    END IF;
END $$;

-- Bài đọc trên Chrome (Sản phẩm 7) là episode loại 'read'.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'episodes_kind_check'
               AND pg_get_constraintdef(oid) NOT LIKE '%read%') THEN
        ALTER TABLE episodes DROP CONSTRAINT episodes_kind_check;
        ALTER TABLE episodes ADD CONSTRAINT episodes_kind_check CHECK (kind IN ('voice', 'code', 'read'));
    END IF;
END $$;

-- One Zalo chat on one day (product 10) is an episode of kind 'chat'.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'episodes_kind_check'
               AND pg_get_constraintdef(oid) NOT LIKE '%chat%') THEN
        ALTER TABLE episodes DROP CONSTRAINT episodes_kind_check;
        ALTER TABLE episodes ADD CONSTRAINT episodes_kind_check CHECK (kind IN ('voice', 'code', 'read', 'chat'));
    END IF;
END $$;

-- Speaker identity across recordings (product 6). A speaker is 'named' (confirmed by the user),
-- 'unknown' (a voice heard before but not named yet) or 'ignored' (the user said the name does not matter).
-- Unknown and ignored speakers have no name and are deleted after STRANGER_KEEP_DAYS without being heard.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'speakers' AND column_name = 'status') THEN
        ALTER TABLE speakers ALTER COLUMN name DROP NOT NULL;
        ALTER TABLE speakers ADD COLUMN status text NOT NULL DEFAULT 'named'
            CHECK (status IN ('named', 'unknown', 'ignored'));
        ALTER TABLE speakers ADD COLUMN sample_path text;          -- short audio sample, kept forever for named speakers
        ALTER TABLE speakers ADD COLUMN last_seen_at timestamptz;
        ALTER TABLE speakers ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'episode_speakers' AND column_name = 'assigned_by') THEN
        -- 'auto': matched by voice; 'user': set or detached by the user, never changed by matching.
        ALTER TABLE episode_speakers ADD COLUMN assigned_by text CHECK (assigned_by IN ('auto', 'user'));
        ALTER TABLE episode_speakers ADD COLUMN match_score real;
        ALTER TABLE episode_speakers ADD COLUMN role text;              -- LLM label for an unnamed voice, e.g. "nhân viên tổng đài"
        ALTER TABLE episode_speakers ADD COLUMN suggested_name text;    -- name the LLM heard in the conversation
        ALTER TABLE episode_speakers ADD COLUMN llm_checked boolean NOT NULL DEFAULT false;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'speakers' AND column_name = 'ignored_at') THEN
        -- When the user last said "Not important". Voices heard after it count again for asking.
        ALTER TABLE speakers ADD COLUMN ignored_at timestamptz;
        UPDATE speakers SET ignored_at = updated_at WHERE status = 'ignored';
    END IF;
END $$;
CREATE UNIQUE INDEX IF NOT EXISTS speakers_name_idx ON speakers (lower(name)) WHERE status = 'named';

-- Voice embeddings of each speaker. Several per person, because a voice sounds different over the phone,
-- with a cold or through another microphone. Matching only compares embeddings of the same model.
CREATE TABLE IF NOT EXISTS voiceprints (
    id              bigserial PRIMARY KEY,
    speaker_id      uuid NOT NULL REFERENCES speakers (id) ON DELETE CASCADE,
    embedding       vector NOT NULL,
    embedding_model text NOT NULL,
    origin          text NOT NULL CHECK (origin IN ('enroll', 'confirm', 'auto')),
    episode_id      uuid REFERENCES episodes (id) ON DELETE SET NULL,
    label           text,
    speech_s        double precision,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS voiceprints_speaker_idx ON voiceprints (speaker_id);

-- Voices the user said are not this speaker ("Not Hà"). A voice closer to one of these than to the
-- speaker's own voiceprints is never matched to the speaker. The embedding is copied, so it outlives the episode.
CREATE TABLE IF NOT EXISTS voice_rejections (
    id              bigserial PRIMARY KEY,
    speaker_id      uuid NOT NULL REFERENCES speakers (id) ON DELETE CASCADE,
    embedding       vector NOT NULL,
    embedding_model text NOT NULL,
    episode_id      uuid REFERENCES episodes (id) ON DELETE SET NULL,
    label           text,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS voice_rejections_speaker_idx ON voice_rejections (speaker_id);

-- Calendar events read by the Mac app (source 'calendar'). Latest version of each event, one row per
-- occurrence. Used to suggest who is speaking in a recording and for the day journal.
CREATE TABLE IF NOT EXISTS calendar_events (
    key         text PRIMARY KEY,                  -- calendar item id + '@' + occurrence start
    title       text,                              -- NULL when the title matched a private pattern
    starts_at   timestamptz NOT NULL,
    ends_at     timestamptz NOT NULL,
    all_day     boolean NOT NULL DEFAULT false,
    location    text,
    calendar    text,
    attendees   jsonb NOT NULL DEFAULT '[]',        -- [{name, email, status, me}]
    organizer   jsonb,
    cancelled   boolean NOT NULL DEFAULT false,     -- gone from the calendar or cancelled
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS calendar_events_time_idx ON calendar_events (starts_at, ends_at);

-- Week summaries (product 9), written from the day journals of a finished week (Monday to Sunday).
-- `signature` lists the day journals it was written from; a day journal written again makes it stale.
CREATE TABLE IF NOT EXISTS week_journals (
    week_start   date PRIMARY KEY,                 -- the Monday
    path         text NOT NULL,                    -- relative to data_dir
    signature    text NOT NULL,
    model        text,
    generated_at timestamptz NOT NULL DEFAULT now()
);

-- Product 10, round 3: Kim says which Zalo person is which named voice, so that `people` and the graph see
-- one person. Never guessed: names differ too much between the two ("Hà" and "Thu Hà").
CREATE TABLE IF NOT EXISTS zalo_links (
    zalo_id     text PRIMARY KEY,                  -- Zalo user id, also the thread id of the direct chat
    speaker_id  uuid NOT NULL REFERENCES speakers (id) ON DELETE CASCADE,
    created_at  timestamptz NOT NULL DEFAULT now()
);
