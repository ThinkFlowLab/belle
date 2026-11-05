# Slack app for Belle

A tutorial for installing a Slack app named Belle in your own workspace and obtaining the two tokens Belle needs. The automated route uses the Slack CLI in a hardened rootless-podman container, whose image is defined beside this file. The manual route uses Slack's web pages and needs no container.

Belle needs a Slack app of its own. An app-level token is shared across connections, so a second process on the token of an app that is already in service takes events away from the process that serves it. To try the setup first, run this tutorial in a spare workspace, then repeat it in the workspace Belle will serve.

## One command

`setup`, the image's default command, does steps 4 to 13 of the tutorial below. It creates or updates the app from the manifest, sets the avatar and the name, gets and checks the two tokens and writes `project/out/slack.env`. A person does two things: the Slack login and the invitation of the bot.

1. Build the image, from the repository root:

   ```sh
   podman build --format oci -t localhost/belle:slack-helper-local -f containers/slack-helper/Containerfile \
     --ignorefile containers/slack-helper/.containerignore .
   ```

2. Make a directory for the project state:

   ```sh
   install -d -m 700 ~/slk-belle ~/slk-belle/project
   ```

3. Run `setup`. `-it` gives it a terminal, where it reads the challenge code of the login:

   ```sh
   cd ~/slk-belle
   podman run --rm -it --userns=keep-id:uid=10001,gid=10001 \
     --read-only --tmpfs /tmp:rw,mode=1777 --cap-drop=ALL --security-opt=no-new-privileges \
     --memory=256m --cpus=1 --pids-limit=128 -e BELLE_APP_NAME -e BELLE_COMMAND_NAME \
     -v "$PWD/project:/project:rw" localhost/belle:slack-helper-local
   ```

4. `setup` prints a line `/slackauthticket <ticket>`. Paste it into a channel or a direct message of the workspace, and approve.
5. Type the challenge code that Slack shows. `setup` continues and ends with `wrote /project/out/slack.env`.
6. In the channel Belle will serve, type `/invite @Belle` (step 14).

This form keeps no login: the login stays in the container and goes with it. Keep the login when you expect later changes, such as a new avatar, a manifest or scope change, or a new name. Each later run needs a login, and without a kept one the person does the ticket and the challenge again. To keep it, make `auth/` and add its mount:

```sh
install -d -m 700 ~/slk-belle/auth
cd ~/slk-belle
podman run --rm -it --userns=keep-id:uid=10001,gid=10001 \
  --read-only --tmpfs /tmp:rw,mode=1777 --cap-drop=ALL --security-opt=no-new-privileges \
  --memory=256m --cpus=1 --pids-limit=128 -e BELLE_APP_NAME -e BELLE_COMMAND_NAME \
  -v "$PWD/auth:/auth:rw" -v "$PWD/project:/project:rw" localhost/belle:slack-helper-local
```

With a kept login, `setup` does not log in again.

Without a terminal, for example from an agent or a script, `setup` takes two runs. The first prints the ticket line, keeps the ticket in `project/.slack/login-ticket` (mode 600) and exits with status 1:

```sh
cd ~/slk-belle
podman run --rm --userns=keep-id:uid=10001,gid=10001 \
  --read-only --tmpfs /tmp:rw,mode=1777 --cap-drop=ALL --security-opt=no-new-privileges \
  --memory=256m --cpus=1 --pids-limit=128 -e BELLE_APP_NAME -e BELLE_COMMAND_NAME \
  -v "$PWD/project:/project:rw" localhost/belle:slack-helper-local
```

The person pastes the line, approves, and gives the challenge code. The second run uses the ticket once, removes it, and continues:

```sh
cd ~/slk-belle
podman run --rm --userns=keep-id:uid=10001,gid=10001 \
  --read-only --tmpfs /tmp:rw,mode=1777 --cap-drop=ALL --security-opt=no-new-privileges \
  --memory=256m --cpus=1 --pids-limit=128 -e BELLE_APP_NAME -e BELLE_COMMAND_NAME \
  -v "$PWD/project:/project:rw" localhost/belle:slack-helper-local setup --challenge <code>
```

Add the `auth/` mount to both runs to keep the login.

### What setup writes

- `project/out/slack.env`, mode 600, with exactly two lines, `SLACK_BOT_TOKEN=…` and `SLACK_APP_TOKEN=…`. `setup` writes it only after both tokens pass a check against Slack, and prints only each name, the first five characters and the length.
- `project/.slack/`, the CLI's project state. `apps.dev.json` holds the app id.
- `project/icon.png`, the avatar that the install uploads.
- With the `auth/` mount, `auth/credentials.json`, the login.

Keep `project/` in both forms. Without `apps.dev.json` a later run creates a second app instead of updating the first. A second `setup` with the same `project/` updates the same app. It applies the manifest and the avatar again and sets the name. It gets the tokens again and writes `slack.env` again.

What each mount holds, and how sensitive it is:

| mount | holds | sensitivity |
|-------|-------|-------------|
| `/project`, read-write | the app id, the CLI's project state, `slack.env`, and the login ticket between two runs | `slack.env` acts as the bot. Keep it private |
| `/auth`, read-write, optional | the login | acts as the person in the whole workspace. The most sensitive file |
| `/manifest/manifest.yaml`, `/manifest/icon.png`, read-only, optional | a manifest or a PNG that replaces the image's default | not secret |

Remove the login when you plan no further changes: `rm ~/slk-belle/auth/credentials.json`, or `slack auth logout` as in step 15. Remove also the CLI's authorisation in the workspace's list of installed apps. A reinstall, a manifest or avatar change and a rename need a login again.

### Options

- Another manifest: copy one from `src/belle_agent/platforms/slack/manifests/` to `~/slk-belle/manifest.yaml` and add `-v "$PWD/manifest.yaml:/manifest/manifest.yaml:ro"`. The default is tier 3 (step 2).
- Another avatar: put a square PNG at `~/slk-belle/icon.png` and add `-v "$PWD/icon.png:/manifest/icon.png:ro"`.
- Another name: start the command with `env BELLE_APP_NAME="<name>" BELLE_COMMAND_NAME=<word>`. The command passes both variables to the container. Set the same command word in Belle's configuration (`platforms.slack.command_name`), and the name wherever your harness refers to it. That is your responsibility.
- `setup --force`: the install stops when the app settings hold keys that the manifest does not set (step 8). Read the differences that the CLI prints, then run `setup --force` to replace the app settings with the manifest.

### Belle with slack.env

Give the file to Belle's container with `--env-file`. Podman accepts several, so the model and harness settings stay in your own file:

```sh
podman run --env-file ~/slk-belle/project/out/slack.env --env-file <your settings file> <your Belle image>
```

`containers/opencode/README.md` describes a full Belle start.

## Who this is for

- **A person with a coding agent.** The agent follows this tutorial and runs every command. The person does four manual things: choose the workspace, paste one slash command and approve a permissions window, send the agent the challenge code, and invite the bot to a channel.
- **A person who already runs their own stack** and only needs the Slack app and the tokens. Follow the tutorial to the hand-off file in step 13, or use the manual route.
- **A person doing it by hand.** Every command is copy-paste. The [manual route](#manual-route) gives the web-form steps in full for each step the CLI automates, and needs neither the container nor Belle's code beyond one manifest file.

## What you end with

A Slack app named Belle with the slash command `/belle`, installed in your workspace, with its bot invited to a channel, and these:

- a file `project/out/slack.env` in a single-purpose directory, mode 600, with exactly two lines, `SLACK_BOT_TOKEN=…` and `SLACK_APP_TOKEN=…`, written without printing either value;
- the two token files it was made from (`bot_token`, `app_token`) on the CLI route;
- three facts for the next step: the workspace's `<team id>`, the app's bot user name, and the `<channel id>` of the channel the bot was invited to.

The two-line file is the contract every Belle start method accepts: a container's `--env-file`, or a process environment loaded from it. Podman secrets are the alternative for a reader who starts containers by hand (step 13).

## Division of work between the person and the agent

For the first reader. The table lists the CLI route in order.

| # | who | action | what the person gives away |
|---|-----|--------|----------------------------|
| 1 | person | Chooses a workspace where they may install apps (step 3) and the manifest tier (step 2) | The tier decides the scopes the app will hold |
| 2 | agent | Builds the image, prepares the directory, writes the project files, checks the manifest offline (steps 1, 2, 4, 5) | nothing |
| 3 | agent | Runs `login --no-prompt` and shows the person the one line `/slackauthticket <ticket>` (step 6) | nothing |
| 4 | person | Pastes that line into any channel or DM of the workspace, approves the permissions window, and sends the agent the challenge code Slack shows | The CLI gets workspace-level access, as that user, until the login is removed |
| 5 | agent | Completes the login, shows `auth list`, validates the manifest, creates and installs the app, checks the app name for a " (local)" suffix, runs it to obtain the tokens, checks them (steps 6, 8, 9, 12) | The install grants the app the scopes of the chosen manifest |
| 6 | agent | Writes the hand-off file and shows the person the variable names with prefixes and lengths (step 13) | nothing |
| 7 | person | Types `/invite @Belle` in the channel Belle should serve and sends the agent the channel id (step 14) | The bot can read that channel |
| 8 | agent | Keeps `<team id>`, the bot user name, `<channel id>` and the hand-off file for the next step | nothing |

The agent never prints a token, never puts one in a command-line argument, a commit, a log or a chat message, and shows only a prefix and a length. The login ticket and the challenge code are single-use values for one login attempt. The agent keeps them out of commits and logs too.

## Prerequisites

- A Slack workspace where the person may install apps. A free workspace is enough. Network access to `slack.com` and its subdomains.
- For the CLI route: Linux with **rootless podman 4.3 or later**, a checkout of this repository, and outbound HTTPS to `downloads.slack-edge.com` and `docker.io` during the image build. The run script uses `--userns=keep-id:uid=10001,gid=10001`, which requires podman 4.3 or later. On an older version, use `--userns=keep-id --user "$(id -u):$(id -g)"` in `slk`.
- For the manual route: a browser, and the manifest file from this repository or from a copy of it.
- `sh`, `bash`, or `fish`. The commands use `env` to set variables for one call. Use a working directory path without spaces.

## Detailed CLI route

Use these steps to run the Slack CLI commands individually. Slack login, app approval, and inviting the bot require a person in the workspace.

## 1. The image

From the repository root:

```sh
podman build --format oci -t localhost/belle:slack-helper-local -f containers/slack-helper/Containerfile \
  --ignorefile containers/slack-helper/.containerignore .
```

Check:

```sh
podman images localhost/belle:slack-helper-local --format '{{.Repository}}:{{.Tag}} {{.Size}}'
```

Expected: an image tagged `localhost/belle:slack-helper-local`.

The CLI version and its checksums are build arguments (`SLACK_CLI_VERSION`, `SLACK_CLI_SHA256_AMD64`, `SLACK_CLI_SHA256_ARM64`). A wrong checksum fails the build. The image supports `amd64` and `arm64`.

The image holds `slack`, `git`, CA certificates, `yq`, and `belle-slack-project`. It includes the tier-3 manifest at `/manifest/manifest.yaml` and Belle's avatar at `/manifest/icon.png`. Read-only mounts at either path replace those defaults. The build renders the avatar from `belle-avatar.svg`; build tools and downloads stay in the build stage.

## 2. The host directory and the run script

Use a directory made only for this. From the repository root:

```sh
install -d -m 700 ~/slk-belle-test ~/slk-belle-test/auth ~/slk-belle-test/project
cp src/belle_agent/platforms/slack/manifests/slack-app-manifest-3-gated.yaml ~/slk-belle-test/manifest.yaml
install -m 700 containers/slack-helper/slk ~/slk-belle-test/slk
```

Pick a manifest tier by filename: `0-core`, `1-connector`, `2-tools`, or `3-gated`. [Slack platform settings](../../docs/platforms/slack.md) lists the scopes. Tiers 1 to 3 include `/belle`; tier 0 does not. The container reads your copy of `manifest.yaml` without changing it.

Check:

```sh
cd ~/slk-belle-test && ls -A && stat -c '%a %U' . auth project slk
```

Expected: the names `auth`, `manifest.yaml`, `project` and `slk`, then four lines `700 <you>`.

`slk` is the hardened `podman run` line with the tty off. The image's entry point sends `setup`, `init`, `tokens` and `name` to the helper `belle-slack-project`, and every other command to the Slack CLI. `./slk slack <arguments>` also runs the CLI. The helper's `manifest` subcommand, the CLI's hook, runs with `env SLK_OPTS="--entrypoint belle-slack-project" ./slk manifest`. It works from any shell and from a non-interactive caller. It finds its own directory, so `auth/`, `project/` and `manifest.yaml` beside it are the mounts. Call it as `./slk` from inside the directory. Settings for one call go through `env`:

- `env SLK_OPTS="<podman options>" ./slk <command>` adds podman options.
- `env SLK_TTY=-it ./slk <command>` gives the command a terminal.
- `env BELLE_APP_NAME="<name>" ./slk <command>` sets the app's display name (default `Belle`, at most 35 characters, the manifest's own name). Pass it on every call, because the manifest hook runs in every call.
- `env BELLE_COMMAND_NAME=<word> ./slk <command>` sets the slash command, without the slash (default `belle`). Pass it on every call, for the same reason. Belle's configuration must name the same word in `platforms.slack.command_name`. The default for that key is also `belle`, so the two agree without either variable. Set the variables only for other names.
- `env BELLE_APP_ICON=<file> ./slk <command>` sets the app's avatar from an image in `project/`, named relative to it (default: no icon). The install of step 8 uploads it. Pass it on every call, for the same reason.
- `env SLK_IMAGE=<image> ./slk <command>` runs another image name (default `localhost/belle:slack-helper-local`).

## 3. The workspace

Use the workspace Belle will serve, or a spare one for a first try. Create a spare one at <https://slack.com/get-started#/createnew>. A free workspace is enough. The person needs the right to install apps there.

## 4. The project files

```sh
cd ~/slk-belle-test
./slk init
```

This prints nothing and writes `project/.slack/hooks.json` and `project/.slack/config.json`. Check:

```sh
cd ~/slk-belle-test && cat project/.slack/hooks.json project/.slack/config.json
```

Expected: hooks `get-manifest`, `start` and `deploy` that call `belle-slack-project`, `"sdk-managed-connection-enabled": true`, and `{ "manifest": { "source": "local" } }`.

The `get-manifest` hook converts the YAML to JSON and applies `BELLE_APP_NAME` and `BELLE_COMMAND_NAME`. `config.json` tells the CLI to read that local manifest. The `start` hook saves the tokens in step 12. The YAML source stays untouched.

`slack doctor` still reports the `doctor` and `check-update` hooks as missing. They serve SDK dependency checks, which a project without an SDK does not have.

## 5. The manifest check

Offline, no login:

```sh
cd ~/slk-belle-test
env SLK_OPTS=--network=none ./slk manifest info
```

Expected: `Belle` as the app and bot name, `/belle` as the slash command, and `settings.socket_mode_enabled: true`. Tiers 1 to 3 include the `commands` scope; tier 0 has no slash command.

After logging in at step 6, ask Slack to validate the manifest:

```sh
cd ~/slk-belle-test
./slk manifest validate
```

Expected: `App Manifest Validation Result: Valid`.

Once the app exists, add `--app <app id>` or `--app local --team <team id>` to select it without a prompt.

## 6. The CLI authorisation

The agent starts the login. No terminal is needed:

```sh
cd ~/slk-belle-test
./slk login --no-prompt
```

It prints three lines of instruction and then the line `/slackauthticket <ticket>`. The agent shows the person that one line and asks the person to paste it into any channel or DM of the workspace, approve the permissions window and send back the challenge code Slack shows. Slack's documentation for the flow: <https://docs.slack.dev/tools/slack-cli/guides/authorizing-the-slack-cli>.

The ticket is single-use and tied to that login attempt. Finish the login as soon as the code arrives:

```sh
cd ~/slk-belle-test
./slk login --no-prompt --ticket <ticket> --challenge <code>
./slk auth list
```

Expected: "You've successfully authenticated!" Then `auth list` shows the workspace, team id, user id, and authorization level. Save the `<team id>` for later steps. For an interactive login, use `env SLK_TTY=-it ./slk login`.

`auth/credentials.json` is mode 600 and owned by the operator. It holds one entry per team: a token and a refresh token with an expiry, plus the team and user ids. This file is the broad secret of the setup. It can act as the person in the workspace, which is wider than the two Belle tokens.

## 7. Admin approval

If the workspace requires app approval, an administrator must approve the request in Slack's app management page. The CLI reports `app_approval_request_pending` or `app_approval_request_denied`. After approval, repeat step 8.

## 8. App creation and installation

```sh
cd ~/slk-belle-test
./slk app install -E local --team <team id>
./slk app list
```

`--team` selects the workspace without a prompt. Note the app id from `app list`; step 9 needs it.

If `app install` reports an error after printing "Installed", check `./slk app list` before retrying.

The CLI writes `project/.slack/apps.dev.json` (mode 600). `-E local` can add " (local)" to the app and bot names; step 9 checks them.

### The app icon

Slack's manifest has no field for the app's avatar. The CLI reads an `icon` field from the manifest that the `get-manifest` hook prints, and uploads that image on install. `BELLE_APP_ICON` adds the field. Use a square PNG, for example 1024 by 1024 pixels, and put it in `project/`:

```sh
cd ~/slk-belle-test
cp <square png> project/icon.png
env BELLE_APP_ICON=icon.png ./slk app install -E local --team <team id>
```

Expected: `Updated app icon: icon.png`. Check the app name again in step 9. `manifest info` does not show the `icon` field because it is not part of Slack's manifest.

If the CLI reports `app_manifest_update_error`, inspect the differences it prints. `--force` replaces the app settings with the manifest.

## 9. The app name without the suffix

`app install -E local` can add " (local)" to the app and bot names. Check after each install. The helper sets both names from the manifest through `apps.manifest.update`, using the CLI login.

```sh
cd ~/slk-belle-test
./slk name <app id> <team id>
```

Expected: `ok`. On `token_expired` or `invalid_auth`, check `./slk auth list` and log in again if needed.

Check the result:

```sh
cd ~/slk-belle-test
./slk manifest info --app <app id> --source remote
```

Expected: the name `Belle`, the display name `Belle`, the command `/belle` and the scope `commands` (tiers 1 to 3).

Run `./slk name` again if a later install or run adds the suffix. `setup` sets the name after `slack run`.

`-E deployed` could avoid the suffix, but the tokens come from `slack run`, and the help of `slack run` offers no environment flag. It describes the command as running the app "locally", and its `--cleanup` uninstalls "the local app". So `setup` keeps `-E local` and this step.

The helper reads the team's login token from `/auth/credentials.json` and sends it with the manifest in an HTTPS request. The request body stays in container tmpfs and is deleted on exit. The token is not printed or passed as a command argument.

## 10. The manifest tier

Replace the copy and install again. From the repository root:

```sh
cp src/belle_agent/platforms/slack/manifests/slack-app-manifest-2-tools.yaml ~/slk-belle-test/manifest.yaml
cd ~/slk-belle-test
./slk manifest diff --app local --team <team id>
./slk app install -E local --team <team id>
```

`manifest diff` shows differences between the project manifest and the app settings. Slack applies scope changes after a reinstall. Verify by the new scopes under **OAuth & Permissions** in the app settings. Check the name afterwards (step 9).

## 11. An app made in the web form

```sh
cd ~/slk-belle-test
env SLK_TTY=-it ./slk app link -E local
```

Or without prompts: `./slk app link --team <team id> --app <app id> --environment local`. With `manifest.source` set to `local`, a later install writes this project's manifest over the app's settings. Link only an app made for Belle.

## 12. The two Belle tokens

The hooks reference (<https://docs.slack.dev/tools/slack-cli/reference/hooks>) says that when `config.sdk-managed-connection-enabled` is `true`, the CLI gives the `start` hook "the application's app-level token and bot access token" as the environment variables `SLACK_CLI_XAPP`, `SLACK_CLI_XOXB`, `SLACK_APP_TOKEN` and `SLACK_BOT_TOKEN`. Those are the two names Belle reads. `init` wrote that setting and a `start` hook that stores the variables.

Two commands look like sources and are not. `slack env` sets, lists and unsets the project's own variables in a `.env` file and does not show a token. `slack auth token` gives a service token for the CLI itself, not Belle's tokens.

### Run the app to receive its tokens

```sh
cd ~/slk-belle-test
./slk run --team <team id>
ls -l project/out
```

The `start` hook writes `project/out/bot_token` and `project/out/app_token` with mode 600. It prints file names, not token values. Do not pass `--cleanup`, which uninstalls the app on exit. Check both tokens before using them; the hook does not remove files left by an earlier run.

### Checking the tokens without printing them

Check the app-level token with Slack's `apps.connections.open` API and the bot token with `auth.test`. The command reads the files in a throwaway container. It mounts the token directory read-only and redacts the connection URL, which contains a ticket:

```sh
cd ~/slk-belle-test
podman run --rm --userns=keep-id:uid=10001,gid=10001 --user 10001:10001 \
  --read-only --cap-drop=ALL --security-opt=no-new-privileges \
  -v "$PWD/project/out:/out:ro" docker.io/library/alpine:3.20 sh -c '
  wget -qO- --post-data="" --header="Authorization: Bearer $(cat /out/app_token)" \
    https://slack.com/api/apps.connections.open
  echo
  wget -qO- --post-data="" --header="Authorization: Bearer $(cat /out/bot_token)" \
    https://slack.com/api/auth.test
  echo' | sed 's#wss:[^"]*#wss:REDACTED#g'
```

Expected: each answer starts with `{"ok":true`. The `connections.open` result confirms Socket Mode access. The podman command line contains no token, but `wget` receives one as an argument inside the container; processes owned by the same user may see it while the request runs.

## 13. The hand-off file

The end state is a file `project/out/slack.env`, mode 600, with exactly two lines and nothing else:

```
SLACK_BOT_TOKEN=<bot token>
SLACK_APP_TOKEN=<app-level token>
```

Every Belle start method accepts this file: a container's `--env-file`, or a process environment loaded from it. The agent writes it from the two token files without printing either value. `sh -c` keeps the form identical in `bash` and in `fish`, and `printf` is a shell builtin, so no value reaches a command line:

```sh
cd ~/slk-belle-test
sh -c 'umask 077; printf "SLACK_BOT_TOKEN=%s\nSLACK_APP_TOKEN=%s\n" "$(cat project/out/bot_token)" "$(cat project/out/app_token)" > project/out/slack.env'
```

Check it, printing only the mode, the owner, the line count, and the name, the first five characters and the length of each value:

```sh
cd ~/slk-belle-test/project/out
stat -c '%a %U' slack.env
wc -l < slack.env
awk '{i=index($0,"="); print substr($0,1,i-1), substr($0,i+1,5), length($0)-i}' slack.env
```

Expected: mode `600`, two lines, and values with the prefixes `xoxb-` and `xapp-`. Token lengths can vary. Run the direct check of step 12 before relying on the file. Delete the individual token files only after that check.

For a reader who starts containers by hand, podman secrets are the alternative to an env file:

```sh
cd ~/slk-belle-test
podman secret create slk-belle-bot-token project/out/bot_token
podman secret create slk-belle-app-token project/out/app_token
```

A container takes them as `--secret slk-belle-bot-token,type=env,target=SLACK_BOT_TOKEN` and `--secret slk-belle-app-token,type=env,target=SLACK_APP_TOKEN`.

What each leaves exposed:

- **Podman secrets.** They do not show in `podman inspect` or in the image. They show in the container process's environment, so `podman exec ... env` and `/proc` reveal them to the same user. At rest, the default `file` driver stores them unencrypted under `~/.local/share/containers/storage/secrets`. The `pass` driver keeps them in a GPG-encrypted password store, and the `shell` driver calls scripts you provide. Both are set in `containers.conf` (`[secrets]`). To list secrets, use `podman secret ls --format '{{.Name}}'`: `--format '{{.Driver.Name}}'` fails on podman 4.9.3, where the driver field is a string.
- **A mode 600 env file.** The same exposure at rest as the `file` driver, and `podman inspect` shows a variable passed with `--env-file`.

A CLI stdout pipe straight into `podman secret create name -` does not fit, because the CLI streams a hook's output to its own terminal output together with its other messages. A file is the clean carrier.

Before committing or sharing files, check that no token-shaped string entered the repository or image:

```sh
git grep -nIE 'xox[a-z]-[0-9A-Za-z]|xapp-[0-9A-Za-z]' -- . ':!tests' ':!docs' ':!src'
podman history --no-trunc localhost/belle:slack-helper-local | grep -cE 'xox[a-z]-[0-9A-Za-z]|xapp-[0-9A-Za-z]'
podman inspect localhost/belle:slack-helper-local | grep -cE 'xox[a-z]-[0-9A-Za-z]|xapp-[0-9A-Za-z]'
```

Each prints nothing or `0`. Run the same `history` and `inspect` on any image that will hold the tokens. Never use `echo $TOKEN`, `set -x`, or a token in a command-line argument, which `ps` shows.

## 14. Invite the bot and check the app

A manifest cannot add a bot to a channel. The person does two things in Slack:

1. In the channel Belle should serve, type `/invite @Belle` and send. A private channel works the same way.
2. Find the channel id and send it to the agent. Open the channel's name at the top, then **About**: the id is at the bottom (it begins with `C`). Or use **Copy link** on the channel: the last path segment of `https://<workspace>.slack.com/archives/<channel id>` is the id.

Once Belle runs with the tokens (see Next), check the app:

- Type `/belle help` in the channel or in a direct message. Belle answers privately when `platforms.slack.command_name` is `belle`.
- With `platforms.slack.home_tab_welcome: true`, open the app's Home tab. The manifests enable that tab.

## 15. Rotation and clean-up

Rotate the tokens. Revoke the app-level token under **Basic Information → App-Level Tokens**. Uninstall the app (`./slk app uninstall`) and install it again to issue a new bot token. A reinstall alone keeps the existing bot token. Then redo step 12, rewrite `slack.env` (step 13) and replace the secrets, if any:

```sh
podman secret rm slk-belle-bot-token slk-belle-app-token
```

Remove the app:

```sh
cd ~/slk-belle-test
env SLK_TTY=-it ./slk app uninstall      # keeps the app and its data
env SLK_TTY=-it ./slk app delete         # deletes the app permanently
```

Revoke the CLI's login, then remove everything local:

```sh
cd ~/slk-belle-test
env SLK_TTY=-it ./slk auth logout --all
cd ~ && rm -rf ~/slk-belle-test
podman rmi localhost/belle:slack-helper-local
```

`auth logout` removes local credentials. Also remove the CLI's authorisation in the workspace's installed-app list. The files belong to the operator because the container uses `keep-id`.

## Manual route

Use Slack's app settings if you prefer a browser to the CLI container. See [Slack platform setup](../../docs/platforms/slack.md) for the app settings.

1. **Make the manifest.** Copy a manifest file from `src/belle_agent/platforms/slack/manifests/` (`slack-app-manifest-3-gated.yaml` for the maximum tier, `1-connector` for less). The copy names the app `Belle` and declares `/belle` (tier 0 declares no command). Edit it only to use other names: set `display_information.name` and `features.bot_user.display_name` (at most 35 characters), and the `command` line of the slash command.
2. **Create the app.** Open <https://api.slack.com/apps>, **Create New App**, **From a manifest**, choose the workspace, paste the YAML, and create it.
3. **Create the app-level token.** Under **Basic Information → App-Level Tokens**, **Generate Token and Scopes**, add the scope `connections:write`, generate, and copy the `xapp-` value.
4. **Install.** Under **Install App** (or **Install to Workspace** on the first page), install to the workspace. This grants the app the scopes of the manifest. Copy the **Bot User OAuth Token**, the `xoxb-` value.
5. **Write the hand-off file** from a terminal, with the values typed without echo. The values never appear in a command line:

   ```sh
   install -d -m 700 ~/slk-belle-test ~/slk-belle-test/project ~/slk-belle-test/project/out
   cd ~/slk-belle-test/project/out
   sh -c 'umask 077; stty -echo; printf "bot token: "; read -r B; printf "\napp-level token: "; read -r A; stty echo; printf "\n"; printf "SLACK_BOT_TOKEN=%s\nSLACK_APP_TOKEN=%s\n" "$B" "$A" > slack.env'
   ```

   Check it with the commands in step 13. The values must start with `xoxb-` and `xapp-`.
6. **Invite the bot and find the channel id** as in step 14. The bot name is under **App Home** in the app's settings. An app made in the web form has no " (local)" suffix, so step 9 does not apply. The `<team id>` is only needed by the CLI. It is the `T…` segment in the address `https://app.slack.com/client/<team id>/…` of the workspace in a browser.

## Troubleshooting

| seen | cause and fix |
|------|---------------|
| `app install` exits with status 1 after printing "Installed" | Run `./slk app list` to check whether the app was installed |
| `prompt_error`, "Select an app", "The input device is not a TTY" | The app exists and the command was not told which one. Add `--app <app id>`, or `--app local --team <team id>` |
| `slack api apps.connections.open --token <app-level token>` answers `invalid_auth` | The command wrongly rejects a valid app-level token. Use the direct check of step 12 |
| `feature_disabled` when creating the app from the tier-3 manifest | The workspace lacks Slack's agent feature. Use the tier-2 manifest instead (step 10) |
| Files in `auth/` or `project/` belong to a high numeric uid, and `ls -l` shows no owner name | The `--userns=keep-id:...` option was dropped, for example by a hand-written `podman run`. Use `./slk`, which carries it. Remove such files with `podman unshare rm -r` |
| `Permission denied` writing to `/auth` or `/project` | The same cause. Without the user-namespace option, the operator's directory maps to root and uid 10001 cannot write to it |
| `--userns` option not understood | Podman older than 4.3. Use the alternative in the prerequisites |
| `login --no-prompt --ticket ... --challenge ...` fails | The ticket is single-use and tied to one login attempt. Run `./slk login --no-prompt` again for a fresh ticket and use the new challenge code |
| A second `./slk run` rewrites the token files | Check the new tokens and update any environment file or secrets that use them |
| A token file from an earlier run is still there | The hook never deletes. Remove `project/out` before a run if a stale file would mislead |
| `app_approval_request_pending` or `app_approval_request_denied` | An administrator must approve the app (step 7) |
| The app is named `Belle (local)` in Slack | The CLI added the suffix at an install. Run step 9 |
| `./slk name` prints `token_expired` or `invalid_auth` | Check `./slk auth list` and log in again if needed (step 6) |

## Several workspaces

Each workspace gets its own app and its own two tokens. The recommended layout is one directory per workspace, each built as in step 2 with its own `auth/`, `project/` and `manifest.yaml`. From the repository root:

```sh
install -d -m 700 ~/slk-belle-test-2 ~/slk-belle-test-2/auth ~/slk-belle-test-2/project
cp src/belle_agent/platforms/slack/manifests/slack-app-manifest-3-gated.yaml ~/slk-belle-test-2/manifest.yaml
install -m 700 containers/slack-helper/slk ~/slk-belle-test-2/slk
```

Repeat steps 4 to 14 inside the new directory. Each workspace needs separate token files and project state. The same image serves every directory.

## Design

Each rule has its reason.

- Rootless podman. A container escape lands as an unprivileged user.
- Non-root user inside (`slack`, uid 10001), `--cap-drop=ALL`, `--security-opt=no-new-privileges`. The CLI needs no capability and no privilege gain.
- Read-only root filesystem with a tmpfs on `/tmp`. The image cannot be altered at run time. The CLI writes only to the two mounts.
- `--memory`, `--cpus`, `--pids-limit`. A runaway hook stays contained.
- No published port. The CLI needs outbound HTTPS to Slack and nothing inbound. The default network is the one deliberate opening.
- The manifest is one file mounted `:ro`. The container never changes the source. The image converts it to the JSON the CLI wants at run time.
- Two single-purpose host directories mounted read-write. `auth/` holds the CLI login. `project/` holds the project state and the token output. They are separate because the login can act as the person in the workspace, a wider secret than one app's tokens.
- `--userns=keep-id:uid=10001,gid=10001`. The invoking host user becomes uid 10001 inside, so files written to the mounts belong to the operator on the host and need no `podman unshare`.
- The CLI's own credential files are mode 600. The helper and the image entrypoint run with `umask 077`, so the token files, the CLI's own directories and the token directory are private (600 and 700).
- Mount only directories made for this. Never a home directory, a dotfile directory such as `~/.config` or `~/.slack`, a cache path or a container socket.

Named volume against bind mount for `auth/` and `project/`. A bind mount is owned by the operator, is visible to ordinary tools, backs up with them and is wiped with `rm -r`. A named volume lives in podman's storage under a subordinate uid, is reachable only through podman, and is wiped with `podman volume rm`. Both are readable by every process running as the operator, so neither protects against malware in that session. A bind mount has one extra exposure: a backup or sync tool that sweeps the home directory will copy it. Keep the directory outside any swept tree, or use a named volume if that cannot be arranged. This tutorial uses bind mounts.

On SELinux hosts, add `:Z` to the project and auth mounts and `:ro,Z` to the manifest mount. This relabels the host paths. Never use `:Z` on a directory shared with anything else.

## State locations

| path in the container | host directory | content |
|-----------------------|----------------|---------|
| `/auth/credentials.json` | `auth/` | CLI login credentials (mode 600); keep private |
| `/auth/config.json`, `/auth/logs/` | `auth/` | CLI settings and logs |
| `/project/.slack/` | `project/` | hooks, app ID, and CLI project state |
| `/project/out/` | `project/` | token files and the environment file; `setup` leaves only `slack.env` |
| `/project/.slack/login-ticket`, `/project/icon.png` | `project/` | temporary login ticket and uploaded avatar |

`SLACK_CONFIG_DIR` (also `--config-dir`) moves the system directory from `~/.slack` to `/auth`. The documentation shows `~/.slack/credentials.json` as the default, and the binary contains the variable name. The image sets it, and `SLACK_DISABLE_TELEMETRY` and `SLACK_SKIP_UPDATE` too.

## Next

This tutorial ends with the app, the hand-off file and three facts: the `<team id>`, the bot user name and the `<channel id>`. To run Belle with them, follow `containers/opencode/README.md`. The two tutorials can be done in either order. A reader who runs their own stack passes the file as the environment of Belle and sets the channel id in Belle's configuration as the one served channel. The configuration's `platforms.slack.command_name` must name the manifest's command. Belle's default is `belle`, which matches. The configuration may set `platforms.slack.home_tab_welcome: true`.
