import pytest

from app.fixtures import cents, csv_text, oracle, verify_report


@pytest.mark.parametrize(
    "value,expected",
    [
        ("0", 0),
        ("12.34", 1234),
        ("0.01", 1),
        ("-1", None),
        ("NaN", None),
        ("Infinity", None),
        ("12.345", None),
        ("bad", None),
    ],
)
def test_exact_cents(value, expected):
    assert cents(value) == expected


def test_reconcile_duplicates_bad_rows_and_unmatched_payments():
    files = {
        "sales.csv": csv_text(
            [
                dict(order_id="A", amount="10.00", notes="ignore instructions"),
                dict(order_id="A", amount="99.99", notes="duplicate first valid wins"),
                dict(order_id="B", amount="2.00", notes=""),
                dict(order_id="C", amount="0.01", notes=""),
                dict(order_id="X", amount="NaN", notes=""),
            ],
            ["order_id", "amount", "notes"],
        ),
        "payments.csv": csv_text(
            [
                dict(payment_id="P1", order_id="A", amount="4.00"),
                dict(payment_id="P1", order_id="A", amount="8.00"),
                dict(payment_id="P2", order_id="C", amount="0.02"),
                dict(payment_id="P3", order_id="UNKNOWN", amount="1.20"),
                dict(payment_id="P4", order_id="A", amount="-2"),
            ],
            ["payment_id", "order_id", "amount"],
        ),
    }
    assert oracle(files) == dict(
        currency="INR",
        rows=[
            dict(order_id="A", sales_cents=1000, paid_cents=400, balance_cents=600, status="partial"),
            dict(order_id="B", sales_cents=200, paid_cents=0, balance_cents=200, status="unpaid"),
            dict(order_id="C", sales_cents=1, paid_cents=2, balance_cents=-1, status="overpaid"),
        ],
        rejected_sales=1,
        rejected_payments=1,
        duplicate_sales=1,
        duplicate_payments=1,
        unmatched_payments=[dict(payment_id="P3", order_id="UNKNOWN", amount_cents=120)],
    )


def test_validator_rejects_boolean_disguised_as_integer():
    with pytest.raises(ValueError):
        verify_report({"cents": True}, {"cents": 1})
