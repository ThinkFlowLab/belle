---
name: session-work
description: Choose whether requested work should continue in the current turn, a detached session, or a retained session when the available tools support it.
---

# Session work

Use the current turn for short answers and work that needs back-and-forth,
needs more input, or must finish as part of the current response.

For substantial, self-contained work that will take sustained effort, use
`session_start` by default when that tool is available. This fits work that can
proceed from the request while the sender continues the conversation. The
current turn can end while the work runs. Pass the task, constraints, and
needed context in its prompt. Report the returned session ID when it helps the
sender check or continue the work. If `session_start` is unavailable, do the
work in the current turn.

Continue a retained session when the request clearly extends or revises earlier
work and the current tools expose that capability. Use its session ID. The
new run continues the conversation of that session. A session of a scheduled
job can be continued as well.

If the conversation suggests a relevant detached session but gives no ID, use
`session_list` to find candidates. The list has no task text, so continue only
when the session matches the visible context clearly. Ask which session the
sender means when the match is unclear. Do not list sessions only because new
work could run independently. If the tool is absent, continue in the current
turn or start a new detached session when the request allows it. Do not assume
another backend or platform has the same capability.
