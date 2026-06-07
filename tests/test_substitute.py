from assertpy import assert_that

from project import Config, build_var_lookup, scalar, substitute


class TestScalar:
    def test_bool_true(self):
        assert_that(scalar(True)).is_equal_to("true")

    def test_bool_false(self):
        assert_that(scalar(False)).is_equal_to("false")

    def test_int(self):
        assert_that(scalar(8)).is_equal_to("8")

    def test_float(self):
        assert_that(scalar(1.5)).is_equal_to("1.5")

    def test_str(self):
        assert_that(scalar("hi")).is_equal_to("hi")

    def test_list_is_none(self):
        assert_that(scalar([1, 2])).is_none()

    def test_dict_is_none(self):
        assert_that(scalar({"a": 1})).is_none()


class TestBuildVarLookup:
    def test_project_and_tools(self):
        cfg = Config(
            project={"name": "g", "license": "0BSD"},
            tools={"clang_tidy": {"binary": "ct-21", "jobs": 8}},
        )
        lk = build_var_lookup(cfg)
        assert_that(lk).contains_entry({"project.name": "g"}, {"project.license": "0BSD"})
        assert_that(lk).contains_entry({"clang_tidy.binary": "ct-21"}, {"clang_tidy.jobs": "8"})

    def test_skips_non_scalars(self):
        cfg = Config(project={"name": "g", "authors": ["a", "b"]})
        lk = build_var_lookup(cfg)
        assert_that(lk).contains_key("project.name")
        assert_that(lk).does_not_contain_key("project.authors")

    def test_skips_non_dict_tool_sections(self):
        cfg = Config(tools={"weird": "notatable"})
        assert_that(build_var_lookup(cfg)).is_equal_to({})


class TestSubstitute:
    def _lk(self):
        return {"project.name": "my-game", "project.license": "0BSD"}

    def test_replaces_known(self):
        out = substitute(b'set_project("{{project.name}}")', self._lk())
        assert_that(out.decode()).is_equal_to('set_project("my-game")')

    def test_whitespace_inside_braces(self):
        assert_that(substitute(b"{{ project.name }}", self._lk()).decode()).is_equal_to("my-game")

    def test_unknown_left_alone(self):
        assert_that(substitute(b"{{not.real}}", self._lk()).decode()).is_equal_to("{{not.real}}")

    def test_single_braces_untouched(self):
        out = substitute(b"local t = {1, 2, 3}", self._lk())
        assert_that(out.decode()).is_equal_to("local t = {1, 2, 3}")

    def test_empty_lookup_returns_same_object(self):
        content = b"{{project.name}}"
        assert_that(substitute(content, {})).is_same_as(content)

    def test_no_match_returns_same_object(self):
        content = b"no vars here"
        assert_that(substitute(content, self._lk())).is_same_as(content)

    def test_binary_passthrough(self):
        content = b"\x00\x01\xff{{project.name}}"
        assert_that(substitute(content, self._lk())).is_same_as(content)
