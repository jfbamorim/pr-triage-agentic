import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from pr_triage.authz import Action, HumanApproval
from pr_triage.domain.category import Category
from pr_triage.erp.faults import (
    ErpConflict,
    ErpRejected,
    ErpResponseLost,
    ErpUnavailable,
    Faults,
)
from pr_triage.erp.models import (
    ActionRecord,
    Budget,
    CatalogItem,
    CostCenter,
    EmailRecord,
    FrameworkContract,
    Person,
    PrRecord,
    PrState,
    Quote,
    Supplier,
    WriteResult,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
    email TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL,
    default_cost_center TEXT
);
CREATE TABLE IF NOT EXISTS cost_centers (
    code TEXT PRIMARY KEY, name TEXT NOT NULL, department TEXT NOT NULL,
    owner_email TEXT NOT NULL, active INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS budgets (
    cost_center TEXT NOT NULL, year INTEGER NOT NULL,
    total_eur REAL NOT NULL, committed_eur REAL NOT NULL,
    PRIMARY KEY (cost_center, year)
);
CREATE TABLE IF NOT EXISTS suppliers (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, approved INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS framework_contracts (
    id TEXT PRIMARY KEY, supplier_id TEXT NOT NULL, category TEXT NOT NULL,
    valid_from TEXT NOT NULL, valid_to TEXT NOT NULL,
    max_value_eur REAL NOT NULL, consumed_eur REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS catalog_items (
    sku TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL,
    supplier_id TEXT NOT NULL, unit_price_eur REAL NOT NULL, available INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS purchase_requests (
    pr_id INTEGER PRIMARY KEY AUTOINCREMENT, requester_email TEXT NOT NULL,
    item TEXT NOT NULL, quantity INTEGER NOT NULL, value_eur REAL NOT NULL,
    cost_center TEXT NOT NULL, category TEXT NOT NULL, urgent INTEGER NOT NULL,
    state TEXT NOT NULL, decided_by TEXT, decision_reason TEXT,
    cited_clauses TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL, decided_at TEXT
);
CREATE TABLE IF NOT EXISTS quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, pr_id INTEGER NOT NULL,
    supplier_id TEXT NOT NULL, amount_eur REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS human_approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, pr_id INTEGER NOT NULL,
    approver TEXT NOT NULL, decision TEXT NOT NULL, approved_value_eur REAL NOT NULL,
    granted_at TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT, to_addr TEXT NOT NULL, subject TEXT NOT NULL,
    body TEXT NOT NULL, kind TEXT NOT NULL, pr_id INTEGER,
    cited_clauses TEXT NOT NULL DEFAULT '[]', sent_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, idempotency_key TEXT NOT NULL UNIQUE,
    action TEXT NOT NULL, pr_id INTEGER, actor TEXT NOT NULL,
    result_json TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ErpStore:
    def __init__(
        self,
        path: str | Path,
        clock: Callable[[], datetime] = _utc_now,
        faults: Faults | None = None,
    ):
        self.path = str(path)
        self.clock = clock
        self.faults = faults or Faults()
        self._calls: dict[str, int] = {}
        with closing(self.connect()) as conn, conn:
            conn.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _enter(self, tool: str) -> int:
        number = self._calls.get(tool, 0) + 1
        self._calls[tool] = number
        if tool in self.faults.unavailable or number in self.faults.fail_calls.get(tool, []):
            raise ErpUnavailable(f"{tool} is unavailable")
        return number

    def _after_write(self, tool: str, number: int) -> None:
        if number in self.faults.drop_response.get(tool, []):
            raise ErpResponseLost(f"{tool} was applied but the response was lost")

    def _now(self) -> str:
        return self.clock().isoformat()

    def _write(
        self, tool: str, key: str, pr_id: int | None, actor: str, apply: Callable
    ) -> WriteResult:
        number = self._enter(tool)
        with closing(self.connect()) as conn, conn:
            prior = conn.execute(
                "SELECT * FROM actions WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if prior is not None:
                if prior["action"] != tool or prior["pr_id"] != pr_id:
                    raise ErpConflict("idempotency key already used for another action")
                outcome = WriteResult(
                    action_id=prior["id"],
                    replayed=True,
                    result=json.loads(prior["result_json"]),
                )
            else:
                result = apply(conn)
                cursor = conn.execute(
                    "INSERT INTO actions (idempotency_key, action, pr_id, actor, result_json, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (key, tool, pr_id, actor, json.dumps(result), self._now()),
                )
                outcome = WriteResult(action_id=cursor.lastrowid, replayed=False, result=result)
        self._after_write(tool, number)
        return outcome

    @staticmethod
    def _pr_from_row(row: sqlite3.Row) -> PrRecord:
        return PrRecord(
            pr_id=row["pr_id"],
            requester_email=row["requester_email"],
            item=row["item"],
            quantity=row["quantity"],
            value_eur=row["value_eur"],
            cost_center=row["cost_center"],
            category=Category(row["category"]),
            urgent=bool(row["urgent"]),
            state=PrState(row["state"]),
            decided_by=row["decided_by"],
            decision_reason=row["decision_reason"],
            cited_clauses=json.loads(row["cited_clauses"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            decided_at=datetime.fromisoformat(row["decided_at"]) if row["decided_at"] else None,
        )

    def _load_pr(self, conn: sqlite3.Connection, pr_id: int) -> PrRecord:
        row = conn.execute(
            "SELECT * FROM purchase_requests WHERE pr_id = ?", (pr_id,)
        ).fetchone()
        if row is None:
            raise ErpRejected("unknown_pr", f"Purchase request {pr_id} does not exist.")
        return self._pr_from_row(row)

    def _budget(self, conn: sqlite3.Connection, cost_center: str) -> Budget | None:
        row = conn.execute(
            "SELECT * FROM budgets WHERE cost_center = ? AND year = ?",
            (cost_center, self.clock().year),
        ).fetchone()
        return Budget(**dict(row)) if row else None

    def get_person(self, email: str) -> Person | None:
        self._enter("get_person")
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT * FROM people WHERE email = ?", (email,)).fetchone()
        return Person(**dict(row)) if row else None

    def find_people_by_role(self, role: str) -> list[Person]:
        self._enter("find_people_by_role")
        with closing(self.connect()) as conn:
            rows = conn.execute("SELECT * FROM people WHERE role = ?", (role,)).fetchall()
        return [Person(**dict(row)) for row in rows]

    def get_cost_center(self, code: str) -> CostCenter | None:
        self._enter("get_cost_center")
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT * FROM cost_centers WHERE code = ?", (code,)).fetchone()
        return CostCenter(**{**dict(row), "active": bool(row["active"])}) if row else None

    def get_budget(self, cost_center: str) -> Budget | None:
        self._enter("get_budget")
        with closing(self.connect()) as conn:
            return self._budget(conn, cost_center)

    def get_supplier(self, supplier_id: str) -> Supplier | None:
        self._enter("get_supplier")
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT * FROM suppliers WHERE id = ?", (supplier_id,)).fetchone()
        return Supplier(**{**dict(row), "approved": bool(row["approved"])}) if row else None

    def find_framework_contracts(self, category: Category) -> list[FrameworkContract]:
        self._enter("find_framework_contracts")
        today = self.clock().date().isoformat()
        with closing(self.connect()) as conn:
            rows = conn.execute(
                "SELECT c.*, s.name AS supplier_name FROM framework_contracts c"
                " JOIN suppliers s ON s.id = c.supplier_id WHERE c.category = ?"
                " ORDER BY c.valid_to DESC",
                (category.value,),
            ).fetchall()
        return [
            FrameworkContract(
                **{k: row[k] for k in row.keys()},
                in_force=row["valid_from"] <= today <= row["valid_to"],
            )
            for row in rows
        ]

    def search_catalog(
        self, query: str | None = None, category: Category | None = None
    ) -> list[CatalogItem]:
        self._enter("search_catalog")
        sql = "SELECT * FROM catalog_items WHERE 1 = 1"
        params: list = []
        if query:
            sql += " AND LOWER(name) LIKE ?"
            params.append(f"%{query.lower()}%")
        if category:
            sql += " AND category = ?"
            params.append(category.value)
        with closing(self.connect()) as conn:
            rows = conn.execute(sql + " ORDER BY sku", params).fetchall()
        return [CatalogItem(**{**dict(r), "available": bool(r["available"])}) for r in rows]

    def get_pr(self, pr_id: int) -> PrRecord | None:
        self._enter("get_pr")
        with closing(self.connect()) as conn:
            try:
                return self._load_pr(conn, pr_id)
            except ErpRejected:
                return None

    def get_quotes(self, pr_id: int) -> list[Quote]:
        self._enter("get_quotes")
        with closing(self.connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM quotes WHERE pr_id = ? ORDER BY id", (pr_id,)
            ).fetchall()
        return [Quote(**dict(row)) for row in rows]

    def get_human_approval(self, pr_id: int) -> HumanApproval | None:
        self._enter("get_human_approval")
        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT * FROM human_approvals WHERE pr_id = ? ORDER BY id DESC LIMIT 1",
                (pr_id,),
            ).fetchone()
        if row is None:
            return None
        return HumanApproval(
            request_id=row["pr_id"],
            approver=row["approver"],
            decision=Action(row["decision"]),
            approved_value_eur=row["approved_value_eur"],
            granted_at=datetime.fromisoformat(row["granted_at"]),
            expires_at=datetime.fromisoformat(row["expires_at"]),
        )

    def get_outbox(self, pr_id: int | None = None) -> list[EmailRecord]:
        self._enter("get_outbox")
        with closing(self.connect()) as conn:
            if pr_id is None:
                rows = conn.execute("SELECT * FROM outbox ORDER BY id").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM outbox WHERE pr_id = ? ORDER BY id", (pr_id,)
                ).fetchall()
        return [
            EmailRecord(
                **{**dict(r), "cited_clauses": json.loads(r["cited_clauses"])},
            )
            for r in rows
        ]

    def get_actions(self, pr_id: int | None = None) -> list[ActionRecord]:
        self._enter("get_actions")
        with closing(self.connect()) as conn:
            if pr_id is None:
                rows = conn.execute("SELECT * FROM actions ORDER BY id").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM actions WHERE pr_id = ? ORDER BY id", (pr_id,)
                ).fetchall()
        return [
            ActionRecord(
                id=r["id"],
                idempotency_key=r["idempotency_key"],
                action=r["action"],
                pr_id=r["pr_id"],
                actor=r["actor"],
                result=json.loads(r["result_json"]),
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]

    def register_pr(
        self,
        key: str,
        actor: str,
        requester_email: str,
        item: str,
        quantity: int,
        value_eur: float,
        cost_center: str,
        category: Category,
        urgent: bool = False,
    ) -> WriteResult:
        def apply(conn: sqlite3.Connection) -> dict:
            center = conn.execute(
                "SELECT active FROM cost_centers WHERE code = ?", (cost_center,)
            ).fetchone()
            if center is None:
                raise ErpRejected("unknown_cost_center", f"{cost_center} does not exist.")
            if not center["active"]:
                raise ErpRejected("inactive_cost_center", f"{cost_center} is not active.")
            cursor = conn.execute(
                "INSERT INTO purchase_requests (requester_email, item, quantity, value_eur,"
                " cost_center, category, urgent, state, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)",
                (
                    requester_email, item, quantity, value_eur, cost_center,
                    category.value, int(urgent), self._now(),
                ),
            )
            return {"pr_id": cursor.lastrowid}

        return self._write("register_pr", key, None, actor, apply)

    def add_quote(
        self, key: str, actor: str, pr_id: int, supplier_id: str, amount_eur: float
    ) -> WriteResult:
        def apply(conn: sqlite3.Connection) -> dict:
            self._load_pr(conn, pr_id)
            cursor = conn.execute(
                "INSERT INTO quotes (pr_id, supplier_id, amount_eur) VALUES (?, ?, ?)",
                (pr_id, supplier_id, amount_eur),
            )
            return {"quote_id": cursor.lastrowid}

        return self._write("add_quote", key, pr_id, actor, apply)

    def record_human_approval(self, key: str, approval: HumanApproval) -> WriteResult:
        def apply(conn: sqlite3.Connection) -> dict:
            self._load_pr(conn, approval.request_id)
            cursor = conn.execute(
                "INSERT INTO human_approvals (pr_id, approver, decision, approved_value_eur,"
                " granted_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    approval.request_id, approval.approver, approval.decision.value,
                    approval.approved_value_eur, approval.granted_at.isoformat(),
                    approval.expires_at.isoformat(),
                ),
            )
            return {"approval_id": cursor.lastrowid}

        return self._write(
            "record_human_approval", key, approval.request_id, approval.approver, apply
        )

    def approve_pr(
        self, key: str, actor: str, pr_id: int, cited_clauses: list[str], reason: str
    ) -> WriteResult:
        def apply(conn: sqlite3.Connection) -> dict:
            pr = self._load_pr(conn, pr_id)
            if pr.state != PrState.OPEN:
                raise ErpConflict(f"Purchase request {pr_id} is already {pr.state.value}.")
            budget = self._budget(conn, pr.cost_center)
            if budget is None:
                raise ErpRejected("no_budget", f"{pr.cost_center} has no budget this year.")
            if pr.value_eur > budget.available_eur:
                raise ErpRejected(
                    "insufficient_budget",
                    f"{pr.value_eur} exceeds the available {budget.available_eur}.",
                )
            conn.execute(
                "UPDATE purchase_requests SET state = 'approved', decided_by = ?,"
                " decision_reason = ?, cited_clauses = ?, decided_at = ? WHERE pr_id = ?",
                (actor, reason, json.dumps(cited_clauses), self._now(), pr_id),
            )
            conn.execute(
                "UPDATE budgets SET committed_eur = committed_eur + ?"
                " WHERE cost_center = ? AND year = ?",
                (pr.value_eur, pr.cost_center, self.clock().year),
            )
            return {"state": "approved", "committed_eur": pr.value_eur}

        return self._write("approve_pr", key, pr_id, actor, apply)

    def reject_pr(
        self, key: str, actor: str, pr_id: int, cited_clauses: list[str], reason: str
    ) -> WriteResult:
        def apply(conn: sqlite3.Connection) -> dict:
            pr = self._load_pr(conn, pr_id)
            if pr.state != PrState.OPEN:
                raise ErpConflict(f"Purchase request {pr_id} is already {pr.state.value}.")
            conn.execute(
                "UPDATE purchase_requests SET state = 'rejected', decided_by = ?,"
                " decision_reason = ?, cited_clauses = ?, decided_at = ? WHERE pr_id = ?",
                (actor, reason, json.dumps(cited_clauses), self._now(), pr_id),
            )
            return {"state": "rejected"}

        return self._write("reject_pr", key, pr_id, actor, apply)

    def send_email(
        self,
        key: str,
        actor: str,
        pr_id: int,
        to_addr: str,
        subject: str,
        body: str,
        kind: str,
        cited_clauses: list[str],
    ) -> WriteResult:
        def apply(conn: sqlite3.Connection) -> dict:
            self._load_pr(conn, pr_id)
            cursor = conn.execute(
                "INSERT INTO outbox (to_addr, subject, body, kind, pr_id, cited_clauses, sent_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (to_addr, subject, body, kind, pr_id, json.dumps(cited_clauses), self._now()),
            )
            return {"email_id": cursor.lastrowid}

        return self._write("send_email", key, pr_id, actor, apply)
