import ast
import csv
import io
import operator as _op
import os
import re
import sys
import logging
from functools import wraps

# Гарантируем что папка crm/ в пути — нужно для sber_api.py
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from datetime import date, datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
import sqlite3
import threading
import json as _json_lib

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
app.secret_key = 'sushi-crm-secret-2024-change-in-prod'
app.config['TEMPLATES_AUTO_RELOAD'] = True
# Railway и другие прокси-хосты: корректно определяем HTTPS и хост
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

DATABASE = os.environ.get('DATABASE_PATH', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'crm.db'))


@app.template_filter('datefmt')
def datefmt(val):
    """Конвертирует YYYY-MM-DD → DD-MM-YYYY для отображения."""
    if not val:
        return ''
    try:
        return datetime.strptime(str(val)[:10], '%Y-%m-%d').strftime('%d-%m-%Y')
    except Exception:
        return str(val)


ROLE_LABELS = {
    'admin': 'Администратор',
    'sushi': 'Сушист',
    'packer': 'Упаковщик',
    'courier': 'Курьер',
    'cleaner': 'Уборщица',
    'cook': 'Повар',
}

# Hardcoded defaults — seeded into DB on first run
_DEFAULT_EXPENSE_CATEGORIES = [
    ('repair_plumbing', 'Ремонт сантех.', 1),
    ('repair_grease', 'Чистка жироуловителя', 2),
    ('repair_electric', 'Ремонт электрика', 3),
    ('repair_fridge', 'Ремонт холод.оборуд.', 4),
    ('repair_other', 'Ремонт другой', 5),
    ('shop', 'Магазин / Аптека', 6),
    ('taxi', 'Такси', 7),
    ('cash_plus', 'Плюсы в кассу', 8),
    ('oil', 'За масло отработанное', 9),
    ('fish', 'Рыба (головы, хребты)', 10),
    ('change', 'Размен внёс Алексей', 11),
    ('other', 'Другое', 12),
]

ACTION_LABELS = {
    'revenue_update': ('Выручка',      'info'),
    'expense_add':    ('Расход +',     'success'),
    'expense_update': ('Расход ✎',     'warning'),
    'expense_delete': ('Расход −',     'danger'),
    'staff_add':      ('Сотрудник +',  'success'),
    'staff_update':   ('Сотрудник ✎',  'warning'),
    'staff_delete':   ('Сотрудник −',  'danger'),
    'shift_close':    ('Закрытие',     'secondary'),
    'shift_reopen':   ('Переоткрытие', 'warning'),
    'salary_paid':    ('Выплата ЗП',   'primary'),
}

FORMULA_VARS = {
    'revenue':        'Общая выручка',
    'cash':           'Наличные из выручки',
    'card':           'Безналичные',
    'online':         'Онлайн',
    'delivery':       'Выручка доставки',
    'pickup':         'Выручка самовывоза',
    'orders':         'Кол-во заказов',
    'shifts':         'Кол-во смен',
    'expenses_cash':  'Расходы наличными',
    'expenses_card':  'Расходы картой',
    'expenses_total': 'Расходы всего',
    'pay_admin':      'ФОТ администраторов',
    'pay_cook':       'ФОТ поваров',
    'pay_sushi':      'ФОТ сушистов',
    'pay_courier':    'ФОТ курьеров',
    'pay_cleaner':    'ФОТ уборщиц',
    'pay_packer':     'ФОТ упаковщиков',
    'pay_total':      'Весь ФОТ',
    'profit':         'Прибыль (выручка − расходы − ФОТ)',
}

_SAFE_OPS = {
    ast.Add: _op.add, ast.Sub: _op.sub,
    ast.Mult: _op.mul, ast.Div: _op.truediv,
    ast.Pow: _op.pow, ast.Mod: _op.mod,
    ast.USub: _op.neg, ast.UAdd: lambda x: x,
}


def safe_eval(formula, variables):
    def _eval(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.Name):
            if node.id not in variables:
                raise ValueError(f"Unknown: {node.id}")
            return float(variables[node.id] or 0)
        if isinstance(node, ast.BinOp) and type(node.op) in _SAFE_OPS:
            l, r = _eval(node.left), _eval(node.right)
            if isinstance(node.op, ast.Div) and r == 0:
                return 0.0
            return _SAFE_OPS[type(node.op)](l, r)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _SAFE_OPS:
            return _SAFE_OPS[type(node.op)](_eval(node.operand))
        raise ValueError(f"Unsupported node: {type(node)}")
    try:
        return _eval(ast.parse(formula.strip(), mode='eval').body)
    except Exception:
        return None


def get_db():
    conn = sqlite3.connect(DATABASE, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA busy_timeout = 10000')
    conn.execute('PRAGMA journal_mode = WAL')
    return conn


def get_branch_groups(conn):
    groups = conn.execute(
        'SELECT * FROM branch_groups ORDER BY sort_order, name'
    ).fetchall()
    result = []
    for g in groups:
        members = conn.execute(
            '''SELECT b.id, b.name FROM branch_group_members bgm
               JOIN branches b ON b.id = bgm.branch_id
               WHERE bgm.group_id = ? AND b.is_active = 1
               ORDER BY b.name''',
            (g['id'],)
        ).fetchall()
        result.append({
            'id': g['id'],
            'name': g['name'],
            'abbr': g['abbr'] or '',
            'sort_order': g['sort_order'],
            'branches': [dict(m) for m in members],
            'branch_ids': [m['id'] for m in members],
        })
    return result


def get_expense_categories(conn):
    return conn.execute(
        'SELECT id, code, label, type, parent_id FROM expense_categories WHERE is_active=1 ORDER BY sort_order, label'
    ).fetchall()


# ─── BANK HELPERS ─────────────────────────────────────────────────────────────

def _detect_encoding(raw_bytes):
    for enc in ('utf-8-sig', 'utf-8', 'cp1251'):
        try:
            raw_bytes.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return 'utf-8'


def _parse_date_str(s):
    s = str(s).strip().split(' ')[0].split('T')[0]
    for fmt in ('%d.%m.%Y', '%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%Y.%m.%d'):
        try:
            return datetime.strptime(s, fmt).strftime('%Y-%m-%d')
        except ValueError:
            continue
    return None


def _clean_num(s):
    if not s:
        return None
    s = str(s).strip().replace('\xa0', '').replace(' ', '').replace(' ', '').replace(' ', '').replace(',', '.')
    try:
        return float(s)
    except ValueError:
        return None


def _map_csv_columns(fieldnames):
    cols = {(f or '').lower().strip(): f for f in fieldnames}
    DATE_P  = ['дата операции', 'дата проведения', 'дата платежа', 'дата', 'date_oper', 'o_date', 'date']
    DEBIT_P = ['сумма списания', 'расход', 'дебет', 'сумма дебет', 'списание']
    CRED_P  = ['сумма зачисления', 'приход', 'кредит', 'сумма кредит', 'зачисление']
    AMT_P   = ['сумма операции', 'сумма платежа', 'sum_rur', 'sum_val', 'сумма', 'amount']
    TYPE_P  = ['вид операции', 'приход/расход', 'тип операции', 'д/к', 'd_c', 'dc', 'тип']
    DESC_P  = ['назначение платежа', 'text70', 'назначение', 'описание', 'description', 'наименование']
    CTR_P   = ['контрагент', 'pol_name', 'получатель', 'plat_name', 'плательщик', 'наименование контрагента']
    INN_P   = ['инн контрагента', 'инн плательщика', 'инн получателя', 'инн', 'inn', 'inn_pol', 'inn_plat']

    def find(patterns):
        for p in patterns:
            if p in cols:
                return cols[p]
            for k, v in cols.items():
                if p in k:
                    return v
        return None

    return {
        'date': find(DATE_P), 'debit': find(DEBIT_P), 'credit': find(CRED_P),
        'amount': find(AMT_P), 'type': find(TYPE_P),
        'description': find(DESC_P), 'counterparty': find(CTR_P),
        'inn': find(INN_P),
    }


def _parse_bank_csv(raw_bytes):
    enc = _detect_encoding(raw_bytes)
    text = raw_bytes.decode(enc)
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        raise ValueError('Файл пустой')

    DATE_WORDS = ('дата', 'date')
    AMT_WORDS  = ('сумм', 'amount', 'sum', 'приход', 'расход', 'поступлен', 'списан', 'дебет', 'кредит')

    # Find the header row (first row containing a date keyword + amount keyword and multiple columns)
    header_idx = 0
    for i, line in enumerate(lines[:30]):
        low = line.lower()
        has_date = any(w in low for w in DATE_WORDS)
        has_amt  = any(w in low for w in AMT_WORDS)
        n_cols   = max(line.count(';'), line.count(','), line.count('\t'))
        if has_date and has_amt and n_cols >= 2:
            header_idx = i
            break

    data_text = '\n'.join(lines[header_idx:])

    # Try semicolon, then comma, then tab as delimiter
    rows = []
    reader = None
    for delim in (';', ',', '\t'):
        r = csv.DictReader(io.StringIO(data_text), delimiter=delim)
        rs = list(r)
        fnames = [f for f in (r.fieldnames or []) if f and f.strip()]
        if rs and len(fnames) >= 2:
            rows, reader = rs, r
            break

    if not rows:
        preview = '\n'.join(lines[:5])
        raise ValueError(f'Не удалось распознать формат CSV. Начало файла:\n{preview}')

    fieldnames = reader.fieldnames or []
    col = _map_csv_columns(fieldnames)

    # Auto-detect date column if mapping failed
    if not col['date']:
        for f in fieldnames:
            if not f:
                continue
            vals = [row.get(f, '') for row in rows[:5] if row.get(f)]
            if vals and any(_parse_date_str(v) for v in vals):
                col['date'] = f
                break

    # Auto-detect amount columns if mapping failed
    if not col['amount'] and not col.get('debit') and not col.get('credit'):
        for f in fieldnames:
            if not f or f == col.get('date'):
                continue
            nums = [_clean_num(row.get(f)) for row in rows[:5]]
            if any(v is not None and v != 0 for v in nums):
                col['amount'] = f
                break

    result = []
    for row in rows:
        date_val = _parse_date_str(row.get(col.get('date') or '\x00', '') or '')
        if not date_val:
            continue
        if col.get('debit') or col.get('credit'):
            d = _clean_num(row.get(col.get('debit') or '\x00', '')) or 0
            c = _clean_num(row.get(col.get('credit') or '\x00', '')) or 0
            if c > 0:
                amount = c
            elif d > 0:
                amount = -d
            else:
                continue
        elif col.get('amount'):
            amount = _clean_num(row.get(col['amount'], ''))
            if amount is None:
                continue
            if col.get('type'):
                tv = (row.get(col['type']) or '').strip().lower()
                if tv in ('d', 'д') or any(w in tv for w in ('списан', 'расход', 'дебет', ' д ')):
                    amount = -abs(amount)
                elif tv in ('c', 'к') or any(w in tv for w in ('зачисл', 'приход', 'кредит', ' к ')):
                    amount = abs(amount)
        else:
            continue
        desc = (row.get(col.get('description') or '\x00', '') or '').strip()
        ctr  = (row.get(col.get('counterparty') or '\x00', '') or '').strip()
        inn  = re.sub(r'\D', '', (row.get(col.get('inn') or '\x00', '') or '').strip())
        result.append({'date': date_val, 'amount': amount, 'description': desc, 'counterparty': ctr, 'inn': inn})

    if not result and rows:
        col_info = '; '.join(f'{k}={v}' for k, v in col.items() if v) or 'ни одна не определена'
        first_cols = ', '.join(str(f) for f in fieldnames[:8] if f)
        raise ValueError(
            f'Строки найдены ({len(rows)} шт.), но транзакции не распознаны. '
            f'Колонки файла: {first_cols}. '
            f'Маппинг: {col_info}.'
        )

    return result


def _detect_branch_card(conn, txn):
    """Если транзакция — расход по карте филиала, возвращает (card4, branch_name), иначе None."""
    desc = (txn.get('description') or '') + ' ' + (txn.get('counterparty') or '')
    if 'PURCHASE' not in desc.upper():
        return None
    card_sequences = re.findall(r'[A-Za-z0-9]{10,24}', desc)
    cards = conn.execute(
        'SELECT bc.card_number, b.name as branch_name '
        'FROM branch_cards bc JOIN branches b ON b.id=bc.branch_id WHERE bc.is_active=1'
    ).fetchall()
    for card in cards:
        num = card['card_number'].replace(' ', '')
        if any(seq.endswith(num) or (len(num) >= 8 and num in seq) for seq in card_sequences):
            card4 = num[-4:] if len(num) >= 4 else num
            return (card4, card['branch_name'])
    return None


# Слова которые нельзя использовать как ключевые — слишком общие, дают ложные совпадения
_KW_BLACKLIST = {
    'ооо', 'оао', 'пао', 'зао', 'ао', 'ип', 'ичп', 'гуп', 'муп', 'фгуп', 'фгбу',
    'нко', 'ано', 'нао', 'кфх', 'тсж', 'снт', 'пк', 'пот', 'оп',
    'llc', 'ltd', 'inc', 'corp', 'gmbh', 'ag',
}

def _filter_keywords(kw_list):
    """Убирает из списка ключевых слов общие юридические формы."""
    return [k for k in kw_list if k.lower() not in _KW_BLACKLIST and len(k) > 2]


def _match_contractors(conn, txns):
    contractors = conn.execute('SELECT id, name, category, keywords, inn FROM contractors WHERE is_active=1').fetchall()

    def _refresh():
        nonlocal contractors
        contractors = conn.execute('SELECT id, name, category, keywords, inn FROM contractors WHERE is_active=1').fetchall()

    def _set_inn(cid, inn):
        """Записывает ИНН контрагенту если у него его ещё нет."""
        if inn:
            conn.execute(
                'UPDATE contractors SET inn=? WHERE id=? AND (inn IS NULL OR inn="")',
                (inn, cid)
            )

    for txn in txns:
        inn         = (txn.get('inn') or '').strip()
        # Матчим только по counterparty, description — назначение платежа, не имя контрагента
        cp_text     = (txn.get('counterparty') or '').lower().strip()
        branch_card = txn.pop('_branch_card', None)  # сохраняем и убираем из dict
        is_card     = txn.pop('is_card', False)       # флаг карточной покупки из API

        # 1. Матч по ИНН (наивысший приоритет)
        if inn:
            row = conn.execute(
                'SELECT id, category FROM contractors WHERE inn=? AND is_active=1', (inn,)
            ).fetchone()
            if row:
                txn['contractor_id'] = row['id']
                txn['category'] = txn.get('category') or row['category']
                continue

        # 2. Матч по ключевым словам (только против поля counterparty)
        matched_ctr = None
        for c in contractors:
            raw_kw = (c['keywords'] or '').strip()
            name_lc = (c['name'] or '').lower()
            if raw_kw:
                # Ключевые слова заданы вручную — разбиваем по запятой, фильтруем общие слова
                kws = _filter_keywords([k.strip().lower() for k in raw_kw.split(',') if k.strip()])
            else:
                # Ключевых слов нет — используем имя целиком как одну фразу
                kws = [name_lc] if name_lc else []
            if any(kw and kw in cp_text for kw in kws):
                matched_ctr = c
                break

        if matched_ctr:
            txn['contractor_id'] = matched_ctr['id']
            txn['category'] = txn.get('category') or matched_ctr['category']
            # Если у контрагента нет ИНН — заполняем из выписки
            if inn and not (matched_ctr['inn'] or '').strip():
                _set_inn(matched_ctr['id'], inn)
                _refresh()
            continue

        # 3. Карточная покупка или расход по карте филиала — контрагента не создаём
        if branch_card or is_card:
            continue

        # 4. Новый контрагент: ищем по ИНН или имени, иначе создаём
        cp = (txn.get('counterparty') or '').strip()
        if not cp:
            continue

        existing = None
        if inn:
            existing = conn.execute('SELECT id FROM contractors WHERE inn=?', (inn,)).fetchone()
        if not existing:
            existing = conn.execute('SELECT id FROM contractors WHERE LOWER(name)=LOWER(?)', (cp,)).fetchone()

        if existing:
            _set_inn(existing['id'], inn)
            cid = existing['id']
        else:
            conn.execute(
                'INSERT INTO contractors (name, keywords, inn) VALUES (?,?,?)',
                (cp, cp, inn or None)
            )
            cid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            _refresh()

        txn['contractor_id'] = cid

    return txns


def _match_terminal(conn, txn):
    text = (txn.get('description') or '') + ' ' + (txn.get('counterparty') or '')

    # 1. Совпадение по номеру карты из настроек филиала
    # Извлекаем все длинные буквенно-цифровые последовательности (маскированные номера карт Сбербанка)
    # Пример: "по карте MIR 2202209264BD8602" → '2202209264BD8602'
    card_sequences = re.findall(r'[A-Za-z0-9]{10,24}', text)

    cards = conn.execute(
        'SELECT bc.id as card_id, bc.card_number, bc.card_name, bc.branch_id, b.name as branch_name '
        'FROM branch_cards bc JOIN branches b ON b.id=bc.branch_id WHERE bc.is_active=1'
    ).fetchall()
    for card in cards:
        num = card['card_number'].replace(' ', '')
        # Проверяем все длинные последовательности: номер карты должен быть суффиксом
        matched = any(seq.endswith(num) or (len(num) >= 8 and num in seq)
                      for seq in card_sequences)
        if matched:
            label = f'{card["branch_name"]} – {card["card_name"] or card["card_number"]}'
            t = conn.execute(
                'SELECT id FROM bank_terminals WHERE terminal_number=? AND branch_id=?',
                (num, card['branch_id'])
            ).fetchone()
            if t:
                return t['id']
            conn.execute(
                'INSERT INTO bank_terminals (terminal_number, name, branch_id) VALUES (?,?,?)',
                (num, label, card['branch_id'])
            )
            return conn.execute('SELECT last_insert_rowid()').fetchone()[0]

    # 2. Совпадение по номерам мерчанта/терминала из настроек филиала
    branches = conn.execute(
        'SELECT id, name, merchant_numbers FROM branches '
        'WHERE merchant_numbers IS NOT NULL AND merchant_numbers != ""'
    ).fetchall()
    for branch in branches:
        numbers = [n.strip() for n in (branch['merchant_numbers'] or '').split(',') if n.strip()]
        for num in numbers:
            if num and num in text:
                t = conn.execute(
                    'SELECT id FROM bank_terminals WHERE terminal_number=? AND branch_id=?',
                    (num, branch['id'])
                ).fetchone()
                if t:
                    return t['id']
                conn.execute(
                    'INSERT INTO bank_terminals (terminal_number, name, branch_id) VALUES (?,?,?)',
                    (num, f'{branch["name"]} – {num}', branch['id'])
                )
                return conn.execute('SELECT last_insert_rowid()').fetchone()[0]

    # 3. Фолбэк: стандартный TID паттерн
    m = re.search(r'TID\s*[:\-]?\s*(\d{6,12})', text, re.IGNORECASE)
    if m:
        tid = m.group(1)
        t = conn.execute('SELECT id FROM bank_terminals WHERE terminal_number=?', (tid,)).fetchone()
        if t:
            return t['id']
    return None


# ──────────────────────────────────────────────────────────────────────────────

def build_cats_groups(cats):
    """Build grouped structure for dropdown: [(parent_dict, [child_dict, ...]), ...]."""
    children_map = {}
    for c in cats:
        if c['parent_id']:
            children_map.setdefault(c['parent_id'], []).append(dict(c))
    groups = []
    for c in cats:
        if not c['parent_id']:
            groups.append((dict(c), children_map.get(c['id'], [])))
    return groups


def get_kpi_values(conn, branch_id, date_from, date_to):
    bf = f"AND s.branch_id={int(branch_id)}" if branch_id else ""
    rev = conn.execute(f'''
        SELECT COALESCE(SUM(r.total_revenue),0) as revenue,
               COALESCE(SUM(r.cash_amount),0) as cash,
               COALESCE(SUM(r.card_amount),0) as card,
               COALESCE(SUM(r.online_amount),0) as online,
               COALESCE(SUM(r.delivery_revenue),0) as delivery,
               COALESCE(SUM(r.pickup_revenue),0) as pickup,
               COALESCE(SUM(r.delivery_orders+r.pickup_orders),0) as orders,
               COUNT(DISTINCT s.id) as shifts
        FROM shifts s LEFT JOIN shift_revenue r ON r.shift_id=s.id
        WHERE s.date BETWEEN ? AND ? {bf}
    ''', (date_from, date_to)).fetchone()

    exp = conn.execute(f'''
        SELECT COALESCE(SUM(e.amount_cash),0) as expenses_cash,
               COALESCE(SUM(e.amount_card),0) as expenses_card
        FROM expenses e JOIN shifts s ON s.id=e.shift_id
        WHERE s.date BETWEEN ? AND ? {bf}
    ''', (date_from, date_to)).fetchone()

    pay_rows = conn.execute(f'''
        SELECT es.role_snapshot, COALESCE(SUM(es.total_amount),0) as total
        FROM employee_shifts es JOIN shifts s ON s.id=es.shift_id
        WHERE s.date BETWEEN ? AND ? {bf}
        GROUP BY es.role_snapshot
    ''', (date_from, date_to)).fetchall()

    pay_by_role = {r['role_snapshot']: r['total'] for r in pay_rows}
    pay_total = sum(pay_by_role.values())

    vals = dict(rev)
    vals.update({
        'expenses_cash':  exp['expenses_cash'],
        'expenses_card':  exp['expenses_card'],
        'expenses_total': exp['expenses_cash'] + exp['expenses_card'],
        'pay_admin':      pay_by_role.get('admin', 0),
        'pay_cook':       pay_by_role.get('cook', 0),
        'pay_sushi':      pay_by_role.get('sushi', 0),
        'pay_courier':    pay_by_role.get('courier', 0),
        'pay_cleaner':    pay_by_role.get('cleaner', 0),
        'pay_packer':     pay_by_role.get('packer', 0),
        'pay_total':      pay_total,
        'profit':         vals['revenue'] - exp['expenses_cash'] - exp['expenses_card'] - pay_total,
    })
    return vals


def init_db():
    with open(os.path.join(os.path.dirname(__file__), 'schema.sql')) as f:
        with get_db() as conn:
            conn.executescript(f.read())
    with get_db() as conn:
        # Seed owner user
        owner = conn.execute("SELECT id FROM users WHERE role='owner'").fetchone()
        if not owner:
            conn.execute(
                "INSERT INTO users (username, password_hash, role, full_name) VALUES (?,?,?,?)",
                ('owner', generate_password_hash('admin123', method='pbkdf2:sha256'), 'owner', 'Владелец')
            )
            conn.execute("INSERT INTO branches (name) VALUES (?)", ('КВАДРАТ',))

        # Add auto_bonus column if missing (migration for existing DBs)
        existing_cols = [r[1] for r in conn.execute("PRAGMA table_info(employee_shifts)").fetchall()]
        if 'auto_bonus' not in existing_cols:
            conn.execute("ALTER TABLE employee_shifts ADD COLUMN auto_bonus REAL DEFAULT 0")

        # Add branch_id to bonus_rules if missing
        br_cols = [r[1] for r in conn.execute("PRAGMA table_info(bonus_rules)").fetchall()]
        if 'branch_id' not in br_cols:
            conn.execute("ALTER TABLE bonus_rules ADD COLUMN branch_id INTEGER REFERENCES branches(id)")

        # Add first_name / last_name to employees if missing
        emp_cols = [r[1] for r in conn.execute("PRAGMA table_info(employees)").fetchall()]
        if 'last_name' not in emp_cols:
            conn.execute("ALTER TABLE employees ADD COLUMN last_name TEXT DEFAULT ''")
        if 'first_name' not in emp_cols:
            conn.execute("ALTER TABLE employees ADD COLUMN first_name TEXT DEFAULT ''")
        # Migrate existing full_name → last_name + first_name
        conn.execute("""
            UPDATE employees SET
                last_name = CASE WHEN INSTR(full_name,' ')>0
                    THEN TRIM(SUBSTR(full_name,1,INSTR(full_name,' ')-1))
                    ELSE full_name END,
                first_name = CASE WHEN INSTR(full_name,' ')>0
                    THEN TRIM(SUBSTR(full_name,INSTR(full_name,' ')+1))
                    ELSE '' END
            WHERE last_name = '' OR last_name IS NULL
        """)

        # Create rate_templates and related tables if missing
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS rate_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,
                name TEXT NOT NULL,
                rate REAL DEFAULT 0,
                rate_per_km REAL DEFAULT 10,
                rate_per_order REAL DEFAULT 100,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS rate_template_branches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                template_id INTEGER NOT NULL REFERENCES rate_templates(id) ON DELETE CASCADE,
                branch_id INTEGER NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
                UNIQUE(template_id, branch_id)
            );
            CREATE TABLE IF NOT EXISTS rate_template_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                template_id INTEGER NOT NULL REFERENCES rate_templates(id) ON DELETE CASCADE,
                rate REAL DEFAULT 0,
                rate_per_km REAL DEFAULT 10,
                rate_per_order REAL DEFAULT 100,
                valid_from DATE NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')

        # Create taxi and address tables (safe no-ops if already exist — handled by schema IF NOT EXISTS)
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS employee_address_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id INTEGER NOT NULL REFERENCES employees(id),
                address TEXT NOT NULL,
                valid_from DATE NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS taxi_trips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shift_id INTEGER NOT NULL REFERENCES shifts(id),
                amount REAL DEFAULT 0,
                payment_type TEXT DEFAULT 'cash',
                in_gulyash INTEGER DEFAULT 0,
                note TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS taxi_trip_employees (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trip_id INTEGER NOT NULL REFERENCES taxi_trips(id) ON DELETE CASCADE,
                employee_id INTEGER REFERENCES employees(id),
                name_snapshot TEXT NOT NULL,
                address_snapshot TEXT
            );
        ''')

        # Seed default bonus rules for cooks
        if conn.execute("SELECT COUNT(*) FROM bonus_rules").fetchone()[0] == 0:
            conn.execute("INSERT INTO bonus_rules (role,threshold_pct,bonus_pct) VALUES ('cook',8.0,1.5)")
            conn.execute("INSERT INTO bonus_rules (role,threshold_pct,bonus_pct) VALUES ('cook',10.0,1.0)")

        # Seed expense categories
        existing = conn.execute("SELECT COUNT(*) FROM expense_categories").fetchone()[0]
        if existing == 0:
            for code, label, sort in _DEFAULT_EXPENSE_CATEGORIES:
                conn.execute(
                    "INSERT OR IGNORE INTO expense_categories (code, label, sort_order) VALUES (?,?,?)",
                    (code, label, sort)
                )

            conn.execute('''CREATE TABLE IF NOT EXISTS contractor_categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                sort_order INTEGER DEFAULT 0
            )''')
            _DEFAULT_CTR_CATS = [
                ('Продукты', 1), ('Упаковка', 2), ('Налоги', 3),
                ('Зарплата', 4), ('Аренда', 5), ('Коммунальные', 6),
                ('Транспорт', 7), ('Реклама', 8), ('Оборудование', 9), ('Прочее', 10),
            ]
            for name, sort in _DEFAULT_CTR_CATS:
                conn.execute(
                    "INSERT OR IGNORE INTO contractor_categories (name, sort_order) VALUES (?,?)",
                    (name, sort)
                )

        # Create user_branches table (many-to-many users ↔ branches)
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS user_branches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                branch_id INTEGER NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
                UNIQUE(user_id, branch_id)
            );
            CREATE TABLE IF NOT EXISTS employee_branches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
                branch_id INTEGER NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
                UNIQUE(employee_id, branch_id)
            );
        ''')
        conn.execute('''
            INSERT OR IGNORE INTO user_branches (user_id, branch_id)
            SELECT id, branch_id FROM users WHERE branch_id IS NOT NULL
        ''')
        conn.execute('''
            INSERT OR IGNORE INTO employee_branches (employee_id, branch_id)
            SELECT id, branch_id FROM employees WHERE branch_id IS NOT NULL
        ''')

        # Add allowed_ip to branches if missing
        branch_cols = [r[1] for r in conn.execute("PRAGMA table_info(branches)").fetchall()]
        if 'allowed_ip' not in branch_cols:
            conn.execute("ALTER TABLE branches ADD COLUMN allowed_ip TEXT DEFAULT NULL")
        if 'merchant_numbers' not in branch_cols:
            conn.execute("ALTER TABLE branches ADD COLUMN merchant_numbers TEXT DEFAULT ''")

        # Add type and parent_id to expense_categories if missing
        ec_cols = [r[1] for r in conn.execute("PRAGMA table_info(expense_categories)").fetchall()]
        if 'type' not in ec_cols:
            conn.execute("ALTER TABLE expense_categories ADD COLUMN type TEXT DEFAULT 'expense'")
        if 'parent_id' not in ec_cols:
            conn.execute("ALTER TABLE expense_categories ADD COLUMN parent_id INTEGER REFERENCES expense_categories(id)")

        # Create branch groups tables
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS branch_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                sort_order INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS branch_group_members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL REFERENCES branch_groups(id) ON DELETE CASCADE,
                branch_id INTEGER NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
                UNIQUE(group_id, branch_id)
            );
            CREATE TABLE IF NOT EXISTS branch_cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                branch_id INTEGER NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
                card_number TEXT NOT NULL,
                card_name TEXT DEFAULT '',
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')

        try:
            conn.execute("ALTER TABLE shift_revenue ADD COLUMN actual_cash_comment TEXT DEFAULT ''")
        except Exception:
            pass

        try:
            conn.execute("ALTER TABLE shift_revenue ADD COLUMN plus_amount REAL DEFAULT 0")
        except Exception:
            pass

        conn.executescript('''
            CREATE TABLE IF NOT EXISTS cash_plus_entries (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                shift_id    INTEGER NOT NULL REFERENCES shifts(id) ON DELETE CASCADE,
                amount      REAL    NOT NULL DEFAULT 0,
                description TEXT    DEFAULT '',
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')

        try:
            conn.execute("ALTER TABLE shift_revenue ADD COLUMN morning_cash REAL DEFAULT 0")
        except Exception:
            pass

        # Add category and cash/card split to cash_plus_entries
        plus_cols = [r[1] for r in conn.execute("PRAGMA table_info(cash_plus_entries)").fetchall()]
        if 'category' not in plus_cols:
            conn.execute("ALTER TABLE cash_plus_entries ADD COLUMN category TEXT DEFAULT ''")
        if 'amount_cash' not in plus_cols:
            conn.execute("ALTER TABLE cash_plus_entries ADD COLUMN amount_cash REAL DEFAULT 0")
            conn.execute("ALTER TABLE cash_plus_entries ADD COLUMN amount_card REAL DEFAULT 0")
            conn.execute("UPDATE cash_plus_entries SET amount_cash = amount WHERE amount > 0")

        try:
            conn.execute("ALTER TABLE purchases ADD COLUMN payer TEXT DEFAULT ''")
        except Exception:
            pass

        try:
            conn.execute("ALTER TABLE purchases ADD COLUMN import_hash TEXT")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_purchases_import_hash ON purchases(import_hash) WHERE import_hash IS NOT NULL")
        except Exception:
            pass

        try:
            conn.execute("ALTER TABLE contractors ADD COLUMN is_card_merchant INTEGER DEFAULT 0")
        except Exception:
            pass

        try:
            conn.execute("ALTER TABLE contractors ADD COLUMN inn TEXT DEFAULT ''")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_contractors_inn ON contractors(inn) WHERE inn IS NOT NULL AND inn != ''")
        except Exception:
            pass

        try:
            conn.execute("ALTER TABLE branches ADD COLUMN abbr TEXT DEFAULT ''")
        except Exception:
            pass

        try:
            conn.execute("ALTER TABLE branch_groups ADD COLUMN abbr TEXT DEFAULT ''")
        except Exception:
            pass

        # Create bank module tables
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS contractors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                category TEXT DEFAULT '',
                keywords TEXT DEFAULT '',
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS contractor_categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                sort_order INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS bank_terminals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                terminal_number TEXT NOT NULL UNIQUE,
                name TEXT DEFAULT '',
                branch_id INTEGER REFERENCES branches(id),
                is_active INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS bank_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                bank_name TEXT DEFAULT '',
                account_number TEXT DEFAULT '',
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS bank_account_branches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bank_account_id INTEGER NOT NULL REFERENCES bank_accounts(id) ON DELETE CASCADE,
                branch_id INTEGER NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
                UNIQUE(bank_account_id, branch_id)
            );
            CREATE TABLE IF NOT EXISTS bank_statements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bank_account_id INTEGER NOT NULL REFERENCES bank_accounts(id) ON DELETE CASCADE,
                filename TEXT NOT NULL,
                uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                uploaded_by INTEGER REFERENCES users(id),
                date_from DATE,
                date_to DATE,
                row_count INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS bank_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                statement_id INTEGER NOT NULL REFERENCES bank_statements(id) ON DELETE CASCADE,
                bank_account_id INTEGER NOT NULL REFERENCES bank_accounts(id),
                txn_date DATE NOT NULL,
                amount REAL NOT NULL,
                description TEXT DEFAULT '',
                counterparty TEXT DEFAULT '',
                contractor_id INTEGER REFERENCES contractors(id),
                category TEXT DEFAULT '',
                terminal_id INTEGER REFERENCES bank_terminals(id),
                is_ignored INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS api_settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS api_1c_tokens (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                branch_id   INTEGER NOT NULL REFERENCES branches(id),
                token       TEXT    NOT NULL UNIQUE,
                description TEXT,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS api_1c_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                token       TEXT,
                branch_id   INTEGER,
                method      TEXT,
                path        TEXT,
                body        TEXT,
                status      TEXT DEFAULT 'received',
                parsed_ok   INTEGER DEFAULT 0,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS purchase_suppliers (
                id   INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS purchases (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                branch_id      INTEGER NOT NULL REFERENCES branches(id),
                supplier       TEXT    NOT NULL,
                amount         REAL    NOT NULL DEFAULT 0,
                date           DATE    NOT NULL,
                invoice_number TEXT,
                note           TEXT,
                created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_by     INTEGER REFERENCES users(id)
            );
        ''')

        # Feature: multiple roles per employee
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS employee_roles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                rate REAL DEFAULT 0,
                rate_per_km REAL DEFAULT 10,
                rate_per_order REAL DEFAULT 100,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(employee_id, role)
            );
        ''')

        # Feature: import batches for Excel imports
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS import_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                branch_id INTEGER NOT NULL REFERENCES branches(id),
                filename TEXT NOT NULL,
                imported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                imported_by INTEGER REFERENCES users(id),
                shifts_created INTEGER DEFAULT 0,
                expenses_created INTEGER DEFAULT 0,
                employees_created INTEGER DEFAULT 0,
                employee_shifts_created INTEGER DEFAULT 0
            );
        ''')
        _shifts_cols = [r[1] for r in conn.execute("PRAGMA table_info(shifts)").fetchall()]
        if 'import_batch_id' not in _shifts_cols:
            conn.execute("ALTER TABLE shifts ADD COLUMN import_batch_id INTEGER REFERENCES import_batches(id)")

        # Feature: fire/restore employees
        _emp_cols2 = [r[1] for r in conn.execute("PRAGMA table_info(employees)").fetchall()]
        if 'is_fired' not in _emp_cols2:
            conn.execute("ALTER TABLE employees ADD COLUMN is_fired INTEGER DEFAULT 0")
        if 'fired_at' not in _emp_cols2:
            conn.execute("ALTER TABLE employees ADD COLUMN fired_at TIMESTAMP")
        if 'fired_comment' not in _emp_cols2:
            conn.execute("ALTER TABLE employees ADD COLUMN fired_comment TEXT DEFAULT ''")

        # Feature: monthly salary flag
        if 'pay_monthly' not in _emp_cols2:
            conn.execute("ALTER TABLE employees ADD COLUMN pay_monthly INTEGER DEFAULT 0")

        # One shift per branch per day: unique index
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_shifts_branch_date ON shifts(branch_id, date)"
        )

        # Per-account Sber auto-sync columns
        _ba_cols = [r[1] for r in conn.execute("PRAGMA table_info(bank_accounts)").fetchall()]
        if 'sber_auto_sync' not in _ba_cols:
            conn.execute("ALTER TABLE bank_accounts ADD COLUMN sber_auto_sync INTEGER DEFAULT 0")
        if 'sber_last_sync' not in _ba_cols:
            conn.execute("ALTER TABLE bank_accounts ADD COLUMN sber_last_sync TEXT DEFAULT ''")
        if 'sber_last_result' not in _ba_cols:
            conn.execute("ALTER TABLE bank_accounts ADD COLUMN sber_last_result TEXT DEFAULT ''")

        # Per-shift terminal breakdown for beznal
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS shift_terminals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shift_id INTEGER NOT NULL REFERENCES shifts(id) ON DELETE CASCADE,
                terminal_number TEXT DEFAULT '',
                amount REAL DEFAULT 0,
                sort_order INTEGER DEFAULT 0
            );
        ''')

        # Feature: change schedules
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS change_schedule (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                branch_id INTEGER REFERENCES branches(id),
                weekday INTEGER,
                amount REAL NOT NULL DEFAULT 0,
                valid_from TEXT NOT NULL,
                valid_to TEXT,
                label TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS change_date_overrides (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                branch_id INTEGER NOT NULL REFERENCES branches(id),
                date TEXT NOT NULL,
                amount REAL NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(branch_id, date)
            );
        ''')

        # Правила разбора банковских операций
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS bank_parse_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bank_account_id INTEGER REFERENCES bank_accounts(id),
                name TEXT NOT NULL,
                direction TEXT NOT NULL DEFAULT 'any',
                keyword TEXT NOT NULL,
                commission_included INTEGER DEFAULT 1,
                commission_pattern TEXT DEFAULT '',
                category TEXT DEFAULT '',
                sort_order INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1
            );
        ''')
        try:
            conn.execute("ALTER TABLE bank_parse_rules ADD COLUMN category TEXT DEFAULT ''")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE contractor_categories ADD COLUMN direction TEXT DEFAULT 'any'")
        except Exception:
            pass

        # PnL report settings storage
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS pnl_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        ''')

        # Google Sheets export settings
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS gsheet_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        ''')

        conn.commit()


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def owner_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get('role') != 'owner':
            flash('Доступ только для владельца', 'danger')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated


def get_current_user():
    if 'user_id' not in session:
        return None
    with get_db() as conn:
        return conn.execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()


def get_client_ip():
    xff = request.headers.get('X-Forwarded-For', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.remote_addr


def _apply_change_amount_to_shift(conn, shift_id, branch_id, shift_date):
    """Apply change_amount to shift: override table first, then schedule rules."""
    try:
        d = date.fromisoformat(shift_date) if isinstance(shift_date, str) else shift_date
        weekday = d.weekday()
        shift_date = d.isoformat()
    except Exception:
        return
    override = conn.execute(
        'SELECT amount FROM change_date_overrides WHERE branch_id=? AND date=?',
        (branch_id, shift_date)
    ).fetchone()
    if override:
        conn.execute('UPDATE shift_revenue SET change_amount=? WHERE shift_id=?', (override['amount'], shift_id))
        return
    row = conn.execute(
        '''SELECT amount FROM change_schedule
           WHERE (branch_id IS NULL OR branch_id=?)
             AND (weekday IS NULL OR weekday=?)
             AND valid_from <= ?
             AND (valid_to IS NULL OR valid_to >= ?)
           ORDER BY branch_id DESC NULLS LAST, weekday DESC NULLS LAST, id DESC
           LIMIT 1''',
        (branch_id, weekday, shift_date, shift_date)
    ).fetchone()
    if row:
        conn.execute('UPDATE shift_revenue SET change_amount=? WHERE shift_id=?', (row['amount'], shift_id))


def _apply_all_change_schedules(conn):
    """Apply all change schedules to matching existing OPEN shifts only."""
    schedules = conn.execute(
        'SELECT * FROM change_schedule ORDER BY branch_id NULLS FIRST, weekday NULLS FIRST, id'
    ).fetchall()
    for sched in schedules:
        params = [sched['amount'], sched['valid_from']]
        clauses = ["s.date >= ?", "s.status != 'closed'"]
        if sched['valid_to']:
            clauses.append('s.date <= ?')
            params.append(sched['valid_to'])
        if sched['branch_id']:
            clauses.append('s.branch_id = ?')
            params.append(sched['branch_id'])
        if sched['weekday'] is not None:
            clauses.append("CAST(strftime('%u', s.date) AS INTEGER) - 1 = ?")
            params.append(sched['weekday'])
        where = ' AND '.join(clauses)
        conn.execute(f'''
            UPDATE shift_revenue SET change_amount = ?
            WHERE shift_id IN (SELECT s.id FROM shifts s WHERE {where})
        ''', params)


# ─── AUTH ─────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']
        with get_db() as conn:
            user = conn.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
        if user and check_password_hash(user['password_hash'], password):
            rows = conn.execute(
                'SELECT branch_id FROM user_branches WHERE user_id=? ORDER BY branch_id',
                (user['id'],)
            ).fetchall()
            branch_ids = [r['branch_id'] for r in rows]
            if not branch_ids and user['branch_id']:
                branch_ids = [user['branch_id']]
            # IP restriction: non-owners are limited to the branch matching their IP
            if user['role'] != 'owner':
                client_ip = get_client_ip()
                ip_branch = conn.execute(
                    "SELECT id FROM branches WHERE allowed_ip=? AND is_active=1",
                    (client_ip,)
                ).fetchone()
                if ip_branch:
                    branch_ids = [ip_branch['id']]
            session['user_id'] = user['id']
            session['role'] = user['role']
            session['full_name'] = user['full_name']
            session['branch_id'] = branch_ids[0] if branch_ids else None
            session['branch_ids'] = branch_ids
            return redirect(url_for('dashboard'))
        flash('Неверный логин или пароль', 'danger')
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# ─── DASHBOARD ────────────────────────────────────────────────────────────────

@app.route('/dashboard')
@login_required
def dashboard():
    role = session.get('role')
    with get_db() as conn:
        if role == 'owner':
            branches = conn.execute('SELECT * FROM branches WHERE is_active=1 ORDER BY name').fetchall()
            stats = conn.execute('''
                SELECT b.name, s.date, COALESCE(r.total_revenue,0) as revenue,
                       COALESCE(r.delivery_orders,0)+COALESCE(r.pickup_orders,0) as orders,
                       s.status, s.id as shift_id
                FROM branches b
                LEFT JOIN shifts s ON s.branch_id=b.id AND s.date >= date('now','-7 days')
                LEFT JOIN shift_revenue r ON r.shift_id=s.id
                WHERE b.is_active=1
                ORDER BY s.date DESC, b.name
            ''').fetchall()
            weekly = conn.execute('''
                SELECT COALESCE(SUM(r.total_revenue),0) as total,
                       COALESCE(SUM(r.delivery_revenue),0) as delivery,
                       COALESCE(SUM(r.pickup_revenue),0) as pickup,
                       COALESCE(SUM(r.delivery_orders+r.pickup_orders),0) as orders
                FROM shifts s JOIN shift_revenue r ON r.shift_id=s.id
                WHERE s.date >= date('now','weekday 0','-7 days')
            ''').fetchone()
            open_shifts = conn.execute('''
                SELECT s.*, b.name as branch_name
                FROM shifts s JOIN branches b ON b.id=s.branch_id
                WHERE s.date=date('now') AND s.status='open'
            ''').fetchall()
            # KPI blocks
            kpi_blocks = conn.execute(
                'SELECT * FROM kpi_blocks WHERE is_active=1 ORDER BY sort_order, id'
            ).fetchall()
            # Current month stats
            month_rev = conn.execute('''
                SELECT
                    COALESCE(SUM(r.cash_amount), 0)   AS cash_amount,
                    COALESCE(SUM(r.card_amount), 0)   AS card_amount,
                    COALESCE(SUM(r.online_amount), 0) AS online_amount,
                    COALESCE(SUM(r.total_revenue), 0) AS total_revenue
                FROM shifts s
                JOIN shift_revenue r ON r.shift_id = s.id
                WHERE s.date >= date('now', 'start of month')
            ''').fetchone()
            month_fot = conn.execute('''
                SELECT COALESCE(SUM(es.total_amount), 0) AS fot
                FROM employee_shifts es
                JOIN shifts s ON s.id = es.shift_id
                WHERE s.date >= date('now', 'start of month')
            ''').fetchone()
            branch_groups = get_branch_groups(conn)
            return render_template('dashboard_owner.html',
                branches=branches, stats=stats, weekly=weekly,
                open_shifts=open_shifts, kpi_blocks=kpi_blocks,
                month_rev=month_rev, month_fot=month_fot['fot'] or 0,
                branch_groups=branch_groups)
        else:
            today = date.today().isoformat()
            bids = _session_branch_ids()
            if not bids:
                flash('У вас не назначен филиал', 'warning')
                return render_template('dashboard_admin.html', branches_shifts=[], today=today, recent=[])
            branches_shifts = []
            for bid in bids:
                branch = conn.execute('SELECT * FROM branches WHERE id=?', (bid,)).fetchone()
                if not branch:
                    continue
                shift = conn.execute(
                    'SELECT s.*, u.full_name as closed_by_name FROM shifts s '
                    'LEFT JOIN users u ON u.id=s.closed_by '
                    'WHERE s.branch_id=? AND s.date=?',
                    (bid, today)
                ).fetchone()
                branches_shifts.append({'branch': branch, 'shift': shift})
            if bids:
                ids_str = ','.join(str(int(b)) for b in bids)
                recent = conn.execute(f'''
                    SELECT s.*, b.name as branch_name, COALESCE(r.total_revenue,0) as revenue
                    FROM shifts s JOIN branches b ON b.id=s.branch_id
                    LEFT JOIN shift_revenue r ON r.shift_id=s.id
                    WHERE s.branch_id IN ({ids_str}) ORDER BY s.date DESC LIMIT 10
                ''').fetchall()
            else:
                recent = []
            return render_template('dashboard_admin.html',
                branches_shifts=branches_shifts, today=today, recent=recent)


# ─── KPI API ──────────────────────────────────────────────────────────────────

@app.route('/api/kpi-values')
@login_required
@owner_required
def api_kpi_values():
    branch_id = request.args.get('branch_id', '')
    date_from = request.args.get('date_from', date.today().isoformat())
    date_to = request.args.get('date_to', date.today().isoformat())
    with get_db() as conn:
        vals = get_kpi_values(conn, branch_id, date_from, date_to)
        blocks = conn.execute(
            'SELECT * FROM kpi_blocks WHERE is_active=1 ORDER BY sort_order, id'
        ).fetchall()
        results = []
        for b in blocks:
            value = safe_eval(b['formula'], vals)
            results.append({
                'id': b['id'],
                'title': b['title'],
                'value': round(value, 2) if value is not None else None,
                'color': b['color'],
                'unit': b['unit'],
            })
    return jsonify({'ok': True, 'blocks': results, 'vars': {k: round(v, 2) for k, v in vals.items()}})


@app.route('/api/revenue-year')
@login_required
@owner_required
def api_revenue_year():
    today = date.today()
    # скользящие 12 месяцев, заканчивающихся текущим
    start_m = today.month - 11
    start_y = today.year
    if start_m <= 0:
        start_m += 12
        start_y -= 1
    date_from = date(start_y, start_m, 1).isoformat()
    date_to   = today.isoformat()
    with get_db() as conn:
        rows = conn.execute('''
            SELECT CAST(strftime('%Y', s.date) AS INTEGER) AS year,
                   CAST(strftime('%m', s.date) AS INTEGER) AS month,
                   COALESCE(SUM(r.total_revenue), 0) AS revenue
            FROM shifts s JOIN shift_revenue r ON r.shift_id = s.id
            WHERE s.date BETWEEN ? AND ?
            GROUP BY year, month ORDER BY year, month
        ''', (date_from, date_to)).fetchall()
    rev = {(r['year'], r['month']): int(r['revenue']) for r in rows}
    labels = ['', 'Янв', 'Фев', 'Мар', 'Апр', 'Май', 'Июн',
              'Июл', 'Авг', 'Сен', 'Окт', 'Ноя', 'Дек']
    months_list = []
    y, m = start_y, start_m
    for _ in range(12):
        months_list.append({'year': y, 'month': m, 'label': labels[m], 'revenue': rev.get((y, m), 0)})
        m += 1
        if m > 12:
            m = 1
            y += 1
    return jsonify({'ok': True, 'total': sum(x['revenue'] for x in months_list), 'months': months_list})


@app.route('/api/lfl')
@login_required
@owner_required
def api_lfl():
    from calendar import monthrange
    today  = date.today()
    metric = request.args.get('metric', 'revenue')  # 'revenue' or 'orders'
    raw_bids = request.args.get('branch_ids', '')
    bids = [int(x) for x in raw_bids.split(',') if x.strip().isdigit()]
    bf   = f"AND s.branch_id IN ({','.join('?'*len(bids))})" if bids else ''
    if metric == 'orders':
        agg = 'COALESCE(SUM(r.delivery_orders),0) + COALESCE(SUM(r.pickup_orders),0)'
    else:
        agg = 'COALESCE(SUM(r.total_revenue),0)'
    month_labels = ['', 'Янв', 'Фев', 'Мар', 'Апр', 'Май', 'Июн',
                    'Июл', 'Авг', 'Сен', 'Окт', 'Ноя', 'Дек']
    with get_db() as conn:
        months_seq = []
        y, m = today.year, today.month
        for _ in range(12):
            months_seq.insert(0, (y, m))
            m -= 1
            if m == 0:
                m = 12
                y -= 1
        result = []
        for (yr, mo) in months_seq:
            is_current = (yr == today.year and mo == today.month)
            days_in_this = monthrange(yr, mo)[1]
            days_in_last = monthrange(yr - 1, mo)[1]
            if is_current:
                d_from_this = date(yr, mo, 1).isoformat()
                d_to_this   = today.isoformat()
                d_from_last = date(yr - 1, mo, 1).isoformat()
                d_to_last   = date(yr - 1, mo, min(today.day, days_in_last)).isoformat()
            else:
                d_from_this = date(yr, mo, 1).isoformat()
                d_to_this   = date(yr, mo, days_in_this).isoformat()
                d_from_last = date(yr - 1, mo, 1).isoformat()
                d_to_last   = date(yr - 1, mo, days_in_last).isoformat()
            val_this = conn.execute(
                f'SELECT {agg} FROM shifts s JOIN shift_revenue r ON r.shift_id=s.id WHERE s.date BETWEEN ? AND ? {bf}',
                [d_from_this, d_to_this] + bids
            ).fetchone()[0] or 0
            val_last = conn.execute(
                f'SELECT {agg} FROM shifts s JOIN shift_revenue r ON r.shift_id=s.id WHERE s.date BETWEEN ? AND ? {bf}',
                [d_from_last, d_to_last] + bids
            ).fetchone()[0] or 0
            lfl_pct = round((val_this / val_last - 1) * 100, 1) if val_last > 0 else None
            result.append({
                'year': yr, 'month': mo,
                'label': month_labels[mo] + " '" + str(yr)[-2:],
                'this_year': int(val_this),
                'last_year': int(val_last),
                'lfl_pct': lfl_pct,
                'is_current': is_current,
            })
    return jsonify({'ok': True, 'months': result})


@app.route('/api/revenue-summary')
@login_required
@owner_required
def api_revenue_summary():
    date_from = request.args.get('date_from', date.today().isoformat())
    date_to   = request.args.get('date_to',   date.today().isoformat())
    raw_bids  = request.args.get('branch_ids', '')
    bids      = [int(x) for x in raw_bids.split(',') if x.strip().isdigit()]
    bf        = f"AND s.branch_id IN ({','.join('?'*len(bids))})" if bids else ''
    with get_db() as conn:
        total_row = conn.execute(f'''
            SELECT COALESCE(SUM(r.total_revenue), 0)    AS total,
                   COALESCE(SUM(r.cash_amount),   0)    AS cash,
                   COALESCE(SUM(r.card_amount),   0)    AS card,
                   COALESCE(SUM(r.online_amount), 0)    AS online,
                   COALESCE(SUM(r.delivery_revenue), 0) AS delivery,
                   COALESCE(SUM(r.pickup_revenue),   0) AS pickup,
                   COALESCE(SUM(r.delivery_orders),  0) AS delivery_orders
            FROM shifts s JOIN shift_revenue r ON r.shift_id = s.id
            WHERE s.date BETWEEN ? AND ? {bf}
        ''', [date_from, date_to] + bids).fetchone()
        fot_row = conn.execute(f'''
            SELECT COALESCE(SUM(es.total_amount), 0) AS fot
            FROM employee_shifts es JOIN shifts s ON s.id = es.shift_id
            WHERE s.date BETWEEN ? AND ? {bf}
        ''', [date_from, date_to] + bids).fetchone()
        courier_fot_row = conn.execute(f'''
            SELECT COALESCE(SUM(es.total_amount), 0) AS courier_fot
            FROM employee_shifts es JOIN shifts s ON s.id = es.shift_id
            WHERE s.date BETWEEN ? AND ? AND es.role_snapshot = 'courier' {bf}
        ''', [date_from, date_to] + bids).fetchone()
        branch_rev_rows = conn.execute(f'''
            SELECT b.id, b.name, b.abbr,
                   COALESCE(SUM(r.total_revenue), 0)    AS revenue,
                   COALESCE(SUM(r.pickup_revenue), 0)   AS pickup,
                   COALESCE(SUM(r.delivery_revenue), 0) AS delivery_revenue,
                   COALESCE(SUM(r.delivery_orders),  0) AS delivery_orders
            FROM shifts s
            JOIN branches b ON b.id = s.branch_id
            JOIN shift_revenue r ON r.shift_id = s.id
            WHERE s.date BETWEEN ? AND ? {bf}
            GROUP BY b.id, b.name ORDER BY revenue DESC
        ''', [date_from, date_to] + bids).fetchall()
        branch_fot_rows = conn.execute(f'''
            SELECT b.name,
                   COALESCE(SUM(es.total_amount), 0) AS fot,
                   COALESCE(SUM(CASE WHEN es.role_snapshot='courier' THEN es.total_amount ELSE 0 END), 0) AS courier_fot
            FROM employee_shifts es
            JOIN shifts s ON s.id = es.shift_id
            JOIN branches b ON b.id = s.branch_id
            WHERE s.date BETWEEN ? AND ? {bf}
            GROUP BY b.id, b.name
        ''', [date_from, date_to] + bids).fetchall()

    total       = int(total_row['total'] or 0)
    fot         = int(fot_row['fot'] or 0)
    courier_fot = int(courier_fot_row['courier_fot'] or 0)
    fot_by_name = {r['name']: {'fot': int(r['fot']), 'courier_fot': int(r['courier_fot'])} for r in branch_fot_rows}
    branches = []
    for br in branch_rev_rows:
        name = br['name'] or ''
        abbr = (br['abbr'] or '').strip() or name[:3].upper()
        bf_   = fot_by_name.get(name, {'fot': 0, 'courier_fot': 0})
        branches.append({
            'abbr': abbr, 'name': name,
            'revenue':         int(br['revenue']),
            'pickup':          int(br['pickup']),
            'delivery_revenue':int(br['delivery_revenue']),
            'delivery_orders': int(br['delivery_orders']),
            'fot':             bf_['fot'],
            'courier_fot':     bf_['courier_fot'],
        })

    return jsonify({
        'ok': True,
        'total': total,
        'cash':  int(total_row['cash'] or 0),
        'card':  int(total_row['card'] or 0),
        'online': int(total_row['online'] or 0),
        'delivery': int(total_row['delivery'] or 0),
        'delivery_orders': int(total_row['delivery_orders'] or 0),
        'pickup': int(total_row['pickup'] or 0),
        'fot':   fot,
        'courier_fot': courier_fot,
        'branches': branches,
    })


@app.route('/api/revenue-days')
@login_required
@owner_required
def api_revenue_days():
    date_from = request.args.get('date_from', date.today().isoformat())
    date_to   = request.args.get('date_to',   date.today().isoformat())
    raw_bids  = request.args.get('branch_ids', '')
    bids      = [int(x) for x in raw_bids.split(',') if x.strip().isdigit()]
    bfilt     = f"AND s.branch_id IN ({','.join('?'*len(bids))})" if bids else ''
    with get_db() as conn:
        rows = conn.execute(f'''
            SELECT s.date, COALESCE(SUM(r.total_revenue), 0) AS revenue
            FROM shifts s JOIN shift_revenue r ON r.shift_id = s.id
            WHERE s.date BETWEEN ? AND ? {bfilt}
            GROUP BY s.date ORDER BY s.date
        ''', [date_from, date_to] + bids).fetchall()
    return jsonify({
        'ok': True,
        'days': [{'date': r['date'], 'revenue': int(r['revenue'])} for r in rows]
    })


@app.route('/api/fot-summary')
@login_required
@owner_required
def api_fot_summary():
    date_from = request.args.get('date_from', date.today().isoformat())
    date_to   = request.args.get('date_to',   date.today().isoformat())
    raw_bids  = request.args.get('branch_ids', '')
    bids      = [int(x) for x in raw_bids.split(',') if x.strip().isdigit()]
    bf        = f"AND s.branch_id IN ({','.join('?'*len(bids))})" if bids else ''
    with get_db() as conn:
        rev_row = conn.execute(f'''
            SELECT COALESCE(SUM(r.total_revenue), 0) AS revenue
            FROM shifts s JOIN shift_revenue r ON r.shift_id = s.id
            WHERE s.date BETWEEN ? AND ? {bf}
        ''', [date_from, date_to] + bids).fetchone()
        fot_row = conn.execute(f'''
            SELECT COALESCE(SUM(es.total_amount), 0) AS fot
            FROM employee_shifts es JOIN shifts s ON s.id = es.shift_id
            WHERE s.date BETWEEN ? AND ? {bf}
        ''', [date_from, date_to] + bids).fetchone()
        role_rows = conn.execute(f'''
            SELECT es.role_snapshot,
                   COALESCE(SUM(es.total_amount), 0) AS fot
            FROM employee_shifts es JOIN shifts s ON s.id = es.shift_id
            WHERE s.date BETWEEN ? AND ? {bf}
            GROUP BY es.role_snapshot ORDER BY fot DESC
        ''', [date_from, date_to] + bids).fetchall()
    revenue = int(rev_row['revenue'] or 0)
    fot     = int(fot_row['fot'] or 0)
    fot_pct = round(fot / revenue * 100, 1) if revenue > 0 else 0
    role_labels = {'admin':'Администраторы','sushi':'Сушисты','packer':'Упаковщики',
                   'courier':'Курьеры','cleaner':'Уборщицы','cook':'Повара'}
    roles = []
    for r in role_rows:
        rfot = int(r['fot'] or 0)
        role = r['role_snapshot'] or ''
        if rfot <= 0 or not role:
            continue
        roles.append({
            'role':    role,
            'label':   role_labels.get(role, role),
            'fot':     rfot,
            'pct':     round(rfot / fot * 100) if fot > 0 else 0,
            'rev_pct': round(rfot / revenue * 100, 1) if revenue > 0 else 0,
        })
    return jsonify({'ok': True, 'fot': fot, 'revenue': revenue, 'fot_pct': fot_pct, 'roles': roles})


@app.route('/api/fot-year')
@login_required
@owner_required
def api_fot_year():
    today   = date.today()
    start_m = today.month - 11
    start_y = today.year
    if start_m <= 0:
        start_m += 12
        start_y -= 1
    date_from = date(start_y, start_m, 1).isoformat()
    date_to   = today.isoformat()
    raw_bids  = request.args.get('branch_ids', '')
    bids      = [int(x) for x in raw_bids.split(',') if x.strip().isdigit()]
    bf        = f"AND s.branch_id IN ({','.join('?'*len(bids))})" if bids else ''
    with get_db() as conn:
        fot_rows = conn.execute(f'''
            SELECT CAST(strftime('%Y',s.date) AS INTEGER) AS year,
                   CAST(strftime('%m',s.date) AS INTEGER) AS month,
                   COALESCE(SUM(es.total_amount),0) AS fot
            FROM employee_shifts es JOIN shifts s ON s.id=es.shift_id
            WHERE s.date BETWEEN ? AND ? {bf} GROUP BY year,month
        ''', [date_from, date_to] + bids).fetchall()
        rev_rows = conn.execute(f'''
            SELECT CAST(strftime('%Y',s.date) AS INTEGER) AS year,
                   CAST(strftime('%m',s.date) AS INTEGER) AS month,
                   COALESCE(SUM(r.total_revenue),0) AS revenue
            FROM shifts s JOIN shift_revenue r ON r.shift_id=s.id
            WHERE s.date BETWEEN ? AND ? {bf} GROUP BY year,month
        ''', [date_from, date_to] + bids).fetchall()
    fot_map = {(r['year'],r['month']): int(r['fot']) for r in fot_rows}
    rev_map = {(r['year'],r['month']): int(r['revenue']) for r in rev_rows}
    labels  = ['','Янв','Фев','Мар','Апр','Май','Июн','Июл','Авг','Сен','Окт','Ноя','Дек']
    months_list = []
    y, m = start_y, start_m
    for _ in range(12):
        fv = fot_map.get((y, m), 0)
        rv = rev_map.get((y, m), 0)
        months_list.append({'year':y,'month':m,'label':labels[m],
                            'fot':fv,'revenue':rv,
                            'fot_pct': round(fv/rv*100, 1) if rv > 0 else 0})
        m += 1
        if m > 12: m = 1; y += 1
    return jsonify({'ok': True, 'months': months_list})


@app.route('/api/fot-days')
@login_required
@owner_required
def api_fot_days():
    date_from = request.args.get('date_from', date.today().isoformat())
    date_to   = request.args.get('date_to',   date.today().isoformat())
    role      = request.args.get('role', '')
    raw_bids  = request.args.get('branch_ids', '')
    bids      = [int(x) for x in raw_bids.split(',') if x.strip().isdigit()]
    bf        = f"AND s.branch_id IN ({','.join('?'*len(bids))})" if bids else ''
    with get_db() as conn:
        rev_rows = conn.execute(f'''
            SELECT s.date, COALESCE(SUM(r.total_revenue),0) AS revenue
            FROM shifts s JOIN shift_revenue r ON r.shift_id=s.id
            WHERE s.date BETWEEN ? AND ? {bf}
            GROUP BY s.date ORDER BY s.date
        ''', [date_from, date_to] + bids).fetchall()
        if role:
            fot_rows = conn.execute(f'''
                SELECT s.date, COALESCE(SUM(es.total_amount),0) AS fot
                FROM employee_shifts es JOIN shifts s ON s.id=es.shift_id
                WHERE s.date BETWEEN ? AND ? AND es.role_snapshot=? {bf}
                GROUP BY s.date ORDER BY s.date
            ''', [date_from, date_to, role] + bids).fetchall()
        else:
            fot_rows = conn.execute(f'''
                SELECT s.date, COALESCE(SUM(es.total_amount),0) AS fot
                FROM employee_shifts es JOIN shifts s ON s.id=es.shift_id
                WHERE s.date BETWEEN ? AND ? {bf}
                GROUP BY s.date ORDER BY s.date
            ''', [date_from, date_to] + bids).fetchall()
    rev_map = {r['date']: int(r['revenue']) for r in rev_rows}
    fot_map = {r['date']: int(r['fot'])     for r in fot_rows}
    days = []
    for d in sorted(rev_map):
        rv = rev_map[d]; fv = fot_map.get(d, 0)
        days.append({'date': d, 'fot': fv, 'revenue': rv,
                     'fot_pct': round(fv / rv * 100, 1) if rv > 0 else 0})
    return jsonify({'ok': True, 'days': days})


@app.route('/fot-dashboard')
@login_required
@owner_required
def fot_dashboard():
    with get_db() as conn:
        branches = [dict(b) for b in conn.execute('SELECT * FROM branches WHERE is_active=1 ORDER BY name').fetchall()]
        branch_groups = get_branch_groups(conn)
    return render_template('fot_dashboard.html', branches=branches, branch_groups=branch_groups)


@app.route('/lfl')
@login_required
@owner_required
def lfl_dashboard():
    with get_db() as conn:
        branches = [dict(b) for b in conn.execute('SELECT * FROM branches WHERE is_active=1 ORDER BY name').fetchall()]
        branch_groups = get_branch_groups(conn)
    return render_template('lfl_dashboard.html', branches=branches, branch_groups=branch_groups)


# ─── SHIFTS ───────────────────────────────────────────────────────────────────

@app.route('/shift/open', methods=['POST'])
@login_required
def open_shift():
    role = session.get('role')
    if role == 'owner':
        branch_id = request.form.get('branch_id', session.get('branch_id'))
    else:
        branch_id = request.form.get('branch_id') or session.get('branch_id')
        bids = _session_branch_ids()
        if branch_id and int(branch_id) not in bids:
            branch_id = bids[0] if bids else None
    if not branch_id:
        flash('Филиал не определён', 'danger')
        return redirect(url_for('dashboard'))
    today = date.today().isoformat()
    with get_db() as conn:
        existing = conn.execute(
            'SELECT id FROM shifts WHERE branch_id=? AND date=?', (branch_id, today)
        ).fetchone()
        if existing:
            flash('Смена на этот день уже существует', 'warning')
            return redirect(url_for('shift_view', shift_id=existing['id']))
        try:
            conn.execute(
                'INSERT INTO shifts (branch_id, date, opened_by) VALUES (?,?,?)',
                (branch_id, today, session['user_id'])
            )
        except Exception:
            # Race condition: another request created the shift simultaneously
            existing2 = conn.execute(
                'SELECT id FROM shifts WHERE branch_id=? AND date=?', (branch_id, today)
            ).fetchone()
            if existing2:
                flash('Смена на этот день уже существует', 'warning')
                return redirect(url_for('shift_view', shift_id=existing2['id']))
            raise
        shift_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        prev_cash_row = conn.execute('''
            SELECT COALESCE(r.morning_cash, 0)
                   + COALESCE(r.cash_amount, 0)
                   + COALESCE(r.change_amount, 0)
                   - COALESCE((SELECT SUM(e.amount_cash) FROM expenses e WHERE e.shift_id=s.id), 0)
                   - COALESCE((SELECT SUM(es.total_amount) FROM employee_shifts es
                                WHERE es.shift_id=s.id AND es.is_paid=1), 0)
                   AS kassa_nal
            FROM shifts s JOIN shift_revenue r ON r.shift_id=s.id
            WHERE s.branch_id=? AND s.date<?
            ORDER BY s.date DESC LIMIT 1
        ''', (int(branch_id), today)).fetchone()
        prev_morning = (prev_cash_row['kassa_nal'] or 0) if prev_cash_row else 0
        conn.execute(
            'INSERT INTO shift_revenue (shift_id, morning_cash) VALUES (?, ?)',
            (shift_id, prev_morning)
        )
        _apply_change_amount_to_shift(conn, shift_id, int(branch_id), today)
        conn.commit()
    return redirect(url_for('shift_view', shift_id=shift_id))


@app.route('/shift/<int:shift_id>')
@login_required
def shift_view(shift_id):
    with get_db() as conn:
        shift = conn.execute('''
            SELECT s.*, b.name as branch_name
            FROM shifts s JOIN branches b ON b.id=s.branch_id
            WHERE s.id=?
        ''', (shift_id,)).fetchone()
        if not shift:
            flash('Смена не найдена', 'danger')
            return redirect(url_for('dashboard'))
        role = session.get('role')
        if role != 'owner' and shift['branch_id'] not in _session_branch_ids():
            flash('Нет доступа к этой смене', 'danger')
            return redirect(url_for('dashboard'))

        revenue = conn.execute('SELECT * FROM shift_revenue WHERE shift_id=?', (shift_id,)).fetchone()
        expenses = conn.execute('SELECT * FROM expenses WHERE shift_id=? ORDER BY id', (shift_id,)).fetchall()
        plus_entries = conn.execute('SELECT * FROM cash_plus_entries WHERE shift_id=? ORDER BY id', (shift_id,)).fetchall()
        staff = conn.execute(
            '''SELECT es.*, COALESCE(e.pay_monthly, 0) as pay_monthly
               FROM employee_shifts es
               LEFT JOIN employees e ON e.id = es.employee_id
               WHERE es.shift_id=? ORDER BY es.role_snapshot, es.full_name_snapshot''',
            (shift_id,)
        ).fetchall()
        employees = conn.execute(
            '''SELECT DISTINCT e.* FROM employees e
               JOIN employee_branches eb ON eb.employee_id=e.id
               WHERE eb.branch_id=? AND e.is_active=1
               ORDER BY e.role, e.full_name''',
            (shift['branch_id'],)
        ).fetchall()
        # Current address per employee (as of shift date)
        emp_addresses = {}
        for emp in employees:
            addr = conn.execute(
                'SELECT address FROM employee_address_history WHERE employee_id=? AND valid_from<=? ORDER BY valid_from DESC LIMIT 1',
                (emp['id'], shift['date'])
            ).fetchone()
            emp_addresses[emp['id']] = addr['address'] if addr else ''
        # Taxi trips for this shift
        taxi_trips = conn.execute(
            'SELECT * FROM taxi_trips WHERE shift_id=? ORDER BY id',
            (shift_id,)
        ).fetchall()
        taxi_trip_emps = {}
        for t in taxi_trips:
            tte = conn.execute(
                'SELECT * FROM taxi_trip_employees WHERE trip_id=? ORDER BY id',
                (t['id'],)
            ).fetchall()
            taxi_trip_emps[t['id']] = tte
        shift_terminals_rows = conn.execute(
            'SELECT terminal_number, amount FROM shift_terminals WHERE shift_id=? ORDER BY sort_order, id',
            (shift_id,)
        ).fetchall()
        all_cats = get_expense_categories(conn)
        expense_cats = [c for c in all_cats if c['type'] != 'income']
        income_cats  = [c for c in all_cats if c['type'] == 'income']
        expense_cats_groups = build_cats_groups(expense_cats)
        expense_cats_flat   = [(c['code'], c['label']) for c in expense_cats]
        income_cats_groups  = build_cats_groups(income_cats)
        income_cats_flat    = [(c['code'], c['label']) for c in income_cats]
        seen_taxi_ids = set()
        taxi_staff = []
        for s in staff:
            if s['role_snapshot'] != 'courier' and s['employee_id'] and s['employee_id'] not in seen_taxi_ids:
                seen_taxi_ids.add(s['employee_id'])
                taxi_staff.append(s)
        can_edit = (role == 'owner') or (shift['status'] == 'open')
        try:
            shift_weekday = date.fromisoformat(shift['date']).weekday()  # 0=Mon, 4=Fri, 5=Sat
        except Exception:
            shift_weekday = 0
        # Утром в кассе: итого нал предыдущего дня по этому филиалу
        prev_day_row = conn.execute('''
            SELECT COALESCE(r.morning_cash, 0)
                   + COALESCE(r.cash_amount, 0)
                   + COALESCE(r.change_amount, 0)
                   - COALESCE((SELECT SUM(e.amount_cash) FROM expenses e WHERE e.shift_id=s.id), 0)
                   - COALESCE((SELECT SUM(es.total_amount) FROM employee_shifts es
                                WHERE es.shift_id=s.id AND es.is_paid=1), 0)
                   AS kassa_nal
            FROM shifts s JOIN shift_revenue r ON r.shift_id=s.id
            WHERE s.branch_id=? AND s.date<?
            ORDER BY s.date DESC LIMIT 1
        ''', (shift['branch_id'], shift['date'])).fetchone()
        prev_actual_cash = (prev_day_row['kassa_nal'] or 0) if prev_day_row else None
        return render_template('shift.html',
            shift=shift, revenue=revenue, expenses=expenses, plus_entries=plus_entries,
            staff=staff, employees=employees, taxi_staff=taxi_staff,
            emp_addresses=emp_addresses,
            taxi_trips=taxi_trips, taxi_trip_emps=taxi_trip_emps,
            expense_categories=expense_cats_flat,
            expense_cats_groups=expense_cats_groups,
            income_categories=income_cats_flat,
            income_cats_groups=income_cats_groups,
        role_labels=ROLE_LABELS,
            can_edit=can_edit,
            is_owner=(role == 'owner'),
            shift_weekday=shift_weekday,
            prev_actual_cash=prev_actual_cash,
            shift_terminals=[{'terminal_number': r['terminal_number'], 'amount': r['amount']}
                             for r in shift_terminals_rows])


@app.route('/shift/<int:shift_id>/save-revenue', methods=['POST'])
@login_required
def save_revenue(shift_id):
    if not _can_edit_shift(shift_id):
        return jsonify({'error': 'Нет доступа'}), 403
    data = request.json or {}
    with get_db() as conn:
        conn.execute('''
            UPDATE shift_revenue SET
                total_revenue=?, delivery_revenue=?, delivery_orders=?,
                pickup_revenue=?, pickup_orders=?,
                cash_amount=?, card_amount=?, online_amount=?,
                change_amount=?, actual_cash=?, terminal_last3=?, terminal_amount=?,
                morning_cash=?
            WHERE shift_id=?
        ''', (
            _f(data, 'total_revenue'), _f(data, 'delivery_revenue'), _i(data, 'delivery_orders'),
            _f(data, 'pickup_revenue'), _i(data, 'pickup_orders'),
            _f(data, 'cash_amount'), _f(data, 'card_amount'), _f(data, 'online_amount'),
            _f(data, 'change_amount'), _f(data, 'actual_cash'),
            data.get('terminal_last3', ''), _f(data, 'terminal_amount'),
            _f(data, 'morning_cash'),
            shift_id
        ))
        desc = (f"нал {_fmt_money(data.get('cash_amount'))}, "
                f"безнал {_fmt_money(data.get('card_amount'))}, "
                f"онлайн {_fmt_money(data.get('online_amount'))}, "
                f"итого {_fmt_money(data.get('total_revenue'))}")
        log_action(conn, 'revenue_update', desc, shift_id=shift_id, upsert_by_shift=True)
        auto_bonuses = calculate_bonuses(conn, shift_id)
        conn.commit()
    return jsonify({'ok': True, 'auto_bonuses': auto_bonuses})


@app.route('/shift/<int:shift_id>/save-terminals', methods=['POST'])
@login_required
def save_terminals(shift_id):
    if not _can_edit_shift(shift_id):
        return jsonify({'error': 'Нет доступа'}), 403
    data = request.json or {}
    terminals = data.get('terminals', [])
    with get_db() as conn:
        conn.execute('DELETE FROM shift_terminals WHERE shift_id=?', (shift_id,))
        for i, t in enumerate(terminals):
            tn = str(t.get('terminal_number', '')).strip()
            raw = str(t.get('amount', 0)).replace(' ', '').replace(' ', '').replace(',', '.')
            try:
                amt = float(raw)
            except ValueError:
                amt = 0.0
            if tn or amt:
                conn.execute(
                    'INSERT INTO shift_terminals (shift_id, terminal_number, amount, sort_order) VALUES (?,?,?,?)',
                    (shift_id, tn, amt, i)
                )
        conn.commit()
    return jsonify({'ok': True})


@app.route('/shift/<int:shift_id>/save-plus', methods=['POST'])
@login_required
def save_cash_plus(shift_id):
    if not _can_edit_shift(shift_id):
        return jsonify({'error': 'Нет доступа'}), 403
    data = request.json or {}
    entry_id    = data.get('id')
    amount_cash = float(data.get('amount_cash') or 0)
    amount_card = float(data.get('amount_card') or 0)
    amount      = amount_cash + amount_card
    category    = (data.get('category') or '').strip()
    description = (data.get('description') or '').strip()
    with get_db() as conn:
        if entry_id:
            conn.execute(
                'UPDATE cash_plus_entries SET amount=?, amount_cash=?, amount_card=?, category=?, description=? WHERE id=? AND shift_id=?',
                (amount, amount_cash, amount_card, category, description, entry_id, shift_id)
            )
        else:
            conn.execute(
                'INSERT INTO cash_plus_entries (shift_id, amount, amount_cash, amount_card, category, description) VALUES (?,?,?,?,?,?)',
                (shift_id, amount, amount_cash, amount_card, category, description)
            )
            entry_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.commit()
    return jsonify({'ok': True, 'id': entry_id})


@app.route('/shift/<int:shift_id>/delete-plus/<int:entry_id>', methods=['POST'])
@login_required
def delete_cash_plus(shift_id, entry_id):
    if not _can_edit_shift(shift_id):
        return jsonify({'error': 'Нет доступа'}), 403
    with get_db() as conn:
        conn.execute('DELETE FROM cash_plus_entries WHERE id=? AND shift_id=?', (entry_id, shift_id))
        conn.commit()
    return jsonify({'ok': True})


@app.route('/shift/<int:shift_id>/save-expense', methods=['POST'])
@login_required
def save_expense(shift_id):
    if not _can_edit_shift(shift_id):
        return jsonify({'error': 'Нет доступа'}), 403
    data = request.json or {}
    expense_id = data.get('id')
    with get_db() as conn:
        cats = {r['code']: r['label'] for r in get_expense_categories(conn)}
        cat_label = cats.get(data.get('category', 'other'), data.get('category', 'другое'))
        amount = _f(data, 'amount_cash') + _f(data, 'amount_card')
        pay_type = 'нал' if _f(data, 'amount_cash') > 0 else 'безнал'
        note = (data.get('description') or '').strip()
        if expense_id:
            conn.execute('''
                UPDATE expenses SET category=?, description=?, amount_cash=?, amount_card=?, is_gulash=?
                WHERE id=? AND shift_id=?
            ''', (data.get('category', 'other'), data.get('description', ''),
                  _f(data, 'amount_cash'), _f(data, 'amount_card'), 1 if data.get('is_gulash') else 0,
                  expense_id, shift_id))
            desc = f"Изменён расход: {cat_label}"
            if note:
                desc += f" «{note}»"
            desc += f", {_fmt_money(amount)} ({pay_type})"
            log_action(conn, 'expense_update', desc, shift_id=shift_id, entity_id=expense_id)
        else:
            conn.execute('''
                INSERT INTO expenses (shift_id, category, description, amount_cash, amount_card, is_gulash)
                VALUES (?,?,?,?,?,?)
            ''', (shift_id, data.get('category', 'other'), data.get('description', ''),
                  _f(data, 'amount_cash'), _f(data, 'amount_card'), 1 if data.get('is_gulash') else 0))
            expense_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            desc = f"Добавлен расход: {cat_label}"
            if note:
                desc += f" «{note}»"
            desc += f", {_fmt_money(amount)} ({pay_type})"
            log_action(conn, 'expense_add', desc, shift_id=shift_id, entity_id=expense_id)
        conn.commit()
    return jsonify({'ok': True, 'id': expense_id})


@app.route('/shift/<int:shift_id>/delete-expense/<int:expense_id>', methods=['POST'])
@login_required
def delete_expense(shift_id, expense_id):
    if not _can_edit_shift(shift_id):
        return jsonify({'error': 'Нет доступа'}), 403
    with get_db() as conn:
        exp = conn.execute('SELECT * FROM expenses WHERE id=? AND shift_id=?', (expense_id, shift_id)).fetchone()
        if exp:
            cats = {r['code']: r['label'] for r in get_expense_categories(conn)}
            cat_label = cats.get(exp['category'], exp['category'])
            amount = (exp['amount_cash'] or 0) + (exp['amount_card'] or 0)
            pay_type = 'нал' if (exp['amount_cash'] or 0) > 0 else 'безнал'
            note = (exp['description'] or '').strip()
            desc = f"Удалён расход: {cat_label}"
            if note:
                desc += f" «{note}»"
            desc += f", {_fmt_money(amount)} ({pay_type})"
        else:
            desc = f"Удалён расход #{expense_id}"
        conn.execute('DELETE FROM expenses WHERE id=? AND shift_id=?', (expense_id, shift_id))
        log_action(conn, 'expense_delete', desc, shift_id=shift_id, entity_id=expense_id)
        conn.commit()
    return jsonify({'ok': True})


@app.route('/shift/<int:shift_id>/save-staff', methods=['POST'])
@login_required
def save_staff(shift_id):
    if not _can_edit_shift(shift_id):
        return jsonify({'error': 'Нет доступа'}), 403
    data = request.json or {}
    staff_id = data.get('id')
    with get_db() as conn:
        if staff_id:
            # Preserve bonus fields if client didn't send them (e.g. hours-only autosave)
            existing = conn.execute(
                'SELECT bonus_amount, penalty_amount, bonus_comment FROM employee_shifts WHERE id=? AND shift_id=?',
                (staff_id, shift_id)
            ).fetchone()
            has_bonus = 'bonus_amount' in data
            bonus_amount  = _f(data, 'bonus_amount')  if has_bonus else float(existing['bonus_amount']  or 0 if existing else 0)
            penalty_amount = _f(data, 'penalty_amount') if has_bonus else float(existing['penalty_amount'] or 0 if existing else 0)
            bonus_comment  = data.get('bonus_comment', (existing['bonus_comment'] or '') if existing else '') if has_bonus else ((existing['bonus_comment'] or '') if existing else '')
            conn.execute('''
                UPDATE employee_shifts SET
                    full_name_snapshot=?, role_snapshot=?, rate_snapshot=?,
                    rate_per_km_snapshot=?, rate_per_order_snapshot=?,
                    shift_start=?, shift_end=?, hours_worked=?,
                    km=?, orders=?, bonus_amount=?, penalty_amount=?, bonus_comment=?,
                    base_pay=?, total_amount=?, is_paid=?, paid_amount=?
                WHERE id=? AND shift_id=?
            ''', (
                data.get('full_name_snapshot', ''), data.get('role_snapshot', ''),
                _f(data, 'rate_snapshot'), _f(data, 'rate_per_km_snapshot'), _f(data, 'rate_per_order_snapshot'),
                data.get('shift_start', ''), data.get('shift_end', ''), _f(data, 'hours_worked'),
                _f(data, 'km'), _i(data, 'orders'),
                bonus_amount, penalty_amount, bonus_comment,
                _f(data, 'base_pay'), _f(data, 'total_amount'),
                1 if data.get('is_paid') else 0, _f(data, 'paid_amount'),
                staff_id, shift_id
            ))
            name = data.get('full_name_snapshot', '')
            role_lbl = ROLE_LABELS.get(data.get('role_snapshot', ''), data.get('role_snapshot', ''))
            log_action(conn, 'staff_update',
                f"Обновлены данные: {name} ({role_lbl}), итого {_fmt_money(data.get('total_amount'))}",
                shift_id=shift_id, entity_id=staff_id)
            auto_bonuses = calculate_bonuses(conn, shift_id)
        else:
            emp_id = data.get('employee_id')
            if emp_id:
                already = conn.execute(
                    'SELECT id FROM employee_shifts WHERE shift_id=? AND employee_id=?',
                    (shift_id, emp_id)
                ).fetchone()
                if already:
                    return jsonify({'ok': False, 'error': 'duplicate'}), 200
            rate = _f(data, 'rate_snapshot')
            rate_km = _f(data, 'rate_per_km_snapshot')
            rate_ord = _f(data, 'rate_per_order_snapshot')
            if emp_id:
                # Look up rate active on the shift date
                shift = conn.execute('SELECT date FROM shifts WHERE id=?', (shift_id,)).fetchone()
                shift_date = shift['date'] if shift else date.today().isoformat()
                hist = conn.execute('''
                    SELECT rate, rate_per_km, rate_per_order
                    FROM employee_rate_history
                    WHERE employee_id=? AND effective_from <= ?
                    ORDER BY effective_from DESC LIMIT 1
                ''', (emp_id, shift_date)).fetchone()
                if hist:
                    rate = rate or hist['rate']
                    rate_km = rate_km or hist['rate_per_km']
                    rate_ord = rate_ord or hist['rate_per_order']
                else:
                    emp = conn.execute('SELECT * FROM employees WHERE id=?', (emp_id,)).fetchone()
                    if emp:
                        rate = rate or emp['rate']
                        rate_km = rate_km or emp['rate_per_km']
                        rate_ord = rate_ord or emp['rate_per_order']
            conn.execute('''
                INSERT INTO employee_shifts
                (shift_id, employee_id, full_name_snapshot, role_snapshot,
                 rate_snapshot, rate_per_km_snapshot, rate_per_order_snapshot,
                 shift_start, shift_end, hours_worked, km, orders,
                 bonus_amount, penalty_amount, bonus_comment,
                 base_pay, total_amount, is_paid, paid_amount)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ''', (
                shift_id, emp_id or None,
                data.get('full_name_snapshot', ''), data.get('role_snapshot', ''),
                rate, rate_km, rate_ord,
                data.get('shift_start', ''), data.get('shift_end', ''), _f(data, 'hours_worked'),
                _f(data, 'km'), _i(data, 'orders'),
                _f(data, 'bonus_amount'), _f(data, 'penalty_amount'), data.get('bonus_comment', ''),
                _f(data, 'base_pay'), _f(data, 'total_amount'),
                1 if data.get('is_paid') else 0, _f(data, 'paid_amount')
            ))
            staff_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            name = data.get('full_name_snapshot', '')
            role_lbl = ROLE_LABELS.get(data.get('role_snapshot', ''), data.get('role_snapshot', ''))
            log_action(conn, 'staff_add',
                f"Добавлен сотрудник: {name} ({role_lbl})",
                shift_id=shift_id, entity_id=staff_id)
            auto_bonuses = calculate_bonuses(conn, shift_id)
        conn.commit()
    return jsonify({'ok': True, 'id': staff_id, 'auto_bonuses': auto_bonuses})


@app.route('/shift/<int:shift_id>/pay-staff/<int:staff_id>', methods=['POST'])
@login_required
def pay_staff(shift_id, staff_id):
    if not _can_edit_shift(shift_id):
        return jsonify({'error': 'Нет доступа'}), 403
    data = request.json or {}
    amount = _f(data, 'amount')
    with get_db() as conn:
        row = conn.execute(
            'SELECT * FROM employee_shifts WHERE id=? AND shift_id=?', (staff_id, shift_id)
        ).fetchone()
        if not row:
            return jsonify({'error': 'Не найдено'}), 404
        if row['employee_id']:
            emp = conn.execute('SELECT pay_monthly FROM employees WHERE id=?', (row['employee_id'],)).fetchone()
            if emp and emp['pay_monthly']:
                return jsonify({'error': 'pay_monthly'}), 400
        conn.execute(
            'UPDATE employee_shifts SET is_paid=1, paid_amount=? WHERE id=?',
            (amount, staff_id)
        )
        conn.execute('''
            INSERT INTO salary_payments
            (employee_id, employee_shift_id, amount, payment_date, paid_by, paid_by_name)
            VALUES (?,?,?,?,?,?)
        ''', (row['employee_id'], staff_id, amount, date.today().isoformat(),
              session['user_id'], session.get('full_name', '')))
        name = row['full_name_snapshot']
        role_lbl = ROLE_LABELS.get(row['role_snapshot'], row['role_snapshot'])
        log_action(conn, 'salary_paid',
            f"Выплата ЗП: {name} ({role_lbl}), {_fmt_money(amount)}",
            shift_id=shift_id, entity_id=staff_id)
        conn.commit()
    return jsonify({'ok': True})


@app.route('/shift/<int:shift_id>/delete-staff/<int:staff_id>', methods=['POST'])
@login_required
def delete_staff(shift_id, staff_id):
    if not _can_edit_shift(shift_id):
        return jsonify({'error': 'Нет доступа'}), 403
    with get_db() as conn:
        row = conn.execute(
            'SELECT * FROM employee_shifts WHERE id=? AND shift_id=?', (staff_id, shift_id)
        ).fetchone()
        if row:
            name = row['full_name_snapshot']
            role_lbl = ROLE_LABELS.get(row['role_snapshot'], row['role_snapshot'])
            desc = f"Удалён из смены: {name} ({role_lbl})"
        else:
            desc = f"Удалён сотрудник #{staff_id}"
        conn.execute('DELETE FROM employee_shifts WHERE id=? AND shift_id=?', (staff_id, shift_id))
        log_action(conn, 'staff_delete', desc, shift_id=shift_id, entity_id=staff_id)
        auto_bonuses = calculate_bonuses(conn, shift_id)
        conn.commit()
    return jsonify({'ok': True, 'auto_bonuses': auto_bonuses})


GSHEET_COLS = [
    ('date',            'Дата'),
    ('branch',          'Филиал'),
    ('revenue_total',   'Выручка итого'),
    ('revenue_cash',    'Наличные приход'),
    ('revenue_card',    'Безнал приход'),
    ('actual_cash',     'Факт в кассе'),
    ('exp_cash_total',  'Расходы нал итого'),
    ('exp_card_total',  'Расходы карта итого'),
    ('exp_by_cat',      'Расходы по категориям'),
    ('salary_total',    'ФОТ итого'),
    ('salary_by_role',  'ФОТ по должностям'),
    ('profit',          'Прибыль'),
    ('closed_by',       'Кто закрыл'),
    ('comment',         'Комментарий'),
]

def _gsheet_load_settings(conn):
    rows = conn.execute('SELECT key, value FROM gsheet_settings').fetchall()
    cfg  = {r['key']: r['value'] for r in rows}
    # По умолчанию все включены
    result = {}
    for key, _ in GSHEET_COLS:
        result[key] = int(cfg.get(f'col_{key}', '1'))
    return result


def _export_shift_to_gsheet(shift_id):
    """Экспорт смены в Google Sheets. Ошибки не прерывают работу."""
    try:
        import gspread
        from google.oauth2.service_account import Credentials

        creds_json = os.environ.get('GOOGLE_CREDENTIALS_JSON')
        sheet_id   = os.environ.get('GOOGLE_SHEET_ID')
        if not creds_json or not sheet_id:
            return

        creds_dict = _json_lib.loads(creds_json)
        scopes = [
            'https://spreadsheets.google.com/feeds',
            'https://www.googleapis.com/auth/drive',
        ]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        gc    = gspread.authorize(creds)
        sh    = gc.open_by_key(sheet_id)

        with get_db() as conn:
            shift = conn.execute('''
                SELECT s.*, b.name AS branch_name
                FROM shifts s JOIN branches b ON b.id = s.branch_id
                WHERE s.id = ?
            ''', (shift_id,)).fetchone()
            if not shift:
                return

            rev = dict(conn.execute(
                'SELECT * FROM shift_revenue WHERE shift_id=?', (shift_id,)
            ).fetchone() or {})

            all_exp_cats = conn.execute(
                'SELECT code, label FROM expense_categories ORDER BY sort_order, label'
            ).fetchall()

            exp_totals = conn.execute('''
                SELECT COALESCE(SUM(amount_cash),0) AS cash_total,
                       COALESCE(SUM(amount_card),0) AS card_total
                FROM expenses WHERE shift_id=?
            ''', (shift_id,)).fetchone()

            exp_by_cat_rows = conn.execute('''
                SELECT e.category,
                       COALESCE(SUM(e.amount_cash),0) AS cash_amt,
                       COALESCE(SUM(e.amount_card),0) AS card_amt
                FROM expenses e WHERE e.shift_id=?
                GROUP BY e.category
            ''', (shift_id,)).fetchall()
            exp_by_cat = {r['category']: {'cash': r['cash_amt'] or 0, 'card': r['card_amt'] or 0}
                          for r in exp_by_cat_rows}

            exp_items = conn.execute('''
                SELECT COALESCE(ec.label, e.category, '?') AS cat_label,
                       e.description, e.amount_cash, e.amount_card
                FROM expenses e
                LEFT JOIN expense_categories ec ON ec.code = e.category
                WHERE e.shift_id=?
                ORDER BY ec.sort_order, e.category, e.id
            ''', (shift_id,)).fetchall()

            staff = conn.execute('''
                SELECT full_name_snapshot, role_snapshot,
                       shift_start, shift_end, hours_worked, km, orders,
                       rate_snapshot, base_pay, bonus_amount, penalty_amount,
                       auto_bonus, total_amount, is_paid, paid_amount, bonus_comment
                FROM employee_shifts WHERE shift_id=?
                ORDER BY role_snapshot, full_name_snapshot
            ''', (shift_id,)).fetchall()

            taxi_trips = conn.execute(
                'SELECT * FROM taxi_trips WHERE shift_id=? ORDER BY id', (shift_id,)
            ).fetchall()
            taxi_emps = {}
            for t in taxi_trips:
                taxi_emps[t['id']] = conn.execute(
                    'SELECT name_snapshot, address_snapshot FROM taxi_trip_employees WHERE trip_id=? ORDER BY id',
                    (t['id'],)
                ).fetchall()

            terminals = conn.execute(
                'SELECT terminal_number, amount FROM shift_terminals WHERE shift_id=? ORDER BY sort_order, id',
                (shift_id,)
            ).fetchall()

        def _rv(col, default=0):
            v = rev.get(col, default)
            return v if v is not None else default

        total_rev      = _rv('total_revenue')
        cash_rev       = _rv('cash_amount')
        card_rev       = _rv('card_amount')
        online_rev     = _rv('online_amount')
        change_amt     = _rv('change_amount')
        morning_cash   = _rv('morning_cash', 0)
        actual_cash    = _rv('actual_cash', '')
        actual_cash_cmt= _rv('actual_cash_comment', '')
        delivery_rev   = _rv('delivery_revenue')
        delivery_orders= int(_rv('delivery_orders'))
        pickup_rev     = _rv('pickup_revenue')
        pickup_orders  = int(_rv('pickup_orders'))
        exp_cash       = round((exp_totals['cash_total'] or 0) if exp_totals else 0, 2)
        exp_card       = round((exp_totals['card_total'] or 0) if exp_totals else 0, 2)
        salary         = round(sum((s['total_amount'] or 0) for s in staff), 2)
        taxi_total     = round(sum((t['amount'] or 0) for t in taxi_trips), 2)
        profit         = round(total_rev - exp_cash - exp_card - salary, 2)

        role_labels_map = {
            'admin': 'Администратор', 'cook': 'Повар', 'sushi': 'Сушист',
            'courier': 'Курьер', 'packer': 'Упаковщик', 'cleaner': 'Уборщица',
        }
        all_roles = ['admin', 'cook', 'sushi', 'courier', 'packer', 'cleaner']
        sal_by_role = {}
        for s in staff:
            role = s['role_snapshot'] or 'other'
            sal_by_role[role] = sal_by_role.get(role, 0) + (s['total_amount'] or 0)

        # ─── Build header ──────────────────────────────────────────────────
        header = [
            'Дата', 'Филиал',
            'Выручка итого',
            'Доставка сумма', 'Доставка заказов',
            'Самовывоз сумма', 'Самовывоз заказов',
            'Наличные приход', 'Безнал приход', 'Онлайн приход',
            'Размен', 'Утренняя касса',
            'Факт в кассе', 'Комментарий к факту',
            'Терминалы',
            'Расходы нал итого', 'Расходы безнал итого',
        ]
        for ec in all_exp_cats:
            header.append(f'{ec["label"]} (нал)')
            header.append(f'{ec["label"]} (безнал)')
        header += [
            'Детали расходов',
            'ФОТ итого',
        ]
        for role in all_roles:
            header.append(f'ФОТ {role_labels_map[role]}')
        header += [
            'Такси итого',
            'Сотрудники',
            'Детали такси',
            'Прибыль', 'Кто закрыл', 'Комментарий смены',
        ]

        # ─── Build row ─────────────────────────────────────────────────────
        terminals_text = '; '.join(
            f'{t["terminal_number"]}: {round(t["amount"] or 0, 2)}р' for t in terminals
        )

        exp_details = []
        for ei in exp_items:
            total_ei = (ei['amount_cash'] or 0) + (ei['amount_card'] or 0)
            pay = 'нал' if (ei['amount_cash'] or 0) > 0 else 'безнал'
            line = f'{ei["cat_label"]}: {round(total_ei, 2)}р ({pay})'
            if ei['description']:
                line += f' — {ei["description"]}'
            exp_details.append(line)

        staff_lines = []
        for s in staff:
            role_lbl = role_labels_map.get(s['role_snapshot'], s['role_snapshot'] or '')
            hours = s['hours_worked'] or 0
            total = s['total_amount'] or 0
            paid_mark = '✓' if s['is_paid'] else '—'
            line = f'{s["full_name_snapshot"]} ({role_lbl}): {hours}ч → {round(total, 2)}р [{paid_mark}]'
            if s['bonus_comment']:
                line += f' ({s["bonus_comment"]})'
            staff_lines.append(line)

        taxi_lines = []
        for t in taxi_trips:
            emps = taxi_emps.get(t['id'], [])
            emp_names = ', '.join(e['name_snapshot'] for e in emps)
            addrs = [e['address_snapshot'] for e in emps if e['address_snapshot']]
            pay_type = 'нал' if (t['payment_type'] or 'cash') == 'cash' else 'безнал'
            line = f'{round(t["amount"] or 0, 2)}р ({pay_type})'
            if emp_names:
                line += f': {emp_names}'
            if addrs:
                line += f' → {", ".join(addrs)}'
            if t['note']:
                line += f' [{t["note"]}]'
            taxi_lines.append(line)

        row = [
            shift['date'], shift['branch_name'],
            round(total_rev, 2),
            round(delivery_rev, 2), delivery_orders,
            round(pickup_rev, 2), pickup_orders,
            round(cash_rev, 2), round(card_rev, 2), round(online_rev, 2),
            round(change_amt, 2), round(morning_cash, 2),
            round(actual_cash, 2) if actual_cash != '' else '',
            actual_cash_cmt,
            terminals_text,
            exp_cash, exp_card,
        ]
        for ec in all_exp_cats:
            cat = exp_by_cat.get(ec['code'], {'cash': 0, 'card': 0})
            row.append(round(cat['cash'], 2))
            row.append(round(cat['card'], 2))
        row += [
            '\n'.join(exp_details),
            salary,
        ]
        for role in all_roles:
            row.append(round(sal_by_role.get(role, 0), 2))
        row += [
            taxi_total,
            '\n'.join(staff_lines),
            '\n'.join(taxi_lines),
            profit,
            shift['closed_by_name'] or '',
            shift['comment'] or '',
        ]

        year = (shift['date'] or '')[:4] or str(date.today().year)
        try:
            ws = sh.worksheet(year)
        except Exception:
            ws = sh.add_worksheet(title=year, rows=3000, cols=len(header) + 5)
            ws.append_row(header)

        ws.append_row(row, value_input_option='USER_ENTERED')
        print(f'[GSheets] shift {shift_id} exported OK ({len(row)} columns)')
    except Exception as e:
        print(f'[GSheets] shift {shift_id} export error: {e}')


def _export_shift_to_gdrive_xlsx(shift_id):
    """Экспорт смены в Google Drive как xlsx в формате импорта."""
    print(f'[GDrive] start shift {shift_id}')
    try:
        import openpyxl
        from openpyxl import Workbook
        import io as _io
        import urllib.request
        import datetime as _dt_mod

        creds_json = os.environ.get('GOOGLE_CREDENTIALS_JSON')
        folder_id  = os.environ.get('GOOGLE_DRIVE_FOLDER_ID')
        print(f'[GDrive] creds={bool(creds_json)} folder_id={folder_id!r}')
        if not creds_json or not folder_id:
            print(f'[GDrive] missing env vars, exit')
            return

        with get_db() as conn:
            shift = conn.execute('''
                SELECT s.*, b.name AS branch_name
                FROM shifts s JOIN branches b ON b.id = s.branch_id
                WHERE s.id = ?
            ''', (shift_id,)).fetchone()
            if not shift:
                return

            rev = dict(conn.execute(
                'SELECT * FROM shift_revenue WHERE shift_id=?', (shift_id,)
            ).fetchone() or {})

            exp_rows = conn.execute(
                "SELECT category, description, amount_cash, amount_card FROM expenses WHERE shift_id=? ORDER BY category, id",
                (shift_id,)
            ).fetchall()

            taxi_trips = conn.execute(
                'SELECT amount, payment_type, note FROM taxi_trips WHERE shift_id=? ORDER BY id',
                (shift_id,)
            ).fetchall()

            couriers = conn.execute(
                "SELECT full_name_snapshot, hours_worked, km, orders, rate_snapshot, "
                "rate_per_km_snapshot, rate_per_order_snapshot, bonus_comment, total_amount, is_paid "
                "FROM employee_shifts WHERE shift_id=? AND role_snapshot='courier' ORDER BY full_name_snapshot",
                (shift_id,)
            ).fetchall()

            non_couriers = conn.execute(
                "SELECT full_name_snapshot, role_snapshot, rate_snapshot, shift_start, shift_end, "
                "hours_worked, bonus_amount, penalty_amount, bonus_comment, total_amount, is_paid "
                "FROM employee_shifts WHERE shift_id=? AND role_snapshot IN ('admin','sushi','cleaner','cook','packer') "
                "ORDER BY role_snapshot, full_name_snapshot",
                (shift_id,)
            ).fetchall()

            terminals = conn.execute(
                'SELECT terminal_number, amount FROM shift_terminals WHERE shift_id=? ORDER BY sort_order, id',
                (shift_id,)
            ).fetchall()

        def _rv(col, default=0):
            v = rev.get(col, default)
            return v if v is not None else default

        # Reverse map: category code → import label
        _CAT_REVERSE = {}
        for _lbl, _code in _XL_CAT_MAP.items():
            if _code not in _CAT_REVERSE:
                _CAT_REVERSE[_code] = _lbl

        # Fixed row positions for known expense categories (match import template)
        _CAT_ROW = {
            'repair_plumbing': 10, 'repair_grease':  11,
            'repair_electric': 12, 'repair_fridge':  13,
            'repair_other':    14, 'shop':            15,
        }

        # Aggregate expenses by category
        exp_by_cat = {}
        for e in exp_rows:
            cat = e['category'] or 'other'
            if cat == 'taxi':
                continue
            if cat not in exp_by_cat:
                exp_by_cat[cat] = {'cash': 0.0, 'card': 0.0, 'descs': []}
            exp_by_cat[cat]['cash'] += e['amount_cash'] or 0
            exp_by_cat[cat]['card'] += e['amount_card'] or 0
            if e['description']:
                exp_by_cat[cat]['descs'].append(e['description'])

        # Build workbook
        wb = Workbook()
        ws = wb.active
        shift_date = _dt_mod.date.fromisoformat(shift['date'])
        wd_names = ['ПН', 'ВТ', 'СР', 'ЧТ', 'ПТ', 'СБ', 'ВС']
        ws.title = wd_names[shift_date.weekday()]

        def w(row, col, val):
            ws.cell(row=row, column=col, value=val)

        # ─── Revenue (cells match what _xl_process_sheet reads) ────────────
        w(2, 6, _rv('total_revenue'))            # F2
        w(3, 2, shift_date)                       # B3 = date
        w(3, 4, _rv('morning_cash', 0))           # D3 = утренняя касса
        w(4, 7, _rv('delivery_revenue'))          # G4
        w(5, 4, _rv('cash_amount'))               # D5
        w(5, 7, int(_rv('delivery_orders')))      # G5
        w(6, 4, _rv('card_amount'))               # D6
        w(6, 7, _rv('pickup_revenue'))            # G6
        w(7, 4, _rv('online_amount'))             # D7
        w(7, 7, int(_rv('pickup_orders')))        # G7
        w(31, 4, _rv('change_amount', 0))         # D31 = размен
        w(33, 4, _rv('plus_amount', 0))           # D33 = плюс в кассу

        # ─── Couriers (rows 3-8, cols K-V) ─────────────────────────────────
        for i, c in enumerate(couriers[:6]):
            r = 3 + i
            km      = c['km'] or 0
            orders  = c['orders'] or 0
            hours   = c['hours_worked'] or 0
            km_rate = c['rate_per_km_snapshot'] or 10
            or_rate = c['rate_per_order_snapshot'] or 100
            hr_rate = c['rate_snapshot'] or 0
            w(r, 11, c['full_name_snapshot'])          # K
            w(r, 13, km)                               # M = km
            w(r, 14, round(km * km_rate, 2))           # N = km pay
            w(r, 15, hours)                            # O = hours
            w(r, 16, round(hours * hr_rate, 2))        # P = hrs pay
            w(r, 17, orders)                           # Q = orders
            w(r, 18, round(orders * or_rate, 2))       # R = ord pay
            w(r, 19, c['bonus_comment'] or '')         # S = comment
            w(r, 21, 'Да' if c['is_paid'] else '')    # U = paid
            w(r, 22, c['total_amount'] or 0)           # V = total

        # ─── Expense categories (rows 10-20, cols B,C,F,G) ─────────────────
        # Write labels for known categories
        for code, row in _CAT_ROW.items():
            lbl = _CAT_REVERSE.get(code, '')
            if lbl:
                w(row, 2, lbl)
        w(20, 2, 'Другое')

        other_cash, other_card, other_descs = 0.0, 0.0, []
        for cat_code, data in exp_by_cat.items():
            cat_row = _CAT_ROW.get(cat_code)
            if cat_row:
                w(cat_row, 6, round(data['cash'], 2))    # F = нал
                w(cat_row, 7, round(data['card'], 2))    # G = безнал
                if data['descs']:
                    w(cat_row, 3, '; '.join(data['descs'][:3]))  # C = описание
            else:
                other_cash += data['cash']
                other_card += data['card']
                other_descs.extend(data['descs'])
        if other_cash or other_card:
            w(20, 6, round(other_cash, 2))
            w(20, 7, round(other_card, 2))
            if other_descs:
                w(20, 3, '; '.join(other_descs[:5]))

        # ─── Admin/Sushi/Cleaner (rows 10-22, cols K-V) ────────────────────
        # Rows 10-14 → admin role (row_idx 9-13 in importer)
        # Rows 15-21 → sushi role (row_idx 14-20)
        # Row 22     → cleaner (name must be 'Уборщица' for importer)
        admins   = [s for s in non_couriers if s['role_snapshot'] in ('admin', 'cook', 'packer')]
        sushis   = [s for s in non_couriers if s['role_snapshot'] == 'sushi']
        cleaners = [s for s in non_couriers if s['role_snapshot'] == 'cleaner']

        def _write_staff(r, s):
            bonus_val = (s['bonus_amount'] or 0) - (s['penalty_amount'] or 0)
            w(r, 11, s['full_name_snapshot'])
            w(r, 13, s['rate_snapshot'] or 0)        # M = ставка
            if s['shift_start']:
                w(r, 14, s['shift_start'])            # N = начало
            if s['shift_end']:
                w(r, 15, s['shift_end'])              # O = конец
            w(r, 16, s['hours_worked'] or 0)          # P = часы
            if bonus_val:
                w(r, 18, bonus_val)                   # R = премия/штраф
            if s['bonus_comment']:
                w(r, 19, s['bonus_comment'])          # S = комментарий
            w(r, 21, 'Да' if s['is_paid'] else '')   # U = выплачено
            w(r, 22, s['total_amount'] or 0)          # V = итого

        for i, s in enumerate(admins[:5]):
            _write_staff(10 + i, s)

        for i, s in enumerate(sushis[:7]):
            _write_staff(15 + i, s)

        # Cleaner: write at row 22; importer identifies by name 'Уборщица'
        if cleaners:
            cl = cleaners[0]
            total_cl = sum(s['total_amount'] or 0 for s in cleaners)
            w(22, 11, 'Уборщица')
            w(22, 13, cl['rate_snapshot'] or 0)
            w(22, 16, sum(s['hours_worked'] or 0 for s in cleaners))
            w(22, 21, 'Да' if cl['is_paid'] else '')
            w(22, 22, total_cl)

        # ─── Taxi section (rows 22-24) ──────────────────────────────────────
        # Row 22 col B = 'ТАКСИ' skips it as taxi data; actual trips in 23-24
        w(22, 2, 'ТАКСИ')
        for i, t in enumerate(taxi_trips[:2]):
            r = 23 + i
            cash = round(t['amount'] or 0, 2) if (t['payment_type'] or 'cash') == 'cash' else 0
            card = round(t['amount'] or 0, 2) if (t['payment_type'] or 'cash') != 'cash' else 0
            w(r, 3, t['note'] or '')   # C = описание
            w(r, 6, card)              # F = безнал (в импорте F=card, G=cash — инверсия!)
            w(r, 7, cash)              # G = нал

        # ─── Terminals (rows 26, 29-32) ─────────────────────────────────────
        if terminals:
            w(26, 5, 'По терминалам:')
            w(26, 7, round(sum(t['amount'] or 0 for t in terminals), 2))
            for i, t in enumerate(terminals[:4]):
                w(29 + i, 5, t['terminal_number'])        # E = номер
                w(29 + i, 7, round(t['amount'] or 0, 2)) # G = сумма

        # ─── Actual cash, who closed ────────────────────────────────────────
        actual_cash = _rv('actual_cash', None)
        if actual_cash is not None:
            w(27, 2, 'Факт в кассе:')
            w(27, 4, round(actual_cash, 2))

        closed_by = shift['closed_by_name'] if 'closed_by_name' in shift.keys() else None
        if closed_by:
            w(28, 2, 'Смену закрыл(а):')
            w(28, 5, closed_by)

        # ─── Save & upload to Google Drive ──────────────────────────────────
        buf = _io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        xlsx_bytes = buf.getvalue()

        from google.oauth2.service_account import Credentials
        import google.auth.transport.requests as _gatr

        creds_dict = _json_lib.loads(creds_json)
        creds = Credentials.from_service_account_info(
            creds_dict, scopes=['https://www.googleapis.com/auth/drive']
        )
        creds.refresh(_gatr.Request())
        token = creds.token

        branch_nm = shift['branch_name'].replace(' ', '_')
        filename = f'{branch_nm}_{shift["date"]}_{wd_names[shift_date.weekday()]}.xlsx'
        xlsx_mime = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        boundary = '---GDriveXlsxBnd9347'
        meta_json = _json_lib.dumps({'name': filename, 'parents': [folder_id]})
        body = (
            f'--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n'
            f'{meta_json}\r\n'
            f'--{boundary}\r\nContent-Type: {xlsx_mime}\r\n\r\n'
        ).encode('utf-8') + xlsx_bytes + f'\r\n--{boundary}--'.encode('utf-8')

        upload_req = urllib.request.Request(
            'https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart',
            data=body,
            headers={
                'Authorization': f'Bearer {token}',
                'Content-Type': f'multipart/related; boundary="{boundary}"',
            },
            method='POST'
        )
        with urllib.request.urlopen(upload_req, timeout=60) as resp:
            result = _json_lib.loads(resp.read())
        print(f'[GDrive] shift {shift_id} xlsx uploaded: {filename} id={result.get("id","?")}')

    except Exception as e:
        print(f'[GDrive] shift {shift_id} xlsx error: {e}')


@app.route('/shift/<int:shift_id>/close', methods=['POST'])
@login_required
def close_shift(shift_id):
    if not _can_edit_shift(shift_id):
        flash('Нет доступа', 'danger')
        return redirect(url_for('shift_view', shift_id=shift_id))
    try:
        comment = request.form.get('comment', '')
        closed_by_name = request.form.get('closed_by_name', session.get('full_name', ''))
        actual_cash_comment = request.form.get('actual_cash_comment', '').strip()
        if not closed_by_name or not closed_by_name.strip():
            flash('Укажите, кто закрыл смену', 'danger')
            return redirect(url_for('shift_view', shift_id=shift_id))
        with get_db() as conn:
            rows_updated = conn.execute('''
                UPDATE shifts SET status='closed', closed_by=?, closed_at=CURRENT_TIMESTAMP,
                comment=?, closed_by_name=?
                WHERE id=? AND status='open'
            ''', (session['user_id'], comment, closed_by_name, shift_id)).rowcount
            if rows_updated == 0:
                flash('Смена не найдена или уже закрыта', 'danger')
                conn.rollback()
                return redirect(url_for('shift_view', shift_id=shift_id))
            if actual_cash_comment:
                conn.execute(
                    'UPDATE shift_revenue SET actual_cash_comment=? WHERE shift_id=?',
                    (actual_cash_comment, shift_id)
                )
            log_action(conn, 'shift_close', 'Смена закрыта', shift_id=shift_id)
            conn.commit()
        flash('Смена закрыта', 'success')
        threading.Thread(target=_export_shift_to_gsheet, args=(shift_id,), daemon=True).start()
        threading.Thread(target=_export_shift_to_gdrive_xlsx, args=(shift_id,), daemon=True).start()
    except Exception as e:
        flash(f'Ошибка при закрытии смены: {e}', 'danger')
    return redirect(url_for('shift_view', shift_id=shift_id))


@app.route('/shift/<int:shift_id>/reopen', methods=['POST'])
@login_required
@owner_required
def reopen_shift(shift_id):
    with get_db() as conn:
        conn.execute(
            "UPDATE shifts SET status='open', closed_by=NULL, closed_at=NULL WHERE id=?",
            (shift_id,)
        )
        log_action(conn, 'shift_reopen', 'Смена переоткрыта', shift_id=shift_id)
        conn.commit()
    flash('Смена переоткрыта', 'success')
    return redirect(url_for('shift_view', shift_id=shift_id))


@app.route('/shift/<int:shift_id>/delete', methods=['POST'])
@login_required
@owner_required
def delete_shift(shift_id):
    with get_db() as conn:
        shift = conn.execute('SELECT * FROM shifts WHERE id=?', (shift_id,)).fetchone()
        if not shift:
            flash('Смена не найдена', 'danger')
            return redirect(url_for('dashboard'))
        branch_date = f'{shift["date"]} · {shift["branch_id"]}'
        conn.execute(
            'DELETE FROM salary_payments WHERE employee_shift_id IN (SELECT id FROM employee_shifts WHERE shift_id=?)',
            (shift_id,)
        )
        conn.execute('DELETE FROM employee_shifts WHERE shift_id=?', (shift_id,))
        conn.execute('DELETE FROM expenses WHERE shift_id=?', (shift_id,))
        conn.execute(
            'DELETE FROM taxi_trip_employees WHERE trip_id IN (SELECT id FROM taxi_trips WHERE shift_id=?)',
            (shift_id,)
        )
        conn.execute('DELETE FROM taxi_trips WHERE shift_id=?', (shift_id,))
        conn.execute('DELETE FROM shift_revenue WHERE shift_id=?', (shift_id,))
        conn.execute('DELETE FROM change_log WHERE shift_id=?', (shift_id,))
        conn.execute('DELETE FROM shifts WHERE id=?', (shift_id,))
        conn.commit()
    flash(f'Смена #{shift_id} удалена', 'success')
    return redirect(url_for('shifts_archive'))


# ─── EMPLOYEES ────────────────────────────────────────────────────────────────

@app.route('/employees')
@login_required
def employees():
    role = session.get('role')
    selected_branches = [bid for bid in request.args.getlist('branch_ids') if bid.isdigit()] if role == 'owner' else []
    with get_db() as conn:
        if role == 'owner':
            all_branches = conn.execute('SELECT * FROM branches WHERE is_active=1 ORDER BY name').fetchall()
            if selected_branches:
                ids_str = ','.join(str(int(b)) for b in selected_branches)
                emps = conn.execute(f'''
                    SELECT e.*, b.name as branch_name
                    FROM employees e LEFT JOIN branches b ON b.id=e.branch_id
                    WHERE e.branch_id IN ({ids_str}) AND COALESCE(e.is_fired,0)=0
                    ORDER BY b.name, e.role, e.full_name
                ''').fetchall()
                fired_emps = conn.execute(f'''
                    SELECT e.*, b.name as branch_name
                    FROM employees e LEFT JOIN branches b ON b.id=e.branch_id
                    WHERE e.branch_id IN ({ids_str}) AND e.is_fired=1
                    ORDER BY e.fired_at DESC, e.full_name
                ''').fetchall()
                branches = [b for b in all_branches if str(b['id']) in selected_branches]
            else:
                emps = conn.execute('''
                    SELECT e.*, b.name as branch_name
                    FROM employees e LEFT JOIN branches b ON b.id=e.branch_id
                    WHERE COALESCE(e.is_fired,0)=0
                    ORDER BY b.name, e.role, e.full_name
                ''').fetchall()
                fired_emps = conn.execute('''
                    SELECT e.*, b.name as branch_name
                    FROM employees e LEFT JOIN branches b ON b.id=e.branch_id
                    WHERE e.is_fired=1
                    ORDER BY e.fired_at DESC, e.full_name
                ''').fetchall()
                branches = all_branches
        else:
            all_branches = []
            fired_emps = []
            bids = _session_branch_ids()
            if bids:
                ids_str = ','.join(str(int(b)) for b in bids)
                emps = conn.execute(f'''
                    SELECT e.*, b.name as branch_name FROM employees e
                    LEFT JOIN branches b ON b.id=e.branch_id
                    WHERE e.branch_id IN ({ids_str}) AND COALESCE(e.is_fired,0)=0
                    ORDER BY b.name, e.role, e.full_name
                ''').fetchall()
                branches = conn.execute(f'SELECT * FROM branches WHERE id IN ({ids_str}) ORDER BY name').fetchall()
            else:
                emps = []
                branches = []

        # Rate history per employee
        rate_history = {}
        address_history = {}
        all_emps_for_hist = list(emps) + list(fired_emps)
        for emp in all_emps_for_hist:
            hist = conn.execute('''
                SELECT * FROM employee_rate_history WHERE employee_id=?
                ORDER BY effective_from DESC LIMIT 5
            ''', (emp['id'],)).fetchall()
            rate_history[emp['id']] = hist
            addr_hist = conn.execute('''
                SELECT * FROM employee_address_history WHERE employee_id=?
                ORDER BY valid_from DESC LIMIT 5
            ''', (emp['id'],)).fetchall()
            address_history[emp['id']] = addr_hist

        emp_branches_map = {}
        for row in conn.execute('''
            SELECT eb.employee_id, eb.branch_id
            FROM employee_branches eb
        ''').fetchall():
            emp_branches_map.setdefault(row['employee_id'], []).append(row['branch_id'])

        # Extra roles per employee
        emp_roles_map = {}
        for row in conn.execute('SELECT * FROM employee_roles WHERE is_active=1 ORDER BY role').fetchall():
            emp_roles_map.setdefault(row['employee_id'], []).append(dict(row))

        shift_counts = {}
        all_emp_ids = [e['id'] for e in all_emps_for_hist]
        if all_emp_ids:
            ids_str = ','.join(str(i) for i in all_emp_ids)
            for row in conn.execute(f'''
                SELECT employee_id, COUNT(*) as cnt FROM employee_shifts
                WHERE employee_id IN ({ids_str}) GROUP BY employee_id
            ''').fetchall():
                shift_counts[row['employee_id']] = row['cnt']

        all_tmpls = conn.execute(
            'SELECT * FROM rate_templates WHERE is_active=1 ORDER BY role, name'
        ).fetchall()
        tmpl_branch_sets = {}
        for row in conn.execute('SELECT template_id, branch_id FROM rate_template_branches').fetchall():
            tmpl_branch_sets.setdefault(row['template_id'], set()).add(row['branch_id'])

    def tmpls_for_emp(emp):
        bid = emp['branch_id']
        result = []
        for t in all_tmpls:
            branches_set = tmpl_branch_sets.get(t['id'], set())
            if not branches_set or bid in branches_set:
                result.append(t)
        return result

    return render_template('employees.html', employees=emps, fired_employees=fired_emps,
                           branches=branches,
                           all_branches=all_branches,
                           selected_branches=selected_branches,
                           emp_branches_map=emp_branches_map,
                           emp_roles_map=emp_roles_map,
        role_labels=ROLE_LABELS, is_owner=(role == 'owner'),
                           rate_history=rate_history, address_history=address_history,
                           rate_templates=all_tmpls,
                           tmpl_branch_sets=tmpl_branch_sets,
                           tmpls_for_emp=tmpls_for_emp,
                           shift_counts=shift_counts,
                           today=date.today().isoformat(),
                           branch_groups=get_branch_groups(conn))


@app.route('/employees/add', methods=['POST'])
@login_required
def add_employee():
    role = session.get('role')
    if role == 'owner':
        branch_ids_form = [bid for bid in request.form.getlist('branch_ids') if bid.isdigit()]
        branch_id = int(branch_ids_form[0]) if branch_ids_form else None
    else:
        branch_ids_form = [str(b) for b in _session_branch_ids()]
        branch_id = session.get('branch_id')
    last_name  = request.form.get('last_name', '').strip()
    first_name = request.form.get('first_name', '').strip()
    full_name  = (last_name + (' ' + first_name if first_name else '')).strip()
    emp_role = request.form.get('role', 'sushi')
    rate = float(request.form.get('rate', 0) or 0)
    rate_km = float(request.form.get('rate_per_km', 10) or 10)
    rate_ord = float(request.form.get('rate_per_order', 100) or 100)
    effective_from = request.form.get('effective_from') or date.today().isoformat()
    pay_monthly = 1 if request.form.get('pay_monthly') else 0
    if not full_name:
        flash('Введите фамилию сотрудника', 'danger')
        return redirect(url_for('employees'))
    with get_db() as conn:
        conn.execute(
            'INSERT INTO employees (branch_id, full_name, last_name, first_name, role, rate, rate_per_km, rate_per_order, pay_monthly) VALUES (?,?,?,?,?,?,?,?,?)',
            (branch_id, full_name, last_name, first_name, emp_role, rate, rate_km, rate_ord, pay_monthly)
        )
        emp_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.execute(
            'INSERT INTO employee_rate_history (employee_id, rate, rate_per_km, rate_per_order, effective_from) VALUES (?,?,?,?,?)',
            (emp_id, rate, rate_km, rate_ord, effective_from)
        )
        for bid in branch_ids_form:
            conn.execute('INSERT OR IGNORE INTO employee_branches (employee_id, branch_id) VALUES (?,?)', (emp_id, int(bid)))
        address = request.form.get('address', '').strip()
        if address:
            conn.execute(
                'INSERT INTO employee_address_history (employee_id, address, valid_from) VALUES (?,?,?)',
                (emp_id, address, effective_from)
            )
        conn.commit()
    flash(f'Сотрудник {full_name} добавлен', 'success')
    return redirect(url_for('employees'))


@app.route('/employees/<int:emp_id>/update-rate', methods=['POST'])
@login_required
def update_employee_rate(emp_id):
    rate = float(request.form.get('rate', 0) or 0)
    rate_km = float(request.form.get('rate_per_km', 10) or 10)
    rate_ord = float(request.form.get('rate_per_order', 100) or 100)
    effective_from = request.form.get('effective_from') or date.today().isoformat()
    with get_db() as conn:
        emp = conn.execute('SELECT * FROM employees WHERE id=?', (emp_id,)).fetchone()
        if not emp:
            flash('Сотрудник не найден', 'danger')
            return redirect(url_for('employees'))
        conn.execute(
            'INSERT INTO employee_rate_history (employee_id, rate, rate_per_km, rate_per_order, effective_from) VALUES (?,?,?,?,?)',
            (emp_id, rate, rate_km, rate_ord, effective_from)
        )
        # Update current rate only if effective_from <= today
        if effective_from <= date.today().isoformat():
            conn.execute(
                'UPDATE employees SET rate=?, rate_per_km=?, rate_per_order=? WHERE id=?',
                (rate, rate_km, rate_ord, emp_id)
            )
        conn.commit()
    flash(f'Ставка сохранена (с {effective_from})', 'success')
    return redirect(url_for('employees'))


@app.route('/employees/<int:emp_id>/toggle', methods=['POST'])
@login_required
def toggle_employee(emp_id):
    with get_db() as conn:
        conn.execute('UPDATE employees SET is_active = 1-is_active WHERE id=?', (emp_id,))
        conn.commit()
    return redirect(url_for('employees'))


@app.route('/employees/<int:emp_id>/fire', methods=['POST'])
@login_required
@owner_required
def fire_employee(emp_id):
    comment = request.form.get('comment', '').strip()
    with get_db() as conn:
        conn.execute(
            'UPDATE employees SET is_fired=1, fired_at=CURRENT_TIMESTAMP, fired_comment=?, is_active=0 WHERE id=?',
            (comment, emp_id)
        )
        conn.commit()
    flash('Сотрудник уволен', 'warning')
    return redirect(url_for('employees'))


@app.route('/employees/<int:emp_id>/restore', methods=['POST'])
@login_required
@owner_required
def restore_employee(emp_id):
    with get_db() as conn:
        conn.execute(
            'UPDATE employees SET is_fired=0, fired_at=NULL, fired_comment=NULL, is_active=1 WHERE id=?',
            (emp_id,)
        )
        conn.commit()
    flash('Сотрудник восстановлен', 'success')
    return redirect(url_for('employees'))


@app.route('/employees/<int:emp_id>/address', methods=['POST'])
@login_required
def update_employee_address(emp_id):
    address = request.form.get('address', '').strip()
    valid_from = request.form.get('valid_from') or date.today().isoformat()
    if not address:
        flash('Введите адрес', 'danger')
        return redirect(url_for('employees'))
    with get_db() as conn:
        conn.execute(
            'INSERT INTO employee_address_history (employee_id, address, valid_from) VALUES (?,?,?)',
            (emp_id, address, valid_from)
        )
        conn.commit()
    flash('Адрес сохранён', 'success')
    return redirect(url_for('employees'))


@app.route('/employees/<int:emp_id>/edit', methods=['POST'])
@login_required
def edit_employee(emp_id):
    last_name  = request.form.get('last_name', '').strip()
    first_name = request.form.get('first_name', '').strip()
    full_name  = (last_name + (' ' + first_name if first_name else '')).strip()
    address    = request.form.get('address', '').strip()
    address_from = request.form.get('address_from') or date.today().isoformat()
    rate      = float(request.form.get('rate', 0) or 0)
    rate_km   = float(request.form.get('rate_per_km', 10) or 10)
    rate_ord  = float(request.form.get('rate_per_order', 100) or 100)
    rate_from = request.form.get('rate_from') or date.today().isoformat()
    pay_monthly = 1 if request.form.get('pay_monthly') else 0
    with get_db() as conn:
        emp = conn.execute('SELECT * FROM employees WHERE id=?', (emp_id,)).fetchone()
        if not emp:
            flash('Сотрудник не найден', 'danger')
            return redirect(url_for('employees'))
        if full_name:
            conn.execute(
                'UPDATE employees SET full_name=?, last_name=?, first_name=?, pay_monthly=? WHERE id=?',
                (full_name, last_name, first_name, pay_monthly, emp_id)
            )
        if session.get('role') == 'owner':
            branch_ids_form = [bid for bid in request.form.getlist('branch_ids') if bid.isdigit()]
            if branch_ids_form:
                conn.execute('UPDATE employees SET branch_id=? WHERE id=?', (int(branch_ids_form[0]), emp_id))
                conn.execute('DELETE FROM employee_branches WHERE employee_id=?', (emp_id,))
                for bid in branch_ids_form:
                    conn.execute('INSERT OR IGNORE INTO employee_branches (employee_id, branch_id) VALUES (?,?)', (emp_id, int(bid)))
        # Rate: save to history; update current values only if rate_from <= today
        conn.execute(
            'INSERT INTO employee_rate_history (employee_id, rate, rate_per_km, rate_per_order, effective_from) VALUES (?,?,?,?,?)',
            (emp_id, rate, rate_km, rate_ord, rate_from)
        )
        if rate_from <= date.today().isoformat():
            conn.execute(
                'UPDATE employees SET rate=?, rate_per_km=?, rate_per_order=? WHERE id=?',
                (rate, rate_km, rate_ord, emp_id)
            )
        if address:
            conn.execute(
                'INSERT INTO employee_address_history (employee_id, address, valid_from) VALUES (?,?,?)',
                (emp_id, address, address_from)
            )
        conn.commit()
    flash('Данные сотрудника сохранены', 'success')
    return redirect(url_for('employees'))


@app.route('/employees/<int:emp_id>/delete', methods=['POST'])
@login_required
def delete_employee(emp_id):
    if session.get('role') != 'owner':
        flash('Только владелец может удалять сотрудников', 'danger')
        return redirect(url_for('employees'))
    with get_db() as conn:
        emp = conn.execute('SELECT * FROM employees WHERE id=?', (emp_id,)).fetchone()
        if not emp:
            flash('Сотрудник не найден', 'danger')
            return redirect(url_for('employees'))
        shift_count = conn.execute(
            'SELECT COUNT(*) FROM employee_shifts WHERE employee_id=?', (emp_id,)
        ).fetchone()[0]
        if shift_count > 0:
            flash(
                f'Нельзя удалить «{emp["full_name"]}» — есть {shift_count} записей в сменах. Деактивируйте сотрудника.',
                'danger'
            )
            return redirect(url_for('employees'))
        conn.execute('UPDATE taxi_trip_employees SET employee_id=NULL WHERE employee_id=?', (emp_id,))
        conn.execute('DELETE FROM employee_rate_history WHERE employee_id=?', (emp_id,))
        conn.execute('DELETE FROM employee_address_history WHERE employee_id=?', (emp_id,))
        conn.execute('DELETE FROM employee_branches WHERE employee_id=?', (emp_id,))
        conn.execute('DELETE FROM employees WHERE id=?', (emp_id,))
        conn.commit()
    flash(f'Сотрудник «{emp["full_name"]}» удалён', 'success')
    return redirect(url_for('employees'))


@app.route('/employees/<int:emp_id>/reassign-delete', methods=['POST'])
@login_required
def reassign_and_delete_employee(emp_id):
    if session.get('role') != 'owner':
        flash('Только владелец может выполнять это действие', 'danger')
        return redirect(url_for('employees'))
    target_id = request.form.get('target_id', type=int)
    if not target_id or target_id == emp_id:
        flash('Выберите другого сотрудника для переназначения', 'danger')
        return redirect(url_for('employees'))
    with get_db() as conn:
        emp = conn.execute('SELECT * FROM employees WHERE id=?', (emp_id,)).fetchone()
        target = conn.execute('SELECT * FROM employees WHERE id=?', (target_id,)).fetchone()
        if not emp or not target:
            flash('Сотрудник не найден', 'danger')
            return redirect(url_for('employees'))
        shift_count = conn.execute(
            'SELECT COUNT(*) FROM employee_shifts WHERE employee_id=?', (emp_id,)
        ).fetchone()[0]
        conn.execute(
            'UPDATE employee_shifts SET employee_id=?, full_name_snapshot=? WHERE employee_id=?',
            (target_id, target['full_name'], emp_id)
        )
        conn.execute('UPDATE salary_payments SET employee_id=? WHERE employee_id=?', (target_id, emp_id))
        conn.execute('UPDATE taxi_trip_employees SET employee_id=? WHERE employee_id=?', (target_id, emp_id))
        conn.execute('DELETE FROM employee_rate_history WHERE employee_id=?', (emp_id,))
        conn.execute('DELETE FROM employee_address_history WHERE employee_id=?', (emp_id,))
        conn.execute('DELETE FROM employee_branches WHERE employee_id=?', (emp_id,))
        conn.execute('DELETE FROM employees WHERE id=?', (emp_id,))
        conn.commit()
    flash(
        f'Переназначено {shift_count} смен: «{emp["full_name"]}» → «{target["full_name"]}». Дубликат удалён.',
        'success'
    )
    return redirect(url_for('employees'))


# ─── TAXI ─────────────────────────────────────────────────────────────────────

@app.route('/shift/<int:shift_id>/taxi/add', methods=['POST'])
@login_required
def add_taxi_trip(shift_id):
    if not _can_edit_shift(shift_id):
        return jsonify({'error': 'Нет доступа'}), 403
    data = request.json or {}
    amount = float(data.get('amount', 0) or 0)
    payment_type = data.get('payment_type', 'cash')
    in_gulyash = 1 if data.get('in_gulyash') else 0
    note = data.get('note', '') or ''
    emps = data.get('employees', [])
    with get_db() as conn:
        conn.execute(
            'INSERT INTO taxi_trips (shift_id, amount, payment_type, in_gulyash, note) VALUES (?,?,?,?,?)',
            (shift_id, amount, payment_type, in_gulyash, note)
        )
        trip_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        for emp in emps:
            conn.execute(
                'INSERT INTO taxi_trip_employees (trip_id, employee_id, name_snapshot, address_snapshot) VALUES (?,?,?,?)',
                (trip_id, emp.get('id') or None, emp.get('name', ''), emp.get('address', ''))
            )
        conn.commit()
    return jsonify({'ok': True, 'trip_id': trip_id})


@app.route('/taxi/<int:trip_id>/delete', methods=['POST'])
@login_required
def delete_taxi_trip(trip_id):
    with get_db() as conn:
        trip = conn.execute('SELECT * FROM taxi_trips WHERE id=?', (trip_id,)).fetchone()
        if not trip:
            return jsonify({'error': 'Not found'}), 404
        if not _can_edit_shift(trip['shift_id']):
            return jsonify({'error': 'Нет доступа'}), 403
        conn.execute('DELETE FROM taxi_trip_employees WHERE trip_id=?', (trip_id,))
        conn.execute('DELETE FROM taxi_trips WHERE id=?', (trip_id,))
        conn.commit()
    return jsonify({'ok': True})


@app.route('/taxi/<int:trip_id>/toggle-gulyash', methods=['POST'])
@login_required
def toggle_taxi_gulyash(trip_id):
    with get_db() as conn:
        trip = conn.execute('SELECT * FROM taxi_trips WHERE id=?', (trip_id,)).fetchone()
        if not trip:
            return jsonify({'error': 'Not found'}), 404
        if not _can_edit_shift(trip['shift_id']):
            return jsonify({'error': 'Нет доступа'}), 403
        new_val = 0 if trip['in_gulyash'] else 1
        conn.execute('UPDATE taxi_trips SET in_gulyash=? WHERE id=?', (new_val, trip_id))
        conn.commit()
    return jsonify({'ok': True, 'in_gulyash': new_val})


@app.route('/shift/<int:shift_id>/taxi/create', methods=['POST'])
@login_required
def create_taxi_trip(shift_id):
    if not _can_edit_shift(shift_id):
        return jsonify({'error': 'Нет доступа'}), 403
    with get_db() as conn:
        conn.execute('INSERT INTO taxi_trips (shift_id) VALUES (?)', (shift_id,))
        trip_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.commit()
    return jsonify({'ok': True, 'trip_id': trip_id})


@app.route('/taxi/<int:trip_id>/employee/add', methods=['POST'])
@login_required
def add_taxi_employee(trip_id):
    with get_db() as conn:
        trip = conn.execute('SELECT * FROM taxi_trips WHERE id=?', (trip_id,)).fetchone()
        if not trip or not _can_edit_shift(trip['shift_id']):
            return jsonify({'error': 'Нет доступа'}), 403
        data = request.json or {}
        employee_id = data.get('employee_id') or None
        if employee_id:
            already = conn.execute('''
                SELECT COUNT(*) FROM taxi_trip_employees tte
                JOIN taxi_trips tt ON tt.id = tte.trip_id
                WHERE tt.shift_id = ? AND tte.employee_id = ?
            ''', (trip['shift_id'], employee_id)).fetchone()[0]
            if already:
                return jsonify({'error': 'Сотрудник уже добавлен в рейс этой смены', 'duplicate': True}), 400
        conn.execute(
            'INSERT INTO taxi_trip_employees (trip_id, employee_id, name_snapshot, address_snapshot) VALUES (?,?,?,?)',
            (trip_id, employee_id, data.get('name', ''), data.get('address', ''))
        )
        tte_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.commit()
    return jsonify({'ok': True, 'tte_id': tte_id})


@app.route('/taxi/employee/<int:tte_id>/remove', methods=['POST'])
@login_required
def remove_taxi_employee(tte_id):
    with get_db() as conn:
        tte = conn.execute('SELECT * FROM taxi_trip_employees WHERE id=?', (tte_id,)).fetchone()
        if not tte:
            return jsonify({'error': 'Not found'}), 404
        trip = conn.execute('SELECT * FROM taxi_trips WHERE id=?', (tte['trip_id'],)).fetchone()
        if not _can_edit_shift(trip['shift_id']):
            return jsonify({'error': 'Нет доступа'}), 403
        conn.execute('DELETE FROM taxi_trip_employees WHERE id=?', (tte_id,))
        conn.commit()
    return jsonify({'ok': True})


@app.route('/taxi/<int:trip_id>/update', methods=['POST'])
@login_required
def update_taxi_trip(trip_id):
    with get_db() as conn:
        trip = conn.execute('SELECT * FROM taxi_trips WHERE id=?', (trip_id,)).fetchone()
        if not trip or not _can_edit_shift(trip['shift_id']):
            return jsonify({'error': 'Нет доступа'}), 403
        data = request.json or {}
        fields, vals = [], []
        if 'amount' in data:
            fields.append('amount=?'); vals.append(float(data['amount'] or 0))
        if 'payment_type' in data:
            fields.append('payment_type=?'); vals.append(data['payment_type'])
        if 'in_gulyash' in data:
            fields.append('in_gulyash=?'); vals.append(1 if data['in_gulyash'] else 0)
        if fields:
            vals.append(trip_id)
            conn.execute('UPDATE taxi_trips SET ' + ', '.join(fields) + ' WHERE id=?', vals)
            conn.commit()
    return jsonify({'ok': True})


@app.route('/taxi/employee/<int:tte_id>/update', methods=['POST'])
@login_required
def update_taxi_employee(tte_id):
    with get_db() as conn:
        tte = conn.execute('SELECT * FROM taxi_trip_employees WHERE id=?', (tte_id,)).fetchone()
        if not tte:
            return jsonify({'error': 'Not found'}), 404
        trip = conn.execute('SELECT * FROM taxi_trips WHERE id=?', (tte['trip_id'],)).fetchone()
        if not _can_edit_shift(trip['shift_id']):
            return jsonify({'error': 'Нет доступа'}), 403
        data = request.json or {}
        conn.execute('UPDATE taxi_trip_employees SET address_snapshot=? WHERE id=?',
                     (data.get('address', ''), tte_id))
        conn.commit()
    return jsonify({'ok': True})


# ─── BRANCHES ─────────────────────────────────────────────────────────────────

@app.route('/branches')
@login_required
@owner_required
def branches():
    with get_db() as conn:
        blist = conn.execute('SELECT * FROM branches ORDER BY name').fetchall()
        groups = get_branch_groups(conn)
        cards_rows = conn.execute('SELECT * FROM branch_cards ORDER BY branch_id, id').fetchall()
    cards_by_branch = {}
    for c in cards_rows:
        cards_by_branch.setdefault(c['branch_id'], []).append(dict(c))
    return render_template('branches.html', branches=blist, my_ip=get_client_ip(),
                           branch_groups=groups, cards_by_branch=cards_by_branch)


@app.route('/branches/groups/add', methods=['POST'])
@login_required
@owner_required
def add_branch_group():
    name = request.form.get('name', '').strip()
    abbr = request.form.get('abbr', '').strip().upper()[:3]
    branch_ids = [b for b in request.form.getlist('branch_ids') if b.isdigit()]
    if not name:
        flash('Введите название группы', 'danger')
        return redirect(url_for('branches'))
    with get_db() as conn:
        sort_order = conn.execute('SELECT COUNT(*) FROM branch_groups').fetchone()[0] * 10
        conn.execute('INSERT INTO branch_groups (name, abbr, sort_order) VALUES (?,?,?)', (name, abbr, sort_order))
        group_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        for bid in branch_ids:
            conn.execute(
                'INSERT OR IGNORE INTO branch_group_members (group_id, branch_id) VALUES (?,?)',
                (group_id, int(bid))
            )
        conn.commit()
    flash(f'Группа «{name}» создана', 'success')
    return redirect(url_for('branches'))


@app.route('/branches/groups/<int:group_id>/edit', methods=['POST'])
@login_required
@owner_required
def edit_branch_group(group_id):
    name = request.form.get('name', '').strip()
    abbr = request.form.get('abbr', '').strip().upper()[:3]
    branch_ids = [b for b in request.form.getlist('branch_ids') if b.isdigit()]
    if not name:
        flash('Введите название группы', 'danger')
        return redirect(url_for('branches'))
    with get_db() as conn:
        conn.execute('UPDATE branch_groups SET name=?, abbr=? WHERE id=?', (name, abbr, group_id))
        conn.execute('DELETE FROM branch_group_members WHERE group_id=?', (group_id,))
        for bid in branch_ids:
            conn.execute(
                'INSERT OR IGNORE INTO branch_group_members (group_id, branch_id) VALUES (?,?)',
                (group_id, int(bid))
            )
        conn.commit()
    flash(f'Группа «{name}» обновлена', 'success')
    return redirect(url_for('branches'))


@app.route('/branches/groups/<int:group_id>/delete', methods=['POST'])
@login_required
@owner_required
def delete_branch_group(group_id):
    with get_db() as conn:
        g = conn.execute('SELECT name FROM branch_groups WHERE id=?', (group_id,)).fetchone()
        if g:
            conn.execute('DELETE FROM branch_groups WHERE id=?', (group_id,))
            conn.commit()
            flash(f'Группа «{g["name"]}» удалена', 'success')
    return redirect(url_for('branches'))


@app.route('/branches/add', methods=['POST'])
@login_required
@owner_required
def add_branch():
    name = request.form.get('name', '').strip().upper()
    if not name:
        flash('Введите название филиала', 'danger')
        return redirect(url_for('branches'))
    allowed_ip = request.form.get('allowed_ip', '').strip() or None
    abbr = request.form.get('abbr', '').strip().upper()[:3]
    with get_db() as conn:
        conn.execute('INSERT INTO branches (name, allowed_ip, abbr) VALUES (?,?,?)', (name, allowed_ip, abbr))
        conn.commit()
    flash(f'Филиал {name} добавлен', 'success')
    return redirect(url_for('branches'))


@app.route('/branches/<int:branch_id>/edit', methods=['POST'])
@login_required
@owner_required
def edit_branch(branch_id):
    allowed_ip = request.form.get('allowed_ip', '').strip() or None
    merchant_numbers = request.form.get('merchant_numbers', '').strip()
    abbr = request.form.get('abbr', '').strip().upper()[:3]
    with get_db() as conn:
        conn.execute(
            'UPDATE branches SET allowed_ip=?, merchant_numbers=?, abbr=? WHERE id=?',
            (allowed_ip, merchant_numbers, abbr, branch_id)
        )
        conn.commit()
    flash('Настройки филиала сохранены', 'success')
    return redirect(url_for('branches'))


@app.route('/branches/<int:branch_id>/cards/add', methods=['POST'])
@login_required
@owner_required
def add_branch_card(branch_id):
    card_number = request.form.get('card_number', '').strip().replace(' ', '')
    card_name   = request.form.get('card_name', '').strip()
    if not card_number:
        flash('Введите номер карты', 'danger')
        return redirect(url_for('branches'))
    with get_db() as conn:
        conn.execute(
            'INSERT OR IGNORE INTO branch_cards (branch_id, card_number, card_name) VALUES (?,?,?)',
            (branch_id, card_number, card_name)
        )
        conn.commit()
    flash(f'Карта {card_number} добавлена', 'success')
    return redirect(url_for('branches'))


@app.route('/branches/cards/<int:card_id>/delete', methods=['POST'])
@login_required
@owner_required
def delete_branch_card(card_id):
    with get_db() as conn:
        conn.execute('DELETE FROM branch_cards WHERE id=?', (card_id,))
        conn.commit()
    flash('Карта удалена', 'success')
    return redirect(url_for('branches'))


# ─── USERS ────────────────────────────────────────────────────────────────────

@app.route('/users')
@login_required
@owner_required
def users():
    with get_db() as conn:
        ulist = conn.execute('''
            SELECT u.*, b.name as branch_name
            FROM users u LEFT JOIN branches b ON b.id=u.branch_id
            ORDER BY u.role, u.full_name
        ''').fetchall()
        branches = conn.execute('SELECT * FROM branches WHERE is_active=1 ORDER BY name').fetchall()
        user_branches_map = {}
        for row in conn.execute('''
            SELECT ub.user_id, b.name, b.id
            FROM user_branches ub JOIN branches b ON b.id=ub.branch_id
            ORDER BY b.name
        ''').fetchall():
            user_branches_map.setdefault(row['user_id'], []).append({'id': row['id'], 'name': row['name']})
    return render_template('users.html', users=ulist, branches=branches, user_branches_map=user_branches_map)


@app.route('/users/add', methods=['POST'])
@login_required
@owner_required
def add_user():
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '')
    full_name = request.form.get('full_name', '').strip()
    role = request.form.get('role', 'admin')
    branch_ids = [bid for bid in request.form.getlist('branch_ids') if bid.isdigit()]
    primary_branch_id = int(branch_ids[0]) if branch_ids else None
    if not username or not password or not full_name:
        flash('Заполните все поля', 'danger')
        return redirect(url_for('users'))
    with get_db() as conn:
        existing = conn.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()
        if existing:
            flash('Логин уже занят', 'danger')
            return redirect(url_for('users'))
        conn.execute(
            'INSERT INTO users (username, password_hash, role, full_name, branch_id) VALUES (?,?,?,?,?)',
            (username, generate_password_hash(password, method='pbkdf2:sha256'), role, full_name, primary_branch_id)
        )
        user_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        for bid in branch_ids:
            conn.execute('INSERT OR IGNORE INTO user_branches (user_id, branch_id) VALUES (?,?)', (user_id, int(bid)))
        conn.commit()
    flash(f'Пользователь {full_name} создан', 'success')
    return redirect(url_for('users'))


@app.route('/users/<int:user_id>/reset-password', methods=['POST'])
@login_required
@owner_required
def reset_password(user_id):
    new_password = request.form.get('password', '')
    if not new_password:
        flash('Введите пароль', 'danger')
        return redirect(url_for('users'))
    with get_db() as conn:
        conn.execute(
            'UPDATE users SET password_hash=? WHERE id=?',
            (generate_password_hash(new_password, method='pbkdf2:sha256'), user_id)
        )
        conn.commit()
    flash('Пароль обновлён', 'success')
    return redirect(url_for('users'))


# ─── SETTINGS ─────────────────────────────────────────────────────────────────

@app.route('/settings')
@login_required
@owner_required
def settings():
    with get_db() as conn:
        exp_cats = conn.execute(
            'SELECT * FROM expense_categories ORDER BY COALESCE(parent_id,id), sort_order, label'
        ).fetchall()
        exp_cats_parents = [dict(c) for c in exp_cats if not c['parent_id']]
        kpi_blocks = conn.execute(
            'SELECT * FROM kpi_blocks ORDER BY sort_order, id'
        ).fetchall()
        bonus_rules = conn.execute(
            'SELECT br.*, b.name AS branch_name FROM bonus_rules br '
            'LEFT JOIN branches b ON b.id=br.branch_id '
            'ORDER BY COALESCE(b.name, \'ААА\'), br.role, br.threshold_pct'
        ).fetchall()
        branches = conn.execute('SELECT * FROM branches WHERE is_active=1 ORDER BY name').fetchall()
        rate_templates = conn.execute(
            'SELECT * FROM rate_templates ORDER BY role, name'
        ).fetchall()
        # Branch associations per template (list, not set — Jinja2 can't call set())
        tmpl_branches = {}
        for row in conn.execute('SELECT template_id, branch_id FROM rate_template_branches').fetchall():
            tmpl_branches.setdefault(row['template_id'], []).append(row['branch_id'])
        # Rate history per template
        tmpl_history = {}
        for row in conn.execute(
            'SELECT * FROM rate_template_history ORDER BY template_id, valid_from DESC'
        ).fetchall():
            tmpl_history.setdefault(row['template_id'], []).append(row)
        api_tokens = conn.execute('''
            SELECT t.*, b.name AS branch_name
            FROM api_1c_tokens t JOIN branches b ON b.id=t.branch_id
            ORDER BY b.name
        ''').fetchall()
        api_log = conn.execute(
            'SELECT l.*, b.name AS branch_name FROM api_1c_log l '
            'LEFT JOIN branches b ON b.id=l.branch_id '
            'ORDER BY l.created_at DESC LIMIT 50'
        ).fetchall()
    return render_template('settings.html',
        exp_cats=exp_cats, exp_cats_parents=exp_cats_parents,
        kpi_blocks=kpi_blocks,
        bonus_rules=bonus_rules, branches=branches,
        rate_templates=rate_templates,
        tmpl_branches=tmpl_branches, tmpl_history=tmpl_history,
        api_tokens=api_tokens, api_log=api_log,
        today=date.today().isoformat(),
        formula_vars=FORMULA_VARS, role_labels=ROLE_LABELS)


@app.route('/settings/expense-cat/add', methods=['POST'])
@login_required
@owner_required
def add_expense_cat():
    label = request.form.get('label', '').strip()
    cat_type = request.form.get('type', 'expense')
    parent_id = request.form.get('parent_id') or None
    if cat_type not in ('expense', 'income'):
        cat_type = 'expense'
    if not label:
        flash('Введите название', 'danger')
        return redirect(url_for('settings'))
    code = label.lower().replace(' ', '_').replace('/', '_')[:30]
    with get_db() as conn:
        if parent_id:
            parent_row = conn.execute('SELECT id, type FROM expense_categories WHERE id=?', (parent_id,)).fetchone()
            if not parent_row:
                flash('Родительская категория не найдена', 'danger')
                return redirect(url_for('settings'))
            cat_type = parent_row['type']
            parent_id = parent_row['id']
        existing = conn.execute('SELECT id FROM expense_categories WHERE code=?', (code,)).fetchone()
        if existing:
            code = code + '_' + str(int(datetime.now().timestamp()))[-4:]
        max_sort = conn.execute('SELECT COALESCE(MAX(sort_order),0) FROM expense_categories').fetchone()[0]
        conn.execute(
            'INSERT INTO expense_categories (code, label, type, parent_id, sort_order) VALUES (?,?,?,?,?)',
            (code, label, cat_type, parent_id, max_sort + 1)
        )
        conn.commit()
    kind = 'Подкатегория' if parent_id else 'Категория'
    flash(f'{kind} «{label}» добавлена', 'success')
    return redirect(url_for('settings'))


@app.route('/settings/expense-cat/<int:cat_id>/toggle', methods=['POST'])
@login_required
@owner_required
def toggle_expense_cat(cat_id):
    with get_db() as conn:
        conn.execute('UPDATE expense_categories SET is_active=1-is_active WHERE id=?', (cat_id,))
        # Also toggle all subcategories
        conn.execute(
            'UPDATE expense_categories SET is_active=(SELECT is_active FROM expense_categories WHERE id=?) WHERE parent_id=?',
            (cat_id, cat_id)
        )
        conn.commit()
    return redirect(url_for('settings'))


@app.route('/settings/expense-cat/<int:cat_id>/delete', methods=['POST'])
@login_required
@owner_required
def delete_expense_cat(cat_id):
    with get_db() as conn:
        # Move subcategories to top-level before deleting parent
        conn.execute('UPDATE expense_categories SET parent_id=NULL WHERE parent_id=?', (cat_id,))
        conn.execute('DELETE FROM expense_categories WHERE id=?', (cat_id,))
        conn.commit()
    flash('Категория удалена', 'success')
    return redirect(url_for('settings'))


@app.route('/settings/kpi/add', methods=['POST'])
@login_required
@owner_required
def add_kpi_block():
    title = request.form.get('title', '').strip()
    formula = request.form.get('formula', '').strip()
    color = request.form.get('color', 'primary')
    unit = request.form.get('unit', '₽').strip()
    if not title or not formula:
        flash('Введите название и формулу', 'danger')
        return redirect(url_for('settings'))
    # Validate formula
    test_vars = {k: 1.0 for k in FORMULA_VARS}
    result = safe_eval(formula, test_vars)
    if result is None:
        flash('Ошибка в формуле — проверьте переменные и операторы', 'danger')
        return redirect(url_for('settings'))
    with get_db() as conn:
        max_sort = conn.execute('SELECT COALESCE(MAX(sort_order),0) FROM kpi_blocks').fetchone()[0]
        conn.execute(
            'INSERT INTO kpi_blocks (title, formula, color, unit, sort_order) VALUES (?,?,?,?,?)',
            (title, formula, color, unit, max_sort + 1)
        )
        conn.commit()
    flash(f'Блок «{title}» добавлен', 'success')
    return redirect(url_for('settings'))


@app.route('/settings/kpi/<int:block_id>/delete', methods=['POST'])
@login_required
@owner_required
def delete_kpi_block(block_id):
    with get_db() as conn:
        conn.execute('DELETE FROM kpi_blocks WHERE id=?', (block_id,))
        conn.commit()
    flash('Блок удалён', 'success')
    return redirect(url_for('settings'))


@app.route('/settings/kpi/<int:block_id>/toggle', methods=['POST'])
@login_required
@owner_required
def toggle_kpi_block(block_id):
    with get_db() as conn:
        conn.execute('UPDATE kpi_blocks SET is_active=1-is_active WHERE id=?', (block_id,))
        conn.commit()
    return redirect(url_for('settings'))


@app.route('/settings/bonus-rules/add', methods=['POST'])
@login_required
@owner_required
def add_bonus_rule():
    role = request.form.get('role', '')
    threshold = request.form.get('threshold_pct', '')
    bonus = request.form.get('bonus_pct', '')
    if not role or not threshold or not bonus:
        flash('Заполните все поля', 'danger')
        return redirect(url_for('settings') + '#tab-bonuses')
    try:
        threshold = float(threshold)
        bonus = float(bonus)
    except ValueError:
        flash('Неверный формат числа', 'danger')
        return redirect(url_for('settings') + '#tab-bonuses')
    branch_id = request.form.get('branch_id') or None
    if branch_id:
        try: branch_id = int(branch_id)
        except ValueError: branch_id = None
    with get_db() as conn:
        conn.execute(
            'INSERT INTO bonus_rules (role, threshold_pct, bonus_pct, branch_id) VALUES (?,?,?,?)',
            (role, threshold, bonus, branch_id)
        )
        conn.commit()
    flash(f'Правило добавлено', 'success')
    return redirect(url_for('settings') + '#tab-bonuses')


@app.route('/settings/bonus-rules/<int:rule_id>/delete', methods=['POST'])
@login_required
@owner_required
def delete_bonus_rule(rule_id):
    with get_db() as conn:
        conn.execute('DELETE FROM bonus_rules WHERE id=?', (rule_id,))
        conn.commit()
    flash('Правило удалено', 'success')
    return redirect(url_for('settings') + '#tab-bonuses')


@app.route('/settings/bonus-rules/<int:rule_id>/toggle', methods=['POST'])
@login_required
@owner_required
def toggle_bonus_rule(rule_id):
    with get_db() as conn:
        conn.execute('UPDATE bonus_rules SET is_active=1-is_active WHERE id=?', (rule_id,))
        conn.commit()
    return redirect(url_for('settings') + '?tab=bonuses')


@app.route('/bonus_rules/<int:rule_id>/edit', methods=['POST'])
@login_required
@owner_required
def edit_bonus_rule(rule_id):
    role = request.form.get('role')
    branch_id = request.form.get('branch_id') or None
    if branch_id:
        try:
            branch_id = int(branch_id)
        except ValueError:
            branch_id = None
    try:
        threshold_pct = float(request.form.get('threshold_pct', 0))
        bonus_pct = float(request.form.get('bonus_pct', 0))
    except ValueError:
        flash('Неверные значения', 'danger')
        return redirect(url_for('settings') + '?tab=bonuses')
    with get_db() as conn:
        conn.execute(
            'UPDATE bonus_rules SET role=?, threshold_pct=?, bonus_pct=?, branch_id=? WHERE id=?',
            (role, threshold_pct, bonus_pct, branch_id, rule_id)
        )
        conn.commit()
    flash('Правило обновлено', 'success')
    return redirect(url_for('settings') + '?tab=bonuses')


# ─── RATE TEMPLATES ───────────────────────────────────────────────────────────

@app.route('/settings/rate-templates/add', methods=['POST'])
@login_required
@owner_required
def add_rate_template():
    role       = request.form.get('role', '').strip()
    name       = request.form.get('name', '').strip()
    rate       = float(request.form.get('rate', 0) or 0)
    rate_km    = float(request.form.get('rate_per_km', 10) or 10)
    rate_ord   = float(request.form.get('rate_per_order', 100) or 100)
    branch_ids = request.form.getlist('branch_ids')
    valid_from = request.form.get('valid_from') or date.today().isoformat()
    if not role or not name:
        flash('Заполните роль и название', 'danger')
        return redirect(url_for('settings') + '?tab=rates')
    with get_db() as conn:
        conn.execute(
            'INSERT INTO rate_templates (role, name, rate, rate_per_km, rate_per_order) VALUES (?,?,?,?,?)',
            (role, name, rate, rate_km, rate_ord)
        )
        tmpl_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        for bid in branch_ids:
            try:
                conn.execute('INSERT OR IGNORE INTO rate_template_branches (template_id, branch_id) VALUES (?,?)',
                             (tmpl_id, int(bid)))
            except (ValueError, Exception):
                pass
        conn.execute(
            'INSERT INTO rate_template_history (template_id, rate, rate_per_km, rate_per_order, valid_from) VALUES (?,?,?,?,?)',
            (tmpl_id, rate, rate_km, rate_ord, valid_from)
        )
        conn.commit()
    flash(f'Ставка «{name}» добавлена', 'success')
    return redirect(url_for('settings') + '?tab=rates')


@app.route('/settings/rate-templates/<int:tmpl_id>/delete', methods=['POST'])
@login_required
@owner_required
def delete_rate_template(tmpl_id):
    with get_db() as conn:
        conn.execute('DELETE FROM rate_templates WHERE id=?', (tmpl_id,))
        conn.commit()
    flash('Ставка удалена', 'success')
    return redirect(url_for('settings') + '?tab=rates')


@app.route('/settings/rate-templates/<int:tmpl_id>/edit', methods=['POST'])
@login_required
@owner_required
def edit_rate_template(tmpl_id):
    name       = request.form.get('name', '').strip()
    rate       = float(request.form.get('rate', 0) or 0)
    rate_km    = float(request.form.get('rate_per_km', 10) or 10)
    rate_ord   = float(request.form.get('rate_per_order', 100) or 100)
    branch_ids = request.form.getlist('branch_ids')
    valid_from = request.form.get('valid_from') or date.today().isoformat()
    with get_db() as conn:
        # Update current rates if valid_from <= today
        if valid_from <= date.today().isoformat():
            conn.execute(
                'UPDATE rate_templates SET name=?, rate=?, rate_per_km=?, rate_per_order=? WHERE id=?',
                (name, rate, rate_km, rate_ord, tmpl_id)
            )
        else:
            conn.execute('UPDATE rate_templates SET name=? WHERE id=?', (name, tmpl_id))
        # Save rate history entry
        conn.execute(
            'INSERT INTO rate_template_history (template_id, rate, rate_per_km, rate_per_order, valid_from) VALUES (?,?,?,?,?)',
            (tmpl_id, rate, rate_km, rate_ord, valid_from)
        )
        # Replace branch associations
        conn.execute('DELETE FROM rate_template_branches WHERE template_id=?', (tmpl_id,))
        for bid in branch_ids:
            try:
                conn.execute('INSERT OR IGNORE INTO rate_template_branches (template_id, branch_id) VALUES (?,?)',
                             (tmpl_id, int(bid)))
            except (ValueError, Exception):
                pass
        conn.commit()
    flash('Ставка обновлена', 'success')
    return redirect(url_for('settings') + '?tab=rates')


# ─── REPORTS ──────────────────────────────────────────────────────────────────

@app.route('/reports')
@login_required
@owner_required
def reports():
    period = request.args.get('period', 'week')
    branch_ids = [bid for bid in request.args.getlist('branch_ids') if bid.isdigit()]
    active_tab = request.args.get('tab', 'shifts')

    today = date.today().isoformat()
    month_start = date.today().replace(day=1).isoformat()
    s_date_from = request.args.get('s_date_from', month_start)
    s_date_to   = request.args.get('s_date_to',   today)
    s_branch_id = request.args.get('s_branch_id', '')
    s_role      = request.args.get('s_role', '')
    s_unpaid    = request.args.get('s_unpaid', '')

    with get_db() as conn:
        branches = conn.execute('SELECT * FROM branches WHERE is_active=1 ORDER BY name').fetchall()

        date_filter   = "AND s.date >= date('now','-7 days')" if period == 'week' else \
                        "AND s.date >= date('now','start of month')" if period == 'month' else \
                        "AND s.date >= date('now','-30 days')"
        if branch_ids:
            ids_str = ','.join(str(int(bid)) for bid in branch_ids)
            branch_filter = f"AND s.branch_id IN ({ids_str})"
        else:
            branch_filter = ""

        shifts_data = conn.execute(f'''
            SELECT s.date, s.branch_id, b.name as branch_name, s.status,
                   COALESCE(r.total_revenue,0)    as revenue,
                   COALESCE(r.delivery_revenue,0) as delivery,
                   COALESCE(r.pickup_revenue,0)   as pickup,
                   COALESCE(r.delivery_orders,0)+COALESCE(r.pickup_orders,0) as orders,
                   COALESCE(r.cash_amount,0)       as cash,
                   COALESCE(r.card_amount,0)       as card,
                   COALESCE(r.online_amount,0)     as online,
                   COALESCE((SELECT SUM(es.total_amount) FROM employee_shifts es
                              WHERE es.shift_id = s.id), 0) as fot,
                   s.id as shift_id
            FROM shifts s
            JOIN branches b ON b.id=s.branch_id
            LEFT JOIN shift_revenue r ON r.shift_id=s.id
            WHERE 1=1 {date_filter} {branch_filter}
            ORDER BY s.date DESC, b.name
        ''').fetchall()
        totals = conn.execute(f'''
            SELECT COALESCE(SUM(r.total_revenue),0) as revenue,
                   COALESCE(SUM(r.delivery_revenue),0) as delivery,
                   COALESCE(SUM(r.pickup_revenue),0) as pickup,
                   COALESCE(SUM(r.delivery_orders+r.pickup_orders),0) as orders,
                   COALESCE(SUM(r.cash_amount),0) as cash,
                   COALESCE(SUM(r.card_amount),0) as card,
                   COALESCE((SELECT SUM(es.total_amount) FROM employee_shifts es
                              JOIN shifts s2 ON s2.id=es.shift_id
                              WHERE 1=1 {date_filter} {branch_filter}
                              ), 0) as fot
            FROM shifts s LEFT JOIN shift_revenue r ON r.shift_id=s.id
            WHERE 1=1 {date_filter} {branch_filter}
        ''').fetchone()

        # Group shifts by date for the grouped view
        from collections import OrderedDict as _OD
        _dg = _OD()
        for _row in shifts_data:
            _d = _row['date']
            if _d not in _dg:
                _dg[_d] = {'date': _d, 'revenue': 0.0, 'pickup': 0.0,
                            'fot': 0.0, 'branches': []}
            _dg[_d]['revenue'] += _row['revenue']
            _dg[_d]['pickup']  += _row['pickup']
            _dg[_d]['fot']     += _row['fot']
            _dg[_d]['branches'].append(dict(_row))
        day_groups = list(_dg.values())
        salary_data = conn.execute(f'''
            SELECT es.full_name_snapshot, es.role_snapshot,
                   SUM(es.total_amount) as earned, SUM(es.paid_amount) as paid,
                   SUM(es.total_amount)-SUM(es.paid_amount) as debt,
                   COUNT(*) as shifts_count
            FROM employee_shifts es
            JOIN shifts s ON s.id=es.shift_id
            WHERE 1=1 {date_filter} {branch_filter}
            GROUP BY es.full_name_snapshot, es.role_snapshot
            ORDER BY debt DESC
        ''').fetchall()

        # ── Зарплатный отчёт ──────────────────────────────────────────────
        s_group = request.args.get('s_group', 'summary')
        s_emps  = request.args.getlist('s_emps')

        sal_conds  = ['s.date BETWEEN ? AND ?']
        sal_params = [s_date_from, s_date_to]
        if s_branch_id.isdigit():
            sal_conds.append('s.branch_id = ?')
            sal_params.append(int(s_branch_id))
        if s_role:
            sal_conds.append('es.role_snapshot = ?')
            sal_params.append(s_role)
        sal_where  = ' AND '.join(sal_conds)
        sal_having = 'HAVING SUM(es.total_amount) > SUM(es.paid_amount)' if s_unpaid == '1' else ''

        sal_report = conn.execute(f'''
            SELECT es.employee_id,
                   COALESCE(e.full_name, es.full_name_snapshot) AS name,
                   COALESCE(e.role, es.role_snapshot)           AS role,
                   b.name                                        AS branch_name,
                   COUNT(*)                                      AS shifts_count,
                   COALESCE(SUM(es.total_amount), 0)             AS earned,
                   COALESCE(SUM(es.paid_amount),  0)             AS paid,
                   COALESCE(SUM(es.total_amount - es.paid_amount), 0) AS debt
            FROM employee_shifts es
            JOIN shifts    s ON s.id    = es.shift_id
            JOIN branches  b ON b.id    = s.branch_id
            LEFT JOIN employees e ON e.id = es.employee_id
            WHERE {sal_where}
            GROUP BY COALESCE(CAST(es.employee_id AS TEXT), es.full_name_snapshot),
                     es.role_snapshot, b.id
            {sal_having}
            ORDER BY b.name, es.role_snapshot, COALESCE(e.full_name, es.full_name_snapshot)
        ''', sal_params).fetchall()

        # Список всех сотрудников в периоде (для дропдауна выбора)
        all_sal_emps = conn.execute(f'''
            SELECT DISTINCT COALESCE(e3.full_name, es.full_name_snapshot) AS name
            FROM employee_shifts es
            JOIN shifts s ON s.id = es.shift_id
            LEFT JOIN employees e3 ON e3.id = es.employee_id
            WHERE {sal_where}
            ORDER BY 1
        ''', sal_params).fetchall()
        all_sal_emps = [r['name'] for r in all_sal_emps]

        # ── Сводная таблица по периодам ───────────────────────────────────
        pivot_rows = []
        pivot_emps = []

        if s_group != 'summary':
            raw_params = sal_params[:]
            emp_filter_sql = ''
            if s_emps:
                placeholders = ','.join('?' * len(s_emps))
                emp_filter_sql = f'AND COALESCE(e2.full_name, es.full_name_snapshot) IN ({placeholders})'
                raw_params = raw_params + s_emps

            raw_rows = conn.execute(f'''
                SELECT s.date,
                       COALESCE(e2.full_name, es.full_name_snapshot) AS name,
                       COALESCE(es.total_amount, 0) AS earned,
                       COALESCE(es.paid_amount,  0) AS paid
                FROM employee_shifts es
                JOIN shifts s ON s.id = es.shift_id
                LEFT JOIN employees e2 ON e2.id = es.employee_id
                WHERE {sal_where} {emp_filter_sql}
                ORDER BY s.date, COALESCE(e2.full_name, es.full_name_snapshot)
            ''', raw_params).fetchall()

            def _period_key(date_str):
                d = datetime.strptime(date_str, '%Y-%m-%d')
                if s_group == 'day':
                    return date_str
                elif s_group == 'week':
                    return (d - timedelta(days=d.weekday())).strftime('%Y-%m-%d')
                else:  # month
                    return date_str[:7]

            def _period_label(key):
                if s_group == 'day':
                    d = datetime.strptime(key, '%Y-%m-%d')
                    days_ru = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
                    return f"{days_ru[d.weekday()]} {d.strftime('%d.%m.%Y')}"
                elif s_group == 'week':
                    mon = datetime.strptime(key, '%Y-%m-%d')
                    sun = mon + timedelta(days=6)
                    return f"{mon.strftime('%d.%m')}–{sun.strftime('%d.%m.%Y')}"
                else:
                    months_ru = ['', 'Январь', 'Февраль', 'Март', 'Апрель', 'Май',
                                 'Июнь', 'Июль', 'Август', 'Сентябрь', 'Октябрь',
                                 'Ноябрь', 'Декабрь']
                    y, m = key.split('-')
                    return f"{months_ru[int(m)]} {y}"

            from collections import OrderedDict
            periods = OrderedDict()
            emp_order = []

            for row in raw_rows:
                pk  = _period_key(row['date'])
                nm  = row['name']
                if pk not in periods:
                    periods[pk] = {'label': _period_label(pk), 'emps': {}, 'earned': 0.0, 'paid': 0.0}
                if nm not in periods[pk]['emps']:
                    periods[pk]['emps'][nm] = {'earned': 0.0, 'paid': 0.0}
                periods[pk]['emps'][nm]['earned'] += row['earned']
                periods[pk]['emps'][nm]['paid']   += row['paid']
                periods[pk]['earned'] += row['earned']
                periods[pk]['paid']   += row['paid']
                if nm not in emp_order:
                    emp_order.append(nm)

            for p in periods.values():
                p['debt'] = p['earned'] - p['paid']
                for cell in p['emps'].values():
                    cell['debt'] = cell['earned'] - cell['paid']

            pivot_rows = list(periods.values())
            pivot_emps = emp_order

    return render_template('reports.html',
        shifts_data=shifts_data, totals=totals, branches=branches,
        salary_data=salary_data, period=period, selected_branches=branch_ids,
        role_labels=ROLE_LABELS, active_tab=active_tab,
        sal_report=sal_report,
        s_date_from=s_date_from, s_date_to=s_date_to,
        s_branch_id=s_branch_id, s_role=s_role, s_unpaid=s_unpaid,
        s_group=s_group, s_emps=s_emps,
        all_sal_emps=all_sal_emps, pivot_rows=pivot_rows, pivot_emps=pivot_emps,
        day_groups=day_groups,
        branch_groups=get_branch_groups(conn))


# ─── EMPLOYEE SALARY DETAIL ───────────────────────────────────────────────────

@app.route('/reports/employee/<int:emp_id>')
@login_required
@owner_required
def employee_salary_detail(emp_id):
    month_start = date.today().replace(day=1).isoformat()
    today = date.today().isoformat()
    date_from = request.args.get('date_from', month_start)
    date_to   = request.args.get('date_to',   today)

    with get_db() as conn:
        emp = conn.execute('SELECT * FROM employees WHERE id=?', (emp_id,)).fetchone()
        if not emp:
            flash('Сотрудник не найден', 'danger')
            return redirect(url_for('reports') + '?tab=salary')

        shifts_data = conn.execute('''
            SELECT es.id AS es_id,
                   s.id  AS shift_id,
                   s.date,
                   b.name AS branch_name,
                   es.role_snapshot,
                   es.hours_worked,
                   es.km,
                   es.orders,
                   es.shift_start,
                   es.shift_end,
                   es.base_pay,
                   es.bonus_amount,
                   COALESCE(es.auto_bonus, 0) AS auto_bonus,
                   es.penalty_amount,
                   es.total_amount,
                   es.paid_amount,
                   es.is_paid,
                   es.bonus_comment
            FROM employee_shifts es
            JOIN shifts s ON s.id = es.shift_id
            JOIN branches b ON b.id = s.branch_id
            WHERE es.employee_id = ? AND s.date BETWEEN ? AND ?
            ORDER BY s.date DESC, b.name
        ''', (emp_id, date_from, date_to)).fetchall()

        total_earned = sum(float(r['total_amount'] or 0) for r in shifts_data)
        total_paid   = sum(float(r['paid_amount']   or 0) for r in shifts_data)
        total_debt   = total_earned - total_paid

    return render_template('employee_salary_detail.html',
        emp=emp, shifts_data=shifts_data,
        date_from=date_from, date_to=date_to,
        total_earned=total_earned, total_paid=total_paid, total_debt=total_debt,
        role_labels=ROLE_LABELS)


# ─── EXPENSES REPORT ──────────────────────────────────────────────────────────


# ─── CASH FLOW REPORT ────────────────────────────────────────────────────────

@app.route('/report/cash-flow')
@login_required
@owner_required
def cash_flow_report():
    from collections import defaultdict
    today      = date.today().isoformat()
    month_start = date.today().replace(day=1).isoformat()
    date_from  = request.args.get('date_from', month_start)
    date_to    = request.args.get('date_to',   today)
    branch_ids = [b for b in request.args.getlist('branch_ids') if b.isdigit()]

    with get_db() as conn:
        branches      = conn.execute('SELECT * FROM branches WHERE is_active=1 ORDER BY name').fetchall()
        branch_groups = get_branch_groups(conn)
        if branch_ids:
            bf_ids = ','.join(branch_ids)
            bf = f'AND s.branch_id IN ({bf_ids})'
        else:
            bf = ''

        revenue_rows = conn.execute(f'''
            SELECT s.date, s.id AS shift_id, b.id AS branch_id, b.name AS branch_name,
                   COALESCE(r.cash_amount, 0)    AS cash_revenue,
                   COALESCE(r.actual_cash, 0)    AS actual_cash,
                   COALESCE(r.actual_cash_comment, '') AS actual_cash_comment,
                   COALESCE(r.change_amount, 0)  AS razmen,
                   COALESCE(r.plus_amount, 0)    AS plus_amount,
                   COALESCE(r.morning_cash, 0)   AS morning_cash
            FROM shifts s
            JOIN branches b ON b.id = s.branch_id
            LEFT JOIN shift_revenue r ON r.shift_id = s.id
            WHERE s.date BETWEEN ? AND ? {bf}
            ORDER BY s.date, b.name
        ''', (date_from, date_to)).fetchall()

        expense_rows = conn.execute(f'''
            SELECT s.date, s.branch_id,
                COALESCE(SUM(e.amount_cash), 0) AS expenses_cash
            FROM expenses e
            JOIN shifts s ON s.id = e.shift_id
            WHERE s.date BETWEEN ? AND ? {bf}
            GROUP BY s.date, s.branch_id
        ''', (date_from, date_to)).fetchall()

        salary_rows = conn.execute(f'''
            SELECT s.date, s.branch_id, COALESCE(SUM(es.paid_amount), 0) AS salary_paid
            FROM employee_shifts es
            JOIN shifts s ON s.id = es.shift_id
            WHERE s.date BETWEEN ? AND ? {bf} AND es.is_paid = 1
            GROUP BY s.date, s.branch_id
        ''', (date_from, date_to)).fetchall()

    exp_map = defaultdict(float)
    for r in expense_rows:
        exp_map[(r['date'], r['branch_id'])] += r['expenses_cash']

    sal_map = defaultdict(float)
    for r in salary_rows:
        sal_map[(r['date'], r['branch_id'])] += r['salary_paid']

    days = {}
    for r in revenue_rows:
        d = r['date']
        bid = r['branch_id']
        exp  = exp_map.get((d, bid), 0)
        sal  = sal_map.get((d, bid), 0)
        raz  = r['razmen']
        plus = r['plus_amount']
        mrn = r['morning_cash']
        if d not in days:
            days[d] = {'date': d, 'shifts': [], 'cash_revenue': 0.0,
                       'expenses_cash': 0.0, 'razmen': 0.0, 'plus_amount': 0.0,
                       'morning_cash': 0.0, 'salary_paid': 0.0, 'actual_cash': 0.0}
        days[d]['shifts'].append({
            'shift_id':    r['shift_id'],
            'branch_name': r['branch_name'],
            'cash_revenue': r['cash_revenue'],
            'expenses_cash': exp,
            'razmen':       raz,
            'plus_amount':  plus,
            'morning_cash': mrn,
            'salary_paid':  sal,
            'actual_cash':  r['actual_cash'],
            'actual_cash_comment': r['actual_cash_comment'],
        })
        days[d]['cash_revenue']  += r['cash_revenue']
        days[d]['expenses_cash'] += exp
        days[d]['razmen']        += raz
        days[d]['plus_amount']   += plus
        days[d]['morning_cash']  += mrn
        days[d]['salary_paid']   += sal
        days[d]['actual_cash']   += r['actual_cash']

    all_dates   = sorted(days.keys())
    prev_cash   = {}
    for i, d in enumerate(all_dates):
        prev_cash[d] = days[all_dates[i-1]]['actual_cash'] if i > 0 else 0.0

    sorted_days = [days[d] for d in reversed(all_dates)]
    for day in sorted_days:
        day['morning_cash'] = prev_cash.get(day['date'], 0.0)

    totals = {
        'razmen':       sum(d['razmen']       for d in sorted_days),
        'plus_amount':  sum(d['plus_amount']  for d in sorted_days),
        'cash_revenue': sum(d['cash_revenue'] for d in sorted_days),
        'expenses_cash':sum(d['expenses_cash']for d in sorted_days),
        'salary_paid':  sum(d['salary_paid']  for d in sorted_days),
    }

    return render_template('cash_flow.html',
        days=sorted_days,
        totals=totals,
        branches=branches,
        branch_groups=branch_groups,
        branch_ids=[str(b) for b in branch_ids],
        date_from=date_from, date_to=date_to)


# ─── API 1C ИНТЕГРАЦИЯ ────────────────────────────────────────────────────────

import secrets, xml.etree.ElementTree as _ET

@app.route('/api/1c/<token>', defaults={'subpath': ''}, methods=['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'])
@app.route('/api/1c/<token>/<path:subpath>', methods=['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'])
def api_1c_endpoint(token, subpath):
    body = request.get_data(as_text=True)
    with get_db() as conn:
        rec = conn.execute(
            'SELECT * FROM api_1c_tokens WHERE token=?', (token,)
        ).fetchone()

        conn.execute(
            'INSERT INTO api_1c_log (token, branch_id, method, path, body) VALUES (?,?,?,?,?)',
            (token, rec['branch_id'] if rec else None,
             request.method, request.full_path, body[:20000])
        )
        log_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]

        if not rec:
            conn.commit()
            return 'Unauthorized', 401

        branch_id = rec['branch_id']
        parsed = 0

        if request.method == 'POST' and body.strip():
            try:
                purchases_created = _parse_1c_body(conn, branch_id, body,
                                                   request.content_type or '')
                if purchases_created:
                    parsed = 1
                    conn.execute('UPDATE api_1c_log SET parsed_ok=1, status=? WHERE id=?',
                                 (f'создано {purchases_created} накладных', log_id))
            except Exception as e:
                conn.execute('UPDATE api_1c_log SET status=? WHERE id=?',
                             (f'ошибка: {str(e)[:200]}', log_id))

        conn.commit()

    ct = request.content_type or ''
    if 'xml' in ct:
        return '<?xml version="1.0"?><result>true</result>', 200, {'Content-Type': 'application/xml'}
    return '{"result":true}', 200, {'Content-Type': 'application/json'}


def _parse_1c_body(conn, branch_id, body, content_type):
    """Парсит тело запроса от 1С и создаёт накладные. Возвращает кол-во созданных записей."""
    created = 0
    body_s = body.strip()

    # ── JSON ────────────────────────────────────────────────────────────────────
    if body_s.startswith('{') or body_s.startswith('[') or 'json' in content_type:
        import json as _json
        data = _json.loads(body_s)
        docs = data if isinstance(data, list) else data.get('Документы', data.get('documents', [data]))
        for doc in docs:
            supplier = (doc.get('Контрагент') or doc.get('supplier') or doc.get('КонтрагентНаименование') or '')
            if isinstance(supplier, dict):
                supplier = supplier.get('Наименование') or supplier.get('name') or ''
            amount = float(doc.get('СуммаДокумента') or doc.get('amount') or doc.get('Сумма') or 0)
            date_str = (doc.get('Дата') or doc.get('date') or '')[:10]
            inv_num = str(doc.get('Номер') or doc.get('number') or '')
            if supplier and amount > 0:
                conn.execute(
                    'INSERT INTO purchases (branch_id,supplier,amount,date,invoice_number,note) VALUES (?,?,?,?,?,?)',
                    (branch_id, supplier[:200], amount, date_str or date.today().isoformat(),
                     inv_num[:100] or None, '1С импорт')
                )
                conn.execute('INSERT OR IGNORE INTO purchase_suppliers (name) VALUES (?)', (supplier[:200],))
                created += 1
        return created

    # ── XML (EnterpriseData / CommerceML) ───────────────────────────────────────
    if body_s.startswith('<'):
        root = _ET.fromstring(body_s)
        ns_map = {'ed': 'http://v8.1c.ru/edi/edi_stnd/EnterpriseData/1.0',
                  'cm': 'urn:1C.ru:commerceml_3'}

        def find_text(el, *tags):
            for tag in tags:
                for ns_prefix, ns_uri in ns_map.items():
                    found = el.find(f'{{{ns_uri}}}{tag}')
                    if found is not None and found.text:
                        return found.text.strip()
                found = el.find(tag)
                if found is not None and found.text:
                    return found.text.strip()
            return ''

        # Ищем документы поступления товаров
        candidates = (
            list(root.iter('{http://v8.1c.ru/edi/edi_stnd/EnterpriseData/1.0}Документ')) +
            list(root.iter('Документ')) +
            list(root.iter('{urn:1C.ru:commerceml_3}Документ')) +
            list(root.iter('document'))
        )
        for doc in candidates:
            type_attr = doc.get('Тип') or doc.get('type') or find_text(doc, 'Тип', 'type')
            if type_attr and 'Поступ' not in type_attr and 'Receipt' not in type_attr and 'Invoice' not in type_attr:
                continue
            supplier = find_text(doc, 'КонтрагентНаименование', 'Контрагент', 'supplier', 'Поставщик')
            if not supplier:
                ct_el = doc.find('Контрагент') or doc.find('{http://v8.1c.ru/edi/edi_stnd/EnterpriseData/1.0}Контрагент')
                if ct_el is not None:
                    supplier = find_text(ct_el, 'Наименование', 'name') or (ct_el.text or '').strip()
            amount_str = find_text(doc, 'СуммаДокумента', 'Сумма', 'amount', 'Total')
            amount = float(amount_str.replace(',', '.')) if amount_str else 0
            date_str = find_text(doc, 'Дата', 'date', 'Date')[:10] if find_text(doc, 'Дата', 'date', 'Date') else ''
            inv_num  = find_text(doc, 'Номер', 'number', 'Number')
            if supplier and amount > 0:
                conn.execute(
                    'INSERT INTO purchases (branch_id,supplier,amount,date,invoice_number,note) VALUES (?,?,?,?,?,?)',
                    (branch_id, supplier[:200], amount, date_str or date.today().isoformat(),
                     inv_num[:100] or None, '1С импорт')
                )
                conn.execute('INSERT OR IGNORE INTO purchase_suppliers (name) VALUES (?)', (supplier[:200],))
                created += 1
        return created

    return 0


@app.route('/settings/api/tokens/add', methods=['POST'])
@login_required
@owner_required
def api_token_add():
    branch_id   = request.form.get('branch_id', type=int)
    description = request.form.get('description', '').strip()
    if not branch_id:
        flash('Выберите филиал', 'danger')
        return redirect(url_for('settings') + '?tab=api')
    token = secrets.token_urlsafe(24)
    with get_db() as conn:
        conn.execute(
            'INSERT INTO api_1c_tokens (branch_id, token, description) VALUES (?,?,?)',
            (branch_id, token, description or None)
        )
        conn.commit()
    flash('Токен создан', 'success')
    return redirect(url_for('settings') + '?tab=api')


@app.route('/settings/api/tokens/<int:tid>/delete', methods=['POST'])
@login_required
@owner_required
def api_token_delete(tid):
    with get_db() as conn:
        conn.execute('DELETE FROM api_1c_tokens WHERE id=?', (tid,))
        conn.commit()
    flash('Токен удалён', 'success')
    return redirect(url_for('settings') + '?tab=api')


@app.route('/settings/api/log/clear', methods=['POST'])
@login_required
@owner_required
def api_log_clear():
    with get_db() as conn:
        conn.execute('DELETE FROM api_1c_log')
        conn.commit()
    flash('Лог очищен', 'success')
    return redirect(url_for('settings') + '?tab=api')


# ─── PURCHASES (накладные) ────────────────────────────────────────────────────

@app.route('/purchases')
@login_required
def purchases():
    role = session.get('role')
    with get_db() as conn:
        if role == 'owner':
            all_branches  = conn.execute('SELECT * FROM branches WHERE is_active=1 ORDER BY name').fetchall()
            branch_groups = get_branch_groups(conn)
        else:
            all_branches  = []
            branch_groups = []

        today     = date.today().isoformat()
        first_day = date.today().replace(day=1).isoformat()
        date_from       = request.args.get('p_date_from', first_day)
        date_to         = request.args.get('p_date_to',   today)
        branch_flt_ids  = [b for b in request.args.getlist('p_branch_ids')      if b.isdigit()]
        supp_flt_names  = request.args.getlist('p_supplier_names')

        where  = ['p.date >= ?', 'p.date <= ?']
        params = [date_from, date_to]

        if role != 'owner':
            bids = _session_branch_ids()
            if bids:
                where.append(f"p.branch_id IN ({','.join(str(b) for b in bids)})")
        elif branch_flt_ids:
            ph = ','.join('?' * len(branch_flt_ids))
            where.append(f'p.branch_id IN ({ph})')
            params.extend(int(b) for b in branch_flt_ids)

        if supp_flt_names:
            ph = ','.join('?' * len(supp_flt_names))
            where.append(f'p.supplier IN ({ph})')
            params.extend(supp_flt_names)

        sql_where = ' AND '.join(where)

        rows = conn.execute(f'''
            SELECT p.*, b.name AS branch_name
            FROM purchases p
            JOIN branches b ON b.id = p.branch_id
            WHERE {sql_where}
            ORDER BY p.date DESC, p.id DESC
        ''', params).fetchall()

        by_supplier = conn.execute(f'''
            SELECT p.supplier,
                   COUNT(*) AS cnt,
                   SUM(p.amount) AS total
            FROM purchases p
            JOIN branches b ON b.id = p.branch_id
            WHERE {sql_where}
            GROUP BY p.supplier
            ORDER BY total DESC
        ''', params).fetchall()

        suppliers = [r['name'] for r in conn.execute(
            'SELECT name FROM purchase_suppliers ORDER BY name'
        ).fetchall()]

    return render_template('purchases.html',
                           rows=rows,
                           by_supplier=by_supplier,
                           suppliers=suppliers,
                           all_branches=all_branches,
                           branch_groups=branch_groups,
                           is_owner=(role == 'owner'),
                           date_from=date_from,
                           date_to=date_to,
                           branch_flt_ids=branch_flt_ids,
                           supp_flt_names=supp_flt_names,
                           today=today)


@app.route('/purchases/add', methods=['POST'])
@login_required
def purchases_add():
    role = session.get('role')
    if role == 'owner':
        branch_id = request.form.get('branch_id', type=int)
    else:
        branch_id = session.get('branch_id')
    supplier = request.form.get('supplier', '').strip()
    amount   = float(request.form.get('amount', 0) or 0)
    p_date   = request.form.get('date') or date.today().isoformat()
    inv_num  = request.form.get('invoice_number', '').strip()
    note     = request.form.get('note', '').strip()
    if not supplier or not branch_id or amount <= 0:
        flash('Заполните поставщика, филиал и сумму', 'danger')
        return redirect(url_for('purchases'))
    with get_db() as conn:
        conn.execute(
            'INSERT INTO purchases (branch_id, supplier, amount, date, invoice_number, note, created_by) VALUES (?,?,?,?,?,?,?)',
            (branch_id, supplier, amount, p_date, inv_num or None, note or None, session['user_id'])
        )
        conn.execute('INSERT OR IGNORE INTO purchase_suppliers (name) VALUES (?)', (supplier,))
        conn.commit()
    flash('Накладная добавлена', 'success')
    return redirect(url_for('purchases'))


@app.route('/purchases/<int:pid>/edit', methods=['POST'])
@login_required
def purchases_edit(pid):
    role = session.get('role')
    supplier = request.form.get('supplier', '').strip()
    amount   = float(request.form.get('amount', 0) or 0)
    p_date   = request.form.get('date') or date.today().isoformat()
    inv_num  = request.form.get('invoice_number', '').strip()
    note     = request.form.get('note', '').strip()
    if role == 'owner':
        branch_id = request.form.get('branch_id', type=int)
    else:
        branch_id = None
    with get_db() as conn:
        row = conn.execute('SELECT * FROM purchases WHERE id=?', (pid,)).fetchone()
        if not row:
            flash('Запись не найдена', 'danger')
            return redirect(url_for('purchases'))
        if role != 'owner' and row['branch_id'] not in _session_branch_ids():
            flash('Нет доступа', 'danger')
            return redirect(url_for('purchases'))
        bid = branch_id if (role == 'owner' and branch_id) else row['branch_id']
        conn.execute(
            'UPDATE purchases SET branch_id=?, supplier=?, amount=?, date=?, invoice_number=?, note=? WHERE id=?',
            (bid, supplier, amount, p_date, inv_num or None, note or None, pid)
        )
        conn.execute('INSERT OR IGNORE INTO purchase_suppliers (name) VALUES (?)', (supplier,))
        conn.commit()
    flash('Накладная обновлена', 'success')
    return redirect(url_for('purchases'))


@app.route('/purchases/<int:pid>/delete', methods=['POST'])
@login_required
def purchases_delete(pid):
    with get_db() as conn:
        row = conn.execute('SELECT * FROM purchases WHERE id=?', (pid,)).fetchone()
        if not row:
            flash('Запись не найдена', 'danger')
            return redirect(url_for('purchases'))
        if session.get('role') != 'owner' and row['branch_id'] not in _session_branch_ids():
            flash('Нет доступа', 'danger')
            return redirect(url_for('purchases'))
        conn.execute('DELETE FROM purchases WHERE id=?', (pid,))
        conn.commit()
    flash('Накладная удалена', 'success')
    return redirect(url_for('purchases'))


# ─── PURCHASES EXCEL IMPORT ───────────────────────────────────────────────────

def _xls_parse_invoices(file_bytes):
    """Parse .xls (BIFF8) invoices file. Returns list of row dicts."""
    import struct
    from datetime import datetime as _dt, timedelta as _td

    data = file_bytes

    def _xl_date(v):
        try:
            return (_dt(1899, 12, 30) + _td(days=float(v))).strftime('%Y-%m-%d')
        except Exception:
            return ''

    # ---- find SST and worksheet BOF offsets ----
    def _scan_bof(data):
        results = []
        for i in range(0, len(data) - 8):
            if data[i] == 0x09 and data[i+1] == 0x08:
                rlen = struct.unpack_from('<H', data, i+2)[0]
                if rlen in (8, 16):
                    ver  = struct.unpack_from('<H', data, i+4)[0]
                    typ  = struct.unpack_from('<H', data, i+6)[0]
                    results.append((i, ver, typ))
        return results

    bofs = _scan_bof(data)
    wb_offset = next((o for o, v, t in bofs if t == 0x0005), None)
    ws_offset = next((o for o, v, t in bofs if t == 0x0010), None)
    if wb_offset is None or ws_offset is None:
        raise ValueError('Не найдена структура XLS-файла')

    # ---- parse SST ----
    def _parse_sst(data, start):
        sst = []; offset = start
        while offset < len(data) - 4:
            rtype = struct.unpack_from('<H', data, offset)[0]
            rlen  = struct.unpack_from('<H', data, offset+2)[0]
            rdata = data[offset+4:offset+4+rlen]; offset += 4 + rlen
            if rtype == 0x00FC:
                unique = struct.unpack_from('<I', rdata, 4)[0]; pos = 8
                for _ in range(unique):
                    if pos + 3 > len(rdata): break
                    n = struct.unpack_from('<H', rdata, pos)[0]
                    flags = rdata[pos+2]; pos += 3
                    is_uni = flags & 1; grbit_run = (flags >> 3) & 1
                    if is_uni: s = rdata[pos:pos+n*2].decode('utf-16-le', errors='replace'); pos += n*2
                    else:      s = rdata[pos:pos+n].decode('cp1251', errors='replace'); pos += n
                    if grbit_run:
                        nr = struct.unpack_from('<H', rdata, pos)[0] if pos+2<=len(rdata) else 0
                        pos += 2 + nr*4
                    sst.append(s)
                break
            if rtype == 0x000A: break
        return sst

    # ---- parse worksheet cells ----
    def _parse_ws(data, start, sst):
        cells = {}; offset = start
        while offset < len(data) - 4:
            rtype = struct.unpack_from('<H', data, offset)[0]
            rlen  = struct.unpack_from('<H', data, offset+2)[0]
            rdata = data[offset+4:offset+4+rlen]; offset += 4 + rlen
            if rtype == 0x00FD and len(rdata) >= 10:
                r = struct.unpack_from('<H', rdata, 0)[0]; c = struct.unpack_from('<H', rdata, 2)[0]
                idx = struct.unpack_from('<I', rdata, 6)[0]
                cells[(r, c)] = sst[idx] if idx < len(sst) else ''
            elif rtype == 0x0203 and len(rdata) >= 14:
                r = struct.unpack_from('<H', rdata, 0)[0]; c = struct.unpack_from('<H', rdata, 2)[0]
                cells[(r, c)] = struct.unpack_from('<d', rdata, 6)[0]
            elif rtype == 0x027E and len(rdata) >= 10:
                r = struct.unpack_from('<H', rdata, 0)[0]; c = struct.unpack_from('<H', rdata, 2)[0]
                rk = struct.unpack_from('<I', rdata, 6)[0]; flt = rk & 2; div = rk & 1
                val = float(rk >> 2) if flt else struct.unpack_from('<d', b'\x00\x00\x00\x00' + struct.pack('<I', rk & 0xFFFFFFFC))[0]
                if div: val /= 100.0
                cells[(r, c)] = val
            elif rtype == 0x00BD and len(rdata) >= 6:
                r = struct.unpack_from('<H', rdata, 0)[0]; c0 = struct.unpack_from('<H', rdata, 2)[0]
                for k in range((len(rdata) - 6) // 6):
                    rk = struct.unpack_from('<I', rdata, 4 + k*6 + 2)[0]; flt = rk & 2; div = rk & 1
                    val = float(rk >> 2) if flt else struct.unpack_from('<d', b'\x00\x00\x00\x00' + struct.pack('<I', rk & 0xFFFFFFFC))[0]
                    if div: val /= 100.0
                    cells[(r, c0 + k)] = val
            elif rtype == 0x000A: break
        return cells

    sst   = _parse_sst(data, wb_offset)
    cells = _parse_ws(data, ws_offset, sst)
    if not cells:
        raise ValueError('Данные не найдены в файле')

    max_row = max(r for r, c in cells)

    def cv(r, c):
        v = cells.get((r, c), '')
        return str(v).strip() if v != '' else ''

    rows = []
    for r in range(1, max_row + 1):
        date_str   = _xl_date(cv(r, 1))   # col 2 → index 1
        inv_num    = cv(r, 3)              # col 4 → index 3
        branch_raw = cv(r, 8)             # col 9 → index 8
        supp_raw   = cv(r, 10)            # col 11 → index 10
        payer_raw  = cv(r, 13)            # col 14 → index 13
        amt_raw    = cv(r, 17)            # col 18 → index 17

        if not date_str or not branch_raw:
            continue
        try:
            amount = float(amt_raw) if amt_raw else 0.0
        except ValueError:
            amount = 0.0
        if amount <= 0:
            continue

        rows.append({
            'date':         date_str,
            'invoice_number': inv_num,
            'branch_raw':   branch_raw,
            'supplier_raw': supp_raw,
            'payer_raw':    payer_raw,
            'amount':       amount,
        })
    return rows


def _xlsx_parse_invoices(file_bytes):
    """Parse .xlsx invoices file."""
    import openpyxl, io as _io
    from datetime import datetime as _dt

    wb = openpyxl.load_workbook(_io.BytesIO(file_bytes), data_only=True)
    ws = wb.active
    rows = []
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:
            continue
        try:
            date_v   = row[1]
            inv_num  = str(row[3] or '').strip()
            branch_r = str(row[8] or '').strip()
            supp_r   = str(row[10] or '').strip()
            payer_r  = str(row[13] or '').strip()
            amt_v    = row[17]

            if isinstance(date_v, _dt):
                date_str = date_v.strftime('%Y-%m-%d')
            elif date_v:
                date_str = _parse_date_str(str(date_v))
            else:
                continue

            amount = float(amt_v or 0)
            if amount <= 0 or not branch_r:
                continue
            rows.append({
                'date': date_str, 'invoice_number': inv_num,
                'branch_raw': branch_r, 'supplier_raw': supp_r,
                'payer_raw': payer_r, 'amount': amount,
            })
        except Exception:
            continue
    return rows


@app.route('/purchases/import-excel', methods=['GET', 'POST'])
@login_required
@owner_required
def purchases_import_excel():
    import json, uuid, os

    with get_db() as conn:
        branches    = conn.execute('SELECT id, name FROM branches WHERE is_active=1 ORDER BY name').fetchall()
        contractors = conn.execute('SELECT id, name FROM contractors WHERE is_active=1 ORDER BY name').fetchall()

    if request.method == 'GET':
        return render_template('purchases_import_excel.html',
            step=1, branches=branches, contractors=contractors)

    file = request.files.get('excel_file')
    if not file or file.filename == '':
        flash('Выберите файл', 'danger')
        return render_template('purchases_import_excel.html', step=1, branches=branches, contractors=contractors)

    file_bytes = file.read()
    fname = file.filename.lower()

    try:
        if fname.endswith('.xls'):
            rows = _xls_parse_invoices(file_bytes)
        elif fname.endswith('.xlsx'):
            rows = _xlsx_parse_invoices(file_bytes)
        else:
            flash('Поддерживаются только .xls и .xlsx файлы', 'danger')
            return render_template('purchases_import_excel.html', step=1, branches=branches, contractors=contractors)
    except Exception as e:
        flash(f'Ошибка чтения файла: {e}', 'danger')
        return render_template('purchases_import_excel.html', step=1, branches=branches, contractors=contractors)

    if not rows:
        flash('Не найдено строк с данными (сумма > 0 и указан филиал)', 'warning')
        return render_template('purchases_import_excel.html', step=1, branches=branches, contractors=contractors)

    unique_branches  = sorted(set(r['branch_raw']  for r in rows if r['branch_raw']))
    unique_suppliers = sorted(set(r['supplier_raw'] for r in rows if r['supplier_raw']))
    unique_payers    = sorted(set(r['payer_raw']    for r in rows if r['payer_raw']))

    import hashlib

    def _row_hash(row):
        key = f"{row['date']}|{row['invoice_number']}|{row['supplier_raw']}|{row['amount']:.4f}"
        return hashlib.md5(key.encode('utf-8')).hexdigest()

    hashes = [_row_hash(r) for r in rows]

    with get_db() as conn:
        existing_hashes = set()
        for h in hashes:
            if conn.execute('SELECT 1 FROM purchases WHERE import_hash=?', (h,)).fetchone():
                existing_hashes.add(h)
    already_count = len(existing_hashes)

    import_key = str(uuid.uuid4())
    temp_path  = f'/tmp/crm_inv_{import_key}.json'
    with open(temp_path, 'w', encoding='utf-8') as f:
        json.dump({
            'rows': rows, 'filename': file.filename,
            'unique_branches': unique_branches,
            'unique_suppliers': unique_suppliers,
            'unique_payers': unique_payers,
        }, f, ensure_ascii=False)
    session['inv_import_key'] = import_key

    date_min = min(r['date'] for r in rows)
    date_max = max(r['date'] for r in rows)

    return render_template('purchases_import_excel.html',
        step=2, branches=branches, contractors=contractors,
        preview=rows[:15], total_rows=len(rows),
        already_count=already_count,
        unique_branches=unique_branches,
        unique_suppliers=unique_suppliers,
        unique_payers=unique_payers,
        filename=file.filename,
        date_min=date_min, date_max=date_max)


@app.route('/purchases/import-excel/confirm', methods=['POST'])
@login_required
@owner_required
def purchases_import_excel_confirm():
    import json, os, hashlib

    import_key = session.get('inv_import_key')
    if not import_key:
        flash('Сессия истекла, загрузите файл заново', 'danger')
        return redirect(url_for('purchases_import_excel'))

    temp_path = f'/tmp/crm_inv_{import_key}.json'
    try:
        with open(temp_path, 'r', encoding='utf-8') as f:
            idata = json.load(f)
    except FileNotFoundError:
        flash('Данные не найдены, загрузите файл заново', 'danger')
        return redirect(url_for('purchases_import_excel'))

    rows             = idata['rows']
    unique_branches  = idata['unique_branches']
    unique_suppliers = idata['unique_suppliers']
    unique_payers    = idata['unique_payers']

    branch_map = {}
    for raw in unique_branches:
        val = request.form.get(f'branch_{raw}', '').strip()
        if val.isdigit():
            branch_map[raw] = int(val)

    supplier_map = {}
    for raw in unique_suppliers:
        val = request.form.get(f'supplier_{raw}', '').strip()
        supplier_map[raw] = val if val else raw

    include_payers = set()
    for raw in unique_payers:
        if request.form.get(f'payer_{raw}'):
            include_payers.add(raw)

    def _row_hash(row):
        key = f"{row['date']}|{row['invoice_number']}|{row['supplier_raw']}|{row['amount']:.4f}"
        return hashlib.md5(key.encode('utf-8')).hexdigest()

    imported = skipped = duplicates = 0
    with get_db() as conn:
        for row in rows:
            payer = row['payer_raw']
            if unique_payers and payer not in include_payers:
                skipped += 1
                continue
            branch_id = branch_map.get(row['branch_raw'])
            if not branch_id:
                skipped += 1
                continue
            supplier = supplier_map.get(row['supplier_raw'], row['supplier_raw'])
            if not supplier:
                skipped += 1
                continue

            h = _row_hash(row)
            existing = conn.execute('SELECT id FROM purchases WHERE import_hash=?', (h,)).fetchone()
            if existing:
                duplicates += 1
                continue

            conn.execute(
                'INSERT INTO purchases (branch_id, supplier, amount, date, invoice_number, payer, import_hash, created_by) VALUES (?,?,?,?,?,?,?,?)',
                (branch_id, supplier, row['amount'], row['date'],
                 row['invoice_number'] or None, payer or None, h, session.get('user_id'))
            )
            conn.execute('INSERT OR IGNORE INTO purchase_suppliers (name) VALUES (?)', (supplier,))
            if not conn.execute('SELECT id FROM contractors WHERE LOWER(name)=LOWER(?)', (supplier,)).fetchone():
                conn.execute('INSERT INTO contractors (name, keywords) VALUES (?,?)', (supplier, supplier))
            imported += 1
        conn.commit()

    try:
        os.remove(temp_path)
    except Exception:
        pass
    session.pop('inv_import_key', None)

    parts = [f'Импортировано {imported} накладных']
    if duplicates:
        parts.append(f'пропущено дублей: {duplicates}')
    if skipped:
        parts.append(f'пропущено без маппинга: {skipped}')
    flash(', '.join(parts), 'success' if imported > 0 else 'warning')
    return redirect(url_for('purchases'))


@app.route('/report/reconciliation')
@login_required
@owner_required
def report_reconciliation():
    today      = date.today().isoformat()
    month_start = date.today().replace(day=1).isoformat()
    date_from      = request.args.get('date_from', month_start)
    date_to        = request.args.get('date_to',   today)
    contractor_id  = request.args.get('contractor_id', '').strip()

    with get_db() as conn:
        contractors = conn.execute(
            'SELECT id, name FROM contractors WHERE is_active=1 ORDER BY name'
        ).fetchall()

        contractor = None
        rows = []
        opening_balance = 0.0
        total_debit = 0.0
        total_credit = 0.0

        if contractor_id.isdigit():
            contractor = conn.execute(
                'SELECT * FROM contractors WHERE id=?', (int(contractor_id),)
            ).fetchone()

        if contractor:
            # Сальдо начальное: накладные и оплаты ДО начала периода
            ob_purchases = conn.execute('''
                SELECT COALESCE(SUM(amount), 0)
                FROM purchases
                WHERE LOWER(TRIM(supplier)) = LOWER(TRIM(?))
                  AND date < ?
            ''', (contractor['name'], date_from)).fetchone()[0] or 0.0

            ob_payments = conn.execute('''
                SELECT COALESCE(SUM(amount), 0)
                FROM bank_transactions
                WHERE contractor_id = ?
                  AND txn_date < ?
                  AND is_ignored = 0
            ''', (contractor['id'], date_from)).fetchone()[0] or 0.0

            # payments хранятся как отрицательные (расход), поэтому суммируем
            opening_balance = ob_purchases + ob_payments

            # Накладные за период → Дебет (нам поставили, мы должны)
            purchases_rows = conn.execute('''
                SELECT p.date, p.invoice_number, p.supplier, p.amount,
                       b.name AS branch_name
                FROM purchases p
                JOIN branches b ON b.id = p.branch_id
                WHERE LOWER(TRIM(p.supplier)) = LOWER(TRIM(?))
                  AND p.date BETWEEN ? AND ?
                ORDER BY p.date, p.id
            ''', (contractor['name'], date_from, date_to)).fetchall()

            # Оплаты из банковских выписок → Кредит (мы заплатили)
            payment_rows = conn.execute('''
                SELECT bt.txn_date AS date, bt.description,
                       bt.counterparty, bt.amount,
                       ba.name AS account_name
                FROM bank_transactions bt
                JOIN bank_accounts ba ON ba.id = bt.bank_account_id
                WHERE bt.contractor_id = ?
                  AND bt.txn_date BETWEEN ? AND ?
                  AND bt.is_ignored = 0
                ORDER BY bt.txn_date, bt.id
            ''', (contractor['id'], date_from, date_to)).fetchall()

            for r in purchases_rows:
                rows.append({
                    'date': r['date'],
                    'doc': 'Накладная №' + (r['invoice_number'] or '—') + ' (' + r['branch_name'] + ')',
                    'debit': r['amount'],
                    'credit': None,
                })
                total_debit += r['amount']

            for r in payment_rows:
                amt = abs(r['amount'])
                desc = r['description'] or r['counterparty'] or 'Оплата'
                rows.append({
                    'date': r['date'],
                    'doc': desc,
                    'debit': None,
                    'credit': amt,
                })
                total_credit += amt

            rows.sort(key=lambda x: x['date'])

    # Сальдо конечное: сальдо начальное + обороты по дебету − обороты по кредиту
    closing_balance = opening_balance + total_debit - total_credit

    return render_template('report_reconciliation.html',
        contractors=contractors,
        contractor=contractor,
        contractor_id=contractor_id,
        rows=rows,
        opening_balance=opening_balance,
        total_debit=total_debit,
        total_credit=total_credit,
        closing_balance=closing_balance,
        date_from=date_from,
        date_to=date_to)


@app.route('/report/expenses')
@login_required
@owner_required
def expenses_report():
    today = date.today().isoformat()
    month_start = date.today().replace(day=1).isoformat()
    date_from   = request.args.get('date_from', month_start)
    date_to     = request.args.get('date_to', today)
    branch_ids  = [b for b in request.args.getlist('branch_ids') if b.isdigit()]
    cat_filter  = request.args.get('category', '')
    pay_filter  = request.args.get('pay_type', '')   # 'cash' | 'card' | ''

    with get_db() as conn:
        branches = conn.execute('SELECT * FROM branches WHERE is_active=1 ORDER BY name').fetchall()
        all_cats = conn.execute('SELECT * FROM expense_categories ORDER BY sort_order, label').fetchall()

        conds  = ["s.date BETWEEN ? AND ?"]
        params = [date_from, date_to]
        if branch_ids:
            ph = ','.join('?' * len(branch_ids))
            conds.append(f's.branch_id IN ({ph})')
            params.extend(int(b) for b in branch_ids)
        if cat_filter:
            conds.append('e.category = ?')
            params.append(cat_filter)
        if pay_filter == 'cash':
            conds.append('e.amount_cash > 0')
        elif pay_filter == 'card':
            conds.append('e.amount_card > 0')
        where = ' AND '.join(conds)

        rows = conn.execute(f'''
            SELECT e.id, s.date, s.id as shift_id,
                   b.name as branch_name,
                   e.category, e.description,
                   COALESCE(e.amount_cash,0) as amount_cash,
                   COALESCE(e.amount_card,0) as amount_card,
                   COALESCE(e.amount_cash,0)+COALESCE(e.amount_card,0) as total,
                   e.is_gulash
            FROM expenses e
            JOIN shifts s ON s.id = e.shift_id
            JOIN branches b ON b.id = s.branch_id
            WHERE {where}
            ORDER BY s.date DESC, b.name, e.id
        ''', params).fetchall()

        tot = conn.execute(f'''
            SELECT COALESCE(SUM(e.amount_cash),0) as cash,
                   COALESCE(SUM(e.amount_card),0) as card,
                   COALESCE(SUM(e.amount_cash+e.amount_card),0) as total
            FROM expenses e
            JOIN shifts s ON s.id = e.shift_id
            WHERE {where}
        ''', params).fetchone()

        by_cat = conn.execute(f'''
            SELECT e.category,
                   COALESCE(SUM(e.amount_cash),0) as cash,
                   COALESCE(SUM(e.amount_card),0) as card,
                   COALESCE(SUM(e.amount_cash+e.amount_card),0) as total,
                   COUNT(*) as cnt
            FROM expenses e
            JOIN shifts s ON s.id = e.shift_id
            WHERE {where}
            GROUP BY e.category
            ORDER BY total DESC
        ''', params).fetchall()

    cat_map = {c['code']: c['label'] for c in all_cats}

    return render_template('expenses_report.html',
        rows=rows, tot=tot, by_cat=by_cat,
        branches=branches, all_cats=all_cats, cat_map=cat_map,
        date_from=date_from, date_to=date_to,
        branch_ids=branch_ids, cat_filter=cat_filter, pay_filter=pay_filter,
        branch_groups=get_branch_groups(conn))


# ─── PnL REPORT ───────────────────────────────────────────────────────────────

import json as _json

_MONTH_NAMES_RU = {
    1: 'Янв', 2: 'Фев', 3: 'Мар', 4: 'Апр',
    5: 'Май', 6: 'Июн', 7: 'Июл', 8: 'Авг',
    9: 'Сен', 10: 'Окт', 11: 'Ноя', 12: 'Дек',
}


def _pnl_period_label(p):
    if p == 'total':
        return 'Итого'
    try:
        dt = datetime.strptime(p, '%Y-%m')
        return f"{_MONTH_NAMES_RU[dt.month]} {dt.year}"
    except Exception:
        return p



def _pnl_load_settings(conn):
    rows = conn.execute('SELECT key, value FROM pnl_settings').fetchall()
    cfg  = {r['key']: r['value'] for r in rows}

    def _lst(key, default):
        raw = cfg.get(key)
        if raw is None:
            return default
        try:
            return _json.loads(raw)
        except Exception:
            return default

    default_cash_exp = [c['code'] for c in conn.execute(
        "SELECT code FROM expense_categories WHERE is_active=1 AND parent_id IS NULL ORDER BY sort_order, label"
    ).fetchall()]

    bi = _lst('bank_income_ctr_cats',  None)
    be = _lst('bank_expense_ctr_cats', None)
    return {
        'cash_expense_cats':     _lst('cash_expense_cats', default_cash_exp),
        'bank_income_ctr_cats':  bi or None,   # пустой список [] → None (все)
        'bank_expense_ctr_cats': be or None,   # пустой список [] → None (все)
        'include_salary':           int(cfg.get('include_salary', '1')),
        'include_salary_breakdown': int(cfg.get('include_salary_breakdown', '1')),
    }


@app.route('/report/pnl')
@login_required
@owner_required
def pnl_report():
    from collections import defaultdict

    today       = date.today().isoformat()
    month_start = date.today().replace(day=1).isoformat()

    date_from  = request.args.get('date_from', month_start)
    date_to    = request.args.get('date_to', today)
    branch_ids = [b for b in request.args.getlist('branch_ids') if b.isdigit()]
    group_by   = request.args.get('group_by', 'month')

    with get_db() as conn:
        branches      = conn.execute('SELECT * FROM branches WHERE is_active=1 ORDER BY name').fetchall()
        branch_groups = get_branch_groups(conn)
        all_cats      = get_expense_categories(conn)
        all_ctr_cats  = conn.execute('SELECT * FROM contractor_categories ORDER BY sort_order, name').fetchall()
        cfg           = _pnl_load_settings(conn)

        if branch_ids:
            ph    = ','.join('?' * len(branch_ids))
            bf    = f'AND s.branch_id IN ({ph})'
            bf_p  = f'AND p.branch_id IN ({ph})'
            bf_bt = (f'AND bt.bank_account_id IN '
                     f'(SELECT bank_account_id FROM bank_account_branches WHERE branch_id IN ({ph}))')
            b_args = [int(b) for b in branch_ids]
        else:
            bf = bf_p = bf_bt = ''
            b_args = []

        pe    = "strftime('%Y-%m', s.date)"      if group_by == 'month' else "'total'"
        pe_bt = "strftime('%Y-%m', bt.txn_date)" if group_by == 'month' else "'total'"

        # Общая выручка (база для %)
        total_rev_by_p = {}
        for r in conn.execute(f"""
            SELECT {pe} AS period, COALESCE(SUM(r.total_revenue), 0) AS amount
            FROM shifts s LEFT JOIN shift_revenue r ON r.shift_id=s.id
            WHERE s.date BETWEEN ? AND ? {bf}
            GROUP BY period ORDER BY period
        """, [date_from, date_to] + b_args).fetchall():
            total_rev_by_p[r['period']] = r['amount']

        # Наличный приход
        cash_rev_by_p = {}
        for r in conn.execute(f"""
            SELECT {pe} AS period, COALESCE(SUM(r.cash_amount), 0) AS amount
            FROM shifts s LEFT JOIN shift_revenue r ON r.shift_id=s.id
            WHERE s.date BETWEEN ? AND ? {bf}
            GROUP BY period ORDER BY period
        """, [date_from, date_to] + b_args).fetchall():
            cash_rev_by_p[r['period']] = r['amount']

        # Приход из банка по категориям контрагентов
        bank_inc_by = defaultdict(lambda: defaultdict(float))
        _bi_filter = ''
        _bi_args   = []
        if cfg['bank_income_ctr_cats'] is not None:
            if cfg['bank_income_ctr_cats']:
                ph_c = ','.join('?' * len(cfg['bank_income_ctr_cats']))
                _bi_filter = f"AND bt.category IN ({ph_c})"
                _bi_args   = cfg['bank_income_ctr_cats']
            else:
                _bi_filter = 'AND 0=1'  # пустой список = ничего
        for r in conn.execute(f"""
            SELECT {pe_bt} AS period,
                   bt.category AS cat_name,
                   SUM(bt.amount) AS amount
            FROM bank_transactions bt
            WHERE bt.txn_date BETWEEN ? AND ?
              AND bt.amount > 0 AND bt.is_ignored=0
              AND bt.category IS NOT NULL AND bt.category != ''
              {_bi_filter} {bf_bt}
            GROUP BY period, bt.category
            HAVING amount > 0
        """, [date_from, date_to] + _bi_args + b_args).fetchall():
            bank_inc_by[r['cat_name']][r['period']] += r['amount']

        # Наличные расходы по категориям
        cash_exp_by = defaultdict(lambda: defaultdict(float))
        if cfg['cash_expense_cats']:
            ph_c = ','.join('?' * len(cfg['cash_expense_cats']))
            for r in conn.execute(f"""
                SELECT {pe} AS period, e.category AS cat,
                       COALESCE(SUM(e.amount_cash), 0) AS amount
                FROM expenses e JOIN shifts s ON s.id=e.shift_id
                WHERE s.date BETWEEN ? AND ?
                  AND e.category IN ({ph_c}) {bf}
                GROUP BY period, e.category
            """, [date_from, date_to] + cfg['cash_expense_cats'] + b_args).fetchall():
                cash_exp_by[r['cat']][r['period']] += r['amount']

        # Расходы из банка по категориям контрагентов
        bank_exp_by = defaultdict(lambda: defaultdict(float))
        _be_filter = ''
        _be_args   = []
        if cfg['bank_expense_ctr_cats'] is not None:
            if cfg['bank_expense_ctr_cats']:
                ph_c = ','.join('?' * len(cfg['bank_expense_ctr_cats']))
                _be_filter = f"AND bt.category IN ({ph_c})"
                _be_args   = cfg['bank_expense_ctr_cats']
            else:
                _be_filter = 'AND 0=1'
        for r in conn.execute(f"""
            SELECT {pe_bt} AS period,
                   bt.category AS cat_name,
                   SUM(-bt.amount) AS amount
            FROM bank_transactions bt
            WHERE bt.txn_date BETWEEN ? AND ?
              AND bt.amount < 0 AND bt.is_ignored=0
              AND bt.category IS NOT NULL AND bt.category != ''
              {_be_filter} {bf_bt}
            GROUP BY period, bt.category
            HAVING amount > 0
        """, [date_from, date_to] + _be_args + b_args).fetchall():
            bank_exp_by[r['cat_name']][r['period']] += r['amount']

        # Диагностика банковских данных
        _bt_debug = conn.execute("""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN category IS NOT NULL AND category!='' THEN 1 ELSE 0 END) AS with_cat,
                   SUM(CASE WHEN amount>0 AND is_ignored=0 THEN 1 ELSE 0 END) AS income_rows,
                   SUM(CASE WHEN amount<0 AND is_ignored=0 THEN 1 ELSE 0 END) AS expense_rows,
                   SUM(CASE WHEN category IS NOT NULL AND category!='' AND is_ignored=0 AND amount<0 THEN 1 ELSE 0 END) AS cat_exp_active,
                   SUM(CASE WHEN category IS NOT NULL AND category!='' AND is_ignored=0 AND amount>0 THEN 1 ELSE 0 END) AS cat_inc_active,
                   SUM(CASE WHEN category IS NOT NULL AND category!='' AND is_ignored=1 THEN 1 ELSE 0 END) AS cat_ignored
            FROM bank_transactions
            WHERE txn_date BETWEEN ? AND ?
        """, [date_from, date_to]).fetchone()
        bt_debug = dict(_bt_debug) if _bt_debug else {}
        _bt_all = conn.execute("""
            SELECT COUNT(*) AS total_all,
                   MIN(txn_date) AS min_date, MAX(txn_date) AS max_date,
                   SUM(CASE WHEN category IS NOT NULL AND category!='' THEN 1 ELSE 0 END) AS with_cat_all
            FROM bank_transactions
        """).fetchone()
        bt_debug.update(dict(_bt_all) if _bt_all else {})
        _bt_cats = conn.execute("""
            SELECT DISTINCT category FROM bank_transactions
            WHERE category IS NOT NULL AND category != '' LIMIT 10
        """).fetchall()
        bt_debug['sample_cats'] = [r['category'] for r in _bt_cats]
        _ctr_cats = conn.execute("SELECT name FROM contractor_categories ORDER BY sort_order, name").fetchall()
        bt_debug['ctr_cats'] = [r['name'] for r in _ctr_cats]

        # ФОТ по ролям
        sal_by_role = defaultdict(lambda: defaultdict(float))
        if cfg['include_salary']:
            for r in conn.execute(f"""
                SELECT {pe} AS period,
                       COALESCE(es.role_snapshot, 'other') AS role,
                       COALESCE(SUM(es.total_amount), 0) AS amount
                FROM employee_shifts es JOIN shifts s ON s.id=es.shift_id
                WHERE s.date BETWEEN ? AND ? {bf}
                GROUP BY period, role
            """, [date_from, date_to] + b_args).fetchall():
                sal_by_role[r['role']][r['period']] += r['amount']

    # Все периоды
    all_p = set(total_rev_by_p) | set(cash_rev_by_p)
    for d in list(bank_inc_by.values()) + list(cash_exp_by.values()) + list(bank_exp_by.values()):
        all_p.update(d)
    for d in sal_by_role.values():
        all_p.update(d)
    periods = sorted(all_p) if group_by == 'month' else ['total']

    cat_map = {c['code']: c['label'] for c in all_cats}

    def _row(label, by_p, badge=None, sub=False):
        r = {'label': label, 'amounts': {}, 'total': 0.0, 'badge': badge, 'sub': sub}
        for p in periods:
            v = by_p.get(p, 0.0)
            r['amounts'][p] = v
            r['total'] += v
        return r

    # ── ДОХОДЫ ────────────────────────────────────────────────────────────────
    inc_totals = {}
    inc_rows   = [_row('Наличные', cash_rev_by_p, 'cash')]

    for cat_name, by_p in sorted(bank_inc_by.items()):
        inc_rows.append(_row(cat_name, dict(by_p), 'bank'))

    for row in inc_rows:
        for p in periods:
            inc_totals[p] = inc_totals.get(p, 0.0) + row['amounts'].get(p, 0.0)

    inc_grand = sum(inc_totals.get(p, 0) for p in periods)

    # ── РАСХОДЫ ───────────────────────────────────────────────────────────────
    exp_totals = {}
    exp_rows   = []

    # ФОТ
    if cfg['include_salary']:
        sal_total_by_p = {}
        for role, by_p in sal_by_role.items():
            for p, v in by_p.items():
                sal_total_by_p[p] = sal_total_by_p.get(p, 0.0) + v
        sal_total_row = _row('ФОТ (итого)', sal_total_by_p, 'salary')
        exp_rows.append(sal_total_row)

        if cfg['include_salary_breakdown']:
            role_order = ['admin', 'cook', 'sushi', 'courier', 'packer', 'cleaner']
            for role in role_order:
                if role in sal_by_role:
                    exp_rows.append(_row(ROLE_LABELS.get(role, role), dict(sal_by_role[role]), 'salary', sub=True))
            # прочие роли не в списке
            for role, by_p in sal_by_role.items():
                if role not in role_order:
                    exp_rows.append(_row(ROLE_LABELS.get(role, role), dict(by_p), 'salary', sub=True))

    # Наличные расходы по категориям
    for cat_code in cfg['cash_expense_cats']:
        by_p = cash_exp_by.get(cat_code, {})
        if any(v > 0 for v in by_p.values()):
            exp_rows.append(_row(cat_map.get(cat_code, cat_code), dict(by_p), 'cash'))

    # Банковские расходы по категориям контрагентов
    for cat_name, by_p in sorted(bank_exp_by.items()):
        exp_rows.append(_row(cat_name, dict(by_p), 'bank'))

    for row in exp_rows:
        if not row['sub']:
            for p in periods:
                exp_totals[p] = exp_totals.get(p, 0.0) + row['amounts'].get(p, 0.0)

    exp_grand = sum(exp_totals.get(p, 0) for p in periods)

    profit_by_p   = {p: inc_totals.get(p, 0) - exp_totals.get(p, 0) for p in periods}
    profit_grand  = inc_grand - exp_grand
    total_rev_grand = sum(total_rev_by_p.get(p, 0) for p in periods)
    period_labels = {p: _pnl_period_label(p) for p in periods}

    return render_template('pnl.html',
        periods=periods, period_labels=period_labels,
        inc_rows=inc_rows, inc_totals=inc_totals, inc_grand=inc_grand,
        exp_rows=exp_rows, exp_totals=exp_totals, exp_grand=exp_grand,
        profit_by_p=profit_by_p, profit_grand=profit_grand,
        total_rev_by_p=total_rev_by_p, total_rev_grand=total_rev_grand,
        cfg=cfg,
        branches=branches, branch_groups=branch_groups,
        all_cats=all_cats, cat_map=cat_map,
        all_ctr_cats=all_ctr_cats,
        role_labels=ROLE_LABELS,
        date_from=date_from, date_to=date_to,
        branch_ids=branch_ids, group_by=group_by,
        bt_debug=bt_debug,
    )


@app.route('/report/pnl/settings', methods=['POST'])
@login_required
@owner_required
def pnl_settings_save():
    bank_inc_all = request.form.get('bank_income_all')
    bank_exp_all = request.form.get('bank_expense_all')
    bank_income_val  = None if bank_inc_all else request.form.getlist('bank_income_ctr_cats')
    bank_expense_val = None if bank_exp_all else request.form.getlist('bank_expense_ctr_cats')

    with get_db() as conn:
        for key, val in [
            ('cash_expense_cats',       _json.dumps(request.form.getlist('cash_expense_cats'))),
            ('bank_income_ctr_cats',    _json.dumps(bank_income_val)),
            ('bank_expense_ctr_cats',   _json.dumps(bank_expense_val)),
            ('include_salary',          '1' if request.form.get('include_salary') else '0'),
            ('include_salary_breakdown','1' if request.form.get('include_salary_breakdown') else '0'),
        ]:
            conn.execute('INSERT OR REPLACE INTO pnl_settings (key, value) VALUES (?, ?)', (key, val))
        conn.commit()

    flash('Настройки P&L сохранены', 'success')
    params = {k: request.form.get(k) for k in ('date_from', 'date_to', 'group_by') if request.form.get(k)}
    bids = request.form.getlist('branch_ids')
    if bids:
        params['branch_ids'] = bids
    return redirect(url_for('pnl_report', **params))



# ─── GSHEET SETTINGS ──────────────────────────────────────────────────────────

@app.route('/settings/gsheet', methods=['GET', 'POST'])
@login_required
@owner_required
def gsheet_settings():
    with get_db() as conn:
        if request.method == 'POST':
            for key, _ in GSHEET_COLS:
                val = '1' if request.form.get(f'col_{key}') else '0'
                conn.execute(
                    'INSERT OR REPLACE INTO gsheet_settings (key, value) VALUES (?,?)',
                    (f'col_{key}', val)
                )
            conn.commit()
            flash('Настройки экспорта сохранены', 'success')
            return redirect(url_for('gsheet_settings'))
        cfg = _gsheet_load_settings(conn)
    return render_template('gsheet_settings.html', cfg=cfg, cols=GSHEET_COLS,
                           sheet_id=os.environ.get('GOOGLE_SHEET_ID', ''),
                           drive_folder_id=os.environ.get('GOOGLE_DRIVE_FOLDER_ID', ''),
                           has_creds=bool(os.environ.get('GOOGLE_CREDENTIALS_JSON')))


# ─── HISTORY ──────────────────────────────────────────────────────────────────

@app.route('/history')
@login_required
def history():
    role = session.get('role')
    today = date.today().isoformat()
    week_ago = (date.today() - timedelta(days=7)).isoformat()
    date_from = request.args.get('date_from', week_ago)
    date_to = request.args.get('date_to', today)
    branch_id = request.args.get('branch_id', '')
    user_filter = request.args.get('user_id', '')
    action_filter = request.args.get('action', '')

    with get_db() as conn:
        conds = ['cl.created_at >= ? AND cl.created_at <= ?']
        params = [date_from + ' 00:00:00', date_to + ' 23:59:59']

        if role != 'owner':
            conds.append('cl.user_id = ?')
            params.append(session['user_id'])
            if session.get('branch_id'):
                conds.append('cl.branch_id = ?')
                params.append(session['branch_id'])
        else:
            if branch_id:
                conds.append('cl.branch_id = ?')
                params.append(int(branch_id))
            if user_filter:
                conds.append('cl.user_id = ?')
                params.append(int(user_filter))

        if action_filter:
            conds.append('cl.action = ?')
            params.append(action_filter)

        where = ' AND '.join(conds)
        logs = conn.execute(f'''
            SELECT cl.*
            FROM change_log cl
            WHERE {where}
            ORDER BY cl.created_at DESC
            LIMIT 300
        ''', params).fetchall()

        branches = []
        users = []
        if role == 'owner':
            branches = conn.execute(
                'SELECT * FROM branches WHERE is_active=1 ORDER BY name'
            ).fetchall()
            users = conn.execute(
                "SELECT id, full_name FROM users WHERE role != 'owner' ORDER BY full_name"
            ).fetchall()

    return render_template('history.html',
        logs=logs, branches=branches, users=users,
        date_from=date_from, date_to=date_to,
        selected_branch=branch_id, selected_user=user_filter,
        selected_action=action_filter,
        action_labels=ACTION_LABELS,
        is_owner=(role == 'owner'),
        branch_groups=get_branch_groups(conn))


# ─── API ──────────────────────────────────────────────────────────────────────

@app.route('/api/employee/<int:emp_id>')
@login_required
def api_employee(emp_id):
    with get_db() as conn:
        emp = conn.execute('SELECT * FROM employees WHERE id=?', (emp_id,)).fetchone()
        if not emp:
            return jsonify({}), 404
        roles = conn.execute(
            'SELECT * FROM employee_roles WHERE employee_id=? AND is_active=1 ORDER BY role',
            (emp_id,)
        ).fetchall()
        return jsonify({
            'id': emp['id'],
            'full_name': emp['full_name'],
            'role': emp['role'],
            'rate': emp['rate'],
            'rate_per_km': emp['rate_per_km'],
            'rate_per_order': emp['rate_per_order'],
            'extra_roles': [
                {'role': r['role'], 'rate': r['rate'],
                 'rate_per_km': r['rate_per_km'], 'rate_per_order': r['rate_per_order']}
                for r in roles
            ],
        })


@app.route('/employees/<int:emp_id>/roles/add', methods=['POST'])
@login_required
@owner_required
def add_employee_role(emp_id):
    role = request.form.get('role', '').strip()
    rate = float(request.form.get('rate', 0) or 0)
    rate_km  = float(request.form.get('rate_per_km', 10) or 10)
    rate_ord = float(request.form.get('rate_per_order', 100) or 100)
    if not role or role not in ROLE_LABELS:
        flash('Выберите должность', 'danger')
        return redirect(url_for('employees'))
    with get_db() as conn:
        try:
            conn.execute(
                'INSERT INTO employee_roles (employee_id, role, rate, rate_per_km, rate_per_order) VALUES (?,?,?,?,?)',
                (emp_id, role, rate, rate_km, rate_ord)
            )
            conn.commit()
            flash('Должность добавлена', 'success')
        except Exception:
            conn.execute(
                'UPDATE employee_roles SET rate=?, rate_per_km=?, rate_per_order=? WHERE employee_id=? AND role=?',
                (rate, rate_km, rate_ord, emp_id, role)
            )
            conn.commit()
            flash('Ставка по должности обновлена', 'success')
    return redirect(url_for('employees'))


@app.route('/employees/roles/<int:role_id>/delete', methods=['POST'])
@login_required
@owner_required
def delete_employee_role(role_id):
    with get_db() as conn:
        conn.execute('DELETE FROM employee_roles WHERE id=?', (role_id,))
        conn.commit()
    flash('Должность удалена', 'success')
    return redirect(url_for('employees'))


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def _session_branch_ids():
    ids = session.get('branch_ids')
    if ids:
        return ids
    bid = session.get('branch_id')
    return [bid] if bid else []


def _can_edit_shift(shift_id):
    role = session.get('role')
    if role == 'owner':
        return True
    with get_db() as conn:
        shift = conn.execute('SELECT * FROM shifts WHERE id=?', (shift_id,)).fetchone()
        if not shift:
            return False
        return shift['status'] == 'open' and shift['branch_id'] in _session_branch_ids()


def _f(data, key):
    try:
        return float(data.get(key) or 0)
    except (ValueError, TypeError):
        return 0.0


def _i(data, key):
    try:
        return int(data.get(key) or 0)
    except (ValueError, TypeError):
        return 0


def calculate_bonuses(conn, shift_id):
    """Recalculate auto_bonus for all staff in a shift based on bonus_rules."""
    rev = conn.execute(
        'SELECT total_revenue FROM shift_revenue WHERE shift_id=?', (shift_id,)
    ).fetchone()
    total_revenue = float((rev['total_revenue'] or 0) if rev else 0)

    shift = conn.execute('SELECT branch_id FROM shifts WHERE id=?', (shift_id,)).fetchone()
    branch_id = shift['branch_id'] if shift else None

    staff = conn.execute(
        'SELECT id, role_snapshot, hours_worked, base_pay, bonus_amount, penalty_amount '
        'FROM employee_shifts WHERE shift_id=?', (shift_id,)
    ).fetchall()
    if not staff:
        return []

    rules = conn.execute(
        'SELECT role, threshold_pct, bonus_pct FROM bonus_rules '
        'WHERE is_active=1 AND (branch_id IS NULL OR branch_id=?) '
        'ORDER BY threshold_pct ASC', (branch_id,)
    ).fetchall()

    rules_by_role = {}
    for r in rules:
        rules_by_role.setdefault(r['role'], []).append((r['threshold_pct'], r['bonus_pct']))

    staff_by_role = {}
    for s in staff:
        staff_by_role.setdefault(s['role_snapshot'], []).append(s)

    auto_bonus_per_id = {}
    for role, role_rules in rules_by_role.items():
        role_staff = staff_by_role.get(role, [])
        if not role_staff or total_revenue <= 0:
            continue
        role_payroll = sum(float(s['base_pay'] or 0) for s in role_staff)
        payroll_pct = role_payroll / total_revenue * 100
        applicable_pct = 0
        for threshold, bonus_pct in role_rules:
            if payroll_pct < threshold:
                applicable_pct = bonus_pct
                break
        if applicable_pct <= 0:
            continue
        bonus_pool = total_revenue * applicable_pct / 100
        total_hours = sum(float(s['hours_worked'] or 0) for s in role_staff)
        for s in role_staff:
            h = float(s['hours_worked'] or 0)
            auto_bonus_per_id[s['id']] = round(bonus_pool * h / total_hours) if total_hours > 0 else 0

    results = []
    for s in staff:
        ab = auto_bonus_per_id.get(s['id'], 0)
        new_total = round(float(s['base_pay'] or 0) + float(s['bonus_amount'] or 0) + ab - float(s['penalty_amount'] or 0))
        conn.execute('UPDATE employee_shifts SET auto_bonus=?, total_amount=? WHERE id=?', (ab, new_total, s['id']))
        results.append({'id': s['id'], 'auto_bonus': ab, 'total_amount': new_total})
    return results


def _fmt_money(v):
    try:
        return f"{float(v or 0):,.0f}".replace(',', ' ') + ' ₽'
    except Exception:
        return '0 ₽'


def log_action(conn, action, description, shift_id=None, entity_id=None, upsert_by_shift=False):
    if not session.get('user_id'):
        return
    branch_id = branch_name = shift_date = None
    if shift_id:
        row = conn.execute(
            'SELECT s.branch_id, b.name, s.date FROM shifts s JOIN branches b ON b.id=s.branch_id WHERE s.id=?',
            (shift_id,)
        ).fetchone()
        if row:
            branch_id, branch_name, shift_date = row['branch_id'], row['name'], row['date']
    if upsert_by_shift and shift_id:
        existing = conn.execute(
            'SELECT id FROM change_log WHERE shift_id=? AND action=? ORDER BY id DESC LIMIT 1',
            (shift_id, action)
        ).fetchone()
        if existing:
            conn.execute(
                'UPDATE change_log SET description=?, user_id=?, user_name=?, created_at=CURRENT_TIMESTAMP WHERE id=?',
                (description, session['user_id'], session.get('full_name', ''), existing['id'])
            )
            return
    conn.execute('''
        INSERT INTO change_log
            (user_id, user_name, action, entity_id, shift_id, branch_id, branch_name, shift_date, description)
        VALUES (?,?,?,?,?,?,?,?,?)
    ''', (session['user_id'], session.get('full_name', ''), action, entity_id,
          shift_id, branch_id, branch_name, shift_date, description))


@app.template_filter('money')
def money_filter(value):
    try:
        return '{:,.0f}'.format(float(value or 0)).replace(',', ' ')
    except Exception:
        return str(value)


@app.template_filter('datetime_fmt')
def datetime_fmt(value):
    if not value:
        return ''
    try:
        dt = datetime.strptime(str(value)[:19], '%Y-%m-%d %H:%M:%S')
        return dt.strftime('%d.%m %H:%M')
    except Exception:
        return value


@app.template_filter('date_fmt')
def date_fmt(value):
    if not value:
        return ''
    try:
        dt = datetime.strptime(str(value)[:10], '%Y-%m-%d')
        return dt.strftime('%d.%m.%Y')
    except Exception:
        return value


@app.context_processor
def inject_globals():
    return {'now': datetime.now}


# ─── SHIFTS ARCHIVE ───────────────────────────────────────────────────────────

@app.route('/shifts')
@login_required
def shifts_archive():
    role = session.get('role')
    today = date.today().isoformat()
    month_start = date.today().replace(day=1).isoformat()

    date_from  = request.args.get('date_from', month_start)
    date_to    = request.args.get('date_to',   today)
    branch_ids = [bid for bid in request.args.getlist('branch_ids') if bid.isdigit()]
    status_filter = request.args.get('status', '')

    with get_db() as conn:
        branches = conn.execute(
            'SELECT * FROM branches WHERE is_active=1 ORDER BY name'
        ).fetchall()

        query = '''
            SELECT s.id, s.date, s.status,
                   b.name as branch_name,
                   COALESCE(r.total_revenue, 0)       as revenue,
                   COALESCE(r.delivery_orders, 0) +
                   COALESCE(r.pickup_orders, 0)        as orders,
                   COALESCE(r.delivery_revenue, 0)     as delivery_revenue,
                   COALESCE(r.pickup_revenue, 0)       as pickup_revenue,
                   COALESCE(r.cash_amount, 0)          as cash_amount,
                   COALESCE(r.card_amount, 0)          as card_amount,
                   COALESCE(
                     NULLIF(r.morning_cash, 0),
                     (SELECT COALESCE(r2.morning_cash,0)+COALESCE(r2.cash_amount,0)
                             +COALESCE(r2.change_amount,0)
                             -COALESCE((SELECT SUM(e2.amount_cash) FROM expenses e2 WHERE e2.shift_id=s2.id),0)
                             -COALESCE((SELECT SUM(es2.total_amount) FROM employee_shifts es2
                                        WHERE es2.shift_id=s2.id AND es2.is_paid=1),0)
                      FROM shifts s2 JOIN shift_revenue r2 ON r2.shift_id=s2.id
                      WHERE s2.branch_id=s.branch_id AND s2.date<s.date
                      ORDER BY s2.date DESC LIMIT 1),
                     0
                   )                                    as morning_cash,
                   COALESCE(r.change_amount, 0)        as change_amount,
                   r.actual_cash,
                   (SELECT COALESCE(SUM(e.amount_cash),0)
                    FROM expenses e WHERE e.shift_id=s.id)  as exp_cash,
                   (SELECT COALESCE(SUM(es.total_amount),0)
                    FROM employee_shifts es
                    WHERE es.shift_id=s.id AND es.is_paid=1) as paid_salary,
                   (SELECT COALESCE(SUM(COALESCE(cp.amount_cash, cp.amount, 0)),0)
                    FROM cash_plus_entries cp WHERE cp.shift_id=s.id) as plus_cash,
                   s.opened_at, s.closed_at,
                   s.closed_by_name
            FROM shifts s
            JOIN branches b ON b.id = s.branch_id
            LEFT JOIN shift_revenue r ON r.shift_id = s.id
            WHERE s.date BETWEEN ? AND ?
        '''
        params = [date_from, date_to]

        if role != 'owner':
            bids = _session_branch_ids()
            if bids:
                ids_str = ','.join(str(int(b)) for b in bids)
                query += f' AND s.branch_id IN ({ids_str})'
        elif branch_ids:
            ids_str = ','.join(str(int(bid)) for bid in branch_ids)
            query += f' AND s.branch_id IN ({ids_str})'

        if status_filter:
            query += ' AND s.status = ?'
            params.append(status_filter)

        query += ' ORDER BY s.date DESC, b.name'
        shifts = conn.execute(query, params).fetchall()

        total_revenue = sum(s['revenue'] for s in shifts)
        total_orders  = sum(s['orders']  for s in shifts)

    return render_template('shifts_archive.html',
        shifts=shifts, branches=branches,
        date_from=date_from, date_to=date_to,
        selected_branches=branch_ids, status_filter=status_filter,
        total_revenue=total_revenue, total_orders=total_orders,
        is_owner=(role == 'owner'),
        branch_groups=get_branch_groups(conn))


# ─── BANK STATEMENTS ──────────────────────────────────────────────────────────

@app.route('/bank/')
@login_required
@owner_required
def bank():
    tab = request.args.get('tab', 'statements')
    date_from = request.args.get('date_from', (date.today().replace(day=1)).isoformat())
    date_to   = request.args.get('date_to', date.today().isoformat())
    with get_db() as conn:
        accounts    = conn.execute('SELECT * FROM bank_accounts WHERE is_active=1 ORDER BY name').fetchall()
        acc_branches = {}
        for row in conn.execute('''
            SELECT bab.bank_account_id, b.id, b.name FROM bank_account_branches bab
            JOIN branches b ON b.id=bab.branch_id ORDER BY b.name
        ''').fetchall():
            acc_branches.setdefault(row['bank_account_id'], []).append({'id': row['id'], 'name': row['name']})
        statements  = conn.execute('''
            SELECT bs.*, ba.name as account_name
            FROM bank_statements bs JOIN bank_accounts ba ON ba.id=bs.bank_account_id
            ORDER BY bs.uploaded_at DESC
        ''').fetchall()
        contractors = conn.execute(
            'SELECT * FROM contractors WHERE is_active=1 ORDER BY name'
        ).fetchall()
        terminals   = conn.execute(
            'SELECT t.*, b.name as branch_name FROM bank_terminals t LEFT JOIN branches b ON b.id=t.branch_id ORDER BY t.terminal_number'
        ).fetchall()
        branches    = conn.execute('SELECT * FROM branches WHERE is_active=1 ORDER BY name').fetchall()
        exp_cats    = get_expense_categories(conn)
        ctr_cats    = conn.execute('SELECT * FROM contractor_categories ORDER BY sort_order, name').fetchall()

        beznal_rows = []
        beznal_branches = []
        if tab == 'beznal':
            beznal_branches = conn.execute('''
                SELECT DISTINCT b.id, b.name FROM bank_terminals t
                JOIN branches b ON b.id=t.branch_id WHERE t.is_active=1 ORDER BY b.name
            ''').fetchall()
            # Получаем каждую транзакцию отдельно, чтобы извлечь комиссию из описания
            raw = conn.execute('''
                SELECT bt.txn_date, t.branch_id, bt.amount, bt.description
                FROM bank_transactions bt
                JOIN bank_terminals t ON t.id=bt.terminal_id
                WHERE bt.txn_date BETWEEN ? AND ? AND bt.amount > 0 AND bt.is_ignored=0
                ORDER BY bt.txn_date DESC
            ''', (date_from, date_to)).fetchall()
            _comm_re = re.compile(r'[Кк]омисси[яи]\s+([\d]+[.,][\d]+)', re.IGNORECASE)
            days = {}
            for r in raw:
                desc = r['description'] or ''
                m = _comm_re.search(desc)
                commission = float(m.group(1).replace(',', '.')) if m else 0.0
                net = float(r['amount'])
                gross = net + commission
                cell = days.setdefault(r['txn_date'], {}).setdefault(r['branch_id'],
                    {'gross': 0.0, 'commission': 0.0})
                cell['gross'] += gross
                cell['commission'] += commission
            beznal_rows = sorted(days.items(), reverse=True)

        expense_rows = []
        expense_total = 0
        if tab == 'expenses':
            expense_rows = conn.execute('''
                SELECT
                    COALESCE(c.name, bt.counterparty, bt.description, '—') as name,
                    bt.category,
                    ec.label as cat_label,
                    SUM(-bt.amount) as total,
                    COUNT(*) as cnt
                FROM bank_transactions bt
                LEFT JOIN contractors c ON c.id=bt.contractor_id
                LEFT JOIN expense_categories ec ON ec.code=bt.category
                WHERE bt.txn_date BETWEEN ? AND ? AND bt.amount < 0 AND bt.is_ignored=0
                GROUP BY bt.contractor_id, bt.category, COALESCE(c.name, bt.counterparty, bt.description)
                ORDER BY total DESC
            ''', (date_from, date_to)).fetchall()
            expense_total = sum(r['total'] for r in expense_rows)

        compare_rows = []
        compare_bank = 0
        compare_crm  = 0
        if tab == 'compare':
            compare_rows = conn.execute('''
                SELECT
                    COALESCE(bt.txn_date, e.date) as dt,
                    COALESCE(c.name, bt.counterparty, bt.description, '—') as counterparty,
                    bt.category as bank_cat,
                    ec1.label as bank_cat_label,
                    -bt.amount as bank_amount,
                    e.amount_cash + e.amount_card as crm_amount,
                    e.description as crm_desc
                FROM bank_transactions bt
                LEFT JOIN contractors c ON c.id=bt.contractor_id
                LEFT JOIN expense_categories ec1 ON ec1.code=bt.category
                LEFT JOIN expenses e ON e.date=bt.txn_date
                    AND ABS(e.amount_cash+e.amount_card - (-bt.amount)) < 1
                WHERE bt.txn_date BETWEEN ? AND ? AND bt.amount < 0 AND bt.is_ignored=0
                ORDER BY dt DESC
            ''', (date_from, date_to)).fetchall()
            compare_bank = conn.execute(
                'SELECT COALESCE(SUM(-amount),0) FROM bank_transactions WHERE txn_date BETWEEN ? AND ? AND amount<0 AND is_ignored=0',
                (date_from, date_to)
            ).fetchone()[0]
            compare_crm = conn.execute(
                "SELECT COALESCE(SUM(amount_cash+amount_card),0) FROM expenses e JOIN shifts s ON s.id=e.shift_id WHERE s.date BETWEEN ? AND ?",
                (date_from, date_to)
            ).fetchone()[0]

        sber_auto_count = conn.execute(
            "SELECT COUNT(*) FROM bank_accounts WHERE is_active=1 AND sber_auto_sync=1 AND account_number != '' AND account_number IS NOT NULL"
        ).fetchone()[0]

        parse_rules = conn.execute('''
            SELECT pr.*, ba.name as account_name
            FROM bank_parse_rules pr
            LEFT JOIN bank_accounts ba ON ba.id=pr.bank_account_id
            ORDER BY pr.sort_order, pr.id
        ''').fetchall()

    return render_template('bank.html',
        tab=tab, date_from=date_from, date_to=date_to,
        accounts=accounts, acc_branches=acc_branches, statements=statements,
        contractors=contractors, terminals=terminals,
        branches=branches, exp_cats=exp_cats, ctr_cats=ctr_cats,
        beznal_rows=beznal_rows, beznal_branches=beznal_branches,
        expense_rows=expense_rows, expense_total=expense_total,
        compare_rows=compare_rows, compare_bank=compare_bank, compare_crm=compare_crm,
        sber_auto_count=sber_auto_count,
        parse_rules=parse_rules,
    )


@app.route('/bank/upload', methods=['POST'])
@login_required
@owner_required
def bank_upload():
    account_id = request.form.get('account_id', '')
    f = request.files.get('file')
    if not account_id or not f:
        flash('Выберите счёт и файл', 'danger')
        return redirect(url_for('bank'))
    raw = f.read()
    try:
        txns = _parse_bank_csv(raw)
    except Exception as e:
        flash(f'Ошибка разбора файла: {e}', 'danger')
        return redirect(url_for('bank'))
    if not txns:
        flash('Не найдено ни одной транзакции', 'warning')
        return redirect(url_for('bank'))
    with get_db() as conn:
        # Сначала помечаем расходы по картам филиалов
        for t in txns:
            bc = _detect_branch_card(conn, t)
            if bc:
                t['_branch_card'] = bc
        _match_contractors(conn, txns)
        dates = [t['date'] for t in txns]
        stmt_id = conn.execute(
            'INSERT INTO bank_statements (bank_account_id, filename, uploaded_by, date_from, date_to, row_count) VALUES (?,?,?,?,?,?)',
            (int(account_id), f.filename, session['user_id'], min(dates), max(dates), len(txns))
        ).lastrowid
        for t in txns:
            tid = _match_terminal(conn, t)
            conn.execute(
                'INSERT INTO bank_transactions (statement_id, bank_account_id, txn_date, amount, description, counterparty, contractor_id, category, terminal_id) VALUES (?,?,?,?,?,?,?,?,?)',
                (stmt_id, int(account_id), t['date'], t['amount'], t['description'], t['counterparty'],
                 t.get('contractor_id'), t.get('category', ''), tid)
            )
        conn.commit()
    flash(f'Загружено {len(txns)} транзакций.', 'success')
    return redirect(url_for('bank_statement_view', stmt_id=stmt_id))


@app.route('/bank/statement/<int:stmt_id>/classify', methods=['GET', 'POST'])
@login_required
@owner_required
def bank_classify(stmt_id):
    with get_db() as conn:
        stmt = conn.execute('''
            SELECT bs.*, ba.name as account_name
            FROM bank_statements bs JOIN bank_accounts ba ON ba.id=bs.bank_account_id
            WHERE bs.id=?
        ''', (stmt_id,)).fetchone()
        if not stmt:
            flash('Выписка не найдена', 'danger')
            return redirect(url_for('bank'))

        if request.method == 'POST':
            ctr_names    = request.form.getlist('ctr_name')
            ctr_cats     = request.form.getlist('ctr_category')
            ctr_keywords = request.form.getlist('ctr_keywords')
            ctr_ids      = request.form.getlist('ctr_id')
            tid_numbers  = request.form.getlist('tid_number')
            tid_branches = request.form.getlist('tid_branch')
            tid_names    = request.form.getlist('tid_name')

            for name, cat, kw, ctr_id_str in zip(ctr_names, ctr_cats, ctr_keywords, ctr_ids):
                name = (name or '').strip()
                cat  = (cat or '').strip()
                if not name or not cat:
                    continue
                kw = (kw or name).strip() or name
                pre_id = int(ctr_id_str) if ctr_id_str and ctr_id_str.isdigit() else None

                if pre_id:
                    # Contractor already matched — update its category
                    conn.execute('UPDATE contractors SET category=?, keywords=? WHERE id=?',
                                 (cat, kw, pre_id))
                    # Update all transactions for this contractor in the statement
                    conn.execute('''
                        UPDATE bank_transactions SET category=?
                        WHERE statement_id=? AND contractor_id=?
                    ''', (cat, stmt_id, pre_id))
                    # Also link any remaining unlinked rows by counterparty text
                    conn.execute('''
                        UPDATE bank_transactions SET contractor_id=?, category=?
                        WHERE statement_id=? AND LOWER(TRIM(counterparty))=LOWER(?) AND contractor_id IS NULL
                    ''', (pre_id, cat, stmt_id, name))
                else:
                    existing = conn.execute(
                        'SELECT id FROM contractors WHERE LOWER(name)=LOWER(?)', (name,)
                    ).fetchone()
                    if existing:
                        conn.execute('UPDATE contractors SET category=?, keywords=? WHERE id=?',
                                     (cat, kw, existing['id']))
                        pre_id = existing['id']
                    else:
                        pre_id = conn.execute(
                            'INSERT INTO contractors (name, category, keywords) VALUES (?,?,?)',
                            (name, cat, kw)
                        ).lastrowid
                    conn.execute('''
                        UPDATE bank_transactions SET contractor_id=?, category=?
                        WHERE statement_id=? AND LOWER(TRIM(counterparty))=LOWER(?)
                    ''', (pre_id, cat, stmt_id, name))

            for tid, branch_str, tname in zip(tid_numbers, tid_branches, tid_names):
                tid = (tid or '').strip()
                if not tid:
                    continue
                branch_id = int(branch_str) if branch_str and branch_str.isdigit() else None
                tname = (tname or f'Терминал {tid}').strip()
                existing = conn.execute(
                    'SELECT id FROM bank_terminals WHERE terminal_number=?', (tid,)
                ).fetchone()
                if existing:
                    conn.execute('UPDATE bank_terminals SET branch_id=?, name=? WHERE id=?',
                                 (branch_id, tname, existing['id']))
                    term_id = existing['id']
                else:
                    term_id = conn.execute(
                        'INSERT INTO bank_terminals (terminal_number, name, branch_id) VALUES (?,?,?)',
                        (tid, tname, branch_id)
                    ).lastrowid
                conn.execute('''
                    UPDATE bank_transactions SET terminal_id=?
                    WHERE statement_id=?
                      AND (description LIKE ? OR counterparty LIKE ?
                           OR description LIKE ? OR counterparty LIKE ?)
                ''', (term_id, stmt_id,
                      f'%TID%{tid}%', f'%TID%{tid}%',
                      f'%{tid}%', f'%{tid}%'))

            conn.commit()
            flash('Разметка сохранена', 'success')
            return redirect(url_for('bank_statement_view', stmt_id=stmt_id))

        # GET — build unique counterparty and terminal lists
        ctr_rows = conn.execute('''
            SELECT TRIM(counterparty) as name, COUNT(*) as cnt, SUM(amount) as total,
                   MIN(contractor_id) as contractor_id, MIN(category) as category
            FROM bank_transactions
            WHERE statement_id=? AND TRIM(counterparty) != ''
            GROUP BY LOWER(TRIM(counterparty))
            ORDER BY SUM(amount)
        ''', (stmt_id,)).fetchall()

        all_ctrs = conn.execute('SELECT id, name, category, keywords FROM contractors').fetchall()
        existing_ctrs_by_name = {c['name'].lower(): c for c in all_ctrs}
        existing_ctrs_by_id   = {c['id']: c for c in all_ctrs}

        counterparties = []
        for row in ctr_rows:
            name   = row['name']
            ctr_id = row['contractor_id']
            # Prefer lookup by ID (handles keyword-matched contractors with different name)
            ex = existing_ctrs_by_id.get(ctr_id) if ctr_id else None
            if ex is None:
                ex = existing_ctrs_by_name.get(name.lower())
            matched_name = ex['name'] if ex and ex['name'].lower() != name.lower() else None
            counterparties.append({
                'name':         name,
                'cnt':          row['cnt'],
                'total':        row['total'],
                'ctr_id':       ex['id'] if ex else None,
                'is_new':       ex is None,
                'current_cat':  (ex['category'] if ex else row['category']) or '',
                'keywords':     (ex['keywords'] if ex else name) or name,
                'matched_name': matched_name,
            })

        # Detect TIDs in descriptions
        all_txns = conn.execute(
            'SELECT description, counterparty FROM bank_transactions WHERE statement_id=?', (stmt_id,)
        ).fetchall()
        tid_cnts = {}
        for t in all_txns:
            text = f"{t['description'] or ''} {t['counterparty'] or ''}"
            m = re.search(r'TID\s*[:\-]?\s*(\d{6,12})', text, re.IGNORECASE)
            if m:
                tid = m.group(1)
                tid_cnts[tid] = tid_cnts.get(tid, 0) + 1

        existing_tids = {r['terminal_number']: r for r in
                         conn.execute('SELECT terminal_number, name, branch_id FROM bank_terminals').fetchall()}

        tid_list = []
        for tid, cnt in sorted(tid_cnts.items()):
            ex = existing_tids.get(tid)
            tid_list.append({
                'tid':       tid,
                'cnt':       cnt,
                'is_new':    ex is None,
                'name':      (ex['name'] if ex else '') or '',
                'branch_id': (ex['branch_id'] if ex else None),
            })

        txn_count = conn.execute(
            'SELECT COUNT(*) FROM bank_transactions WHERE statement_id=?', (stmt_id,)
        ).fetchone()[0]
        branches = conn.execute('SELECT * FROM branches WHERE is_active=1 ORDER BY name').fetchall()
        exp_cats = get_expense_categories(conn)

    return render_template('bank_classify.html',
        stmt=stmt, counterparties=counterparties, tid_list=tid_list,
        branches=branches, exp_cats=exp_cats, txn_count=txn_count)


@app.route('/bank/statement/<int:stmt_id>/delete', methods=['POST'])
@login_required
@owner_required
def bank_statement_delete(stmt_id):
    with get_db() as conn:
        conn.execute('DELETE FROM bank_statements WHERE id=?', (stmt_id,))
        conn.commit()
    flash('Выписка удалена', 'success')
    return redirect(url_for('bank'))


@app.route('/bank/statements/<int:stmt_id>/rematch', methods=['POST'])
@login_required
@owner_required
def bank_statement_rematch(stmt_id):
    """Повторно применить автоматический матчинг контрагентов и терминалов."""
    with get_db() as conn:
        txns_rows = conn.execute(
            'SELECT id, description, counterparty FROM bank_transactions WHERE statement_id=?',
            (stmt_id,)
        ).fetchall()
        updated = 0
        contractors = conn.execute(
            'SELECT id, name, category, keywords FROM contractors WHERE is_active=1'
        ).fetchall()
        for row in txns_rows:
            txn = dict(row)
            matched_cid = None
            matched_cat = None
            text = ((txn.get('description') or '') + ' ' + (txn.get('counterparty') or '')).lower()
            for c in contractors:
                kws = [k.strip().lower() for k in (c['keywords'] or c['name']).split(',') if k.strip()]
                if any(kw in text for kw in kws):
                    matched_cid = c['id']
                    matched_cat = c['category']
                    break
            # Если не найден — создаём из counterparty
            if not matched_cid:
                cp = (txn.get('counterparty') or '').strip()
                if cp:
                    existing = conn.execute(
                        'SELECT id FROM contractors WHERE LOWER(name)=LOWER(?)', (cp,)
                    ).fetchone()
                    if existing:
                        matched_cid = existing['id']
                    else:
                        conn.execute('INSERT INTO contractors (name, keywords) VALUES (?,?)', (cp, cp))
                        matched_cid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
                        contractors = conn.execute(
                            'SELECT id, name, category, keywords FROM contractors WHERE is_active=1'
                        ).fetchall()
            # Матчинг терминала
            tid = _match_terminal(conn, txn)
            conn.execute(
                'UPDATE bank_transactions SET contractor_id=?, category=COALESCE(NULLIF(category,""),?), terminal_id=COALESCE(terminal_id,?) WHERE id=?',
                (matched_cid, matched_cat or '', tid, row['id'])
            )
            if matched_cid or tid:
                updated += 1
        conn.commit()
    flash(f'Переназначено: {updated} из {len(txns_rows)} транзакций', 'success')
    return redirect(url_for('bank_statement_view', stmt_id=stmt_id))


@app.route('/bank/transaction/<int:txn_id>/update', methods=['POST'])
@login_required
@owner_required
def bank_txn_update(txn_id):
    contractor_id = request.form.get('contractor_id') or None
    category = request.form.get('category', '')
    is_ignored = 1 if request.form.get('is_ignored') else 0
    with get_db() as conn:
        conn.execute(
            'UPDATE bank_transactions SET contractor_id=?, category=?, is_ignored=? WHERE id=?',
            (contractor_id, category, is_ignored, txn_id)
        )
        conn.commit()
    return jsonify({'ok': True})


@app.route('/bank/accounts/add', methods=['POST'])
@login_required
@owner_required
def bank_account_add():
    name = request.form.get('name', '').strip()
    bank_name = request.form.get('bank_name', '').strip()
    account_number = request.form.get('account_number', '').strip()
    branch_ids = [b for b in request.form.getlist('branch_ids') if b.isdigit()]
    if not name:
        flash('Введите название счёта', 'danger')
        return redirect(url_for('bank', tab='accounts'))
    with get_db() as conn:
        acc_id = conn.execute(
            'INSERT INTO bank_accounts (name, bank_name, account_number) VALUES (?,?,?)',
            (name, bank_name, account_number)
        ).lastrowid
        for bid in branch_ids:
            conn.execute(
                'INSERT OR IGNORE INTO bank_account_branches (bank_account_id, branch_id) VALUES (?,?)',
                (acc_id, int(bid))
            )
        conn.commit()
    flash(f'Счёт «{name}» добавлен', 'success')
    return redirect(url_for('bank', tab='accounts'))


@app.route('/bank/accounts/<int:acc_id>/delete', methods=['POST'])
@login_required
@owner_required
def bank_account_delete(acc_id):
    with get_db() as conn:
        conn.execute('UPDATE bank_accounts SET is_active=0 WHERE id=?', (acc_id,))
        conn.commit()
    flash('Счёт удалён', 'success')
    return redirect(url_for('bank', tab='accounts'))


@app.route('/bank/contractor-categories/add', methods=['POST'])
@login_required
@owner_required
def bank_ctr_cat_add():
    name      = request.form.get('name', '').strip()
    direction = request.form.get('direction', 'any')
    if name:
        with get_db() as conn:
            conn.execute('INSERT OR IGNORE INTO contractor_categories (name, direction) VALUES (?,?)', (name, direction))
            conn.commit()
    return redirect(url_for('bank', tab='contractors'))


@app.route('/bank/contractor-categories/<int:cat_id>/direction', methods=['POST'])
@login_required
@owner_required
def bank_ctr_cat_direction(cat_id):
    direction = request.form.get('direction', 'any')
    with get_db() as conn:
        conn.execute('UPDATE contractor_categories SET direction=? WHERE id=?', (direction, cat_id))
        conn.commit()
    return redirect(url_for('bank', tab='contractors'))


@app.route('/bank/contractor-categories/<int:cat_id>/delete', methods=['POST'])
@login_required
@owner_required
def bank_ctr_cat_delete(cat_id):
    with get_db() as conn:
        conn.execute('DELETE FROM contractor_categories WHERE id=?', (cat_id,))
        conn.commit()
    return redirect(url_for('bank', tab='contractors'))


@app.route('/bank/contractors/add', methods=['POST'])
@login_required
@owner_required
def bank_contractor_add():
    name = request.form.get('name', '').strip()
    category = request.form.get('category', '').strip()
    raw_kw = request.form.get('keywords', '').strip()
    keywords = ', '.join(_filter_keywords([k.strip() for k in raw_kw.split(',') if k.strip()])) if raw_kw else ''
    inn = re.sub(r'\D', '', request.form.get('inn', '').strip())
    next_url = request.form.get('next', '').strip()
    if not name:
        flash('Введите название контрагента', 'danger')
        return redirect(next_url or url_for('bank', tab='contractors'))
    with get_db() as conn:
        conn.execute(
            'INSERT INTO contractors (name, category, keywords, inn) VALUES (?,?,?,?)',
            (name, category, keywords, inn or None)
        )
        conn.commit()
    flash(f'Контрагент «{name}» добавлен', 'success')
    return redirect(next_url or url_for('bank', tab='contractors'))


@app.route('/bank/contractors/<int:ctr_id>/edit', methods=['POST'])
@login_required
@owner_required
def bank_contractor_edit(ctr_id):
    name = request.form.get('name', '').strip()
    category = request.form.get('category', '').strip()
    raw_kw = request.form.get('keywords', '').strip()
    keywords = ', '.join(_filter_keywords([k.strip() for k in raw_kw.split(',') if k.strip()])) if raw_kw else ''
    inn = re.sub(r'\D', '', request.form.get('inn', '').strip())
    with get_db() as conn:
        conn.execute(
            'UPDATE contractors SET name=?, category=?, keywords=?, inn=? WHERE id=?',
            (name, category, keywords, inn or None, ctr_id)
        )
        conn.commit()
    flash('Контрагент обновлён', 'success')
    return redirect(url_for('bank', tab='contractors'))


@app.route('/bank/contractors/<int:ctr_id>/delete', methods=['POST'])
@login_required
@owner_required
def bank_contractor_delete(ctr_id):
    with get_db() as conn:
        conn.execute('UPDATE contractors SET is_active=0 WHERE id=?', (ctr_id,))
        conn.commit()
    flash('Контрагент удалён', 'success')
    return redirect(url_for('bank', tab='contractors'))


@app.route('/bank/terminals/add', methods=['POST'])
@login_required
@owner_required
def bank_terminal_add():
    terminal_number = request.form.get('terminal_number', '').strip()
    name = request.form.get('name', '').strip()
    branch_id = request.form.get('branch_id') or None
    if not terminal_number:
        flash('Введите номер терминала', 'danger')
        return redirect(url_for('bank', tab='terminals'))
    with get_db() as conn:
        try:
            conn.execute(
                'INSERT INTO bank_terminals (terminal_number, name, branch_id) VALUES (?,?,?)',
                (terminal_number, name, branch_id)
            )
            conn.commit()
            flash(f'Терминал {terminal_number} добавлен', 'success')
        except Exception:
            flash('Терминал с таким номером уже существует', 'danger')
    return redirect(url_for('bank', tab='terminals'))


@app.route('/bank/terminals/<int:tid>/edit', methods=['POST'])
@login_required
@owner_required
def bank_terminal_edit(tid):
    name = request.form.get('name', '').strip()
    branch_id = request.form.get('branch_id') or None
    with get_db() as conn:
        conn.execute(
            'UPDATE bank_terminals SET name=?, branch_id=? WHERE id=?',
            (name, branch_id, tid)
        )
        conn.commit()
    flash('Терминал обновлён', 'success')
    return redirect(url_for('bank', tab='terminals'))


@app.route('/bank/terminals/<int:tid>/delete', methods=['POST'])
@login_required
@owner_required
def bank_terminal_delete(tid):
    with get_db() as conn:
        conn.execute('UPDATE bank_terminals SET is_active=0 WHERE id=?', (tid,))
        conn.commit()
    flash('Терминал удалён', 'success')
    return redirect(url_for('bank', tab='terminals'))


@app.route('/bank/parse-rules/add', methods=['POST'])
@login_required
@owner_required
def bank_parse_rule_add():
    name       = request.form.get('name', '').strip()
    keyword    = request.form.get('keyword', '').strip()
    direction  = request.form.get('direction', 'any')
    account_id = request.form.get('bank_account_id', '').strip() or None
    comm_incl  = 1 if request.form.get('commission_included') == '1' else 0
    comm_pat   = request.form.get('commission_pattern', '').strip()
    category   = request.form.get('category', '').strip()
    sort_order = int(request.form.get('sort_order', 0) or 0)
    if not name or not keyword:
        flash('Укажите название и ключевое слово', 'danger')
        return redirect(url_for('bank', tab='rules'))
    with get_db() as conn:
        conn.execute(
            'INSERT INTO bank_parse_rules (bank_account_id, name, direction, keyword, commission_included, commission_pattern, category, sort_order) VALUES (?,?,?,?,?,?,?,?)',
            (account_id, name, direction, keyword, comm_incl, comm_pat, category, sort_order)
        )
        conn.commit()
    flash('Правило добавлено', 'success')
    return redirect(url_for('bank', tab='rules'))


@app.route('/bank/parse-rules/<int:rule_id>/edit', methods=['POST'])
@login_required
@owner_required
def bank_parse_rule_edit(rule_id):
    name       = request.form.get('name', '').strip()
    keyword    = request.form.get('keyword', '').strip()
    direction  = request.form.get('direction', 'any')
    account_id = request.form.get('bank_account_id', '').strip() or None
    comm_incl  = 1 if request.form.get('commission_included') == '1' else 0
    comm_pat   = request.form.get('commission_pattern', '').strip()
    category   = request.form.get('category', '').strip()
    sort_order = int(request.form.get('sort_order', 0) or 0)
    if not name or not keyword:
        flash('Укажите название и ключевое слово', 'danger')
        return redirect(url_for('bank', tab='rules'))
    with get_db() as conn:
        conn.execute(
            'UPDATE bank_parse_rules SET bank_account_id=?, name=?, direction=?, keyword=?, commission_included=?, commission_pattern=?, category=?, sort_order=? WHERE id=?',
            (account_id, name, direction, keyword, comm_incl, comm_pat, category, sort_order, rule_id)
        )
        conn.commit()
    flash('Правило обновлено', 'success')
    return redirect(url_for('bank', tab='rules'))


@app.route('/bank/parse-rules/<int:rule_id>/delete', methods=['POST'])
@login_required
@owner_required
def bank_parse_rule_delete(rule_id):
    with get_db() as conn:
        conn.execute('DELETE FROM bank_parse_rules WHERE id=?', (rule_id,))
        conn.commit()
    flash('Правило удалено', 'success')
    return redirect(url_for('bank', tab='rules'))


@app.route('/bank/statement/<int:stmt_id>')
@login_required
@owner_required
def bank_statement_view(stmt_id):
    with get_db() as conn:
        stmt = conn.execute('''
            SELECT bs.*, ba.name as account_name
            FROM bank_statements bs JOIN bank_accounts ba ON ba.id=bs.bank_account_id
            WHERE bs.id=?
        ''', (stmt_id,)).fetchone()
        if not stmt:
            flash('Выписка не найдена', 'danger')
            return redirect(url_for('bank'))
        txns_raw = conn.execute('''
            SELECT bt.*, c.name as contractor_name,
                   t.terminal_number, b.name as terminal_branch
            FROM bank_transactions bt
            LEFT JOIN contractors c ON c.id=bt.contractor_id
            LEFT JOIN bank_terminals t ON t.id=bt.terminal_id
            LEFT JOIN branches b ON b.id=t.branch_id
            WHERE bt.statement_id=?
            ORDER BY bt.txn_date DESC, bt.id DESC
        ''', (stmt_id,)).fetchall()
        # Для строк с пустым counterparty — извлекаем из описания
        # Определяем тип операции для каждой транзакции
        txns = []
        for row in txns_raw:
            d = dict(row)
            desc  = d.get('description') or ''
            desc_u = desc.upper()
            desc_l = desc.lower()
            amount = d.get('amount', 0)

            if not d.get('counterparty'):
                m = re.search(r'[Сс]бербанка\s+(.+?)\s+по\s+карте', desc)
                if not m:
                    m = re.search(r'в\s+ТУ\s+(.+?)\s+по\s+(?:карте|к)', desc, re.IGNORECASE)
                if m:
                    d['counterparty'] = m.group(1).strip()

            if 'PURCHASE' in desc_u:
                # Карточная покупка — извлекаем последние 4 цифры карты
                cm = re.search(r'по\s+карте\s+(\S+)', desc, re.IGNORECASE)
                if cm:
                    digits = re.sub(r'\D', '', cm.group(1))
                    d['op_card4'] = digits[-4:] if len(digits) >= 4 else digits
                else:
                    d['op_card4'] = ''
                d['op_type'] = 'card'
            elif d.get('terminal_id') or 'эквайрин' in desc_l:
                d['op_type'] = 'acquiring'
                d['op_card4'] = ''
            elif 'перераспредел' in desc_l or 'между счет' in desc_l:
                d['op_type'] = 'transfer'
                d['op_card4'] = ''
            elif amount < 0:
                d['op_type'] = 'contractor'
                d['op_card4'] = ''
            else:
                d['op_type'] = 'transfer'
                d['op_card4'] = ''

            txns.append(d)

        # Применяем правила разбора (Альфа и др.)
        parse_rules = conn.execute(
            'SELECT * FROM bank_parse_rules WHERE is_active=1 ORDER BY sort_order, id'
        ).fetchall()
        _rx_cache = {}
        for d in txns:
            d['parse_rule_name'] = ''
            d['parse_rule_category'] = ''
            d['commission_extracted'] = None
            for rule in parse_rules:
                if rule['bank_account_id'] and rule['bank_account_id'] != stmt['bank_account_id']:
                    continue
                if rule['direction'] == 'income' and d['amount'] <= 0:
                    continue
                if rule['direction'] == 'expense' and d['amount'] >= 0:
                    continue
                kw = (rule['keyword'] or '').lower()
                if not kw or kw not in (d.get('description') or '').lower():
                    continue
                d['parse_rule_name'] = rule['name']
                d['parse_rule_category'] = rule['category'] or ''
                if not rule['commission_included'] and rule['commission_pattern']:
                    pat = rule['commission_pattern']
                    if pat not in _rx_cache:
                        try:
                            _rx_cache[pat] = re.compile(pat, re.IGNORECASE)
                        except re.error:
                            _rx_cache[pat] = None
                    rx = _rx_cache.get(pat)
                    if rx:
                        m2 = rx.search(d.get('description') or '')
                        if m2:
                            try:
                                d['commission_extracted'] = float(m2.group(1).replace(',', '.'))
                            except (ValueError, IndexError):
                                pass
                break

        contractors = conn.execute(
            'SELECT * FROM contractors WHERE is_active=1 AND COALESCE(is_card_merchant,0)=0 ORDER BY name'
        ).fetchall()
        all_ctr_cats = conn.execute('SELECT * FROM contractor_categories ORDER BY sort_order, name').fetchall()
        ctr_cats_income  = [c for c in all_ctr_cats if c['direction'] in ('income', 'any')]
        ctr_cats_expense = [c for c in all_ctr_cats if c['direction'] in ('expense', 'any')]
    unique_cats = sorted(set(d['category'] for d in txns if d.get('category')))
    return render_template('bank_statement.html',
        stmt=stmt, txns=txns, contractors=contractors,
        ctr_cats_income=ctr_cats_income, ctr_cats_expense=ctr_cats_expense,
        unique_cats=unique_cats)


# ─── SBERBANK API SYNC ────────────────────────────────────────────────────────

def _sber_get(conn, key, default=''):
    row = conn.execute('SELECT value FROM api_settings WHERE key=?', (key,)).fetchone()
    return row[0] if row else default

def _sber_set(conn, key, value):
    conn.execute('INSERT OR REPLACE INTO api_settings(key,value) VALUES(?,?)', (key, value))


@app.route('/bank/sber/debug')
@login_required
@owner_required
def sber_debug():
    """Отладка: показать состояние токенов в базе."""
    import time
    with get_db() as conn:
        keys = ['sber_client_id','sber_account_number','sber_access_token',
                'sber_refresh_token','sber_token_expires','sber_npa_active','sber_last_sync','sber_last_result']
        info = {}
        for k in keys:
            v = _sber_get(conn, k)
            if k in ('sber_access_token','sber_refresh_token') and v:
                info[k] = v[:12] + '...' + f' (len={len(v)})'
            elif k == 'sber_token_expires' and v:
                try:
                    exp = float(v)
                    remaining = exp - time.time()
                    info[k] = f'{v} (осталось {int(remaining)}с / {int(remaining/60)}мин)'
                except:
                    info[k] = v
            else:
                info[k] = v or '(пусто)'
    return jsonify(info)


@app.route('/bank/sber/debug-tx')
@login_required
@owner_required
def sber_debug_tx():
    """Показать сырую структуру первых 2 транзакций от Сбера (для отладки полей)."""
    from sber_api import get_statement, _mtls, STMT_URL
    import time, requests, urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    with get_db() as conn:
        access_token   = _sber_get(conn, 'sber_access_token')
        client_id      = _sber_get(conn, 'sber_client_id', '71154')
        account_number = _sber_get(conn, 'sber_account_number')
        if not access_token or not account_number:
            return jsonify({'error': 'Нет токена или счёта'})
        today = date.today().isoformat()
        resp = requests.get(
            STMT_URL,
            cert=_mtls(), verify=False,
            headers={'Authorization': f'Bearer {access_token}',
                     'x-ibm-client-id': str(client_id),
                     'Accept': 'application/json'},
            params={'accountNumber': account_number, 'statementDate': today, 'page': 1},
            timeout=30,
        )
        raw = resp.json() if resp.ok else {'status': resp.status_code, 'body': resp.text[:1000]}
        ops = (raw.get('transactions') or raw.get('operations') or raw.get('operationList') or
               raw.get('items') or [])
        return jsonify({'status': resp.status_code, 'keys_top': list(raw.keys()),
                        'first_2_ops': ops[:2]})


@app.route('/bank/sber/settings', methods=['GET', 'POST'])
@login_required
@owner_required
def sber_settings():
    with get_db() as conn:
        if request.method == 'POST':
            _sber_set(conn, 'sber_client_id',     request.form.get('client_id', '').strip())
            _sber_set(conn, 'sber_client_secret',  request.form.get('client_secret', '').strip())
            conn.commit()
            flash('Настройки API сохранены', 'success')
            return redirect(url_for('sber_settings'))

        cfg = {
            'client_id':    _sber_get(conn, 'sber_client_id', '71154'),
            'client_secret': _sber_get(conn, 'sber_client_secret'),
            'last_sync':    _sber_get(conn, 'sber_last_sync'),
            'last_result':  _sber_get(conn, 'sber_last_result'),
            'has_token':    bool(_sber_get(conn, 'sber_refresh_token')) or _sber_get(conn, 'sber_npa_active') == '1',
        }
        accounts = conn.execute(
            'SELECT id, name, account_number, sber_auto_sync, sber_last_sync, sber_last_result '
            'FROM bank_accounts WHERE is_active=1 ORDER BY name'
        ).fetchall()
    return render_template('sber_settings.html', cfg=cfg, accounts=accounts)


@app.route('/bank/sber/auth')
@login_required
@owner_required
def sber_auth():
    """Редирект на страницу авторизации СберБизнес (OAuth Authorization Code)."""
    import secrets
    from sber_api import build_auth_url
    with get_db() as conn:
        client_id = _sber_get(conn, 'sber_client_id', '71154')
        state = secrets.token_urlsafe(16)
        nonce = secrets.token_urlsafe(16)
        _sber_set(conn, 'sber_oauth_state', state)
        conn.commit()
    redirect_uri = url_for('sber_callback', _external=True)
    auth_url = build_auth_url(client_id, redirect_uri, state, nonce)
    return redirect(auth_url)


@app.route('/bank/sber/callback')
@login_required
@owner_required
def sber_callback():
    """Обработчик callback после авторизации в СберБизнес."""
    from sber_api import exchange_code
    import time
    code  = request.args.get('code')
    state = request.args.get('state')
    error = request.args.get('error')

    if error:
        flash(f'Сбербанк отказал в доступе: {error}', 'danger')
        return redirect(url_for('sber_settings'))

    with get_db() as conn:
        saved_state = _sber_get(conn, 'sber_oauth_state')
        if state != saved_state:
            flash('Ошибка безопасности: state не совпадает. Попробуйте снова.', 'danger')
            return redirect(url_for('sber_settings'))

        client_id     = _sber_get(conn, 'sber_client_id', '71154')
        client_secret = _sber_get(conn, 'sber_client_secret')

        try:
            redirect_uri = url_for('sber_callback', _external=True)
            tokens = exchange_code(client_id, client_secret, code, redirect_uri)
            _sber_set(conn, 'sber_access_token',  tokens['access_token'])
            _sber_set(conn, 'sber_refresh_token', tokens.get('refresh_token', ''))
            _sber_set(conn, 'sber_token_expires',
                      str(time.time() + int(tokens.get('expires_in', 3600))))
            _sber_set(conn, 'sber_npa_active', '1')
            conn.commit()
            flash('Сбербанк успешно подключён! Можно загружать выписки.', 'success')
        except Exception as e:
            flash(f'Ошибка получения токена: {e}', 'danger')

    return redirect(url_for('sber_settings'))


def _sber_get_token(conn):
    """Вернуть актуальный access_token, обновив через refresh если нужно. None = нет токена."""
    from sber_api import refresh_access_token
    import time
    client_id     = _sber_get(conn, 'sber_client_id', '71154')
    cached_token  = _sber_get(conn, 'sber_access_token')
    token_exp_str = _sber_get(conn, 'sber_token_expires')
    refresh_token = _sber_get(conn, 'sber_refresh_token')
    now_ts = time.time()
    if cached_token and token_exp_str and float(token_exp_str) > now_ts + 30:
        return cached_token, client_id
    if not refresh_token:
        return None, client_id
    client_secret = _sber_get(conn, 'sber_client_secret')
    tokens = refresh_access_token(client_id, client_secret, refresh_token)
    access_token = tokens['access_token']
    _sber_set(conn, 'sber_access_token',  access_token)
    _sber_set(conn, 'sber_token_expires', str(now_ts + int(tokens.get('expires_in', 3600))))
    if tokens.get('refresh_token'):
        _sber_set(conn, 'sber_refresh_token', tokens['refresh_token'])
    conn.commit()
    return access_token, client_id


def _sber_do_sync(conn, access_token, client_id, bank_account_id, account_number, date_from, date_to):
    """Загрузить выписку для одного счёта. Возвращает dict с ok/added/total/stmt_id."""
    from sber_api import get_statement, parse_transactions
    stmt_json = get_statement(access_token, client_id, account_number, date_from, date_to)
    txns = parse_transactions(stmt_json)
    now_str = datetime.now().strftime('%d.%m.%Y %H:%M')

    if not txns:
        conn.execute(
            "UPDATE bank_accounts SET sber_last_sync=?, sber_last_result=? WHERE id=?",
            (now_str, '0 транзакций', bank_account_id)
        )
        _sber_set(conn, 'sber_last_sync', now_str)
        _sber_set(conn, 'sber_last_result', '0 новых транзакций')
        conn.commit()
        return {'ok': True, 'added': 0, 'total': 0, 'raw': stmt_json}

    conn.execute(
        '''INSERT INTO bank_statements
           (bank_account_id, filename, date_from, date_to, row_count, uploaded_at)
           VALUES(?,?,?,?,?,?)''',
        (bank_account_id, f'Сбербанк {date_from} — {date_to}',
         date_from, date_to, len(txns), datetime.now().isoformat())
    )
    stmt_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]

    txns = _match_contractors(conn, txns)
    for txn in txns:
        if not txn.get('terminal_id'):
            txn['terminal_id'] = _match_terminal(conn, txn)

    added = 0
    for txn in txns:
        dup = conn.execute('''
            SELECT id FROM bank_transactions
            WHERE bank_account_id=? AND txn_date=? AND amount=? AND description=?
        ''', (bank_account_id, txn['date'], txn['amount'], txn.get('description', ''))).fetchone()
        if dup:
            continue
        conn.execute('''
            INSERT INTO bank_transactions
            (statement_id, bank_account_id, txn_date, amount,
             description, counterparty, contractor_id, category, terminal_id)
            VALUES(?,?,?,?,?,?,?,?,?)
        ''', (
            stmt_id, bank_account_id, txn['date'], txn['amount'],
            txn.get('description', ''), txn.get('counterparty', ''),
            txn.get('contractor_id'), txn.get('category', ''),
            txn.get('terminal_id')
        ))
        added += 1

    result_str = f'+{added} новых из {len(txns)}'
    conn.execute(
        "UPDATE bank_accounts SET sber_last_sync=?, sber_last_result=? WHERE id=?",
        (now_str, result_str, bank_account_id)
    )
    _sber_set(conn, 'sber_last_sync', now_str)
    _sber_set(conn, 'sber_last_result', result_str)
    conn.commit()
    return {'ok': True, 'added': added, 'total': len(txns), 'stmt_id': stmt_id}


@app.route('/bank/sber/sync', methods=['POST'])
@login_required
@owner_required
def sber_sync():
    data = request.get_json(silent=True) or {}
    date_from = data.get('date_from') or (date.today() - timedelta(days=7)).isoformat()
    date_to   = data.get('date_to')   or date.today().isoformat()

    with get_db() as conn:
        bank_account_id = data.get('bank_account_id') or _sber_get(conn, 'sber_bank_account_id')

        # account_number: из записи банк. счёта (если указан) или глобальная настройка
        if bank_account_id:
            ba_row = conn.execute('SELECT account_number FROM bank_accounts WHERE id=?',
                                  (bank_account_id,)).fetchone()
            account_number = ba_row['account_number'] if ba_row else ''
        else:
            account_number = _sber_get(conn, 'sber_account_number')

        if not account_number:
            return jsonify({'ok': False, 'error': 'Не задан номер счёта. Укажите его в настройках счёта.'})

        try:
            access_token, client_id = _sber_get_token(conn)
            if not access_token:
                return jsonify({'ok': False, 'error': 'Нет токена авторизации. Нажмите «Переподключить СберБизнес» и войдите снова.'})

            # Если bank_account_id не задан — найти или создать по account_number
            if not bank_account_id:
                ba = conn.execute(
                    "SELECT id FROM bank_accounts WHERE account_number=?", (account_number,)
                ).fetchone()
                if ba:
                    bank_account_id = ba['id']
                else:
                    conn.execute(
                        "INSERT INTO bank_accounts(name, account_number, bank_name, is_active) VALUES(?,?,?,1)",
                        (f'Сбербанк {account_number[-4:]}', account_number, 'Сбербанк')
                    )
                    bank_account_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
                    conn.commit()
                _sber_set(conn, 'sber_bank_account_id', str(bank_account_id))
                conn.commit()

            result = _sber_do_sync(conn, access_token, client_id, bank_account_id,
                                   account_number, date_from, date_to)
            return jsonify(result)

        except Exception as e:
            err = str(e)
            logging.exception('sber_sync error')
            _sber_set(conn, 'sber_last_result', f'Ошибка: {err[:500]}')
            if bank_account_id:
                conn.execute(
                    "UPDATE bank_accounts SET sber_last_result=? WHERE id=?",
                    (f'Ошибка: {err[:200]}', bank_account_id)
                )
            conn.commit()
            return jsonify({'ok': False, 'error': err})


@app.route('/bank/sber/sync-all', methods=['POST'])
@login_required
@owner_required
def sber_sync_all():
    """Синхронизировать все счета с включённой авто-загрузкой."""
    data = request.get_json(silent=True) or {}
    date_from = data.get('date_from') or (date.today() - timedelta(days=7)).isoformat()
    date_to   = data.get('date_to')   or date.today().isoformat()

    with get_db() as conn:
        accounts = conn.execute(
            "SELECT id, name, account_number FROM bank_accounts "
            "WHERE is_active=1 AND sber_auto_sync=1 AND account_number != '' AND account_number IS NOT NULL"
        ).fetchall()

        if not accounts:
            return jsonify({'ok': True, 'results': [], 'total_added': 0,
                            'message': 'Нет счетов с включённой автозагрузкой'})

        try:
            access_token, client_id = _sber_get_token(conn)
            if not access_token:
                return jsonify({'ok': False, 'error': 'Нет токена авторизации. Откройте «Сбербанк авто-импорт» и переподключите.'})
        except Exception as e:
            return jsonify({'ok': False, 'error': f'Ошибка получения токена: {e}'})

        results = []
        total_added = 0
        for acc in accounts:
            try:
                r = _sber_do_sync(conn, access_token, client_id, acc['id'],
                                  acc['account_number'], date_from, date_to)
                total_added += r.get('added', 0)
                results.append({'id': acc['id'], 'name': acc['name'],
                                'added': r.get('added', 0), 'ok': True})
            except Exception as e:
                err = str(e)[:200]
                conn.execute(
                    "UPDATE bank_accounts SET sber_last_result=? WHERE id=?",
                    (f'Ошибка: {err}', acc['id'])
                )
                conn.commit()
                results.append({'id': acc['id'], 'name': acc['name'], 'ok': False, 'error': err})

        return jsonify({'ok': True, 'results': results, 'total_added': total_added})


@app.route('/bank/accounts/<int:acc_id>/sber-toggle', methods=['POST'])
@login_required
@owner_required
def bank_account_sber_toggle(acc_id):
    """Включить / выключить авто-синхронизацию Сбербанка для конкретного счёта."""
    with get_db() as conn:
        cur = conn.execute('SELECT sber_auto_sync FROM bank_accounts WHERE id=?', (acc_id,)).fetchone()
        if not cur:
            return jsonify({'ok': False, 'error': 'Счёт не найден'})
        new_val = 0 if cur['sber_auto_sync'] else 1
        conn.execute('UPDATE bank_accounts SET sber_auto_sync=? WHERE id=?', (new_val, acc_id))
        conn.commit()
    return jsonify({'ok': True, 'auto_sync': new_val})


@app.route('/bank/sber/test', methods=['POST'])
@login_required
@owner_required
def sber_test():
    """Проверка токена: пробуем refresh, если нет — сообщаем переподключиться."""
    from sber_api import refresh_access_token
    import time
    with get_db() as conn:
        client_id     = _sber_get(conn, 'sber_client_id', '71154')
        client_secret = _sber_get(conn, 'sber_client_secret')
        refresh_token = _sber_get(conn, 'sber_refresh_token')
        cached_token  = _sber_get(conn, 'sber_access_token')
        token_exp_str = _sber_get(conn, 'sber_token_expires')
    now_ts = time.time()
    if cached_token and token_exp_str and float(token_exp_str or 0) > now_ts + 30:
        return jsonify({'ok': True, 'expires_in': int(float(token_exp_str) - now_ts)})
    if not refresh_token:
        return jsonify({'ok': False, 'error': 'Нет токена. Нажмите «Переподключить СберБизнес».'})
    try:
        tokens = refresh_access_token(client_id, client_secret, refresh_token)
        with get_db() as conn:
            _sber_set(conn, 'sber_access_token',  tokens['access_token'])
            _sber_set(conn, 'sber_token_expires', str(now_ts + int(tokens.get('expires_in', 3600))))
            if tokens.get('refresh_token'):
                _sber_set(conn, 'sber_refresh_token', tokens['refresh_token'])
            conn.commit()
        return jsonify({'ok': True, 'expires_in': tokens.get('expires_in', 3600)})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


# ──────────────────────────────────────────────────────────────────────────────
# ИМПОРТ EXCEL
# ──────────────────────────────────────────────────────────────────────────────

_XL_DAY_SHEETS = {'ПН', 'ВТ', 'СР', 'ЧТ', 'ПТ', 'СБ', 'ВС'}

_XL_ROLE_MAP = {
    'Админ.': 'admin', 'Адм.': 'admin', 'Адм/Упак': 'admin', 'Адм': 'admin',
    'Администратор': 'admin', 'Упак.': 'admin', 'Упак': 'admin',
    'Упаковщик': 'admin', 'Упаковщица': 'admin',
    'Сушист': 'sushi', 'Сушист.': 'sushi',
    'Уборщица': 'cleaner', 'Уборщик': 'cleaner',
    'Повара': 'cook', 'Повар': 'cook', 'Повар.': 'cook',
}

_XL_CAT_MAP = {
    'Ремонт сантех.': 'repair_plumbing', 'Чистка жироул-ля': 'repair_grease',
    'Чистка жироуловителя': 'repair_grease', 'Ремонт электрик': 'repair_electric',
    'Ремонт холод.оборуд.': 'repair_fridge', 'Ремонт другой': 'repair_other',
    'Магазин / Апт.': 'shop', 'Магазин/Апт.': 'shop', 'Другое': 'other',
}

_XL_SKIP = {
    'Курьеры', 'Повара', 'Админ', 'Курьеры:', 'Повара:', 'ФАМИЛИЯ ИМЯ:', 'ФАМИЛИЯ:',
    'Курьер 1', 'Курьер 2', 'Курьер 3', 'Курьер 4', 'Курьер 5', 'Курьер 6',
    'Курьер 1:', 'Курьер 2:', 'Курьер 3:', 'Курьер 4:', 'Курьер 5:', 'Курьер 6:',
}


def _xf(val):
    if val is None or val is False or str(val).strip() in ('х', 'x', 'Х', ''):
        return 0.0
    try:
        return float(val)
    except Exception:
        return 0.0


def _xt(t):
    from datetime import time as _t
    if t is None:
        return 0.0
    if isinstance(t, _t):
        return t.hour + t.minute / 60.0
    try:
        return float(t)
    except Exception:
        return 0.0


def _xts(t):
    from datetime import time as _t
    if isinstance(t, _t):
        return f"{t.hour:02d}:{t.minute:02d}"
    return None


def _xl_process_sheet(ws, branch_id, conn, stats, batch_id=None):
    from datetime import date as _d, datetime as _dt
    rows = list(ws.iter_rows(min_row=1, values_only=True))
    if len(rows) < 7:
        return

    date_val = rows[2][1] if len(rows) > 2 else None
    if isinstance(date_val, _dt):
        shift_date = date_val.date()
    elif isinstance(date_val, _d):
        shift_date = date_val
    else:
        return

    total_revenue = _xf(rows[1][5]) if len(rows) > 1 else 0
    delivery_rev  = _xf(rows[3][6]) if len(rows) > 3 else 0
    cash_amount   = _xf(rows[4][3]) if len(rows) > 4 else 0
    delivery_ord  = int(_xf(rows[4][6])) if len(rows) > 4 else 0
    card_amount   = _xf(rows[5][3]) if len(rows) > 5 else 0
    pickup_rev    = _xf(rows[5][6]) if len(rows) > 5 else 0
    online_amount = _xf(rows[6][3]) if len(rows) > 6 else 0
    pickup_ord    = int(_xf(rows[6][6])) if len(rows) > 6 else 0

    terminal_amount = 0.0
    terminal_codes  = []
    actual_cash     = 0.0
    closed_by_name  = None
    for r in rows[25:]:
        if r[4] == 'По терминалам:' and r[6] is not None:
            terminal_amount = _xf(r[6])
        if r[1] == 'Факт в кассе:':
            actual_cash = _xf(r[3])
        if r[1] == 'Смену закрыл(а):':
            closed_by_name = str(r[4]).strip() if r[4] else None
    for r in rows[28:32]:
        code = r[4]
        amt  = r[6]
        if code is not None and _xf(amt) > 0:
            c = str(code).strip()
            if c:
                terminal_codes.append(c)

    morning_cash  = _xf(rows[2][3]) if len(rows) > 2 else 0.0
    change_amount = _xf(rows[30][3]) if len(rows) > 30 else 0.0
    # D29=масло, D30=рыба, D33:D36=другие плюсы
    plus_amount   = sum(_xf(rows[i][3]) for i in ([28, 29] + list(range(32, 36))) if len(rows) > i)

    existing = conn.execute(
        "SELECT id FROM shifts WHERE branch_id=? AND date=?",
        (branch_id, shift_date.isoformat())
    ).fetchone()
    if existing:
        shift_id = existing[0]
    else:
        status = 'closed' if closed_by_name else 'open'
        conn.execute(
            "INSERT INTO shifts (branch_id, date, status, closed_by_name, import_batch_id) VALUES (?,?,?,?,?)",
            (branch_id, shift_date.isoformat(), status, closed_by_name, batch_id)
        )
        shift_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        stats['shifts'] += 1

    if not conn.execute("SELECT id FROM shift_revenue WHERE shift_id=?", (shift_id,)).fetchone():
        conn.execute(
            """INSERT INTO shift_revenue
               (shift_id, total_revenue, delivery_revenue, delivery_orders,
                pickup_revenue, pickup_orders, cash_amount, card_amount,
                online_amount, terminal_last3, terminal_amount, actual_cash,
                change_amount, plus_amount, morning_cash)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (shift_id, total_revenue, delivery_rev, delivery_ord,
             pickup_rev, pickup_ord, cash_amount, card_amount,
             online_amount, ','.join(terminal_codes), terminal_amount, actual_cash,
             change_amount, plus_amount, morning_cash)
        )

    cur_cat = None
    for r in rows[9:20]:
        cat_str = r[1]
        if cat_str and isinstance(cat_str, str) and cat_str in _XL_CAT_MAP:
            cur_cat = _XL_CAT_MAP[cat_str]
        if cur_cat is None:
            continue
        cash_e = _xf(r[5])
        card_e = _xf(r[6])
        desc   = str(r[2]).strip() if r[2] and str(r[2]).strip() else None
        gulash = 1 if r[7] is True else 0
        if cash_e > 0 or card_e > 0:
            conn.execute(
                "INSERT INTO expenses (shift_id,category,description,amount_cash,amount_card,is_gulash) VALUES (?,?,?,?,?,?)",
                (shift_id, cur_cat, desc, cash_e, card_e, gulash)
            )
            stats['expenses'] += 1

    for r in rows[21:24]:
        desc = str(r[2]).strip() if r[2] else None
        if not desc or r[1] == 'ТАКСИ':
            continue
        cash_t = _xf(r[6])
        card_t = _xf(r[5])
        if cash_t > 0 or card_t > 0:
            conn.execute(
                "INSERT INTO expenses (shift_id,category,description,amount_cash,amount_card,is_gulash) VALUES (?,?,?,?,?,?)",
                (shift_id, 'taxi', desc, cash_t, card_t, 0)
            )
            stats['expenses'] += 1

    def get_or_create(name, role, rate):
        r = conn.execute(
            "SELECT id FROM employees WHERE full_name=? AND branch_id=?", (name, branch_id)
        ).fetchone()
        if r:
            return r[0]
        conn.execute(
            "INSERT INTO employees (branch_id,full_name,role,rate,is_active) VALUES (?,?,?,?,1)",
            (branch_id, name, role, rate)
        )
        eid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        stats['employees'] += 1
        return eid

    def add_shift(emp_id, name, role, rate, hours=0, km=0, orders=0,
                  rate_km=10, rate_ord=100, start=None, end=None,
                  bonus=0, penalty=0, comment='', base_pay=0, total=0, paid=0):
        if conn.execute(
            "SELECT id FROM employee_shifts WHERE shift_id=? AND employee_id=?", (shift_id, emp_id)
        ).fetchone():
            return
        conn.execute(
            """INSERT INTO employee_shifts
               (shift_id,employee_id,full_name_snapshot,role_snapshot,
                rate_snapshot,rate_per_km_snapshot,rate_per_order_snapshot,
                hours_worked,km,orders,shift_start,shift_end,
                bonus_amount,penalty_amount,bonus_comment,
                base_pay,total_amount,is_paid,paid_amount)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (shift_id, emp_id, name, role, rate, rate_km, rate_ord,
             hours, km, orders, start, end,
             bonus, penalty, comment, base_pay, total, paid, total if paid else 0)
        )
        stats['employee_shifts'] += 1

    for r in rows[2:8]:
        name = r[10]
        if not name or not isinstance(name, str) or name.strip() in _XL_SKIP:
            continue
        name = name.strip()
        if not name:
            continue
        total = _xf(r[21])
        if total == 0:
            continue
        km      = _xf(r[12]); km_pay  = _xf(r[13])
        hours   = _xf(r[14]); hrs_pay = _xf(r[15])
        orders  = int(_xf(r[16])); ord_pay = _xf(r[17])
        comment = str(r[18]).strip() if r[18] and str(r[18]) != 'Ничего' else ''
        paid    = 1 if r[20] == 'Да' else 0
        rate_km  = round(km_pay / km, 2) if km > 0 else 10.0
        rate_ord = round(ord_pay / orders, 2) if orders > 0 else 100.0
        rate_hr  = round(hrs_pay / hours, 2) if hours > 0 else 0.0
        emp_id   = get_or_create(name, 'courier', rate_hr)
        add_shift(emp_id, name, 'courier', rate_hr,
                  hours=hours, km=km, orders=orders,
                  rate_km=rate_km, rate_ord=rate_ord, comment=comment,
                  base_pay=hrs_pay + km_pay, total=total, paid=paid)

    for row_idx, r in enumerate(rows[9:22], start=9):
        name = r[10]
        if not name or not isinstance(name, str) or name.strip() in _XL_SKIP:
            continue
        name = name.strip()
        if not name:
            continue
        total = _xf(r[21])
        if total == 0:
            continue
        # Роль определяется по позиции строки, не по метке в колонке L:
        # idx 9-13  (Excel 10-14) = администраторы и упаковщики → admin
        # idx 14-20 (Excel 15-21) = сушисты → sushi
        if name == 'Уборщица':
            role = 'cleaner'
        elif row_idx <= 13:
            role = 'admin'
        else:
            role = 'sushi'
        rate   = _xf(r[12])
        start  = _xts(r[13])
        end    = _xts(r[14])
        hours  = _xt(r[15])
        bval   = r[17]
        bonus   = _xf(bval) if isinstance(bval, (int, float)) and bval > 0 else 0
        penalty = abs(_xf(bval)) if isinstance(bval, (int, float)) and bval < 0 else 0
        comment = str(r[18]).strip() if r[18] and str(r[18]) != 'Ничего' else ''
        paid    = 1 if r[20] == 'Да' else 0
        base    = round(rate * hours, 2)
        emp_id  = get_or_create(name, role, rate)
        add_shift(emp_id, name, role, rate,
                  hours=hours, start=start, end=end,
                  bonus=bonus, penalty=penalty, comment=comment,
                  base_pay=base, total=total, paid=paid)

    conn.commit()


@app.route('/excel-import', methods=['GET', 'POST'])
@login_required
@owner_required
def excel_import():
    return redirect(url_for('gdrive_import'))


@app.route('/excel-import/batch/<int:batch_id>/delete', methods=['POST'])
@login_required
@owner_required
def delete_import_batch(batch_id):
    with get_db() as conn:
        batch = conn.execute('SELECT * FROM import_batches WHERE id=?', (batch_id,)).fetchone()
        if not batch:
            flash('Импорт не найден', 'danger')
            return redirect(url_for('excel_import'))

        shift_ids = [r[0] for r in conn.execute(
            'SELECT id FROM shifts WHERE import_batch_id=?', (batch_id,)
        ).fetchall()]

        if shift_ids:
            ids_str = ','.join(str(i) for i in shift_ids)
            conn.execute(f'DELETE FROM salary_payments WHERE employee_shift_id IN (SELECT id FROM employee_shifts WHERE shift_id IN ({ids_str}))')
            conn.execute(f'DELETE FROM employee_shifts WHERE shift_id IN ({ids_str})')
            conn.execute(f'DELETE FROM expenses WHERE shift_id IN ({ids_str})')
            conn.execute(f'DELETE FROM taxi_trip_employees WHERE trip_id IN (SELECT id FROM taxi_trips WHERE shift_id IN ({ids_str}))')
            conn.execute(f'DELETE FROM taxi_trips WHERE shift_id IN ({ids_str})')
            conn.execute(f'DELETE FROM shift_revenue WHERE shift_id IN ({ids_str})')
            conn.execute(f'DELETE FROM change_log WHERE shift_id IN ({ids_str})')
            conn.execute(f'DELETE FROM shifts WHERE id IN ({ids_str})')

        conn.execute('DELETE FROM import_batches WHERE id=?', (batch_id,))
        conn.commit()

    flash(f'Импорт «{batch["filename"]}» удалён ({len(shift_ids)} смен)', 'success')
    return redirect(url_for('gdrive_import'))


@app.route('/shifts/bulk-delete', methods=['GET', 'POST'])
@login_required
@owner_required
def shifts_bulk_delete():
    with get_db() as conn:
        branches = conn.execute(
            "SELECT id, name FROM branches WHERE is_active=1 ORDER BY name"
        ).fetchall()

    date_from = request.values.get('date_from', '')
    date_to   = request.values.get('date_to', '')
    branch_id = request.values.get('branch_id', '')
    action    = request.values.get('action', '')

    preview_shifts = []
    if (date_from or date_to) and action in ('preview', 'delete'):
        conditions = []
        params = []
        if date_from:
            conditions.append('s.date >= ?')
            params.append(date_from)
        if date_to:
            conditions.append('s.date <= ?')
            params.append(date_to)
        if branch_id:
            conditions.append('s.branch_id = ?')
            params.append(int(branch_id))
        where = 'WHERE ' + ' AND '.join(conditions) if conditions else ''

        with get_db() as conn:
            preview_shifts = conn.execute(f'''
                SELECT s.id, s.date, s.status, b.name as branch_name,
                       COUNT(DISTINCT es.id) as staff_count,
                       COUNT(DISTINCT e.id)  as expense_count
                FROM shifts s
                JOIN branches b ON b.id = s.branch_id
                LEFT JOIN employee_shifts es ON es.shift_id = s.id
                LEFT JOIN expenses e ON e.shift_id = s.id
                {where}
                GROUP BY s.id
                ORDER BY s.date DESC, b.name
            ''', params).fetchall()

            if action == 'delete' and preview_shifts:
                shift_ids = [r['id'] for r in preview_shifts]
                ids_str = ','.join(str(i) for i in shift_ids)
                conn.execute(f'DELETE FROM salary_payments WHERE employee_shift_id IN (SELECT id FROM employee_shifts WHERE shift_id IN ({ids_str}))')
                conn.execute(f'DELETE FROM employee_shifts WHERE shift_id IN ({ids_str})')
                conn.execute(f'DELETE FROM expenses WHERE shift_id IN ({ids_str})')
                conn.execute(f'DELETE FROM shift_revenue WHERE shift_id IN ({ids_str})')
                conn.execute(f'DELETE FROM shifts WHERE id IN ({ids_str})')
                conn.commit()
                flash(f'Удалено {len(shift_ids)} смен(ы)', 'success')
                return redirect(url_for('shifts_bulk_delete'))

    return render_template('bulk_delete_shifts.html',
                           branches=branches,
                           date_from=date_from,
                           date_to=date_to,
                           branch_id=branch_id,
                           preview_shifts=preview_shifts)


@app.route('/gdrive-import', methods=['GET', 'POST'])
@login_required
@owner_required
def gdrive_import():
    import urllib.request, urllib.parse, json, io as _io, time

    def _get_ctx():
        with get_db() as conn:
            branches = conn.execute(
                "SELECT id, name FROM branches WHERE is_active=1 ORDER BY name"
            ).fetchall()
            row = conn.execute("SELECT value FROM api_settings WHERE key='gdrive_sa_json'").fetchone()
            sa_json = row[0] if row else ''
            import_batches = conn.execute('''
                SELECT ib.*, b.name as branch_name
                FROM import_batches ib
                JOIN branches b ON b.id = ib.branch_id
                ORDER BY ib.imported_at DESC LIMIT 50
            ''').fetchall()
        return branches, sa_json, import_batches

    def _get_access_token(sa_json_str):
        import jwt as _jwt
        sa = json.loads(sa_json_str)
        now = int(time.time())
        payload = {
            'iss': sa['client_email'],
            'scope': 'https://www.googleapis.com/auth/drive.readonly',
            'aud': 'https://oauth2.googleapis.com/token',
            'iat': now,
            'exp': now + 3600,
        }
        signed = _jwt.encode(payload, sa['private_key'], algorithm='RS256')
        post_data = urllib.parse.urlencode({
            'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer',
            'assertion': signed,
        }).encode('utf-8')
        req = urllib.request.Request(
            'https://oauth2.googleapis.com/token', data=post_data, method='POST'
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())['access_token']

    def _drive_request(url, token):
        req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()

    action     = request.form.get('action', '')
    folder_url = request.form.get('folder_url', '').strip()
    branch_id  = request.form.get('branch_id', '')

    # ── Сохранить сервисный аккаунт ──────────────────────────────────────────
    if action == 'save_sa':
        sa_text = request.form.get('sa_json', '').strip()
        try:
            parsed = json.loads(sa_text)
            if parsed.get('type') != 'service_account':
                raise ValueError('Не сервисный аккаунт')
            with get_db() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO api_settings (key, value) VALUES ('gdrive_sa_json', ?)",
                    (sa_text,)
                )
            flash(f'Сервисный аккаунт сохранён: {parsed.get("client_email", "")}', 'success')
        except Exception as e:
            flash(f'Ошибка в JSON: {e}', 'danger')
        return redirect(url_for('gdrive_import'))

    drive_files = []

    # ── Показать файлы в папке ───────────────────────────────────────────────
    if action == 'list':
        branches, sa_json, import_batches = _get_ctx()
        if not sa_json:
            flash('Сначала настройте сервисный аккаунт', 'danger')
        elif not folder_url:
            flash('Введите ссылку на папку', 'danger')
        else:
            import re
            m = re.search(r'/folders/([a-zA-Z0-9_-]+)', folder_url)
            if not m:
                flash('Не удалось определить ID папки из ссылки', 'danger')
            else:
                folder_id = m.group(1)
                try:
                    token = _get_access_token(sa_json)
                    q = f"'{folder_id}' in parents and trashed=false"
                    api_url = (
                        'https://www.googleapis.com/drive/v3/files'
                        '?q=' + urllib.parse.quote(q) +
                        '&fields=files(id,name,mimeType,modifiedTime)' +
                        '&orderBy=name&pageSize=100'
                    )
                    data = json.loads(_drive_request(api_url, token))
                    all_files = data.get('files', [])
                    _SHEET_MIME = 'application/vnd.google-apps.spreadsheet'
                    drive_files = [f for f in all_files
                                   if f['name'].lower().endswith('.xlsx')
                                   or f.get('mimeType') == _SHEET_MIME]
                    if not all_files:
                        flash('Папка пуста или сервисный аккаунт не имеет к ней доступа. Поделитесь папкой с email сервисного аккаунта.', 'warning')
                    elif not drive_files:
                        flash(f'В папке {len(all_files)} файл(ов), но нет Google Таблиц или .xlsx.', 'warning')
                except urllib.error.HTTPError as e:
                    body = e.read().decode('utf-8', errors='ignore')
                    flash(f'Ошибка Drive API {e.code}: {body[:300]}', 'danger')
                except Exception as e:
                    flash(f'Ошибка: {e}', 'danger')
        return render_template('import_shifts.html',
                               branches=branches, sa_json=sa_json,
                               import_batches=import_batches,
                               folder_url=folder_url, branch_id=branch_id,
                               drive_files=drive_files)

    # ── Импортировать из Google Drive ────────────────────────────────────────
    elif action == 'gdrive_import':
        branches, sa_json, import_batches = _get_ctx()
        file_data_list = request.form.getlist('file_data')
        branch_id_int  = int(branch_id) if branch_id else None

        if not sa_json:
            flash('Сервисный аккаунт не настроен', 'danger')
        elif not branch_id_int:
            flash('Выберите филиал', 'danger')
        elif not file_data_list:
            flash('Выберите хотя бы один файл', 'danger')
        else:
            try:
                import openpyxl
            except ImportError:
                flash('Библиотека openpyxl не установлена на сервере', 'danger')
                return redirect(url_for('gdrive_import'))

            _SHEET_MIME = 'application/vnd.google-apps.spreadsheet'
            _XLSX_MIME  = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

            try:
                token = _get_access_token(sa_json)
            except Exception as e:
                flash(f'Ошибка авторизации: {e}', 'danger')
                return redirect(url_for('gdrive_import'))

            with get_db() as conn:
                for file_data in file_data_list:
                    parts = file_data.split('|||')
                    if len(parts) != 3:
                        continue
                    fid, fname, fmime = parts
                    is_sheet = fmime == _SHEET_MIME
                    if is_sheet:
                        dl_url = (
                            f'https://www.googleapis.com/drive/v3/files/{fid}/export'
                            f'?mimeType={urllib.parse.quote(_XLSX_MIME)}'
                        )
                    else:
                        dl_url = f'https://www.googleapis.com/drive/v3/files/{fid}?alt=media'
                    try:
                        file_bytes = _drive_request(dl_url, token)
                    except Exception as e:
                        flash(f'Ошибка скачивания «{fname}»: {e}', 'danger')
                        continue

                    wb = openpyxl.load_workbook(_io.BytesIO(file_bytes), data_only=True)
                    file_stats = {'shifts': 0, 'expenses': 0, 'employees': 0, 'employee_shifts': 0}
                    conn.execute(
                        'INSERT INTO import_batches (branch_id, filename, imported_by) VALUES (?,?,?)',
                        (branch_id_int, fname, session.get('user_id'))
                    )
                    batch_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
                    for sheet_name in wb.sheetnames:
                        if sheet_name not in _XL_DAY_SHEETS:
                            continue
                        ws = wb[sheet_name]
                        stats = {'shifts': 0, 'expenses': 0, 'employees': 0, 'employee_shifts': 0}
                        _xl_process_sheet(ws, branch_id_int, conn, stats, batch_id=batch_id)
                        for k in file_stats:
                            file_stats[k] += stats[k]
                    conn.execute(
                        '''UPDATE import_batches SET shifts_created=?, expenses_created=?,
                           employees_created=?, employee_shifts_created=? WHERE id=?''',
                        (file_stats['shifts'], file_stats['expenses'],
                         file_stats['employees'], file_stats['employee_shifts'], batch_id)
                    )
                    flash(
                        f'«{fname}»: смен +{file_stats["shifts"]}, '
                        f'расходов +{file_stats["expenses"]}, '
                        f'записей ЗП +{file_stats["employee_shifts"]}',
                        'success'
                    )
        return redirect(url_for('gdrive_import'))

    # ── Загрузить файл вручную ───────────────────────────────────────────────
    elif action == 'upload':
        branches, api_key, import_batches = _get_ctx()
        branch_id_int = int(branch_id) if branch_id else None
        files = request.files.getlist('files')

        if not branch_id_int:
            flash('Выберите филиал', 'danger')
        elif not files or all(f.filename == '' for f in files):
            flash('Выберите хотя бы один файл', 'danger')
        else:
            try:
                import openpyxl
            except ImportError:
                flash('Библиотека openpyxl не установлена на сервере', 'danger')
                return redirect(url_for('gdrive_import'))

            with get_db() as conn:
                for file in files:
                    if not file.filename.lower().endswith('.xlsx'):
                        continue
                    data = file.read()
                    wb = openpyxl.load_workbook(_io.BytesIO(data), data_only=True)
                    file_stats = {'shifts': 0, 'expenses': 0, 'employees': 0, 'employee_shifts': 0}
                    conn.execute(
                        'INSERT INTO import_batches (branch_id, filename, imported_by) VALUES (?,?,?)',
                        (branch_id_int, file.filename, session.get('user_id'))
                    )
                    batch_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
                    for sheet_name in wb.sheetnames:
                        if sheet_name not in _XL_DAY_SHEETS:
                            continue
                        ws = wb[sheet_name]
                        stats = {'shifts': 0, 'expenses': 0, 'employees': 0, 'employee_shifts': 0}
                        _xl_process_sheet(ws, branch_id_int, conn, stats, batch_id=batch_id)
                        for k in file_stats:
                            file_stats[k] += stats[k]
                    conn.execute(
                        '''UPDATE import_batches SET shifts_created=?, expenses_created=?,
                           employees_created=?, employee_shifts_created=? WHERE id=?''',
                        (file_stats['shifts'], file_stats['expenses'],
                         file_stats['employees'], file_stats['employee_shifts'], batch_id)
                    )
                    flash(
                        f'«{file.filename}»: смен +{file_stats["shifts"]}, '
                        f'расходов +{file_stats["expenses"]}, '
                        f'записей ЗП +{file_stats["employee_shifts"]}',
                        'success'
                    )
        return redirect(url_for('gdrive_import'))

    branches, sa_json, import_batches = _get_ctx()
    return render_template('import_shifts.html',
                           branches=branches, sa_json=sa_json,
                           import_batches=import_batches,
                           folder_url=folder_url, branch_id=branch_id,
                           drive_files=drive_files)


# ─── CHANGE (РАЗМЕН) SETTINGS ────────────────────────────────────────────────

@app.route('/settings/change')
@login_required
@owner_required
def change_settings():
    WD_LABELS = ['Пн','Вт','Ср','Чт','Пт','Сб','Вс']
    today_str = date.today().isoformat()
    date_from = request.args.get('date_from', (date.today() - timedelta(days=6)).isoformat())
    date_to   = request.args.get('date_to',   date.today().isoformat())
    branch_ids_filter = request.args.getlist('branch_ids')

    # Build full date range list first
    dates_list = []
    try:
        d_cur = date.fromisoformat(date_from)
        d_end = date.fromisoformat(date_to)
        while d_cur <= d_end:
            wd = d_cur.weekday()
            dates_list.append({'iso': d_cur.isoformat(), 'weekday': wd,
                                'wd_label': WD_LABELS[wd], 'is_weekend': wd >= 5})
            d_cur += timedelta(days=1)
        dates_list.reverse()
    except Exception:
        pass

    with get_db() as conn:
        branches = conn.execute('SELECT * FROM branches WHERE is_active=1 ORDER BY name').fetchall()
        branch_groups_raw = conn.execute('SELECT * FROM branch_groups ORDER BY name').fetchall()
        branch_group_members = {}
        for m in conn.execute('SELECT * FROM branch_group_members').fetchall():
            branch_group_members.setdefault(m['group_id'], []).append(m['branch_id'])
        q_branches = branch_ids_filter if branch_ids_filter else [str(b['id']) for b in branches]
        q_branch_ids = [int(x) for x in q_branches]
        sel_branches = [b for b in branches if b['id'] in q_branch_ids]
        placeholders = ','.join('?' * len(q_branches))
        rows = conn.execute(f'''
            SELECT s.id as shift_id, s.date, b.id as branch_id,
                   s.status, COALESCE(r.change_amount, 0) as change_amount
            FROM shifts s
            JOIN branches b ON b.id = s.branch_id
            LEFT JOIN shift_revenue r ON r.shift_id = s.id
            WHERE s.date BETWEEN ? AND ?
              AND s.branch_id IN ({placeholders})
            ORDER BY s.date DESC, b.name
        ''', [date_from, date_to] + list(q_branches)).fetchall()
        schedules = conn.execute('''
            SELECT cs.*, b.name AS branch_name FROM change_schedule cs
            LEFT JOIN branches b ON b.id = cs.branch_id ORDER BY cs.id DESC
        ''').fetchall()

        # Build cross-tab grid
        grid = {}
        for r in rows:
            grid.setdefault(r['date'], {})[r['branch_id']] = {
                'shift_id': r['shift_id'], 'change_amount': r['change_amount'], 'status': r['status'],
            }

        # Add future "no-shift" cells (today and forward) with override or schedule amounts
        future_days = [d for d in dates_list if d['iso'] >= today_str]
        if future_days and sel_branches:
            b_ids = [b['id'] for b in sel_branches]
            fd_isos = [d['iso'] for d in future_days]
            ph_fd = ','.join('?' * len(fd_isos))
            ph_b  = ','.join('?' * len(b_ids))
            ov_rows = conn.execute(
                f'SELECT branch_id, date, amount FROM change_date_overrides WHERE date IN ({ph_fd}) AND branch_id IN ({ph_b})',
                fd_isos + b_ids
            ).fetchall()
            overrides = {(o['branch_id'], o['date']): o['amount'] for o in ov_rows}

            for day in future_days:
                day_iso, weekday = day['iso'], day['weekday']
                for b in sel_branches:
                    if b['id'] not in grid.get(day_iso, {}):
                        amount = overrides.get((b['id'], day_iso))
                        if amount is None:
                            srow = conn.execute(
                                '''SELECT amount FROM change_schedule
                                   WHERE (branch_id IS NULL OR branch_id=?)
                                     AND (weekday IS NULL OR weekday=?)
                                     AND valid_from <= ? AND (valid_to IS NULL OR valid_to >= ?)
                                   ORDER BY branch_id DESC NULLS LAST, weekday DESC NULLS LAST, id DESC
                                   LIMIT 1''',
                                (b['id'], weekday, day_iso, day_iso)
                            ).fetchone()
                            amount = srow['amount'] if srow else 0
                        grid.setdefault(day_iso, {})[b['id']] = {
                            'shift_id': None, 'status': 'future',
                            'change_amount': amount, 'branch_id': b['id'], 'date': day_iso,
                        }

    # Branch totals — real shifts only
    branch_totals = {}
    for cells in grid.values():
        for bid, cell in cells.items():
            if cell.get('status') != 'future':
                branch_totals[bid] = branch_totals.get(bid, 0) + (cell['change_amount'] or 0)

    return render_template('change_settings.html',
        branches=branches, branch_groups=branch_groups_raw,
        branch_group_members=branch_group_members,
        sel_branches=sel_branches, grid=grid,
        branch_totals=branch_totals, dates_list=dates_list,
        schedules=schedules,
        date_from=date_from, date_to=date_to,
        branch_ids_filter=branch_ids_filter,
        weekday_labels=WD_LABELS,
        today=today_str)


@app.route('/settings/change/manual/save', methods=['POST'])
@login_required
@owner_required
def change_manual_save():
    data = request.json or {}
    with get_db() as conn:
        for shift_id_str, amount_str in data.items():
            try:
                sid = int(shift_id_str)
                amt = float(str(amount_str).replace(' ', '').replace(',', '.') or 0)
            except (ValueError, TypeError):
                continue
            shift = conn.execute('SELECT status FROM shifts WHERE id=?', (sid,)).fetchone()
            if not shift or shift['status'] == 'closed':
                continue
            conn.execute('UPDATE shift_revenue SET change_amount=? WHERE shift_id=?', (amt, sid))
        conn.commit()
    return jsonify({'ok': True})


@app.route('/settings/change/future/save', methods=['POST'])
@login_required
@owner_required
def change_future_save():
    data = request.json or {}
    try:
        branch_id = int(data['branch_id'])
        date_str  = str(data['date'])
        amount    = float(str(data.get('amount', 0)).replace(' ', '').replace(',', '.') or 0)
    except (KeyError, ValueError, TypeError):
        return jsonify({'ok': False}), 400
    with get_db() as conn:
        conn.execute('''
            INSERT OR REPLACE INTO change_date_overrides (branch_id, date, amount)
            VALUES (?, ?, ?)
        ''', (branch_id, date_str, amount))
        conn.commit()
    return jsonify({'ok': True})


@app.route('/settings/change/schedule/add', methods=['POST'])
@login_required
@owner_required
def change_schedule_add():
    branch_id  = request.form.get('branch_id') or None
    weekday    = request.form.get('weekday')
    weekday    = int(weekday) if weekday and weekday.strip() else None
    amount     = float(request.form.get('amount') or 0)
    valid_from = request.form.get('valid_from')
    valid_to   = request.form.get('valid_to') or None
    label      = request.form.get('label', '').strip()
    if valid_from and valid_from < date.today().isoformat():
        flash('Дата начала не может быть в прошлом', 'danger')
        return redirect(url_for('change_settings') + '#tab-schedule')
    with get_db() as conn:
        conn.execute(
            'INSERT INTO change_schedule (branch_id, weekday, amount, valid_from, valid_to, label) VALUES (?,?,?,?,?,?)',
            (branch_id, weekday, amount, valid_from, valid_to, label)
        )
        _apply_all_change_schedules(conn)
        conn.commit()
    flash('Расписание сохранено и применено к сменам', 'success')
    return redirect(url_for('change_settings') + '#tab-schedule')


@app.route('/settings/change/schedule/delete/<int:schedule_id>', methods=['POST'])
@login_required
@owner_required
def change_schedule_delete(schedule_id):
    with get_db() as conn:
        conn.execute('DELETE FROM change_schedule WHERE id=?', (schedule_id,))
        conn.commit()
    flash('Расписание удалено', 'success')
    return redirect(url_for('change_settings') + '#tab-schedule')


@app.route('/settings/change/schedule/apply', methods=['POST'])
@login_required
@owner_required
def change_schedule_apply():
    with get_db() as conn:
        _apply_all_change_schedules(conn)
        conn.commit()
    flash('Расписание применено ко всем подходящим сменам', 'success')
    return redirect(url_for('change_settings') + '#tab-schedule')


# ──────────────────────────────────────────────────────────────────────────────

init_db()

if __name__ == '__main__':
    print('\n' + '=' * 50)
    print('CRM Суши запущена!')
    print('Откройте браузер: http://localhost:5050')
    print('Логин: owner | Пароль: admin123')
    print('=' * 50 + '\n')
    port = int(os.environ.get('PORT', 5050))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
