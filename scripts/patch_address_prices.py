"""Patch dialog_engine.py: add address rule + ban 'актуальная цена' phrase."""
FILEPATH = "src/core/dialog_engine.py"

with open(FILEPATH, encoding="utf-8") as f:
    src = f.read()

# 1. Fix "Актуальный остаток" in Case 1 — remove word "Актуальный"
src = src.replace(
    '  → «Да, эта модель у нас есть 😊 Актуальный остаток подтвержу перед вашим приездом»',
    '  → «Да, эта модель у нас есть 😊 Наличие подтвержу перед вашим приездом»',
)

# 2. Fix расхождение цен — remove "актуальная цена"
src = src.replace(
    '→ «Да, в объявлении вышла ошибка с ценой — актуальная цена [X] ₽, уже исправляем. Скажите, устраивает вас эта цена?»',
    '→ «Да, в объявлении вышла ошибка с ценой — цена [X] ₽, уже исправляем. Скажите, устраивает вас эта цена?»',
)

# 3. Add blanket ban on "актуальная/актуальные цены" phrase + address section
#    Insert before ═══ ЗАПРЕЩЕНО ═══
FORBIDDEN_SECTION = '═══ ЗАПРЕЩЕНО ═══'
ADDRESS_AND_PRICE_RULES = (
    '═══ АДРЕС И САМОВЫВОЗ ═══\n'
    'Если покупатель спрашивает адрес / как пройти / где находитесь / самовывоз:\n'
    '→ «Барклая 8, возле БЦ Рубин. Как подойдёте — позвоните по номеру 8 916 202-43-44, '
    'мы встретим или сориентируем 😊»\n\n'
    '═══ КАК НАЗЫВАТЬ ЦЕНУ ═══\n'
    'Называй цену просто числом с ₽. НЕЛЬЗЯ использовать слова «актуальная», «актуальный», '
    '«неактуальная» рядом с ценой — это снижает рейтинг объявления.\n'
    'Правильно: «Цена — 101 000 ₽» или просто «101 000 ₽».\n'
    'Неправильно: «Актуальная цена — 101 000 ₽», «актуальный прайс».\n\n'
    + FORBIDDEN_SECTION
)
src = src.replace(FORBIDDEN_SECTION, ADDRESS_AND_PRICE_RULES)

with open(FILEPATH, "w", encoding="utf-8") as f:
    f.write(src)

print("Done. Verifying...")
checks = [
    ("Барклая 8", True),
    ("КАК НАЗЫВАТЬ ЦЕНУ", True),
    ("актуальная цена", False),
    ("Актуальный остаток", False),
    ("Наличие подтвержу", True),
    ("8 916 202-43-44", True),
]
import re
for phrase, should_exist in checks:
    found = bool(re.search(re.escape(phrase), src, re.IGNORECASE))
    status = "OK" if found == should_exist else "FAIL"
    print(f"  {status}  {'present' if should_exist else 'absent'}: {phrase!r}")
