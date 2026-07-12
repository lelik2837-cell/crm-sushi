CREATE TABLE IF NOT EXISTS branches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    is_active INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('owner', 'admin', 'employee', 'director')),
    full_name TEXT NOT NULL,
    branch_id INTEGER REFERENCES branches(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS employees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_id INTEGER REFERENCES branches(id),
    full_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin', 'sushi', 'packer', 'courier', 'cleaner', 'cook')),
    rate REAL DEFAULT 0,
    rate_per_km REAL DEFAULT 10,
    rate_per_order REAL DEFAULT 100,
    is_active INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS employee_rate_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER REFERENCES employees(id),
    rate REAL NOT NULL,
    rate_per_km REAL DEFAULT 10,
    rate_per_order REAL DEFAULT 100,
    effective_from DATE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS shifts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_id INTEGER NOT NULL REFERENCES branches(id),
    date DATE NOT NULL,
    status TEXT DEFAULT 'open' CHECK(status IN ('open', 'closed')),
    opened_by INTEGER REFERENCES users(id),
    closed_by INTEGER REFERENCES users(id),
    opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    closed_at TIMESTAMP,
    comment TEXT,
    closed_by_name TEXT,
    UNIQUE(branch_id, date)
);

CREATE TABLE IF NOT EXISTS shift_revenue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shift_id INTEGER UNIQUE NOT NULL REFERENCES shifts(id),
    total_revenue REAL DEFAULT 0,
    delivery_revenue REAL DEFAULT 0,
    delivery_orders INTEGER DEFAULT 0,
    pickup_revenue REAL DEFAULT 0,
    pickup_orders INTEGER DEFAULT 0,
    cash_amount REAL DEFAULT 0,
    card_amount REAL DEFAULT 0,
    online_amount REAL DEFAULT 0,
    change_amount REAL DEFAULT 0,
    actual_cash REAL DEFAULT 0,
    terminal_last3 TEXT DEFAULT '',
    terminal_amount REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shift_id INTEGER NOT NULL REFERENCES shifts(id),
    category TEXT NOT NULL,
    description TEXT,
    amount_cash REAL DEFAULT 0,
    amount_card REAL DEFAULT 0,
    is_gulash INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS employee_shifts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shift_id INTEGER NOT NULL REFERENCES shifts(id),
    employee_id INTEGER REFERENCES employees(id),
    full_name_snapshot TEXT NOT NULL,
    role_snapshot TEXT NOT NULL,
    rate_snapshot REAL DEFAULT 0,
    rate_per_km_snapshot REAL DEFAULT 10,
    rate_per_order_snapshot REAL DEFAULT 100,
    shift_start TEXT,
    shift_end TEXT,
    hours_worked REAL DEFAULT 0,
    km REAL DEFAULT 0,
    orders INTEGER DEFAULT 0,
    bonus_amount REAL DEFAULT 0,
    penalty_amount REAL DEFAULT 0,
    bonus_comment TEXT,
    base_pay REAL DEFAULT 0,
    total_amount REAL DEFAULT 0,
    is_paid INTEGER DEFAULT 0,
    paid_amount REAL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS salary_payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER REFERENCES employees(id),
    employee_shift_id INTEGER REFERENCES employee_shifts(id),
    amount REAL NOT NULL,
    payment_date DATE NOT NULL,
    comment TEXT,
    paid_by INTEGER REFERENCES users(id),
    paid_by_name TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS expense_categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    label TEXT NOT NULL,
    is_active INTEGER DEFAULT 1,
    sort_order INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS kpi_blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    formula TEXT NOT NULL,
    color TEXT DEFAULT 'primary',
    unit TEXT DEFAULT '₽',
    sort_order INTEGER DEFAULT 0,
    is_active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS bonus_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,
    threshold_pct REAL NOT NULL,
    bonus_pct REAL NOT NULL,
    is_active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS change_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id),
    user_name TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_id INTEGER,
    shift_id INTEGER REFERENCES shifts(id),
    branch_id INTEGER,
    branch_name TEXT,
    shift_date DATE,
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

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

CREATE TABLE IF NOT EXISTS taxi_trip_employees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER NOT NULL REFERENCES taxi_trips(id) ON DELETE CASCADE,
    employee_id INTEGER REFERENCES employees(id),
    name_snapshot TEXT NOT NULL,
    address_snapshot TEXT
);

CREATE TABLE IF NOT EXISTS orders_import_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT,
    imported_count INTEGER DEFAULT 0,
    duplicate_count INTEGER DEFAULT 0,
    skipped_count INTEGER DEFAULT 0,
    created_by INTEGER,
    imported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS orders_report (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_number TEXT NOT NULL,
    branch_raw TEXT NOT NULL,
    branch_id INTEGER REFERENCES branches(id),
    received_at TEXT NOT NULL,
    promised_minutes INTEGER,
    order_type_raw TEXT,
    order_type TEXT,
    ready_minutes INTEGER,
    delivery_minutes INTEGER,
    promo_code TEXT,
    amount REAL DEFAULT 0,
    new_client TEXT,
    import_batch_id INTEGER REFERENCES orders_import_batches(id) ON DELETE CASCADE,
    import_hash TEXT UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_orders_report_received ON orders_report(received_at);
CREATE INDEX IF NOT EXISTS idx_orders_report_branch ON orders_report(branch_id);
CREATE INDEX IF NOT EXISTS idx_orders_report_number ON orders_report(order_number);
