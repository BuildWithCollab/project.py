import subprocess

import pytest
from assertpy import assert_that

from project import git_blob_sha, git_normalize


class TestGitNormalize:
    def test_crlf_to_lf(self):
        assert_that(git_normalize(b"a\r\nb\r\n")).is_equal_to(b"a\nb\n")

    def test_lf_untouched(self):
        assert_that(git_normalize(b"a\nb\n")).is_equal_to(b"a\nb\n")

    def test_binary_with_nul_untouched(self):
        data = b"a\r\n\x00b\r\n"
        assert_that(git_normalize(data)).is_equal_to(data)


class TestGitBlobSha:
    def test_matches_known_empty_blob(self):
        # `printf '' | git hash-object --stdin`
        assert_that(git_blob_sha(b"")).is_equal_to("e69de29bb2d1d6434b8b29ae775ad8c2e48c5391")

    def test_matches_known_hello_blob(self):
        # `printf 'hello\n' | git hash-object --stdin`
        assert_that(git_blob_sha(b"hello\n")).is_equal_to("ce013625030ba8dba906f756967f9e9ca394464a")

    def test_matches_real_git_hash_object(self, tmp_path):
        content = b"some\ntemplate\ncontent\n"
        f = tmp_path / "blob.txt"
        f.write_bytes(content)
        try:
            out = subprocess.run(
                ["git", "hash-object", str(f)],
                capture_output=True, text=True, check=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError):
            pytest.skip("git not available")
        assert_that(git_blob_sha(git_normalize(content))).is_equal_to(out.stdout.strip())
