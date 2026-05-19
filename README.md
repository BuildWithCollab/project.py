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

The `:` is only needed when referencing something outside `project.py` — it separates the module path from the attribute.

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

`sync` requires `GH_TOKEN` set to a GitHub PAT (read-only public-repo access is enough). Without it, you'd hit GitHub's 60 req/hour unauthenticated rate limit immediately.

---

## Commands

| Command          | What it does                                            |
| ---------------- | ------------------------------------------------------- |
| `init`           | Write a starter `project.toml` (refuses to overwrite).  |
| `self-update`    | Pull latest `project.py` from this repo.                |
| `sync`           | Pull template files from this repo into your project.   |
| `<your command>` | Whatever you defined under `[commands]` in your toml.   |
| `--help`         | argparse help.                                          |
| `--version`      | Print version.                                          |

Extra args after the command get forwarded to tasks as `cfg.args`:

```bash
python project.py lint --fix         # cfg.args == ["--fix"]
```
