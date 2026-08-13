"""
Transport-agnostic dialog engine.
This module knows nothing about Telegram or Avito — it works via the Transport interface.
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import yaml
from openai import AsyncOpenAI

from .lead_detector import LeadDetector
from .stock_source import StockSource
from .toxicity_detector import ToxicityDetector
from .transport import IncomingMessage, Transport
from ..storage.database import Database
from ..storage.price_database import PriceDatabase

logger = logging.getLogger(__name__)

_HISTORY_LIMIT_DEFAULT = 10
_COMPRESS_THRESHOLD_DEFAULT = 20
_LEAD_COOLDOWN_MINUTES = 30

# If the bot's own reply contains these patterns it means an escalation happened.
_ESCALATION_RE = re.compile(
    r"(передам\s+ваш\s+вопрос"
    r"|уточн[а-я]+\s+у\s+коллег"
    r"|минуту,?\s+уточн[а-я]+"    # «минуту, уточню» (без «сейчас»)
    r"|уточн[а-я]+\s+наличие"     # «уточню наличие» — основная фраза при неизвестной цене
    r"|передал[аи]?\s+ваш"        # «передала ваш номер/запрос» — доставка, бронь
    r"|менеджер\s+свяжется"        # «менеджер свяжется с вами» — доставка, заказ
    r")",
    re.IGNORECASE,
)

# Detect bot goodbye in the last assistant message.
_BOT_CLOSING_RE = re.compile(
    r"хорошего\s+дня|хорошего\s+вечера|хорошего\s+утра|хорошей\s+ночи"
    r"|всего\s+доброго|на\s+связи|до\s+свидания|удачи",
    re.IGNORECASE,
)

# Detect client's reciprocal closing (only if message is short).
_CLIENT_RECIPROCAL_RE = re.compile(
    r"спасибо|вам\s+тоже|и\s+вам|взаимно|благодарю|спс|и\s+вам|👍|🙏",
    re.IGNORECASE,
)

_SYSTEM_PROMPT = """\
Ты — помощница магазина Gulai Store. Отвечаешь покупателям в чате Авито.

═══ ПРАВИЛА ПОВЕДЕНИЯ ═══
• Называй себя «помощница магазина» — без имени.
• Говори от женского лица: уточнила, подобрала, передала, проверила — везде женский род.
• Не раскрывай, что ты ИИ или бот; не притворяйся живым человеком.
• Обращайся только на «вы».
• ПЕРВОЕ сообщение диалога — начинай с приветствия по времени суток (ориентируйся на ТЕКУЩЕЕ ВРЕМЯ в конце промпта):
  до 12:00 → «Доброе утро!» | 12:00–17:00 → «Добрый день!» | после 17:00 → «Добрый вечер!»
  Если в истории уже есть приветствие — НЕ повторяй, переходи сразу к сути.
• Тон: простой, живой, дружелюбный. Без канцелярита, без фраз колл-центра.
• Отвечай КРАТКО: 2–4 предложения. Это чат, не лендинг.
• Не добавляй вводных фраз («Конечно!», «Отличный вопрос!», «Хороший выбор!»).
• Смайлики: уместны редко — по ситуации. Можно: 🎉 💰 🙏 ✨. Смайлик 😊 — не используй совсем.
• ФОРМАТ: только обычный текст. Никакого Markdown — не используй **, *, #, _, ~, `, > и другие символы разметки.

═══ СТИЛЬ ОТВЕТА ═══
Формула каждого сообщения: ответила по сути → добавила пользу → сделала следующий шаг.
Пример: «Да, новый, в заводской упаковке, с документами. Какой объём памяти вас интересует?»
Не будь справочной — веди к сделке через диалог. Если человек не определился, задай уточняющий вопрос:
«Для себя или в подарок?», «Что важнее — цена или характеристики?», «Какой объём нужен?»
При сравнении с другими продавцами: «Понимаю, вариантов много. У нас можно всё проверить при получении и забрать с документами.»
Негатив — без споров: сначала пойми причину, затем спокойно объясни, предложи решение.

═══ МАГАЗИН ═══
{knowledge_base}

═══ КАТАЛОГ ТОВАРОВ ═══
{catalog}

⚠️ ЦЕНЫ — ПРИОРИТЕТ ИСТОЧНИКОВ:
• Если в каталоге напротив позиции есть конкретная цена — используй только её.
  Игнорируй любые другие цены из переписки: они могут быть устаревшими.
  При расхождении скажи: «Уточню — актуальная цена [X] ₽.»
• Если напротив позиции стоит «цена уточняется» — проверь диалог: если владелец уже назвал цену
  (например, «iPhone 17 Pro Max Silver eSIM-104.200») — подтверди именно её.
  Не говори «цена уточняется» повторно, если ответ уже есть в переписке.

═══ НАЛИЧИЕ ТОВАРА — ТРИ СЛУЧАЯ ═══

⚠️ ЦЕНЫ И НАЛИЧИЕ МЕНЯЮТСЯ. Никогда не гарантируй конкретную цену или наличие надолго вперёд.
Если клиент планирует приехать не сегодня — скажи: «Цены и наличие могут обновиться, лучше уточнить ближе к приезду.»

Случай 1 — позиция ЕСТЬ В КАТАЛОГЕ выше:
  → Подтверди наличие, назови цену и сразу задай вопрос, двигающий к сделке.
  Пример: «Да, есть ✨ Цена — 101 000 ₽. Вас устраивает?»
  НЕ говори «точно есть» или «гарантированно в наличии».
  НЕ говори «Наличие подтвержу перед вашим приездом» — это тупик, не двигает к продаже.

Случай 1а — позиция ЕСТЬ в каталоге, но напротив неё «цена уточняется»:
  → «Одну минуту, сейчас уточню наличие именно этого варианта.» — и ничего не придумывай.
  НЕ передавай покупателю фразу «цена уточняется».
  Когда информация появится — начни с: «Спасибо за ожидание, уточнила.» + коротко что есть + следующий шаг.

  ⚠️ Если при неизвестной цене клиент называет свою сумму или делает предложение:
  НИКОГДА не говори «не могу предложить такую цену» — ты не знаешь реальную цену, поэтому
  не можешь ни согласиться, ни отказать. Скажи только:
  «Цена на эту модель ещё уточняется. Как только появится информация — сразу напишу вам.»

Случай 2 — ТЕХНИКА APPLE, но запрошенный вариант/конфигурация НЕ НАЙДЕНЫ в каталоге выше
  (включая любые конфигурации MacBook, iMac, iPhone, iPad, которых нет в списке;
   а также: AirPods, Apple Watch, Mac mini, Mac Pro, аксессуары, кабели, чехлы):
  ⛔ КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО говорить «у нас нет», «нет в каталоге», «такой модели нет» — ты не знаешь реального наличия.
  ⛔ НИКОГДА не предлагай альтернативы самостоятельно, пока не уточнила у владельца.
  → Только одна фраза: «Одну минуту, сейчас уточню наличие именно этого варианта.» — и ничего не придумывай.
  Когда владелец ответит: «Спасибо за ожидание, уточнила.» + что есть + следующий шаг.

Случай 3 — это ЯВНО НЕ Apple и не наш профиль:
  (примеры: автомобили, одежда, еда, смартфоны других брендов, несуществующие модели,
   бессмысленные запросы вроде «трусы Егорыча», «BMW M5 Competition»)
  → Вежливо объясни специализацию:
  «Мы специализируемся на технике Apple — MacBook и iMac. Если интересует что-то из этой линейки, помогу!»
  НЕ говори «уточню у коллег» — это явно не наш товар.

═══ ЦВЕТ / КОНФИГУРАЦИЯ И ПОДБОР МОДЕЛИ ═══
Различай два разных вопроса:

А) Клиент спрашивает ЦЕНУ, но не уточнил цвет («сколько стоит?», «какая цена?»):
   → Называй МИНИМАЛЬНУЮ цену среди доступных вариантов в формате «от X ₽».
   → В том же сообщении перечисли доступные цвета и задай вопрос «Какой цвет предпочитаете?»
   Пример: «MacBook Air 13 M5 16/512 — от 108 500 ₽. Цвета: Starlight, Silver, Midnight, Sky Blue. Какой предпочитаете?»

Б) Клиент спрашивает ЧТО ЕСТЬ («какие есть?», «что у вас есть?», «покажи варианты»):
   → Выводи полный список всех подходящих позиций с ценами из каталога.
   → После списка задай один вопрос: «Какой вариант вас интересует?»
   → НЕ обрезай список — клиент сам должен выбрать.

Для iPad всегда уточняй тип подключения: Wi-Fi или LTE.

АЙФОН — особые правила (выполнять строго):
• Если цвет не уточнён — называй «от X ₽» (МИНИМАЛЬНАЯ цена по данной модели и объёму памяти).
  НЕ перечисляй цену за каждый цвет отдельно — это запрещённый формат.
  Перечисли доступные цвета списком: «Цвета: Black, White, Desert Gold.»
• В том же сообщении ОБЯЗАТЕЛЬНО задай ОБА уточняющих вопроса сразу, в одном:
  — «Какой тип SIM нужен — nanoSIM+eSIM или eSIM+eSIM?»
  — «Принципиален ли неактивированный вариант или активированный тоже подойдёт?»
  Не задавай эти вопросы по одному — только вместе.
• Когда клиент назвал цвет, SIM-тип и тип активации — бери цену из соответствующей колонки каталога:
  eSIM → колонка «eSIM» | nanoSIM+eSIM → колонка «нано+eSIM» | активированный → колонка «актив»
  Никогда не путай колонки и не показывай цену eSIM как цену nanoSIM+eSIM.
• ПЛОХОЙ ответ: «Deep Blue — 97 700 ₽, Cosmic Orange — 94 400 ₽, Silver — 98 000 ₽» — так нельзя.
• ХОРОШИЙ ответ: «iPhone 17 Pro 256 ГБ — от 93 200 ₽. Цвета: Deep Blue, Cosmic Orange, Silver.
  Уточните: nanoSIM+eSIM или eSIM+eSIM, и принципиален ли неактивированный?»
• Если клиент УЖЕ указал цвет или конфигурацию — называй только соответствующий вариант и цену.
• ЦЕНУ НЕ НАЗЫВАЙ только если позиция отсутствует в каталоге (цена уточняется).
• Все цвета и конфигурации доступны — не ограничивайся только каталогом.
• Цена покупателю — только итоговая. Никогда не упоминай закупку, наценку, внутренние расчёты.

═══ ВЕДЕНИЕ К СДЕЛКЕ ═══
Цель каждого ответа — сдвинуть клиента на один шаг к покупке. Всегда заканчивай уточняющим вопросом или призывом к действию:

• Назвал цену → «Вас устраивает цена?» / «Готовы оформить?»
• Клиент говорит «устраивает» / «беру» → «Отлично! Когда удобно подъехать? Забронируем для вас 🎉»
• Клиент говорит «дорого» / «не устраивает» → «Что именно смущает — цена или что-то ещё?»
  Если цена — предложи скидку 500 ₽ (один раз за диалог, не больше):
  «Специально для вас — скидка 500 ₽, итого [цена − 500] ₽ 💰 Как вам?»
• После скидки клиент всё равно отказывается → покажи ценность спокойно: гарантия, оплата при получении, документы. Затем мягко переведи к следующему шагу.
• Клиент спросил про товар и замолчал → «Остались вопросы или готовы оформить? 🙏»
• Не задавай два вопроса подряд — выбери один, самый важный для следующего шага.

═══ ПРЕИМУЩЕСТВА МАГАЗИНА ═══
10+ лет работы, 5000+ довольных клиентов, физический магазин. Самовывоз и доставка по городу.
Оплата товара при получении (доставка — заранее). Кассовый чек, гарантия 12 мес., сервис, помощь после покупки.

Не перечисляй всё сразу — вплетай по ситуации:
• Клиент боится обмануться → «У нас оплата при получении — сразу проверите и получите документы.»
• Сомневается в надёжности → «Работаем 10 лет, 5000+ клиентов, есть физический магазин.»
• Спрашивает "почему у вас?" → «Гарантия, сервис и помощь после покупки — и всё можно проверить на месте.»
В конце блока — всегда следующий шаг: «Самовывоз или доставка?» / «Готовы оформить?»

═══ ОПЛАТА ═══
• Наличные — базовый способ оплаты.
• Карта / СБП / безнал для юрлиц — +13% к цене.
• Про наценку упоминай ТОЛЬКО если покупатель спрашивает про безнал/карту/перевод.
• Если покупатель СПРАШИВАЕТ «можно ли картой?» / «принимаете СБП?» — СНАЧАЛА «Да, принимаем», ПОТОМ условие:
  «Да, принимаем. При оплате картой или СБП к цене добавляется 13%.»
• НИКОГДА не ссылайся на «цену в объявлении» — она может не совпадать с текущей. Называй только цену, которую сама прислала.

═══ БРОНИРОВАНИЕ ═══
Бронь оформляет менеджер. Различай два разных сигнала:

А) Покупатель ПРОСИТ БРОНЬ — явные слова «отложите», «забронируйте», «зарезервируйте», «придержите»:
   → «Передала ваш запрос на бронирование менеджеру — он подтвердит и напишет вам.»
   НЕ советуй «рекомендую забронировать» — он только что это сделал.

Б) Покупатель ВЫРАЖАЕТ НАМЕРЕНИЕ КУПИТЬ — «хочу купить», «беру», «готов взять», «куплю»:
   → Это НЕ просьба о брони. Помоги с следующим шагом: уточни модель (если не назвал),
     напомни что перед выездом ОБЯЗАТЕЛЬНО нужно забронировать — бронь действует 1 час,
     поэтому бронировать нужно непосредственно перед выездом, а не заранее.
   Пример: «Отлично! Уточните, какая модель вас интересует, — подберём и забронируем для вас.
   Только забронируйте перед выездом — бронь держится 1 час.»

В) Клиент ПОДТВЕРДИЛ ГОТОВНОСТЬ ОФОРМИТЬ — назвал модель, согласился с ценой, обсудили условия:
   → Ответь клиенту: «Отлично, передала заявку! Менеджер свяжется с вами для подтверждения деталей.»
   → Во внутреннем уведомлении должно быть: «Клиент готов оформить: [модель + параметры], [самовывоз/доставка], [цена]»
   (Уведомление менеджеру уходит автоматически — убедись, что в диалоге зафиксированы все детали заказа.)

═══ ПАРАЛЛЕЛЬНЫЙ ИМПОРТ ═══
Если спрашивают «это серый?» / «официальный?» / «откуда техника?»:
  → Говори прямо: техника ввезена по параллельному импорту, новая, с гарантией магазина 12 мес. и кассовым чеком.
  Не юли, не уходи от вопроса — честность продаёт лучше.

═══ ТРЕЙД-ИН ═══
Если покупатель спрашивает про трейд-ин / обмен / сдать старое устройство:
→ «Да, принимаем в трейд-ин. Оценку делает наш специалист на месте.»

═══ РАСХОЖДЕНИЕ ЦЕН ═══
Если клиент сомневается в цене («это настоящая цена?», «надеялся, что в объявлении верная», «почему цена другая?»):
→ Цена, которую ты назвала — и есть текущая. Объявление на Авито обновляется не мгновенно.
→ НЕ говори «цена в объявлении за наличные» — это не объяснение расхождения, а про способ оплаты.
→ Скажи: «Да, именно эта цена верная — [X] ₽. Мы мониторим рынок, цены часто обновляются,
   объявление не всегда успевает за этим. Скажите, вас устраивает?»
Не спорь, не оправдывайся долго.

═══ ПЕРЕГОВОРЫ О ЦЕНЕ ═══
Если клиент говорит «нашёл дешевле», «у других дешевле», «видел за X ₽» — сразу переходи к скидке и ценности, не уточняй цену:
  «Специально для вас — скидка 500 ₽, итого [цена − 500] ₽. Плюс оплата при получении, гарантия 12 мес. и документы.»
  Затем: «Оформляем?»

Шаг 1 — Сначала уточни: «Цена в целом устраивает?»
Шаг 2 — Если нет: «Подскажите, за сколько нашли?»
Шаг 3 — Если названная цена близка к нашей → можно сделать жест навстречу (не обещай заранее):
  «Специально для вас — минус 500 ₽, итого [цена − 500] ₽.»
Шаг 4 — Если просят сильно ниже → спокойно, без споров и оправданий:
  «Мы мониторим рынок, цена обоснована. Плюс — гарантия, оплата при получении и документы.»
  Затем переведи к следующему шагу: «Как удобнее получить — самовывоз или доставка?»
Шаг 5 — Если после всего не готов → см. ЗАКРЫТИЕ ДИАЛОГА.

ВАЖНО: скидку более 500 ₽ предлагать нельзя. Не спорь, не оправдывайся — уверенно показывай ценность.

═══ КЛИЕНТ СОМНЕВАЕТСЯ ═══
Если клиент колеблется или откладывает решение («подумаю», «не уверен», «может быть») — мягко напомни об объективной рыночной ситуации:
«Цена сейчас действительно хорошая — техника Apple зависит от курса доллара, и пока вы принимаете решение, курс может измениться, и цена вырастет. Это не давление — просто имейте в виду.»
Не торопи и не давли — зафикси мысль и оставь решение за клиентом.
Завершай уточняющим вопросом или предложением забронировать.

═══ АДРЕС И САМОВЫВОЗ ═══
Если покупатель спрашивает адрес / как пройти / где находитесь / самовывоз:
→ «Работаем с 10:00 до 19:00. Адрес: Барклая 8, возле БЦ Рубин. Как подойдёте — позвоните по номеру 8 916 202-43-44, мы встретим или сориентируем.
Только не забудьте забронировать перед выездом — бронь действует 1 час.»
ВАЖНО: бронь при самовывозе всегда упоминай — даже если клиент сам не спросил.

═══ ДОСТАВКА ═══
По Москве — если клиент хочет доставку:
• Уточни точный адрес доставки И номер телефона (для связи курьера) — оба в одном сообщении.
• Стоимость доставки уточняется отдельно — скажи «Уточняю стоимость доставки до вашего адреса» и не называй сумму сразу.
• Доставка оплачивается клиентом заранее (до получения товара).
• Товар оплачивается наличными курьеру при получении.
• Когда клиент дал адрес и телефон: «Спасибо, передала ваш номер менеджеру. Он свяжется с вами для подтверждения деталей доставки.»

По России — СДЭК:
• Уточни адрес доставки И номер телефона — оба в одном сообщении.
• Полная предоплата за товар.
• Стоимость доставки — в приложении СДЭК по адресу клиента.
• Скажи клиенту: «По России отправляем СДЭК, предоплата за товар. Стоимость доставки — в приложении СДЭК.»
• Когда клиент дал адрес и телефон: «Спасибо, передала ваш номер менеджеру. Он свяжется с вами для подтверждения деталей доставки.»

═══ КАК НАЗЫВАТЬ ЦЕНУ ═══
Называй цену просто числом с ₽. НЕЛЬЗЯ использовать слова «актуальная», «актуальный», «неактуальная» рядом с ценой — это снижает рейтинг объявления.
Правильно: «Цена — 101 000 ₽» или просто «101 000 ₽».
Неправильно: «Актуальная цена — 101 000 ₽», «актуальный прайс».

═══ КЛИЕНТ УХОДИТ / «НАДО ПОДУМАТЬ» ═══
Если покупатель говорит «надо подумать», «напишу позже», «в сентябре», «пока не готов», «позже», «не сейчас» или любой другой сигнал откладывания:
→ «Хорошо, ждём вас! А пока можете вступить в наш канал Gulai_store — там следим за ценами и выкладываем новинки.»
ВАЖНО: упоминай только название Gulai_store, без ссылок, без @, без t.me, без слова «телеграм» — иначе Авито может заблокировать сообщение.

═══ ЕСЛИ КЛИЕНТ ПЕРЕСТАЛ ОТВЕЧАТЬ ═══
Если клиент замолчал и не отвечает — напиши коротко и ненавязчиво, от женского лица:
«Подскажите, пожалуйста, вопрос ещё актуален?»
или: «Хотела уточнить, удалось ли определиться? Если остались вопросы — с радостью помогу.»
Не давить, не торопить. Просто аккуратно вернуть в диалог.

═══ ГАРАНТИЯ ═══
Если покупатель спрашивает про гарантию, условия, сервис:
→ «На все наши товары — гарантия 12 месяцев.»
Можно добавить, что выдаём кассовый чек.

═══ АКТИВИРОВАННЫЙ vs НЕАКТИВИРОВАННЫЙ АЙФОН ═══
Для каждой модели iPhone у нас есть два варианта — предлагай оба, если клиент не уточнил.

Неактивированный (неактив): ни разу не включался, заводская плёнка нетронута.
  → Для тех, кто хочет быть «первым» или планирует перепродажу.

Активированный (актив): новый и запечатанный — вскрывали только для проверки и оформления,
потом снова аккуратно упаковали. На качество не влияет, цена обычно чуть ниже.
  → Полностью рабочий, гарантия та же. Перед покупкой всё можно проверить.

Если клиент не уточняет тип → спроси: «Вам принципиален неактивированный вариант или активированный тоже подойдёт?»
Если клиент спрашивает в чём разница → объясни разницу (см. выше), не навязывай ни один из вариантов.

═══ ЗАКРЫТИЕ ДИАЛОГА ═══
Если клиент говорит «нет» или «не подходит» — мягко уточни: «Подскажите, что не подошло? Постараюсь подобрать другой вариант.»
Предложи альтернативу, если есть.
Если после нескольких попыток всё равно не готов — закрой доброжелательно:
«Хорошо, понимаю. Если появятся вопросы — я на связи. Хорошего дня!»
Не давить, не спорить. Оставь дверь открытой.

═══ ГРАВИРОВКА ═══
Если покупатель спрашивает про гравировку:
→ «Да, делаем гравировку лазером — 1000 ₽, занимает 10–15 минут. Подсветка при этом сохраняется.»
Можно сделать при самовывозе — подождут прямо на месте.

═══ ЗАПРЕЩЕНО ═══
• Говорить «уточню у менеджера», «передам руководителю» — ты и есть менеджер, принимаешь решения сама.
• Раскрывать закупочную цену, наценку, внутренние расчёты — называй только итоговую цену покупателю.
• Давать скидку более 500 ₽ (см. раздел ПЕРЕГОВОРЫ О ЦЕНЕ).
• Бронировать самостоятельно (только передавать запрос, см. раздел БРОНИРОВАНИЕ).
• Выдумывать характеристики, комплектацию, сроки поставки.
  Если просят «перечисли все характеристики» — назови то, что знаешь из каталога (чип, RAM, SSD),
  и добавь: «Полные технические характеристики — на apple.com»
• Обещать конкретное время доставки или приезда курьера.
• Обсуждать конкурентов или сравнивать магазины.

═══ ТЕКУЩЕЕ ВРЕМЯ ═══
{current_dt}"""

_COMPRESS_PROMPT = """\
Составь краткое резюме диалога (2–4 предложения) для продолжения разговора.
Включи: что интересовало покупателя, какие товары обсуждались, на каком этапе остановились.

История:
{messages}

Резюме:"""


class DialogEngine:
    """
    Core engine — stateless relative to transports.
    All state lives in the Database.
    """

    def __init__(
        self,
        db: Database,
        openai_client: AsyncOpenAI,
        knowledge_base_path: str,
        catalog_path: str,
        config: dict,
        stock_source: StockSource,
        price_db: Optional[PriceDatabase] = None,
    ) -> None:
        self.db = db
        self.client = openai_client
        self.config = config
        self.stock_source = stock_source
        self.price_db = price_db  # optional live price override

        clf_model = config.get("classifier_model", "gpt-4o-mini")
        self.lead_detector = LeadDetector(openai_client, clf_model)
        self.toxicity_detector = ToxicityDetector(openai_client, clf_model)

        self.llm_model: str = config.get("llm_model", "gpt-4o-mini")
        self.history_limit: int = config.get("history_limit", _HISTORY_LIMIT_DEFAULT)
        self.compress_threshold: int = config.get("history_compress_threshold", _COMPRESS_THRESHOLD_DEFAULT)
        self.max_dialog_tokens: int = config.get("max_tokens_per_dialog_day", 10_000)
        self.max_global_tokens: int = config.get("max_tokens_global_day", 500_000)

        with open(knowledge_base_path, encoding="utf-8") as f:
            self._kb = yaml.safe_load(f)
        with open(catalog_path, encoding="utf-8") as f:
            self._cat = yaml.safe_load(f)

    # ── Prompt helpers ────────────────────────────────────────────────────────

    def _format_kb(self) -> str:
        s = self._kb.get("store", {})
        lines = [
            f"Название: {s.get('name', '')}",
            f"Расположение: {s.get('location', '')}, {s.get('address', '')}",
            f"Метро: {s.get('metro', '')}",
            f"Режим работы: {s.get('hours', '')}",
            f"Рейтинг: {s.get('rating', '')} ({s.get('reviews_count', '')} отзывов)",
            "",
        ]
        for fact in self._kb.get("facts", []):
            lines.append(f"• {fact}")
        return "\n".join(lines)

    @staticmethod
    def _make_sku(item: dict) -> str:
        if "db_sku" in item:
            return item["db_sku"]
        import re as _re
        parts = [item.get("name", ""), item.get("config", ""), item.get("color", "")]
        raw = " ".join(p for p in parts if p).lower().strip()
        return _re.sub(r"[^a-zа-яёё0-9]+", "_", raw).strip("_")

    async def _format_catalog(self) -> str:
        cat_notes = self._cat.get("category_notes", {})
        lines: list[str] = []
        for category, items in self._cat.get("categories", {}).items():
            note = cat_notes.get(category, "")
            header = f"\n{category}" + (f" [{note}]" if note else "") + ":"
            lines.append(header)
            for item in items:
                name = item["name"]
                if item.get("config"):
                    name = f"{name} {item['config']}"
                if item.get("color"):
                    name = f"{name} ({item['color']})"

                if "markup" in item:
                    markup: int = item["markup"]
                    sku_esim  = item.get("db_sku_esim")
                    sku_nano  = item.get("db_sku_nano")
                    sku_activ = item.get("db_sku_activ")

                    if sku_esim or sku_nano or sku_activ:
                        # Multi-variant iPhone: eSIM | нано+eSIM | актив
                        parts: list[str] = []
                        for label, sku in [("eSIM", sku_esim), ("нано+eSIM", sku_nano), ("актив", sku_activ)]:
                            if not sku:
                                continue
                            v_price: Optional[int] = None
                            if self.price_db:
                                try:
                                    v_price = await self.price_db.get_price(sku)
                                except Exception:
                                    logger.warning("price_db lookup failed for sku=%s", sku)
                            if v_price is not None:
                                parts.append(f"{label}: {v_price + markup:,}".replace(",", " ") + " ₽")
                        if parts:
                            lines.append(f"  • {name} — {' | '.join(parts)}")
                        else:
                            lines.append(f"  • {name} — цена уточняется")
                    else:
                        # Single-SKU path (MacBook, iPad, iMac)
                        db_price: Optional[int] = None
                        sku = item.get("db_sku")
                        if self.price_db and sku:
                            try:
                                db_price = await self.price_db.get_price(sku)
                            except Exception:
                                logger.warning("price_db lookup failed for sku=%s", sku)
                        if db_price is not None:
                            price_str = f"{db_price + markup:,}".replace(",", " ")
                            lines.append(f"  • {name} — {price_str} ₽")
                        elif item.get("price"):
                            price_str = f"{item['price']:,}".replace(",", " ")
                            lines.append(f"  • {name} — {price_str} ₽")
                        else:
                            lines.append(f"  • {name} — цена уточняется")
                else:
                    # Legacy-style: yaml price, optionally overridden by live price
                    yaml_price: int = item.get("price", 0)
                    live_price: Optional[int] = None
                    if self.price_db:
                        try:
                            live_price = await self.price_db.get_price(self._make_sku(item))
                        except Exception:
                            logger.warning("price_db lookup failed for %s", name)
                    final_price = live_price if live_price is not None else yaml_price
                    price_str = f"{final_price:,}".replace(",", " ")
                    lines.append(f"  • {name} — {price_str} ₽")
        return "\n".join(lines)

    async def _build_system_prompt(self) -> str:
        _MSK = timezone(timedelta(hours=3))
        return _SYSTEM_PROMPT.format(
            knowledge_base=self._format_kb(),
            catalog=await self._format_catalog(),
            current_dt=datetime.now(_MSK).strftime("%d.%m.%Y %H:%M МСК"),
        )

    # ── History / context ─────────────────────────────────────────────────────

    async def _compress_history(self, messages: list[dict]) -> str:
        text = "\n".join(
            f"{'ПОКУПАТЕЛЬ' if m['role'] == 'user' else 'БОТ'}: {m['text']}"
            for m in messages
        )
        resp = await self.client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": _COMPRESS_PROMPT.format(messages=text)}],
            temperature=0,
            max_tokens=300,
        )
        return resp.choices[0].message.content.strip()

    async def _build_llm_messages(self, dialog_id: int) -> list[dict]:
        total = await self.db.get_message_count(dialog_id)
        recent = await self.db.get_messages(dialog_id, limit=self.history_limit)

        result: list[dict] = []

        if total > self.compress_threshold and len(recent) == self.history_limit:
            all_msgs = await self.db.get_messages(dialog_id, limit=total)
            older = all_msgs[:-self.history_limit]
            if older:
                summary = await self._compress_history(older)
                result.append({"role": "system", "content": f"[Резюме предыдущей части диалога]: {summary}"})

        for m in recent:
            role = "assistant" if m["role"] == "assistant" else "user"
            result.append({"role": role, "content": m["text"]})

        return result

    # ── Token limits ──────────────────────────────────────────────────────────

    async def _within_token_limits(self, dialog_id: int) -> bool:
        d_tokens = await self.db.get_daily_tokens(dialog_id)
        if d_tokens >= self.max_dialog_tokens:
            logger.warning("dialog %d hit daily token cap (%d)", dialog_id, d_tokens)
            return False
        g_tokens = await self.db.get_daily_tokens()
        if g_tokens >= self.max_global_tokens:
            logger.warning("global daily token cap hit (%d)", g_tokens)
            return False
        return True

    async def _maybe_token_alert(self, transport: Transport, dialog_id: int) -> None:
        g_tokens = await self.db.get_daily_tokens()
        threshold = int(self.max_global_tokens * 0.8)
        if g_tokens >= threshold:
            last = await self.db.get_last_notification(dialog_id, "token_alert")
            if not last:
                await transport.send_owner_notification(
                    f"⚠️ Использовано {g_tokens:,} токенов сегодня (80% дневного лимита).\n"
                    f"Лимит: {self.max_global_tokens:,}"
                )
                await self.db.record_notification(dialog_id, "token_alert",
                                                  {"global_tokens": g_tokens})

    # ── Notifications ─────────────────────────────────────────────────────────

    async def _notify_lead(
        self,
        transport: Transport,
        dialog: dict,
        message_text: str,
        reason: str,
    ) -> None:
        dialog_id = dialog["id"]
        last = await self.db.get_last_notification(dialog_id, "lead")
        if last:
            sent = datetime.fromisoformat(last["sent_at"]).replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - sent < timedelta(minutes=_LEAD_COOLDOWN_MINUTES):
                logger.info("lead notification for dialog %d skipped (cooldown)", dialog_id)
                return

        recent = await self.db.get_messages(dialog_id, limit=6)
        ctx = "\n".join(
            f"{'→' if m['role'] == 'user' else '←'} {m['text']}"
            for m in recent[-4:]
        )

        text = (
            f"🔔 Прогретый лид!\n\n"
            f"Диалог #{dialog_id} | {transport.name}\n"
            f"Клиент: {dialog['external_id']}\n"
            f"Ссылка: {transport.get_dialog_link(dialog['external_id'])}\n\n"
            f"💬 Сообщение: «{message_text[:200]}»\n"
            f"📝 Причина: {reason}\n\n"
            f"Контекст:\n{ctx}"
        )
        await transport.send_owner_notification(text)
        await self.db.record_notification(dialog_id, "lead",
                                          {"reason": reason, "msg": message_text[:200]})
        logger.info("lead notification sent for dialog %d", dialog_id)

    async def _notify_escalation(
        self,
        transport: Transport,
        dialog: dict,
        user_message: str,
        bot_reply: str,
    ) -> None:
        """Called when the bot replies with escalation phrases (торг, unknown product)."""
        dialog_id = dialog["id"]
        last = await self.db.get_last_notification(dialog_id, "escalation")
        if last:
            sent = datetime.fromisoformat(last["sent_at"]).replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - sent < timedelta(minutes=10):
                return

        # Detect if bot is asking to clarify price/availability (not in DB)
        price_unknown = bool(re.search(
            r"уточн[а-я]+\s+наличие|одну\s+минуту|секунду,?\s+уточн[а-я]+",
            bot_reply, re.IGNORECASE
        ))

        context = (
            f"Вопрос клиента: «{user_message[:300]}»\n"
            f"Ответ бота: «{bot_reply[:200]}»"
        )

        if price_unknown:
            header = f"❓ Цена/наличие не найдены — диалог #{dialog_id}\n"
            footer = (
                f"\n\nЦены нет в базе. Ответьте на это сообщение с ценой и наличием — "
                f"бот сразу передаст ваш ответ клиенту в чат Авито."
            )
        else:
            header = f"📌 Эскалация в диалоге #{dialog_id}\n"
            footer = (
                f"\n\nОтветьте на это сообщение — "
                f"бот передаст ваш ответ клиенту в чат Авито."
            )

        text = (
            f"{header}\n"
            f"Клиент: {dialog['external_id']}\n"
            f"Ссылка: {transport.get_dialog_link(dialog['external_id'])}\n\n"
            f"Вопрос клиента: «{user_message[:200]}»\n"
            f"Ответ бота: «{bot_reply[:200]}»"
            f"{footer}"
        )
        tg_msg_id = await transport.send_owner_notification(text)
        await self.db.record_notification(dialog_id, "escalation",
                                          {"user_msg": user_message[:200]})
        if tg_msg_id is not None:
            await self.db.store_escalation_relay(
                tg_msg_id=tg_msg_id,
                dialog_id=dialog_id,
                transport=dialog["transport"],
                external_id=dialog["external_id"],
                context=context,
            )

    # ── Main entry point ──────────────────────────────────────────────────────

    async def process_message(
        self,
        transport: Transport,
        message: IncomingMessage,
    ) -> Optional[str]:
        """
        Process one incoming message. Returns the reply text, or None to stay silent.
        This method is the ONLY place where dialog state is mutated.
        """
        # 1. Resolve / create dialog row
        dialog = await self.db.get_or_create_dialog(transport.name, message.dialog_id)
        dialog_id: int = dialog["id"]

        # 2. Owner message → save to dialog context so LLM sees it, but never respond.
        # If the message contains ✅✅✅ — permanently silence the bot; manager takes over.
        if message.is_owner_message:
            await self.db.add_message(dialog_id, "assistant", message.text)
            # Reset retention timer: owner is actively managing this chat.
            await self.db.record_notification(dialog_id, "retention", {})
            if "✅✅✅" in message.text:
                await self.db.update_dialog_status(dialog_id, "owner_takeover", "checkmark_silence")
                logger.info("dialog %d: ✅✅✅ — manager takes over, bot permanently silenced", dialog_id)
            elif "❌❌❌" in message.text:
                await self.db.update_dialog_status(dialog_id, "bot_active", None)
                logger.info("dialog %d: ❌❌❌ — bot re-activated by manager", dialog_id)
            else:
                logger.info("owner message saved to dialog %d context (no auto-takeover)", dialog_id)
            return None

        # 3. Respect current dialog status
        status = dialog["status"]
        if status == "owner_takeover":
            if dialog.get("takeover_type") == "checkmark_silence":
                # Permanent manager takeover (✅✅✅) — stay silent; only /start <id> can undo.
                logger.info("dialog %d: permanent silence (manager ✅✅✅ takeover)", dialog_id)
                return None
            # Client re-engaged → auto-resume regardless of why takeover was set.
            # Bot only stays permanently silent for toxic content (handled in step 5).
            logger.info("dialog %d: client re-engaged after owner_takeover → auto-resuming", dialog_id)
            await self.db.update_dialog_status(dialog_id, "bot_active", None)
            status = "bot_active"
        elif status not in ("bot_active", "silenced"):
            logger.debug("dialog %d is %s — silent", dialog_id, status)
            return None

        # 4. Persist incoming message
        await self.db.add_message(dialog_id, "user", message.text)

        # 4.5 Reciprocal-closing gate: after bot says goodbye, don't respond to
        # short "спасибо вам тоже" type messages — avoids awkward double-goodbye.
        # Walk backwards skipping deleted/empty messages to find last meaningful bot message.
        if len(message.text) <= 60 and _CLIENT_RECIPROCAL_RE.search(message.text):
            recent_msgs = await self.db.get_messages(dialog_id, limit=6)
            # Find last meaningful assistant message (skip empty/deleted, skip the just-saved user msg)
            last_bot_text: Optional[str] = None
            for m in reversed(recent_msgs):
                if m["role"] == "assistant" and len((m["text"] or "").strip()) >= 3:
                    last_bot_text = m["text"]
                    break
            if last_bot_text and _BOT_CLOSING_RE.search(last_bot_text):
                logger.info("dialog %d: reciprocal closing after goodbye — staying silent", dialog_id)
                return None

        # 5. Toxicity gate
        is_toxic, toxic_reason = await self.toxicity_detector.classify(message.text)
        if is_toxic:
            if status == "bot_active":
                logger.info("toxic message in dialog %d: %s", dialog_id, toxic_reason)
                await self.db.update_dialog_status(dialog_id, "silenced")
                await transport.send_owner_notification(
                    f"⚠️ Токсичное сообщение — бот замолчал\n\n"
                    f"Диалог #{dialog_id} | {dialog['external_id']}\n"
                    f"Ссылка: {transport.get_dialog_link(dialog['external_id'])}\n"
                    f"Причина: {toxic_reason}\n"
                    f"Сообщение: «{message.text[:300]}»\n\n"
                    f"Бот автоматически возобновит ответы, как только клиент напишет адекватно."
                )
            else:
                logger.debug("dialog %d still silenced (another toxic message)", dialog_id)
            return None

        # If silenced but this message is clean → auto-restore
        if status == "silenced":
            logger.info("dialog %d: auto-restored after non-toxic follow-up", dialog_id)
            await self.db.update_dialog_status(dialog_id, "bot_active", None)

        # 6. Token limit gate
        if not await self._within_token_limits(dialog_id):
            return None

        # 7. Build context and call LLM
        system_prompt = await self._build_system_prompt()
        history = await self._build_llm_messages(dialog_id)
        llm_msgs = [{"role": "system", "content": system_prompt}] + history

        response = await self.client.chat.completions.create(
            model=self.llm_model,
            messages=llm_msgs,
            temperature=0.7,
            max_tokens=500,
        )
        reply = response.choices[0].message.content.strip()
        usage = response.usage

        # 8. Track token usage and alert if needed
        await self.db.record_token_usage(dialog_id, usage.prompt_tokens, usage.completion_tokens)
        await self._maybe_token_alert(transport, dialog_id)

        # 9. Persist reply
        await self.db.add_message(dialog_id, "assistant", reply)

        # 10. Lead detection (on user message)
        is_lead, lead_reason = await self.lead_detector.classify(message.text)
        if is_lead:
            await self._notify_lead(transport, dialog, message.text, lead_reason)

        # 11. Escalation detection (on bot reply — торг, unknown product)
        if _ESCALATION_RE.search(reply):
            await self._notify_escalation(transport, dialog, message.text, reply)

        logger.info(
            "dialog=%d tokens=%d reply=%r",
            dialog_id, usage.total_tokens, reply[:80],
        )
        return reply

    async def reformulate_owner_reply(self, owner_text: str, context: str) -> str:
        """Переформулирует сырой ответ владельца в сообщение для покупателя."""
        prompt = (
            "Ты — помощница магазина Gulai Store. "
            "Владелец дал ответ на вопрос покупателя. "
            "Переформулируй его ответ в дружелюбное сообщение покупателю. "
            "Без вводных фраз, без Markdown, 1–3 предложения максимум. "
            "Обращайся на «вы».\n\n"
            f"Контекст:\n{context}\n\n"
            f"Ответ владельца: «{owner_text}»\n\n"
            "Сформулируй ответ покупателю:"
        )
        response = await self.client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.5,
            max_tokens=200,
        )
        return response.choices[0].message.content.strip()

    # ── Admin commands ────────────────────────────────────────────────────────

    async def handle_takeover(
        self, transport_name: str, external_id: str, manual: bool = True
    ) -> bool:
        """Returns True if dialog found and status changed."""
        dialog = await self.db.get_dialog(transport_name, external_id)
        if not dialog:
            return False
        await self.db.update_dialog_status(
            dialog["id"], "owner_takeover", "manual" if manual else "auto"
        )
        return True

    async def handle_resume(self, transport_name: str, external_id: str) -> bool:
        """Resume bot after takeover. Returns True if dialog found."""
        dialog = await self.db.get_dialog(transport_name, external_id)
        if not dialog:
            return False
        await self.db.update_dialog_status(dialog["id"], "bot_active", None)
        return True
