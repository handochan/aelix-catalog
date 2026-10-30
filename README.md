# aelix-catalog

The hosted copy of [aelix](https://github.com/handochan/aelix-ai)'s model catalog,
published by this repository's workflow to GitHub Pages:

    https://handochan.github.io/aelix-catalog/v1/index.json

The design is aelix-ai's ADR-0264 (`docs/decisions/0264-*.md`). In short:

- **What is published.** Content-addressed revisions, `v1/revisions/sha256-<hex>.json`,
  of aelix's own corrected catalog, and `v1/index.json`, which names the current one
  per minimum aelix version. A row carries facts only (name, reasoning, thinking map,
  input, cost, context window, output cap) plus `family` and `releaseDate`. It never
  carries `api`, `baseUrl`, `headers`, `compat` or `provider`: a client takes those
  from its own shipped catalog, so nothing here can send a request anywhere a
  release does not already send it, or add a provider.
- **When.** Weekdays at 10:17, 12:17 and 14:17 KST, from the newest aelix-ai commit
  whose `ci.yml` push run on `main` succeeded, and by hand. A slot with nothing new
  commits nothing. All logic lives in aelix-ai (`scripts/publish_catalog.py`); the
  only code here is `tools/publish_job.py`, which the job that can write runs, and
  its stdlib tests (`python3 -m unittest discover -s tests`), which
  `.github/workflows/test.yml` runs on a push to `main` or a pull request that changes
  `tools/`, `tests/` or a workflow.
- **Repair.** `Actions -> Publish the model catalog -> Run workflow`:
  - `rollback` with `revision=sha256-...` points the index at a deployed revision;
  - `withdraw` publishes an index with no catalog (the kill switch);
  - both write `control/hold.json`, and while it exists every slot and every
    `publish` dispatch publishes nothing. Only `release-hold` removes it (or delete
    the file by hand). A rollback or withdrawal counts as done only when its run
    succeeded and its summary shows the new index and the hold; dispatch a cancelled
    one again.
- **History.** This repository's commits are the publication log. A revision stays
  deployed while an index entry names it, and the newest 30 always stay.

`control/` is never deployed; only `v1/` is.

Scheduled workflows in a public repository stop after 60 days without activity;
re-enable them from the Actions tab if that happens. Clients keep the last revision
meanwhile.
