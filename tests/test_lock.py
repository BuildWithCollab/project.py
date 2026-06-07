from assertpy import assert_that

from project import format_lock, parse_lock


class TestParseLock:
    def test_empty(self):
        h, m, a = parse_lock("")
        assert_that(h).is_equal_to("")
        assert_that(m).is_equal_to({})
        assert_that(a).is_equal_to(set())

    def test_full(self):
        text = (
            "# comment\n"
            "[cfg_hash]\n"
            "abc123\n"
            "[managed]\n"
            "sha1  path/one.txt\n"
            "sha2  path/two.txt\n"
            "[append]\n"
            ".gitignore\n"
        )
        h, m, a = parse_lock(text)
        assert_that(h).is_equal_to("abc123")
        assert_that(m).is_equal_to({"path/one.txt": "sha1", "path/two.txt": "sha2"})
        assert_that(a).is_equal_to({".gitignore"})

    def test_sectionless_backward_compat(self):
        # Old locks had no section headers: every line is a managed "sha  path".
        text = "shaA  a.txt\nshaB  b.txt\n"
        _, m, _ = parse_lock(text)
        assert_that(m).is_equal_to({"a.txt": "shaA", "b.txt": "shaB"})


class TestRoundTrip:
    def test_round_trip(self):
        h = "deadbeef"
        m = {"b.txt": "s2", "a.txt": "s1"}
        a = {"z.gitignore", ".gitignore"}
        h2, m2, a2 = parse_lock(format_lock(h, m, a))
        assert_that(h2).is_equal_to(h)
        assert_that(m2).is_equal_to(m)
        assert_that(a2).is_equal_to(a)

    def test_managed_sorted(self):
        text = format_lock("h", {"b": "2", "a": "1"}, set())
        assert_that(text.index("  a")).is_less_than(text.index("  b"))

    def test_no_managed_means_no_section(self):
        text = format_lock("h", {}, {".gitignore"})
        assert_that(text).does_not_contain("[managed]")
        assert_that(text).contains("[append]")
