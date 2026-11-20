

def test_a_read_github_fails_on_its_side_is_tried_again():
    import httpx

    from editor import sitegit

    answers = iter([httpx.Response(500, json={"message": "Internal Server Error"}),
                    httpx.Response(200, json={"content": "aGVsbG8="})])
    calls, pauses = [], []

    def call(method, path, **kw):
        calls.append(method)
        return next(answers)

    repo = sitegit.Repo(call, "o/r", sleep=pauses.append)
    assert repo.file("llms.txt", "main") == b"hello" and calls == ["GET", "GET"] and pauses == [2]
    # A write of the publication is repeated too: git objects, branches and pull requests are found again.
    flaky = iter([httpx.Response(502, text="bad gateway"), httpx.Response(201, json={"sha": "abc"})])
    calls.clear()
    again = sitegit.Repo(lambda m, p, **kw: calls.append(m) or next(flaky), "o/r", sleep=pauses.append)
    assert again.call("POST", "/x").status_code == 201 and calls == ["POST", "POST"]


def test_a_merge_whose_answer_was_lost_is_not_sent_twice():
    import httpx

    from editor import sitegit

    calls = []

    def call(method, path, **kw):
        calls.append(f"{method} {path}")
        if method == "PUT":
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, json={"merged": True, "merge_commit_sha": "m1"})

    repo = sitegit.Repo(call, "o/r", sleep=lambda s: None)
    assert repo.merge(7, "t") == "m1"
    assert calls == ["PUT /repos/o/r/pulls/7/merge", "GET /repos/o/r/pulls/7"]


def test_a_merge_that_did_not_happen_is_sent_again():
    import httpx

    from editor import sitegit

    answers = iter([httpx.Response(502, text="bad gateway"), httpx.Response(200, json={"merged": False}),
                    httpx.Response(200, json={"sha": "m2"})])
    repo = sitegit.Repo(lambda m, p, **kw: next(answers), "o/r", sleep=lambda s: None)
    assert repo.merge(7, "t") == "m2"


def test_checks_uses_the_link_of_a_later_failure():
    import httpx

    from editor import sitegit

    def call(method, path, **kw):
        if path.endswith("/status"):
            return httpx.Response(200, json={"statuses": [
                {"state": "failure", "target_url": None},
                {"state": "failure", "target_url": "https://ci.example/failed"},
            ]})
        return httpx.Response(200, json={"check_runs": []})

    repo = sitegit.Repo(call, "o/r", sleep=lambda s: None)
    assert repo.checks("abc") == ("failure", "https://ci.example/failed")
