# Conversation scopes

Add `scopes:` to Belle's [configuration file](config.md). Each rule matches requests and sets `delivery`, `agent`, `permissions`, or `clicks`. The Slack guide describes the [delivery](platforms/slack.md#delivery-settings) and [agent](platforms/slack.md#agent-settings) settings. The connector supplies defaults. `scopes: []` changes nothing.

Connectors declare their supported axes and settings. A match key or a setting that a connector does not support produces a startup warning. Belle does not start with a `permissions` or `clicks` rule whose match key the connector cannot fill, because that rule would not apply as written. Slack supports all four sections. A rule with no `channel` applies on every connector that runs, so Belle checks each of its values as each of these connectors checks it.

`belle_agent.scopes.settings` composes delivery and agent overrides, including
per-sender and per-workspace rules. Connectors supply their mode parser and
event policy vocabulary. Slack keeps its legacy group-chat modes and mention
subscription rules in its adapter. Empty values override inherited settings.
Only absent values inherit.

## Start with a rule

This rule answers links in one Slack channel and requires approval for `bash`:

```yaml
scopes:
  - match: {channel: slack, chat: "C000000AAAA"}
    delivery:
      mode: [mention, url]
      prompt_append: "Fetch a linked page before analyzing it."
    permissions:
      tools: {bash: ask}
```

Quote conversation, workspace, and user IDs. For Slack, `chat_type` accepts `channel`, `group`, `im`, and `mpim`. Belle does not start with a `chat_type` that the connector does not have.

## Match requests

A `match` can use these keys:

| Key | Selects | Form |
| --- | --- | --- |
| `channel` | Connector, such as `slack` | One name |
| `workspace` | One installation of that connector. On Slack, use the team ID. On Mattermost, use the team ID or `direct`, after `<server name>:` where the section lists `servers`. On Matrix, use the account's name: `default`, or the `name` of an entry of `accounts` | One quoted ID |
| `chat` | One conversation | One quoted ID |
| `chat_type` | One or more kinds of conversation | One name or a list |
| `user` | Message senders | List of quoted IDs |
| `role` | Senders named in top-level `roles:` | One name or a list |
| `not` | Excludes senders from the rule | Mapping with `user` or `role` |

Axes combine with AND. Values within `chat_type`, `user`, and `role` combine with OR. `not` excludes people. A rule cannot name both `chat` and `chat_type`. Include `channel` when matching platform-specific IDs or kinds.

A rule whose `channel` names a platform that is off in this process does not apply. Belle logs one line for each such platform, with the number of these rules. Belle does not start with a `channel` that names no platform and no connector.

Define reusable identities beside `scopes:` in the Belle config file:

```yaml
people:
  alice: {slack: "U000000AAAA"}
  bob: {slack: "U000000BBBB"}

roles:
  operators: [alice, bob]

scopes:
  - match:
      channel: slack
      chat: "C000000AAAA"
      not: {role: operators}
    delivery: {mode: [mention]}
```

This rule requires a mention except from operators. Belle does not start if an entry under `people` or `roles` is not valid. The error names each such entry. Write each ID in quotes, because YAML reads an unquoted number as a number. Write each name once. Belle removes the spaces around a name, so `alice` and `alice ` are the same name. A role is a list of one or more names from `people`. A role cannot contain a role.

Belle does not start with a rule that names a role that is not in `roles`. Roles without IDs on that platform match nobody. Unidentified senders match no positive identity list and remain subject to `not` restrictions.

## Resolve overlapping rules

Scope matching applies connector defaults, then orders matching scopes by specificity:

1. A named `channel` beats an unspecified channel.
2. A named `workspace` beats an unspecified workspace.
3. An exact `chat` beats `chat_type`, which beats an unspecified conversation.
4. Explicit `user` IDs beat a `role` or a `not`-only rule, which beat an unspecified sender.

The comparison follows this order. A workspace rule beats a chat rule without that workspace. A chat rule beats a sender rule without that chat. Include the broader axes when a narrower rule must win. At equal specificity, the later rule wins.

Settings compose independently. Scalars and unsigned lists replace inherited values. Lists with only `+` or `-` entries change the inherited set, except `agent.skills` and `agent.subagents`, which take unsigned names only. Do not mix signed and unsigned entries. Mappings merge by key. `prompt` replaces text. `prompt_append` adds a paragraph.

`permissions.tools` has a separate rule: matching restrictions combine to the strictest level, regardless of file order.

With Belle's own launcher, a tool call belongs to the turn that requested it. If that turn ends or stops while Belle checks whether approval is needed, Belle refuses the call.

Permission narrowing uses OpenJiuwen when a host applies `permissions.tools`. On JiuwenSwarm, Belle refuses a tool call whose conversation is unknown while a scope sets `permissions.tools`. See [Tool permissions of a scope](integrations/jiuwenswarm.md#tool-permissions-of-a-scope).

`agent.skills.unavailable` lists skills that a turn cannot use. A turn can use every other skill that `agent.skills.available` lists. Without `available`, that is every installed skill, including the skills that are installed later. Use `unavailable` to remove a skill where it has no use, for example a skill for another platform. A narrower rule replaces the inherited list, and `[]` clears it. The other `agent.skills` keys stay inherited. A skill cannot be both required and unavailable. Belle does not start with a rule that names one skill in `required` and in `unavailable`. Belle refuses a turn where the composed scope names one skill in both, and logs a warning that names the two keys.

`agent.skills.required` also works on a backend that cannot attach a skill to a turn. Belle then adds the `SKILL.md` text of each required skill to the turn's instructions. Belle has this text only for the skills it packages. A required skill that Belle does not package produces a startup warning.

## Check a configuration

Reload the host after editing the Belle config file. Belle does not start with an invalid rule. The error names each invalid entry by its index, for example `scopes[2].permissions.tools.bash`, and gives the form that it expects. Check startup logs for `scopes:` warnings about valid rules, for example a setting that a connector does not support.

Test a matching and nonmatching request for each rule. For Slack privacy rules, test a public channel, private channel, and DM. Check connector logs for the effective rules.
