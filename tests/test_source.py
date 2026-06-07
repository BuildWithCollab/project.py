from assertpy import assert_that

from project import (
    DEFAULT_REPOS,
    Config,
    GitHubSource,
    LocalSource,
    ProjectError,
    SearchPathSource,
    get_source,
    git_blob_sha,
    repos_for,
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
        blobs = sp.list_blobs(["cpp"])
        assert_that([p for p, _ in blobs]).is_equal_to(["templates/cpp/x.lua"])
        assert_that(sp.blob(blobs[0][1])).is_equal_to(b"from-a\n")

    def test_distinct_templates_merge(self):
        a = DictSource({"templates/cpp/x.lua": b"a\n"})
        b = DictSource({"templates/git/.gitignore": b"build/\n"})
        sp = SearchPathSource([a, b])
        paths = sorted(p for p, _ in sp.list_blobs(["cpp", "git"]))
        assert_that(paths).is_equal_to(["templates/cpp/x.lua", "templates/git/.gitignore"])

    def test_later_source_fills_gap(self):
        # "cpp" only in the second source -> it's still picked up.
        a = DictSource({"templates/git/.gitignore": b"x\n"})
        b = DictSource({"templates/cpp/x.lua": b"y\n"})
        sp = SearchPathSource([a, b])
        cpp = [(p, s) for p, s in sp.list_blobs(["cpp", "git"]) if p == "templates/cpp/x.lua"]
        assert_that(cpp).is_length(1)
        assert_that(sp.blob(cpp[0][1])).is_equal_to(b"y\n")

    def test_nested_siblings_dont_collide_across_sources(self):
        # cpp/base from A and cpp/xmake from B must BOTH survive — they're different
        # templates that merely share a top-level dir. (The old top-dir keying broke this.)
        a = DictSource({"templates/cpp/base/a.lua": b"base\n"})
        b = DictSource({"templates/cpp/xmake/x.lua": b"xmake\n"})
        sp = SearchPathSource([a, b])
        paths = sorted(p for p, _ in sp.list_blobs(["cpp/base", "cpp/xmake"]))
        assert_that(paths).is_equal_to(
            ["templates/cpp/base/a.lua", "templates/cpp/xmake/x.lua"]
        )

    def test_unneeded_unready_child_is_never_consulted(self):
        # The whole point: local covers every wanted template, so the not-ready github
        # stand-in is never touched and never demands a token.
        local = DictSource({"templates/cpp/x.lua": b"local\n"})
        github = DictSource({"templates/cpp/x.lua": b"remote\n"}, ready=False)
        sp = SearchPathSource([local, github])
        blobs = sp.list_blobs(["cpp"])  # does not raise
        assert_that(sp.blob(blobs[0][1])).is_equal_to(b"local\n")

    def test_needed_unready_child_still_raises(self):
        # cpp is only in the not-ready source -> we must reach it, so it raises.
        local = DictSource({"templates/git/.gitignore": b"x\n"})
        github = DictSource({"templates/cpp/x.lua": b"y\n"}, ready=False)
        sp = SearchPathSource([local, github])
        assert_that(sp.list_blobs).raises(ProjectError).when_called_with(["cpp"])

    def test_read_falls_through(self):
        a = DictSource({"presets/cpp.toml": b"[a]\n"})
        b = DictSource({"presets/other.toml": b"[b]\n"})
        sp = SearchPathSource([a, b])
        assert_that(sp.read("presets/other.toml")).is_equal_to(b"[b]\n")

    def test_read_missing_everywhere_raises(self):
        sp = SearchPathSource([DictSource({}), DictSource({})])
        assert_that(sp.read).raises(ProjectError).when_called_with("nope.toml")

    def test_ensure_ready_is_lazy_noop(self):
        # ensure_ready no longer eagerly checks children — readiness is enforced only
        # when list_blobs actually reaches a child.
        sp = SearchPathSource([DictSource({}, ready=True), DictSource({}, ready=False)])
        sp.ensure_ready()  # does not raise

    def test_no_hint_returns_full_merged_tree(self):
        # Source contract: a composite given no `wanted` hint returns everything, first
        # source winning per top-level template. (Exercises the wanted=None branch.)
        a = DictSource({"templates/cpp/x.lua": b"a\n"})
        b = DictSource({"templates/cpp/x.lua": b"b\n", "templates/git/.gitignore": b"g\n"})
        sp = SearchPathSource([a, b])
        blobs = dict(sp.list_blobs())
        assert_that(sorted(blobs)).is_equal_to(
            ["templates/cpp/x.lua", "templates/git/.gitignore"]
        )
        assert_that(sp.blob(blobs["templates/cpp/x.lua"])).is_equal_to(b"a\n")


class TestReposFor:
    def test_default_when_absent(self):
        assert_that(repos_for(Config())).is_equal_to(DEFAULT_REPOS)

    def test_does_not_alias_the_default(self):
        # Mutating the returned list must not corrupt the module-level DEFAULT_REPOS.
        repos_for(Config()).append("junk/repo")
        assert_that(DEFAULT_REPOS).does_not_contain("junk/repo")

    def test_string_coerced_to_single_repo_list(self):
        cfg = Config(tools={"sources": {"repos": "owner/repo"}})
        assert_that(repos_for(cfg)).is_equal_to(["owner/repo"])

    def test_list_passthrough(self):
        cfg = Config(tools={"sources": {"repos": ["a/b", "c/d"]}})
        assert_that(repos_for(cfg)).is_equal_to(["a/b", "c/d"])


class TestGitHubSourceReady:
    def test_no_token_not_ready(self):
        assert_that(GitHubSource(None).ensure_ready).raises(ProjectError).when_called_with()

    def test_token_ready(self):
        GitHubSource("tok").ensure_ready()  # does not raise
