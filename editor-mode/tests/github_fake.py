"""A small GitHub in memory, for the publications to a site's repository:
branches, files, commits made through the Git data API, pull requests,
checks and merges."""

import base64
import itertools
import json
import re
from urllib.parse import unquote

import httpx

API = "https://api.github.com"


class FakeGitHub:
    def __init__(self, files: dict[str, str | bytes], repo: str = "me/site", default: str = "main"):
        self.repo, self.default = repo, default
        self._ids = (f"{n:040x}" for n in itertools.count(1))
        root = {p: (c.encode() if isinstance(c, str) else c) for p, c in files.items()}
        self.trees: dict[str, dict[str, bytes]] = {}
        self.blobs: dict[str, bytes] = {}
        self.commits: dict[str, dict] = {}
        first = self._commit(self._tree(root), [], "initial")
        self.branches = {default: first}
        self.pulls: list[dict] = []
        self.checks: dict[str, list[str]] = {}  # sha -> states returned one call after the other
        self.check_runs = False
        self.calls: list[tuple[str, str]] = []
        self.token = None

    def _tree(self, files):
        sha = next(self._ids)
        self.trees[sha] = dict(files)
        return sha

    def _commit(self, tree, parents, message):
        sha = next(self._ids)
        self.commits[sha] = {"tree": tree, "parents": parents, "message": message}
        return sha

    def files(self, branch: str | None = None) -> dict[str, bytes]:
        return self.trees[self.commits[self.branches[branch or self.default]]["tree"]]

    def text(self, path: str, branch: str | None = None) -> str:
        return self.files(branch)[path].decode()

    def route(self, router):
        router.route(url__regex=rf"^{re.escape(API)}/repos/{re.escape(self.repo)}(/|$|\?)").mock(side_effect=self.handle)
        return self

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.token = request.headers.get("authorization")
        path = unquote(request.url.raw_path.decode().split("?")[0])[len(f"/repos/{self.repo}"):]
        method = request.method
        self.calls.append((method, path))
        body = json.loads(request.content) if request.content else {}
        q = dict(request.url.params)
        if method == "GET" and path == "":
            return httpx.Response(200, json={"default_branch": self.default})
        if method == "GET" and path.startswith("/contents/"):
            want = path[len("/contents/"):]
            files = self.files(q.get("ref"))
            if want in files:
                return httpx.Response(200, json={"type": "file", "path": want,
                                                 "content": base64.b64encode(files[want]).decode()})
            prefix = want.rstrip("/") + "/"
            names = {}
            for p in files:
                if p.startswith(prefix):
                    rest = p[len(prefix):]
                    name = rest.split("/", 1)[0]
                    names[name] = "dir" if "/" in rest else "file"
            if not names:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json=[{"name": n, "path": prefix + n, "type": t} for n, t in sorted(names.items())])
        if m := re.fullmatch(r"/git/ref/heads/(.+)", path):
            if method == "GET":
                if m.group(1) not in self.branches:
                    return httpx.Response(404, json={"message": "Not Found"})
                return httpx.Response(200, json={"object": {"sha": self.branches[m.group(1)]}})
        if m := re.fullmatch(r"/git/refs/heads/(.+)", path):
            name = m.group(1)
            if method == "PATCH":
                if name not in self.branches:
                    return httpx.Response(422, json={"message": "Reference does not exist"})
                if not body.get("force") and self.branches[name] not in self._ancestors(body["sha"]):
                    return httpx.Response(422, json={"message": "Update is not a fast forward"})
                self.branches[name] = body["sha"]
                return httpx.Response(200, json={})
            if method == "DELETE":
                self.branches.pop(name, None)
                return httpx.Response(204)
        if method == "GET" and (m := re.fullmatch(r"/git/commits/(\w+)", path)):
            return httpx.Response(200, json={"tree": {"sha": self.commits[m.group(1)]["tree"]}})
        if method == "POST" and path == "/git/blobs":
            sha = next(self._ids)
            self.blobs[sha] = base64.b64decode(body["content"])
            return httpx.Response(201, json={"sha": sha})
        if method == "POST" and path == "/git/trees":
            files = dict(self.trees[body["base_tree"]])
            for e in body["tree"]:
                files[e["path"]] = self.blobs[e["sha"]]
            return httpx.Response(201, json={"sha": self._tree(files)})
        if method == "POST" and path == "/git/commits":
            return httpx.Response(201, json={"sha": self._commit(body["tree"], body["parents"], body["message"])})
        if method == "POST" and path == "/git/refs":
            name = body["ref"].removeprefix("refs/heads/")
            if name in self.branches:
                return httpx.Response(422, json={"message": "Reference already exists"})
            self.branches[name] = body["sha"]
            return httpx.Response(201, json={})
        if method == "POST" and path == "/pulls":
            if any(p["head"] == body["head"] and p["state"] == "open" for p in self.pulls):
                return httpx.Response(422, json={"message": "A pull request already exists"})
            pr = {"number": len(self.pulls) + 1, "head": body["head"], "base": body["base"], "state": "open",
                  "title": body["title"], "html_url": f"https://github.com/{self.repo}/pull/{len(self.pulls) + 1}"}
            self.pulls.append(pr)
            return httpx.Response(201, json=pr)
        if method == "GET" and path == "/pulls":
            head = q.get("head", "").split(":", 1)[-1]
            return httpx.Response(200, json=[p for p in self.pulls if p["head"] == head and p["state"] == "open"])
        if method == "PUT" and (m := re.fullmatch(r"/pulls/(\d+)/merge", path)):
            pr = self.pulls[int(m.group(1)) - 1]
            head = self.branches[pr["head"]]
            base_sha = self.branches[pr["base"]]
            files = dict(self.files(pr["base"]))
            files.update(self.trees[self.commits[head]["tree"]])
            self.branches[pr["base"]] = self._commit(self._tree(files), [base_sha, head], body.get("commit_title", ""))
            pr["state"] = "merged"
            return httpx.Response(200, json={"sha": self.branches[pr["base"]], "merged": True})
        if method == "GET" and (m := re.fullmatch(r"/commits/(\w+)/status", path)):
            seq = self.checks.get(m.group(1)) or self.checks.get("*") or []
            state = seq.pop(0) if len(seq) > 1 else (seq[0] if seq else None)
            statuses = [] if state is None else [{"state": state, "context": "Vercel",
                                                   "target_url": "https://vercel.com/me/site/dpl_1"}]
            return httpx.Response(200, json={"state": state or "pending", "statuses": statuses})
        if method == "GET" and re.fullmatch(r"/commits/\w+/check-runs", path):
            if not self.check_runs:
                return httpx.Response(403, json={"message": "Resource not accessible by integration"})
            return httpx.Response(200, json={"check_runs": []})
        return httpx.Response(404, json={"message": f"fake: {method} {path}"})

    def _ancestors(self, sha):
        seen, todo = set(), [sha]
        while todo:
            s = todo.pop()
            if s in seen:
                continue
            seen.add(s)
            todo += self.commits[s]["parents"]
        return seen
