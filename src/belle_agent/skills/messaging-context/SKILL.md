---
name: messaging-context
description: "Check chat history when the model session may omit earlier messages or actions. Use when an answer or action depends on events outside the visible session."
---

# Messaging context

The model session is only a partial view of the chat room. It may start after
earlier room activity, expire, or omit work done in another turn or a detached
session. The bot may have joined after earlier room activity. The session may
lack messages, files, reactions, and even your own earlier posts or actions.
Memory can also be incomplete. Do not treat an absence from the session as
proof that something never happened in the room.

Do not read history on every turn. When the current message and supplied
context are enough, proceed. If an answer or action depends on an earlier
message, decision, assignment, file, link, or action, check the relevant room
record first. Start with the referenced message or thread, or a bounded span
of nearby history. Search the platform when you do not know where the item is.
Use a focused query before a wider search. Check later replies for changes to
an earlier decision. If you cannot access the needed record, say what you
could not check.
