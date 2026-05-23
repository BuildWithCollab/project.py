# project.py
🛠️ Project Tools 🛠️

A single-file, zero-dependency (Python 3.11+ stdlib only) per-repo CLI runner. Drop `project.py` into the root of any repo, pair it with a `project.toml`, and it dispatches commands like `setup` / `lint` / `build` to built-in tasks or repo-local scripts.

---

## Install

Copy `project.py` into the root of a repo:

```bash
curl -O https://raw.githubusercontent.com/BuildWithCollab/project.py/main/project.py
```

Then generate a starter `project.toml`:

```bash
python project.py init
```

To pull the latest `project.py` later:

```bash
python project.py self-update
```

> `self-update` uses the GitHub Contents API. Set `GH_TOKEN` if you hit rate limits.

---

## How it works

`project.toml` defines named commands. Each command is a list of task references. Run any command with `python project.py <name>`.

```toml
[project]
name = "my-cpp-thing"

[commands]
setup = ["xmake_config"]
build = ["xmake_build"]
lint  = ["clang_tidy"]

[clang_tidy]
binary = "clang-tidy-21"
jobs = 16
```

```
$ python project.py setup
$ xmake config

$ python project.py lint
checking 42 files across 16 workers
...
```

### Task references

A task reference is either:

| Form                                   | Meaning                                                                |
| -------------------------------------- | ---------------------------------------------------------------------- |
| `clang_tidy`                           | A built-in function (top-level function in `project.py`).              |
| `scripts.somethingcustom:do_custom`    | A function in a repo-local Python file. Dotted path → file, `:` → attr. |
| `$rm -rf build/` or `$ rm -rf build/`  | A shell one-liner. The space after `$` is optional. Runs through the OS shell (`cmd.exe` on Windows, `/bin/sh` elsewhere). |

The `:` is only needed when referencing something outside `project.py` — it separates the module path from the attribute. The `$` prefix marks a shell command — the rest of the string is passed to the OS shell as-is.

### Platform-specific commands

Suffix a command name with `:windows`, `:macos`, or `:linux` to override its tasks on that platform. The platform-specific entry wins if it exists; otherwise sync falls back to the plain name.

**TOML gotcha:** bare keys in TOML can't contain `:` — quote the key as a string.

```toml
[commands]
lint           = ["clang_tidy"]                  # default for any OS not covered below
"lint:macos"   = ["scripts.lint:brew_tidy"]
"lint:windows" = ["scripts.lint:choco_tidy"]
```

On macOS, `python project.py lint` runs `scripts.lint:brew_tidy`. On Linux, it falls back to `clang_tidy`. The fallback chain is `<name>:<current-platform>` first, then `<name>`.

Pairs naturally with shell commands when the OS shell differs:

```toml
[commands]
clean           = ["$rm -rf build/"]
"clean:windows" = ["$rmdir /s /q build"]
```

---

## Built-in tasks

**Config contract:** a task named `X` reads its config from `[X]` in `project.toml`. Want to grep for what feeds a task? The section name is the task name.

| Task             | Runs                                                       | Optional `[X]` keys                                |
| ---------------- | ---------------------------------------------------------- | -------------------------------------------------- |
| `clang_tidy`     | Two-pass clang-tidy (parallel check, serial `-fix-errors`) | `binary`, `jobs`, `fix`                            |
| `xmake_config`   | `xmake config`                                             | —                                                  |
| `xmake_build`    | `xmake build`                                              | —                                                  |
| `npm_install`    | `<package_manager> install`                                | `package_manager`                                  |
| `eslint`         | `npx eslint .`                                             | —                                                  |
| `ruff`           | `ruff check .`                                             | —                                                  |

### `[clang_tidy]` keys

| Key       | Default          | Notes                                              |
| --------- | ---------------- | -------------------------------------------------- |
| `binary`  | `clang-tidy`     | e.g. `clang-tidy-21` on Ubuntu.                    |
| `jobs`    | `os.cpu_count()` | Parallel workers for the check pass.               |
| `fix`     | `true`           | Run the serial `-fix-errors` pass after checking.  |

### `[npm_install]` keys

| Key                | Default | Notes                              |
| ------------------ | ------- | ---------------------------------- |
| `package_manager`  | `npm`   | e.g. `pnpm`, `yarn`, `bun`.        |

---

## Custom scripts

Drop a Python file anywhere under the repo (convention: `./scripts/`) and reference it from `project.toml` using the `module.path:attr` form:

```
my-repo/
├── project.py
├── project.toml
└── scripts/
    ├── repochecks.py
    └── deploy/
        └── staging.py
```

```toml
[commands]
lint   = ["clang_tidy", "scripts.repochecks:run"]
deploy = ["scripts.deploy.staging:go"]
```

Each task function takes one argument: the `Config` instance. By convention, a custom task reads its own config from `cfg.tools["section-name"]`, where the section name is whatever you put in `project.toml` — typically matching the function name.

```python
# scripts/repochecks.py
from project import Config, run

def check(cfg: Config) -> None:
    opts = cfg.tools.get("repochecks", {})
    timeout = opts.get("timeout", 30)
    run(["echo", f"checking with timeout={timeout}"])
```

```toml
[commands]
lint = ["clang_tidy", "scripts.repochecks:check"]

[repochecks]
timeout = 60
```

Helpers available to import from `project`:

- `run(cmd, *, check=True, **kw)` — friendly default for one-shot subprocess calls (prints the command, then `subprocess.run` with `check=True`).
- `xmake(*args, **kw)` — shorthand for `run(["xmake", *args])`.
- `platform() -> Platform` — returns `Platform.WINDOWS` / `Platform.LINUX` / `Platform.MAC`.
- `Config` — the typed config dataclass. Custom scripts will mostly read `cfg.tools["your-section"]`.

For anything beyond friendly single-shot subprocess (output parsing, parallelism, batch work), use `subprocess.run` / `ThreadPoolExecutor` directly. `run()` is the simple default, not a Swiss Army knife.

---

## Syncing templates from GitHub

`sync` pulls files from the `templates/<name>/` folders in this repo into your project.

```toml
[sync]
templates = ["python-base", "github-actions", "cpp/xmake"]
```

```bash
GH_TOKEN=ghp_xxx python project.py sync
```

What it does:

- Each named template is a folder under `templates/` in `BuildWithCollab/project.py`. Every file in that folder gets copied into your repo at the same relative path. So `templates/python-base/.gitignore` lands at `./.gitignore`; `templates/github-actions/.github/workflows/ci.yml` lands at `./.github/workflows/ci.yml`.
- Template names can be nested: `"cpp/xmake"` pulls everything under `templates/cpp/xmake/`. Organize templates into subfolders however you like.
- Templates compose in order. If two templates ship the same file, the later one in the list wins. If you list overlapping prefixes like `["cpp", "cpp/xmake"]`, the most specific one claims its subtree.
- Only files whose content actually changed get re-downloaded. `sync` lists the whole tree in one API call, compares each blob's git sha to `.project-sync.lock`, and skips anything unchanged.
- Files that were in the previous sync but are no longer in any listed template get deleted.
- `.project-sync.lock` is written at the repo root after each sync. **Commit it to git** so deletions propagate across machines and CI.

### Write-once files (`_write_once_/`)

Sometimes a template ships a file you only want as a starter — `xmake.lua`, an initial config, a stub — and after the first sync the consumer takes it over. The template author declares this by putting those files under a `_write_once_/` subfolder inside the template:

```
templates/cpp/xmake/
├── clang_tidy.config          ← managed (overwritten on change)
└── _write_once_/
    └── xmake.lua              ← seeded once, then left alone forever
```

Behavior of files under `_write_once_/`:

- The `_write_once_/` segment is stripped from the destination path: the file above lands at `./xmake.lua`, not `./_write_once_/xmake.lua`.
- On first sync (file absent locally): written. On every sync after that (file exists): left alone, untouched.
- Never tracked in `.project-sync.lock`. Never deleted by sync. The consumer owns the file after the first sync.

`_write_once_` is a reserved folder name at the top level of any template — you can't ship a literal `./_write_once_/` directory into a consumer repo through sync.

### Append blocks (`_append_/`)

For files that need contributions from *multiple* templates plus user-specific lines — `.gitignore` is the poster child — put them under `_append_/`. Each contributing template's content is injected as a marker-delimited block. The user's own content lives anywhere outside the markers and is never touched.

```
templates/cpp/xmake/
└── _append_/
    └── .gitignore             ← contributes a block to ./.gitignore

templates/rust/
└── _append_/
    └── .gitignore             ← contributes another block to ./.gitignore
```

Result on the consumer's disk (with `templates = ["cpp/xmake", "rust"]`):

```gitignore
# user's own lines (live anywhere outside markers)
secrets.toml
.env

# [START cpp/xmake]
build/
*.o
# [END cpp/xmake]

# [START rust]
target/
# [END rust]
```

Behavior of files under `_append_/`:

- Each contributing template gets its own `# [START <template>]` / `# [END <template>]` block in the destination file. Block name is the full template name.
- Block updates propagate: if a template changes its `_append_/.gitignore`, the matching block's content is replaced in place on the next sync.
- Block removals propagate: if a template stops shipping `_append_/.gitignore`, or is removed from `[sync].templates`, its block is stripped on the next sync.
- Content **outside** all marker blocks is the user's. Sync never touches it.
- Content **inside** a marker block is owned by the template. Local edits there get overwritten on the next sync — that's the deal.
- If a file's last managed block is stripped and only user content remains, the file is kept. If nothing remains, the file is deleted.

The append paths are tracked in `.project-sync.lock` under an `[append]` section so blocks can be stripped cleanly even when a template gets removed from the list entirely.

Marker format uses `#` as the comment character, which works for `.gitignore`, `.gitattributes`, `.editorconfig`, dotenv files, requirements.txt, and most line-oriented config formats. Files that use a different comment syntax (JSON, HTML, etc.) aren't supported.

`sync` requires `GH_TOKEN` set to a GitHub PAT (read-only public-repo access is enough). Without it, you'd hit GitHub's 60 req/hour unauthenticated rate limit immediately.

---

## Commands

| Command          | What it does                                            |
| ---------------- | ------------------------------------------------------- |
| `init`           | Write a starter `project.toml` (refuses to overwrite).  |
| `init <preset>`  | Write a `project.toml` pre-configured from a preset in this repo, then auto-run `sync`. |
| `self-update`    | Pull latest `project.py` from this repo.                |
| `sync`           | Pull template files from this repo into your project.   |
| `<your command>` | Whatever you defined under `[commands]` in your toml.   |
| `--help`         | argparse help.                                          |
| `--version`      | Print version.                                          |

Extra args after the command get forwarded to tasks as `cfg.args`:

```bash
python project.py lint --fix         # cfg.args == ["--fix"]
```

---

## Presets — bootstrap a new repo in one command

`init <preset>` fetches a pre-configured starter from `presets/<name>.toml` in this repo, writes it to your `project.toml`, and then auto-runs `sync` so your template files land immediately.

```bash
GH_TOKEN=ghp_xxx python project.py init cpp
```

After that one command, the consumer's repo has:

- A `project.toml` with `[commands]`, `[sync]`, and any per-task config sections already filled in.
- All template files from `[sync].templates` already synced in (including any `_write_once_/` scaffolds).
- A `.project-sync.lock` ready to commit.

### Writing a preset

A preset file lives at `presets/<name>.toml` in this repo. It contains **everything except the `[project]` section** — `init` prepends `[project]` itself using the consumer's repo folder name, so the preset stays drift-free.

```toml
# presets/cpp.toml
[commands]
setup = ["xmake_config"]
build = ["xmake_build"]
lint  = ["clang_tidy"]

[sync]
templates = ["cpp/xmake", "git"]

[clang_tidy]
binary = "clang-tidy-21"
```

After `python project.py init cpp` in a folder called `my-game/`, the resulting `project.toml` is:

```toml
[project]
name = "my-game"

[commands]
setup = ["xmake_config"]
build = ["xmake_build"]
lint  = ["clang_tidy"]

[sync]
templates = ["cpp/xmake", "git"]

[clang_tidy]
binary = "clang-tidy-21"
```

…and sync runs automatically.

Rules:

- **Don't include `[project]` in a preset** — `init` writes that section itself.
- **Don't put `sync` inside `[commands]`** — sync is its own top-level command, and `init` auto-runs it once for you. Commands like `setup`/`build` are expected to run against the files sync already put in place.
- `init <preset>` requires `GH_TOKEN` (it makes a GitHub API call). Plain `init` does not.
