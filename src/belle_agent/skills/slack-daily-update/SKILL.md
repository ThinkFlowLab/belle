---
name: slack-daily-update
description: Create a daily overview of the team's work based on a Slack list
metadata: {"openclaw":{"emoji":"📋","requires":{"bins":["python3"],"env":["SLACK_BOT_TOKEN","SLACK_LIST_ID"]}}}
---

# Slack Daily Update

Create a daily update of the team's work when a user requests one.

## Usage

Run `python3 scripts/daily_update.py --skip-idle --block-kit`. Send its Block Kit output without added commentary or changes.
