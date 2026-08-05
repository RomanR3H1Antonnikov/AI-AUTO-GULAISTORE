import json
import logging
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

_TOXICITY_PROMPT = """\
Ты проверяешь сообщения покупателей на токсичность для службы поддержки магазина.

Считай токсичным, если сообщение содержит:
- Нецензурную лексику / мат
- Оскорбления в адрес магазина, персонала или бота
- Явный троллинг (бессмысленные провокации, попытки «сломать» бота)
- Спам (бессмысленный повторяющийся текст, ссылки на сторонние сайты)

КОНСЕРВАТИВНАЯ оценка — при малейшем сомнении считай НЕ токсичным:
- Раздражённый клиент («это возмутительно», «ужасный сервис») → НЕ токсично
- Сарказм («ну конечно, всё у вас есть») → НЕ токсично
- Эмоциональные вопросы → НЕ токсично

Сообщение: {message}

Ответь строго JSON:
{{"is_toxic": true/false, "reason": "одно предложение"}}"""


class ToxicityDetector:
    def __init__(self, client: AsyncOpenAI, model: str = "gpt-4o-mini"):
        self.client = client
        self.model = model

    async def classify(self, message: str) -> tuple[bool, str]:
        """
        Returns (is_toxic, reason).
        On LLM failure returns (False, ...) — conservative default.
        """
        try:
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": _TOXICITY_PROMPT.format(message=message)}],
                temperature=0,
                max_tokens=100,
                response_format={"type": "json_object"},
            )
            data = json.loads(resp.choices[0].message.content)
            is_toxic: bool = bool(data.get("is_toxic", False))
            reason: str = data.get("reason", "")
            logger.info("toxicity msg=%r is_toxic=%s reason=%s", message[:60], is_toxic, reason)
            return is_toxic, reason

        except Exception as exc:
            logger.error("Toxicity classification failed (assuming safe): %s", exc)
            return False, "classification unavailable"
