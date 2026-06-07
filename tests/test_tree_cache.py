from assertpy import assert_that

from project import GitHubSource, TreeCache, parse_tree, resolve_tree


class _CannedGitHub(GitHubSource):
    """Real GitHubSource with only the HTTP seam replaced by canned responses, so the
    cache/304 wiring is exercised without a network call (no mocks, no patching)."""

    def __init__(self, responses, **kw):
        super().__init__("tok", **kw)
        self._responses = list(responses)  # each: (status, etag, data)
        self.sent_etags = []

    def _conditional_get(self, url, etag=None, context=""):
        self.sent_etags.append(etag)
        return self._responses.pop(0)


class TestParseTree:
    def test_keeps_blobs_drops_trees(self):
        data = {
            "tree": [
                {"path": "templates/git/.gitignore", "sha": "aaa", "type": "blob"},
                {"path": "templates/git", "sha": "bbb", "type": "tree"},
                {"path": "templates/cpp/x.lua", "sha": "ccc", "type": "blob"},
            ]
        }
        assert_that(parse_tree(data)).is_equal_to(
            [("templates/git/.gitignore", "aaa"), ("templates/cpp/x.lua", "ccc")]
        )

    def test_empty(self):
        assert_that(parse_tree({})).is_equal_to([])


class TestResolveTree:
    def test_200_parses_and_returns_cache_entry(self):
        data = {"tree": [{"path": "templates/git/x", "sha": "s1", "type": "blob"}]}
        blobs, entry = resolve_tree(200, 'W/"etag1"', data, cached_blobs=None)
        assert_that(blobs).is_equal_to([("templates/git/x", "s1")])
        assert_that(entry).is_equal_to(('W/"etag1"', [("templates/git/x", "s1")]))

    def test_304_reuses_cache_and_writes_nothing(self):
        cached = [("templates/git/x", "s1")]
        blobs, entry = resolve_tree(304, 'W/"etag1"', None, cached_blobs=cached)
        assert_that(blobs).is_equal_to(cached)
        assert_that(entry).is_none()

    def test_304_with_no_cache_is_empty(self):
        blobs, entry = resolve_tree(304, None, None, cached_blobs=None)
        assert_that(blobs).is_equal_to([])
        assert_that(entry).is_none()


class TestTreeCache:
    def test_put_then_get_round_trips(self, tmp_path):
        cache = TreeCache(tmp_path / "c.json")
        cache.put("you/repo", 'W/"e1"', [("templates/git/x", "s1")])
        # fresh instance -> proves it persisted to disk, not just memory
        etag, blobs = TreeCache(tmp_path / "c.json").get("you/repo")
        assert_that(etag).is_equal_to('W/"e1"')
        assert_that(blobs).is_equal_to([("templates/git/x", "s1")])

    def test_miss_returns_none_none(self, tmp_path):
        assert_that(TreeCache(tmp_path / "nope.json").get("any/repo")).is_equal_to((None, None))

    def test_corrupt_file_treated_as_empty(self, tmp_path):
        p = tmp_path / "c.json"
        p.write_text("{ this is not json", encoding="utf-8")
        assert_that(TreeCache(p).get("any/repo")).is_equal_to((None, None))

    def test_holds_multiple_repos_in_one_file(self, tmp_path):
        cache = TreeCache(tmp_path / "c.json")
        cache.put("org/a", 'W/"ea"', [("templates/a/x", "sa")])
        cache.put("org/b", 'W/"eb"', [("templates/b/y", "sb")])
        reloaded = TreeCache(tmp_path / "c.json")
        assert_that(reloaded.get("org/a")[0]).is_equal_to('W/"ea"')
        assert_that(reloaded.get("org/b")[1]).is_equal_to([("templates/b/y", "sb")])


class TestListBlobsCaching:
    def test_first_call_sends_no_etag_and_populates_cache(self, tmp_path):
        cache = TreeCache(tmp_path / "c.json")
        tree = {"tree": [{"path": "templates/git/x", "sha": "s1", "type": "blob"}]}
        src = _CannedGitHub([(200, 'W/"e1"', tree)], repo="org/a", cache=cache)
        blobs = src.list_blobs(["git"])
        assert_that(blobs).is_equal_to([("templates/git/x", "s1")])
        assert_that(src.sent_etags).is_equal_to([None])  # nothing cached yet
        # cache now holds the etag, visible to a fresh handle
        assert_that(TreeCache(tmp_path / "c.json").get("org/a")[0]).is_equal_to('W/"e1"')

    def test_second_call_sends_etag_and_304_reuses_cache(self, tmp_path):
        cache = TreeCache(tmp_path / "c.json")
        tree = {"tree": [{"path": "templates/git/x", "sha": "s1", "type": "blob"}]}
        src = _CannedGitHub(
            [(200, 'W/"e1"', tree), (304, 'W/"e1"', None)], repo="org/a", cache=cache
        )
        first = src.list_blobs(["git"])
        second = src.list_blobs(["git"])
        assert_that(src.sent_etags).is_equal_to([None, 'W/"e1"'])  # 2nd sent the cached tag
        assert_that(second).is_equal_to(first)  # 304 -> identical blobs, from cache

    def test_stale_etag_gets_200_and_refreshes_cache(self, tmp_path):
        # Repo changed since last sync: we send the stale etag, GitHub answers 200 with a
        # new etag + tree, and the cache is overwritten. (Live this is masked by GitHub's
        # ~60s anonymous edge cache; here it's deterministic.)
        cache = TreeCache(tmp_path / "c.json")
        cache.put("org/a", 'W/"old"', [("templates/old/x", "sold")])
        fresh = {"tree": [{"path": "templates/new/y", "sha": "snew", "type": "blob"}]}
        src = _CannedGitHub([(200, 'W/"new"', fresh)], repo="org/a", cache=cache)
        blobs = src.list_blobs(["new"])
        assert_that(src.sent_etags).is_equal_to(['W/"old"'])  # sent the stale tag
        assert_that(blobs).is_equal_to([("templates/new/y", "snew")])  # fresh blobs
        reloaded = TreeCache(tmp_path / "c.json").get("org/a")
        assert_that(reloaded[0]).is_equal_to('W/"new"')  # cache overwritten
        assert_that(reloaded[1]).is_equal_to([("templates/new/y", "snew")])

    def test_no_cache_means_no_etag_ever_sent(self, tmp_path):
        # Without a cache (e.g. tests injecting sources), behavior is the old unconditional
        # listing: no If-None-Match, no persistence.
        tree = {"tree": [{"path": "templates/git/x", "sha": "s1", "type": "blob"}]}
        src = _CannedGitHub([(200, 'W/"e1"', tree)], repo="org/a")  # cache=None
        src.list_blobs(["git"])
        assert_that(src.sent_etags).is_equal_to([None])
