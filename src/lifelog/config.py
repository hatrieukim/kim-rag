"""Cấu hình đọc từ biến môi trường và file .env ở thư mục đang chạy lệnh."""
from __future__ import annotations

from datetime import date, datetime, time
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://lifelog:lifelog@127.0.0.1:54329/lifelog"
    data_dir: Path = Path("data")
    timezone: str = "Asia/Ho_Chi_Minh"
    # Để trống thì API không đòi token. Đặt giá trị khi mở server ra ngoài máy này.
    api_token: str = ""
    max_upload_mb: int = 1024

    # Số lần thử lại tối đa cho một chunk hoặc episode trước khi dừng và chờ `lifelog retry`.
    max_attempts: int = 3

    # `lifelog worker --once` stops taking new work after this many seconds, so launchd's next run can start.
    worker_run_s: float = 1800.0
    # A run still alive this many seconds after it started is stuck (a call that never returns): it is killed.
    worker_kill_s: float = 3600.0
    # Longest wait for one request to Google (Gemini, embeddings, Vertex AI) before it fails and is retried.
    google_timeout_s: float = 300.0

    # Ghép episode: hai đoạn có tiếng nói cách nhau dưới ngần này giây là cùng một cuộc hội thoại.
    episode_gap_s: float = 600.0
    # An iPhone chunk with less speech than this (stray sounds, a TV) neither opens nor extends an episode.
    weak_chunk_speech_s: float = 5.0

    # Tách người nói.
    diarizer: str = "pyannote"  # pyannote | none
    pyannote_model: str = "pyannote/speaker-diarization-community-1"
    hf_token: str = ""
    diarize_device: str = "auto"  # auto | cpu | mps | cuda

    # Speaker identity (product 6). Cosine similarity at or above this means the same person.
    speaker_match: float = 0.65
    # Voices without a name are forgotten after this many days without being heard again.
    stranger_keep_days: int = 90

    # Cắt lượt nói trước khi gửi cho API chuyển chữ.
    turn_merge_gap_s: float = 1.0
    turn_max_s: float = 50.0  # Chirp 3 gọi đồng bộ chỉ nhận dưới 60 giây.
    turn_min_s: float = 0.4
    turn_pad_s: float = 0.15

    # Chuyển giọng thành chữ.
    asr_backend: str = "gemini-3.5-transcribe"  # gemini-3.5-transcribe | gpt-transcribe | chirp_3
    asr_language: str = "vi"
    asr_keywords: str = ""  # tên riêng, thuật ngữ, cách nhau bằng dấu phẩy
    asr_concurrency: int = 4

    openai_api_key: str = ""
    # Google: gọi qua project Google Cloud để tính vào credit Cloud. Đăng nhập bằng
    # `gcloud auth application-default login` hoặc GOOGLE_APPLICATION_CREDENTIALS.
    google_cloud_project: str = ""
    # Mã model thật gửi cho Google. Project mới có thể chỉ được cấp bản `-preview`.
    gemini_model: str = "gemini-3.5-transcribe"
    gemini_location: str = "global"
    chirp_location: str = "us"
    # Đường phụ: khóa AI Studio. Chỉ dùng khi không đặt google_cloud_project.
    gemini_api_key: str = ""

    # Bộ nhớ (Sản phẩm 2). Cả hai kho gọi Google qua google_cloud_project ở trên.
    # Chỉ mục nguyên văn: vài lượt nói liền nhau thành một cửa sổ, mỗi cửa sổ một vector trong Postgres.
    embed_model: str = "gemini-embedding-2"
    embed_location: str = "global"
    embed_concurrency: int = 4
    window_max_chars: int = 1200
    window_overlap: int = 1  # số lượt nói lặp lại giữa hai cửa sổ liền nhau

    # Graphiti trên FalkorDB: rút người, việc và fact có thời gian hiệu lực.
    graph: str = "graphiti"  # graphiti | none
    graph_llm_model: str = "gemini-3.8-flash"
    graph_llm_small_model: str = ""  # để trống thì dùng luôn graph_llm_model
    graph_llm_location: str = "global"
    graph_name: str = "lifelog"
    # Chỉ nạp vào Graphiti những episode bắt đầu từ ngày này (YYYY-MM-DD, giờ TIMEZONE). Episode cũ hơn
    # dừng ở bước indexed: vẫn tìm được theo nguyên văn, chỉ không có fact. Để trống thì nạp hết.
    graph_since: str = ""
    # Zalo chats may start earlier than the rest (product 10): Kim loaded three months of chat history.
    # Empty means the same day as graph_since. They go in only when nothing newer is waiting.
    graph_chat_since: str = ""
    graph_part_chars: int = 6000  # episode dài được chia thành nhiều phần cỡ này trước khi nạp
    falkordb_host: str = "127.0.0.1"
    falkordb_port: int = 63790

    # Nhật ký làm việc (Sản phẩm 3): đọc lịch sử phiên Claude Code.
    claude_code_dir: Path = Path("~/.claude/projects")
    # Tên các dự án được phép đọc, cách nhau bằng dấu phẩy. Để trống thì không đọc gì.
    claude_code_projects: str = ""
    # Hai lượt trong cùng một phiên cách nhau quá ngần này giây thì tách thành hai episode.
    code_gap_s: float = 3600.0
    owner_name: str = "Kim"  # tên người dùng trong transcript của phiên Claude Code
    # The user's own email addresses, comma separated. Calendar attendees with these emails are the user, even
    # when the invite went to an address other than the calendar's account.
    my_emails: str = ""
    # Extra words in Zalo names that are tags, not names (e.g. a workplace), comma separated.
    zalo_name_tags: str = ""
    # Model viết tóm tắt episode và nhật ký ngày. Để trống thì dùng graph_llm_model.
    summary_model: str = ""

    # Hoạt động trên Mac (Sản phẩm 4). Commit chỉ đọc từ ngày này (YYYY-MM-DD); để trống thì không đọc.
    git_since: str = ""

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def inbox_dir(self) -> Path:
        return self.data_dir / "inbox"

    @property
    def episodes_dir(self) -> Path:
        return self.data_dir / "episodes"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def graph_since_at(self) -> datetime | None:
        if not self.graph_since.strip():
            return None
        return datetime.combine(date.fromisoformat(self.graph_since.strip()), time.min, self.tz)

    @property
    def graph_chat_since_at(self) -> datetime | None:
        if not self.graph_chat_since.strip():
            return self.graph_since_at
        return datetime.combine(date.fromisoformat(self.graph_chat_since.strip()), time.min, self.tz)

    @property
    def git_since_at(self) -> datetime | None:
        if not self.git_since.strip():
            return None
        return datetime.combine(date.fromisoformat(self.git_since.strip()), time.min, self.tz)

    @property
    def journal_dir(self) -> Path:
        return self.data_dir / "journal"

    @property
    def code_projects(self) -> list[str]:
        return [p.strip() for p in self.claude_code_projects.split(",") if p.strip()]

    @property
    def name_tags(self) -> set[str]:
        return {t.strip().lower() for t in self.zalo_name_tags.split(",") if t.strip()}

    @property
    def own_emails(self) -> set[str]:
        return {e.strip().lower() for e in self.my_emails.split(",") if e.strip()}

    @property
    def keywords(self) -> list[str]:
        return [k.strip() for k in self.asr_keywords.split(",") if k.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
