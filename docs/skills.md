# Skills and test probes

## Install skills

Clone this repository onto the JiuwenSwarm host.
In the web UI, open **Skills → Import local skill**.
Enter the absolute server-side path of a skill directory, such as `/path/to/your-clone/skills/messaging-context`.
Import each skill you need.
Find it under **My Skills**.
The [JiuwenSwarm example](integrations/jiuwenswarm.md#4-belle-config) requires `messaging-context`, `group-chat-participation`, `belle-command-help`, `slack-app-home`, and `welcome-onboarding`. The scopes in [examples/config/belle-config.yaml](../examples/config/belle-config.yaml) also require `session-work`.
The other reusable skills are optional.
Install test probes separately from `contrib/` when you need them.

Importing a skill adds it to the global library. The agent can load it in any
conversation unless a scope restricts `skills.available` or names the skill in
`skills.unavailable`.

## Reusable skills

Reusable skills are in [`src/belle_agent/skills/`](../src/belle_agent/skills/).
The root [`skills/`](../skills/) path is a symlink to that directory.

General guidance on IM platforms:

| Skill | What it does |
|-------|--------------|
| `messaging-context` | Check relevant room history and actions when the model session lacks needed context |
| `group-chat-participation` | Identify addressees and decide whether to answer, react, or stay silent in a group |
| `session-work` | Choose whether work belongs in the current turn, a detached session, or a retained session when the backend supports continuation |
| `belle-command-help` | Suggest the exact help command supplied by the current platform. Default forms are `/belle help` for slash commands and `!belle help` for text commands |
| `chat-digest` | Turn a span of conversation history into a digest of what changed, was decided, is blocked or is still open |
| `typed-decision-answers` | Read the typed answers of a decision model, with their probabilities and confidence |

For Slack:

| Skill | What it does |
|-------|--------------|
| `slack-block-kit-rendering` | Describe the rich blocks this connector can render |
| `slack-app-home` | Explain the private App Home when it helps with a request; change it only when the person asks |
| `slack-daily-update` | Build a daily overview of what each person started, has in progress, and finished, read from a Slack list. Needs `SLACK_BOT_TOKEN` and `SLACK_LIST_ID` in the environment |

Misc. for teams:

| Skill | What it does |
|-------|--------------|
| `reading-digest` | When someone shares reading material, post a brief overview. Add a summary for long pieces. Ignore other links. |
| `pr-tracker` | Track linked pull requests and changes since the last delivered report |
| `welcome-onboarding` | Send a private welcome message to a person who has just joined the team, using the agent's memory, documents, and other available sources |

### Test probes

`contrib/` contains temporary fixtures for testing configuration and connector behavior together.
Each probe has a fixed marker or fixture.
Compare a scope that matches with one that does not, then repeat the check after code or configuration changes to catch regressions.
The two skill probes test `skills.available` and `skills.required` separately.
Cedar and Lantern make `agent.expert` selection visible.

Install only the probes needed for a test.
Remove them afterwards so they do not appear in the normal skill and expert catalogs.
If a marker is missing, check scope resolution and runtime logs.

- `contrib/skills/slack-block-rendering-probe`: check rich-block rendering with fixed fixtures.
- `contrib/skills/slack-question-input-probe`: check question inputs and submission.
- `contrib/skills/scope-available-probe`: check explicit skill loading.
- `contrib/skills/scope-required-probe`: check required skill instructions.
- `contrib/experts/scope-expert-cedar-probe` and `contrib/experts/scope-expert-lantern-probe`: check expert selection with distinct reply markers.

Import a skill probe from its directory, as described above.
Install each expert probe as a local agent template.
Select an expert with a scope.
Then ask, "Which temporary expert is active?"
Each expert's README gives its expected marker.
The probes do not require specific Slack channels.
