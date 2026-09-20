"""
# Canvas Mirror

Read-only access to canvas.mit.edu, and the incremental state that makes a
second sync download nothing.

## 1. What This Module Does

`client` is the HTTP layer: bearer auth, `Link`-header pagination, serial
requests, and backoff on throttling. `manifest` is the DuckDB table keyed on a
file's Canvas `uuid`. `sync` walks courses, folders, files, modules, pages,
assignments and announcements and writes them verbatim into `_canvas/`.

## 2. Why This Module Exists

Canvas holds graded work, so the access here is **read-only by enforcement,
not by convention**: `CanvasClient._request` refuses any method outside
`{GET, HEAD}` with `CanvasWriteRefused`, and a test walks the AST of every
module in the package looking for a write call.

## 3. How It Fits in the Architecture

This package is the only one that speaks HTTP, and `_canvas/` is the source of
truth everything downstream reads. `filing` copies *out* of the mirror and
never into it; `knowledge` extracts from it; `schedule` reads the `_meta/`
JSON it writes. Deleting `_canvas/` entirely is safe -- a full sync rebuilds
it.

## 4. Key Concepts

**The manifest is the diff.** Canvas documents no ETag or
`If-Modified-Since` support, so incrementality is ours to implement: `uuid` ->
`updated_at` + `sha256`. Nothing is ever derived from "time since the last
run", which is what makes a missed scheduled run cost nothing.

**Pre-signed URLs expire.** A file's `url` is re-resolved via `/files/:id` at
download time and never cached.

**403 is ambiguous.** Canvas returns it both for throttling and for a course
that has hidden its Files tab; both are handled, the latter by falling back to
walking Modules.
"""
