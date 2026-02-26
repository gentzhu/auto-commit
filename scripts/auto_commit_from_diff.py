#!/usr/bin/env python3
"""Auto commit from git diff with optional DeepSeek AI generation."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ALLOWED_TYPES = (
    "feat",
    "fix",
    "refactor",
    "docs",
    "style",
    "test",
    "chore",
    "perf",
    "ci",
    "build",
    "revert",
)

DOC_EXTENSIONS = {".md", ".rst", ".adoc", ".txt"}
STYLE_EXTENSIONS = {".css", ".scss", ".sass", ".less", ".styl"}
DOC_BASENAMES = {"readme.md", "changelog.md", "license", "license.md"}
STYLE_CONFIG_BASENAMES = {
    ".editorconfig",
    ".prettierrc",
    ".stylelintrc",
    ".eslintrc",
    ".eslintrc.js",
    ".eslintrc.cjs",
    ".eslintrc.json",
}
BUILD_BASENAMES = {
    "makefile",
    "dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "package.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "package-lock.json",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "gradle.properties",
    "pyproject.toml",
    "requirements.txt",
}
CI_PATH_HINTS = {
    ".github/workflows/",
    ".gitlab-ci.yml",
    ".circleci/",
    "azure-pipelines.yml",
    "azure-pipelines.yaml",
    "jenkinsfile",
}
ROOT_SCOPE_BASENAMES = {
    ".gitignore",
    ".gitattributes",
    ".npmrc",
    ".nvmrc",
    "readme.md",
    "license",
    "license.md",
}
FIX_KEYWORDS = (
    "fix",
    "bug",
    "hotfix",
    "crash",
    "error",
    "exception",
    "null",
    "patch",
    "修复",
    "错误",
    "异常",
)
REFACTOR_KEYWORDS = (
    "refactor",
    "cleanup",
    "restructure",
    "rename",
    "重构",
    "整理",
)
PERF_KEYWORDS = ("perf", "performance", "optimiz", "benchmark", "性能", "优化")


@dataclass(frozen=True)
class Change:
    status: str
    path: str
    old_path: str | None = None


def run_git(repo: Path, args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if check and proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        details = stderr if stderr else stdout
        raise RuntimeError(f"git {' '.join(args)} failed: {details}")
    return proc


def normalize_path(path: str) -> str:
    return path.replace("\\", "/").strip()


def split_path(path: str) -> list[str]:
    return [part for part in normalize_path(path).split("/") if part]


def extension(path: str) -> str:
    return Path(path).suffix.lower()


def basename(path: str) -> str:
    return Path(path).name.lower()


def is_docs(path: str) -> bool:
    p = normalize_path(path).lower()
    b = basename(p)
    parts = split_path(p)
    return (
        extension(p) in DOC_EXTENSIONS
        or b in DOC_BASENAMES
        or "docs" in parts
        or "doc" in parts
        or "documentation" in parts
    )


def is_test(path: str) -> bool:
    p = normalize_path(path).lower()
    b = basename(p)
    parts = split_path(p)
    if any(part in {"test", "tests", "testing", "__tests__", "spec", "specs", "e2e"} for part in parts):
        return True
    return (
        b.startswith("test_")
        or b.endswith("_test.py")
        or b.endswith(".spec.ts")
        or b.endswith(".spec.tsx")
        or b.endswith(".spec.js")
        or b.endswith(".spec.jsx")
    )


def is_ci(path: str) -> bool:
    p = normalize_path(path).lower()
    if any(p.startswith(hint) for hint in CI_PATH_HINTS):
        return True
    return basename(p) in {"jenkinsfile", ".gitlab-ci.yml"}


def is_build(path: str) -> bool:
    p = normalize_path(path).lower()
    b = basename(p)
    parts = split_path(p)
    if b in BUILD_BASENAMES:
        return True
    return (
        "build" in parts
        or b.startswith("vite.config")
        or b.startswith("webpack.config")
        or b.startswith("rollup.config")
    )


def is_style(path: str) -> bool:
    p = normalize_path(path).lower()
    return extension(p) in STYLE_EXTENSIONS or basename(p) in STYLE_CONFIG_BASENAMES


def is_perf(path: str) -> bool:
    p = normalize_path(path).lower()
    return "perf" in p or "benchmark" in p


def has_keyword(text: str, keywords: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(keyword in lowered for keyword in keywords)


def extract_content_signal(diff_text: str) -> str:
    content_lines: list[str] = []
    for line in diff_text.splitlines():
        if (
            line.startswith("diff --git ")
            or line.startswith("index ")
            or line.startswith("@@ ")
            or line.startswith("--- ")
            or line.startswith("+++ ")
        ):
            continue
        if line.startswith("+") or line.startswith("-"):
            content_lines.append(line[1:])
    return "\n".join(content_lines).lower()


def parse_name_status(raw: str) -> list[Change]:
    changes: list[Change] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status_token = parts[0].strip()
        status = status_token[0]
        if status in {"R", "C"} and len(parts) >= 3:
            old_path = normalize_path(parts[1])
            new_path = normalize_path(parts[2])
            changes.append(Change(status=status, old_path=old_path, path=new_path))
        elif len(parts) >= 2:
            target_path = normalize_path(parts[1])
            changes.append(Change(status=status, path=target_path))
    return changes


def count_changes(changes: list[Change]) -> dict[str, int]:
    counts = {"A": 0, "M": 0, "D": 0, "R": 0}
    for change in changes:
        status = change.status
        if status in counts:
            counts[status] += 1
        elif status == "C":
            counts["A"] += 1
        else:
            counts["M"] += 1
    return counts


def infer_type(paths: list[str], diff_text: str, counts: dict[str, int]) -> str:
    if not paths:
        return "chore"

    if "this reverts commit" in diff_text.lower():
        return "revert"

    if all(is_docs(path) for path in paths):
        return "docs"
    if all(is_test(path) for path in paths):
        return "test"
    if all(is_ci(path) for path in paths):
        return "ci"
    if all(is_build(path) for path in paths):
        return "build"
    if all(is_style(path) for path in paths):
        return "style"

    joined_paths = " ".join(paths).lower()
    content_signal = extract_content_signal(diff_text)
    signal_text = f"{joined_paths}\n{content_signal}"

    if any(is_perf(path) for path in paths) or has_keyword(signal_text, PERF_KEYWORDS):
        return "perf"
    if has_keyword(signal_text, FIX_KEYWORDS):
        return "fix"
    if has_keyword(signal_text, REFACTOR_KEYWORDS):
        return "refactor"

    if counts["A"] > 0:
        return "feat"
    return "chore"


def sanitize_scope(scope: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", scope).strip("-.").lower()
    return cleaned or "repo"


def infer_scope(paths: list[str], commit_type: str) -> str:
    if not paths:
        return "repo"

    if commit_type == "ci":
        return "ci"
    if commit_type == "docs":
        return "docs"
    if commit_type == "build":
        return "build"

    root_like = 0
    tops: list[str] = []
    for path in paths:
        normalized = normalize_path(path)
        parts = split_path(normalized)
        if len(parts) <= 1 or basename(normalized) in ROOT_SCOPE_BASENAMES:
            root_like += 1
            continue
        top = parts[0]
        if top.startswith(".") and len(parts) > 1:
            top = top.lstrip(".") or parts[0]
        tops.append(top)

    unique = sorted(set(tops))
    if len(unique) == 1:
        return sanitize_scope(unique[0])
    if len(unique) > 1:
        return "multi"
    if root_like > 0:
        return "repo"
    return "repo"


def infer_theme(scope: str, counts: dict[str, int], commit_type: str) -> str:
    if commit_type == "revert":
        return "回滚上一轮改动"
    if counts["A"] > 0 and counts["M"] == 0 and counts["D"] == 0 and counts["R"] == 0:
        return f"新增{scope}相关内容"
    if counts["D"] > 0 and counts["A"] == 0 and counts["M"] == 0:
        return f"移除{scope}冗余内容"
    if counts["R"] > 0 and counts["A"] == 0:
        return f"整理{scope}文件结构"
    if counts["M"] > 0 and counts["A"] == 0:
        return f"完善{scope}相关实现"
    return f"同步{scope}相关改动"


def infer_intro(scope: str, counts: dict[str, int], paths: list[str], max_files: int) -> str:
    total = len(paths)
    preview = "，".join(paths[:max_files])
    suffix = ""
    if total > max_files:
        suffix = f" 等{total}个文件"
    return (
        f"对{total}个文件进行了变更（新增{counts['A']}、修改{counts['M']}、删除{counts['D']}、重命名{counts['R']}），"
        f"主要集中在{scope}范围，涉及{preview}{suffix}。"
    )


def build_body(commit_type: str, scope: str, theme: str, intro: str) -> str:
    return "\n".join(
        [
            f"类型: {commit_type}",
            f"作用域: {scope}",
            f"主题: {theme}",
            f"简介: {intro}",
        ]
    )


def ensure_inside_git_repo(repo: Path) -> None:
    run_git(repo, ["rev-parse", "--is-inside-work-tree"])


def current_status_has_changes(repo: Path) -> bool:
    status = run_git(repo, ["status", "--porcelain"], check=False)
    return bool((status.stdout or "").strip())


def load_staged_changes(repo: Path) -> list[Change]:
    proc = run_git(repo, ["diff", "--cached", "--name-status", "-M"], check=False)
    return parse_name_status(proc.stdout or "")


def build_ai_prompt(repo: Path, paths: list[str], counts: dict[str, int], diff_text: str) -> tuple[str, str]:
    system = (
        "你是资深代码审阅助手。根据 git diff 生成 commit 四要素。"
        "必须返回严格 JSON，不要 markdown，不要解释。"
        "字段: type, scope, theme, intro。"
        "type 必须是 feat/fix/refactor/docs/style/test/chore/perf/ci/build/revert 之一。"
        "theme 和 intro 必须是简洁中文。"
    )
    preview_paths = paths[:30]
    diff_slice = diff_text[:12000]
    user = {
        "repo": str(repo),
        "changed_files": preview_paths,
        "change_counts": counts,
        "diff": diff_slice,
        "output_schema": {
            "type": "feat|fix|refactor|docs|style|test|chore|perf|ci|build|revert",
            "scope": "string",
            "theme": "string",
            "intro": "string",
        },
    }
    return system, json.dumps(user, ensure_ascii=False)


def request_deepseek(system_prompt: str, user_prompt: str, timeout_sec: float) -> str:
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is not set")

    url = "https://api.deepseek.com/chat/completions"
    model = "deepseek-chat"

    payload = {
        "model": model,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"DeepSeek HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"DeepSeek request failed: {exc}") from exc

    data = json.loads(body)
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("DeepSeek returned empty choices")
    message = choices[0].get("message") or {}
    content = message.get("content", "")
    if not content:
        raise RuntimeError("DeepSeek returned empty content")
    return content


def normalize_ai_result(raw_json: str) -> tuple[str, str, str, str]:
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"AI returned non-JSON content: {exc}") from exc

    commit_type = str(data.get("type", "")).strip().lower()
    scope = sanitize_scope(str(data.get("scope", "")).strip() or "repo")
    theme = str(data.get("theme", "")).strip()
    intro = str(data.get("intro", "")).strip()

    if commit_type not in ALLOWED_TYPES:
        raise RuntimeError(f"AI returned invalid type: {commit_type}")
    if not theme:
        raise RuntimeError("AI returned empty theme")
    if not intro:
        raise RuntimeError("AI returned empty intro")

    return commit_type, scope, theme, intro


def run() -> int:
    parser = argparse.ArgumentParser(
        description="自动分析 git diff，生成中文提交描述并执行 commit。",
    )
    parser.add_argument("--repo", default=".", help="目标仓库路径，默认为当前目录")
    parser.add_argument("--no-stage", action="store_true", help="不自动执行 git add -A")
    parser.add_argument("--dry-run", action="store_true", help="只生成描述，不执行 commit")
    parser.add_argument("--type", dest="type_override", choices=ALLOWED_TYPES, help="手动指定提交类型")
    parser.add_argument("--scope", dest="scope_override", help="手动指定作用域")
    parser.add_argument("--theme", dest="theme_override", help="手动指定主题")
    parser.add_argument("--intro", dest="intro_override", help="手动指定简介")
    parser.add_argument("--max-files", type=int, default=5, help="简介中展示的最多文件数")
    parser.add_argument("--no-verify", action="store_true", help="提交时添加 --no-verify")
    parser.add_argument("--no-ai", action="store_true", help="禁用 DeepSeek，使用本地规则")
    parser.add_argument("--ai-required", action="store_true", help="强制要求 DeepSeek 成功，否则退出")
    parser.add_argument("--ai-timeout", type=float, default=30.0, help="DeepSeek 请求超时秒数")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    if args.max_files < 1:
        print("错误: --max-files 需要 >= 1", file=sys.stderr)
        return 2

    try:
        ensure_inside_git_repo(repo)
    except RuntimeError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2

    if not current_status_has_changes(repo):
        print("没有可提交的变更。")
        return 1

    try:
        if not args.no_stage:
            run_git(repo, ["add", "-A"])

        staged_changes = load_staged_changes(repo)
        if not staged_changes:
            print("没有已暂存变更，无法生成提交信息。")
            return 1

        paths = [change.path for change in staged_changes]
        diff_text = run_git(repo, ["diff", "--cached", "--unified=1", "--no-color"], check=False).stdout or ""
        counts = count_changes(staged_changes)

        local_type = args.type_override or infer_type(paths, diff_text, counts)
        local_scope = sanitize_scope(args.scope_override) if args.scope_override else infer_scope(paths, local_type)
        local_theme = args.theme_override or infer_theme(local_scope, counts, local_type)
        local_intro = args.intro_override or infer_intro(local_scope, counts, paths, args.max_files)

        commit_type, scope, theme, intro = local_type, local_scope, local_theme, local_intro
        ai_used = False

        can_try_ai = not args.no_ai and not any([args.type_override, args.scope_override, args.theme_override, args.intro_override])
        if can_try_ai:
            try:
                system_prompt, user_prompt = build_ai_prompt(repo, paths, counts, diff_text)
                ai_raw = request_deepseek(system_prompt, user_prompt, args.ai_timeout)
                commit_type, scope, theme, intro = normalize_ai_result(ai_raw)
                ai_used = True
            except RuntimeError as exc:
                if args.ai_required:
                    print(f"错误: DeepSeek 调用失败且启用了 --ai-required: {exc}", file=sys.stderr)
                    return 2
                print(f"提示: DeepSeek 不可用，已回退本地规则。原因: {exc}", file=sys.stderr)

        if commit_type not in ALLOWED_TYPES:
            commit_type = "chore"

        commit_header = f"{commit_type}({scope}): {theme}"
        commit_body = build_body(commit_type, scope, theme, intro)

        print("已生成提交描述:")
        print(f"类型: {commit_type}")
        print(f"作用域: {scope}")
        print(f"主题: {theme}")
        print(f"简介: {intro}")
        print(f"来源: {'DeepSeek AI' if ai_used else 'Local Rules'}")
        print(f"commit: {commit_header}")

        if args.dry_run:
            print("dry-run: 未执行 git commit。")
            return 0

        commit_args = ["commit"]
        if args.no_verify:
            commit_args.append("--no-verify")
        commit_args.extend(["-m", commit_header, "-m", commit_body])
        run_git(repo, commit_args)
        short_sha = run_git(repo, ["rev-parse", "--short", "HEAD"]).stdout.strip()
        print(f"提交完成: {short_sha}")
        return 0
    except RuntimeError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(run())
