"""Pydantic request/response models for the moni-app API."""
from typing import Optional

from pydantic import BaseModel


class CategoryOut(BaseModel):
    key: str
    name: str
    emoji: str
    income: bool
    keywords: list[str]


class CategoryCreate(BaseModel):
    name: str
    emoji: str
    income: bool = False
    keywords: list[str] = []


class CategoryUpdate(BaseModel):
    name: Optional[str] = None
    emoji: Optional[str] = None
    keywords: Optional[list[str]] = None


class TransactionOut(BaseModel):
    id: str
    timestamp: str  # ISO-8601
    type: str  # "chi" | "thu"
    amount: int
    category: str
    description: str
    user_name: str
    excluded: bool


class TransactionCreate(BaseModel):
    type: str  # "chi" | "thu"
    amount: int
    description: str
    category: Optional[str] = None
    timestamp: Optional[str] = None  # ISO-8601; defaults to now
    user_name: Optional[str] = None  # defaults to the authenticated user's name


class TransactionUpdate(BaseModel):
    type: Optional[str] = None  # "chi" | "thu"
    amount: Optional[int] = None
    category: Optional[str] = None
    description: Optional[str] = None
    timestamp: Optional[str] = None  # ISO-8601
    excluded: Optional[bool] = None
    user_name: Optional[str] = None


class DailyTotals(BaseModel):
    expense: int
    income: int


class TransactionGroup(BaseModel):
    label: str
    transactions: list[TransactionOut]


class HomeSummary(BaseModel):
    month: str
    today: DailyTotals
    month_totals: DailyTotals
    groups: list[TransactionGroup]


class CategoryTotal(BaseModel):
    category: str
    amount: int


class MonthTotals(BaseModel):
    month: str  # YYYY-MM
    expense: int
    income: int


class AnalysisSummary(BaseModel):
    by_category: list[CategoryTotal]
    monthly: list[MonthTotals]
