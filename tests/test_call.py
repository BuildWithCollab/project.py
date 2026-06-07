import textwrap

from assertpy import assert_that

from project import Config, ProjectError, classify_spec, dispatch
from tests._fakes import RecordingRunner


class TestClassifySpec:
    def test_shell(self):
        assert_that(classify_spec("$rm -rf build")).is_equal_to(("shell", "rm -rf build"))

    def test_shell_optional_space(self):
        assert_that(classify_spec("$ rm -rf build")).is_equal_to(("shell", "rm -rf build"))

    def test_empty_shell_raises(self):
        assert_that(classify_spec).raises(ProjectError).when_called_with("$")

    def test_module_attr(self):
        assert_that(classify_spec("scripts.deploy:go")).is_equal_to(("module", ("scripts.deploy", "go")))

    def test_missing_attr_raises(self):
        assert_that(classify_spec).raises(ProjectError).when_called_with("scripts.deploy:")

    def test_builtin(self):
        assert_that(classify_spec("clang_tidy")).is_equal_to(("builtin", "clang_tidy"))


class TestDispatch:
    def test_shell_runs_through_runner(self, tmp_path):
        r = RecordingRunner()
        dispatch("$echo hi", Config(), root=tmp_path, runner=r)
        assert_that(r.calls).is_length(1)
        assert_that(r.calls[0].shell).is_true()
        assert_that(r.calls[0].cmd).is_equal_to("echo hi")

    def test_builtin_invoked(self, tmp_path):
        r = RecordingRunner()
        dispatch("xmake_build", Config(), root=tmp_path, runner=r)
        assert_that(r.calls[0].cmd).is_equal_to(["xmake", "build"])

    def test_unknown_builtin_raises(self, tmp_path):
        assert_that(dispatch).raises(ProjectError).when_called_with(
            "bogus", Config(), root=tmp_path, runner=RecordingRunner()
        )

    def test_module_attr_invoked(self, tmp_path):
        (tmp_path / "tgt_go.py").write_text(
            textwrap.dedent(
                """
                invoked = []
                def go(cfg):
                    invoked.append(cfg)
                """
            ),
            encoding="utf-8",
        )
        cfg = Config(args=["x"])
        dispatch("tgt_go:go", cfg, root=tmp_path, runner=RecordingRunner())
        import tgt_go

        assert_that(tgt_go.invoked).is_length(1)
        assert_that(tgt_go.invoked[0]).is_same_as(cfg)

    def test_module_not_found_raises(self, tmp_path):
        assert_that(dispatch).raises(ProjectError).when_called_with(
            "nonexistent_mod_xyz:go", Config(), root=tmp_path, runner=RecordingRunner()
        )

    def test_missing_attr_raises(self, tmp_path):
        (tmp_path / "tgt_attr.py").write_text("x = 1\n", encoding="utf-8")
        assert_that(dispatch).raises(ProjectError).when_called_with(
            "tgt_attr:nope", Config(), root=tmp_path, runner=RecordingRunner()
        )

    def test_not_callable_raises(self, tmp_path):
        (tmp_path / "tgt_value.py").write_text("value = 42\n", encoding="utf-8")
        assert_that(dispatch).raises(ProjectError).when_called_with(
            "tgt_value:value", Config(), root=tmp_path, runner=RecordingRunner()
        )
