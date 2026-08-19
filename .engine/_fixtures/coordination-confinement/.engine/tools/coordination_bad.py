#!/usr/bin/env python3
"""A deliberately NON-confined coordination file — the negative fixture for coordination-confinement
(StarshipSuperjam/engine-template#939). It is never imported or run; the confinement check reads it as text and MUST flag each
authority reach below. If a future change makes any of these pass the check, the check has regressed and the
hard-check-bite meta-check goes red here."""
import ack_status  # the commit-status writer — forbidden import


def do_bad(transport, repo, n):
    transport("POST", f"/repos/{repo}/pulls/{n}/merge", {})              # merge endpoint
    transport("POST", f"/repos/{repo}/issues/{n}/labels", {"labels": ["x"]})  # label endpoint
    transport("PATCH", f"/repos/{repo}/issues/{n}", {"body": "x"})       # issue-body edit (non-comment write)
    ack_status.post_ack_status(repo, n, "success")                       # a commit-status write
