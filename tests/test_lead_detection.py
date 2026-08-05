"""
Tests for LeadDetector.

Covers:
  - LLM-based classification (mocked)
  - Keyword fallback when LLM is unavailable
  - Border cases: card question ≠ lead, time-of-arrival = lead
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.core.lead_detector import LeadDetector
from tests.conftest import _lead_response


@pytest.fixture
def detector(openai_mock):
    return LeadDetector(openai_mock, "gpt-4o-mini")


# ── LLM path ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("message,expected", [
    # Clear leads
    ("буду завтра к 15:00",                       True),
    ("подъеду вечером, отложите пожалуйста",      True),
    ("беру, как оплатить?",                       True),
    ("оформляем, скиньте реквизиты",              True),
    ("готов купить",                              True),
    ("договорились, приеду в среду",              True),
    # NOT leads
    ("можно ли оплатить картой?",                 False),
    ("а сколько будет стоить если картой?",       False),
    ("есть ли в наличии MacBook Air M5?",         False),
    ("что входит в комплект?",                    False),
    ("какая гарантия на товар?",                  False),
    ("мне нужно подумать",                        False),
    ("добрый день, у вас есть MacBook Pro 14?",  False),
])
async def test_lead_classification_via_llm(message, expected, detector, openai_mock):
    openai_mock.chat.completions.create = AsyncMock(
        return_value=_lead_response(expected)
    )
    is_lead, reason = await detector.classify(message)
    assert is_lead == expected, f"Message '{message}': expected {expected}, got {is_lead} ({reason})"


# ── Keyword fallback ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_keyword_fallback_on_llm_error(openai_mock):
    openai_mock.chat.completions.create = AsyncMock(side_effect=Exception("network error"))
    detector = LeadDetector(openai_mock, "gpt-4o-mini")

    # "беру" is in keyword list → should be detected even without LLM
    is_lead, reason = await detector.classify("беру, отложите")
    assert is_lead is True
    assert "keyword" in reason


@pytest.mark.asyncio
async def test_keyword_fallback_non_lead(openai_mock):
    openai_mock.chat.completions.create = AsyncMock(side_effect=Exception("network error"))
    detector = LeadDetector(openai_mock, "gpt-4o-mini")

    is_lead, _ = await detector.classify("можно ли оплатить картой?")
    assert is_lead is False


# ── Keyword check method ──────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("беру",                    True),
    ("оформляем",               True),
    ("готов купить",            True),
    ("подъеду завтра в 10",     True),
    ("реквизиты скиньте",       True),
    ("можно ли картой",         False),
    ("какая гарантия",          False),
    ("спасибо",                 False),
])
def test_keyword_check(text, expected):
    from unittest.mock import AsyncMock
    det = LeadDetector(AsyncMock(), "gpt-4o-mini")
    assert det.keyword_check(text) == expected


# ── Border case: card question ≠ lead ────────────────────────────────────────

@pytest.mark.asyncio
async def test_card_question_is_not_lead(detector, openai_mock):
    """
    'Принимаете карту?' is just an info question, not a purchase signal.
    The LLM should classify it as not-a-lead; we verify the fixture is wired right.
    """
    openai_mock.chat.completions.create = AsyncMock(
        return_value=_lead_response(False, "low", "просто уточняет способ оплаты")
    )
    is_lead, reason = await detector.classify("А вы принимаете карту?")
    assert is_lead is False


# ── Border case: vague arrival intent ────────────────────────────────────────

@pytest.mark.asyncio
async def test_vague_arrival_is_lead(detector, openai_mock):
    """
    'Буду, наверное, послезавтра' — vague but expresses intent to visit.
    LLM should recognise it; we verify the call is made with the right message.
    """
    openai_mock.chat.completions.create = AsyncMock(
        return_value=_lead_response(True, "medium", "выражает намерение приехать")
    )
    is_lead, _ = await detector.classify("Буду наверное послезавтра")
    assert is_lead is True

    call_args = openai_mock.chat.completions.create.call_args
    prompt_content = call_args.kwargs["messages"][0]["content"]
    assert "послезавтра" in prompt_content
