from assertpy import assert_that

from project import Config, ProjectError, parse_lock, sync
from tests._fakes import DictSource


def cfg_with(templates, **project):
    return Config(project=project, tools={"sync": {"templates": list(templates)}})


class TestSyncManaged:
    def test_writes_managed_file(self, tmp_path):
        src = DictSource({"templates/git/.gitattributes": b"* text=auto\n"})
        sync(cfg_with(["git"]), root=tmp_path, source=src)
        assert_that((tmp_path / ".gitattributes").read_bytes()).is_equal_to(b"* text=auto\n")

    def test_writes_lock_with_managed_entry(self, tmp_path):
        src = DictSource({"templates/git/.gitattributes": b"x\n"})
        sync(cfg_with(["git"]), root=tmp_path, source=src)
        lock = tmp_path / ".project-sync.lock"
        assert_that(lock.exists()).is_true()
        _, managed, _ = parse_lock(lock.read_text())
        assert_that(managed).contains_key(".gitattributes")

    def test_unchanged_skipped_on_second_run(self, tmp_path, capsys):
        src = DictSource({"templates/git/.gitattributes": b"x\n"})
        sync(cfg_with(["git"]), root=tmp_path, source=src)
        capsys.readouterr()
        sync(cfg_with(["git"]), root=tmp_path, source=src)
        assert_that(capsys.readouterr().out).contains("1 unchanged")

    def test_deletes_dropped_managed(self, tmp_path):
        sync(cfg_with(["git"]), root=tmp_path, source=DictSource({"templates/git/.gitattributes": b"x\n"}))
        assert_that((tmp_path / ".gitattributes").exists()).is_true()
        sync(cfg_with(["other"]), root=tmp_path, source=DictSource({"templates/other/keep.txt": b"y\n"}))
        assert_that((tmp_path / ".gitattributes").exists()).is_false()
        assert_that((tmp_path / "keep.txt").exists()).is_true()

    def test_nested_path_written(self, tmp_path):
        # subdir lives inside the template -> it's recreated under the repo root
        src = DictSource({"templates/cpp/xmake/targets.lua": b"-- targets\n"})
        sync(cfg_with(["cpp"]), root=tmp_path, source=src)
        assert_that((tmp_path / "xmake" / "targets.lua").read_text()).is_equal_to("-- targets\n")


class TestSyncSubstitution:
    def test_substitutes_managed(self, tmp_path):
        src = DictSource({"templates/cpp/x.lua": b'name = "{{project.name}}"\n'})
        sync(cfg_with(["cpp"], name="my-game"), root=tmp_path, source=src)
        assert_that((tmp_path / "x.lua").read_text()).is_equal_to('name = "my-game"\n')

    def test_cfg_change_rerenders(self, tmp_path, capsys):
        src = DictSource({"templates/cpp/x.lua": b"{{project.name}}\n"})
        sync(cfg_with(["cpp"], name="one"), root=tmp_path, source=src)
        capsys.readouterr()
        sync(cfg_with(["cpp"], name="two"), root=tmp_path, source=src)
        assert_that((tmp_path / "x.lua").read_text()).is_equal_to("two\n")


class TestSyncWriteOnce:
    def test_seeds_then_preserves(self, tmp_path, capsys):
        src = DictSource({"templates/cpp/_write_once_/xmake.lua": b"seed\n"})
        sync(cfg_with(["cpp"]), root=tmp_path, source=src)
        assert_that((tmp_path / "xmake.lua").read_text()).is_equal_to("seed\n")
        (tmp_path / "xmake.lua").write_text("user edited\n")
        sync(cfg_with(["cpp"]), root=tmp_path, source=src)
        assert_that((tmp_path / "xmake.lua").read_text()).is_equal_to("user edited\n")

    def test_write_once_not_tracked_in_lock(self, tmp_path):
        src = DictSource({"templates/cpp/_write_once_/xmake.lua": b"seed\n"})
        sync(cfg_with(["cpp"]), root=tmp_path, source=src)
        assert_that((tmp_path / ".project-sync.lock").read_text()).does_not_contain("xmake.lua")


class TestSyncAppend:
    def test_merges_block(self, tmp_path):
        src = DictSource({"templates/git/_append_/.gitignore": b"build/\n"})
        sync(cfg_with(["git"]), root=tmp_path, source=src)
        assert_that((tmp_path / ".gitignore").read_text()).contains(
            "# [START git]\nbuild/\n# [END git]"
        )

    def test_preserves_user_lines(self, tmp_path):
        (tmp_path / ".gitignore").write_text("secrets.env\n")
        src = DictSource({"templates/git/_append_/.gitignore": b"build/\n"})
        sync(cfg_with(["git"]), root=tmp_path, source=src)
        text = (tmp_path / ".gitignore").read_text()
        assert_that(text).contains("secrets.env")
        assert_that(text).contains("# [START git]")

    def test_unmerge_deletes_when_only_block(self, tmp_path):
        sync(cfg_with(["git"]), root=tmp_path, source=DictSource({"templates/git/_append_/.gitignore": b"build/\n"}))
        sync(cfg_with(["other"]), root=tmp_path, source=DictSource({"templates/other/keep.txt": b"k\n"}))
        assert_that((tmp_path / ".gitignore").exists()).is_false()

    def test_unmerge_keeps_user_content(self, tmp_path):
        (tmp_path / ".gitignore").write_text("mine.txt\n")
        sync(cfg_with(["git"]), root=tmp_path, source=DictSource({"templates/git/_append_/.gitignore": b"build/\n"}))
        sync(cfg_with(["other"]), root=tmp_path, source=DictSource({"templates/other/keep.txt": b"k\n"}))
        text = (tmp_path / ".gitignore").read_text()
        assert_that(text).contains("mine.txt")
        assert_that(text).does_not_contain("START")


class TestSyncErrors:
    def test_no_templates_raises(self, tmp_path):
        cfg = Config(tools={"sync": {"templates": []}})
        assert_that(sync).raises(ProjectError).when_called_with(cfg, root=tmp_path, source=DictSource({}))

    def test_source_not_ready_raises(self, tmp_path):
        src = DictSource({"templates/git/x": b"y"}, ready=False)
        assert_that(sync).raises(ProjectError).when_called_with(cfg_with(["git"]), root=tmp_path, source=src)
