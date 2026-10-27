#!/usr/bin/env python3
"""The publish job's own code: decide, check and commit what the catalog repository serves.

ADR-0264 §4.2 (handochan/aelix-ai). This file is the ONLY code the ``publish``
job runs. It is stdlib Python and lives here, in the catalog repository, so the
job that can write (``contents: write``, ``pages: write``) runs no aelix-ai code
and no ``uv``. The ``build`` job, which cannot write, runs aelix-ai's
``scripts/publish_catalog.py`` and hands over an artifact; from that artifact
this takes exactly two files, ``v1/revisions/<name>.json`` and ``v1/index.json``,
and only after checking them. Everything else it writes (``control/hold.json``,
a rollback's or a withdrawal's index, the pruning) it decides itself.

Actions (each prints Markdown for the job summary on stdout and, with
``--github-output``, ``changed=``/``deploy=``/``held=`` lines to that file):

    check-hold      is control/hold.json present? (the build job's first step)
    publish         take the build's revision and index, unless a hold exists
    rollback        point the index at a deployed revision, and hold
    withdraw        publish an index with no catalog, and hold
    release-hold    remove the hold; the next slot publishes from main
    stage-site      copy v1/ (and nothing else) into a directory for Pages

publish, rollback, withdraw and release-hold commit to the repository given by
``--repo`` when they change it; the workflow pushes without force.

Exit status: 0 done (including "held, nothing published"), 1 refused, 2 usage.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

INDEX_SCHEMA = "aelix-model-catalog-index/1"
REVISION_SCHEMA = "aelix-model-catalog/1"
INDEX_REL = "v1/index.json"
REVISIONS_REL = "v1/revisions"
HOLD_REL = "control/hold.json"
KEEP_LATEST = 30
MAX_REVISION_BYTES = 16 * 1024 * 1024
MAX_INDEX_BYTES = 64 * 1024

_REVISION_RE = re.compile(r"sha256-[0-9a-f]{64}")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_VERSION_RE = re.compile(r"[0-9]+(\.[0-9]+)*((a|b|rc)[0-9]+)?(\.post[0-9]+)?(\.dev[0-9]+)?")
# What a build artifact may hold. Only the first two are ever taken.
_ARTIFACT_EXTRAS = {"summary.md", "result.json"}


class Refused(Exception):
    """The action is refused; nothing is written."""


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if proc.returncode != 0:
        raise Refused(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


# ── shapes ─────────────────────────────────────────────────────────────


def check_index(index: Any) -> list[str]:
    """Problems with an index's shape (schema 1), as this repository publishes it."""

    problems: list[str] = []
    if not isinstance(index, dict) or index.get("schema") != INDEX_SCHEMA:
        return ["the index is not an object of schema aelix-model-catalog-index/1"]
    catalogs = index.get("catalogs")
    if not isinstance(catalogs, list):
        return ["catalogs is not a list"]
    floors: set[str] = set()
    for entry in catalogs:
        if not isinstance(entry, dict):
            problems.append("a catalog entry is not an object")
            continue
        floor = entry.get("minimumAelixVersion")
        if not isinstance(floor, str) or not _VERSION_RE.fullmatch(floor):
            problems.append(f"minimumAelixVersion {floor!r} is not a version")
        elif floor in floors:
            problems.append(f"two entries for {floor}")
        else:
            floors.add(floor)
        if not isinstance(entry.get("revision"), str) or not _REVISION_RE.fullmatch(entry["revision"]):
            problems.append("revision is not sha256-<64 hex>")
        if not isinstance(entry.get("publishedAt"), str):
            problems.append("publishedAt is missing")
        if not isinstance(entry.get("sourceCommit"), str) or not _COMMIT_RE.fullmatch(entry["sourceCommit"]):
            problems.append("sourceCommit is not a 40-hex commit")
        upstream = entry.get("upstream")
        if not (
            isinstance(upstream, dict)
            and all(
                isinstance(upstream.get(k), str) and _SHA256_RE.fullmatch(upstream[k])
                for k in ("modelsDevSha256", "openRouterSha256")
            )
            and isinstance(upstream.get("fetchedAt"), str)
        ):
            problems.append("upstream is malformed")
        for count in ("rows", "newRows"):
            value = entry.get(count)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                problems.append(f"{count} is not a count")
    if "hold" in index:
        hold = index["hold"]
        if not (
            isinstance(hold, dict)
            and hold.get("action") in ("rollback", "withdraw")
            and isinstance(hold.get("at"), str)
        ):
            problems.append("hold is malformed")
    unknown = set(index) - {"schema", "catalogs", "hold"}
    if unknown:
        problems.append(f"unknown top-level fields {sorted(unknown)}")
    return problems


def verify_revision(name: str, raw: bytes) -> list[str]:
    """The content address holds, and the bytes are a schema-1 revision."""

    if not _REVISION_RE.fullmatch(name):
        return [f"{name!r} is not a revision name"]
    if len(raw) > MAX_REVISION_BYTES:
        return [f"{name} is over {MAX_REVISION_BYTES} bytes"]
    if "sha256-" + hashlib.sha256(raw).hexdigest() != name:
        return [f"{name}: the bytes do not hash to the name"]
    try:
        document = json.loads(raw)
    except ValueError:
        return [f"{name} is not JSON"]
    if not isinstance(document, dict) or document.get("schema") != REVISION_SCHEMA:
        return [f"{name} is not a schema-1 revision"]
    return []


# ── the hold ───────────────────────────────────────────────────────────


def read_hold(repo: Path) -> dict[str, Any] | None:
    path = repo / HOLD_REL
    if not path.is_file():
        return None
    try:
        hold = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        # A hand-added file that is not JSON is still a hold: fail closed.
        return {"action": "hold", "reason": "control/hold.json is not JSON"}
    return hold if isinstance(hold, dict) else {"action": "hold"}


def _hold_summary(hold: dict[str, Any]) -> str:
    return (
        "**Held.** `control/hold.json` exists, so nothing is built or published until "
        "`release-hold`:\n\n```json\n" + json.dumps(hold, indent=2)[:4000] + "\n```\n"
    )


# ── writing ────────────────────────────────────────────────────────────


def _write(repo: Path, rel: str, data: bytes) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _commit(repo: Path, message: str) -> bool:
    _git(repo, "add", "-A", "v1", "control")
    if not _git(repo, "status", "--porcelain", "--", "v1", "control").strip():
        return False
    _git(
        repo,
        "-c",
        "user.name=aelix-catalog[bot]",
        "-c",
        "user.email=aelix-catalog@users.noreply.github.com",
        "commit",
        "-q",
        "-m",
        message,
    )
    return True


def _added_at(repo: Path, rel: str) -> int:
    stamps = _git(repo, "log", "--diff-filter=A", "--format=%ct", "--", rel).split()
    return int(stamps[-1]) if stamps else 0


def prune(repo: Path, index: dict[str, Any]) -> list[str]:
    """Remove revisions no index entry names, keeping the newest :data:`KEEP_LATEST` always."""

    named = {entry["revision"] for entry in index.get("catalogs") or []}
    directory = repo / REVISIONS_REL
    if not directory.is_dir():
        return []
    files = sorted(directory.glob("sha256-*.json"))
    tracked = [(f, _added_at(repo, f"{REVISIONS_REL}/{f.name}")) for f in files]
    newest = {f.name for f, _ in sorted(tracked, key=lambda pair: pair[1], reverse=True)[:KEEP_LATEST]}
    removed = []
    for path, _stamp in tracked:
        if path.stem in named or path.name in newest:
            continue
        path.unlink()
        removed.append(path.stem)
    return removed


# ── actions ────────────────────────────────────────────────────────────


def act_publish(repo: Path, artifact: Path) -> tuple[bool, bool, str]:
    hold = read_hold(repo)
    if hold is not None:
        return False, False, _hold_summary(hold) + "\nThe build's revision is NOT published.\n"
    files = sorted(
        p.relative_to(artifact).as_posix() for p in artifact.rglob("*") if p.is_file() or p.is_symlink()
    )
    if any(p.is_symlink() for p in artifact.rglob("*")):
        raise Refused("the artifact holds a symbolic link")
    revisions = [f for f in files if f.startswith(f"{REVISIONS_REL}/")]
    others = [f for f in files if f not in revisions and f != INDEX_REL and f not in _ARTIFACT_EXTRAS]
    if others:
        raise Refused(f"the artifact names paths the build may not supply: {others}")
    if INDEX_REL not in files or len(revisions) != 1:
        raise Refused("the artifact must hold v1/index.json and exactly one v1/revisions/<name>.json")
    rel = revisions[0]
    name = rel[len(REVISIONS_REL) + 1 :]
    if not name.endswith(".json") or "/" in name:
        raise Refused(f"{rel} is not a revision file")
    name = name[: -len(".json")]
    raw = (artifact / rel).read_bytes()
    problems = verify_revision(name, raw)
    index_raw = (artifact / INDEX_REL).read_bytes()
    if len(index_raw) > MAX_INDEX_BYTES:
        problems.append("the index is over 64 KiB")
    try:
        index = json.loads(index_raw)
    except ValueError:
        index = None
    problems += check_index(index)
    if not problems:
        assert isinstance(index, dict)
        if "hold" in index:
            problems.append("a publication's index carries a hold")
        named = [entry["revision"] for entry in index["catalogs"]]
        if name not in named:
            problems.append(f"the index does not name {name}")
        for other in named:
            if other != name and not (repo / REVISIONS_REL / f"{other}.json").is_file():
                problems.append(f"the index names {other}, which is not deployed")
    if problems:
        raise Refused("; ".join(problems))
    assert isinstance(index, dict)
    _write(repo, f"{REVISIONS_REL}/{name}.json", raw)
    _write(repo, INDEX_REL, _canonical(index))
    pruned = prune(repo, index)
    changed = _commit(repo, f"publish {name}")
    summary = f"Published `{name}` ({len(raw):,} bytes)." if changed else f"`{name}` is already current."
    if pruned:
        summary += f" Pruned {len(pruned)} revision(s) no entry names."
    return changed, changed, summary + "\n"


def _history_entry(repo: Path, revision: str) -> dict[str, Any] | None:
    """The newest index entry this repository's history ever published for ``revision``."""

    for sha in _git(repo, "log", "--format=%H", "--", INDEX_REL).split():
        try:
            index = json.loads(_git(repo, "show", f"{sha}:{INDEX_REL}"))
        except (Refused, ValueError):
            continue
        for entry in index.get("catalogs") or []:
            if entry.get("revision") == revision:
                return entry
    return None


def act_hold(repo: Path, action: str, revision: str, reason: str, run: str) -> tuple[bool, bool, str]:
    at = _now()
    current = json.loads((repo / INDEX_REL).read_bytes()) if (repo / INDEX_REL).is_file() else None
    if action == "rollback":
        if not _REVISION_RE.fullmatch(revision or ""):
            raise Refused("rollback needs --revision sha256-<64 hex>")
        path = repo / REVISIONS_REL / f"{revision}.json"
        if not path.is_file():
            raise Refused(f"{revision} is not deployed")
        problems = verify_revision(revision, path.read_bytes())
        if problems:
            raise Refused("; ".join(problems))
        entry = _history_entry(repo, revision)
        if entry is None:
            raise Refused(f"no index in this repository's history named {revision}")
        # The entry for that floor is replaced where it stands; other floors keep theirs.
        catalogs = [dict(e) for e in (current or {}).get("catalogs") or []]
        floors = [e.get("minimumAelixVersion") for e in catalogs]
        if entry.get("minimumAelixVersion") in floors:
            catalogs[floors.index(entry.get("minimumAelixVersion"))] = entry
        else:
            catalogs.append(entry)
        index: dict[str, Any] = {
            "schema": INDEX_SCHEMA,
            "catalogs": catalogs,
            "hold": {"action": "rollback", "at": at},
        }
        hold = {"action": "rollback", "revision": revision, "reason": reason, "run": run, "at": at}
    else:
        index = {"schema": INDEX_SCHEMA, "catalogs": [], "hold": {"action": "withdraw", "at": at}}
        hold = {"action": "withdraw", "reason": reason, "run": run, "at": at}
    problems = check_index(index)
    if problems:
        raise Refused("; ".join(problems))
    _write(repo, INDEX_REL, _canonical(index))
    _write(repo, HOLD_REL, (json.dumps(hold, indent=2) + "\n").encode("utf-8"))
    changed = _commit(repo, f"{action}{' to ' + revision if revision else ''}: hold")
    what = f"rolled back to `{revision}`" if action == "rollback" else "withdrew the catalog"
    return changed, changed, f"The index {what} and carries `hold`; `control/hold.json` holds it.\n"


def act_release(repo: Path) -> tuple[bool, bool, str]:
    if read_hold(repo) is None:
        raise Refused("there is no hold to release")
    (repo / HOLD_REL).unlink()
    changed = _commit(repo, "release-hold")
    return changed, False, "The hold is released; the next slot publishes from main.\n"


def stage_site(repo: Path, out: Path) -> None:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    source = repo / "v1"
    if source.is_dir():
        shutil.copytree(source, out / "v1")
    (out / ".nojekyll").write_bytes(b"")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument(
        "action",
        choices=["check-hold", "publish", "rollback", "withdraw", "release-hold", "stage-site"],
    )
    parser.add_argument("--repo", required=True, help="this repository's checkout")
    parser.add_argument("--artifact", help="publish: the build job's artifact directory")
    parser.add_argument("--revision", default="", help="rollback: the revision to point at")
    parser.add_argument("--reason", default="", help="rollback / withdraw: why")
    parser.add_argument("--run", default="", help="the run's URL, recorded in the hold")
    parser.add_argument("--out", help="stage-site: the directory to fill")
    parser.add_argument("--github-output", help="append changed=/deploy=/held= lines here")
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()

    def output(**values: object) -> None:
        if args.github_output:
            with open(args.github_output, "a", encoding="utf-8") as handle:
                for key, value in values.items():
                    handle.write(f"{key}={str(value).lower()}\n")

    try:
        if args.action == "check-hold":
            hold = read_hold(repo)
            output(held=hold is not None)
            print(_hold_summary(hold) if hold else "No hold.")
            return 0
        if args.action == "stage-site":
            if not args.out:
                raise Refused("stage-site needs --out")
            stage_site(repo, Path(args.out).resolve())
            print(f"Staged `v1/` into `{args.out}`.")
            return 0
        if args.action == "publish":
            if not args.artifact:
                raise Refused("publish needs --artifact")
            changed, deploy, summary = act_publish(repo, Path(args.artifact).resolve())
        elif args.action in ("rollback", "withdraw"):
            changed, deploy, summary = act_hold(repo, args.action, args.revision, args.reason, args.run)
        else:
            changed, deploy, summary = act_release(repo)
    except Refused as exc:
        output(changed=False, deploy=False)
        print(f"**Refused - nothing is published.** {exc}")
        return 1
    output(changed=changed, deploy=deploy)
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
