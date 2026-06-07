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
#
# Design:
#   The decision logic (template bucketing, append-block merge, lock parse/format,
#   {{var}} substitution, git blob sha, command resolution, clang-tidy diagnostic
#   parsing) is a set of PURE functions — data in, data out — tested by direct call.
#   The effectful shell (sync / init / self_update / dispatch) takes its dependencies
#   as explicit arguments: a `root` Path, a `Source`, and a `Runner`. Tests pass a
#   real tmp dir + an in-memory `Source` subclass + a recording `Runner`. No globals
#   are read by the logic and nothing is monkeypatched. `main()` is the only place
#   that wires the production defaults and turns a `ProjectError` into an exit code.
from __future__ import annotations

import argparse
import base64
import hashlib
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
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.request import Request, urlopen

__version__ = "0.1.0"

SELF_UPDATE_REPO = "BuildWithCollab/project.py"
SELF_UPDATE_PATH = "project.py"

# When [sources].repos is absent, templates (and presets / self-update) come from here.
DEFAULT_REPOS = [SELF_UPDATE_REPO]

TEMPLATES_REF = "main"
TEMPLATES_DIR = "templates"
WRITE_ONCE_DIR = "_write_once_"
APPEND_DIR = "_append_"

# An os.pathsep-separated list of LOCAL FOLDERS, searched ahead of the github repos —
# like $PATH. A folder that provides templates/<name>/ shadows the same-named template
# from a github repo, so you can iterate on templates locally without pushing.
PATH_ENV = "PROJECT_PY_PATH"
TOML_NAME = "project.toml"
SYNC_LOCK_NAME = ".project-sync.lock"
# Per-machine cache of each github repo's template tree, keyed by ETag. Lets `sync`
# send If-None-Match and take a free 304 instead of re-listing an unchanged tree.
# NOT committed (unlike the lock) — it's a network cache, gitignore it.
SYNC_CACHE_NAME = ".project-sync-cache.json"


class ProjectError(Exception):
    """A user-facing error. Raised by core logic, formatted + exited at the CLI edge."""

    def __init__(self, message: str, *, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


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


_PLATFORM_SUFFIX = {
    Platform.WINDOWS: "windows",
    Platform.MAC: "macos",
    Platform.LINUX: "linux",
}


# --- Config ---

@dataclass
class Config:
    project: dict[str, Any] = field(default_factory=dict)
    commands: dict[str, list[str]] = field(default_factory=dict)
    tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    args: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "Config":
        if not path.exists():
            return cls()
        with path.open("rb") as f:
            data = tomllib.load(f)
        return cls(
            project=data.get("project", {}),
            commands=data.get("commands", {}),
            tools={k: v for k, v in data.items() if k not in {"project", "commands"}},
        )


# --- subprocess: a public helper for consumer scripts, plus an injectable Runner ---

def _echo_run(cmd: list[str] | str, *, check: bool = True, shell: bool = False, **kw: Any) -> subprocess.CompletedProcess:
    pretty = cmd if isinstance(cmd, str) else " ".join(str(a) for a in cmd)
    print(f"$ {pretty}", flush=True)
    return subprocess.run(cmd, check=check, shell=shell, **kw)


def run(cmd: list[str] | str, *, check: bool = True, **kw: Any) -> subprocess.CompletedProcess:
    """Friendly one-shot subprocess for consumer scripts: echoes the command, then runs it."""
    return _echo_run(cmd, check=check, **kw)


def xmake(*args: str, **kw: Any) -> subprocess.CompletedProcess:
    return run(["xmake", *args], **kw)


class Runner:
    """The subprocess seam used by built-in tasks and command dispatch.

    Real implementation echoes + shells out. Tests pass a recording stand-in so
    dispatch and the built-in tasks are exercisable without spawning processes.
    """

    def run(self, cmd: list[str] | str, *, shell: bool = False, check: bool = True,
            capture: bool = False) -> subprocess.CompletedProcess:
        if capture:
            return _echo_run(cmd, shell=shell, check=check,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        return _echo_run(cmd, shell=shell, check=check)


# --- pure: variable substitution ({{section.key}}) ---

_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z_][\w.]*)\s*\}\}")


def scalar(v: Any) -> str | None:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (str, int, float)):
        return str(v)
    return None


def build_var_lookup(cfg: Config) -> dict[str, str]:
    flat: dict[str, str] = {}
    for k, v in cfg.project.items():
        s = scalar(v)
        if s is not None:
            flat[f"project.{k}"] = s
    for section, body in cfg.tools.items():
        if not isinstance(body, dict):
            continue
        for k, v in body.items():
            s = scalar(v)
            if s is not None:
                flat[f"{section}.{k}"] = s
    return flat


def substitute(content: bytes, lookup: dict[str, str]) -> bytes:
    if not lookup:
        return content
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return content
    new_text = _VAR_RE.sub(lambda m: lookup.get(m.group(1), m.group(0)), text)
    if new_text == text:
        return content
    return new_text.encode("utf-8")


def cfg_hash(cfg: Config) -> str:
    payload = json.dumps({"project": cfg.project, "tools": cfg.tools}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --- pure: git blob sha (so local + GitHub sources agree on content identity) ---

def git_normalize(data: bytes) -> bytes:
    # Mirror git's default text handling so local shas match what GitHub serves:
    # text blobs are stored LF-normalized; a file with a NUL byte is treated as
    # binary and passed through untouched (git's own text/binary heuristic).
    if b"\x00" in data:
        return data
    return data.replace(b"\r\n", b"\n")


def git_blob_sha(data: bytes) -> str:
    # sha1 of "blob <len>\0" + content, matching `git hash-object` on LF-normalized bytes.
    h = hashlib.sha1()
    h.update(b"blob " + str(len(data)).encode() + b"\x00")
    h.update(data)
    return h.hexdigest()


# --- pure: command resolution ---

def resolve_command(name: str, commands: dict[str, list[str]], plat: Platform) -> list[str]:
    # Platform-specific overrides win: `name:windows` / `name:macos` / `name:linux`,
    # falling back to the plain `name`.
    plat_key = f"{name}:{_PLATFORM_SUFFIX[plat]}"
    if plat_key in commands:
        return list(commands[plat_key])
    return list(commands.get(name, []))


def classify_spec(spec: str) -> tuple[str, Any]:
    """Pure routing decision for a task reference.

    Returns ("shell", cmd) / ("module", (module_path, attr)) / ("builtin", name).
    """
    if spec.startswith("$"):
        cmd = spec[1:].lstrip()
        if not cmd:
            raise ProjectError(f"empty shell command: {spec!r}")
        return ("shell", cmd)
    if ":" in spec:
        head, _, attr = spec.partition(":")
        if not attr:
            raise ProjectError(f"expected 'module:attr', got {spec!r}")
        return ("module", (head, attr))
    return ("builtin", spec)


# --- clang-tidy: pure selection/parsing + an effectful two-pass orchestrator ---

_DIAG = re.compile(r"^(?P<path>[^:]+):\d+:\d+:\s+(warning|error):\s")
CLANG_TIDY_EXTS = {".cppm", ".cpp", ".cc", ".cxx"}


def select_translation_units(db: list[dict], root: Path, extensions: set[str]) -> list[Path]:
    files: list[Path] = []
    seen: set[Path] = set()
    for entry in db:
        p = Path(entry["file"]).resolve()
        if p.suffix not in extensions or not p.is_relative_to(root) or p in seen:
            continue
        seen.add(p)
        files.append(p)
    return files


def parse_diagnostics(output: str, root: Path) -> set[Path]:
    offenders: set[Path] = set()
    for line in output.splitlines():
        m = _DIAG.match(line)
        if not m:
            continue
        offender = Path(m.group("path")).resolve()
        if offender.is_relative_to(root):
            offenders.add(offender)
    return offenders


def clang_tidy_check_and_fix(
    *,
    runner: Runner,
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

    if not compile_commands.is_file():
        raise ProjectError(f"{compile_commands} not found (run a build/config first to generate it)")

    db = json.loads(compile_commands.read_text(encoding="utf-8"))
    files = select_translation_units(db, root, extensions)
    if not files:
        print("no matching translation units")
        return []

    print(f"checking {len(files)} files across {jobs} workers")
    header_filter = f"^{re.escape(str(root))}/.*"

    def check_one(f: Path) -> set[Path]:
        result = runner.run(
            [binary, "-p", ".", f"-header-filter={header_filter}", str(f)],
            capture=True, check=False,
        )
        return parse_diagnostics(result.stdout, root)

    offenders: set[Path] = set()
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        for found in pool.map(check_one, files):
            offenders |= found

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
        runner.run([binary, "-p", ".", "-fix-errors", f"-header-filter={header_filter}", str(f)], check=False)
    return sorted_offenders


# --- Built-in tasks ---
# Contract: a task named X reads its config from cfg.tools["X"] (i.e. [X] in project.toml).

def clang_tidy(cfg: Config, runner: Runner) -> None:
    opts = cfg.tools.get("clang_tidy", {})
    clang_tidy_check_and_fix(
        runner=runner,
        binary=opts.get("binary", "clang-tidy"),
        jobs=opts.get("jobs"),
        fix=opts.get("fix", True),
    )


def xmake_config(cfg: Config, runner: Runner) -> None:
    runner.run(["xmake", "config"])


def xmake_build(cfg: Config, runner: Runner) -> None:
    runner.run(["xmake", "build"])


def npm_install(cfg: Config, runner: Runner) -> None:
    pm = cfg.tools.get("npm_install", {}).get("package_manager", "npm")
    runner.run([pm, "install"])


def eslint(cfg: Config, runner: Runner) -> None:
    runner.run(["npx", "eslint", "."])


def ruff(cfg: Config, runner: Runner) -> None:
    runner.run(["ruff", "check", "."])


BUILTINS: dict[str, Callable[[Config, Runner], None]] = {
    "clang_tidy": clang_tidy,
    "xmake_config": xmake_config,
    "xmake_build": xmake_build,
    "npm_install": npm_install,
    "eslint": eslint,
    "ruff": ruff,
}


# --- dispatch ---

def _resolve_module_attr(module_path: str, attr: str, root: Path) -> Any:
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        target: Any = importlib.import_module(module_path)
    except ModuleNotFoundError as e:
        raise ProjectError(f"cannot resolve {module_path!r}: {e}") from None
    for piece in attr.split("."):
        target = getattr(target, piece, None)
        if target is None:
            raise ProjectError(f"{module_path}:{attr}: no attribute {piece!r}")
    return target


def dispatch(spec: str, cfg: Config, *, root: Path, runner: Runner) -> None:
    kind, payload = classify_spec(spec)
    if kind == "shell":
        runner.run(payload, shell=True)
        return
    if kind == "builtin":
        fn = BUILTINS.get(payload)
        if fn is None:
            raise ProjectError(f"unknown built-in: {spec!r}")
        fn(cfg, runner)
        return
    module_path, attr = payload
    target = _resolve_module_attr(module_path, attr, root)
    if not callable(target):
        raise ProjectError(f"{spec!r} is not callable")
    target(cfg)


# --- Source: where self-update / sync / presets read from ---
# A Source is one place to read from — a github repo (GitHubSource) or a local folder
# (LocalSource). SearchPathSource chains several in order. Local sources compute the
# *same* git blob shas GitHub serves, so .project-sync.lock stays source-agnostic and a
# PROJECT_PY_PATH checkout can transparently shadow a repo's template.

class Source:
    name = "?"

    def ensure_ready(self) -> None:
        """Fail fast if this source can't be used (e.g. missing credentials)."""

    def read(self, path: str) -> bytes:
        """Read a single repo-relative file's bytes."""
        raise NotImplementedError

    def list_blobs(self, wanted: list[str] | None = None) -> list[tuple[str, str]]:
        """Every blob under templates/, as (repo-relative posix path, git sha).

        `wanted` (template names sync actually asked for) is a hint a composite source
        uses to skip sources it doesn't need; single sources may ignore it and list all.
        """
        raise NotImplementedError

    def blob(self, sha: str) -> bytes:
        """Read a blob's bytes by the sha returned from list_blobs()."""
        raise NotImplementedError


_GH_API = "https://api.github.com"


def gh_contents_url(repo: str, path: str, ref: str = TEMPLATES_REF) -> str:
    return f"{_GH_API}/repos/{repo}/contents/{path}?ref={ref}"


def gh_tree_url(repo: str, ref: str = TEMPLATES_REF) -> str:
    return f"{_GH_API}/repos/{repo}/git/trees/{ref}?recursive=1"


def gh_blob_url(repo: str, sha: str) -> str:
    return f"{_GH_API}/repos/{repo}/git/blobs/{sha}"


# --- pure: tree parse + the conditional-fetch decision (testable without network) ---

def parse_tree(data: dict) -> list[tuple[str, str]]:
    """Pull the blob entries out of a git/trees response as (path, sha)."""
    return [(e["path"], e["sha"]) for e in data.get("tree", []) if e.get("type") == "blob"]


def resolve_tree(
    status: int,
    etag: str | None,
    data: dict | None,
    cached_blobs: list[tuple[str, str]] | None,
) -> tuple[list[tuple[str, str]], tuple[str | None, list[tuple[str, str]]] | None]:
    """Decide what a conditional tree fetch yields.

    Returns (blobs, cache_entry). On 304 we reuse the cached blobs and write nothing
    (cache_entry is None); on 200 we parse the fresh tree and hand back (etag, blobs)
    to store. Pure — the network call is the caller's problem.
    """
    if status == 304:
        return (cached_blobs or []), None
    blobs = parse_tree(data or {})
    return blobs, (etag, blobs)


class TreeCache:
    """Per-repo template-tree cache on disk, keyed by ETag. Per-machine, not committed.

    A corrupt or missing file is treated as empty — a cache miss must never break sync.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._data: dict | None = None

    def _load(self) -> None:
        if self._data is not None:
            return
        try:
            self._data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._data = {}

    def get(self, repo: str) -> tuple[str | None, list[tuple[str, str]] | None]:
        self._load()
        entry = self._data.get(repo) if isinstance(self._data, dict) else None
        if not entry:
            return None, None
        return entry.get("etag"), [tuple(b) for b in entry.get("blobs", [])]

    def put(self, repo: str, etag: str | None, blobs: list[tuple[str, str]]) -> None:
        self._load()
        self._data[repo] = {"etag": etag, "blobs": [list(b) for b in blobs]}
        try:
            self.path.write_text(
                json.dumps(self._data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        except OSError as e:
            print(f"warning: could not write tree cache {self.path}: {e}", file=sys.stderr)


class GitHubSource(Source):
    def __init__(
        self,
        token: str | None = None,
        *,
        repo: str = SELF_UPDATE_REPO,
        cache: TreeCache | None = None,
    ) -> None:
        self.token = token
        self.repo = repo
        self.cache = cache

    @property
    def name(self) -> str:
        return f"github:{self.repo}"

    def ensure_ready(self) -> None:
        if not self.token:
            raise ProjectError(
                "GitHub access requires GH_TOKEN. Set it to a GitHub PAT (read-only "
                f"public-repo access is enough), or put a local checkout on {PATH_ENV}."
            )

    def _request(self, url: str) -> Request:
        req = Request(url)
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        return req

    def _fetch_json(self, url: str, context: str = "") -> dict | list:
        try:
            with urlopen(self._request(url), timeout=10) as response:
                return json.load(response)
        except HTTPError as e:
            ctx = context or url
            if e.code == 404:
                raise ProjectError(f"Not found: {ctx}")
            if e.code == 403:
                raise ProjectError(f"Access denied: {ctx} (set GH_TOKEN to authenticate)")
            raise ProjectError(f"GitHub API error ({e.code}): {ctx}")

    def _conditional_get(self, url: str, etag: str | None = None, context: str = "") -> tuple[int, str | None, dict | list | None]:
        # Like _fetch_json, but sends If-None-Match and surfaces a 304 instead of raising.
        # Returns (status, response ETag, parsed JSON | None-on-304).
        req = self._request(url)
        if etag:
            req.add_header("If-None-Match", etag)
        try:
            with urlopen(req, timeout=10) as response:
                status = getattr(response, "status", None) or response.getcode()
                return status, response.headers.get("ETag"), json.load(response)
        except HTTPError as e:
            if e.code == 304:
                return 304, etag, None
            ctx = context or url
            if e.code == 404:
                raise ProjectError(f"Not found: {ctx}")
            if e.code == 403:
                raise ProjectError(f"Access denied: {ctx} (set GH_TOKEN to authenticate)")
            raise ProjectError(f"GitHub API error ({e.code}): {ctx}")

    def read(self, path: str) -> bytes:
        data = self._fetch_json(gh_contents_url(self.repo, path), context=path)
        if not isinstance(data, dict) or "content" not in data:
            raise ProjectError(f"unexpected response for {path}")
        return base64.b64decode(data["content"])

    def list_blobs(self, wanted: list[str] | None = None) -> list[tuple[str, str]]:
        # Conditional on the cached ETag: an unchanged tree comes back 304 (free, no body,
        # no rate-limit cost) and we reuse the cached blob list instead of re-listing.
        etag, cached = self.cache.get(self.repo) if self.cache else (None, None)
        status, new_etag, data = self._conditional_get(
            gh_tree_url(self.repo), etag, context="template tree"
        )
        if status != 304:
            if not isinstance(data, dict):
                raise ProjectError("unexpected tree response")
            if data.get("truncated"):
                print("warning: GitHub tree response was truncated; some templates may be incomplete", file=sys.stderr)
        blobs, entry = resolve_tree(status, new_etag, data if isinstance(data, dict) else None, cached)
        if entry is not None and self.cache is not None:
            self.cache.put(self.repo, entry[0], entry[1])
        return blobs

    def blob(self, sha: str) -> bytes:
        data = self._fetch_json(gh_blob_url(self.repo, sha), context=f"blob {sha[:8]}")
        if not isinstance(data, dict) or "content" not in data:
            raise ProjectError(f"unexpected blob response for {sha}")
        return base64.b64decode(data["content"])


class LocalSource(Source):
    name = "local"

    def __init__(self, root: Path) -> None:
        self.root = root
        self._sha_to_path: dict[str, Path] = {}

    def read(self, path: str) -> bytes:
        target = self.root / path
        if not target.is_file():
            raise ProjectError(f"not found in local source {self.root}: {path}")
        return git_normalize(target.read_bytes())

    def list_blobs(self, wanted: list[str] | None = None) -> list[tuple[str, str]]:
        self._sha_to_path = {}
        blobs: list[tuple[str, str]] = []
        base = self.root / TEMPLATES_DIR
        if not base.is_dir():
            return blobs
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            sha = git_blob_sha(git_normalize(p.read_bytes()))
            rel = p.relative_to(self.root).as_posix()
            blobs.append((rel, sha))
            self._sha_to_path[sha] = p
        return blobs

    def blob(self, sha: str) -> bytes:
        path = self._sha_to_path.get(sha)
        if path is None:
            raise ProjectError(f"local source: no blob for sha {sha[:8]} (call list_blobs first)")
        return git_normalize(path.read_bytes())


class SearchPathSource(Source):
    """Several sources searched in order, $PATH-style: the first to provide a template
    owns it, shadowing every later source's same-named template.

    Shadowing is keyed on the template name — the first path segment under templates/
    (templates/<name>/...). A source "claims" <name> if it has any blob under it; once
    claimed, later sources' blobs for that <name> are dropped. read()/blob() route to
    the owning source. This is what lets a local checkout on PROJECT_PY_PATH override a
    github repo's template without touching project.toml.
    """

    name = "search path"

    def __init__(self, children: list[Source]) -> None:
        self.children = list(children)
        self._sha_to_child: dict[str, Source] = {}

    def ensure_ready(self) -> None:
        # Deliberately lazy: a child's readiness is checked only if list_blobs/read
        # actually reaches it. That's what lets a local PROJECT_PY_PATH that covers every
        # wanted template avoid ever consulting (and demanding a token for) a github repo.
        pass

    @staticmethod
    def _provides(blobs: list[tuple[str, str]], template: str) -> list[tuple[str, str]]:
        prefix = f"{TEMPLATES_DIR}/{template}/"
        return [(p, s) for p, s in blobs if p.startswith(prefix)]

    @staticmethod
    def _top_level(blobs: list[tuple[str, str]]) -> set[str]:
        names = set()
        for path, _ in blobs:
            parts = path.split("/")
            if len(parts) > 1:
                names.add(parts[1])
        return names

    def _consult(self, child: Source) -> list[tuple[str, str]]:
        child.ensure_ready()
        return child.list_blobs()

    def read(self, path: str) -> bytes:
        last: ProjectError | None = None
        for child in self.children:
            try:
                return child.read(path)
            except ProjectError as e:
                last = e
        raise last or ProjectError(f"not found on search path: {path}")

    def list_blobs(self, wanted: list[str] | None = None) -> list[tuple[str, str]]:
        # Walk children in order; the first child that provides a given template owns it
        # (every file under templates/<template>/). Ownership is keyed on the requested
        # template name, so "cpp/base" and "cpp/xmake" from different repos don't collide.
        # Once every wanted template is claimed we stop — later children are never touched.
        self._sha_to_child = {}
        out: list[tuple[str, str]] = []
        seen_paths: set[str] = set()

        def take(child: Source, blobs: list[tuple[str, str]]) -> None:
            for path, sha in blobs:
                if path in seen_paths:
                    continue
                seen_paths.add(path)
                out.append((path, sha))
                self._sha_to_child.setdefault(sha, child)

        if wanted is None:
            # No hint: own by top-level template dir, first child wins, consult everyone.
            claimed: set[str] = set()
            for child in self.children:
                blobs = self._consult(child)
                for name in self._top_level(blobs) - claimed:
                    take(child, self._provides(blobs, name))
                    claimed.add(name)
            return out

        remaining = list(dict.fromkeys(wanted))  # de-dup, keep order
        for child in self.children:
            if not remaining:
                break
            blobs = self._consult(child)
            still: list[str] = []
            for template in remaining:
                provided = self._provides(blobs, template)
                if provided:
                    take(child, provided)
                else:
                    still.append(template)
            remaining = still
        return out

    def blob(self, sha: str) -> bytes:
        child = self._sha_to_child.get(sha)
        if child is None:
            raise ProjectError(f"no blob for sha {sha[:8]} (call list_blobs first)")
        return child.blob(sha)


def get_source(repos: list[str], env: dict | None = None, *, cache: TreeCache | None = None) -> Source:
    # Build the search path: local folders from PROJECT_PY_PATH first (so they shadow
    # the remotes), then the github repos in declared order. A single source is returned
    # bare; two or more are wrapped in a SearchPathSource. The tree cache, if given, is
    # shared by every github repo (it keys entries by repo, so one file covers them all).
    env = os.environ if env is None else env
    children: list[Source] = []
    for entry in env.get(PATH_ENV, "").split(os.pathsep):
        entry = entry.strip()
        if not entry:
            continue
        root = Path(entry).expanduser().resolve()
        if not root.is_dir():
            raise ProjectError(f"{PATH_ENV} entry {entry!r} is not a directory")
        children.append(LocalSource(root))
    token = env.get("GH_TOKEN")
    children.extend(GitHubSource(token, repo=repo, cache=cache) for repo in repos)
    if not children:
        raise ProjectError(f"no template sources: empty [sources].repos and no {PATH_ENV}")
    return children[0] if len(children) == 1 else SearchPathSource(children)


def repos_for(cfg: Config) -> list[str]:
    """The github repos a project pulls templates from: [sources].repos, or DEFAULT_REPOS."""
    raw = cfg.tools.get("sources", {}).get("repos")
    if raw is None:
        return list(DEFAULT_REPOS)
    if isinstance(raw, str):
        return [raw]
    return list(raw)


# --- pure: classify template files into managed / write-once / append ---

@dataclass
class TemplateFiles:
    managed: dict[str, tuple[str, str]]      # target_path -> (template, sha)
    write_once: dict[str, tuple[str, str]]   # target_path -> (template, sha)
    append: list[tuple[str, str, str]]       # ordered [(template, target_path, sha)]
    warnings: list[str]


def classify_template_files(blobs: list[tuple[str, str]], templates: list[str]) -> TemplateFiles:
    # Sorts each template's files into a bucket by its in-template path:
    #   _write_once_/<rest>  -> write_once, lands at <rest>
    #   _append_/<rest>      -> append, block injected into <rest>
    #   <rest>               -> managed (overwrite-on-change), lands at <rest>
    # Managed: last template in the user's list wins on conflicts.
    # Append: every contributing template gets its own block, in list order.
    by_template: dict[str, list[tuple[str, str, str]]] = {t: [] for t in templates}
    # Longest prefix first so nested names like "cpp/foo" claim their subtree
    # before a broader "cpp" entry can swallow it.
    template_prefixes = sorted(
        ((t, f"{TEMPLATES_DIR}/{t}/") for t in templates),
        key=lambda p: len(p[1]),
        reverse=True,
    )
    write_once_marker = f"{WRITE_ONCE_DIR}/"
    append_marker = f"{APPEND_DIR}/"
    for path, sha in blobs:
        for tname, tprefix in template_prefixes:
            if path.startswith(tprefix):
                rest = path[len(tprefix):]
                if rest.startswith(write_once_marker):
                    by_template[tname].append(("write_once", rest[len(write_once_marker):], sha))
                elif rest.startswith(append_marker):
                    by_template[tname].append(("append", rest[len(append_marker):], sha))
                else:
                    by_template[tname].append(("managed", rest, sha))
                break

    warnings: list[str] = []
    missing = [t for t in templates if not by_template[t]]
    if missing:
        warnings.append(f"warning: no files found for template(s): {', '.join(missing)}")

    managed: dict[str, tuple[str, str]] = {}
    write_once: dict[str, tuple[str, str]] = {}
    append: list[tuple[str, str, str]] = []
    for tname in templates:
        for kind, rel, sha in by_template[tname]:
            if kind == "managed":
                managed[rel] = (tname, sha)
            elif kind == "write_once":
                write_once[rel] = (tname, sha)
            else:
                append.append((tname, rel, sha))

    # If a path is both managed and an append target, append wins.
    append_targets = {p for _, p, _ in append}
    for c in sorted(set(managed) & append_targets):
        warnings.append(f"warning: {c!r} is both managed and append; treating as append")
        managed.pop(c, None)

    return TemplateFiles(managed, write_once, append, warnings)


# --- pure: append-block merge ---

_BLOCK_START_RE = re.compile(r"# \[START (.+?)\]")


def format_append_block(name: str, body: str) -> str:
    return f"# [START {name}]\n{body.rstrip(chr(10))}\n# [END {name}]\n"


def merge_append_blocks(existing: str, blocks: dict[str, str]) -> str:
    # blocks: {template_name: block body without markers}. Returns the new file text.
    # Replaces existing # [START name] / # [END name] regions in place, appends new
    # blocks at the end, and strips blocks whose template is no longer wanted.
    new = existing
    existing_names = set(_BLOCK_START_RE.findall(new))
    wanted_names = set(blocks)

    # Replace blocks we want and that already exist. A function replacement is used
    # (not a replacement string) so block bodies containing backslashes / \1 etc.
    # are inserted literally rather than interpreted by re.
    for name in wanted_names & existing_names:
        pattern = re.compile(
            r"# \[START " + re.escape(name) + r"\]\n.*?\n# \[END " + re.escape(name) + r"\]\n?",
            re.DOTALL,
        )
        new = pattern.sub(lambda _m, n=name: format_append_block(n, blocks[n]), new, count=1)

    # Strip blocks we no longer want (and any single trailing blank line that follows).
    for name in existing_names - wanted_names:
        pattern = re.compile(
            r"\n?# \[START " + re.escape(name) + r"\]\n.*?\n# \[END " + re.escape(name) + r"\]\n?",
            re.DOTALL,
        )
        new = pattern.sub("", new, count=1)

    # Append new blocks for templates not previously present.
    for name in (n for n in blocks if n not in existing_names):
        if new and not new.endswith("\n"):
            new += "\n"
        if new and not new.endswith("\n\n"):
            new += "\n"
        new += format_append_block(name, blocks[name])

    new = re.sub(r"\n{3,}", "\n\n", new)
    if new and not new.endswith("\n"):
        new += "\n"
    return new


def strip_all_blocks(text: str) -> str:
    # Remove every `# [START …]` / `# [END …]` block; collapse blank-line runs.
    new = re.sub(r"\n?# \[START .+?\]\n.*?\n# \[END .+?\]\n?", "", text, flags=re.DOTALL)
    new = re.sub(r"\n{3,}", "\n\n", new)
    return new


# --- pure: .project-sync.lock parse / format ---

def parse_lock(text: str) -> tuple[str, dict[str, str], set[str]]:
    # Returns (cfg_hash, managed_path -> sha, append_paths). Pre-section files (no
    # [cfg_hash]/[managed]/[append] headers) are treated as all-managed for backward compat.
    cfg_hash_val = ""
    managed: dict[str, str] = {}
    append: set[str] = set()
    section = "managed"
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if section == "cfg_hash":
            cfg_hash_val = line
        elif section == "managed":
            sha, _, path = line.partition("  ")
            if sha and path:
                managed[path] = sha
        elif section == "append":
            append.add(line)
    return cfg_hash_val, managed, append


def format_lock(cfg_hash_val: str, managed: dict[str, str], append: set[str]) -> str:
    lines = [
        "# .project-sync.lock — written by `project.py sync`. Do not edit.",
        "",
        "[cfg_hash]",
        cfg_hash_val,
    ]
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
    return "\n".join(lines) + "\n"


# --- sync (effectful shell) ---

def sync(cfg: Config, *, root: Path, source: Source) -> None:
    source.ensure_ready()

    templates = cfg.tools.get("sync", {}).get("templates", [])
    if not templates:
        raise ProjectError("no [sync].templates defined in project.toml")

    print(f"syncing {len(templates)} template(s) from {source.name}: {', '.join(templates)}")
    files = classify_template_files(source.list_blobs(templates), templates)
    for w in files.warnings:
        print(w, file=sys.stderr)

    lock_path = root / SYNC_LOCK_NAME
    old_text = lock_path.read_text(encoding="utf-8") if lock_path.exists() else ""
    old_cfg_hash, old_managed, old_append = parse_lock(old_text)
    new_cfg_hash = cfg_hash(cfg)
    cfg_changed = new_cfg_hash != old_cfg_hash
    lookup = build_var_lookup(cfg)

    written = skipped = seeded = preserved = merged = stripped = deleted = 0
    new_managed: dict[str, str] = {}

    for path, (tname, sha) in sorted(files.managed.items()):
        target = root / path
        new_managed[path] = sha
        if not cfg_changed and old_managed.get(path) == sha and target.exists():
            skipped += 1
            continue
        content = substitute(source.blob(sha), lookup)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        print(f"  wrote {path}  ({tname})")
        written += 1

    for path, (tname, sha) in sorted(files.write_once.items()):
        target = root / path
        if target.exists():
            preserved += 1
            continue
        content = substitute(source.blob(sha), lookup)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        print(f"  seeded {path}  ({tname})")
        seeded += 1

    # Group append entries by destination, preserving template list order per path.
    append_by_target: dict[str, list[tuple[str, str]]] = {}
    for tname, path, sha in files.append:
        append_by_target.setdefault(path, []).append((tname, sha))

    new_append_paths: set[str] = set()
    for path, entries in sorted(append_by_target.items()):
        new_append_paths.add(path)
        target = root / path
        blocks = {tname: substitute(source.blob(sha), lookup).decode("utf-8") for tname, sha in entries}
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        new = merge_append_blocks(existing, blocks)
        if new != existing:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(new, encoding="utf-8")
            print(f"  merged {path}  ({', '.join(t for t, _ in entries)})")
            merged += 1

    # Delete managed files that fell out — unless they're now append targets.
    for path in sorted(set(old_managed) - set(new_managed) - new_append_paths):
        target = root / path
        if target.exists():
            try:
                target.unlink()
                print(f"  deleted {path}")
                deleted += 1
            except OSError as e:
                print(f"  could not delete {path}: {e}", file=sys.stderr)

    # Strip our blocks from files that used to be append targets but aren't anymore.
    for path in sorted(old_append - new_append_paths - set(new_managed)):
        target = root / path
        if not target.exists():
            continue
        existing = target.read_text(encoding="utf-8")
        new = strip_all_blocks(existing)
        if new.strip() == "":
            target.unlink()
            print(f"  unmerged {path}")
            stripped += 1
        elif new != existing:
            target.write_text(new, encoding="utf-8")
            print(f"  unmerged {path}")
            stripped += 1

    lock_path.write_text(format_lock(new_cfg_hash, new_managed, new_append_paths), encoding="utf-8")
    print(
        f"sync done: {written} written, {skipped} unchanged, "
        f"{seeded} seeded, {preserved} preserved, "
        f"{merged} merged, {stripped} unmerged, {deleted} deleted"
    )


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
# `python project.py sync` pulls files from templates/<name>/ folders into
# your project. Names can be nested (e.g. "cpp/xmake"). Last template wins on
# file conflicts. Writes .project-sync.lock — commit it so deletions propagate.
#
# [sync]
# templates = ["python-base", "github-actions"]

# --- sources ---
# Where templates come from: an ordered list of github repos. Defaults to the
# project.py repo if omitted. A template is resolved by searching the repos in
# order; the first repo that has templates/<name>/ wins. Requires GH_TOKEN.
#
# [sources]
# repos = ["BuildWithCollab/project.py", "you/your-templates"]
#
# For local iteration, set PROJECT_PY_PATH to one or more local folders
# (os.pathsep-separated). They're searched AHEAD of the repos — like $PATH —
# so a local templates/<name>/ shadows the same-named template from a repo.
# Nothing local goes in project.toml; it stays portable.
"""


def init(
    root: Path,
    preset: str | None = None,
    *,
    source: Source | None = None,
    runner: Runner | None = None,
    env: dict | None = None,
) -> None:
    toml_path = root / TOML_NAME
    if toml_path.exists():
        raise ProjectError(f"{TOML_NAME} already exists.")

    if preset is None:
        toml_path.write_text(_TOML_TEMPLATE.format(name=root.name), encoding="utf-8")
        print(f"wrote {toml_path}")
        return

    # The preset itself can't yet know its own [sources] (we're about to write it), so
    # it's read off the DEFAULT search path. The chained sync, however, is built AFTER
    # the preset is loaded, so it honors the preset's own [sources].repos. An injected
    # source (tests) overrides both.
    preset_source = source or get_source(DEFAULT_REPOS, env)
    preset_source.ensure_ready()

    body = preset_source.read(f"presets/{preset}.toml").decode("utf-8")
    if not body.endswith("\n"):
        body += "\n"
    header = f'[project]\nname = "{root.name}"\n\n'
    toml_path.write_text(header + body, encoding="utf-8")
    print(f"wrote {toml_path} (preset: {preset})")

    cfg = Config.load(toml_path)
    if cfg.tools.get("sync", {}).get("templates"):
        print("running sync...")
        sync_source = source or get_source(
            repos_for(cfg), env, cache=TreeCache(root / SYNC_CACHE_NAME)
        )
        sync(cfg, root=root, source=sync_source)

    setup_tasks = resolve_command("setup", cfg.commands, platform())
    if setup_tasks:
        print("running setup...")
        runner = runner or Runner()
        for spec in setup_tasks:
            dispatch(spec, cfg, root=root, runner=runner)


# --- self-update ---

def self_update(*, script_path: Path, source: Source) -> None:
    try:
        new_content = source.read(SELF_UPDATE_PATH)
    except ProjectError as e:
        print(f"Failed to check for updates: {e}", file=sys.stderr)
        return
    old_content = script_path.read_bytes()
    if new_content == old_content:
        print("Already up to date.")
        return
    script_path.write_bytes(new_content)
    print(f"Updated {script_path} (from {source.name})")


# --- CLI ---

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="project.py", description="one file to rule the repo")
    parser.add_argument("command", help="command name (init, setup, lint, build, self-update, ...)")
    parser.add_argument("args", nargs=argparse.REMAINDER, help="forwarded to tasks via cfg.args")
    parser.add_argument("--version", action="version", version=__version__)
    return parser


_COMMAND_HELP = {
    "init": """\
usage: project.py init [<preset>]

Write a starter project.toml in the current directory.

Without an argument, writes a default skeleton with commented-out
examples of [commands] and per-task config sections.

With <preset>, fetches presets/<preset>.toml from the project.py
repo on GitHub, prepends a [project] section using the current
directory name, writes the result, and then chains:
  - sync, if the preset defines [sync].templates
  - the `setup` command, if the preset defines one
…so a single `init <preset>` fully bootstraps the repo. Requires
GH_TOKEN.

Refuses to overwrite an existing project.toml.""",
    "sync": """\
usage: project.py sync

Pull template files from the project.py repo's templates/ folder
into the current project, according to [sync].templates in
project.toml.

File categories within each template folder:
  <name>/<rest>                 managed (overwrite-on-change)
  <name>/_write_once_/<rest>    scaffold (write only if absent)
  <name>/_append_/<rest>        block-merged into <rest>

Templates are pulled from the github repos in [sources].repos (default:
the project.py repo), searched in order — first repo with templates/<name>/
wins. Set PROJECT_PY_PATH to local folder(s) to search them first, $PATH
style, so a local checkout shadows a repo's template without editing
project.toml.

State is tracked in .project-sync.lock — commit it to git so
deletions and append-block changes propagate across machines.

Requires GH_TOKEN (unless every template resolves from PROJECT_PY_PATH).""",
    "self-update": """\
usage: project.py self-update

Replace project.py in the current directory with the latest version
from the project.py repo on GitHub. No-op if already up to date.

GH_TOKEN is optional but recommended — avoids unauthenticated
GitHub rate limits.""",
}


def main(
    argv: list[str] | None = None,
    *,
    root: Path | None = None,
    source: Source | None = None,
    runner: Runner | None = None,
    env: dict | None = None,
    script_path: Path | None = None,
) -> int:
    # `argparse.REMAINDER` swallows `-h` / `--help` / `--version` when they appear
    # after the command. Intercept those before argparse so help and version work
    # no matter where they're typed.
    raw = list(sys.argv[1:] if argv is None else argv)

    if raw and raw[0] in _COMMAND_HELP and ("-h" in raw[1:] or "--help" in raw[1:]):
        print(_COMMAND_HELP[raw[0]])
        return 0
    if not raw or raw[0] in ("-h", "--help"):
        build_parser().print_help()
        return 0
    if raw[0] == "--version":
        print(__version__)
        return 0

    ns = build_parser().parse_args(raw)

    root = root or Path(__file__).resolve().parent
    runner = runner or Runner()

    try:
        if ns.command == "self-update":
            # project.py itself isn't a template, so it rides the default search path:
            # local checkout on PROJECT_PY_PATH if present, else the BuildWithCollab repo.
            src = source if source is not None else get_source(DEFAULT_REPOS, env)
            self_update(script_path=script_path or Path(__file__).resolve(), source=src)
            return 0

        if ns.command == "init":
            preset = ns.args[0] if ns.args else None
            # Don't pre-build a source here: init reads the preset off the default path,
            # then builds the sync source from the preset's OWN repos. Pass env through.
            init(root, preset, source=source, runner=runner, env=env)
            return 0

        cfg = Config.load(root / TOML_NAME)
        cfg.args = ns.args

        if ns.command == "sync":
            src = source if source is not None else get_source(
                repos_for(cfg), env, cache=TreeCache(root / SYNC_CACHE_NAME)
            )
            sync(cfg, root=root, source=src)
            return 0

        tasks = resolve_command(ns.command, cfg.commands, platform())
        if not tasks:
            print(f"no '{ns.command}' defined in [commands]", file=sys.stderr)
            return 2
        for spec in tasks:
            dispatch(spec, cfg, root=root, runner=runner)
        return 0
    except ProjectError as e:
        print(e, file=sys.stderr)
        return e.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
