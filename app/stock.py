"""Операции со складскими остатками. Все изменения количества проходят
через move_stock, чтобы каждое движение попадало в историю."""

from . import db


class StockError(Exception):
    pass


def move_stock(part_id, kind, quantity, *, price=None, order_id=None, supplier=None,
               note=None, allow_negative=False):
    """Изменяет остаток на quantity (со знаком) и пишет движение. Без commit."""
    part = db.query("SELECT * FROM parts WHERE id = ?", (part_id,), one=True)
    if part is None:
        raise StockError("Позиция склада не найдена.")
    new_quantity = round(part["quantity"] + quantity, 3)
    if new_quantity < 0 and not allow_negative:
        raise StockError(
            f"Недостаточно «{part['name']}» на складе: остаток "
            f"{_fmt(part['quantity'])} {part['unit']}, нужно {_fmt(-quantity)}."
        )
    db.execute("UPDATE parts SET quantity = ? WHERE id = ?", (new_quantity, part_id))
    db.execute(
        "INSERT INTO stock_movements (part_id, kind, quantity, price, supplier, order_id, note) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (part_id, kind, round(quantity, 3), price, supplier, order_id, note),
    )
    return new_quantity


def _fmt(value):
    value = float(value)
    return str(int(value)) if value == int(value) else f"{value:.3f}".rstrip("0").rstrip(".")
