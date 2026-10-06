"""
# Forum

The Homework 3 forum agent's tools: one Canvas discussion, the course
knowledge it may share, and Google Scholar.

## 1. What This Module Does

`discussion` reads the Homework 3 agent forum and applies the agent's decision
(post, or record a skip) inside fixed bounds. `knowledge` is the agent's view of
the course knowledge base, lecture material only. `scholar` finds recent papers.

## 2. Why This Module Exists

Homework 3 asks for an agent that takes part in a Canvas discussion on a
schedule, remembers what it has seen and done, and stops safely. It is the only
part of mitsync that writes to Canvas. Keeping it in its own package, driven by
its own OpenClaw agent through its own wrapper (`bin/mitsync-forum`), keeps
that write away from everything else: the main agent's wrapper refuses
`forum`, and the forum agent can reach nothing but this package.

## 3. How It Fits in the Architecture

A capability package beside `canvas`, `filing`, `schedule` and `knowledge`.
It reads Canvas through `canvas.client` and the graph through
`knowledge.graph`, and nothing imports it but `cli`.

## 4. Key Concepts

**Data tools, again.** As everywhere in mitsync, the code fetches, checks and
delivers. Whether to post, and what, is the agent's judgment, handed back as a
decision file that `forum act` validates. The boundaries that can be checked
mechanically are checked there, whatever the decision file says.
"""
