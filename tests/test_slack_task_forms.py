from belle_agent.platforms.slack import task_forms


def _form(names: tuple[str, ...] = (), *, initial_name: str = "") -> task_forms.TaskForm:
    return task_forms.TaskForm(
        kind="skill",
        team_id="T1",
        user_id="U1",
        channel_id="D1",
        created_at=0.0,
        names=names,
        initial_name=initial_name,
    )


def test_form_keeps_action_ids_and_cancel_button() -> None:
    blocks = task_forms.task_form_blocks(_form(), "nonce", bot_name="Belle")
    assert blocks[0]["element"]["action_id"] == task_forms.NAME_ACTION
    assert [button["action_id"] for button in blocks[-1]["elements"]] == [
        task_forms.SUBMIT_ACTION,
        task_forms.CANCEL_ACTION,
    ]
    assert task_forms.ACTION_PREFIX == "belle_slash_task:"


def test_large_catalog_uses_searchable_name_select() -> None:
    names = tuple(f"skill-{index}" for index in range(101))
    blocks = task_forms.task_form_blocks(
        _form(names),
        "nonce",
        intro_text="Choose a skill",
        bot_name="Belle",
    )
    element = blocks[1]["element"]
    assert element["type"] == "external_select"
    assert element["action_id"] == task_forms.external_name_action_id("nonce")


def test_external_option_round_trips_a_name_beyond_the_first_page() -> None:
    names = tuple(f"skill-{index}" for index in range(121))
    chosen = task_forms.external_name_option(names[110], 110)
    state = {
        task_forms.NAME_BLOCK: {
            task_forms.external_name_action_id("nonce"): {
                "selected_option": chosen,
            }
        },
        task_forms.TASK_BLOCK: {task_forms.TASK_ACTION: {"value": "Inspect the code"}},
    }
    assert task_forms.read_task_state(state, nonce="nonce", names=names) == (
        names[110],
        "Inspect the code",
    )
    state[task_forms.NAME_BLOCK][task_forms.external_name_action_id("nonce")]["selected_option"]["value"] = "999999"
    assert task_forms.read_task_state(state, nonce="nonce", names=names)[0] == ""


def test_small_catalog_uses_static_select() -> None:
    blocks = task_forms.task_form_blocks(
        _form(("research", "code"), initial_name="code"),
        "nonce",
        bot_name="Belle",
    )
    element = blocks[0]["element"]
    assert element["type"] == "static_select"
    assert element["initial_option"]["value"] == "code"
