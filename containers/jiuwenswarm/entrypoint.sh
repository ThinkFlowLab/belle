#!/bin/sh
set -eu
umask 077

workspace=${JIUWENSWARM_DATA_DIR:-$HOME/.jiuwenswarm}
mkdir -p "$workspace/application_plugins" "$workspace/config"
if [ ! -e "$workspace/config/.env" ]; then
    : > "$workspace/config/.env"
fi
if [ ! -e "$workspace/config/config.yaml" ]; then
    cp /usr/local/share/belle-agent/jiuwenswarm-config.yaml "$workspace/config/config.yaml"
fi

extensions=$(python -c 'from importlib.resources import files; print(files("belle_agent.integrations.jiuwenswarm").joinpath("extensions"))')
for name in agent_plugin matrix mattermost second_brain slack; do
    case "$name" in
        agent_plugin) link=belle-agent-plugin ;;
        matrix) link=belle-matrix ;;
        mattermost) link=belle-mattermost ;;
        second_brain) link=belle-second-brain ;;
        slack) link=belle-slack ;;
    esac
    target="$workspace/application_plugins/$link"
    if [ -e "$target" ] && [ ! -L "$target" ]; then
        echo "Refusing to replace $target" >&2
        exit 1
    fi
    ln -sfnT "$extensions/$name" "$target"
done

cd "$workspace"
exec "$@"
