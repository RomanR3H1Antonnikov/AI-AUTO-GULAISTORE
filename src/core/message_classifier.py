"""Combined lead + toxicity classifier — one LLM call instead of two."""

import json
import logging
import re

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

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

_CLASSIFY_PROMPT = """\
Ты анализируешь сообщение покупателя в чате магазина Apple-техники.

Дай оценку по двум осям:

1. ТОКСИЧНОСТЬ — только явный мат, оскорбления, троллинг или спам.
   НЕ токсично: короткие ответы («да», «нет», «ок»), технические характеристики (SIM, память, цвет), раздражённый но вежливый клиент.
   При малейшем сомнении — НЕ токсично.

2. ЛИД — сигнал о готовности к покупке/визиту (хотя бы одно):
   • Назвал время приезда
   • Просит бронь / отложить / зарезервировать
   • Спрашивает реквизиты для оплаты
   • Явно выражает готовность купить («беру», «оформляем», «договорились»)
   • Называет количество («беру два»)
   НЕ лид: вопросы про наличие/цену/характеристики/доставку, «Можно картой?», «спасибо».

Сообщение: {message}

Ответь строго JSON:
{{"is_toxic": true/false, "toxic_reason": "одно предложение или пустая строка", "is_lead": true/false, "lead_reason": "одно предложение или пустая строка"}}"""


class MessageClassifier:
    def __init__(self, client: AsyncOpenAI, model: str = "gpt-4o-mini"):
        self.client = client
        self.model = model

    def _keyword_lead(self, text: str) -> bool:
        t = text.lower()
        return any(re.search(p, t) for p in _LEAD_PATTERNS)

    async def classify(self, message: str) -> tuple[bool, str, bool, str]:
        """
        Returns (is_toxic, toxic_reason, is_lead, lead_reason).
        Falls back to keyword matching for lead on LLM failure.
        """
        try:
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": _CLASSIFY_PROMPT.format(message=message)}],
                temperature=0,
                max_tokens=150,
                response_format={"type": "json_object"},
            )
            data = json.loads(resp.choices[0].message.content)
            is_toxic: bool = bool(data.get("is_toxic", False))
            toxic_reason: str = data.get("toxic_reason", "")
            is_lead: bool = bool(data.get("is_lead", False))
            lead_reason: str = data.get("lead_reason", "")
            logger.info(
                "classify msg=%r is_toxic=%s is_lead=%s",
                message[:60], is_toxic, is_lead,
            )
            return is_toxic, toxic_reason, is_lead, lead_reason

        except Exception as exc:
            logger.error("combined classifier failed, using fallback: %s", exc)
            hit = self._keyword_lead(message)
            return False, "classification unavailable", hit, "keyword match" if hit else ""
