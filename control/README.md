# control/

`hold.json` here stops every publication until it is removed (ADR-0264 §4.2,
"The hold"). It is never deployed to Pages.

- `rollback` and `withdraw` dispatches write it in the same commit as the index they
  publish: `{"action", "revision" (rollback only), "reason", "run", "at"}`.
- You may also add it by hand in the GitHub UI, with any content (a file that is not
  JSON still holds). The publish job re-reads it at the branch head right before it
  commits, so a hand-added hold also wins over a build that started before it.
- Only the `release-hold` dispatch (or deleting the file) removes it. The next slot
  then publishes from aelix-ai's `main`.
