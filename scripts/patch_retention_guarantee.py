"""Patch dialog_engine.py: add retention (thinking/delay) + guarantee rules."""
FILEPATH = "src/core/dialog_engine.py"

with open(FILEPATH, encoding="utf-8") as f:
    src = f.read()

# Insert both new sections before ═══ ЗАПРЕЩЕНО ═══
FORBIDDEN_SECTION = '═══ ЗАПРЕЩЕНО ═══'

NEW_SECTIONS = (
    '═══ КЛИЕНТ УХОДИТ / «НАДО ПОДУМАТЬ» ═══\n'
    'Если покупатель говорит «надо подумать», «напишу позже», «в сентябре», '
    '«пока не готов», «позже», «не сейчас» или любой другой сигнал откладывания:\n'
    '→ «Хорошо, ждём вас! 😊 А пока можете вступить в наш тг-канал Gulai_store — '
    'там следим за ценами и выкладываем новинки.»\n'
    'ВАЖНО: упоминай только название Gulai_store, без ссылок, без @, без t.me — '
    'иначе Авито может заблокировать сообщение.\n\n'
    '═══ ГАРАНТИЯ ═══\n'
    'Если покупатель спрашивает про гарантию, условия, сервис:\n'
    '→ «На все наши товары — гарантия 12 месяцев 😊»\n'
    'Можно добавить, что выдаём кассовый чек.\n\n'
    + FORBIDDEN_SECTION
)

src = src.replace(FORBIDDEN_SECTION, NEW_SECTIONS)

with open(FILEPATH, "w", encoding="utf-8") as f:
    f.write(src)

print("Done. Verifying...")
import re
checks = [
    ("Gulai_store", True),
    ("гарантия 12 месяцев", True),
    ("КЛИЕНТ УХОДИТ", True),
    ("ГАРАНТИЯ", True),
]
for phrase, should_exist in checks:
    found = bool(re.search(re.escape(phrase), src, re.IGNORECASE))
    status = "OK" if found == should_exist else "FAIL"
    print(f"  {status}  {phrase!r}")
