# Security

Report a vulnerability through GitHub's private advisory form for this
repository (Security tab, "Report a vulnerability"). Include the platform and
harness involved, the message or input that triggers it, and what an attacker
gains.

Belle reads messages, files and links from everyone in the conversations it
joins, and holds the tokens of its chat platforms and its model provider.
[Conversation scopes](docs/scopes.md) set what each conversation may use, as
far as the harness supports each setting: the
[feature matrix](docs/feature-matrix.md#harnesses) lists the gaps. The
[Second Brain](docs/second-brain.md#privacy-and-audience) recalls a document
in any conversation, so its channel selection decides who can see what it
ingested. The reports of most interest:

- Content reaching a person or room beyond what these pages describe.
- A turn using a tool, skill or permission that its scope denies, on a
  harness that supports that setting.
- A platform token or model key leaking into a reply, a log or a file.
- An approved action running outside the turn that asked for it.
