from assertpy import assert_that

from project import main
from tests._fakes import DictSource, RecordingRunner


class TestHelpVersion:
    def test_no_args_prints_help(self, capsys):
        assert_that(main([])).is_equal_to(0)
        assert_that(capsys.readouterr().out).contains("one file to rule the repo")

    def test_version_flag(self, capsys):
        assert_that(main(["--version"])).is_equal_to(0)
        assert_that(capsys.readouterr().out.strip()).is_not_empty()

    def test_per_command_help_after_command(self, capsys):
        assert_that(main(["sync", "--help"])).is_equal_to(0)
        assert_that(capsys.readouterr().out).contains("Pull template files")

    def test_init_help(self, capsys):
        main(["init", "-h"])
        assert_that(capsys.readouterr().out).contains("starter project.toml")


class TestCommandRouting:
    def test_unknown_command_returns_2(self, tmp_path, capsys):
        rc = main(["bogus"], root=tmp_path, runner=RecordingRunner())
        assert_that(rc).is_equal_to(2)
        assert_that(capsys.readouterr().err).contains("no 'bogus'")

    def test_runs_command_tasks(self, tmp_path):
        (tmp_path / "project.toml").write_text('[commands]\nbuild = ["xmake_build"]\n')
        runner = RecordingRunner()
        assert_that(main(["build"], root=tmp_path, runner=runner)).is_equal_to(0)
        assert_that([c.cmd for c in runner.calls]).contains(["xmake", "build"])

    def test_forwards_args(self, tmp_path):
        (tmp_path / "project.toml").write_text('[commands]\ngo = ["cli_argmod:run"]\n')
        (tmp_path / "cli_argmod.py").write_text("seen = []\ndef run(cfg):\n    seen.append(cfg.args)\n")
        main(["go", "--flag", "x"], root=tmp_path, runner=RecordingRunner())
        import cli_argmod

        assert_that(cli_argmod.seen[0]).is_equal_to(["--flag", "x"])

    def test_sync_via_main(self, tmp_path):
        (tmp_path / "project.toml").write_text('[sync]\ntemplates = ["git"]\n')
        src = DictSource({"templates/git/.gitattributes": b"x\n"})
        assert_that(main(["sync"], root=tmp_path, source=src)).is_equal_to(0)
        assert_that((tmp_path / ".gitattributes").exists()).is_true()

    def test_init_via_main(self, tmp_path):
        assert_that(main(["init"], root=tmp_path)).is_equal_to(0)
        assert_that((tmp_path / "project.toml").exists()).is_true()

    def test_projecterror_maps_to_exit_code(self, tmp_path, capsys):
        (tmp_path / "project.toml").write_text("x\n")  # exists -> init refuses
        assert_that(main(["init"], root=tmp_path)).is_equal_to(1)
        assert_that(capsys.readouterr().err).contains("already exists")
