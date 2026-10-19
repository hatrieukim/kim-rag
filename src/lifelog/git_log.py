"""Commit của các dự án đã chọn, đọc thẳng từ git trên máy này, cho nhật ký ngày (Sản phẩm 4).

Đọc `git log` thay vì cài hook git, vì hook cài chung sẽ đè lên hook riêng của từng repo. Danh sách repo lấy từ
thư mục làm việc của các phiên Claude Code đã có, nên chỉ đọc đúng những dự án trong CLAUDE_CODE_PROJECTS.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from .config import get_settings
from .db import connect
from .events import store_events

log = logging.getLogger("lifelog")

SOURCE = "git"
_SEP = "\x1f"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60)
    return done.stdout if done.returncode == 0 else ""


def repos() -> dict[Path, str]:
    """Thư mục gốc của từng repo và tên dự án của nó. Worktree quy về repo chính."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT meta->>'project' AS project, meta->>'cwd' AS cwd FROM episodes "
            "WHERE kind = 'code' AND meta->>'cwd' IS NOT NULL"
        ).fetchall()
    out: dict[Path, str] = {}
    for r in rows:
        cwd = Path(r["cwd"])
        if not cwd.is_dir():
            continue
        common = _git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
        if not common:
            continue
        root = Path(common).parent if Path(common).name == ".git" else Path(common)
        out.setdefault(root, r["project"] or root.name)
    return out


def scan() -> int:
    """Ghi các commit mới vào sổ sự kiện. Trả về số commit mới."""
    since = get_settings().git_since_at
    if since is None:
        return 0
    events = []
    for root, project in repos().items():
        email = _git(root, "config", "user.email").strip()
        args = ["log", "--all", "--no-merges", f"--since={since.isoformat()}",
                f"--format=%H{_SEP}%aI{_SEP}%ae{_SEP}%s"]
        if email:
            args.append(f"--author={email}")
        for line in _git(root, *args).splitlines():
            parts = line.split(_SEP)
            if len(parts) != 4:
                continue
            sha, ts, author, subject = parts
            events.append({"uid": f"git:{sha}", "source": SOURCE, "type": "commit", "ts": ts,
                           "payload": {"project": project, "repo": root.name, "sha": sha, "subject": subject,
                                       "author": author}})
    if not events:
        return 0
    with connect() as conn:
        new = store_events(conn, events)
    if new:
        log.info("git: %d commit mới", new)
    return new
