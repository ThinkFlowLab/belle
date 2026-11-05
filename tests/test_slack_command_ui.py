"""Slack picker payloads are usable without a host runtime."""

from belle_agent.platforms.slack import command_ui


def test_model_options_filter_and_obey_slack_limit() -> None:
    names = [f"model-{index}" for index in range(120)] + ["x" * 151]
    options = command_ui.model_options(names, "MODEL")
    assert len(options) == 100
    assert options[0] == {
        "text": {"type": "plain_text", "text": "model-0"},
        "value": "model-0",
    }
    assert not command_ui.model_options(names, "missing")


def test_picker_choice_is_stable_and_escapes_preview() -> None:
    option = command_ui.picker_option(
        "session", "request", "Alice", "1710000000.000100", "  Hi <@U123>  "
    )
    assert option[0] == "Hi [reference]"
    assert option[1] == command_ui.slash_picker_key(
        "session", "request", "1710000000.000100"
    )
    assert option[2] == "Alice · 2024-03-09 16:00 UTC"
    assert command_ui.stop_picker_blocks([option])[1]["elements"][0]["options"][0]["value"] == option[1]


def test_picker_invocation_rejects_conflicting_channels() -> None:
    body = {
        "team": {"id": "T1"},
        "user": {"id": "U1"},
        "channel": {"id": "C1"},
        "container": {"channel_id": "C1"},
    }
    assert command_ui.select_invocation(body) == command_ui.Invocation("T1", "U1", "C1", "")
    body["container"] = {"channel_id": "C2"}
    assert command_ui.select_invocation(body) is None


def test_command_listeners_register_exact_and_scoped_action_patterns() -> None:
    registrations = []

    class App:
        def __getattr__(self, kind):
            return lambda selector: lambda handler: registrations.append((kind, selector, handler))

    class Commands(command_ui.SlackCommandInteractions):
        def _slash_command(self):
            return "/belle"

    commands = Commands()
    commands._register_commands(App())
    assert [(kind, handler.__name__) for kind, _, handler in registrations] == [
        ("action", "_handle_model_select_action"),
        ("options", "_handle_model_select_options"),
        ("options", "_handle_slash_task_name_options"),
        ("action", "_handle_slash_task_action"),
        ("action", "_handle_slash_stop_select"),
        ("action", "_handle_slash_queue_drop_select"),
        ("action", "_handle_slash_select_cancel"),
        ("command", "_handle_slash_command"),
    ]
    assert registrations[2][1].match("belle_slash_task:name:nonce")
    assert not registrations[2][1].match("other:name:nonce")
    assert registrations[3][1].match("belle_slash_task:submit")
    assert not registrations[3][1].match("other:submit")
    assert registrations[-1][1] == "/belle"
