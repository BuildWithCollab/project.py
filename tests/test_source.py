from assertpy import assert_that

from project import GitHubSource, LocalSource, ProjectError, get_source, git_blob_sha


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
    def test_default_is_github(self):
        assert_that(get_source({"GH_TOKEN": "x"})).is_instance_of(GitHubSource)

    def test_local_when_env_set(self, tmp_path):
        assert_that(get_source({"PROJECT_PY_SOURCE": str(tmp_path)})).is_instance_of(LocalSource)

    def test_local_bad_dir_raises(self):
        assert_that(get_source).raises(ProjectError).when_called_with(
            {"PROJECT_PY_SOURCE": "/no/such/dir/xyz123"}
        )

    def test_github_carries_token(self):
        assert_that(get_source({"GH_TOKEN": "tok"}).token).is_equal_to("tok")


class TestGitHubSourceReady:
    def test_no_token_not_ready(self):
        assert_that(GitHubSource(None).ensure_ready).raises(ProjectError).when_called_with()

    def test_token_ready(self):
        GitHubSource("tok").ensure_ready()  # does not raise
