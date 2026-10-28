"""Deterministic kirana-billing history and scripted offline demo answers."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEMO_BUG = ("Customers say some bills are ₹1 short. Example: one 'Basmati rice 1kg (loose)' "
            "at ₹42.50 with 0% GST shows a total of ₹42. It should be ₹43, because shops round "
            "50 paise up. Bills like ₹43.50 still look right (₹44).")

DEMO_REPRO_TEST = '''from billing.catalog import Item
from billing.invoice import Invoice


def test_fifty_paise_rounds_up():
    bill = Invoice()
    bill.add(Item("RICE-BAS-1KG", "Basmati rice 1kg (loose)", "42.50", 0), qty=1)
    assert bill.total() == 43, f"got {bill.total()}"
'''

DEMO_EXPLANATION = {
    "headline": "Paise refactor switched to Python round(), which rounds .5 to even",
    "root_cause": "This commit moved money maths to integer paise. round_rupees now calls "
                  "Python's round(paise / 100). Python rounds halves to the nearest even number, "
                  "so 42.5 becomes 42 but 43.5 becomes 44. The old code used Decimal with "
                  "ROUND_HALF_UP, which always rounds 50 paise up.",
    "guilty_change": {"file": "billing/money.py",
                      "before": "return int(to_decimal(amount).quantize(ONE_RUPEE, rounding=ROUND_HALF_UP))",
                      "after": "return round(to_paise(amount) / PAISE_PER_RUPEE)"},
    "why_tests_missed_it": "The tests only used 43.50, which rounds up either way. "
                           "No test had an even rupee amount with exactly 50 paise.",
    "fix_idea": "Keep integer paise, but round half up on purpose: divmod by 100 and add 1 "
                "when the remainder is 50 or more.",
    "confidence": "high",
}

MONEY_BEFORE = '''"""Money helpers for kirana-billing. We use Decimal, never float."""
from decimal import ROUND_HALF_UP, Decimal

TWO_PLACES = Decimal("0.01")
ONE_RUPEE = Decimal("1")


def to_decimal(amount) -> Decimal:
    """'42.5' -> Decimal('42.50'). Accepts str, int or Decimal."""
    return Decimal(str(amount)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def round_rupees(amount) -> int:
    """Round a bill to whole rupees (the 'round off' line). 50 paise and above goes up."""
    return int(to_decimal(amount).quantize(ONE_RUPEE, rounding=ROUND_HALF_UP))
'''

MONEY_AFTER = '''"""Money helpers for kirana-billing. All maths happens in integer paise (1 rupee = 100 paise)."""
from decimal import Decimal

PAISE_PER_RUPEE = 100


def to_paise(amount) -> int:
    """'42.50' -> 4250. Accepts str, int or Decimal."""
    return int(round(Decimal(str(amount)) * PAISE_PER_RUPEE))


def from_paise(paise: int) -> Decimal:
    """4250 -> Decimal('42.50')."""
    return (Decimal(paise) / PAISE_PER_RUPEE).quantize(Decimal("0.01"))


def to_decimal(amount) -> Decimal:
    """'42.5' -> Decimal('42.50'). Accepts str, int or Decimal."""
    return from_paise(to_paise(amount))


def round_rupees(amount) -> int:
    """Round a bill to whole rupees (the 'round off' line)."""
    return round(to_paise(amount) / PAISE_PER_RUPEE)
'''

CATALOG = '''"""Shop items loaded from the stock list."""
import csv
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Item:
    sku: str
    name: str
    price: str
    gst_rate: int


def all_items():
    with (Path(__file__).parent / "data/catalog.csv").open(encoding="utf-8", newline="") as source:
        return [Item(row["sku"], row["name"], row["price"], int(row["gst_rate"]))
                for row in csv.DictReader(source)]


def get_item(sku):
    for item in all_items():
        if item.sku == sku:
            return item
    raise KeyError(sku)
'''

GST = '''"""GST calculations at the supported Indian slabs."""
from billing.money import to_decimal

GST_SLABS = (0, 5, 12, 18, 28)


def line_tax(amount, rate):
    if rate not in GST_SLABS:
        raise ValueError("unsupported GST rate")
    return to_decimal(to_decimal(amount) * rate / 100)
'''

INVOICE = '''"""A grocery bill with line taxes and an optional discount."""
from decimal import Decimal
from billing.gst import line_tax
from billing.money import round_rupees, to_decimal


class Invoice:
    def __init__(self):
        self.lines = []
        self.discount = Decimal("0.00")

    def add(self, item, qty=1):
        if qty <= 0:
            raise ValueError("quantity must be positive")
        self.lines.append((item, qty))

    def subtotal(self):
        return sum((to_decimal(item.price) * qty for item, qty in self.lines), Decimal("0.00"))

    def tax(self):
        return sum((line_tax(to_decimal(item.price) * qty, item.gst_rate)
                    for item, qty in self.lines), Decimal("0.00"))

    def total(self) -> int:
        return round_rupees(self.subtotal() + self.tax() - to_decimal(self.discount))

    def round_off(self):
        return to_decimal(self.total() - (self.subtotal() + self.tax() - to_decimal(self.discount)))
'''

FORMAT = '''"""Display amounts with Indian digit grouping."""
from billing.money import to_decimal


def format_inr(amount):
    value = to_decimal(amount)
    whole, fraction = format(abs(value), ".2f").split(".")
    groups = [whole[-3:]]
    whole = whole[:-3]
    while whole:
        groups.insert(0, whole[-2:])
        whole = whole[:-2]
    sign = "-" if value < 0 else ""
    return sign + "₹" + ",".join(groups) + "." + fraction
'''

RECEIPT = '''"""Render a printable shop receipt."""
from billing.format import format_inr


def render_receipt(invoice, shop="Kirana Store"):
    lines = [shop]
    for item, qty in invoice.lines:
        lines.append(f"{item.name} x {qty}")
    lines.extend([f"Subtotal: {format_inr(invoice.subtotal())}",
                  f"GST: {format_inr(invoice.tax())}",
                  f"Round off: {format_inr(invoice.round_off())}",
                  f"Total: {format_inr(invoice.total())}"])
    return "\\n".join(lines)
'''

DISCOUNTS = '''"""Discount amounts, computed before rounding the final bill."""
from billing.money import to_decimal


def percent_off(amount, percent):
    return to_decimal(to_decimal(amount) * to_decimal(percent) / 100)


def flat_off(amount, discount):
    return min(to_decimal(amount), to_decimal(discount))
'''

EXPORT = '''"""CSV export for the accountant, with exact amounts in paise."""
import csv
import io
from billing.money import to_paise


def export_csv(invoices):
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\\n")
    writer.writerow(["subtotal_paise", "tax_paise", "total_paise"])
    for invoice in invoices:
        writer.writerow([to_paise(invoice.subtotal()), to_paise(invoice.tax()),
                         to_paise(invoice.total())])
    return output.getvalue()
'''

MONEY_TEST = '''from billing.money import round_rupees, to_decimal


def test_money():
    assert str(to_decimal("12.3")) == "12.30"
    assert round_rupees("43.50") == 44
    assert round_rupees("99.49") == 99
'''

EDGE_TEST = '''from billing.money import round_rupees


def test_rounding_edges():
    for amount, expected in [("0.50", 1), ("42.50", 43), ("43.50", 44), ("99.49", 99)]:
        assert round_rupees(amount) == expected
'''

AUTHORS = ("Priya Raman", "Arjun Mehta", "Meera Iyer", "Kabir Shah")
TAGS = {25: "v1.0.0", 42: "v1.1.0", 60: "v1.2.0", 95: "v1.3.0", 135: "v1.4.0", 200: "v1.5.0"}
CULPRIT_SUBJECT = "refactor(money): keep amounts in integer paise"
EXPORT_SUBJECT = "feat(export): CSV export for the accountant"
ITEM_NAMES = (
    "Toor dal 1kg", "Moong dal 1kg", "Masoor dal 1kg", "Urad dal 1kg", "Chana dal 1kg",
    "Rajma 500g", "Kabuli chana 500g", "Kala chana 500g", "Green peas 500g", "Soy chunks 200g",
    "Atta 5kg", "Maida 1kg", "Sooji 500g", "Besan 500g", "Poha 500g",
    "Basmati rice 5kg", "Sona masoori 5kg", "Brown rice 1kg", "Broken rice 1kg", "Idli rice 1kg",
    "Sugar 1kg", "Jaggery 500g", "Salt 1kg", "Rock salt 200g", "Icing sugar 200g",
    "Sunflower oil 1l", "Mustard oil 1l", "Groundnut oil 1l", "Coconut oil 500ml", "Ghee 500g",
    "Turmeric 100g", "Chilli powder 100g", "Coriander powder 100g", "Cumin 100g", "Black pepper 50g",
    "Mustard seeds 100g", "Fenugreek 100g", "Cardamom 25g", "Cloves 25g", "Cinnamon 50g",
    "Tea 250g", "Coffee 100g", "Milk 1l", "Curd 500g", "Paneer 200g",
    "Butter 100g", "Cheese 200g", "Bread 400g", "Rusk 200g", "Biscuits 100g",
    "Oats 500g", "Cornflakes 500g", "Vermicelli 200g", "Pasta 500g", "Noodles 200g",
    "Tomato ketchup 500g", "Soy sauce 200ml", "Vinegar 200ml", "Pickle 400g", "Papad 200g",
    "Almonds 200g", "Cashews 200g", "Raisins 200g", "Peanuts 500g", "Dates 250g",
    "Coconut 1pc", "Potato 1kg", "Onion 1kg", "Tomato 1kg", "Ginger 250g",
    "Garlic 250g", "Lemon 4pc", "Banana 6pc", "Apple 1kg", "Orange 1kg",
    "Soap 100g", "Shampoo 180ml", "Toothpaste 100g", "Detergent 1kg", "Dishwash bar 200g",
    "Floor cleaner 1l", "Tissues 100pc", "Matchbox 10pc", "Candles 6pc", "Incense 100g",
    "Baking soda 100g", "Baking powder 100g", "Cocoa 100g", "Honey 250g", "Jam 200g",
)


def is_demo_repo(path: str | Path) -> bool:
    path = Path(path)
    return (path / ".git").exists() and (path / ".kirana-demo").is_file() and (
        path / ".kirana-demo").read_text(encoding="utf-8").strip() == "kirana-billing demo v1"


def offline_fix(purpose: str, user_prompt: str) -> dict:
    """Return the specified deterministic candidates, preserving current file content."""
    # FIX_USER supplies current source in a fence, including any harmless later edits.
    current = MONEY_AFTER
    import re
    match = re.search(r"--- billing/money\.py ---\n```python\n(.*?)```", user_prompt, re.S)
    if match:
        current = match.group(1).removesuffix("\n")
    if purpose == "fix_minimal":
        content = current.replace(
            "    return round(to_paise(amount) / PAISE_PER_RUPEE)",
            "    rupees, paise = divmod(to_paise(amount), PAISE_PER_RUPEE)\n"
            "    return rupees + (1 if paise >= 50 else 0)",
        )
        return {"files": [{"path": "billing/money.py", "content": content}],
                "summary": "Keep integer paise and round halves up."}
    if purpose == "fix_robust":
        content = current.replace("from decimal import Decimal", "from decimal import ROUND_HALF_UP, Decimal")
        content = content.replace("return int(round(Decimal(str(amount)) * PAISE_PER_RUPEE))",
                                  'return int((Decimal(str(amount)) * PAISE_PER_RUPEE).quantize(Decimal("1"), rounding=ROUND_HALF_UP))')
        content = content.replace("return round(to_paise(amount) / PAISE_PER_RUPEE)",
                                  'return int(Decimal(str(amount)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))')
        return {"files": [{"path": "billing/money.py", "content": content},
                          {"path": "tests/test_rounding_edges.py", "content": EDGE_TEST}],
                "summary": "Use explicit half-up rounding and cover rounding edges."}
    raise ValueError(f"Unknown offline fix purpose: {purpose}")


def _planned_changes(n: int, files: dict[str, str], item_index: int) -> tuple[str, dict[str, str], int]:
    """One meaningful change per planned commit; only #112 edits the money module."""
    milestones = {
        1: ("chore: billing package skeleton", {
            ".kirana-demo": "kirana-billing demo v1\n", "billing/__init__.py": '__version__ = "1.0.0"\n',
            ".gitignore": "__pycache__/\n.pytest_cache/\n*.pyc\n", "requirements.txt": "pytest\n",
            "README.md": "# kirana-billing\n\nGrocery bills with GST and whole-rupee totals.\n",
            "tests/test_package.py": 'from billing import __version__\n\ndef test_version():\n    assert __version__\n',
        }),
        2: ("feat(money): decimal amounts and half-up billing", {"billing/money.py": MONEY_BEFORE, "tests/test_money.py": MONEY_TEST}),
        3: ("feat(catalog): load shop items from CSV", {"billing/catalog.py": CATALOG,
            "billing/data/catalog.csv": "sku,name,price,gst_rate,unit\nRICE-BAS-1KG,Basmati rice 1kg (loose),42.50,0,kg\n",
            "tests/test_catalog.py": 'from billing.catalog import all_items, get_item\n\ndef test_catalog():\n    assert all_items()\n    assert get_item("RICE-BAS-1KG").price == "42.50"\n'}),
        4: ("feat(gst): calculate line taxes", {"billing/gst.py": GST,
            "tests/test_gst.py": 'from billing.gst import line_tax\n\ndef test_tax():\n    assert str(line_tax("100", 5)) == "5.00"\n    assert str(line_tax("40", 12)) == "4.80"\n    assert str(line_tax("25", 18)) == "4.50"\n'}),
        5: ("feat(invoice): stable bill API", {"billing/invoice.py": INVOICE,
            "tests/test_invoice.py": 'from billing.catalog import Item\nfrom billing.invoice import Invoice\n\ndef test_total():\n    bill = Invoice()\n    bill.add(Item("TEA", "Tea", "100", 5))\n    assert bill.total() == 105\n    assert bill.subtotal() == 100\n    assert bill.tax() == 5\n    assert bill.round_off() == 0\n'}),
        26: ("feat(format): Indian digit grouping", {"billing/format.py": FORMAT,
            "tests/test_format.py": 'from billing.format import format_inr\n\ndef test_format():\n    assert format_inr("123456.25") == "₹1,23,456.25"\n'}),
        28: ("feat(receipt): shop receipt and round-off line", {"billing/receipt.py": RECEIPT,
            "tests/test_receipt.py": 'from billing.invoice import Invoice\nfrom billing.receipt import render_receipt\n\ndef test_receipt():\n    text = render_receipt(Invoice(), shop="Corner Shop")\n    assert "Corner Shop" in text\n    assert "Round off:" in text\n'}),
        30: ("docs: explain shop rounding policy", {"docs/rounding.md": "# Rounding\n\nBills round to the nearest rupee; 50 paise and above rounds up, like our billing machine.\n"}),
        43: ("feat(discounts): percentage and flat discounts", {"billing/discounts.py": DISCOUNTS,
            "tests/test_discounts.py": 'from billing.discounts import percent_off, flat_off\n\ndef test_discounts():\n    assert percent_off("100", 5) == 5\n    assert flat_off("40", "10") == 10\n'}),
        50: ("docs: start release changelog", {"CHANGELOG.md": "# Changelog\n\n## 1.2.0\nDiscount helpers and rounding guidance.\n"}),
        65: ("docs: contribution guidelines", {"CONTRIBUTING.md": "# Contributing\n\nRun python -m pytest before sending a change.\n"}),
        80: ("feat(receipt): optional GSTIN and date header", {"billing/receipt.py": RECEIPT.replace(
            'shop="Kirana Store"):', 'shop="Kirana Store", gstin="", date=""):').replace('lines = [shop]', 'lines = [shop]\n    if gstin:\n        lines.append(f"GSTIN: {gstin}")\n    if date:\n        lines.append(f"Date: {date}")')}),
        100: ("docs(gst): document supported slabs", {"billing/gst.py": GST.replace('GST_SLABS =', '# Rates are percentages, not fractions.\nGST_SLABS =')}),
        106: ("docs(format): explain sign placement", {"billing/format.py": FORMAT.replace('    sign =', '    # Keep the minus before the currency symbol.\n    sign =')}),
        112: (CULPRIT_SUBJECT, {"billing/money.py": MONEY_AFTER, "tests/test_money.py": MONEY_TEST + '\n\ndef test_paise():\n    from billing.money import to_paise, from_paise\n    assert to_paise("12.30") == 1230\n    assert str(from_paise(1230)) == "12.30"\n'}),
        118: (EXPORT_SUBJECT, {"billing/export.py": EXPORT,
            "tests/test_export.py": 'from billing.export import export_csv\nfrom billing.invoice import Invoice\nfrom billing.catalog import Item\n\ndef test_export():\n    bill = Invoice()\n    bill.add(Item("TEA", "Tea", "100", 5))\n    assert "10000,500,10500" in export_csv([bill])\n'}),
        125: ("feat(export): number exported invoices", {"billing/export.py": EXPORT.replace(
            '["subtotal_paise",', '["invoice", "subtotal_paise",').replace('for invoice in invoices:', 'for number, invoice in enumerate(invoices, 1):').replace('writer.writerow([to_paise', 'writer.writerow([number, to_paise')}),
        160: ("feat(receipt): thank-you footer", {"billing/receipt.py": files.get("billing/receipt.py", RECEIPT).replace(
            '    return "\\n".join(lines)', '    lines.append("Thank you, visit again!")\n    return "\\n".join(lines)')}),
    }
    if n in milestones:
        subject, changes = milestones[n]
        return subject, changes, item_index
    if n in TAGS:
        version = TAGS[n][1:]
        return f"chore: release {version}", {
            "billing/__init__.py": f'__version__ = "{version}"\n',
            "README.md": files["README.md"] + f"\nRelease {version} is ready for shop use.\n",
        }, item_index
    if (n % 2 == 0 or n % 7 == 0) and item_index < len(ITEM_NAMES):
        name = ITEM_NAMES[item_index]
        row = f"ITEM-{item_index + 1:03d},{name},{20 + item_index}.00,0,pack\n"
        return f"catalog: add {name}", {"billing/data/catalog.csv": files["billing/data/catalog.csv"] + row}, item_index + 1
    if n % 5 == 0 and n > 30:
        return "test(invoice): cover repeated item quantities", {"tests/test_quantities.py": (
            'from billing.catalog import Item\nfrom billing.invoice import Invoice\n\n'
            f'def test_quantity_{n}():\n    bill = Invoice()\n    bill.add(Item("SALT", "Salt", "20", 0), qty={n})\n    assert bill.total() == {n * 20}\n')}, item_index
    return "docs: add shop usage example", {"README.md": files["README.md"] + f"\nExample {n}: keep a receipt for each sale.\n"}, item_index


def build_demo_repo(path: str | Path = "demo/kirana-billing", force: bool = False) -> Path:
    """Create the fixed history, refusing to overwrite unrelated directories."""
    path = Path(path).expanduser().resolve()
    if path.exists() and any(path.iterdir()):
        if not force or not is_demo_repo(path):
            raise ValueError("Destination exists; --force can rebuild only a marked demo repository.")
        def remove_readonly(function, filename, exc_info):
            os.chmod(filename, stat.S_IWRITE)
            function(filename)

        shutil.rmtree(path, onerror=remove_readonly)
    path.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)

    def git(*args: str) -> str:
        result = subprocess.run(["git", "-C", str(path), *args], env=env, capture_output=True)
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).decode("utf-8", errors="replace").strip())
        return result.stdout.decode("utf-8").strip()

    git("init", "-q", "-b", "main", "--object-format=sha1", "--template=")
    for key, value in {"core.autocrlf": "false", "core.filemode": "false", "commit.gpgsign": "false"}.items():
        git("config", key, value)
    files: dict[str, str] = {}
    item_index = 0
    start = datetime(2026, 4, 1, 10, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    for n in range(1, 201):
        subject, changes, item_index = _planned_changes(n, files, item_index)
        if n in (112, 118):
            assert int(git("rev-list", "--count", "HEAD")) + 1 == n
        files.update(changes)
        for rel, content in changes.items():
            destination = path / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content.encode("utf-8"))
        author = AUTHORS[1 if n == 112 else 3 if n == 118 else (n - 1) % len(AUTHORS)]
        date = (start + timedelta(hours=20 * (n - 1))).strftime("%Y-%m-%dT%H:%M:%S%z")
        for role in ("AUTHOR", "COMMITTER"):
            env[f"GIT_{role}_NAME"] = author
            env[f"GIT_{role}_EMAIL"] = author.lower().replace(" ", ".") + "@kirana.example"
            env[f"GIT_{role}_DATE"] = date
        git("add", "-A")
        git("commit", "-q", "-m", subject)
        if n in TAGS:
            git("tag", TAGS[n])
    (path.parent / "BUG.txt").write_bytes((DEMO_BUG + "\n").encode("utf-8"))
    return path
