"""Recovered subscription usage retains its allowance-based billing identity."""
import pytest

from docproof.__main__ import _fold_usage
from docproof.fanout import fold_usage
from docproof.models import Usage
from docproof.providers import NormalizedUsage, cost_of_usage

MODEL = "gpt-5.6-luna"


def usage(billed):
    result = Usage()
    result.add(NormalizedUsage(input_tokens=1000, output_tokens=100, billed=billed), MODEL)
    return result


@pytest.mark.parametrize("fold", [fold_usage, _fold_usage])
def test_combining_subscription_usage_never_creates_an_api_bill(fold):
    total = Usage()
    fold(total, usage(False))
    fold(total, usage(False))
    assert total.by_model[MODEL]["billed"] is False
    assert total.input_tokens == 2000 and total.api_calls == 2
    assert cost_of_usage(total, fallback_model=MODEL) == 0


@pytest.mark.parametrize("fold", [fold_usage, _fold_usage])
@pytest.mark.parametrize("legacy_first", [False, True])
def test_legacy_unmarked_paid_calls_remain_billable(fold, legacy_first):
    old = usage(True)
    old.by_model[MODEL].pop("billed")
    total = Usage()
    for item in (old, usage(False)) if legacy_first else (usage(False), old):
        fold(total, item)
    assert total.by_model[MODEL]["billed"] is True
    assert cost_of_usage(total, fallback_model=MODEL) > 0


def test_mixed_paid_and_subscription_flags_stay_boolean():
    total = Usage()
    for billed in (False, True, False):
        fold_usage(total, usage(billed))
    assert total.by_model[MODEL]["billed"] is True
    assert total.api_calls == 3
