-- Схема базы данных дизельного сервиса.
-- Все суммы хранятся в рублях (REAL, округление до копеек при записи),
-- даты — строками 'YYYY-MM-DD' / 'YYYY-MM-DD HH:MM:SS' по местному времени.

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Мастера: назначаются на работы, по ним считается зарплата.
-- В программу входит только руководитель (пароль хранится в settings).
CREATE TABLE IF NOT EXISTS employees (
    id         INTEGER PRIMARY KEY,
    full_name  TEXT NOT NULL,
    position   TEXT,
    phone      TEXT,
    is_master  INTEGER NOT NULL DEFAULT 1,
    is_active  INTEGER NOT NULL DEFAULT 1,
    notes      TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS clients (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL DEFAULT 'person' CHECK (kind IN ('person', 'company')),
    name       TEXT NOT NULL,
    phone      TEXT,
    phone2     TEXT,
    email      TEXT,
    inn        TEXT,
    address    TEXT,
    notes      TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Техника клиента: автомобиль, трактор, спецтехника, отдельный агрегат.
CREATE TABLE IF NOT EXISTS vehicles (
    id          INTEGER PRIMARY KEY,
    client_id   INTEGER NOT NULL REFERENCES clients (id) ON DELETE CASCADE,
    make        TEXT,
    model       TEXT,
    year        INTEGER,
    vin         TEXT,
    plate       TEXT,
    engine      TEXT,
    fuel_system TEXT,   -- тип топливной системы, номера форсунок / ТНВД
    notes       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS services (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    category       TEXT,
    price          REAL NOT NULL DEFAULT 0,
    master_percent REAL NOT NULL DEFAULT 0,
    is_active      INTEGER NOT NULL DEFAULT 1,
    notes          TEXT
);

-- Типы запчастей на складе (Автоэлектрика, Гидравлика, Фильтра…), порядок — position.
CREATE TABLE IF NOT EXISTS part_categories (
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL UNIQUE,
    position INTEGER NOT NULL DEFAULT 0
);

-- Цена продажи = закупка × (1 + наценка/100), округлённая до сотни.
-- Если markup_percent пустой — цена продажи задана вручную.
CREATE TABLE IF NOT EXISTS parts (
    id             INTEGER PRIMARY KEY,
    sku            TEXT,            -- артикул
    name           TEXT NOT NULL,
    brand          TEXT,
    category_id    INTEGER REFERENCES part_categories (id) ON DELETE SET NULL,
    unit           TEXT NOT NULL DEFAULT 'шт',
    quantity       REAL NOT NULL DEFAULT 0,
    purchase_price REAL NOT NULL DEFAULT 0,
    markup_percent REAL,
    sale_price     REAL NOT NULL DEFAULT 0,
    min_quantity   REAL NOT NULL DEFAULT 0,
    location       TEXT,            -- стеллаж / полка
    is_active      INTEGER NOT NULL DEFAULT 1,
    notes          TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Номер заказ-наряда начинается с 1 каждый календарный год: (year, number).
CREATE TABLE IF NOT EXISTS orders (
    id             INTEGER PRIMARY KEY,
    year           INTEGER NOT NULL,
    number         INTEGER NOT NULL,
    client_id      INTEGER NOT NULL REFERENCES clients (id),
    vehicle_id     INTEGER REFERENCES vehicles (id) ON DELETE SET NULL,
    status         TEXT NOT NULL DEFAULT 'new'
                   CHECK (status IN ('new', 'diagnostics', 'in_progress', 'waiting_parts',
                                     'done', 'issued', 'cancelled')),
    mileage        INTEGER,
    complaint      TEXT,   -- со слов клиента
    units          TEXT,   -- принятые агрегаты: форсунки, ТНВД, серийные номера
    diagnosis      TEXT,
    recommendations TEXT,
    notes          TEXT,
    responsible_id INTEGER REFERENCES employees (id) ON DELETE SET NULL,
    created_at     TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    planned_date   TEXT,
    completed_at   TEXT,   -- когда работы выполнены (статус «Готов» или «Выдан»)
    issued_at      TEXT    -- когда техника выдана клиенту
);

CREATE TABLE IF NOT EXISTS order_services (
    id             INTEGER PRIMARY KEY,
    order_id       INTEGER NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
    service_id     INTEGER REFERENCES services (id) ON DELETE SET NULL,
    name           TEXT NOT NULL,
    quantity       REAL NOT NULL DEFAULT 1,
    price          REAL NOT NULL DEFAULT 0,
    master_id      INTEGER REFERENCES employees (id) ON DELETE SET NULL,
    master_percent REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS order_parts (
    id             INTEGER PRIMARY KEY,
    order_id       INTEGER NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
    part_id        INTEGER NOT NULL REFERENCES parts (id),
    name           TEXT NOT NULL,
    sku            TEXT,
    unit           TEXT,
    quantity       REAL NOT NULL,
    price          REAL NOT NULL DEFAULT 0,
    purchase_price REAL NOT NULL DEFAULT 0
);

-- Движение по складу: приход (+), выдача в заказ-наряд (−), возврат (+),
-- списание (−), корректировка по инвентаризации (±).
CREATE TABLE IF NOT EXISTS stock_movements (
    id          INTEGER PRIMARY KEY,
    part_id     INTEGER NOT NULL REFERENCES parts (id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('receipt', 'order', 'return', 'writeoff', 'adjust')),
    quantity    REAL NOT NULL,
    price       REAL,
    supplier    TEXT,
    order_id    INTEGER REFERENCES orders (id) ON DELETE SET NULL,
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Доходы и расходы. Оплаты по заказ-нарядам и выплаты зарплаты тоже здесь.
CREATE TABLE IF NOT EXISTS transactions (
    id          INTEGER PRIMARY KEY,
    date        TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('income', 'expense')),
    category    TEXT NOT NULL,
    amount      REAL NOT NULL CHECK (amount > 0),
    method      TEXT NOT NULL DEFAULT 'cash' CHECK (method IN ('cash', 'card', 'bank')),
    description TEXT,
    order_id    INTEGER REFERENCES orders (id) ON DELETE SET NULL,
    employee_id INTEGER REFERENCES employees (id) ON DELETE SET NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_vehicles_client ON vehicles (client_id);
CREATE INDEX IF NOT EXISTS idx_orders_client ON orders (client_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders (status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_year_number ON orders (year, number);
CREATE INDEX IF NOT EXISTS idx_order_services_order ON order_services (order_id);
CREATE INDEX IF NOT EXISTS idx_order_services_master ON order_services (master_id);
CREATE INDEX IF NOT EXISTS idx_order_parts_order ON order_parts (order_id);
CREATE INDEX IF NOT EXISTS idx_stock_part ON stock_movements (part_id);
CREATE INDEX IF NOT EXISTS idx_parts_category ON parts (category_id);
CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions (date);
CREATE INDEX IF NOT EXISTS idx_transactions_order ON transactions (order_id);
CREATE INDEX IF NOT EXISTS idx_transactions_employee ON transactions (employee_id);

-- Суммы по заказ-наряду: total — работы + запчасти, paid — оплаты, due — остаток.
CREATE VIEW IF NOT EXISTS order_totals AS
SELECT order_id, total, paid, ROUND(total - paid, 2) AS due
FROM (
    SELECT o.id AS order_id,
           COALESCE((SELECT SUM(quantity * price) FROM order_services WHERE order_id = o.id), 0)
         + COALESCE((SELECT SUM(quantity * price) FROM order_parts WHERE order_id = o.id), 0)
             AS total,
           COALESCE((SELECT SUM(amount) FROM transactions
                     WHERE order_id = o.id AND kind = 'income'), 0) AS paid
    FROM orders o
);
