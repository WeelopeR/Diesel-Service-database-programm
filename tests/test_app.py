"""Проверка основных сценариев работы программы «Дизель Сервис»."""

import re
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from werkzeug.security import generate_password_hash

from app import auth, create_app, db

PASSWORD = "secret123"
YEAR = date.today().year


@pytest.fixture()
def app(tmp_path):
    return create_app({"DATA_DIR": str(tmp_path), "TESTING": True})


@pytest.fixture()
def client(app):
    auth._failed_logins.clear()
    return app.test_client()


def csrf(client, url="/login"):
    html = client.get(url).get_data(as_text=True)
    match = re.search(r'name="_csrf" value="([^"]+)"', html)
    assert match, f"нет CSRF-токена на {url}"
    return match.group(1)


def post(client, url, data=None, form_url=None):
    data = dict(data or {})
    data["_csrf"] = csrf(client, form_url or url)
    return client.post(url, data=data)


def query(app, sql, params=()):
    with app.app_context():
        return db.query(sql, params)


def scalar(app, sql, params=()):
    with app.app_context():
        return db.scalar(sql, params)


def text(response):
    return response.get_data(as_text=True)


@pytest.fixture()
def admin(client):
    response = post(client, "/setup", {"password": PASSWORD, "password2": PASSWORD})
    assert response.status_code == 302
    return client


# ---------- Вход ----------

def test_first_run_asks_for_password(client):
    response = client.get("/")
    assert response.status_code == 302 and "/setup" in response.headers["Location"]
    short = post(client, "/setup", {"password": "123", "password2": "123"})
    assert "не короче 8" in text(short)


def test_password_only_login(admin):
    post(admin, "/logout", form_url="/")
    assert admin.get("/").status_code == 302
    login_page = text(admin.get("/login"))
    assert 'name="password"' in login_page and 'name="username"' not in login_page

    assert "Неверный пароль" in text(post(admin, "/login", {"password": "wrong-pass"}))
    assert post(admin, "/login", {"password": PASSWORD}).status_code == 302
    assert admin.get("/orders/").status_code == 200


def test_password_change_logs_out_other_sessions(app, admin):
    other = app.test_client()
    post(other, "/login", {"password": PASSWORD})
    assert other.get("/").status_code == 200
    post(admin, "/password", {"current": PASSWORD, "password": "newpass123",
                              "password2": "newpass123"})
    assert other.get("/").status_code == 302
    assert admin.get("/").status_code == 200


def test_reset_password_from_console(app, admin):
    with app.app_context():
        auth.set_password("restored-pass")
    assert admin.get("/").status_code == 302
    assert post(admin, "/login", {"password": "restored-pass"}).status_code == 302


def test_post_without_csrf_rejected(admin):
    assert admin.post("/clients/new", data={"name": "Без токена"}).status_code == 400


def test_open_redirect_blocked(admin):
    post(admin, "/logout", form_url="/")
    response = post(admin, "/login?next=//evil.example.com", {"password": PASSWORD})
    assert "evil" not in response.headers["Location"]


def test_login_lockout(admin):
    post(admin, "/logout", form_url="/")
    for _ in range(5):
        post(admin, "/login", {"password": "nope-nope"})
    assert "Слишком много" in text(post(admin, "/login", {"password": PASSWORD}))


# ---------- Данные для сценариев ----------

def make_client(app, client, name, phone, make, model, plate):
    post(client, "/clients/new", {"kind": "person", "name": name, "phone": phone,
                                  "v_make": make, "v_model": model, "v_plate": plate})
    client_id = scalar(app, "SELECT id FROM clients WHERE name = ?", (name,))
    vehicle_id = scalar(app, "SELECT id FROM vehicles WHERE client_id = ?", (client_id,))
    return client_id, vehicle_id


def make_order(app, client, client_id, vehicle_id, **extra):
    url = f"/orders/new?client_id={client_id}"
    response = post(client, url, {"client_id": client_id, "vehicle_id": vehicle_id, **extra},
                    form_url=url)
    return int(re.search(r"/orders/(\d+)", response.headers["Location"]).group(1))


def create_full_order(app, client):
    """Мастер → клиент → услуга → запчасть с приходом → заказ-наряд с работой и запчастью."""
    post(client, "/masters/new", {"full_name": "Сидоров Иван", "is_active": "1"})
    master_id = scalar(app, "SELECT id FROM employees WHERE full_name = 'Сидоров Иван'")

    client_id, vehicle_id = make_client(app, client, "Иванов Сергей", "+7 (900) 123-45-67",
                                        "КАМАЗ", "5490", "а123вс77")
    assert scalar(app, "SELECT plate FROM vehicles WHERE id = ?", (vehicle_id,)) == "А123ВС77"

    post(client, "/services/new", {"name": "Ремонт форсунки CR", "category": "Форсунки",
                                    "price": "4 500", "master_percent": "40", "is_active": "1"})
    service_id = scalar(app, "SELECT id FROM services")

    post(client, "/warehouse/new", {"name": "Распылитель DLLA", "sku": "DLLA150", "unit": "шт",
                                     "purchase_price": "1000", "markup_percent": "80",
                                     "min_quantity": "2", "is_active": "1",
                                     "initial_quantity": "3"})
    assert scalar(app, "SELECT sale_price FROM parts") == 1800
    part_id = scalar(app, "SELECT id FROM parts")
    post(client, "/warehouse/receipt", {
        "supplier": "ООО Поставщик", "document": "Накл. 15", "date": f"{YEAR}-01-20",
        "part_id": str(part_id), "quantity": "5", "price": "1100",
        "record_expense": "1", "update_prices": "1", "method": "bank",
    })
    assert scalar(app, "SELECT quantity FROM parts WHERE id = ?", (part_id,)) == 8
    # Новая закупка 1100 × 1,8 = 1980 → округлено до 2000
    assert scalar(app, "SELECT sale_price FROM parts WHERE id = ?", (part_id,)) == 2000

    order_id = make_order(app, client, client_id, vehicle_id, mileage="350000",
                          complaint="Дымит", units="Форсунки 4 шт.", responsible_id=master_id)
    view = f"/orders/{order_id}"
    post(client, f"{view}/services/add", {"service_id": str(service_id), "quantity": "4",
                                          "master_id": str(master_id)}, form_url=view)
    post(client, f"{view}/parts/add", {"part_id": str(part_id), "quantity": "4"}, form_url=view)
    return {"order_id": order_id, "part_id": part_id, "master_id": master_id,
            "client_id": client_id, "vehicle_id": vehicle_id, "view": view}


def add_old_order(app, client_id, vehicle_id, year, number, status, total=0, paid=0):
    """Заказ-наряд прошлого года (как будто создан тогда)."""
    with app.app_context():
        order_id = db.execute(
            "INSERT INTO orders (year, number, client_id, vehicle_id, status, created_at, "
            "completed_at, issued_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (year, number, client_id, vehicle_id, status, f"{year}-12-20 10:00:00",
             f"{year}-12-21 10:00:00" if status in ("done", "issued") else None,
             f"{year}-12-22 10:00:00" if status == "issued" else None),
        )
        if total:
            db.execute("INSERT INTO order_services (order_id, name, quantity, price) "
                       "VALUES (?, 'Работа прошлого года', 1, ?)", (order_id, total))
        if paid:
            db.execute("INSERT INTO transactions (date, kind, category, amount, order_id) "
                       "VALUES (?, 'income', 'Оплата заказ-наряда', ?, ?)",
                       (f"{year}-12-22", paid, order_id))
        db.commit()
    return order_id


# ---------- Заказ-наряды ----------

def test_full_order_flow(app, admin):
    ctx = create_full_order(app, admin)
    order_id, part_id, view = ctx["order_id"], ctx["part_id"], ctx["view"]

    line = query(app, "SELECT * FROM order_services WHERE order_id = ?", (order_id,))[0]
    assert (line["price"], line["master_percent"], line["quantity"]) == (4500, 40, 4)
    part_line = query(app, "SELECT * FROM order_parts WHERE order_id = ?", (order_id,))[0]
    assert part_line["price"] == 2000
    assert scalar(app, "SELECT quantity FROM parts WHERE id = ?", (part_id,)) == 4

    # Нельзя списать больше, чем есть
    post(admin, f"{view}/parts/add", {"part_id": str(part_id), "quantity": "10"}, form_url=view)
    assert scalar(app, "SELECT quantity FROM parts WHERE id = ?", (part_id,)) == 4

    assert "26 000 ₽" in text(admin.get(view))   # 4×4500 + 4×2000
    assert scalar(app, "SELECT amount FROM transactions WHERE category = 'Закупка запчастей'") \
        == 5500

    post(admin, f"{view}/status", {"status": "done"}, form_url=view)
    post(admin, f"{view}/payments/add", {"amount": "26000", "method": "card"}, form_url=view)
    post(admin, f"{view}/status", {"status": "issued"}, form_url=view)
    order = query(app, "SELECT * FROM orders WHERE id = ?", (order_id,))[0]
    assert order["status"] == "issued" and order["completed_at"] and order["issued_at"]
    payment = query(app, "SELECT * FROM transactions WHERE order_id = ?", (order_id,))[0]
    assert payment["amount"] == 26000 and f"№ 1/{YEAR}" in payment["description"]

    salary = text(admin.get("/salary/"))
    assert "Сидоров Иван" in salary and "7 200 ₽" in salary   # 18 000 × 40 %
    post(admin, f"/salary/{ctx['master_id']}/pay", {"amount": "5000", "method": "cash"},
         form_url=f"/salary/{ctx['master_id']}")
    assert "2 200 ₽" in text(admin.get(f"/salary/{ctx['master_id']}"))

    # В выданном заказ-наряде менять состав нельзя
    post(admin, f"{view}/parts/{part_line['id']}/delete", form_url=view)
    assert scalar(app, "SELECT COUNT(*) FROM order_parts WHERE order_id = ?", (order_id,)) == 1

    printed = text(admin.get(f"{view}/print"))
    assert "ЗАКАЗ-НАРЯД № 1<" in printed and "Ремонт форсунки CR" in printed


def test_print_form_without_warranty_and_with_overpayment(app, admin):
    ctx = create_full_order(app, admin)            # итого 26 000 ₽
    view = ctx["view"]
    printed = text(admin.get(f"{view}/print"))
    assert "Гарантия" not in printed and "гарант" not in printed.lower()
    assert "Задолженности нет" not in printed

    post(admin, f"{view}/payments/add", {"amount": "10000"}, form_url=view)
    printed = text(admin.get(f"{view}/print"))
    assert "Остаток к оплате" in printed and "16\u00a0000\u00a0₽" in printed
    post(admin, f"{view}/payments/add", {"amount": "20000"}, form_url=view)
    printed = text(admin.get(f"{view}/print"))
    assert "Переплата" in printed and "4\u00a0000\u00a0₽" in printed

    # Сохранение настроек не стирает сохранённый текст гарантии (пригодится для кнопки)
    post(admin, "/settings/", {"company_name": "Дизель Сервис"})
    assert "Гарантия" in scalar(app, "SELECT value FROM settings WHERE key = 'order_footer'")


def test_warranty_button(app, admin):
    ctx = create_full_order(app, admin)
    assert f'href="{ctx["view"]}/warranty"' in text(admin.get(ctx["view"]))
    page = text(admin.get(f"{ctx['view']}/warranty"))
    assert "Шаблон гарантии пока не оформлен" in page
    assert admin.get("/orders/99999/warranty").status_code == 404


def test_cancel_returns_parts(app, admin):
    ctx = create_full_order(app, admin)
    post(admin, f"{ctx['view']}/status", {"status": "cancelled"}, form_url=ctx["view"])
    assert scalar(app, "SELECT quantity FROM parts WHERE id = ?", (ctx["part_id"],)) == 8
    assert scalar(app, "SELECT COUNT(*) FROM order_parts") == 0


def test_cancel_blocked_with_payments(app, admin):
    ctx = create_full_order(app, admin)
    post(admin, f"{ctx['view']}/payments/add", {"amount": "1000"}, form_url=ctx["view"])
    post(admin, f"{ctx['view']}/status", {"status": "cancelled"}, form_url=ctx["view"])
    assert scalar(app, "SELECT status FROM orders WHERE id = ?", (ctx["order_id"],)) == "new"


def test_part_quantity_change_moves_stock(app, admin):
    ctx = create_full_order(app, admin)
    line_id = scalar(app, "SELECT id FROM order_parts")
    post(admin, f"{ctx['view']}/parts/{line_id}/update", {"quantity": "2", "price": "2000"},
         form_url=ctx["view"])
    assert scalar(app, "SELECT quantity FROM parts WHERE id = ?", (ctx["part_id"],)) == 6
    post(admin, f"{ctx['view']}/parts/{line_id}/delete", form_url=ctx["view"])
    assert scalar(app, "SELECT quantity FROM parts WHERE id = ?", (ctx["part_id"],)) == 8


# ---------- Нумерация по годам и архив ----------

def test_numbering_restarts_each_year(app, admin):
    client_id, vehicle_id = make_client(app, admin, "Петров", "", "MAN", "TGS", "")
    for n in range(1, 6):
        add_old_order(app, client_id, vehicle_id, YEAR - 1, n, "issued")

    first = make_order(app, admin, client_id, vehicle_id)
    second = make_order(app, admin, client_id, vehicle_id)
    numbers = [scalar(app, "SELECT number || '/' || year FROM orders WHERE id = ?", (i,))
               for i in (first, second)]
    assert numbers == [f"1/{YEAR}", f"2/{YEAR}"]

    # Номер удалённого заказ-наряда повторно не выдаётся
    post(admin, f"/orders/{second}/delete", form_url=f"/orders/{second}")
    third = make_order(app, admin, client_id, vehicle_id)
    assert scalar(app, "SELECT number FROM orders WHERE id = ?", (third,)) == 3


def test_archive_holds_previous_years(app, admin):
    client_id, vehicle_id = make_client(app, admin, "Архивный клиент", "", "John Deere",
                                        "8320R", "")
    closed = add_old_order(app, client_id, vehicle_id, YEAR - 1, 7, "issued", 1000, 1000)
    still_open = add_old_order(app, client_id, vehicle_id, YEAR - 1, 8, "in_progress", 500)
    debt = add_old_order(app, client_id, vehicle_id, YEAR - 2, 3, "issued", 2000, 500)
    current = make_order(app, admin, client_id, vehicle_id)

    def ids(html):
        return {int(x) for x in re.findall(r'data-href="/orders/(\d+)"', html)}

    main = ids(text(admin.get("/orders/?status=all")))
    assert current in main and still_open in main and closed not in main and debt not in main

    archive = ids(text(admin.get("/orders/archive")))
    assert {closed, still_open, debt} <= archive and current not in archive
    assert ids(text(admin.get(f"/orders/archive?year={YEAR - 2}"))) == {debt}
    assert f"7/{YEAR - 1}" in text(admin.get("/orders/archive"))

    # Долги прошлых лет видны и в общем списке
    debts = ids(text(admin.get("/orders/?payment=debt&status=all")))
    assert debts == {debt}
    assert ids(text(admin.get(f"/orders/archive?number=3/{YEAR - 2}"))) == {debt}


# ---------- Фильтры ----------

def test_order_filters(app, admin):
    kamaz_client, kamaz = make_client(app, admin, "Иванов Сергей", "+7 (912) 345-67-89",
                                      "КАМАЗ", "5490", "А123ВС116")
    toyota_client, toyota = make_client(app, admin, "Петренко Олег", "+7 917 111-22-33",
                                        "Toyota", "Land Cruiser", "О777ОО116")
    o1 = make_order(app, admin, kamaz_client, kamaz)
    o2 = make_order(app, admin, toyota_client, toyota)
    with app.app_context():
        db.execute("INSERT INTO order_services (order_id, name, quantity, price) "
                   "VALUES (?, 'Работа', 1, 3000)", (o2,))
        db.execute("UPDATE orders SET status = 'issued' WHERE id = ?", (o2,))
        db.commit()

    def ids(url):
        return {int(x) for x in re.findall(r'data-href="/orders/(\d+)"', text(admin.get(url)))}

    assert ids("/orders/?status=all&number=2") == {o2}
    assert ids("/orders/?status=all&vehicle=камаз") == {o1}          # без учёта регистра
    assert ids("/orders/?status=all&vehicle=land cruiser") == {o2}
    assert ids("/orders/?status=all&vehicle=о777") == {o2}           # госномер
    assert ids("/orders/?status=all&client=иванов") == {o1}
    assert ids("/orders/?status=all&client=912 345") == {o1}         # цифры телефона
    assert ids("/orders/?status=all&payment=debt") == {o2}
    page = text(admin.get("/orders/?status=all&payment=debt"))
    assert "долг <strong" in page and "3 000 ₽" in page


def test_client_filters(app, admin):
    debtor, debtor_vehicle = make_client(app, admin, "Должник Иван", "+7 900 555-44-33",
                                         "MAN", "TGS", "Е456КХ716")
    make_client(app, admin, "ООО «Чистые»", "+7 843 200-10-10", "Volvo", "FH", "")
    order_id = make_order(app, admin, debtor, debtor_vehicle)
    with app.app_context():
        db.execute("INSERT INTO order_services (order_id, name, quantity, price) "
                   "VALUES (?, 'Работа', 1, 4800)", (order_id,))
        db.execute("UPDATE orders SET status = 'issued' WHERE id = ?", (order_id,))
        db.commit()

    def names(url):
        return re.findall(r'class="strong">([^<]+)</a>', text(admin.get(url)))

    assert names("/clients/?debt=1") == ["Должник Иван"]
    assert names("/clients/?vehicle=volvo") == ["ООО «Чистые»"]
    assert names("/clients/?q=должник") == ["Должник Иван"]
    assert names("/clients/?q=5554433") == ["Должник Иван"]
    assert names("/clients/?sort=debt")[0] == "Должник Иван"
    assert "4 800 ₽" in text(admin.get(f"/clients/{debtor}"))


# ---------- Склад ----------

def make_part(app, client, name, category=None, **extra):
    data = {"name": name, "unit": "шт", "is_active": "1", **extra}
    if category:
        data["category_id"] = scalar(app, "SELECT id FROM part_categories WHERE name = ?",
                                     (category,))
    post(client, "/warehouse/new", data)
    return scalar(app, "SELECT id FROM parts WHERE name = ?", (name,))


def test_part_categories_seeded_and_editable(app, admin):
    names = [r["name"] for r in query(app, "SELECT name FROM part_categories ORDER BY position")]
    assert names == ["Автоэлектрика", "Гидравлика",
                     "Запчасти для ремонта импортной мех. техники",
                     "Запчасти для ремонта отечественной мех. техники",
                     "Расходные материалы", "Фильтра", "Готовые форсунки на продажу"]
    filters_id = scalar(app, "SELECT id FROM part_categories WHERE name = 'Фильтра'")
    url = "/warehouse/categories"
    post(admin, url, {"action": "rename", "id": filters_id, "name": "Фильтры"})
    post(admin, url, {"action": "up", "id": filters_id})
    post(admin, url, {"action": "add", "name": "Инструмент"})
    post(admin, url, {"action": "add", "name": "Инструмент"})       # дубль не создаётся
    names = [r["name"] for r in query(app, "SELECT name FROM part_categories ORDER BY position")]
    assert names[4:6] == ["Фильтры", "Расходные материалы"] and names[-1] == "Инструмент"
    assert names.count("Инструмент") == 1

    part_id = make_part(app, admin, "Фильтр топливный", "Фильтры", purchase_price="500",
                        markup_percent="40")
    post(admin, url, {"action": "delete", "id": filters_id})
    assert scalar(app, "SELECT category_id FROM parts WHERE id = ?", (part_id,)) is None


def test_markup_price_rounding(app, admin):
    # От 100 ₽ — всегда вверх до 50 ₽; дешевле 100 ₽ — без округления.
    cases = [("120", "0", 150), ("150", "0", 150), ("151", "0", 200), ("60", "100", 150),
             ("1170", "30", 1550), ("1000", "30", 1300), ("2101", "0", 2150), ("2151", "0", 2200),
             ("30", "50", 45), ("33", "30", 42.9), ("76.92", "30", 100)]
    for i, (purchase, markup, expected) in enumerate(cases):
        part_id = make_part(app, admin, f"Позиция {i}", purchase_price=purchase,
                            markup_percent=markup)
        assert scalar(app, "SELECT sale_price FROM parts WHERE id = ?", (part_id,)) == expected
    manual = make_part(app, admin, "Ручная цена", purchase_price="1000", sale_price="1234")
    row = query(app, "SELECT * FROM parts WHERE id = ?", (manual,))[0]
    assert row["markup_percent"] is None and row["sale_price"] == 1234


def test_receipt_raises_sale_price_only_when_purchase_rises(app, admin):
    by_markup = make_part(app, admin, "Фильтр топливный", purchase_price="1000", markup_percent="30")
    manual = make_part(app, admin, "Форсунка готовая", purchase_price="200", sale_price="330")
    no_price = make_part(app, admin, "Новая позиция", markup_percent="50")

    def receive(part_id, price, update=True):
        data = {"part_id": str(part_id), "quantity": "1", "price": price}
        if update:
            data["update_prices"] = "1"
        return post(admin, "/warehouse/receipt", data)

    def card(part_id):
        row = query(app, "SELECT purchase_price, sale_price FROM parts WHERE id = ?", (part_id,))[0]
        return row["purchase_price"], row["sale_price"]

    assert card(by_markup) == (1000, 1300)
    # Подорожала: 1 080 × 1,3 = 1 404 → вверх до 50 ₽ = 1 450
    response = receive(by_markup, "1080")
    assert card(by_markup) == (1080, 1450)
    page = text(admin.get(response.headers["Location"]))
    assert "цена продажи повышена" in page and "1 300 ₽ → 1 450 ₽" in page.replace("\xa0", " ")
    # Подешевела или та же цена — карточка не меняется, цена продажи не снижается
    receive(by_markup, "900")
    assert card(by_markup) == (1080, 1450)
    receive(by_markup, "1080")
    assert card(by_markup) == (1080, 1450)
    # Галочка снята — карточка не меняется даже при подорожании
    receive(by_markup, "1500", update=False)
    assert card(by_markup) == (1080, 1450)

    # Без наценки: та же разница, что была (330 / 200), 250 × 1,65 = 412,5 → 450
    receive(manual, "250")
    assert card(manual) == (250, 450)
    receive(manual, "240")
    assert card(manual) == (250, 450)

    # Закупка была не указана, наценка есть: 80 × 1,5 = 120 → 150
    receive(no_price, "80")
    assert card(no_price) == (80, 150)
    assert scalar(app, "SELECT quantity FROM parts WHERE id = ?", (by_markup,)) == 4


def test_prices_recalculated_after_update(tmp_path):
    """Цены по наценке, посчитанные прошлыми версиями (до ближайшей сотни, минимум 100 ₽;
    вверх до сотни), пересчитываются по новому правилу при запуске."""
    app = create_app({"DATA_DIR": str(tmp_path), "TESTING": True})
    with app.app_context():
        db.execute("INSERT INTO parts (name, purchase_price, markup_percent, sale_price) "
                   "VALUES ('По наценке', 1170, 30, 1500), ('Дешёвая', 30, 50, 100), "
                   "('Вручную', 1170, NULL, 1555)")
        db.get_db().execute("PRAGMA user_version = 2")
        db.commit()
    app = create_app({"DATA_DIR": str(tmp_path), "TESTING": True})
    prices = {r["name"]: r["sale_price"] for r in query(app, "SELECT name, sale_price FROM parts")}
    assert prices == {"По наценке": 1550, "Дешёвая": 45, "Вручную": 1555}


def test_warehouse_columns_and_filters(app, admin):
    nozzle = make_part(app, admin, "Форсунка Bosch 0 445 110 376", "Готовые форсунки на продажу",
                       sku="0 445 110 376", purchase_price="12000", markup_percent="25",
                       initial_quantity="4", brand="Bosch")
    filter_ = make_part(app, admin, "Фильтр топливный Mann", "Фильтра", sku="WK 8110",
                        purchase_price="900", markup_percent="50", initial_quantity="10",
                        min_quantity="5")
    oil = make_part(app, admin, "Масло моторное", "Расходные материалы", unit="л",
                    purchase_price="400", markup_percent="40", initial_quantity="20")
    # Движение: 7 фильтров в расход, 2 литра масла списано в январе 2020
    with app.app_context():
        from app.stock import move_stock
        move_stock(filter_, "order", -7)
        move_stock(oil, "writeoff", -2)
        db.execute("UPDATE stock_movements SET created_at = '2020-01-15 10:00:00' "
                   "WHERE part_id = ? AND kind = 'writeoff'", (oil,))
        db.commit()

    def found(url):
        html = text(admin.get(url))
        return [int(x) for x in re.findall(r'data-href="/warehouse/(\d+)"', html)], html

    # Обзор: карточка на каждый тип со своими цифрами
    overview = text(admin.get("/warehouse/"))
    assert "Готовые форсунки на продажу" in overview and "1 <span>позиция</span>" in overview
    assert "заканчиваются: 1" in overview

    ids, html = found("/warehouse/list/all")
    assert set(ids) == {nozzle, filter_, oil}
    for header in ("Артикул", "Наименование", "Ед. изм.", "Наценка", "Цена продажи", "Приход",
                   "Расход", "Остаток"):
        assert header in html
    assert "15 000 ₽" in html                          # 12 000 × 1,25

    def ids_of(url):
        return set(found(url)[0])

    cat = scalar(app, "SELECT id FROM part_categories WHERE name = 'Фильтра'")
    assert ids_of(f"/warehouse/list/{cat}") == {filter_}          # отдельная страница типа
    assert "Фильтр топливный Mann" in text(admin.get(f"/warehouse/list/{cat}"))
    assert ids_of(f"/warehouse/list/{cat}?name=масло") == set()   # фильтр внутри типа
    assert admin.get("/warehouse/list/99999").status_code == 404
    old_link = admin.get(f"/warehouse/?category={cat}&show=low")
    assert old_link.status_code == 302 and f"/warehouse/list/{cat}" in old_link.headers["Location"]

    assert ids_of("/warehouse/list/all?q=0445110") == {nozzle}     # быстрый поиск: артикул
    assert ids_of("/warehouse/list/all?q=масло") == {oil}          # быстрый поиск: название
    assert ids_of("/warehouse/list/all?sku=0445110") == {nozzle}   # артикул без пробелов
    assert ids_of("/warehouse/list/all?name=bosch форсунка") == {nozzle}
    assert ids_of("/warehouse/list/all?unit=л") == {oil}
    assert ids_of("/warehouse/list/all?markup_min=30&markup_max=45") == {oil}
    assert ids_of("/warehouse/list/all?price_min=10000") == {nozzle}
    assert ids_of("/warehouse/list/all?out_min=1") == {filter_, oil}
    assert ids_of("/warehouse/list/all?balance_max=3") == {filter_}   # 10 − 7 = 3
    assert ids_of("/warehouse/list/all?stock=low") == {filter_}
    # Период: списание масла было только в январе 2020
    assert ids_of("/warehouse/list/all?start=2020-01-01&end=2020-12-31&stock=moved") == {oil}
    assert ids_of("/warehouse/list/all?start=2020-01-01&end=2020-12-31&out_min=2&out_max=2") \
        == {oil}
    # Остаток на конец периода: на 31.01.2020 масла ещё не было (приход позже)
    assert ids_of("/warehouse/list/all?end=2020-01-31&balance_min=1") == set()
    # Сортировка по остатку по убыванию, без группировки
    assert found("/warehouse/list/all?sort=balance&dir=desc&group=0")[0] == [oil, nozzle, filter_]


def test_warehouse_pagination_keeps_type(app, admin):
    cat = scalar(app, "SELECT id FROM part_categories WHERE name = 'Расходные материалы'")
    with app.app_context():
        for i in range(105):
            db.execute("INSERT INTO parts (name, category_id) VALUES (?, ?)", (f"Кольцо {i:03d}", cat))
        db.commit()
    html = text(admin.get(f"/warehouse/list/{cat}"))
    assert f'href="/warehouse/list/{cat}?page=2"' in html
    assert "Кольцо 104" in text(admin.get(f"/warehouse/list/{cat}?page=2"))


# ---------- Прочее ----------

def test_backup_download(admin):
    response = admin.get("/settings/backup")
    assert response.status_code == 200 and response.data[:15] == b"SQLite format 3"


def test_help_is_private_and_readme_is_clean(app, admin):
    page = text(admin.get("/help"))
    assert "--set-password" in page and "diesel_service.db" in page
    post(admin, "/logout", form_url="/")
    assert admin.get("/help").status_code == 302          # без входа справку не видно

    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
    for secret in ("--set-password", "diesel_service.db", "secret_key", "DIESEL_", "8000",
                   "192.168", "попыт", "без логина", "Tailscale", "%"):
        assert secret not in readme, secret


# ---------- Работа на сервере ----------

def test_first_password_only_from_this_computer(tmp_path):
    app = create_app({"DATA_DIR": str(tmp_path), "TESTING": True})
    outsider = app.test_client()
    outsider.environ_base["REMOTE_ADDR"] = "203.0.113.5"
    page = text(outsider.get("/setup"))
    assert "только на компьютере, где запущена программа" in page and 'name="password"' not in page
    assert outsider.post("/setup", data={"password": "hacker-pass", "password2": "hacker-pass"}) \
        .status_code in (400, 403)
    with app.app_context():
        assert not auth.password_is_set()
    local = app.test_client()                     # тот же компьютер — форма доступна
    assert 'name="password"' in text(local.get("/setup"))


def test_security_headers(admin):
    response = admin.get("/login")
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_waitress_trusts_only_local_proxy(tmp_path, monkeypatch):
    """За Caddy адрес посетителя берётся из X-Forwarded-For (только от 127.0.0.1)."""
    import threading
    import urllib.request
    from waitress.server import create_server
    import run

    app = create_app({"DATA_DIR": str(tmp_path), "TESTING": True})

    def fetch_setup(options):
        server = create_server(app, **{**options, "port": 0})
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.effective_port}/setup",
                headers={"X-Forwarded-For": "203.0.113.5"})
            with urllib.request.urlopen(request) as response:
                return response.read().decode()
        finally:
            server.close()

    monkeypatch.setenv("DIESEL_PROXY", "1")
    behind_proxy = fetch_setup(run.server_options("127.0.0.1", 0))
    assert "только на компьютере" in behind_proxy        # посетитель из интернета

    monkeypatch.delenv("DIESEL_PROXY")
    options = run.server_options("127.0.0.1", 0)
    assert "trusted_proxy" not in options                # дома прокси не доверяем
    assert 'name="password"' in fetch_setup(options)


def test_backup_command(tmp_path, monkeypatch, capsys):
    import run
    data = tmp_path / "data"
    monkeypatch.setenv("DIESEL_DATA_DIR", str(data))
    run.backup(tmp_path / "copies", keep=2)                 # базы ещё нет — не падает
    assert "копировать нечего" in capsys.readouterr().out
    create_app({"DATA_DIR": str(data), "TESTING": True})
    names = []
    for i in range(3):
        target = tmp_path / "copies" / f"diesel_service_2020-01-0{i + 1}_00-00-00.db"
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(b"old")
        names.append(target.name)
    run.backup(tmp_path / "copies", keep=2)
    copies = sorted(x.name for x in (tmp_path / "copies").glob("diesel_service_*.db"))
    assert len(copies) == 2 and names[0] not in copies and names[1] not in copies
    newest = tmp_path / "copies" / copies[-1]
    assert newest.read_bytes()[:15] == b"SQLite format 3"


def test_default_name(admin):
    assert "Дизель Сервис" in text(admin.get("/"))


def test_old_database_is_migrated(tmp_path):
    """База прошлой версии: логины сотрудников, сквозные номера заказ-нарядов."""
    conn = sqlite3.connect(tmp_path / "diesel_service.db")
    conn.executescript("""
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO settings VALUES ('company_name', 'Дизельный сервис');
        CREATE TABLE employees (id INTEGER PRIMARY KEY, full_name TEXT NOT NULL, position TEXT,
            phone TEXT, role TEXT NOT NULL DEFAULT 'master', username TEXT UNIQUE,
            password_hash TEXT, is_master INTEGER NOT NULL DEFAULT 1,
            is_active INTEGER NOT NULL DEFAULT 1, notes TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')));
        CREATE TABLE clients (id INTEGER PRIMARY KEY, kind TEXT NOT NULL DEFAULT 'person',
            name TEXT NOT NULL, phone TEXT, phone2 TEXT, email TEXT, inn TEXT, address TEXT,
            notes TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')));
        CREATE TABLE orders (id INTEGER PRIMARY KEY, client_id INTEGER NOT NULL,
            vehicle_id INTEGER, status TEXT NOT NULL DEFAULT 'new', mileage INTEGER,
            complaint TEXT, units TEXT, diagnosis TEXT, recommendations TEXT, notes TEXT,
            responsible_id INTEGER, created_by INTEGER, created_at TEXT NOT NULL,
            planned_date TEXT, completed_at TEXT, issued_at TEXT);
        INSERT INTO clients (name) VALUES ('Старый клиент');
    """)
    conn.execute("INSERT INTO employees (full_name, role, username, password_hash, is_master) "
                 "VALUES ('Руководитель', 'admin', 'admin', ?, 0)",
                 (generate_password_hash("old-password"),))
    for created in ("2025-03-01", "2025-07-15", "2026-01-10", "2026-02-02"):
        conn.execute("INSERT INTO orders (client_id, status, created_at) VALUES (1, 'issued', ?)",
                     (f"{created} 10:00:00",))
    conn.commit()
    conn.close()

    app = create_app({"DATA_DIR": str(tmp_path), "TESTING": True})
    rows = query(app, "SELECT year, number FROM orders ORDER BY id")
    assert [(r["year"], r["number"]) for r in rows] == [(2025, 1), (2025, 2), (2026, 1), (2026, 2)]
    assert scalar(app, "SELECT value FROM settings WHERE key = 'company_name'") == "Дизель Сервис"
    assert scalar(app, "PRAGMA user_version") == db.SCHEMA_VERSION

    client = app.test_client()
    auth._failed_logins.clear()
    assert post(client, "/login", {"password": "old-password"}).status_code == 302
    assert client.get("/masters/").status_code == 200
    assert "Руководитель" not in text(client.get("/masters/"))
    # Повторный запуск ничего не ломает
    create_app({"DATA_DIR": str(tmp_path), "TESTING": True})


def test_all_pages_render(app, admin):
    ctx = create_full_order(app, admin)
    add_old_order(app, ctx["client_id"], ctx["vehicle_id"], YEAR - 1, 1, "issued", 100)
    pages = [
        "/", "/orders/", "/orders/?status=all", "/orders/?payment=debt", "/orders/archive",
        "/orders/archive?year=2020", "/orders/new", "/orders/new?q=Иванов",
        f"/orders/new?client_id={ctx['client_id']}", ctx["view"], f"{ctx['view']}/edit",
        f"{ctx['view']}/print", "/clients/", "/clients/?q=А123&debt=1&sort=recent",
        f"/clients/{ctx['client_id']}", "/clients/new", "/services/", "/services/new",
        "/warehouse/", "/warehouse/list/all", "/warehouse/list/all?stock=idle&sort=markup&dir=desc",
        "/warehouse/list/all?group=0&group=1", "/warehouse/list/none", "/warehouse/list/1",
        "/warehouse/categories",
        "/warehouse/new", "/warehouse/receipt",
        f"/warehouse/{ctx['part_id']}", f"/warehouse/{ctx['part_id']}/writeoff",
        "/warehouse/movements", "/finance/", "/finance/new", "/finance/report",
        "/salary/", f"/salary/{ctx['master_id']}", "/masters/", "/masters/new",
        f"/masters/{ctx['master_id']}/edit", "/settings/", "/password", "/help",
    ]
    for url in pages:
        response = admin.get(url)
        assert response.status_code == 200, url
