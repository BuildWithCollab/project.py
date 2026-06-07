from assertpy import assert_that

from project import self_update
from tests._fakes import DictSource


class TestSelfUpdate:
    def test_up_to_date_is_noop(self, tmp_path, capsys):
        script = tmp_path / "project.py"
        script.write_bytes(b"print('hi')\n")
        self_update(script_path=script, source=DictSource({"project.py": b"print('hi')\n"}))
        assert_that(capsys.readouterr().out).contains("Already up to date")
        assert_that(script.read_bytes()).is_equal_to(b"print('hi')\n")

    def test_updates_when_changed(self, tmp_path, capsys):
        script = tmp_path / "project.py"
        script.write_bytes(b"old\n")
        self_update(script_path=script, source=DictSource({"project.py": b"new content\n"}))
        assert_that(script.read_bytes()).is_equal_to(b"new content\n")
        assert_that(capsys.readouterr().out).contains("Updated")

    def test_failure_is_reported_not_raised(self, tmp_path, capsys):
        script = tmp_path / "project.py"
        script.write_bytes(b"old\n")
        self_update(script_path=script, source=DictSource({}))  # no project.py -> read raises, swallowed
        assert_that(capsys.readouterr().err).contains("Failed to check for updates")
        assert_that(script.read_bytes()).is_equal_to(b"old\n")
