"""列出待发布文件中可能的隐私类别；只打印路径和类别，不输出匹配内容。"""
from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path


PATTERNS = {
    "联系方式": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"),
    "本机绝对路径": re.compile(r"[A-Za-z]:[\\/](?:Users|workbuddy|Documents|Desktop)[\\/]|/(?:home|Users)/[^\s]+", re.I),
    "疑似密钥": re.compile(r"(?i)(?:sk-[A-Za-z0-9_-]{16,}|Bearer\s+[A-Za-z0-9._-]{16,}|(?:api[_-]?key|password|cookie|token)\s*[:=]\s*['\"][^'\"]{12,}['\"])"),
}
PRIVATE_NAMES = re.compile(r"(?:contacted|messages|jobs|run_|dryscan_|投递清单|profile\.yaml|config\.yaml)", re.I)
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


def candidates(root: Path, staged: bool) -> list[Path]:
    if staged:
        raw = subprocess.check_output(["git", "-C", str(root), "diff", "--cached", "--name-only", "-z"])
        return [root / name.decode("utf-8") for name in raw.split(b"\0") if name]
    return [p for p in root.rglob("*") if p.is_file() and not any(
        part in {".git", ".venv", "venv", "__pycache__", ".pytest_cache", "build", "dist", ".mypy_cache"}
        or part.endswith(".egg-info") for part in p.relative_to(root).parts)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--staged", action="store_true", help="只扫描 Git 暂存文件")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    files = candidates(root, args.staged)
    findings = []
    for path in files:
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        categories = []
        if PRIVATE_NAMES.search(path.name) and not path.name.endswith(".example.yaml"):
            categories.append("可能的运行或个人文件")
        if path.suffix.lower() in IMAGE_SUFFIXES:
            categories.append("图像需人工复核")
        else:
            try:
                content = path.read_text(encoding="utf-8")
            except (UnicodeError, OSError):
                categories.append("非文本需人工复核")
            else:
                categories.extend(name for name, pattern in PATTERNS.items() if pattern.search(content))
        if categories:
            findings.append((rel.as_posix(), ", ".join(categories)))
    for name, categories in sorted(findings):
        print(f"{name}\t{categories}")
    print(f"扫描 {len(files)} 个文件，需复核 {len(findings)} 个文件。")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
