"""Проверка основных сценариев работы программы."""

import re

import pytest

from app import create_app, db


@pytest.fixture()
def app(tmp_path):
    return create_app({"DATA_DIR": str(tmp_path), "TESTING": True})


@pytest.fixture()
def client(app):
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


def setup_admin(client):
    response = post(client, "/setup", {
        "company_name": "Тест-Дизель", "full_name": "Петров Пётр", "username": "admin",
        "password": "secret123", "password2": "secret123",
    })
    assert response.status_code == 302
    return response


def query(app, sql, params=()):
    with app.app_context():
        return db.query(sql, params)


def scalar(app, sql, params=()):
    with app.app_context():
        return db.scalar(sql, params)


@pytest.fixture()
def admin(client):
    setup_admin(client)
    return client


def test_first_run_redirects_to_setup(client):
    response = client.get("/")
    assert response.status_code == 302
    assert "/setup" in response.headers["Location"]


def test_setup_and_login(client):
    setup_admin(client)
    assert client.get("/").status_code == 200
    post(client, "/logout", form_url="/")
    assert client.get("/").status_code == 302

    bad = post(client, "/login", {"username": "admin", "password": "wrong"})
    assert "Неверный логин или пароль" in bad.get_data(as_text=True)
    good = post(client, "/login", {"username": "admin", "password": "secret123"})
    assert good.status_code == 302
    assert client.get("/orders/").status_code == 200


def test_post_without_csrf_rejected(admin):
    assert admin.post("/clients/new", data={"name": "Без токена"}).status_code == 400


def test_open_redirect_blocked(client):
    setup_admin(client)
    post(client, "/logout", form_url="/")
    response = post(client, "/login?next=//evil.example.com", {"username": "admin",
                                                               "password": "secret123"})
    assert "evil" not in response.headers["Location"]


def test_login_lockout(client):
    setup_admin(client)
    post(client, "/logout", form_url="/")
    for _ in range(5):
        post(client, "/login", {"username": "admin", "password": "nope"})
    locked = post(client, "/login", {"username": "admin", "password": "secret123"})
    assert "Слишком много" in locked.get_data(as_text=True)


def create_full_order(app, client):
    """Клиент → услуга → запчасть с приходом → заказ-наряд с работой и запчастью."""
    post(client, "/employees/new", {
        "full_name": "Сидоров Иван", "role": "master", "username": "master1",
        "password": "master123", "is_master": "1", "is_active": "1",
    })
    master_id = scalar(app, "SELECT id FROM employees WHERE username = 'master1'")

    post(client, "/clients/new", {
        "kind": "person", "name": "Иванов Сергей", "phone": "+7 900 000-00-00",
        "v_make": "КАМАЗ", "v_model": "5490", "v_plate": "а123вс77", "v_vin": "xtc123",
    })
    client_id = scalar(app, "SELECT id FROM clients WHERE name = 'Иванов Сергей'")
    vehicle = query(app, "SELECT * FROM vehicles WHERE client_id = ?", (client_id,))[0]
    assert vehicle["plate"] == "А123ВС77"

    post(client, "/services/new", {"name": "Ремонт форсунки CR", "category": "Форсунки",
                                    "price": "4 500", "master_percent": "40", "is_active": "1"})
    service_id = scalar(app, "SELECT id FROM services")

    post(client, "/warehouse/new", {"name": "Распылитель DLLA", "sku": "DLLA150", "unit": "шт",
                                     "purchase_price": "1000", "sale_price": "1800",
                                     "min_quantity": "2", "is_active": "1",
                                     "initial_quantity": "3"})
    part_id = scalar(app, "SELECT id FROM parts")
    post(client, "/warehouse/receipt", {
        "supplier": "ООО Поставщик", "document": "Накл. 15", "date": "2026-11-20",
        "part_id": str(part_id), "quantity": "5", "price": "1100", "sale_price": "1900",
        "record_expense": "1", "update_prices": "1", "method": "bank",
    })
    assert scalar(app, "SELECT quantity FROM parts WHERE id = ?", (part_id,)) == 8

    response = post(client, f"/orders/new?client_id={client_id}", {
        "client_id": str(client_id), "vehicle_id": str(vehicle["id"]), "mileage": "350000",
        "complaint": "Дымит", "units": "Форсунки 4 шт.", "responsible_id": str(master_id),
    })
    order_id = int(re.search(r"/orders/(\d+)", response.headers["Location"]).group(1))

    view = f"/orders/{order_id}"
    post(client, f"{view}/services/add", {"service_id": str(service_id), "quantity": "4",
                                          "master_id": str(master_id)}, form_url=view)
    post(client, f"{view}/parts/add", {"part_id": str(part_id), "quantity": "4"}, form_url=view)
    return {"order_id": order_id, "part_id": part_id, "master_id": master_id,
            "client_id": client_id, "view": view}


def test_full_order_flow(app, admin):
    ctx = create_full_order(app, admin)
    order_id, part_id, view = ctx["order_id"], ctx["part_id"], ctx["view"]

    # Цена услуги и процент мастеру подставились из прайса, запчасть списана со склада
    line = query(app, "SELECT * FROM order_services WHERE order_id = ?", (order_id,))[0]
    assert (line["price"], line["master_percent"], line["quantity"]) == (4500, 40, 4)
    part_line = query(app, "SELECT * FROM order_parts WHERE order_id = ?", (order_id,))[0]
    assert part_line["price"] == 1900   # новая цена продажи из прихода
    assert scalar(app, "SELECT quantity FROM parts WHERE id = ?", (part_id,)) == 4

    # Нельзя списать больше, чем есть
    response = post(admin, f"{view}/parts/add", {"part_id": str(part_id), "quantity": "10"},
                    form_url=view)
    assert scalar(app, "SELECT quantity FROM parts WHERE id = ?", (part_id,)) == 4
    assert scalar(app, "SELECT COUNT(*) FROM order_parts WHERE order_id = ?", (order_id,)) == 1
    assert response.status_code == 302

    html = admin.get(view).get_data(as_text=True)
    assert "25 600 ₽" in html   # 4×4500 + 4×1900

    # Приход записан в расходы
    assert scalar(app, "SELECT amount FROM transactions WHERE category = 'Закупка запчастей'") \
        == 5500

    # Готов → зарплата начислена; оплата → доход; выдан
    post(admin, f"{view}/status", {"status": "done"}, form_url=view)
    post(admin, f"{view}/payments/add", {"amount": "25600", "method": "card",
                                         "date": "2026-11-25"}, form_url=view)
    post(admin, f"{view}/status", {"status": "issued"}, form_url=view)
    order = query(app, "SELECT * FROM orders WHERE id = ?", (order_id,))[0]
    assert order["status"] == "issued" and order["completed_at"] and order["issued_at"]
    assert scalar(app, "SELECT SUM(amount) FROM transactions WHERE order_id = ?", (order_id,)) \
        == 25600

    salary = admin.get("/salary/").get_data(as_text=True)
    assert "Сидоров Иван" in salary
    assert "7 200 ₽" in salary   # 18 000 × 40 %

    # Выплата уменьшает остаток
    post(admin, f"/salary/{ctx['master_id']}/pay", {"amount": "5000", "method": "cash"},
         form_url=f"/salary/{ctx['master_id']}")
    detail = admin.get(f"/salary/{ctx['master_id']}").get_data(as_text=True)
    assert "2 200 ₽" in detail

    # В выданном заказ-наряде менять состав нельзя
    post(admin, f"{view}/parts/{part_line['id']}/delete", form_url=view)
    assert scalar(app, "SELECT COUNT(*) FROM order_parts WHERE order_id = ?", (order_id,)) == 1

    # Печатная форма и финансы
    printed = admin.get(f"{view}/print").get_data(as_text=True)
    assert "ЗАКАЗ-НАРЯД №" in printed and "Ремонт форсунки CR" in printed
    finance = admin.get("/finance/?start=2026-11-01&end=2026-11-30").get_data(as_text=True)
    assert "Оплата заказ-наряда" in finance


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
    post(admin, f"{ctx['view']}/parts/{line_id}/update", {"quantity": "2", "price": "1900"},
         form_url=ctx["view"])
    assert scalar(app, "SELECT quantity FROM parts WHERE id = ?", (ctx["part_id"],)) == 6
    post(admin, f"{ctx['view']}/parts/{line_id}/delete", form_url=ctx["view"])
    assert scalar(app, "SELECT quantity FROM parts WHERE id = ?", (ctx["part_id"],)) == 8


def test_master_permissions(app, admin):
    ctx = create_full_order(app, admin)
    post(admin, "/logout", form_url="/")
    post(admin, "/login", {"username": "master1", "password": "master123"})
    master = admin

    assert master.get("/").status_code == 200
    assert master.get(ctx["view"]).status_code == 200
    for url in ("/finance/", "/salary/", "/employees/", "/settings/", "/clients/",
                f"/salary/{ctx['master_id'] - 1}"):
        assert master.get(url).status_code == 403, url
    assert master.get(f"/salary/{ctx['master_id']}").status_code == 200

    # Мастер меняет статус, но не может выдать
    post(master, f"{ctx['view']}/status", {"status": "in_progress"}, form_url=ctx["view"])
    assert scalar(app, "SELECT status FROM orders WHERE id = ?", (ctx["order_id"],)) \
        == "in_progress"
    post(master, f"{ctx['view']}/status", {"status": "issued"}, form_url=ctx["view"])
    assert scalar(app, "SELECT status FROM orders WHERE id = ?", (ctx["order_id"],)) \
        == "in_progress"
    response = post(master, f"{ctx['view']}/services/add", {"name": "Левая работа",
                                                            "price": "100000"},
                    form_url=ctx["view"])
    assert response.status_code == 403


def test_last_admin_cannot_be_demoted(app, admin):
    admin_id = scalar(app, "SELECT id FROM employees WHERE username = 'admin'")
    post(admin, f"/employees/{admin_id}/edit", {
        "full_name": "Петров Пётр", "role": "manager", "username": "admin", "is_active": "1",
    })
    assert scalar(app, "SELECT role FROM employees WHERE id = ?", (admin_id,)) == "admin"


def test_backup_download(admin):
    response = admin.get("/settings/backup")
    assert response.status_code == 200
    assert response.data[:15] == b"SQLite format 3"


def test_all_pages_render(app, admin):
    ctx = create_full_order(app, admin)
    pages = [
        "/", "/orders/", "/orders/?status=all", "/orders/?status=debt", "/orders/new",
        "/orders/new?q=Иванов", f"/orders/new?client_id={ctx['client_id']}", ctx["view"],
        f"{ctx['view']}/edit", f"{ctx['view']}/print", "/clients/", "/clients/?q=А123",
        f"/clients/{ctx['client_id']}", "/clients/new", "/services/", "/services/new",
        "/warehouse/", "/warehouse/?show=low", "/warehouse/new", "/warehouse/receipt",
        f"/warehouse/{ctx['part_id']}", f"/warehouse/{ctx['part_id']}/writeoff",
        "/warehouse/movements", "/finance/", "/finance/new", "/finance/report",
        "/salary/", f"/salary/{ctx['master_id']}", "/employees/", "/employees/new",
        "/settings/", "/profile/password",
    ]
    for url in pages:
        response = admin.get(url)
        assert response.status_code == 200, url
