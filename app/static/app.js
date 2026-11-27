(function () {
  "use strict";

  function $all(selector, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(selector));
  }

  // Мобильное меню
  $all("[data-nav-toggle]").forEach(function (btn) {
    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      document.body.classList.toggle("nav-open");
    });
  });
  document.addEventListener("click", function (e) {
    if (document.body.classList.contains("nav-open") && !e.target.closest(".sidebar")) {
      document.body.classList.remove("nav-open");
    }
  });

  // Подтверждение опасных действий
  document.addEventListener("submit", function (e) {
    var form = e.target;
    var message = form.getAttribute("data-confirm");
    if (message && !window.confirm(message)) {
      e.preventDefault();
    }
  });

  // Кликабельные строки таблиц
  $all("tr[data-href]").forEach(function (row) {
    row.classList.add("row-link");
    row.addEventListener("click", function (e) {
      if (e.target.closest("a, button, input, select, form")) return;
      window.location = row.getAttribute("data-href");
    });
  });

  // Поиск по длинному выпадающему списку
  $all("[data-filter-for]").forEach(function (input) {
    var select = document.getElementById(input.getAttribute("data-filter-for"));
    if (!select) return;
    var options = $all("option", select);
    input.addEventListener("input", function () {
      var q = input.value.trim().toLowerCase();
      var firstVisible = null;
      options.forEach(function (opt) {
        if (!opt.value) return;
        var visible = !q || opt.textContent.toLowerCase().indexOf(q) !== -1;
        opt.hidden = !visible;
        if (visible && !opt.disabled && !firstVisible) firstVisible = opt;
      });
      $all("optgroup", select).forEach(function (group) {
        group.hidden = !$all("option", group).some(function (o) { return !o.hidden; });
      });
      var current = select.options[select.selectedIndex];
      if (q && firstVisible && (!current || !current.value || current.hidden)) {
        select.value = firstVisible.value;
        select.dispatchEvent(new Event("change"));
      }
    });
  });

  // Подстановка цены и процента при выборе услуги / запчасти
  $all("select[data-fill]").forEach(function (select) {
    var form = select.form;
    select.addEventListener("change", function () {
      var opt = select.options[select.selectedIndex];
      if (!opt) return;
      ["price", "master_percent", "name", "unit"].forEach(function (key) {
        var value = opt.getAttribute("data-" + key.replace("_", "-"));
        var field = form.querySelector("[name=" + key + "]");
        if (field && value !== null) {
          if (field.tagName === "INPUT") field.value = value; else field.textContent = value;
        }
      });
      var stock = form.querySelector("[data-stock-info]");
      if (stock) {
        var s = opt.getAttribute("data-stock");
        stock.textContent = s !== null ? "На складе: " + s : "";
      }
    });
  });

  // Редактирование строк заказ-наряда
  $all("[data-edit-line]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      btn.closest("tr").classList.add("editing");
    });
  });
  $all("[data-cancel-edit]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      btn.closest("tr").classList.remove("editing");
    });
  });

  // Новая техника в форме заказ-наряда
  $all("select[data-vehicle-select]").forEach(function (select) {
    var block = document.getElementById(select.getAttribute("data-vehicle-select"));
    function sync() { if (block) block.hidden = select.value !== "new"; }
    select.addEventListener("change", sync);
    sync();
  });

  // Тип записи в финансах: подставляем подходящие статьи
  $all("[data-kind-switch]").forEach(function (wrap) {
    var lists = { income: wrap.getAttribute("data-income"), expense: wrap.getAttribute("data-expense") };
    var datalist = document.getElementById(wrap.getAttribute("data-kind-switch"));
    $all("input[name=kind]", wrap).forEach(function (radio) {
      radio.addEventListener("change", function () {
        if (!datalist || !lists[radio.value]) return;
        datalist.innerHTML = "";
        JSON.parse(lists[radio.value]).forEach(function (c) {
          var o = document.createElement("option");
          o.value = c;
          datalist.appendChild(o);
        });
      });
    });
  });

  // Приход: добавление и удаление строк
  var receiptBody = document.getElementById("receipt-rows");
  if (receiptBody) {
    var template = document.getElementById("receipt-row-template");
    var counter = 0;

    var updatePrices = document.getElementById("update-prices");

    function toNum(v) {
      v = parseFloat(String(v || "").replace(/\s/g, "").replace(",", "."));
      return isNaN(v) ? null : v;
    }
    function rub(v) { return v.toLocaleString("ru-RU", { maximumFractionDigits: 2 }) + " ₽"; }
    // Как на сервере: от 100 ₽ — вверх до 50 ₽, дешевле — без округления.
    function roundSale(v) {
      var raw = Math.round(v * 100) / 100;
      return raw <= 0 ? 0 : (raw < 100 ? raw : Math.ceil(raw / 50) * 50);
    }

    // Цена продажи в строке: текущая или «старая → новая», если закупка подорожала.
    function showSale(row) {
      var select = row.querySelector("select");
      var opt = select.options[select.selectedIndex];
      var cell = row.querySelector("[data-sale-preview]");
      cell.classList.remove("price-up");
      if (!opt || !opt.value) { cell.textContent = ""; return; }
      var markup = toNum(opt.getAttribute("data-markup"));
      var purchase = toNum(opt.getAttribute("data-price")) || 0;
      var sale = toNum(opt.getAttribute("data-sale")) || 0;
      var price = toNum(row.querySelector("[name=price]").value) || 0;
      var basis = markup !== null ? " · +" + opt.getAttribute("data-markup") + "%" : " · вручную";
      if (updatePrices && updatePrices.checked && price > purchase + 0.009) {
        var next = markup !== null ? roundSale(price * (1 + markup / 100))
          : (purchase > 0 && sale > 0 ? roundSale(price * sale / purchase) : sale);
        if (next > sale + 0.009) {
          cell.textContent = rub(sale) + " → " + rub(next) + basis;
          cell.classList.add("price-up");
          return;
        }
      }
      cell.textContent = rub(sale) + basis;
    }

    function recalc() {
      var total = 0;
      $all("tr", receiptBody).forEach(function (row) {
        var q = parseFloat((row.querySelector("[name=quantity]").value || "0").replace(",", "."));
        var p = parseFloat((row.querySelector("[name=price]").value || "0").replace(",", "."));
        var sum = (isNaN(q) || isNaN(p)) ? 0 : q * p;
        row.querySelector("[data-sum]").textContent = sum ? sum.toLocaleString("ru-RU", { maximumFractionDigits: 2 }) : "";
        total += sum;
        showSale(row);
      });
      document.getElementById("receipt-total").textContent =
        total.toLocaleString("ru-RU", { maximumFractionDigits: 2 }) + " ₽";
    }

    function addRow(partId) {
      counter += 1;
      var row = template.content.firstElementChild.cloneNode(true);
      var select = row.querySelector("select");
      var filter = row.querySelector("[data-row-filter]");
      select.id = "receipt-part-" + counter;
      var options = $all("option", select);
      filter.addEventListener("input", function () {
        var q = filter.value.trim().toLowerCase();
        options.forEach(function (opt) {
          if (!opt.value) return;
          opt.hidden = q && opt.textContent.toLowerCase().indexOf(q) === -1;
        });
      });
      select.addEventListener("change", function () {
        var opt = select.options[select.selectedIndex];
        row.querySelector("[name=price]").value = opt.getAttribute("data-price") || "";
        row.querySelector("[data-unit]").textContent = opt.getAttribute("data-unit") || "";
        recalc();
      });
      row.querySelector("[data-remove]").addEventListener("click", function () {
        row.remove();
        if (!receiptBody.children.length) addRow();
        recalc();
      });
      $all("input", row).forEach(function (i) { i.addEventListener("input", recalc); });
      receiptBody.appendChild(row);
      if (partId) {
        select.value = partId;
        select.dispatchEvent(new Event("change"));
      }
    }

    if (updatePrices) updatePrices.addEventListener("change", recalc);
    document.getElementById("receipt-add").addEventListener("click", function () { addRow(); });
    addRow(receiptBody.getAttribute("data-preselect"));
  }
})();
