"""Every combination of the paper/live gate (runtime-config spec, task 2.1).

The gate decides whether real orders can be sent, so it is tested exhaustively: both the
cases that must work and the many near-misses that must refuse to start.
"""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from contracts.mode import ModeConfigError, TradingMode, resolve_trading_mode

# ---- paper (the default) --------------------------------------------------------


@pytest.mark.parametrize("mode", [None, "", "paper"])
@pytest.mark.parametrize("confirm", [None, "", "yes"])
def test_paper_when_unset_empty_or_paper(mode: str | None, confirm: str | None) -> None:
    # Includes LIVE_CONFIRM=yes without live: harmless, stays paper.
    assert resolve_trading_mode(mode, confirm) is TradingMode.PAPER


def test_default_is_paper() -> None:
    assert resolve_trading_mode(None, None) is TradingMode.PAPER


# ---- live needs both flags, exactly ---------------------------------------------


def test_live_with_confirmation() -> None:
    assert resolve_trading_mode("live", "yes") is TradingMode.LIVE


@pytest.mark.parametrize("confirm", [None, ""])
def test_live_without_confirmation_refuses(confirm: str | None) -> None:
    with pytest.raises(ModeConfigError, match=r"LIVE_CONFIRM=yes"):
        resolve_trading_mode("live", confirm)


# ---- near-misses for TRADING_MODE: error, never paper ---------------------------


@pytest.mark.parametrize(
    "mode",
    ["Live", "LIVE", "Paper", "PAPER", " live", "live ", " paper", "paper ", "\tlive", "live\n"],
)
@pytest.mark.parametrize("confirm", [None, "", "yes"])
def test_wrong_case_or_whitespace_in_mode_refuses(mode: str, confirm: str | None) -> None:
    with pytest.raises(ModeConfigError, match=r"TRADING_MODE") as info:
        resolve_trading_mode(mode, confirm)
    assert "case-sensitive" in str(info.value)  # tells the operator why


@pytest.mark.parametrize(
    "mode", ["prod", "production", "demo", "test", "1", "0", "true", "false", "paper,live", "l"]
)
@pytest.mark.parametrize("confirm", [None, "", "yes"])
def test_unknown_mode_refuses_instead_of_falling_back_to_paper(
    mode: str, confirm: str | None
) -> None:
    with pytest.raises(ModeConfigError, match=r"TRADING_MODE must be 'paper' or 'live'"):
        resolve_trading_mode(mode, confirm)


# ---- near-misses for LIVE_CONFIRM: error in every mode --------------------------

BAD_CONFIRM = ["true", "True", "Yes", "YES", "y", "Y", "1", "no", "false", " yes", "yes ", "yes\n"]


@pytest.mark.parametrize("confirm", BAD_CONFIRM)
@pytest.mark.parametrize("mode", ["live", "paper", "", None])
def test_malformed_confirmation_refuses_in_every_mode(mode: str | None, confirm: str) -> None:
    with pytest.raises(ModeConfigError, match=r"LIVE_CONFIRM"):
        resolve_trading_mode(mode, confirm)


# ---- error messages -------------------------------------------------------------


def test_error_names_the_offending_value() -> None:
    with pytest.raises(ModeConfigError, match=r"'prod'"):
        resolve_trading_mode("prod", None)


# ---- the invariant, for arbitrary input -----------------------------------------


@given(st.one_of(st.none(), st.text()), st.one_of(st.none(), st.text()))
def test_live_only_for_exact_live_and_yes(mode: str | None, confirm: str | None) -> None:
    """For ANY input pair the result is LIVE if and only if mode == 'live' and confirm == 'yes';
    otherwise it is PAPER or an error. It can never return an unintended LIVE."""
    try:
        result = resolve_trading_mode(mode, confirm)
    except ModeConfigError:
        return
    assert (result is TradingMode.LIVE) == (mode == "live" and confirm == "yes")
