"""
# Gradescope

Read-only access to the student's Gradescope dashboard, for what Canvas cannot
tell: whether work handed in on Gradescope was submitted, and its score.

## 1. What This Module Does

`client` fetches the account page and each course page with the student's
session cookie and parses them into a `snapshot.Snapshot`, written to
`state/gradescope.json`. `snapshot` reads that file back and turns an entry
into the status string and title key `deadlines` and the graph use.

## 2. Why This Module Exists

Courses that collect work on Gradescope leave a `not_graded` placeholder on
Canvas, so Canvas reports every such assignment as `unsubmitted` forever.
Gradescope has no public student API, so the dashboard HTML is the only
source.

## 3. How It Fits in the Architecture

`client` is the second module that speaks HTTP, after `canvas.client`, and
obeys the same rule: GET only, refused at the chokepoint. `snapshot` is a leaf
that imports no HTTP, so `deadlines` can read it without pulling in httpx.
"""
