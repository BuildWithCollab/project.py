"""In-memory test doubles. Real subclasses / duck-types — not mocks, no patching.

- `DictSource` is a `Source` backed by an in-memory {path: bytes} map. It mirrors the
  real sources' semantics: `list_blobs` only surfaces `templates/` paths and computes the
  same git blob sha; `read`/`blob` return LF-normalized bytes.
- `RecordingRunner` duck-types `Runner`: it records every call and returns canned stdout
  for `capture=True` based on substring matches, so dispatch and the built-in tasks run
  without spawning processes.
"""
from types import SimpleNamespace

from project import (
    ProjectError,
    Source,
    TEMPLATES_DIR,
    git_blob_sha,
    git_normalize,
)


class DictSource(Source):
    name = "dict"

    def __init__(self, files, *, ready=True):
        self.files = {
            p: (b if isinstance(b, (bytes, bytearray)) else b.encode("utf-8"))
            for p, b in files.items()
        }
        self._ready = ready
        self._by_sha = {}

    def ensure_ready(self):
        if not self._ready:
            raise ProjectError("dict source not ready")

    def read(self, path):
        if path not in self.files:
            raise ProjectError(f"not found in dict source: {path}")
        return git_normalize(self.files[path])

    def list_blobs(self, wanted=None):
        self._by_sha = {}
        out = []
        prefix = f"{TEMPLATES_DIR}/"
        for path in sorted(self.files):
            if not path.startswith(prefix):
                continue
            norm = git_normalize(self.files[path])
            sha = git_blob_sha(norm)
            out.append((path, sha))
            self._by_sha[sha] = norm
        return out

    def blob(self, sha):
        if sha not in self._by_sha:
            raise ProjectError(f"no blob for sha {sha}")
        return self._by_sha[sha]


class RecordingRunner:
    def __init__(self, outputs=None):
        self.calls = []
        self.outputs = outputs or {}

    def run(self, cmd, *, shell=False, check=True, capture=False):
        joined = cmd if isinstance(cmd, str) else " ".join(str(a) for a in cmd)
        self.calls.append(
            SimpleNamespace(cmd=cmd, joined=joined, shell=shell, check=check, capture=capture)
        )
        stdout = ""
        for key, val in self.outputs.items():
            if key in joined:
                stdout = val
                break
        return SimpleNamespace(returncode=0, stdout=stdout)
