"""The subscription lane's tokens are work, not a bill.

On 2026-09-07 a ladder that put 24 cents on the OpenAI invoice reported
"$4.76 spent": forty Sonnet turns and one Fable turn on the Max subscription,
costed at API rates because nothing said they were not billed. That number
reaches the summary, the job card, the calibration store and the OVER BUDGET
log line. These tests pin the flag from the provider to every consumer."""
from __future__ import annotations

from types import SimpleNamespace

from docproof.contract import _cost_dict
from docproof.models import Usage
from docproof.providers.base import NormalizedUsage
from docproof.providers.catalog import (cost_of_usage,
                                        subscription_value_of_usage)

# The real by_model record of the ladder in question.
LUNA = dict(api_calls=169, input_tokens=223089, output_tokens=132270,
            cache_creation_input_tokens=0, cache_read_input_tokens=353917)
SONNET = dict(api_calls=40, input_tokens=80, output_tokens=228981,
              cache_creation_input_tokens=246159, cache_read_input_tokens=140812)


def _usage(**by_model):
    u = Usage()
    u.by_model = dict(by_model)
    return u


def test_every_provider_bills_by_default():
    assert NormalizedUsage().billed is True


def test_the_bucket_remembers_the_lane():
    u = Usage()
    u.add(NormalizedUsage(output_tokens=10, billed=False), model="claude-sonnet-5")
    assert u.by_model["claude-sonnet-5"]["billed"] is False
    # One billed call in the bucket makes it a bill.
    u.add(NormalizedUsage(output_tokens=10), model="claude-sonnet-5")
    assert u.by_model["claude-sonnet-5"]["billed"] is True
    # A usage object from before the flag existed is billed, as it always was.
    u.add(SimpleNamespace(input_tokens=5, output_tokens=5,
                          cache_creation_input_tokens=0,
                          cache_read_input_tokens=0), model="gpt-5.6-luna")
    assert u.by_model["gpt-5.6-luna"]["billed"] is True


def test_the_bill_is_the_openai_share_only():
    honest = _usage(**{"gpt-5.6-luna": LUNA,
                       "claude-sonnet-5": {**SONNET, "billed": False}})
    old_record = _usage(**{"gpt-5.6-luna": LUNA, "claude-sonnet-5": SONNET})
    luna_only = _usage(**{"gpt-5.6-luna": LUNA})

    bill = cost_of_usage(honest, fallback_model="claude-sonnet-5")
    assert bill == cost_of_usage(luna_only, fallback_model="claude-sonnet-5")
    # The record written before the flag still prices as it did — and it is
    # much bigger, which is the whole point.
    assert cost_of_usage(old_record, fallback_model="claude-sonnet-5") > bill * 5
    # What the subscription did is still visible, beside the bill.
    phantom = subscription_value_of_usage(honest, fallback_model="claude-sonnet-5")
    assert phantom > bill * 5
    assert subscription_value_of_usage(old_record, fallback_model="x") == 0.0


def test_an_all_subscription_run_is_priced_at_zero_not_unpriced():
    only_sub = _usage(**{"claude-sonnet-5": {**SONNET, "billed": False}})
    assert cost_of_usage(only_sub, fallback_model="claude-sonnet-5") == 0.0


def test_the_contract_lists_the_lane_at_zero():
    honest = _usage(**{"gpt-5.6-luna": LUNA,
                       "claude-sonnet-5": {**SONNET, "billed": False}})
    cost = _cost_dict(honest, "claude-sonnet-5")
    assert cost["by_model"]["claude-sonnet-5"] == 0.0
    assert cost["by_model"]["gpt-5.6-luna"] > 0
    assert cost["total_usd"] == cost["by_model"]["gpt-5.6-luna"]


def test_the_spend_line_names_the_subscription_share():
    from docproof.__main__ import _cost_line
    honest = _usage(**{"gpt-5.6-luna": LUNA,
                       "claude-sonnet-5": {**SONNET, "billed": False}})
    honest.api_calls = 209
    line = _cost_line(honest, "claude-sonnet-5")
    assert "spent (209 model call(s))" in line
    assert "subscription-lane turns, not billed" in line
    luna_only = _usage(**{"gpt-5.6-luna": LUNA})
    assert "subscription" not in _cost_line(luna_only, "claude-sonnet-5")
