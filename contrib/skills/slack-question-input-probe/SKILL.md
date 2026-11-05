---
name: slack-question-input-probe
description: Test Slack ask_user input cards and submitted answers with fixed text, choice, date/time, and optional-field cases. Use only when explicitly asked to probe Slack question inputs.
---

# Slack Question Input Probe

Run one named case per request. Call `ask_user` with the exact arguments below. Do not answer the question yourself. After submission, quote the tool's answer without changing its values. The person can compare the card and answer with the expected behavior.

## Text

```json
{"query":"Enter the fixture word.","questions":[{"inputs":[{"type":"text","name":"word","label":"Fixture word","placeholder":"cedar"}]}]}
```

The card has one text field and a Submit button. An empty submit stays pending and gives the clicker a private error. Entering `cedar` returns `cedar` in the answer.

## Choices

```json
{"query":"Select the fixture choices.","questions":[{"inputs":[{"type":"select","name":"mode","label":"Mode","options":[{"label":"Alpha","value":"alpha"},{"label":"Beta","value":"beta"}]},{"type":"multi_select","name":"colors","label":"Colors","options":[{"label":"Red","value":"red"},{"label":"Blue","value":"blue"},{"label":"Green","value":"green"}]}]}]}
```

The card has a single-choice menu and a multi-choice menu. Select Alpha, Red, and Blue. The answer contains `{"mode":"alpha","colors":["red","blue"]}`. The order of the color values follows the submitted selection.

## Date and time

```json
{"query":"Choose a fixture date and time.","questions":[{"inputs":[{"type":"datetime","name":"when","label":"When"}]}]}
```

The card has separate date and time pickers. An incomplete submit stays pending. A complete answer is one ISO 8601 instant. It includes the answerer's IANA timezone in brackets when Slack supplies one.

## Optional field

```json
{"query":"Enter the fixture date; the note is optional.","questions":[{"inputs":[{"type":"date","name":"day","label":"Day"},{"type":"text","name":"note","label":"Note","optional":true}]}]}
```

Select a date and leave Note empty. Submit succeeds. The answer contains a JSON object with `day` and no `note`. Submitting both fields empty stays pending.

## Withdrawal

Start the Text case again. Before pressing Submit, send a new ordinary message in the same thread. The pending question closes. The old card must not resume it. Report the result without inventing an answer.
