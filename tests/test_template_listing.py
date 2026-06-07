from assertpy import assert_that

from project import classify_template_files


def blobs(*paths):
    return [(p, f"sha:{p}") for p in paths]


class TestClassify:
    def test_managed(self):
        tf = classify_template_files(blobs("templates/git/.gitattributes"), ["git"])
        assert_that(tf.managed).contains_key(".gitattributes")
        assert_that(tf.managed[".gitattributes"][0]).is_equal_to("git")

    def test_write_once_strips_marker(self):
        tf = classify_template_files(blobs("templates/cpp/_write_once_/xmake.lua"), ["cpp"])
        assert_that(tf.write_once).contains_key("xmake.lua")

    def test_append_strips_marker(self):
        tf = classify_template_files(blobs("templates/git/_append_/.gitignore"), ["git"])
        assert_that([t[1] for t in tf.append]).contains(".gitignore")

    def test_nested_longest_prefix_wins(self):
        tf = classify_template_files(blobs("templates/cpp/xmake/targets.lua"), ["cpp", "cpp/xmake"])
        assert_that(tf.managed["targets.lua"][0]).is_equal_to("cpp/xmake")

    def test_last_template_wins_on_managed_conflict(self):
        tf = classify_template_files(
            blobs("templates/a/shared.txt", "templates/b/shared.txt"), ["a", "b"]
        )
        assert_that(tf.managed["shared.txt"][0]).is_equal_to("b")

    def test_append_over_managed_conflict(self):
        tf = classify_template_files(
            blobs("templates/a/.gitignore", "templates/b/_append_/.gitignore"), ["a", "b"]
        )
        assert_that(tf.managed).does_not_contain_key(".gitignore")
        assert_that([t[1] for t in tf.append]).contains(".gitignore")
        assert_that(tf.warnings).is_not_empty()

    def test_missing_template_warns(self):
        tf = classify_template_files(blobs("templates/git/.gitattributes"), ["git", "ghost"])
        assert_that(" ".join(tf.warnings)).contains("ghost")

    def test_ignores_unlisted_templates(self):
        tf = classify_template_files(blobs("templates/other/x.txt"), ["git"])
        assert_that(tf.managed).is_equal_to({})
