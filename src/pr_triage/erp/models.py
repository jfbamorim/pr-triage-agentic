from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, computed_field

from pr_triage.domain.category import Category


class PrState(str, Enum):
    OPEN = "open"
    APPROVED = "approved"
    REJECTED = "rejected"


class Role(str, Enum):
    REQUESTER = "requester"
    DEPARTMENT_HEAD = "department_head"
    DIRECTOR = "director"
    PURCHASING = "purchasing"
    CONTROLLING = "controlling"


class Person(BaseModel):
    email: str
    name: str
    role: Role
    default_cost_center: str | None = None


class CostCenter(BaseModel):
    code: str
    name: str
    department: str
    owner_email: str
    active: bool


class Budget(BaseModel):
    cost_center: str
    year: int
    total_eur: float
    committed_eur: float

    @computed_field
    @property
    def available_eur(self) -> float:
        return round(self.total_eur - self.committed_eur, 2)


class Supplier(BaseModel):
    id: str
    name: str
    approved: bool


class FrameworkContract(BaseModel):
    id: str
    supplier_id: str
    supplier_name: str
    category: Category
    valid_from: date
    valid_to: date
    max_value_eur: float
    consumed_eur: float
    in_force: bool

    @computed_field
    @property
    def remaining_eur(self) -> float:
        return round(self.max_value_eur - self.consumed_eur, 2)


class CatalogItem(BaseModel):
    sku: str
    name: str
    category: Category
    supplier_id: str
    unit_price_eur: float
    available: bool


class Quote(BaseModel):
    id: int
    pr_id: int
    supplier_id: str
    amount_eur: float


class UrgencyJustification(BaseModel):
    pr_id: int
    by: str
    text: str
    created_at: datetime


class PrRecord(BaseModel):
    pr_id: int
    requester_email: str
    item: str
    quantity: int
    value_eur: float
    cost_center: str
    category: Category
    urgent: bool
    state: PrState
    decided_by: str | None = None
    decision_reason: str | None = None
    cited_clauses: list[str] = []
    created_at: datetime
    decided_at: datetime | None = None


class EmailRecord(BaseModel):
    id: int
    to_addr: str
    subject: str
    body: str
    kind: str
    pr_id: int | None
    cited_clauses: list[str] = []
    sent_at: datetime


class ActionRecord(BaseModel):
    id: int
    idempotency_key: str
    action: str
    pr_id: int | None
    actor: str
    result: dict
    created_at: datetime


class WriteResult(BaseModel):
    action_id: int
    replayed: bool
    result: dict
