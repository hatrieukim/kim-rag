"""Nhờ LLM viết tóm tắt: một bản cho mỗi episode code, một đoạn tổng quan cho mỗi ngày."""
from __future__ import annotations

import threading

from .asr.base import with_retry
from .config import get_settings
from .gcloud import genai_client, retryable

MAX_INPUT = 120_000  # phiên dài hơn thế thì giữ đầu và cuối, bỏ khúc giữa

EPISODE_PROMPT = """\
Bạn viết nhật ký làm việc. Dưới đây là một phiên làm việc giữa {owner} và trợ lý lập trình Claude Code
trong dự án "{project}". Viết bản tóm tắt bằng tiếng Việt, tối đa khoảng 250 từ, theo các mục sau.
Bỏ mục nào không có nội dung.

**Mục tiêu:** {owner} muốn làm gì.
**Đã làm:** những việc đã xong, nêu tên file, công nghệ, con số cụ thể.
**Quyết định:** những lựa chọn đã chốt và lý do.
**Còn dở:** việc chưa xong, lỗi chưa sửa, câu hỏi còn treo.

Chỉ dựa vào nội dung phiên, không suy đoán thêm. Không chép lại khóa, mật khẩu hay token.
Viết thẳng các mục, không mở đầu, không kết luận."""

DAY_PROMPT = """\
Bạn viết nhật ký ngày cho {owner}. Dưới đây là ba nguồn của ngày {day}. (1) Tóm tắt các phiên làm việc với
Claude Code, xếp theo dự án. (2) Số liệu hoạt động trên máy Mac do máy tự đo: thời gian ở từng app và dự án,
trang web, máy đã truy cập, commit, lệnh đáng chú ý, danh sách trang đã đọc trên Chrome, và lịch họp trong ngày.
(3) Tóm tắt các cuộc hội thoại đã ghi âm và các cuộc họp online.
Viết một đoạn tổng quan bằng tiếng Việt, 3 đến 6 câu: hôm nay {owner} làm những việc chính nào, ở dự án nào,
mất khoảng bao lâu, đã nói chuyện hay họp với ai về việc gì, kết quả đáng kể nhất là gì, còn dở việc gì.
Chỉ dựa vào nội dung được đưa. Con số thời gian lấy đúng từ số liệu Mac, không tự cộng hay ước lượng.
Không suy đoán nội dung các cuộc chat; chỉ được nói là đã chat bao lâu, với ai nếu số liệu có ghi.
Nguồn nào ghi "(không có)" thì bỏ qua nguồn đó.
Viết thẳng đoạn văn, không tiêu đề, không gạch đầu dòng."""

CONVERSATION_PROMPT = """\
Bạn viết nhật ký cho {owner}. Dưới đây là chữ do máy chuyển từ một bản ghi âm {what}, mỗi dòng là
"Người nói: câu nói". Chữ có thể sai chính tả, nhất là tên riêng. Nhãn như SPEAKER_01 là người chưa biết tên.
Viết bằng tiếng Việt, 2 đến 4 câu: ai nói chuyện với ai, về việc gì, và nếu có thì ai hứa gì, hẹn gì,
quyết định gì. Viết như người ngoài kể lại: gọi từng người bằng tên hoặc nhãn của họ, không dùng "tôi" hay
"chúng tôi" dù người nói xưng như vậy. Chỉ dựa vào nội dung bản ghi, không suy đoán. Nếu bản ghi rời rạc, không rõ nội dung thì nói
ngắn gọn là vậy. Viết thẳng đoạn văn, không tiêu đề."""

WEEK_PROMPT = """\
Bạn viết bản tóm tắt tuần cho {owner}, tuần {week}. Dưới đây là nhật ký từng ngày trong tuần (tổng quan ngày,
lịch, hoạt động trên Mac, tên các phiên làm việc theo dự án, và các cuộc hội thoại). Viết bằng tiếng Việt,
khoảng 150 đến 350 từ, theo các mục sau. Bỏ mục nào không có nội dung.

**Việc chính:** những mảng việc lớn trong tuần, ở dự án nào, ngày nào.
**Kết quả:** những gì đã xong và đáng kể nhất.
**Gặp gỡ và trao đổi:** đã nói chuyện, họp với ai về việc gì; ai hứa gì, hẹn gì.
**Còn dở:** việc chưa xong, câu hỏi còn treo, việc đã hẹn cho tuần sau.

Chỉ dựa vào nội dung được đưa, không suy đoán. Viết thẳng các mục, không mở đầu, không kết luận."""

# A recording shorter than this is quoted as it is instead of being summarized.
SHORT_CONVERSATION = 300

_client = None
_lock = threading.Lock()


class SummaryUnavailable(RuntimeError):
    """Chưa gọi được LLM vì thiếu cấu hình."""


def model_name() -> str:
    s = get_settings()
    return s.summary_model or s.graph_llm_model


def check() -> None:
    if not get_settings().google_cloud_project:
        raise SummaryUnavailable("thiếu GOOGLE_CLOUD_PROJECT trong .env")


def _generate(system: str, text: str, *, json: bool = False, quick: bool = False) -> str:
    """`quick` asks the model to think little: for simple picks an agent is waiting on."""
    from google.genai import types

    global _client
    check()
    with _lock:
        if _client is None:
            _client = genai_client(get_settings().graph_llm_location)

    def call() -> str:
        config = types.GenerateContentConfig(
            system_instruction=system, response_mime_type="application/json" if json else None,
            thinking_config=types.ThinkingConfig(thinking_level="low") if quick else None)
        resp = _client.models.generate_content(model=model_name(), contents=text, config=config)
        out = (resp.text or "").strip()
        if not out:
            raise RuntimeError("LLM trả về rỗng")
        return out

    return with_retry(call, retryable=retryable)


def summarize_episode(transcript: str, project: str) -> str:
    if len(transcript) > MAX_INPUT:
        half = MAX_INPUT // 2
        transcript = f"{transcript[:half]}\n\n[… lược bỏ khúc giữa …]\n\n{transcript[-half:]}"
    owner = get_settings().owner_name
    return _generate(EPISODE_PROMPT.format(owner=owner, project=project), transcript)


def summarize_day(day: str, sessions: str, mac: str = "", talks: str = "") -> str:
    text = (f"(1) Các phiên làm việc với Claude Code:\n{sessions.strip() or '(không có)'}\n\n"
            f"(2) Hoạt động trên Mac và trang đã đọc:\n{mac.strip() or '(không có)'}\n\n"
            f"(3) Hội thoại và cuộc họp:\n{talks.strip() or '(không có)'}")
    return _generate(DAY_PROMPT.format(owner=get_settings().owner_name, day=day), text)


def summarize_conversation(transcript: str, what: str = "hội thoại") -> str:
    """A few sentences about one recorded conversation. `what` says what kind of recording it is."""
    if len(transcript) > MAX_INPUT:
        half = MAX_INPUT // 2
        transcript = f"{transcript[:half]}\n\n[… lược bỏ khúc giữa …]\n\n{transcript[-half:]}"
    return _generate(CONVERSATION_PROMPT.format(owner=get_settings().owner_name, what=what), transcript)


def summarize_week(week: str, days: str) -> str:
    return _generate(WEEK_PROMPT.format(owner=get_settings().owner_name, week=week), days)


SPEAKERS_PROMPT = """\
You read a machine transcript of a recorded conversation (usually Vietnamese). Speakers without a known name
are labelled SPEAKER_01, SPEAKER_02, ... For each of these labels: {labels}, return
- "name": the person's name only if the conversation clearly shows it (someone calls them by name, they introduce
  themselves). Keep honorifics people use, e.g. "anh Tuấn". Otherwise null. Never guess.
- "role": a short description of who this voice is in this conversation, in the conversation's language, at most
  6 words, e.g. "nhân viên tổng đài Viettel", "người bán hàng", "MC sự kiện". Null if it is not clear.
If calendar context is given, the people invited to a meeting at that time may be among the voices: use their
spelling when the conversation shows which voice is which, but do not name a voice from the calendar alone.
Answer with one JSON object keyed by label, e.g. {{"SPEAKER_01": {{"name": null, "role": "người bán hàng"}}}}."""


def suggest_speakers(transcript: str, labels: list[str], context: str = "") -> dict[str, dict]:
    """Names and roles the LLM can read from a transcript, per unnamed speaker label."""
    import json

    if len(transcript) > MAX_INPUT:
        half = MAX_INPUT // 2
        transcript = f"{transcript[:half]}\n\n[...]\n\n{transcript[-half:]}"
    text = f"Calendar context: {context}\n\nTranscript:\n{transcript}" if context else transcript
    raw = _generate(SPEAKERS_PROMPT.format(labels=", ".join(labels)), text, json=True)
    data = json.loads(raw)
    return data if isinstance(data, dict) else {}


RELEVANT_PROMPT = """\
You pick, from a numbered list of facts and passages taken from someone's notes and conversations, the ones that
are about the topic "{topic}": they mention it, or a decision, change, problem or result that concerns it.
Leave out items that only share a general theme (another database, another project) without concerning the topic.
Answer with one JSON object: {{"keep": [numbers of the items to keep]}}."""


def pick_relevant(topic: str, items: list[str]) -> list[int]:
    """Indexes (from 0) of the items that are about `topic`, in the order given."""
    import json

    if not items:
        return []
    text = "\n".join(f"{i + 1}. {' '.join(item.split())[:600]}" for i, item in enumerate(items))
    data = json.loads(_generate(RELEVANT_PROMPT.format(topic=topic), text, json=True, quick=True))
    keep = data.get("keep") if isinstance(data, dict) else data
    return sorted({int(n) - 1 for n in keep or [] if str(n).isdigit() and 0 < int(n) <= len(items)})
