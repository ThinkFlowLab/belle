from belle_agent.platforms.base import affordances
from belle_agent.platforms.matrix.affordances import MATRIX_AFFORDANCES
from belle_agent.platforms.matrix.connector import MatrixPlatform
from belle_agent.platforms.matrix.surfaces import TurnSurfaces as MatrixSurfaces
from belle_agent.platforms.mattermost.affordances import MATTERMOST_AFFORDANCES
from belle_agent.platforms.mattermost.connector import MattermostPlatform
from belle_agent.platforms.mattermost.surfaces import TurnSurfaces as MattermostSurfaces


def test_each_connector_declares_the_table_its_surfaces_read():
    assert MatrixPlatform.affordances is MATRIX_AFFORDANCES
    assert MattermostPlatform.affordances is MATTERMOST_AFFORDANCES


def test_the_surfaces_show_the_card_and_the_reply_as_the_table_says():
    for surfaces, table in ((MatrixSurfaces, MATRIX_AFFORDANCES), (MattermostSurfaces, MATTERMOST_AFFORDANCES)):
        assert (surfaces.silent_card, surfaces.reply_parts_in_thread) == (table.silent_card, table.reply_parts_in_thread)
    assert (MatrixSurfaces.silent_card, MattermostSurfaces.silent_card) == ("keep", "delete")


def test_the_accessor_gives_the_declared_card_and_reply():
    class Stub:
        ephemeral_posts = False
        affordances = MATRIX_AFFORDANCES

    found = affordances(Stub())
    assert (found.silent_card, found.reply_parts_in_thread) == ("keep", False)
