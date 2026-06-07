from assertpy import assert_that

from project import ProjectError, init
from tests._fakes import DictSource, RecordingRunner


class TestInitPlain:
    def test_writes_skeleton(self, tmp_path):
        init(tmp_path, None)
        toml = (tmp_path / "project.toml").read_text()
        assert_that(toml).contains("[project]")
        assert_that(toml).contains(f'name = "{tmp_path.name}"')
        assert_that(toml).contains("[commands]")

    def test_refuses_overwrite(self, tmp_path):
        (tmp_path / "project.toml").write_text("x\n")
        assert_that(init).raises(ProjectError).when_called_with(tmp_path, None)


class TestInitPreset:
    def test_prepends_project_and_chains_sync_and_setup(self, tmp_path):
        src = DictSource(
            {
                "presets/cpp.toml": b'[commands]\nsetup = ["xmake_config"]\n\n[sync]\ntemplates = ["git"]\n',
                "templates/git/.gitattributes": b"* text=auto\n",
            }
        )
        runner = RecordingRunner()
        init(tmp_path, "cpp", source=src, runner=runner)

        toml = (tmp_path / "project.toml").read_text()
        assert_that(toml).starts_with("[project]")
        assert_that(toml).contains(f'name = "{tmp_path.name}"')
        assert_that(toml).contains("[sync]")
        # sync chained -> template file present
        assert_that((tmp_path / ".gitattributes").exists()).is_true()
        # setup chained -> runner saw `xmake config`
        assert_that([c.cmd for c in runner.calls]).contains(["xmake", "config"])

    def test_chained_sync_honors_preset_repos_not_default(self, tmp_path):
        # The preset (read off the local path) declares repos = [] — local-only — and asks
        # for a template that is NOT on the path. Fixed wiring builds the chained-sync
        # source from the preset's OWN repos ([] -> no github), so the missing template is
        # just a warning. The old bug reused the DEFAULT github source, which would fall
        # through to BuildWithCollab and raise on the missing GH_TOKEN. No token set here,
        # so reaching github at all would fail the test.
        pathsrc = tmp_path / "pathsrc"
        (pathsrc / "presets").mkdir(parents=True)
        (pathsrc / "presets" / "x.toml").write_bytes(
            b'[sources]\nrepos = []\n\n[sync]\ntemplates = ["ghost"]\n'
        )
        proj = tmp_path / "proj"
        proj.mkdir()
        init(proj, "x", env={"PROJECT_PY_PATH": str(pathsrc)})  # must not raise
        assert_that((proj / "project.toml").read_text()).contains('templates = ["ghost"]')

    def test_preset_without_sync_or_setup(self, tmp_path):
        src = DictSource({"presets/min.toml": b'[commands]\nbuild = ["xmake_build"]\n'})
        runner = RecordingRunner()
        init(tmp_path, "min", source=src, runner=runner)
        assert_that((tmp_path / "project.toml").exists()).is_true()
        assert_that(runner.calls).is_empty()
