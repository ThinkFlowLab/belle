# Mattermost setup for Belle

The image beside this file sets up Belle on a Mattermost server with one command. It calls the server's REST API v4 with an admin's personal access token. It needs no access to the server's host. The [Mattermost platform guide](../../docs/platforms/mattermost.md) gives the same steps in the web pages.

## One command

`belle-mattermost-setup`, the image's entry point, does each step of [What setup does](#what-setup-does) and writes `out/mattermost.env`. A person gives an admin's token, the team, the channels and the address of Belle's listener.

1. Build the image, from the repository root:

   ```sh
   podman build --format oci -t localhost/belle:mattermost-helper-local -f containers/mattermost-helper/Containerfile \
     --ignorefile containers/mattermost-helper/.containerignore .
   ```

2. Make a directory, and an admin env file in it with one line, `MATTERMOST_ADMIN_TOKEN=<token>`:

   ```sh
   install -d -m 700 ~/mm-belle ~/mm-belle/out
   install -m 600 /dev/null ~/mm-belle/admin.env
   "${EDITOR:-vi}" ~/mm-belle/admin.env
   ```

3. Run the setup. Give the server and the team's name. Give one `--channel` for each channel Belle is to take part in. Give the address at which the server reaches Belle's listener:

   ```sh
   cd ~/mm-belle
   podman run --rm --userns=keep-id:uid=10001,gid=10001 \
     --read-only --cap-drop=ALL --security-opt=no-new-privileges \
     --memory=128m --cpus=1 --pids-limit=64 \
     -v "$PWD/admin.env:/secrets/admin.env:ro" -v "$PWD/out:/out:rw" \
     localhost/belle:mattermost-helper-local \
     --url https://chat.example.com --team my-team --channel town-square \
     --listener http://belle:18400
   ```

The setup prints one line for each step and ends with `wrote /out/mattermost.env`. Run it again at any time. It makes only what is missing, and it prints `exists`, `member` or `unchanged` for the rest.

The admin token comes only from the mounted file. The setup does not print it and does not write it. After the setup, the admin can revoke the token.

### Options

| Option | Value |
| --- | --- |
| `--url` | The server, `http` or `https`, such as `https://chat.example.com`. Required. |
| `--team` | The team's name, as in the team's URL. Required. |
| `--channel` | A channel's name, as in the channel's URL. Give it once for each channel. Optional: the bot can always receive direct messages. |
| `--listener` | The address at which the server reaches Belle's action listener, such as `http://belle:18400`. Required. The setup adds `/command` for the slash command. |
| `--bot-name` | The bot's user name. Default `belle`. |

### What setup writes

`out/mattermost.env`, mode 600, with four lines:

| Variable | Value |
| --- | --- |
| `MATTERMOST_URL` | `--url` |
| `MATTERMOST_BOT_TOKEN` | The bot's access token |
| `MATTERMOST_COMMAND_TOKEN` | The token of the `/belle` slash command |
| `MATTERMOST_ACTIONS_URL` | `--listener` |

Keep `out/`. A later run reads the bot token from `out/mattermost.env`. It keeps the token while the server accepts it. Without the file, or with a token that the server refuses, the run makes a new bot token. The server keeps the old tokens of the bot until an admin revokes them.

### Belle with mattermost.env

Give the file to Belle's container with `--env-file out/mattermost.env`. For the settings that read these variables, see [Settings](../../docs/platforms/mattermost.md#settings).

## What setup does

Each step first looks for what it makes. The setup stops at the first step that fails.

| Step | REST call | Who can do it |
| --- | --- | --- |
| Admin token | `GET /users/me` | Any account with a personal access token |
| Team | `GET /teams/name/{team}` | A member of the team, or a system admin |
| Bot account | `GET /users/username/{bot}`, else `POST /bots`. The setup enables a disabled bot again. | A system admin |
| Bot token | `GET /users/me` with the token of `out/mattermost.env`, else `POST /users/{bot}/tokens` | A system admin |
| Team membership | `GET`, else `POST /teams/{team}/members` | A system admin or a team admin |
| Channel membership, for each `--channel` | `GET`, else `POST /channels/{channel}/members`. A team admin cannot read the members of a channel it is not in. The setup then adds the bot and prints `added or kept`: an add of a member changes nothing. | A system admin or a team admin |
| Slash command `/belle` | `GET /commands`, else `POST /commands`. A command with another request URL gets `--listener` + `/command`. | A system admin or a team admin |
| Listener check | `GET /config` | A system admin. With another account the setup skips it. |

The "Who can do it" column gives the default permissions of the server. A system admin can change them.

The setup uses the REST API and the Python standard library. It needs no other program. `mmctl` in local mode refuses to make a bot account, and local mode needs the server's host.

## Errors

A failure names the step, the HTTP status and the server's error id. For these ids it also names the setting or the permission:

| Error id | Cause | Fix |
| --- | --- | --- |
| `api.bot.create_disabled` | Bot account creation is off | Set **System Console > Integrations > Bot Accounts > Enable Bot Account Creation** to true |
| `api.context.session_expired.app_error` | The server refused the admin token. The server refuses every personal access token while personal access tokens are off. | Check the token. Set **System Console > Integrations > Integration Management > Enable Personal Access Tokens** to true |
| `api.command.disabled.app_error` | Custom slash commands are off | Set **System Console > Integrations > Integration Management > Enable Custom Slash Commands** to true |
| `api.context.permissions.app_error` | The account of the admin token lacks a permission | Use the token of an account with the permission that the table of [What setup does](#what-setup-does) gives |

The listener check prints a note, not an error. The server calls an address on a private network only where **AllowedUntrustedInternalConnections** names its host. The note names the host that is missing from that setting.

## After the setup

- The admin who ran the setup owns the bot. By default the server disables a bot when an admin deactivates its owner. See **DisableBotsWhenOwnerIsDeactivated**.
- The server must reach `--listener` for the buttons and the slash command. See [Action listener](../../docs/platforms/mattermost.md#action-listener).
