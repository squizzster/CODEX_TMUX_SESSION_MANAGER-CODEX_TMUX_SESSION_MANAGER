import pytest

from rodex.native_composer import matches_composer


@pytest.mark.parametrize("glyph", ["\u203a", "»"])
@pytest.mark.parametrize(
    "rows,cursor,text,width",
    [
        (("Hello!",), 8, "Hello!", 120),
        (("Hello!",), 9, "Hello! ", 120),
        (("a foo-", "barbaz"), 8, "a foo-barbaz", 10),
        (("a foo/", "barbaz"), 8, "a foo/barbaz", 13),
        (("a foo—", "barbaz"), 8, "a foo—barbaz", 13),
        (("abad", "next", ""), 2, "abad  next", 7),
        (("Hello", "  world"), 9, "Hello\n  world", 120),
        (("Hello", "", "world"), 7, "Hello\n\nworld", 120),
        (("Hello", "世界"), 6, "Hello\n世界", 120),
    ],
)
def test_complete_native_composer_matches_both_glyphs_wraps_and_newlines(glyph, rows, cursor, text, width):
    rendered = (f"{glyph} {rows[0]}", *(f"  {row}" for row in rows[1:]))
    assert matches_composer(rendered, cursor, text, width)


@pytest.mark.parametrize(
    "rows,cursor,text",
    [
        (("\u203a Hello",), 7, "Hello!"),
        (("» Hello!",), 7, "Hello!"),
        (("\u203a Hello!",), 8, "Missing beginning\nHello!"),
        (("\u203a [Pasted Content 1001 chars]",), 29, "x" * 1001),
        (("\u203a a b",), 5, "a  b"),
        (("\u203a abc", "  xyz"), 5, "abcDEFxyz"),
        (("! pwd",), 5, "pwd"),
        (("\u203a a", "  b"), 3, "ab"),
        (("\u203a a", "  b"), 3, "a     b"),
    ],
)
def test_incomplete_or_different_composer_never_confirms(rows, cursor, text):
    assert not matches_composer(rows, cursor, text, 120)
