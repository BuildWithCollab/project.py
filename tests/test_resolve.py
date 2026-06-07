from assertpy import assert_that

from project import Platform, resolve_command


class TestResolveCommand:
    def test_plain_name(self):
        cmds = {"build": ["xmake_build"]}
        assert_that(resolve_command("build", cmds, Platform.LINUX)).is_equal_to(["xmake_build"])

    def test_missing_returns_empty(self):
        assert_that(resolve_command("nope", {}, Platform.LINUX)).is_equal_to([])

    def test_platform_override_wins(self):
        cmds = {"lint": ["clang_tidy"], "lint:windows": ["ruff"]}
        assert_that(resolve_command("lint", cmds, Platform.WINDOWS)).is_equal_to(["ruff"])

    def test_falls_back_when_no_platform_entry(self):
        cmds = {"lint": ["clang_tidy"], "lint:windows": ["ruff"]}
        assert_that(resolve_command("lint", cmds, Platform.LINUX)).is_equal_to(["clang_tidy"])

    def test_macos_suffix_is_macos(self):
        cmds = {"lint": ["a"], "lint:macos": ["b"]}
        assert_that(resolve_command("lint", cmds, Platform.MAC)).is_equal_to(["b"])

    def test_returns_a_copy(self):
        original = ["x"]
        result = resolve_command("build", {"build": original}, Platform.LINUX)
        result.append("y")
        assert_that(original).is_equal_to(["x"])
