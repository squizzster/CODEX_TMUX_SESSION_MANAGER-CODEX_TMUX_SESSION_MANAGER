from __future__ import annotations

import re
from pathlib import Path

import pytest

from rodex.status_bar import (
    PREFIX_MODE_STATUS_FORMAT,
    RODEX_BASE_STATUS_LEFT_FORMAT,
    RODEX_CONTEXT_STATUS_OPTION,
    RODEX_STATUS_COLOURS,
    RODEX_STATUS_LEFT_FORMAT,
    RODEX_STATUS_RIGHT_FORMAT,
    StatusBarPart,
    StatusBarSegment,
    TmuxStatusBar,
    compacting_status_segment,
    context_status_segment,
)


def test_base_status_selects_ctrl_b_banner_from_tmux_client_prefix_state() -> None:
    assert "#{client_prefix}" in RODEX_STATUS_LEFT_FORMAT
    assert "#{==:#{prefix},C-b}" in RODEX_STATUS_LEFT_FORMAT
    assert PREFIX_MODE_STATUS_FORMAT in RODEX_STATUS_LEFT_FORMAT
    assert RODEX_BASE_STATUS_LEFT_FORMAT in RODEX_STATUS_LEFT_FORMAT
    assert RODEX_CONTEXT_STATUS_OPTION in RODEX_BASE_STATUS_LEFT_FORMAT
    assert f"fg={RODEX_STATUS_COLOURS.primary_blue}" in RODEX_BASE_STATUS_LEFT_FORMAT


def test_status_palette_has_one_authoritative_value_per_colour_role() -> None:
    assert RODEX_STATUS_COLOURS.primary_blue == "#1402D8"
    assert RODEX_STATUS_COLOURS.tool_count == "cyan"
    assert RODEX_STATUS_COLOURS.mouse_mode == "yellow"
    assert RODEX_STATUS_COLOURS.context_compaction_foregrounds[0] == "colour45"
    assert "colour231" in RODEX_STATUS_COLOURS.context_compaction_foregrounds
    assert len(RODEX_STATUS_COLOURS.context_compaction_foregrounds) == 14
    assert f"fg={RODEX_STATUS_COLOURS.sharing_shared}" in RODEX_STATUS_RIGHT_FORMAT
    assert f"fg={RODEX_STATUS_COLOURS.sharing_private}" in RODEX_STATUS_RIGHT_FORMAT


@pytest.mark.evolutionary_regression
def test_context_alert_colours_render_without_rgb_terminal_support() -> None:
    """Current evidence: named alerts work without RGB; supersede with capability proof."""
    assert RODEX_STATUS_COLOURS.context_warning == "yellow"
    assert RODEX_STATUS_COLOURS.context_danger == "red"


def test_static_status_segments_render_their_own_colours_in_order() -> None:
    rendered_segments = (
        f"#[fg={RODEX_STATUS_COLOURS.primary_blue}]#[bold] Rodex: #S ",
        f"#[fg={RODEX_STATUS_COLOURS.tool_count}]#[bold]| Tools: #{{@rodex_tool_calls}} ",
        f"#[fg={RODEX_STATUS_COLOURS.mouse_mode}]#[bold]| Mouse: #{{?mouse,ON,OFF}} ",
    )
    positions = tuple(
        RODEX_BASE_STATUS_LEFT_FORMAT.index(item) for item in rendered_segments
    )
    assert positions == tuple(sorted(positions))


def test_status_bar_library_updates_only_the_named_part() -> None:
    status_bar = TmuxStatusBar(
        (
            StatusBarSegment(StatusBarPart.RODEX_IDENTITY, "blue", " Rodex "),
            StatusBarSegment(StatusBarPart.MOUSE_MODE, "yellow", "| Mouse "),
        )
    )
    updated = status_bar.modify_colour(StatusBarPart.RODEX_IDENTITY, "#1402D8")
    replaced = updated.update_status_bar(
        StatusBarPart.RODEX_IDENTITY,
        StatusBarSegment(StatusBarPart.RODEX_IDENTITY, "#1402D8", " Rodex: #S "),
    )
    assert replaced.render_part(StatusBarPart.RODEX_IDENTITY) == (
        "#[fg=#1402D8]#[bold] Rodex: #S "
    )
    assert replaced.render_part(StatusBarPart.MOUSE_MODE) == ("#[fg=yellow]#[bold]| Mouse ")
    assert status_bar.render_part(StatusBarPart.RODEX_IDENTITY) == (
        "#[fg=blue]#[bold] Rodex "
    )
    assert status_bar.render() == ("#[fg=blue]#[bold] Rodex #[fg=yellow]#[bold]| Mouse ")


def test_status_renderers_do_not_define_raw_tmux_colours_outside_the_theme() -> None:
    source_root = Path(__file__).parents[1] / "src" / "rodex"
    themed_modules = (
        "runtime.py",
        "status_animation.py",
        "tmux_shared_ctrl_c.py",
        "tmux_status.py",
    )
    raw_tmux_colour = re.compile(r"#\[(?:fg|bg)=(?!\{)")
    assert {
        module_name: raw_tmux_colour.findall(
            (source_root / module_name).read_text(encoding="utf-8")
        )
        for module_name in themed_modules
    } == {module_name: [] for module_name in themed_modules}


@pytest.mark.evolutionary_regression
def test_context_palette_does_not_change_the_independent_mouse_colour() -> None:
    """Current evidence: context cannot recolour Mouse; supersede only by contract."""
    assert "#[fg=yellow]#[bold]| Mouse: #{?mouse,ON,OFF}" in (RODEX_BASE_STATUS_LEFT_FORMAT)


@pytest.mark.parametrize(
    ("context_percent", "colour"),
    (
        (69.9, RODEX_STATUS_COLOURS.primary_blue),
        (70.0, RODEX_STATUS_COLOURS.context_warning),
        (74.4, RODEX_STATUS_COLOURS.context_warning),
        (75.0, RODEX_STATUS_COLOURS.context_danger),
        (79.4, RODEX_STATUS_COLOURS.context_danger),
        (80.0, RODEX_STATUS_COLOURS.context_danger),
    ),
)
def test_context_status_uses_the_compaction_warning_bands(
    context_percent: float,
    colour: str,
) -> None:
    rendered = context_status_segment(context_percent)
    assert f"fg={colour}" in rendered
    assert f"Context: {round(context_percent)}% |" in rendered


def test_context_status_has_stable_unavailable_and_compacting_states() -> None:
    assert f"fg={RODEX_STATUS_COLOURS.primary_blue}" in context_status_segment(None)
    assert "Context: -- |" in context_status_segment(None)
    assert "Context: 32% |" in context_status_segment(31.5)
    compacting_frames = tuple(compacting_status_segment(index) for index in range(14))
    assert "fg=colour45" in compacting_frames[0]
    assert "COMPACTING ▏ |" in compacting_frames[0]
    assert "fg=colour231" in compacting_frames[7]
    assert "COMPACTING █ |" in compacting_frames[7]
    assert len(set(compacting_frames)) == 14
    assert compacting_status_segment(14) == compacting_frames[0]
