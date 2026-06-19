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

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
app.secret_key = 'sushi-crm-secret-2024-change-in-prod'
app.config['TEMPLATES_AUTO_RELOAD'] = True
# Railway и другие прокси-хосты: корректно определяем HTTPS и хост
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

DATABASE = os.environ.get('DATABASE_PATH', os.path.join(os.path.dirname(__file__), 'crm.db'))

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
        result.append({'date': date_val, 'amount': amount, 'description': desc, 'counterparty': ctr})

    if not result and rows:
        col_info = '; '.join(f'{k}={v}' for k, v in col.items() if v) or 'ни одна не определена'
        first_cols = ', '.join(str(f) for f in fieldnames[:8] if f)
        raise ValueError(
            f'Строки найдены ({len(rows)} шт.), но транзакции не распознаны. '
            f'Колонки файла: {first_cols}. '
            f'Маппинг: {col_info}.'
        )

    return result


def _match_contractors(conn, txns):
    contractors = conn.execute('SELECT id, name, category, keywords FROM contractors WHERE is_active=1').fetchall()
    for txn in txns:
        text = ((txn.get('description') or '') + ' ' + (txn.get('counterparty') or '')).lower()
        for c in contractors:
            kws = [k.strip().lower() for k in (c['keywords'] or c['name']).split(',') if k.strip()]
            if any(kw in text for kw in kws):
                txn['contractor_id'] = c['id']
                txn['category'] = txn.get('category') or c['category']
                break
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
            return render_template('dashboard_owner.html',
                branches=branches, stats=stats, weekly=weekly,
                open_shifts=open_shifts, kpi_blocks=kpi_blocks)
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
            return redirect(url_for('shift_view', shift_id=existing['id']))
        conn.execute(
            'INSERT INTO shifts (branch_id, date, opened_by) VALUES (?,?,?)',
            (branch_id, today, session['user_id'])
        )
        shift_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.execute('INSERT INTO shift_revenue (shift_id) VALUES (?)', (shift_id,))
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
        staff = conn.execute(
            'SELECT * FROM employee_shifts WHERE shift_id=? ORDER BY role_snapshot, full_name_snapshot',
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
        expense_cats = get_expense_categories(conn)
        expense_cats_groups = build_cats_groups(expense_cats)
        expense_cats_flat = [(c['code'], c['label']) for c in expense_cats]
        can_edit = (role == 'owner') or (shift['status'] == 'open')
        return render_template('shift.html',
            shift=shift, revenue=revenue, expenses=expenses,
            staff=staff, employees=employees,
            emp_addresses=emp_addresses,
            taxi_trips=taxi_trips, taxi_trip_emps=taxi_trip_emps,
            expense_categories=expense_cats_flat,
            expense_cats_groups=expense_cats_groups,
            role_labels=ROLE_LABELS,
            can_edit=can_edit,
            is_owner=(role == 'owner'))


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
                change_amount=?, actual_cash=?, terminal_last3=?, terminal_amount=?
            WHERE shift_id=?
        ''', (
            _f(data, 'total_revenue'), _f(data, 'delivery_revenue'), _i(data, 'delivery_orders'),
            _f(data, 'pickup_revenue'), _i(data, 'pickup_orders'),
            _f(data, 'cash_amount'), _f(data, 'card_amount'), _f(data, 'online_amount'),
            _f(data, 'change_amount'), _f(data, 'actual_cash'),
            data.get('terminal_last3', ''), _f(data, 'terminal_amount'),
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


@app.route('/shift/<int:shift_id>/close', methods=['POST'])
@login_required
def close_shift(shift_id):
    if not _can_edit_shift(shift_id):
        flash('Нет доступа', 'danger')
        return redirect(url_for('shift_view', shift_id=shift_id))
    comment = request.form.get('comment', '')
    closed_by_name = request.form.get('closed_by_name', session.get('full_name', ''))
    actual_cash_comment = request.form.get('actual_cash_comment', '').strip()
    with get_db() as conn:
        conn.execute('''
            UPDATE shifts SET status='closed', closed_by=?, closed_at=CURRENT_TIMESTAMP,
            comment=?, closed_by_name=?
            WHERE id=?
        ''', (session['user_id'], comment, closed_by_name, shift_id))
        if actual_cash_comment:
            conn.execute(
                'UPDATE shift_revenue SET actual_cash_comment=? WHERE shift_id=?',
                (actual_cash_comment, shift_id)
            )
        log_action(conn, 'shift_close', 'Смена закрыта', shift_id=shift_id)
        conn.commit()
    flash('Смена закрыта', 'success')
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
                    WHERE e.branch_id IN ({ids_str})
                    ORDER BY b.name, e.role, e.full_name
                ''').fetchall()
                branches = [b for b in all_branches if str(b['id']) in selected_branches]
            else:
                emps = conn.execute('''
                    SELECT e.*, b.name as branch_name
                    FROM employees e LEFT JOIN branches b ON b.id=e.branch_id
                    ORDER BY b.name, e.role, e.full_name
                ''').fetchall()
                branches = all_branches
        else:
            all_branches = []
            bids = _session_branch_ids()
            if bids:
                ids_str = ','.join(str(int(b)) for b in bids)
                emps = conn.execute(f'''
                    SELECT e.*, b.name as branch_name FROM employees e
                    LEFT JOIN branches b ON b.id=e.branch_id
                    WHERE e.branch_id IN ({ids_str}) ORDER BY b.name, e.role, e.full_name
                ''').fetchall()
                branches = conn.execute(f'SELECT * FROM branches WHERE id IN ({ids_str}) ORDER BY name').fetchall()
            else:
                emps = []
                branches = []

        # Rate history per employee
        rate_history = {}
        address_history = {}
        for emp in emps:
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

        all_tmpls = conn.execute(
            'SELECT * FROM rate_templates WHERE is_active=1 ORDER BY role, name'
        ).fetchall()
        # branch sets per template (empty set = all branches)
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

    return render_template('employees.html', employees=emps, branches=branches,
                           all_branches=all_branches,
                           selected_branches=selected_branches,
                           emp_branches_map=emp_branches_map,
                           role_labels=ROLE_LABELS, is_owner=(role == 'owner'),
                           rate_history=rate_history, address_history=address_history,
                           rate_templates=all_tmpls,
                           tmpl_branch_sets=tmpl_branch_sets,
                           tmpls_for_emp=tmpls_for_emp,
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
    if not full_name:
        flash('Введите фамилию сотрудника', 'danger')
        return redirect(url_for('employees'))
    with get_db() as conn:
        conn.execute(
            'INSERT INTO employees (branch_id, full_name, last_name, first_name, role, rate, rate_per_km, rate_per_order) VALUES (?,?,?,?,?,?,?,?)',
            (branch_id, full_name, last_name, first_name, emp_role, rate, rate_km, rate_ord)
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
    with get_db() as conn:
        emp = conn.execute('SELECT * FROM employees WHERE id=?', (emp_id,)).fetchone()
        if not emp:
            flash('Сотрудник не найден', 'danger')
            return redirect(url_for('employees'))
        if full_name:
            conn.execute(
                'UPDATE employees SET full_name=?, last_name=?, first_name=? WHERE id=?',
                (full_name, last_name, first_name, emp_id)
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
    branch_ids = [b for b in request.form.getlist('branch_ids') if b.isdigit()]
    if not name:
        flash('Введите название группы', 'danger')
        return redirect(url_for('branches'))
    with get_db() as conn:
        sort_order = conn.execute('SELECT COUNT(*) FROM branch_groups').fetchone()[0] * 10
        conn.execute('INSERT INTO branch_groups (name, sort_order) VALUES (?,?)', (name, sort_order))
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
    branch_ids = [b for b in request.form.getlist('branch_ids') if b.isdigit()]
    if not name:
        flash('Введите название группы', 'danger')
        return redirect(url_for('branches'))
    with get_db() as conn:
        conn.execute('UPDATE branch_groups SET name=? WHERE id=?', (name, group_id))
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
    with get_db() as conn:
        conn.execute('INSERT INTO branches (name, allowed_ip) VALUES (?,?)', (name, allowed_ip))
        conn.commit()
    flash(f'Филиал {name} добавлен', 'success')
    return redirect(url_for('branches'))


@app.route('/branches/<int:branch_id>/edit', methods=['POST'])
@login_required
@owner_required
def edit_branch(branch_id):
    allowed_ip = request.form.get('allowed_ip', '').strip() or None
    merchant_numbers = request.form.get('merchant_numbers', '').strip()
    with get_db() as conn:
        conn.execute(
            'UPDATE branches SET allowed_ip=?, merchant_numbers=? WHERE id=?',
            (allowed_ip, merchant_numbers, branch_id)
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
    return render_template('settings.html',
        exp_cats=exp_cats, exp_cats_parents=exp_cats_parents,
        kpi_blocks=kpi_blocks,
        bonus_rules=bonus_rules, branches=branches,
        rate_templates=rate_templates,
        tmpl_branches=tmpl_branches, tmpl_history=tmpl_history,
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
            SELECT s.date, b.name as branch_name, s.status,
                   COALESCE(r.total_revenue,0) as revenue,
                   COALESCE(r.delivery_revenue,0) as delivery,
                   COALESCE(r.pickup_revenue,0) as pickup,
                   COALESCE(r.delivery_orders,0)+COALESCE(r.pickup_orders,0) as orders,
                   COALESCE(r.cash_amount,0) as cash,
                   COALESCE(r.card_amount,0) as card,
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
                   COALESCE(SUM(r.card_amount),0) as card
            FROM shifts s LEFT JOIN shift_revenue r ON r.shift_id=s.id
            WHERE 1=1 {date_filter} {branch_filter}
        ''').fetchone()
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
            SELECT es.full_name_snapshot  AS name,
                   es.role_snapshot       AS role,
                   b.name                 AS branch_name,
                   COUNT(*)               AS shifts_count,
                   COALESCE(SUM(es.total_amount), 0)  AS earned,
                   COALESCE(SUM(es.paid_amount),  0)  AS paid,
                   COALESCE(SUM(es.total_amount - es.paid_amount), 0) AS debt
            FROM employee_shifts es
            JOIN shifts    s ON s.id    = es.shift_id
            JOIN branches  b ON b.id    = s.branch_id
            WHERE {sal_where}
            GROUP BY es.full_name_snapshot, es.role_snapshot, b.id
            {sal_having}
            ORDER BY b.name, es.role_snapshot, es.full_name_snapshot
        ''', sal_params).fetchall()

        # Список всех сотрудников в периоде (для дропдауна выбора)
        all_sal_emps = conn.execute(f'''
            SELECT DISTINCT es.full_name_snapshot AS name
            FROM employee_shifts es
            JOIN shifts s ON s.id = es.shift_id
            WHERE {sal_where}
            ORDER BY es.full_name_snapshot
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
                emp_filter_sql = f'AND es.full_name_snapshot IN ({placeholders})'
                raw_params = raw_params + s_emps

            raw_rows = conn.execute(f'''
                SELECT s.date, es.full_name_snapshot AS name,
                       COALESCE(es.total_amount, 0) AS earned,
                       COALESCE(es.paid_amount,  0) AS paid
                FROM employee_shifts es
                JOIN shifts s ON s.id = es.shift_id
                WHERE {sal_where} {emp_filter_sql}
                ORDER BY s.date, es.full_name_snapshot
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
        branch_groups=get_branch_groups(conn))


# ─── EXPENSES REPORT ──────────────────────────────────────────────────────────

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
        return jsonify({
            'id': emp['id'],
            'full_name': emp['full_name'],
            'role': emp['role'],
            'rate': emp['rate'],
            'rate_per_km': emp['rate_per_km'],
            'rate_per_order': emp['rate_per_order'],
        })


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
            raw = conn.execute('''
                SELECT bt.txn_date, t.branch_id, SUM(bt.amount) as total
                FROM bank_transactions bt
                JOIN bank_terminals t ON t.id=bt.terminal_id
                WHERE bt.txn_date BETWEEN ? AND ? AND bt.amount > 0 AND bt.is_ignored=0
                GROUP BY bt.txn_date, t.branch_id
                ORDER BY bt.txn_date DESC
            ''', (date_from, date_to)).fetchall()
            days = {}
            for r in raw:
                days.setdefault(r['txn_date'], {})[r['branch_id']] = r['total']
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

    return render_template('bank.html',
        tab=tab, date_from=date_from, date_to=date_to,
        accounts=accounts, acc_branches=acc_branches, statements=statements,
        contractors=contractors, terminals=terminals,
        branches=branches, exp_cats=exp_cats, ctr_cats=ctr_cats,
        beznal_rows=beznal_rows, beznal_branches=beznal_branches,
        expense_rows=expense_rows, expense_total=expense_total,
        compare_rows=compare_rows, compare_bank=compare_bank, compare_crm=compare_crm,
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
    flash(f'Загружено {len(txns)} транзакций. Назначьте контрагентов и терминалы.', 'success')
    return redirect(url_for('bank_classify', stmt_id=stmt_id))


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
            tid_numbers  = request.form.getlist('tid_number')
            tid_branches = request.form.getlist('tid_branch')
            tid_names    = request.form.getlist('tid_name')

            for name, cat, kw in zip(ctr_names, ctr_cats, ctr_keywords):
                name = (name or '').strip()
                cat  = (cat or '').strip()
                if not name or not cat:
                    continue
                kw = (kw or name).strip() or name
                existing = conn.execute(
                    'SELECT id FROM contractors WHERE LOWER(name)=LOWER(?)', (name,)
                ).fetchone()
                if existing:
                    conn.execute('UPDATE contractors SET category=?, keywords=? WHERE id=?',
                                 (cat, kw, existing['id']))
                    ctr_id = existing['id']
                else:
                    ctr_id = conn.execute(
                        'INSERT INTO contractors (name, category, keywords) VALUES (?,?,?)',
                        (name, cat, kw)
                    ).lastrowid
                conn.execute('''
                    UPDATE bank_transactions SET contractor_id=?, category=?
                    WHERE statement_id=? AND LOWER(TRIM(counterparty))=LOWER(?)
                ''', (ctr_id, cat, stmt_id, name))

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

        existing_ctrs = {c['name'].lower(): c for c in
                         conn.execute('SELECT name, category, keywords FROM contractors').fetchall()}

        counterparties = []
        for row in ctr_rows:
            name = row['name']
            ex   = existing_ctrs.get(name.lower())
            counterparties.append({
                'name':        name,
                'cnt':         row['cnt'],
                'total':       row['total'],
                'is_new':      ex is None,
                'current_cat': (ex['category'] if ex else row['category']) or '',
                'keywords':    (ex['keywords'] if ex else name) or name,
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
    name = request.form.get('name', '').strip()
    if name:
        with get_db() as conn:
            conn.execute('INSERT OR IGNORE INTO contractor_categories (name) VALUES (?)', (name,))
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
    keywords = request.form.get('keywords', '').strip()
    if not name:
        flash('Введите название контрагента', 'danger')
        return redirect(url_for('bank', tab='contractors'))
    with get_db() as conn:
        conn.execute(
            'INSERT INTO contractors (name, category, keywords) VALUES (?,?,?)',
            (name, category, keywords)
        )
        conn.commit()
    flash(f'Контрагент «{name}» добавлен', 'success')
    return redirect(url_for('bank', tab='contractors'))


@app.route('/bank/contractors/<int:ctr_id>/edit', methods=['POST'])
@login_required
@owner_required
def bank_contractor_edit(ctr_id):
    name = request.form.get('name', '').strip()
    category = request.form.get('category', '').strip()
    keywords = request.form.get('keywords', '').strip()
    with get_db() as conn:
        conn.execute(
            'UPDATE contractors SET name=?, category=?, keywords=? WHERE id=?',
            (name, category, keywords, ctr_id)
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
        txns = conn.execute('''
            SELECT bt.*, c.name as contractor_name,
                   t.terminal_number, b.name as terminal_branch
            FROM bank_transactions bt
            LEFT JOIN contractors c ON c.id=bt.contractor_id
            LEFT JOIN bank_terminals t ON t.id=bt.terminal_id
            LEFT JOIN branches b ON b.id=t.branch_id
            WHERE bt.statement_id=?
            ORDER BY bt.txn_date DESC, bt.id DESC
        ''', (stmt_id,)).fetchall()
        contractors = conn.execute('SELECT * FROM contractors WHERE is_active=1 ORDER BY name').fetchall()
        ctr_cats    = conn.execute('SELECT * FROM contractor_categories ORDER BY sort_order, name').fetchall()
    return render_template('bank_statement.html',
        stmt=stmt, txns=txns, contractors=contractors, ctr_cats=ctr_cats)


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
            _sber_set(conn, 'sber_client_id',      request.form.get('client_id', '').strip())
            _sber_set(conn, 'sber_client_secret',   request.form.get('client_secret', '').strip())
            _sber_set(conn, 'sber_account_number',  request.form.get('account_number', '').strip())
            _sber_set(conn, 'sber_auto_sync',       '1' if request.form.get('auto_sync') else '0')
            conn.commit()
            flash('Настройки Сбербанка сохранены', 'success')
            return redirect(url_for('sber_settings'))

        cfg = {
            'client_id':      _sber_get(conn, 'sber_client_id', '71154'),
            'client_secret':  _sber_get(conn, 'sber_client_secret'),
            'account_number': _sber_get(conn, 'sber_account_number'),
            'auto_sync':      _sber_get(conn, 'sber_auto_sync', '0'),
            'last_sync':      _sber_get(conn, 'sber_last_sync'),
            'last_result':    _sber_get(conn, 'sber_last_result'),
            'has_token':      bool(_sber_get(conn, 'sber_refresh_token')) or _sber_get(conn, 'sber_npa_active') == '1',
        }
        accounts = conn.execute('SELECT * FROM bank_accounts WHERE is_active=1 ORDER BY name').fetchall()
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


@app.route('/bank/sber/sync', methods=['POST'])
@login_required
@owner_required
def sber_sync():
    from sber_api import get_statement, parse_transactions
    import time

    data = request.get_json(silent=True) or {}
    date_from = data.get('date_from') or (date.today() - timedelta(days=7)).isoformat()
    date_to   = data.get('date_to')   or date.today().isoformat()

    with get_db() as conn:
        client_id       = _sber_get(conn, 'sber_client_id', '71154')
        account_number  = _sber_get(conn, 'sber_account_number')
        bank_account_id = data.get('bank_account_id') or _sber_get(conn, 'sber_bank_account_id')
        if not account_number:
            return jsonify({'ok': False, 'error': 'Не задан номер счёта'})

        try:
            from sber_api import get_npa_token, refresh_access_token
            cached_token  = _sber_get(conn, 'sber_access_token')
            token_exp_str = _sber_get(conn, 'sber_token_expires')
            refresh_token = _sber_get(conn, 'sber_refresh_token')
            now_ts = time.time()
            if cached_token and token_exp_str and float(token_exp_str) > now_ts + 30:
                access_token = cached_token
            elif refresh_token:
                # OAuth flow: обновляем через refresh_token
                client_secret = _sber_get(conn, 'sber_client_secret')
                tokens = refresh_access_token(client_id, client_secret, refresh_token)
                access_token = tokens['access_token']
                _sber_set(conn, 'sber_access_token',  access_token)
                _sber_set(conn, 'sber_token_expires', str(now_ts + int(tokens.get('expires_in', 3600))))
                if tokens.get('refresh_token'):
                    _sber_set(conn, 'sber_refresh_token', tokens['refresh_token'])
                conn.commit()
            else:
                return jsonify({'ok': False, 'error': 'Нет токена авторизации. Нажмите «Переподключить СберБизнес» и войдите снова.'})

            stmt_json = get_statement(access_token, client_id, account_number, date_from, date_to)
            txns = parse_transactions(stmt_json)

            if not txns:
                _sber_set(conn, 'sber_last_sync',   datetime.now().strftime('%d.%m.%Y %H:%M'))
                _sber_set(conn, 'sber_last_result', '0 новых транзакций')
                conn.commit()
                return jsonify({'ok': True, 'added': 0, 'raw': stmt_json})

            # Находим или создаём банковский счёт
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

            # Создаём выписку
            conn.execute(
                '''INSERT INTO bank_statements
                   (bank_account_id, filename, date_from, date_to, row_count, uploaded_at)
                   VALUES(?,?,?,?,?,?)''',
                (bank_account_id,
                 f'Сбербанк {date_from} — {date_to}',
                 date_from, date_to, len(txns),
                 datetime.now().isoformat())
            )
            stmt_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]

            # Применяем матчинг контрагентов и терминалов
            txns = _match_contractors(conn, txns)
            for txn in txns:
                if not txn.get('terminal_id'):
                    txn['terminal_id'] = _match_terminal(conn, txn)

            added = 0
            for txn in txns:
                # Пропускаем дубликаты по дате + сумме + описанию
                dup = conn.execute('''
                    SELECT id FROM bank_transactions
                    WHERE bank_account_id=? AND txn_date=? AND amount=? AND description=?
                ''', (bank_account_id, txn['date'], txn['amount'], txn.get('description',''))).fetchone()
                if dup:
                    continue
                conn.execute('''
                    INSERT INTO bank_transactions
                    (statement_id, bank_account_id, txn_date, amount,
                     description, counterparty, contractor_id, category, terminal_id)
                    VALUES(?,?,?,?,?,?,?,?,?)
                ''', (
                    stmt_id, bank_account_id, txn['date'], txn['amount'],
                    txn.get('description',''), txn.get('counterparty',''),
                    txn.get('contractor_id'), txn.get('category',''),
                    txn.get('terminal_id')
                ))
                added += 1

            _sber_set(conn, 'sber_last_sync',   datetime.now().strftime('%d.%m.%Y %H:%M'))
            _sber_set(conn, 'sber_last_result', f'+{added} новых транзакций из {len(txns)}')
            conn.commit()

            return jsonify({'ok': True, 'added': added, 'total': len(txns), 'stmt_id': stmt_id})

        except Exception as e:
            err = str(e)
            logging.exception('sber_sync error')
            _sber_set(conn, 'sber_last_result', f'Ошибка: {err[:500]}')
            conn.commit()
            return jsonify({'ok': False, 'error': err})


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

init_db()

if __name__ == '__main__':
    print('\n' + '=' * 50)
    print('CRM Суши запущена!')
    print('Откройте браузер: http://localhost:5050')
    print('Логин: owner | Пароль: admin123')
    print('=' * 50 + '\n')
    port = int(os.environ.get('PORT', 5050))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
