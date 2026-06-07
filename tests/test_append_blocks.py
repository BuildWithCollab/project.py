from assertpy import assert_that

from project import format_append_block, merge_append_blocks, strip_all_blocks


class TestFormatBlock:
    def test_wraps(self):
        assert_that(format_append_block("git", "build/\n*.o")).is_equal_to(
            "# [START git]\nbuild/\n*.o\n# [END git]\n"
        )

    def test_strips_trailing_newlines_in_body(self):
        assert_that(format_append_block("git", "build/\n\n")).is_equal_to(
            "# [START git]\nbuild/\n# [END git]\n"
        )


class TestMerge:
    def test_append_into_empty(self):
        assert_that(merge_append_blocks("", {"git": "build/"})).is_equal_to(
            "# [START git]\nbuild/\n# [END git]\n"
        )

    def test_preserves_user_content(self):
        out = merge_append_blocks("secrets.env\n", {"git": "build/"})
        assert_that(out).starts_with("secrets.env\n")
        assert_that(out).contains("# [START git]\nbuild/\n# [END git]\n")

    def test_replaces_in_place(self):
        out = merge_append_blocks("# [START git]\nold/\n# [END git]\n", {"git": "new/"})
        assert_that(out).contains("new/")
        assert_that(out).does_not_contain("old/")

    def test_strips_unwanted_block(self):
        existing = "user\n\n# [START rust]\ntarget/\n# [END rust]\n"
        out = merge_append_blocks(existing, {"git": "build/"})
        assert_that(out).does_not_contain("rust")
        assert_that(out).does_not_contain("target/")
        assert_that(out).contains("# [START git]")
        assert_that(out).contains("user")

    def test_multiple_blocks_in_order(self):
        out = merge_append_blocks("", {"a": "AA", "b": "BB"})
        assert_that(out.index("[START a]")).is_less_than(out.index("[START b]"))

    def test_collapses_blank_runs(self):
        out = merge_append_blocks("u\n\n\n\n", {"g": "x"})
        assert_that(out).does_not_contain("\n\n\n")

    def test_body_with_backslashes_is_literal(self):
        # A function replacement is used so \1 / backslashes in a block body are inserted
        # literally rather than interpreted by re. (A latent bug in the original.)
        out = merge_append_blocks("# [START g]\nold\n# [END g]\n", {"g": r"path\to\thing \1"})
        assert_that(out).contains(r"path\to\thing \1")


class TestStripAll:
    def test_removes_blocks_keeps_user(self):
        out = strip_all_blocks("user\n\n# [START g]\nbuild/\n# [END g]\n")
        assert_that(out).contains("user")
        assert_that(out).does_not_contain("build/")
        assert_that(out).does_not_contain("START")

    def test_only_blocks_becomes_blank(self):
        out = strip_all_blocks("# [START g]\nbuild/\n# [END g]\n")
        assert_that(out.strip()).is_equal_to("")
