#!/usr/bin/env python3
# Author: Mrowr Purr
# Description: Per-repo CLI runner. Reads project.toml, dispatches commands
#              to built-in tasks or repo-local scripts under ./scripts/.
#
# Usage:
#   python project.py setup
#   python project.py lint
#   python project.py build
#   python project.py self-update
#
# Notes:
#   - Python 3.11+, standard library only.
#   - Single file. Drop into any repo, pair with project.toml.
from __future__ import annotations

import argparse
import base64
import importlib
import json
import os
import re
import subprocess
import sys
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

__version__ = "0.1.0"

SELF_UPDATE_REPO = "BuildWithCollab/project.py"
SELF_UPDATE_PATH = "project.py"

TEMPLATES_REF = "main"
TEMPLATES_DIR = "templates"
WRITE_ONCE_DIR = "_write_once_"
APPEND_DIR = "_append_"

ROOT: Path = Path(__file__).resolve().parent
TOML_PATH: Path = ROOT / "project.toml"
SYNC_LOCK_PATH: Path = ROOT / ".project-sync.lock"


# --- Platform ---

class Platform(Enum):
    WINDOWS = "windows"
    LINUX = "linux"
    MAC = "mac"


def platform() -> Platform:
    if sys.platform.startswith("win"):
        return Platform.WINDOWS
    if sys.platform == "darwin":
        return Platform.MAC
    return Platform.LINUX


# --- Config ---

@dataclass
class Config:
    name: str = ""
    commands: dict[str, list[str]] = field(default_factory=dict)
    tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    args: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path = TOML_PATH) -> "Config":
        if not path.exists():
            return cls()
        with path.open("rb") as f:
            data = tomllib.load(f)
        return cls(
            name=data.get("project", {}).get("name", ""),
            commands=data.get("commands", {}),
            tools={k: v for k, v in data.items() if k not in {"project", "commands"}},
        )


# --- subprocess helpers ---

def run(cmd: list[str] | str, *, check: bool = True, **kw: Any) -> subprocess.CompletedProcess:
    pretty = cmd if isinstance(cmd, str) else " ".join(cmd)
    print(f"$ {pretty}", flush=True)
    return subprocess.run(cmd, check=check, **kw)


def xmake(*args: str, **kw: Any) -> subprocess.CompletedProcess:
    return run(["xmake", *args], **kw)


# --- clang-tidy (two-pass: parallel discovery, serial fix) ---

_DIAG = re.compile(r"^(?P<path>[^:]+):\d+:\d+:\s+(warning|error):\s")
CLANG_TIDY_EXTS = {".cppm", ".cpp", ".cc", ".cxx"}


def clang_tidy_check_and_fix(
    *,
    binary: str = "clang-tidy",
    compile_commands: Path = Path("compile_commands.json"),
    root: Path | None = None,
    extensions: set[str] = CLANG_TIDY_EXTS,
    jobs: int | None = None,
    fix: bool = True,
    report_path: Path | None = Path("clang-tidy-files.txt"),
) -> list[Path]:
    root = (root or Path.cwd()).resolve()
    jobs = jobs or os.cpu_count() or 1

    db = json.loads(compile_commands.read_text(encoding="utf-8"))
    files: list[Path] = []
    seen: set[Path] = set()
    for entry in db:
        p = Path(entry["file"]).resolve()
        if p.suffix not in extensions or not p.is_relative_to(root) or p in seen:
            continue
        seen.add(p)
        files.append(p)

    if not files:
        print("no matching translation units")
        return []

    print(f"checking {len(files)} files across {jobs} workers")
    header_filter = f"^{re.escape(str(root))}/.*"

    def check_one(f: Path) -> tuple[Path, str]:
        proc = subprocess.run(
            [binary, "-p", ".", f"-header-filter={header_filter}", str(f)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, check=False,
        )
        return f, proc.stdout

    offenders: set[Path] = set()
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        for _, output in pool.map(check_one, files):
            for line in output.splitlines():
                m = _DIAG.match(line)
                if not m:
                    continue
                offender = Path(m.group("path")).resolve()
                if offender.is_relative_to(root):
                    offenders.add(offender)

    sorted_offenders = sorted(offenders)
    if report_path is not None:
        report_path.write_text(
            "\n".join(str(p.relative_to(root)) for p in sorted_offenders) + "\n",
            encoding="utf-8",
        )

    if not offenders:
        print("clean.")
        return []

    print(f"{len(offenders)} files with diagnostics")
    for p in sorted_offenders:
        print(f"  {p.relative_to(root)}")

    if not fix:
        return sorted_offenders

    print(f"fixing {len(offenders)} files serially")
    for f in sorted_offenders:
        print(f"=== FIXING: {f.relative_to(root)}")
        subprocess.run(
            [binary, "-p", ".", "-fix-errors", f"-header-filter={header_filter}", str(f)],
            check=False,
        )
    return sorted_offenders


# --- Built-in tasks ---
# Contract: a task named X reads its config from cfg.tools["X"] (i.e. [X] in project.toml).

def clang_tidy(cfg: Config) -> None:
    opts = cfg.tools.get("clang_tidy", {})
    clang_tidy_check_and_fix(
        binary=opts.get("binary", "clang-tidy"),
        jobs=opts.get("jobs"),
        fix=opts.get("fix", True),
    )


def xmake_config(cfg: Config) -> None:
    xmake("config")


def xmake_build(cfg: Config) -> None:
    xmake("build")


def npm_install(cfg: Config) -> None:
    pm = cfg.tools.get("npm_install", {}).get("package_manager", "npm")
    run([pm, "install"])


def eslint(cfg: Config) -> None:
    run(["npx", "eslint", "."])


def ruff(cfg: Config) -> None:
    run(["ruff", "check", "."])


# --- Resolver ---

def resolve_command(name: str, cfg: Config) -> list[str]:
    return list(cfg.commands.get(name, []))


def call(spec: str, cfg: Config) -> None:
    if ":" in spec:
        # module.path:attr -> importlib (e.g. scripts.deploy.staging:go)
        head, _, attr = spec.partition(":")
        if not attr:
            raise SystemExit(f"expected 'module:attr', got {spec!r}")
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        try:
            target: Any = importlib.import_module(head)
        except ModuleNotFoundError as e:
            raise SystemExit(f"cannot resolve {spec!r}: {e}") from None
        for piece in attr.split("."):
            target = getattr(target, piece, None)
            if target is None:
                raise SystemExit(f"{spec!r}: no attribute {piece!r}")
    else:
        # plain name -> built-in function in this module
        target = globals().get(spec)
        if target is None:
            raise SystemExit(f"unknown built-in: {spec!r}")

    if not callable(target):
        raise SystemExit(f"{spec!r} is not callable")
    target(cfg)


# --- init ---

_TOML_TEMPLATE = """\
[project]
name = "{name}"

[commands]
# Each command is a list of task references. A task can be:
#   - a built-in function name              (e.g. "clang_tidy", "xmake_build")
#   - a module path with attribute via ":"  (e.g. "scripts.deploy.staging:go")
#
# Uncomment / adapt for this project:
#
# setup = ["xmake_config"]
# build = ["xmake_build"]
# lint  = ["clang_tidy"]
#
# setup = ["npm_install"]
# lint  = ["eslint"]
#
# lint  = ["ruff"]

# --- Per-task config ---
# A task named X reads its config from [X] below.
#
# [clang_tidy]
# binary = "clang-tidy-21"
# jobs = 16
# fix = true
#
# [npm_install]
# package_manager = "pnpm"

# --- sync ---
# `python project.py sync` pulls files from this repo's templates/<name>/
# folders into your project. Names can be nested (e.g. "cpp/xmake").
# Requires GH_TOKEN. Last template wins on file conflicts.
# Writes .project-sync.lock — commit it so deletions propagate.
#
# [sync]
# templates = ["python-base", "github-actions"]
"""


def _fetch_preset(name: str) -> str:
    url = f"https://api.github.com/repos/{SELF_UPDATE_REPO}/contents/presets/{name}.toml?ref={TEMPLATES_REF}"
    data = _github_fetch_json(url, context=f"preset {name!r}")
    if not isinstance(data, dict) or "content" not in data:
        print(f"preset {name!r}: unexpected response", file=sys.stderr)
        raise SystemExit(1)
    return base64.b64decode(data["content"]).decode("utf-8")


def init(preset: str | None = None) -> None:
    if TOML_PATH.exists():
        print(f"{TOML_PATH.name} already exists.", file=sys.stderr)
        raise SystemExit(1)

    if preset is None:
        TOML_PATH.write_text(_TOML_TEMPLATE.format(name=ROOT.name), encoding="utf-8")
        print(f"wrote {TOML_PATH}")
        return

    if not os.environ.get("GH_TOKEN"):
        print(
            "init <preset> requires GH_TOKEN. Set it to a GitHub PAT "
            "(read-only public-repo access is enough).",
            file=sys.stderr,
        )
        raise SystemExit(1)

    body = _fetch_preset(preset)
    if not body.endswith("\n"):
        body += "\n"
    header = f'[project]\nname = "{ROOT.name}"\n\n'
    TOML_PATH.write_text(header + body, encoding="utf-8")
    print(f"wrote {TOML_PATH} (preset: {preset})")

    cfg = Config.load()
    if cfg.tools.get("sync", {}).get("templates"):
        print("running sync...")
        sync(cfg)


# --- GitHub helpers + self-update ---

def _github_request(url: str) -> Request:
    req = Request(url)
    token = os.environ.get("GH_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    return req


def _github_fetch_json(url: str, context: str = "") -> dict | list:
    try:
        with urlopen(_github_request(url), timeout=10) as response:
            return json.load(response)
    except HTTPError as e:
        ctx = context or url
        if e.code == 404:
            print(f"Not found: {ctx}", file=sys.stderr)
        elif e.code == 403:
            print(f"Access denied: {ctx} (set GH_TOKEN to authenticate)", file=sys.stderr)
        else:
            print(f"GitHub API error ({e.code}): {ctx}", file=sys.stderr)
        raise SystemExit(1)


def _github_blob_bytes(sha: str) -> bytes:
    url = f"https://api.github.com/repos/{SELF_UPDATE_REPO}/git/blobs/{sha}"
    data = _github_fetch_json(url, context=f"blob {sha[:8]}")
    if not isinstance(data, dict) or "content" not in data:
        print(f"unexpected blob response for {sha}", file=sys.stderr)
        raise SystemExit(1)
    return base64.b64decode(data["content"])


# --- sync ---

def _read_sync_lock() -> tuple[dict[str, str], set[str]]:
    # Returns (managed_path -> sha, append_paths). Pre-section files (no [managed]/[append]
    # headers) are treated as all-managed for backward compat.
    if not SYNC_LOCK_PATH.exists():
        return {}, set()
    managed: dict[str, str] = {}
    append: set[str] = set()
    section = "managed"
    for raw in SYNC_LOCK_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if section == "managed":
            sha, _, path = line.partition("  ")
            if sha and path:
                managed[path] = sha
        elif section == "append":
            append.add(line)
    return managed, append


def _write_sync_lock(managed: dict[str, str], append: set[str]) -> None:
    lines = ["# .project-sync.lock — written by `project.py sync`. Do not edit."]
    if managed:
        lines.append("")
        lines.append("[managed]")
        for path in sorted(managed):
            lines.append(f"{managed[path]}  {path}")
    if append:
        lines.append("")
        lines.append("[append]")
        for path in sorted(append):
            lines.append(path)
    SYNC_LOCK_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _list_template_files(
    templates: list[str],
) -> tuple[
    dict[str, tuple[str, str]],       # managed: target_path -> (template, sha)
    dict[str, tuple[str, str]],       # write_once: target_path -> (template, sha)
    list[tuple[str, str, str]],       # append: ordered [(template, target_path, sha)]
]:
    # Sorts each template's files into one of three buckets based on its in-template path:
    #   _write_once_/<rest>   -> write_once, lands at <rest>
    #   _append_/<rest>       -> append, block injected into <rest>
    #   <rest>                -> managed (overwrite-on-change), lands at <rest>
    # Managed: last template in the user's list wins on conflicts.
    # Append: every contributing template gets its own block in the destination file, in list order.
    url = f"https://api.github.com/repos/{SELF_UPDATE_REPO}/git/trees/{TEMPLATES_REF}?recursive=1"
    data = _github_fetch_json(url, context="template tree")
    if not isinstance(data, dict):
        print("unexpected tree response", file=sys.stderr)
        raise SystemExit(1)
    if data.get("truncated"):
        print("warning: GitHub tree response was truncated; some templates may be incomplete", file=sys.stderr)

    Bucket = tuple[str, str, str]  # (kind, target_relpath, sha)
    by_template: dict[str, list[Bucket]] = {t: [] for t in templates}
    # Longest prefix first so nested names like "cpp/foo" claim their subtree
    # before a broader "cpp" entry can swallow it.
    template_prefixes = sorted(
        ((t, f"{TEMPLATES_DIR}/{t}/") for t in templates),
        key=lambda p: len(p[1]),
        reverse=True,
    )
    write_once_marker = f"{WRITE_ONCE_DIR}/"
    append_marker = f"{APPEND_DIR}/"
    for entry in data.get("tree", []):
        if entry.get("type") != "blob":
            continue
        path = entry.get("path", "")
        for tname, tprefix in template_prefixes:
            if path.startswith(tprefix):
                rest = path[len(tprefix):]
                if rest.startswith(write_once_marker):
                    by_template[tname].append(("write_once", rest[len(write_once_marker):], entry["sha"]))
                elif rest.startswith(append_marker):
                    by_template[tname].append(("append", rest[len(append_marker):], entry["sha"]))
                else:
                    by_template[tname].append(("managed", rest, entry["sha"]))
                break

    missing = [t for t in templates if not by_template[t]]
    if missing:
        print(f"warning: no files found for template(s): {', '.join(missing)}", file=sys.stderr)

    managed: dict[str, tuple[str, str]] = {}
    write_once: dict[str, tuple[str, str]] = {}
    append: list[tuple[str, str, str]] = []
    for tname in templates:
        for kind, rel, sha in by_template[tname]:
            if kind == "managed":
                managed[rel] = (tname, sha)
            elif kind == "write_once":
                write_once[rel] = (tname, sha)
            else:  # append
                append.append((tname, rel, sha))

    # If any path appears as append, that wins over managed for the same path.
    # Warn so template authors notice the conflict.
    append_targets = {p for _, p, _ in append}
    conflicts = sorted(set(managed) & append_targets)
    for c in conflicts:
        print(f"warning: {c!r} is both managed and append; treating as append", file=sys.stderr)
        managed.pop(c, None)

    return managed, write_once, append


# --- append-block merge ---

_BLOCK_START_RE = re.compile(r"# \[START (.+?)\]")


def _format_append_block(name: str, body: str) -> str:
    return f"# [START {name}]\n{body.rstrip(chr(10))}\n# [END {name}]\n"


def _merge_append_blocks(file_path: Path, blocks: dict[str, str]) -> bool:
    # blocks: {template_name: block body without markers}. Returns True if file changed on disk.
    # Replaces existing # [START name] / # [END name] regions in place; appends new blocks
    # at the end; strips blocks whose template name is not in `blocks`.
    existing = file_path.read_text(encoding="utf-8") if file_path.exists() else ""
    new = existing

    existing_names = set(_BLOCK_START_RE.findall(new))
    wanted_names = set(blocks)

    # Replace blocks we want and that already exist
    for name in wanted_names & existing_names:
        pattern = re.compile(
            r"# \[START " + re.escape(name) + r"\]\n.*?\n# \[END " + re.escape(name) + r"\]\n?",
            re.DOTALL,
        )
        new = pattern.sub(_format_append_block(name, blocks[name]), new, count=1)

    # Strip blocks we no longer want (and any single trailing blank line that follows)
    for name in existing_names - wanted_names:
        pattern = re.compile(
            r"\n?# \[START " + re.escape(name) + r"\]\n.*?\n# \[END " + re.escape(name) + r"\]\n?",
            re.DOTALL,
        )
        new = pattern.sub("", new, count=1)

    # Append new blocks for templates not previously present
    for name in (n for n in blocks if n not in existing_names):
        if new and not new.endswith("\n"):
            new += "\n"
        if new and not new.endswith("\n\n"):
            new += "\n"
        new += _format_append_block(name, blocks[name])

    # Collapse runs of 3+ blank lines to 2
    new = re.sub(r"\n{3,}", "\n\n", new)
    if new and not new.endswith("\n"):
        new += "\n"

    if new == existing:
        return False
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(new, encoding="utf-8")
    return True


def _strip_all_our_blocks(file_path: Path) -> bool:
    # Strip every `# [START …]` / `# [END …]` block from a file that's no longer an append target.
    # Returns True if file changed; deletes the file if it becomes empty.
    if not file_path.exists():
        return False
    existing = file_path.read_text(encoding="utf-8")
    new = re.sub(
        r"\n?# \[START .+?\]\n.*?\n# \[END .+?\]\n?",
        "",
        existing,
        flags=re.DOTALL,
    )
    new = re.sub(r"\n{3,}", "\n\n", new)
    if new.strip() == "":
        file_path.unlink()
        return True
    if new == existing:
        return False
    file_path.write_text(new, encoding="utf-8")
    return True


def sync(cfg: Config) -> None:
    if not os.environ.get("GH_TOKEN"):
        print(
            "sync requires GH_TOKEN. Set it to a GitHub PAT "
            "(read-only public-repo access is enough).",
            file=sys.stderr,
        )
        raise SystemExit(1)

    opts = cfg.tools.get("sync", {})
    templates = opts.get("templates", [])
    if not templates:
        print("no [sync].templates defined in project.toml", file=sys.stderr)
        raise SystemExit(1)

    print(f"syncing {len(templates)} template(s): {', '.join(templates)}")
    managed, write_once, append = _list_template_files(templates)
    old_managed, old_append = _read_sync_lock()
    new_managed: dict[str, str] = {}

    written = 0
    skipped = 0
    seeded = 0
    preserved = 0
    for path, (tname, sha) in sorted(managed.items()):
        target = ROOT / path
        new_managed[path] = sha
        if old_managed.get(path) == sha and target.exists():
            skipped += 1
            continue
        content = _github_blob_bytes(sha)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        print(f"  wrote {path}  ({tname})")
        written += 1

    for path, (tname, sha) in sorted(write_once.items()):
        target = ROOT / path
        if target.exists():
            preserved += 1
            continue
        content = _github_blob_bytes(sha)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        print(f"  seeded {path}  ({tname})")
        seeded += 1

    # Group append entries by destination path, preserving template list order per path.
    append_by_target: dict[str, list[tuple[str, str]]] = {}
    for tname, path, sha in append:
        append_by_target.setdefault(path, []).append((tname, sha))

    merged = 0
    new_append_paths: set[str] = set()
    for path, entries in sorted(append_by_target.items()):
        new_append_paths.add(path)
        target = ROOT / path
        blocks: dict[str, str] = {}
        for tname, sha in entries:
            blocks[tname] = _github_blob_bytes(sha).decode("utf-8")
        if _merge_append_blocks(target, blocks):
            print(f"  merged {path}  ({', '.join(t for t, _ in entries)})")
            merged += 1

    # Delete managed files that fell out — but not if they're now append targets.
    deleted = 0
    for path in sorted(set(old_managed) - set(new_managed) - new_append_paths):
        target = ROOT / path
        if target.exists():
            try:
                target.unlink()
                print(f"  deleted {path}")
                deleted += 1
            except OSError as e:
                print(f"  could not delete {path}: {e}", file=sys.stderr)

    # Strip our blocks from files that used to be append targets but aren't anymore.
    stripped = 0
    for path in sorted(old_append - new_append_paths - set(new_managed)):
        target = ROOT / path
        if _strip_all_our_blocks(target):
            print(f"  unmerged {path}")
            stripped += 1

    _write_sync_lock(new_managed, new_append_paths)
    print(
        f"sync done: {written} written, {skipped} unchanged, "
        f"{seeded} seeded, {preserved} preserved, "
        f"{merged} merged, {stripped} unmerged, {deleted} deleted"
    )


def self_update() -> None:
    url = f"https://api.github.com/repos/{SELF_UPDATE_REPO}/contents/{SELF_UPDATE_PATH}?ref=main"
    try:
        data = _github_fetch_json(url, context="self-update")
    except SystemExit:
        print("Failed to check for updates.", file=sys.stderr)
        return
    new_content = base64.b64decode(data["content"])
    script_path = Path(__file__).resolve()
    old_content = script_path.read_bytes()
    if new_content == old_content:
        print("Already up to date.")
        return
    script_path.write_bytes(new_content)
    print(f"Updated {script_path}")


# --- CLI ---

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="project.py", description="one file to rule the repo")
    parser.add_argument("command", help="command name (init, setup, lint, build, self-update, ...)")
    parser.add_argument("args", nargs=argparse.REMAINDER, help="forwarded to tasks via cfg.args")
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    ns = parser.parse_args(argv)

    if ns.command == "self-update":
        self_update()
        return 0
    if ns.command == "init":
        init(ns.args[0] if ns.args else None)
        return 0

    cfg = Config.load()
    cfg.args = ns.args

    if ns.command == "sync":
        sync(cfg)
        return 0

    tasks = resolve_command(ns.command, cfg)
    if not tasks:
        print(f"no '{ns.command}' defined in [commands]", file=sys.stderr)
        return 2
    for spec in tasks:
        call(spec, cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
