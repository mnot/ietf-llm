"""Entry point for a gather child in tests, run as `python -I fake_gather_child.py`.

It is the real `ietf_llm.gather.child.main` -- the progress pipe, the exit codes,
the error record -- and the real `run_gather` and `_gather_one`, so the ETag
binding and flush, and the IMAP UIDVALIDITY handling, run in a separate process
exactly as they do under the MCP runner. Only the network and the stage bodies
are replaced:

  - Datatracker answers come from `_governed_get`, which logs each request's
    headers to `FAKE_LOG` and replies with the ETag in `FAKE_ETAG`.
  - The IMAP attempt is `_attempt`: the real `_reconcile_uidvalidity` against a
    server reporting `FAKE_UIDVALIDITY`, then the UIDs in `FAKE_UIDS`.
  - The stages are `_stages`: one Datatracker request, one real per-list mail
    sync, and a digest file for the version to publish. `FAKE_OUTCOME=skipped`
    returns SKIPPED instead; `FAKE_RAISE` raises.
  - Preflight is stubbed too (on the cloud backend it reads the config store
    over the network) unless `FAKE_REAL_PREFLIGHT` is set, for a local-backend
    child that should reach SKIPPED the way production does.
"""

import json
import os

from ietf_llm.gather import child, sequencer
from ietf_llm.gather.lifecycle import GatherOutcome
from ietf_llm.gather.sources import datatracker, mbox
from ietf_llm.log import Verbosity
from ietf_llm.paths import get_cache_dir

REQUEST_PATH = "/api/v1/group/group/?acronym=tls"


class _Response:
    status_code = 200

    def __init__(self, etag: str) -> None:
        self.headers = {"ETag": etag}
        self.text = '{"objects": []}'
        self.content = self.text.encode()

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {"objects": []}


def _governed_get(url, headers=None, timeout=None):  # type: ignore[no-untyped-def]
    with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as log:
        log.write(json.dumps({"url": url, "headers": dict(headers or {})}) + "\n")
    return _Response(os.environ.get("FAKE_ETAG", "e1"))


class _Mail:
    def response(self, name):  # type: ignore[no-untyped-def]
        return (name, [os.environ["FAKE_UIDVALIDITY"].encode()])


def _attempt(list_name, months, cache_dir, verbose, on_progress):  # type: ignore[no-untyped-def]
    mbox._reconcile_uidvalidity(_Mail(), cache_dir, verbose)
    uids = [u for u in os.environ.get("FAKE_UIDS", "").split(",") if u]
    new = 0
    for uid in uids:
        path = os.path.join(cache_dir, f"{uid}.eml")
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as eml:
                eml.write(f"Message-Id: <{uid}@x>\n\nbody {uid}\n")
            new += 1
    return uids, new, None


def _stages(args, verbosity, progress, note_fn, pre):  # type: ignore[no-untyped-def]
    if os.environ.get("FAKE_OUTCOME") == "skipped":
        return GatherOutcome.SKIPPED
    if os.environ.get("FAKE_RAISE"):
        raise ValueError(os.environ["FAKE_RAISE"])
    progress("charter", 1, 2, None)
    datatracker._get_json(REQUEST_PATH)
    mbox._sync_one_list(args.wg, args.wg, 12, Verbosity.QUIET)
    digests = os.path.join(get_cache_dir(), args.wg, "files", "digests")
    os.makedirs(digests, exist_ok=True)
    with open(os.path.join(digests, "index.md"), "w", encoding="utf-8") as index:
        index.write("# Overview\n")
    return GatherOutcome.GATHERED


datatracker.governed_get = _governed_get
mbox._imap_sync_attempt = _attempt
if not os.environ.get("FAKE_REAL_PREFLIGHT"):
    sequencer.preflight = lambda *a, **k: sequencer.Preflight(None, {}, False, False)
sequencer._gather_stages = _stages

if __name__ == "__main__":
    child.main()
