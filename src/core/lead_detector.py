import re
import json
import logging
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

# Keyword fallback — used when LLM call fails
_LEAD_PATTERNS = [
    r'\bберу\b',
    r'\bоформляем\b',
    r'готов\s+купить',
    r'хочу\s+купить',
    r'\bбронь\b',
    r'забронируй',
    r'\bотложи(те)?\b',
    r'зарезервируй',
    r'приеду\s+(завтра|сегодня|в\s+\d)',
    r'подъеду\s+(завтра|сегодня|в\s+\d|через)',
    r'буду\s+(завтра|сегодня|в\s+\d|через)',
    r'реквизит',
    r'скинь(те)?\s+номер\s+счёт',
    r'номер\s+(карты|счёт|для\s+перевода)',
    r'как\s+перевести\s+деньги',
    r'договорились',
]

_LEAD_PROMPT = """\
Ты классифицируешь сообщения покупателей в чате магазина Apple-техники.

Определи, является ли сообщение СИГНАЛОМ О ГОТОВНОСТИ К ПОКУПКЕ или ВИЗИТУ.

Критерии (достаточно одного):
- Назвал конкретное время приезда («буду завтра к 15», «подъеду вечером»)
- Просит бронь / отложить / зарезервировать товар
- Спрашивает реквизиты для безналичной оплаты или перевода денег
- Прямо выражает готовность купить («беру», «оформляем», «готов купить», «договорились»)
- Называет количество («беру два», «возьму три штуки», «нужно несколько») — даже если одновременно просит скидку

ВАЖНО: триггеры не исключают друг друга. «Дайте скидку, беру сразу два» — это ЛОПАСТЬ И торг И лид,
потому что «беру два» = явное намерение купить. Торг не отменяет сигнал о покупке.

НЕ является лидом:
- «Можно ли оплатить картой?» / «Принимаете СБП?» — это просто вопрос про способ оплаты
- «А сколько будет стоить если картой?» — уточнение цены, не намерение
- Вопросы про наличие, характеристики, гарантию, доставку
- «Спасибо», «понятно», приветствия

Сообщение покупателя: {message}

Ответь строго JSON:
{{"is_lead": true/false, "confidence": "high/medium/low", "reason": "одно предложение"}}"""


class LeadDetector:
    def __init__(self, client: AsyncOpenAI, model: str = "gpt-4o-mini"):
        self.client = client
        self.model = model

    def keyword_check(self, text: str) -> bool:
        t = text.lower()
        return any(re.search(p, t) for p in _LEAD_PATTERNS)

    async def classify(self, message: str) -> tuple[bool, str]:
        """
        Returns (is_lead, reason).
        Falls back to keyword matching if the LLM call fails.
        """
        try:
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": _LEAD_PROMPT.format(message=message)}],
                temperature=0,
                max_tokens=120,
                response_format={"type": "json_object"},
            )
            data = json.loads(resp.choices[0].message.content)
            is_lead: bool = bool(data.get("is_lead", False))
            reason: str = data.get("reason", "")
            confidence: str = data.get("confidence", "low")
            logger.info("lead_detect msg=%r is_lead=%s confidence=%s reason=%s",
                        message[:60], is_lead, confidence, reason)
            return is_lead, reason

        except Exception as exc:
            logger.error("LLM lead classification failed, using keywords: %s", exc)
            hit = self.keyword_check(message)
            return hit, "keyword match" if hit else "no match (LLM unavailable)"
