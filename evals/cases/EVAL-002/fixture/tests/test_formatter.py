from formatter import format_title


def test_format_title_normalizes_whitespace() -> None:
    assert format_title("  nexus   agent runtime ") == "Nexus Agent Runtime"


def test_format_title_handles_single_word() -> None:
    assert format_title("nEXUS") == "Nexus"
