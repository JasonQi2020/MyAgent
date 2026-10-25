from agent_demo.channels.render import short_args, shorten, split_text, tables_to_code, to_chat


def test_short_args():
    assert short_args({"query": "from:github.com", "limit": 3}) == 'query="from:github.com", limit=3'
    assert short_args({"subject": "周报"}) == 'subject="周报"'
    assert short_args({}) == ""
    long = short_args({"body": "x" * 200})
    assert len(long) == 80 and long.endswith("…")


def test_shorten_makes_one_line():
    assert shorten("a\n  b\tc", 10) == "a b c"
    assert shorten("abcdef", 4) == "abc…"


def test_tables_become_aligned_code_blocks():
    text = "Watchlist:\n\n| Symbol | 名称 | Close |\n|---|:---:|---:|\n| **RY** | 皇家银行 | 165.2 |\n| KO | 可口可乐 | 68 |\n\nDone."
    assert tables_to_code(text) == (
        "Watchlist:\n\n"
        "```\n"
        "Symbol  名称      Close\n"
        "------  --------  -----\n"
        "RY      皇家银行  165.2\n"
        "KO      可口可乐  68\n"
        "```\n\n"
        "Done."
    )


def test_text_without_tables_is_unchanged():
    text = "a | b is not a table\n- list item"
    assert tables_to_code(text) == text


def test_split_text():
    assert split_text("short", 100) == ["short"]
    assert split_text("   ", 100) == []
    parts = split_text("\n\n".join(["p" * 40] * 5), 100)
    assert parts == ["p" * 40 + "\n\n" + "p" * 40] * 2 + ["p" * 40]
    hard = split_text("x" * 250, 100)  # no spaces at all: hard cuts
    assert [len(p) for p in hard] == [100, 100, 50]
    words = split_text("word " * 60, 100)  # never cut inside a word
    assert all(len(p) <= 100 for p in words) and all(set(p.split()) == {"word"} for p in words)
    chinese = split_text("你好，世界。" * 30, 50)  # cut after Chinese punctuation
    assert all(len(p) <= 50 and p[-1] in "，。" for p in chinese[:-1])


def test_to_chat():
    assert to_chat("| a |\n|---|\n| 1 |", 1000) == ["```\na\n-\n1\n```"]
