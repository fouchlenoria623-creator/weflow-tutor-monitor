from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "state", "reports", "logs", "work", "demo-output", "__pycache__"}
TEXT_SUFFIXES = {".py", ".ps1", ".json", ".csv", ".html", ".md", ".yml", ".yaml", ".txt"}
PATTERNS = {
    "微信个人标识": re.compile(r"\bwxid_[A-Za-z0-9_-]{6,}\b", re.I),
    "真实群标识": re.compile(r"\b[A-Za-z0-9_-]{6,}@chatroom\b", re.I),
    "OpenIM 标识": re.compile(r"\b[A-Za-z0-9_-]{6,}@openim\b", re.I),
    "中国大陆手机号": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    "Windows 用户绝对路径": re.compile(r"[A-Za-z]:[\\/]Users[\\/][^\\/\s]+", re.I),
    "macOS 用户绝对路径": re.compile(r"/" r"Users/[^/\s]+/"),
    "Linux 用户绝对路径": re.compile(r"/" r"home/[^/\s]+/"),
    "个人邮箱": re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I),
    "高精度坐标": re.compile(r"(?<![\d.])-?\d{1,3}\.\d{6,}\s*,\s*-?\d{1,3}\.\d{6,}(?![\d.])"),
    "私钥": re.compile(r"-{5}BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-{5}"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    "OpenAI 风格 token": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    "JWT": re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    "地图或 WeFlow token 字面量": re.compile(
        r"(?:BAIDU_MAP_AK|AMAP_KEY|WEFLOW_API_TOKEN|httpApiToken)"
        r"\s*['\"]?\s*(?:,|:|=)\s*['\"]([A-Za-z0-9_+=/-]{16,})['\"]",
        re.I,
    ),
}

FIXTURE_PATTERNS = {
    "测试夹具中的非合成订单编号": re.compile(
        r"(?im)(?:^|[\s#【])((?!(?:DEMO|TEST|EXAMPLE|SYNTHETIC))[A-Z]{1,8}[A-Za-z0-9_-]*\d{4,})\b"
    ),
    "测试夹具中的纯数字订单编号": re.compile(r"(?m)^\s*#?\d{6,}\b"),
    "测试夹具中的真实 AUTO 编号": re.compile(r"\bAUTO-[0-9a-f]{8,}\b", re.I),
}

SYNTHETIC_PREFIXES = ("demo-", "test-", "example-", "synthetic-")
SAFE_EMAIL_DOMAINS = ("example.com", "example.org", "example.net", "users.noreply.github.com")


def git_names(*args):
    output = subprocess.check_output(
        ["git", "ls-files", "-z", *args], cwd=ROOT, stderr=subprocess.DEVNULL
    ).decode("utf-8")
    return [name for name in output.split("\0") if name]


def candidate_files():
    try:
        names = git_names("--cached", "--others", "--exclude-standard")
        if names:
            return [ROOT / name for name in sorted(set(names))]
    except (OSError, subprocess.CalledProcessError, UnicodeDecodeError):
        pass
    return [
        path for path in ROOT.rglob("*")
        if path.is_file() and not SKIP_DIRS.intersection(path.relative_to(ROOT).parts)
    ]


def staged_text(path):
    relative = path.relative_to(ROOT).as_posix()
    try:
        return subprocess.check_output(
            ["git", "show", f":{relative}"], cwd=ROOT, stderr=subprocess.DEVNULL
        ).decode("utf-8")
    except (OSError, subprocess.CalledProcessError, UnicodeDecodeError):
        return None


def documents(path):
    worktree = None
    try:
        worktree = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        pass
    staged = staged_text(path)
    if staged is not None:
        yield "staged", staged
    if worktree is not None and worktree != staged:
        yield "worktree", worktree


def is_fixture(path):
    relative = path.relative_to(ROOT)
    return path.name.startswith("test_") or "examples" in relative.parts


def allowed(label, value):
    lowered = value.lower()
    if label == "微信个人标识":
        return lowered.startswith(tuple(f"wxid_{prefix}" for prefix in SYNTHETIC_PREFIXES))
    if label in {"真实群标识", "OpenIM 标识"}:
        return lowered.startswith(SYNTHETIC_PREFIXES)
    if label == "个人邮箱":
        return lowered.rsplit("@", 1)[-1] in SAFE_EMAIL_DOMAINS
    return False


def scan_text(path, source, text):
    findings = []
    rules = dict(PATTERNS)
    if is_fixture(path):
        rules.update(FIXTURE_PATTERNS)
    seen = set()
    for label, pattern in rules.items():
        for match in pattern.finditer(text):
            value = match.group(1) if match.lastindex else match.group(0)
            if allowed(label, value):
                continue
            line = text.count("\n", 0, match.start()) + 1
            key = (label, line)
            if key in seen:
                continue
            seen.add(key)
            findings.append(f"{path.relative_to(ROOT)}:{line} [{source}]: {label}")
    return findings


def main():
    findings = []
    for path in candidate_files():
        if SKIP_DIRS.intersection(path.relative_to(ROOT).parts) or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        for source, text in documents(path):
            findings.extend(scan_text(path, source, text))
    if findings:
        print("隐私扫描未通过：", file=sys.stderr)
        print("\n".join(findings), file=sys.stderr)
        return 1
    print("隐私扫描通过：未发现常见个人标识、手机号、绝对用户路径、高精度坐标或密钥字面量。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
