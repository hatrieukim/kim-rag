"""Gọi model của Google qua project Google Cloud, để tiền tính vào credit Cloud."""
from __future__ import annotations

import os

from .config import get_settings


def genai_client(location: str):
    """Client google-genai trỏ vào project trong .env."""
    from google import genai

    project = get_settings().google_cloud_project
    # Tính hạn mức và tiền vào đúng project này, không theo project mặc định của gcloud.
    os.environ.setdefault("GOOGLE_CLOUD_QUOTA_PROJECT", project)
    options = http_options()
    try:
        return genai.Client(enterprise=True, project=project, location=location, http_options=options)
    except (TypeError, ValueError):
        # Bản SDK cũ hơn dùng tên tham số `vertexai`.
        return genai.Client(vertexai=True, project=project, location=location, http_options=options)


def http_options():
    """Give every request a deadline: the SDK waits forever by default, and a stalled call froze the worker."""
    from google.genai import types

    return types.HttpOptions(timeout=int(get_settings().google_timeout_s * 1000))  # milliseconds


def retryable(exc: Exception) -> bool:
    """Lỗi quá tải và lỗi phía Google thì thử lại; lỗi do mình gửi sai thì không."""
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code in (408, 429) or code >= 500
    return True
