"""Patch dialog_engine.py: update escalation phrases + add trade-in rule."""
FILEPATH = "src/core/dialog_engine.py"

with open(FILEPATH, encoding="utf-8") as f:
    src = f.read()

# 1. Update _ESCALATION_RE to also catch "Минуту, уточняю / уточню"
src = src.replace(
    'r"(передам\\s+ваш\\s+вопрос|уточн[а-я]+\\s+у\\s+коллег)", re.IGNORECASE',
    'r"(передам\\s+ваш\\s+вопрос|уточн[а-я]+\\s+у\\s+коллег|минуту,?\\s+уточн[а-я]+)", re.IGNORECASE',
)

# 2. Case 2 fallback: "Уточню у коллег и вернусь с ответом" → "Минуту, уточняю"
src = src.replace(
    '  → «Уточню у коллег и вернусь с ответом» — и ничего больше не придумывай.',
    '  → «Минуту, уточняю» — и ничего больше не придумывай.',
)

# 3. Color/config section: "Уточню у коллег точную цену..." → "Минуту, уточняю"
src = src.replace(
    '  либо скажи «Уточню у коллег точную цену для этой конфигурации — вернусь с ответом».',
    '  либо скажи «Минуту, уточняю».',
)

# 4. After "НАЛИЧИЕ ТОВАРА" header block insert Случай 1а (price pending) rule
OLD_CASE1 = (
    'Случай 2 — это ТЕХНИКА APPLE, но её НЕТ в нашем каталоге'
)
NEW_CASE_1A = (
    'Случай 1а — позиция ЕСТЬ в каталоге, но напротив неё «цена уточняется»:\n'
    '  → «Минуту, уточняю» — и ничего больше не придумывай.\n'
    '  НЕ передавай покупателю фразу «цена уточняется».\n\n'
    + OLD_CASE1
)
src = src.replace(OLD_CASE1, NEW_CASE_1A)

# 5. Add trade-in section before "═══ РАСХОЖДЕНИЕ ЦЕН ═══"
TRADE_IN = (
    '═══ ТРЕЙД-ИН ═══\n'
    'Если покупатель спрашивает про трейд-ин / обмен / сдать старое устройство:\n'
    '→ «Да, принимаем в трейд-ин 😊 Оценку делает наш специалист на месте.»\n\n'
)
RASKHOZHDENIE = '═══ РАСХОЖДЕНИЕ ЦЕН ═══'
src = src.replace(RASKHOZHDENIE, TRADE_IN + RASKHOZHDENIE)

with open(FILEPATH, "w", encoding="utf-8") as f:
    f.write(src)

print("Done. Verifying key phrases...")
checks = [
    ("минуту,?\\s+уточн", True),
    ("Уточню у коллег и вернусь с ответом", False),
    ("Случай 1а", True),
    ("ТРЕЙД-ИН", True),
    ("Минуту, уточняю", True),
]
for phrase, should_exist in checks:
    import re
    found = bool(re.search(phrase, src, re.IGNORECASE))
    status = "OK" if found == should_exist else "FAIL"
    print(f"  {status}  {'present' if should_exist else 'absent'}: {phrase!r}")
