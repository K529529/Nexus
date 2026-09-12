from tags import normalize_tags


def test_normalize_tags_trims_deduplicates_and_sorts() -> None:
    assert normalize_tags([" Beta ", "alpha", "ALPHA", ""]) == ("alpha", "beta")


def test_normalize_tags_empty() -> None:
    assert normalize_tags([]) == ()
