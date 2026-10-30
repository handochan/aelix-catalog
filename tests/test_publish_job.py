"""tools/publish_job.py, the only code the job that can write runs (handochan/aelix-ai#616).

Stdlib ``unittest``, no network, as the job itself is stdlib. Each test builds a
throwaway git repository shaped like this one (``v1/``, and ``control/`` unless
the test is about its absence) and runs the job's actions on it. Run from the
repository root::

    python3 -m unittest discover -s tests -v

What is pinned: a build artifact the job refuses changes nothing; a rollback takes
its entry from the index history and holds; the pruning keeps every named
revision and the newest 30; a hold file that is not JSON still holds; a checkout
without ``control/`` still commits; and both jobs read this repository at the
default branch's head, so a run queued behind a rollback stops on its hold.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("publish_job", ROOT / "tools" / "publish_job.py")
assert _spec is not None and _spec.loader is not None
pj = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pj)

RUN = "https://github.com/handochan/aelix-catalog/actions/runs/1"


def revision(tag: str) -> tuple[str, bytes]:
    """A schema-1 revision's name and bytes; ``tag`` makes each one distinct."""

    raw = json.dumps(
        {"schema": pj.REVISION_SCHEMA, "lineage": ["0" * 32], "providers": {}, "tag": tag},
        sort_keys=True,
    ).encode("utf-8")
    return "sha256-" + hashlib.sha256(raw).hexdigest(), raw


def entry(name: str, floor: str = "0.1.0b2", published: str = "2026-10-30T01:17:00Z") -> dict[str, Any]:
    return {
        "minimumAelixVersion": floor,
        "revision": name,
        "publishedAt": published,
        "sourceCommit": "c" * 40,
        "upstream": {
            "modelsDevSha256": "a" * 64,
            "openRouterSha256": "b" * 64,
            "fetchedAt": published,
        },
        "rows": 1,
        "newRows": 0,
    }


def index(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"schema": pj.INDEX_SCHEMA, "catalogs": list(entries)}


class Case(unittest.TestCase):
    """A scratch repository, git isolated from the machine's config (signing, hooks)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        empty = self.tmp / "gitconfig"
        empty.write_text("", encoding="utf-8")
        env = mock.patch.dict(
            os.environ,
            {
                "GIT_CONFIG_GLOBAL": str(empty),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_AUTHOR_NAME": "test",
                "GIT_AUTHOR_EMAIL": "test@example.invalid",
                "GIT_COMMITTER_NAME": "test",
                "GIT_COMMITTER_EMAIL": "test@example.invalid",
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.repo = self.tmp / "catalog"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.stamp = 1_761_786_000  # a fixed epoch; each fixture commit adds a minute

    def git(self, *args: str, when: int | None = None) -> str:
        env = dict(os.environ)
        if when is not None:
            env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"{when} +0000"
        proc = subprocess.run(
            ["git", "-C", str(self.repo), *args],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        return proc.stdout

    def commit_all(self, message: str) -> None:
        self.stamp += 60
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message, when=self.stamp)

    def write(self, rel: str, data: bytes | str) -> None:
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)

    def deploy(self, *tags: str, control: bool = True) -> list[str]:
        """Commit one revision per tag, the index naming the last, as publications would."""

        if control and not (self.repo / "control").exists():
            self.write("control/README.md", "# control/\n")
        names = []
        for tag in tags:
            name, raw = revision(tag)
            self.write(f"{pj.REVISIONS_REL}/{name}.json", raw)
            # Each publication's own time, so an entry from history differs from the current one.
            published = f"2026-10-30T01:{len(names):02}:00Z"
            self.write(pj.INDEX_REL, pj._canonical(index(entry(name, published=published))))
            self.commit_all(f"publish {name}")
            names.append(name)
        return names

    def artifact(self, files: dict[str, bytes]) -> Path:
        out = self.tmp / "artifact"
        if out.exists():
            raise AssertionError("one artifact per test")
        for rel, data in files.items():
            path = out / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return out

    def head(self) -> str:
        return self.git("rev-parse", "HEAD").strip()

    def clean(self) -> bool:
        return not self.git("status", "--porcelain").strip()

    def run_main(self, *args: str) -> tuple[int, str, dict[str, str]]:
        output = self.tmp / "github-output"
        output.write_text("", encoding="utf-8")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            status = pj.main([*args, "--repo", str(self.repo), "--github-output", str(output)])
        lines = output.read_text(encoding="utf-8").splitlines()
        return status, stdout.getvalue(), dict(line.split("=", 1) for line in lines)


class PublishTests(Case):
    def test_a_good_artifact_is_committed_and_deployed(self) -> None:
        (old,) = self.deploy("old")
        name, raw = revision("new")
        artifact = self.artifact(
            {
                f"v1/revisions/{name}.json": raw,
                "v1/index.json": json.dumps(index(entry(name))).encode("utf-8"),
                "summary.md": b"# summary\n",
                "result.json": b'{"state": "publish"}',
            }
        )
        status, out, values = self.run_main("publish", "--artifact", str(artifact))
        self.assertEqual(status, 0, out)
        self.assertEqual(values, {"changed": "true", "deploy": "true"})
        self.assertIn(f"Published `{name}`", out)
        self.assertEqual(self.git("log", "-1", "--format=%s").strip(), f"publish {name}")
        self.assertEqual((self.repo / f"v1/revisions/{name}.json").read_bytes(), raw)
        self.assertEqual(
            (self.repo / pj.INDEX_REL).read_bytes(), pj._canonical(index(entry(name)))
        )
        # Under 30 revisions nothing is pruned, named or not.
        self.assertTrue((self.repo / f"v1/revisions/{old}.json").is_file())
        self.assertTrue(self.clean())

    def test_an_artifact_the_job_refuses_changes_nothing(self) -> None:
        (deployed,) = self.deploy("deployed")
        name, raw = revision("new")
        other, other_raw = revision("other")
        good_index = json.dumps(index(entry(name))).encode("utf-8")
        held = index(entry(name))
        held["hold"] = {"action": "rollback", "at": "2026-10-30T01:17:00Z"}
        not_schema_1 = json.dumps({"schema": "aelix-model-catalog/2"}).encode("utf-8")
        not_schema_1_name = "sha256-" + hashlib.sha256(not_schema_1).hexdigest()
        cases: dict[str, tuple[dict[str, bytes], str]] = {
            "bytes that do not hash to the name": (
                {f"v1/revisions/{name}.json": raw + b" ", "v1/index.json": good_index},
                "do not hash to the name",
            ),
            "a path the build may not supply": (
                {
                    f"v1/revisions/{name}.json": raw,
                    "v1/index.json": good_index,
                    "control/hold.json": b"{}",
                },
                "may not supply",
            ),
            "two revisions": (
                {
                    f"v1/revisions/{name}.json": raw,
                    f"v1/revisions/{other}.json": other_raw,
                    "v1/index.json": good_index,
                },
                "exactly one",
            ),
            "no index": ({f"v1/revisions/{name}.json": raw}, "exactly one"),
            "an index that does not name the revision": (
                {
                    f"v1/revisions/{name}.json": raw,
                    "v1/index.json": json.dumps(index(entry(deployed))).encode("utf-8"),
                },
                f"does not name {name}",
            ),
            "an index carrying a hold": (
                {f"v1/revisions/{name}.json": raw, "v1/index.json": json.dumps(held).encode()},
                "carries a hold",
            ),
            "an index naming a revision that is not deployed": (
                {
                    f"v1/revisions/{name}.json": raw,
                    "v1/index.json": json.dumps(
                        index(entry(name), entry(other, floor="0.2.0"))
                    ).encode("utf-8"),
                },
                f"names {other}, which is not deployed",
            ),
            "an index of the wrong shape": (
                {
                    f"v1/revisions/{name}.json": raw,
                    "v1/index.json": json.dumps(index({**entry(name), "rows": -1})).encode(),
                },
                "rows is not a count",
            ),
            "a revision that is not schema 1": (
                {
                    f"v1/revisions/{not_schema_1_name}.json": not_schema_1,
                    "v1/index.json": json.dumps(index(entry(not_schema_1_name))).encode(),
                },
                "is not a schema-1 revision",
            ),
        }
        before = self.head()
        for label, (files, reason) in cases.items():
            with self.subTest(label):
                artifact = self.tmp / "artifact"
                shutil.rmtree(artifact, ignore_errors=True)
                self.artifact(files)
                status, out, values = self.run_main("publish", "--artifact", str(artifact))
                self.assertEqual(status, 1, out)
                self.assertIn("**Refused - nothing is published.**", out)
                self.assertIn(reason, out)
                self.assertEqual(values, {"changed": "false", "deploy": "false"})
                self.assertEqual(self.head(), before)
                self.assertTrue(self.clean(), self.git("status", "--porcelain"))

    def test_an_artifact_holding_a_symbolic_link_is_refused(self) -> None:
        self.deploy("deployed")
        name, raw = revision("new")
        artifact = self.artifact(
            {
                f"v1/revisions/{name}.json": raw,
                "v1/index.json": json.dumps(index(entry(name))).encode("utf-8"),
            }
        )
        (artifact / "summary.md").symlink_to(self.repo / pj.INDEX_REL)
        before = self.head()
        with self.assertRaisesRegex(pj.Refused, "symbolic link"):
            pj.act_publish(self.repo, artifact)
        self.assertEqual(self.head(), before)
        self.assertTrue(self.clean())


class RollbackTests(Case):
    def test_a_rollback_takes_its_entry_from_the_index_history_and_holds(self) -> None:
        first, second = self.deploy("first", "second")
        # Another floor's entry, which a rollback of 0.1.0b2 leaves where it is.
        third, third_raw = revision("third")
        self.write(f"{pj.REVISIONS_REL}/{third}.json", third_raw)
        second_entry = json.loads(self.git("show", f"HEAD:{pj.INDEX_REL}"))["catalogs"][0]
        current = index(second_entry, entry(third, floor="0.2.0"))
        self.write(pj.INDEX_REL, pj._canonical(current))
        self.commit_all("publish with a second floor")
        first_entry = json.loads(self.git("show", f"HEAD~2:{pj.INDEX_REL}"))["catalogs"][0]
        self.assertNotEqual(first_entry["publishedAt"], second_entry["publishedAt"])

        status, out, values = self.run_main(
            "rollback", "--revision", first, "--reason", "bad maps", "--run", RUN
        )
        self.assertEqual(status, 0, out)
        self.assertEqual(values, {"changed": "true", "deploy": "true"})
        published = json.loads((self.repo / pj.INDEX_REL).read_bytes())
        self.assertEqual(published["catalogs"], [first_entry, entry(third, floor="0.2.0")])
        self.assertEqual(published["hold"]["action"], "rollback")
        hold = json.loads((self.repo / pj.HOLD_REL).read_text(encoding="utf-8"))
        self.assertEqual(
            {k: hold[k] for k in ("action", "revision", "reason", "run")},
            {"action": "rollback", "revision": first, "reason": "bad maps", "run": RUN},
        )
        self.assertEqual(hold["at"], published["hold"]["at"])
        self.assertEqual(self.git("log", "-1", "--format=%s").strip(), f"rollback to {first}: hold")
        self.assertEqual(pj.check_index(published), [])
        self.assertTrue(self.clean())

        # Held: a publication of a good artifact publishes nothing and commits nothing.
        name, raw = revision("after")
        artifact = self.artifact(
            {
                f"v1/revisions/{name}.json": raw,
                "v1/index.json": json.dumps(index(entry(name))).encode("utf-8"),
            }
        )
        held_at = self.head()
        status, out, values = self.run_main("publish", "--artifact", str(artifact))
        self.assertEqual(status, 0, out)
        self.assertIn("NOT published", out)
        self.assertEqual(values, {"changed": "false", "deploy": "false"})
        self.assertEqual(self.head(), held_at)

        # release-hold removes the file, commits, and deploys nothing.
        status, out, values = self.run_main("release-hold")
        self.assertEqual((status, values), (0, {"changed": "true", "deploy": "false"}), out)
        self.assertFalse((self.repo / pj.HOLD_REL).exists())
        self.assertEqual(self.git("ls-files", "--", pj.HOLD_REL), "")
        status, out, _ = self.run_main("release-hold")
        self.assertEqual(status, 1)
        self.assertIn("there is no hold to release", out)

    def test_a_rollback_takes_the_newest_entry_history_has_for_the_revision(self) -> None:
        """Review round 1: a revision two indexes named; the history's newer entry wins."""

        first, second = self.deploy("first", "second")
        again = entry(first, published="2026-10-30T05:17:00Z")
        self.write(pj.INDEX_REL, pj._canonical(index(again)))
        self.commit_all("the first revision again")
        self.write(pj.INDEX_REL, pj._canonical(index(entry(second))))
        self.commit_all("the second again")
        status, out, _ = self.run_main("rollback", "--revision", first)
        self.assertEqual(status, 0, out)
        published = json.loads((self.repo / pj.INDEX_REL).read_bytes())
        self.assertEqual(published["catalogs"], [again])

    def test_a_rollback_needs_a_deployed_revision_some_index_named(self) -> None:
        self.deploy("only")
        stray, stray_raw = revision("stray")
        self.write(f"{pj.REVISIONS_REL}/{stray}.json", stray_raw)
        self.commit_all("a revision no index named")
        missing, _ = revision("missing")
        before = self.head()
        for target, reason in (
            (stray, f"no index in this repository's history named {stray}"),
            (missing, f"{missing} is not deployed"),
            ("sha256-xyz", "rollback needs --revision sha256-<64 hex>"),
        ):
            with self.subTest(reason):
                status, out, values = self.run_main("rollback", "--revision", target)
                self.assertEqual(status, 1, out)
                self.assertIn(reason, out)
                self.assertEqual(values, {"changed": "false", "deploy": "false"})
                self.assertEqual(self.head(), before)
                self.assertFalse((self.repo / pj.HOLD_REL).exists())

    def test_a_withdrawal_publishes_an_index_with_no_catalog_and_holds(self) -> None:
        self.deploy("only")
        status, out, values = self.run_main("withdraw", "--reason", "kill", "--run", RUN)
        self.assertEqual((status, values), (0, {"changed": "true", "deploy": "true"}), out)
        published = json.loads((self.repo / pj.INDEX_REL).read_bytes())
        self.assertEqual(published["catalogs"], [])
        self.assertEqual(published["hold"]["action"], "withdraw")
        self.assertEqual(json.loads((self.repo / pj.HOLD_REL).read_text())["reason"], "kill")


class PruneTests(Case):
    def test_the_pruning_keeps_every_named_revision_and_the_newest_30(self) -> None:
        names = self.deploy(*(f"r{i:02}" for i in range(35)))
        # The index names the oldest: kept although it is not among the newest 30.
        named = index(entry(names[0]))
        self.write(pj.INDEX_REL, pj._canonical(named))
        self.commit_all("roll back to the oldest")
        removed = pj.prune(self.repo, named)
        self.assertEqual(sorted(removed), sorted(names[1:5]))
        left = sorted(p.stem for p in (self.repo / pj.REVISIONS_REL).glob("*.json"))
        self.assertEqual(left, sorted([names[0], *names[5:]]))
        self.assertEqual(len(left), pj.KEEP_LATEST + 1)

    def test_a_publication_prunes_and_commits_the_removals(self) -> None:
        names = self.deploy(*(f"r{i:02}" for i in range(31)))
        name, raw = revision("new")
        artifact = self.artifact(
            {
                f"v1/revisions/{name}.json": raw,
                "v1/index.json": json.dumps(index(entry(name))).encode("utf-8"),
            }
        )
        changed, deploy, summary = pj.act_publish(self.repo, artifact)
        self.assertTrue(changed and deploy)
        self.assertIn("Pruned 1 revision(s)", summary)
        tracked = self.git("ls-files", "--", pj.REVISIONS_REL).split()
        self.assertNotIn(f"{pj.REVISIONS_REL}/{names[0]}.json", tracked)
        self.assertIn(f"{pj.REVISIONS_REL}/{name}.json", tracked)
        self.assertEqual(len(tracked), pj.KEEP_LATEST + 1)
        self.assertTrue(self.clean())


class HoldTests(Case):
    def test_a_hold_file_that_is_not_json_still_holds(self) -> None:
        self.deploy("only")
        for content, expected in (
            ("not json {", {"action": "hold", "reason": "control/hold.json is not JSON"}),
            ("[]", {"action": "hold"}),
            # An empty object is a hold too, and its summary must say so (review round 1).
            ("{}", {}),
        ):
            with self.subTest(content):
                self.write(pj.HOLD_REL, content)
                self.commit_all("hold by hand")
                self.assertEqual(pj.read_hold(self.repo), expected)
                status, out, values = self.run_main("check-hold")
                self.assertEqual((status, values), (0, {"held": "true"}), out)
                self.assertIn("**Held.**", out)
        status, out, values = self.run_main("release-hold")
        self.assertEqual((status, values), (0, {"changed": "true", "deploy": "false"}), out)
        status, out, values = self.run_main("check-hold")
        self.assertEqual((status, values), (0, {"held": "false"}), out)

    def test_a_hold_added_by_hand_while_the_build_ran_wins_over_its_artifact(self) -> None:
        self.deploy("only")
        self.write(pj.HOLD_REL, '{"reason": "added in the GitHub UI"}\n')
        self.commit_all("hold by hand")
        name, raw = revision("new")
        artifact = self.artifact(
            {
                f"v1/revisions/{name}.json": raw,
                "v1/index.json": json.dumps(index(entry(name))).encode("utf-8"),
            }
        )
        before = self.head()
        changed, deploy, summary = pj.act_publish(self.repo, artifact)
        self.assertEqual((changed, deploy), (False, False))
        self.assertIn("added in the GitHub UI", summary)
        self.assertEqual(self.head(), before)
        self.assertFalse((self.repo / f"v1/revisions/{name}.json").exists())


class NoControlDirectoryTests(Case):
    """``_commit`` named ``control`` to ``git add`` whether or not it existed (#616 item 3)."""

    def test_a_checkout_without_control_commits_a_publication(self) -> None:
        self.deploy("only", control=False)
        self.assertFalse((self.repo / "control").exists())
        name, raw = revision("new")
        artifact = self.artifact(
            {
                f"v1/revisions/{name}.json": raw,
                "v1/index.json": json.dumps(index(entry(name))).encode("utf-8"),
            }
        )
        status, out, values = self.run_main("publish", "--artifact", str(artifact))
        self.assertEqual((status, values), (0, {"changed": "true", "deploy": "true"}), out)
        self.assertEqual(self.git("log", "-1", "--format=%s").strip(), f"publish {name}")
        self.assertTrue(self.clean())

    def test_a_hold_and_its_release_commit_without_control_readme(self) -> None:
        (only,) = self.deploy("only", control=False)
        status, out, values = self.run_main("rollback", "--revision", only)
        self.assertEqual((status, values), (0, {"changed": "true", "deploy": "true"}), out)
        self.assertEqual(self.git("ls-files", "--", "control").split(), [pj.HOLD_REL])
        # Releasing removes the directory's only file.
        status, out, values = self.run_main("release-hold")
        self.assertEqual((status, values), (0, {"changed": "true", "deploy": "false"}), out)
        self.assertEqual(self.git("ls-files", "--", "control"), "")
        self.assertTrue(self.clean())

    def test_a_tracked_directory_gone_from_disk_is_committed_as_a_removal(self) -> None:
        self.deploy("only")
        shutil.rmtree(self.repo / "control")
        self.assertTrue(pj._commit(self.repo, "control removed"))
        self.assertEqual(self.git("ls-files", "--", "control"), "")
        self.assertTrue(self.clean())
        self.assertFalse(pj._commit(self.repo, "nothing to commit"))


class StageSiteTests(Case):
    def test_only_v1_is_staged_for_pages(self) -> None:
        self.deploy("only")
        self.write(pj.HOLD_REL, "{}")
        status, out, _ = self.run_main("stage-site", "--out", str(self.tmp / "_site"))
        self.assertEqual(status, 0, out)
        staged = sorted(
            p.relative_to(self.tmp / "_site").as_posix()
            for p in (self.tmp / "_site").rglob("*")
            if p.is_file()
        )
        self.assertEqual(staged, sorted([".nojekyll", pj.INDEX_REL, *self.revisions()]))

    def revisions(self) -> list[str]:
        return [f"{pj.REVISIONS_REL}/{p.name}" for p in (self.repo / pj.REVISIONS_REL).glob("*.json")]


class WorkflowTests(unittest.TestCase):
    """#616 item 1: ``build`` checked this repository out at the run's own sha, so a run
    that waited behind a rollback read the hold as it was before the rollback, and built."""

    def test_both_jobs_check_this_repository_out_at_the_default_branch(self) -> None:
        text = (ROOT / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")
        steps = re.split(r"\n\s+- name: ", text)
        ours = [s for s in steps if "uses: actions/checkout@" in s and "path: catalog\n" in s]
        self.assertEqual(len(ours), 2, "one checkout of this repository per job")
        for step in ours:
            with self.subTest(step.splitlines()[0]):
                # Settings, not comments: a commented-out `ref:` must not pass (review round 1).
                settings = [
                    line.strip() for line in step.splitlines() if not line.strip().startswith("#")
                ]
                self.assertIn("ref: ${{ github.event.repository.default_branch }}", settings)
                self.assertIn("persist-credentials: false", settings)

    def test_a_held_build_stops_before_it_checks_anything_out_of_aelix_ai(self) -> None:
        text = (ROOT / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")
        build = text.split("\n  build:\n", 1)[1].split("\n  publish:\n", 1)[0]
        steps = re.split(r"\n\s+- name: ", build)[1:]
        names = [s.splitlines()[0] for s in steps]
        hold_at = next(i for i, n in enumerate(names) if n == "Stop while a hold exists")
        self.assertEqual(hold_at, 1, names)
        # Every later step runs only without a hold, or on the build's output, which a
        # held run never writes.
        for step in steps[hold_at + 1 :]:
            self.assertTrue(
                "if: steps.hold.outputs.held == 'false'" in step
                or "if: steps.build.outputs.state == 'publish'" in step,
                step,
            )


if __name__ == "__main__":
    unittest.main()
