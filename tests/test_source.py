from assertpy import assert_that

from project import (
    GitHubSource,
    LocalSource,
    ProjectError,
    SearchPathSource,
    get_source,
    git_blob_sha,
)
from tests._fakes import DictSource


def _checkout(root):
    (root / "templates" / "git").mkdir(parents=True)
    (root / "templates" / "git" / ".gitattributes").write_bytes(b"* text=auto\n")
    (root / "presets").mkdir()
    (root / "presets" / "cpp.toml").write_bytes(b"[sync]\n")
    return root


class TestLocalSource:
    def test_read(self, tmp_path):
        src = LocalSource(_checkout(tmp_path))
        assert_that(src.read("presets/cpp.toml")).is_equal_to(b"[sync]\n")

    def test_read_missing_raises(self, tmp_path):
        src = LocalSource(_checkout(tmp_path))
        assert_that(src.read).raises(ProjectError).when_called_with("nope.txt")

    def test_read_normalizes_crlf(self, tmp_path):
        (tmp_path / "presets").mkdir()
        (tmp_path / "presets" / "x.toml").write_bytes(b"a\r\nb\r\n")
        assert_that(LocalSource(tmp_path).read("presets/x.toml")).is_equal_to(b"a\nb\n")

    def test_list_blobs_only_templates(self, tmp_path):
        blobs = LocalSource(_checkout(tmp_path)).list_blobs()
        assert_that([p for p, _ in blobs]).is_equal_to(["templates/git/.gitattributes"])

    def test_list_blobs_sha_matches_git(self, tmp_path):
        blobs = dict(LocalSource(_checkout(tmp_path)).list_blobs())
        assert_that(blobs["templates/git/.gitattributes"]).is_equal_to(git_blob_sha(b"* text=auto\n"))

    def test_blob_round_trip(self, tmp_path):
        src = LocalSource(_checkout(tmp_path))
        sha = src.list_blobs()[0][1]
        assert_that(src.blob(sha)).is_equal_to(b"* text=auto\n")

    def test_blob_before_list_raises(self, tmp_path):
        src = LocalSource(_checkout(tmp_path))
        assert_that(src.blob).raises(ProjectError).when_called_with("deadbeef")

    def test_no_templates_dir_is_empty(self, tmp_path):
        assert_that(LocalSource(tmp_path).list_blobs()).is_equal_to([])


class TestGetSource:
    def test_single_repo_is_bare_github(self):
        src = get_source(["BuildWithCollab/project.py"], {"GH_TOKEN": "x"})
        assert_that(src).is_instance_of(GitHubSource)
        assert_that(src.repo).is_equal_to("BuildWithCollab/project.py")

    def test_multiple_repos_is_search_path(self):
        src = get_source(["a/b", "c/d"], {"GH_TOKEN": "x"})
        assert_that(src).is_instance_of(SearchPathSource)
        assert_that([c.repo for c in src.children]).is_equal_to(["a/b", "c/d"])

    def test_path_only_is_bare_local(self, tmp_path):
        assert_that(get_source([], {"PROJECT_PY_PATH": str(tmp_path)})).is_instance_of(LocalSource)

    def test_path_then_repo_is_search_path_local_first(self, tmp_path):
        src = get_source(["a/b"], {"PROJECT_PY_PATH": str(tmp_path), "GH_TOKEN": "x"})
        assert_that(src).is_instance_of(SearchPathSource)
        assert_that(src.children[0]).is_instance_of(LocalSource)
        assert_that(src.children[1]).is_instance_of(GitHubSource)

    def test_multiple_path_entries_split_on_pathsep(self, tmp_path):
        import os

        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        src = get_source([], {"PROJECT_PY_PATH": os.pathsep.join([str(a), str(b)])})
        assert_that(src).is_instance_of(SearchPathSource)
        assert_that([c.root for c in src.children]).is_equal_to([a, b])

    def test_no_sources_at_all_raises(self):
        assert_that(get_source).raises(ProjectError).when_called_with([], {})

    def test_path_bad_dir_raises(self):
        assert_that(get_source).raises(ProjectError).when_called_with(
            ["a/b"], {"PROJECT_PY_PATH": "/no/such/dir/xyz123"}
        )

    def test_github_carries_token(self):
        assert_that(get_source(["a/b"], {"GH_TOKEN": "tok"}).token).is_equal_to("tok")


class TestSearchPathSource:
    def test_first_source_owns_template(self):
        # Both provide template "cpp"; the earlier source wins for every file under it.
        a = DictSource({"templates/cpp/x.lua": b"from-a\n"})
        b = DictSource({"templates/cpp/x.lua": b"from-b\n"})
        sp = SearchPathSource([a, b])
        blobs = sp.list_blobs()
        assert_that([p for p, _ in blobs]).is_equal_to(["templates/cpp/x.lua"])
        sha = blobs[0][1]
        assert_that(sp.blob(sha)).is_equal_to(b"from-a\n")

    def test_distinct_templates_merge(self):
        a = DictSource({"templates/cpp/x.lua": b"a\n"})
        b = DictSource({"templates/git/.gitignore": b"build/\n"})
        sp = SearchPathSource([a, b])
        paths = sorted(p for p, _ in sp.list_blobs())
        assert_that(paths).is_equal_to(["templates/cpp/x.lua", "templates/git/.gitignore"])

    def test_later_source_fills_gap(self):
        # "cpp" only in the second source -> it's still picked up.
        a = DictSource({"templates/git/.gitignore": b"x\n"})
        b = DictSource({"templates/cpp/x.lua": b"y\n"})
        sp = SearchPathSource([a, b])
        cpp = [(p, s) for p, s in sp.list_blobs() if p == "templates/cpp/x.lua"]
        assert_that(cpp).is_length(1)
        assert_that(sp.blob(cpp[0][1])).is_equal_to(b"y\n")

    def test_shadowing_is_whole_template_not_per_file(self):
        # First source owns "cpp" via one file; second source's EXTRA cpp file is dropped.
        a = DictSource({"templates/cpp/a.lua": b"a\n"})
        b = DictSource({"templates/cpp/a.lua": b"b\n", "templates/cpp/b.lua": b"extra\n"})
        sp = SearchPathSource([a, b])
        paths = sorted(p for p, _ in sp.list_blobs())
        assert_that(paths).is_equal_to(["templates/cpp/a.lua"])

    def test_read_falls_through(self):
        a = DictSource({"presets/cpp.toml": b"[a]\n"})
        b = DictSource({"presets/other.toml": b"[b]\n"})
        sp = SearchPathSource([a, b])
        assert_that(sp.read("presets/other.toml")).is_equal_to(b"[b]\n")

    def test_read_missing_everywhere_raises(self):
        sp = SearchPathSource([DictSource({}), DictSource({})])
        assert_that(sp.read).raises(ProjectError).when_called_with("nope.toml")

    def test_ensure_ready_checks_all_children(self):
        sp = SearchPathSource([DictSource({}, ready=True), DictSource({}, ready=False)])
        assert_that(sp.ensure_ready).raises(ProjectError).when_called_with()


class TestGitHubSourceReady:
    def test_no_token_not_ready(self):
        assert_that(GitHubSource(None).ensure_ready).raises(ProjectError).when_called_with()

    def test_token_ready(self):
        GitHubSource("tok").ensure_ready()  # does not raise
