from belle_agent.identifiers import id_list


def test_configured_ids_keep_order_and_drop_blank_duplicates() -> None:
    assert id_list(" U1 ") == ("U1",)
    assert id_list([" U1 ", None, "U1", "U2", 0]) == ("U1", "U2")
    assert id_list({"unexpected": "mapping"}) == ()
