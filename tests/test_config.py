import textwrap

from assertpy import assert_that

from project import Config


class TestConfigLoad:
    def test_missing_file_returns_empty(self, tmp_path):
        cfg = Config.load(tmp_path / "nope.toml")
        assert_that(cfg.project).is_equal_to({})
        assert_that(cfg.commands).is_equal_to({})
        assert_that(cfg.tools).is_equal_to({})
        assert_that(cfg.args).is_equal_to([])

    def test_parses_project_and_commands(self, tmp_path):
        p = tmp_path / "project.toml"
        p.write_text(
            textwrap.dedent(
                """
                [project]
                name = "demo"
                [commands]
                build = ["xmake_build"]
                """
            ),
            encoding="utf-8",
        )
        cfg = Config.load(p)
        assert_that(cfg.project).is_equal_to({"name": "demo"})
        assert_that(cfg.commands).is_equal_to({"build": ["xmake_build"]})

    def test_tools_bucket_excludes_project_and_commands(self, tmp_path):
        p = tmp_path / "project.toml"
        p.write_text(
            textwrap.dedent(
                """
                [project]
                name = "demo"
                [commands]
                lint = ["clang_tidy"]
                [clang_tidy]
                binary = "clang-tidy-21"
                jobs = 8
                [sync]
                templates = ["git"]
                """
            ),
            encoding="utf-8",
        )
        cfg = Config.load(p)
        assert_that(cfg.tools).contains_key("clang_tidy", "sync")
        assert_that(cfg.tools).does_not_contain_key("project", "commands")
        assert_that(cfg.tools["clang_tidy"]).is_equal_to({"binary": "clang-tidy-21", "jobs": 8})

    def test_default_construction_is_empty(self):
        cfg = Config()
        assert_that(cfg.project).is_equal_to({})
        assert_that(cfg.args).is_equal_to([])
