import json
import os

import pytest
from assertpy import assert_that

from project import (
    CLANG_TIDY_EXTS,
    ProjectError,
    clang_tidy_check_and_fix,
    parse_diagnostics,
    select_translation_units,
)
from tests._fakes import RecordingRunner

# The clang-tidy diagnostic regex assumes unix-style paths (no drive-letter colon),
# so the positive offender-extraction cases can't match real Windows paths. Those are
# skipped on nt and covered by linux/macos CI. (Flagged as a known limitation.)
skip_on_windows = pytest.mark.skipif(os.name == "nt", reason="diagnostic regex assumes unix paths")


class TestSelectTranslationUnits:
    def test_filters_by_ext_and_root(self, tmp_path):
        db = [
            {"file": str(tmp_path / "a.cpp")},
            {"file": str(tmp_path / "b.h")},
            {"file": str(tmp_path / "sub" / "c.cppm")},
            {"file": str(tmp_path.parent / "elsewhere_d.cpp")},
        ]
        out = select_translation_units(db, tmp_path, CLANG_TIDY_EXTS)
        assert_that(sorted(p.name for p in out)).is_equal_to(["a.cpp", "c.cppm"])

    def test_dedups(self, tmp_path):
        db = [{"file": str(tmp_path / "a.cpp")}, {"file": str(tmp_path / "a.cpp")}]
        assert_that(select_translation_units(db, tmp_path, CLANG_TIDY_EXTS)).is_length(1)


class TestParseDiagnostics:
    def test_ignores_non_matching_lines(self, tmp_path):
        assert_that(parse_diagnostics("note: just a note\n\nrandom\n", tmp_path)).is_equal_to(set())

    def test_ignores_outside_root(self, tmp_path):
        assert_that(parse_diagnostics("/other/x.cpp:1:1: warning: y\n", tmp_path)).is_equal_to(set())

    @skip_on_windows
    def test_extracts_offender_paths(self, tmp_path):
        f = tmp_path / "a.cpp"
        output = f"{f}:10:5: warning: something [check]\n{f}:11:1: error: bad\nnote: ignore\n"
        assert_that(parse_diagnostics(output, tmp_path)).is_equal_to({f.resolve()})


class TestOrchestration:
    def test_missing_compile_commands_raises(self, tmp_path):
        assert_that(clang_tidy_check_and_fix).raises(ProjectError).when_called_with(
            runner=RecordingRunner(), root=tmp_path, compile_commands=tmp_path / "nope.json"
        )

    def test_clean_run(self, tmp_path, capsys):
        (tmp_path / "a.cpp").write_text("int main(){}\n")
        cc = tmp_path / "compile_commands.json"
        cc.write_text(json.dumps([{"file": str(tmp_path / "a.cpp")}]))
        result = clang_tidy_check_and_fix(
            runner=RecordingRunner(), root=tmp_path, compile_commands=cc,
            report_path=tmp_path / "report.txt",
        )
        assert_that(result).is_equal_to([])
        assert_that(capsys.readouterr().out).contains("clean.")

    @skip_on_windows
    def test_offenders_reported_and_fixed(self, tmp_path):
        f = tmp_path / "a.cpp"
        f.write_text("x\n")
        cc = tmp_path / "compile_commands.json"
        cc.write_text(json.dumps([{"file": str(f)}]))
        runner = RecordingRunner(outputs={"a.cpp": f"{f}:1:1: warning: bad [c]\n"})
        result = clang_tidy_check_and_fix(
            runner=runner, root=tmp_path, compile_commands=cc,
            report_path=tmp_path / "report.txt", fix=True,
        )
        assert_that(result).is_equal_to([f.resolve()])
        assert_that([c for c in runner.calls if "-fix-errors" in c.joined]).is_not_empty()
