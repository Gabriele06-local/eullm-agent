"""Unit tests for topics.assign: greedy clustering, no database needed."""

from editor.topics import assign, keyword_label


def test_keyword_label_handles_an_empty_list():
    assert keyword_label([]) == ("", [])


def test_related_items_share_one_cluster():
    clusters = assign([
        (1, "Open Fiber accelera cablaggio FTTH aree bianche", ""),
        (2, "Open Fiber cablaggio FTTH aree bianche sud Italia", ""),
    ], [])
    assert len(clusters) == 1
    assert clusters[0].item_ids == [1, 2]


def test_unrelated_item_opens_a_new_cluster():
    clusters = assign([
        (1, "Open Fiber accelera cablaggio FTTH aree bianche", ""),
        (2, "MikroTik RouterOS roaming Wi-Fi rete mesh", ""),
    ], [])
    assert len(clusters) == 2


def test_single_shared_term_is_not_enough():
    clusters = assign([(1, "Open Fiber FTTH cablaggio pianura", "")], [])
    clusters = assign([(2, "Open proxy server sicuri aziendali", "")], clusters)
    assert len(clusters) == 2


def test_short_title_joins_when_all_terms_match():
    clusters = assign([(1, "Open Fiber FTTH cablaggio pianura", "")], [])
    clusters = assign([(2, "Open Fiber", "")], clusters)
    assert len(clusters) == 1 and clusters[0].item_ids == [1, 2]


def test_empty_title_opens_its_own_cluster():
    clusters = assign([(1, "", "")], [])
    assert len(clusters) == 1 and clusters[0].item_ids == [1]
