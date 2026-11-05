#!/bin/bash
# Check the separate-container layout of the ACP backend, one rule at a time. Prints PASS or FAIL per rule.
# usage: check-v1-layout.sh <belle container> <harness container> <socket volume> <image with python>
# Belle's container holds BELLE_FAKE_PLATFORM_TOKEN, a canary that stands for its platform credentials.
set -u
BELLE=$1 HARNESS=$2 VOLUME=$3 IMAGE=$4
result() { if [ "$2" = 0 ]; then echo "PASS $1"; else echo "FAIL $1"; fi; }

# The socket volume is in exactly Belle's container and the harness's container.
mounted=$(podman ps -a --filter "volume=$VOLUME" --format '{{.Names}}' | sort | tr '\n' ' ')
expected=$(printf '%s\n' "$BELLE" "$HARNESS" | sort | tr '\n' ' ')
[ "$mounted" = "$expected" ]; result "socket volume in exactly $expected" $?

# The socket is the relay user's only.
mode=$(podman exec "$HARNESS" stat -c '%A %u' /run/belle-acp/agent.sock)
[ "$mode" = "srwx------ 1000" ]; result "socket mode and owner: $mode" $?

# A process under another user ID cannot connect. One under Belle's user ID can.
connect='import socket,sys; s=socket.socket(socket.AF_UNIX)
try: s.connect("/run/belle-acp/agent.sock"); print("connected")
except OSError as e: print(type(e).__name__)'
other=$(podman run --rm --network none --user 1001 -v "$VOLUME:/run/belle-acp" "$IMAGE" python -c "$connect")
[ "$other" = PermissionError ]; result "user 1001 cannot connect: $other" $?
same=$(podman run --rm --network none --user 1000 -v "$VOLUME:/run/belle-acp" "$IMAGE" python -c "$connect")
[ "$same" = connected ]; result "user 1000 connects: $same" $?

# No network port serves ACP: the harness container listens on no TCP port that another container reaches.
# The check reports a listener on the container's own loopback (127.0.0.1 or ::1) and does not count it.
listeners=$(podman exec "$HARNESS" sh -c "cat /proc/net/tcp /proc/net/tcp6 2>/dev/null | awk '\$4 == \"0A\" {print \$2}'")
loopback=$(echo "$listeners" | grep -cE '^(0100007F|00000000000000000000000001000000):')
reachable=$(echo "$listeners" | grep -vcE '^(0100007F|00000000000000000000000001000000):|^$')
[ "$reachable" = 0 ]; result "harness listens on $reachable reachable TCP ports ($loopback on its loopback)" $?

# The harness has no platform credential, and cannot read Belle's environment or state.
canary=$(podman exec "$BELLE" printenv BELLE_FAKE_PLATFORM_TOKEN)
podman exec "$HARNESS" printenv BELLE_FAKE_PLATFORM_TOKEN >/dev/null; [ $? != 0 ]; result "harness environment holds no platform credential" $?
seen=$(podman exec "$HARNESS" sh -c "{ grep -lsF '$canary' /proc/[0-9]*/environ; grep -rlsF --exclude-dir=proc --exclude-dir=sys --exclude-dir=dev '$canary' /; } 2>/dev/null | wc -l")
[ "$seen" = 0 ]; result "harness finds Belle's canary in $seen files" $?
states=$(podman inspect "$HARNESS" --format '{{range .Mounts}}{{.Destination}} {{end}}')
case " $states " in *belle-agent*) false;; *) true;; esac; result "harness mounts no Belle state: $states" $?

# The harness reaches Belle's tool server on the private network, with the token.
url=$(podman exec "$BELLE" printenv BELLE_TOOLS_URL)
token=$(podman exec "$BELLE" printenv BELLE_TOOLS_TOKEN)
host=${url#http://}; host=${host%%/*}; name=${host%%:*}; port=${host##*:}
request='{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
# The token goes through standard input: a command line shows in the host's process list.
status=$(printf '%s\n' "$token" | podman exec -i -e "N=$name" -e "P=$port" -e "B=$request" "$HARNESS" bash -c \
  'read -r T; exec 3<>/dev/tcp/$N/$P; printf "POST /mcp HTTP/1.1\r\nHost: %s:%s\r\nAuthorization: Bearer %s\r\nAccept: application/json, text/event-stream\r\nContent-Type: application/json\r\nContent-Length: %s\r\nConnection: close\r\n\r\n%s" "$N" "$P" "$T" "${#B}" "$B" >&3; head -1 <&3')
case "$status" in *" 200 "*) true;; *) false;; esac; result "harness reaches the tool server: ${status%$'\r'}" $?
