"""
Database Schemas for Oman Store Billing System

Each Pydantic model represents a MongoDB collection in the connected database.
Collection name is the lowercase of the class name (e.g., User -> "user").
"""

from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime


class User(BaseModel):
    """Users of the system (admin/cashier)"""
    username: str = Field(..., description="Unique username")
    full_name: str = Field(..., description="Full name")
    role: str = Field("cashier", description="Role: admin or cashier")
    password_hash: str = Field(..., description="Hashed password (server-side only)")
    is_active: bool = Field(True, description="Whether the user is active")


class Product(BaseModel):
    """Products available for sale"""
    name: str = Field(..., description="Product name")
    category: str = Field("General", description="Category")
    quantity: int = Field(0, ge=0, description="Quantity in stock")
    purchase_price: float = Field(..., ge=0, description="Purchase price")
    selling_price: float = Field(..., ge=0, description="Selling price")
    barcode: Optional[str] = Field(None, description="Item code / barcode")


class InvoiceItem(BaseModel):
    product_id: str = Field(..., description="Referenced product _id as string")
    name: str = Field(..., description="Snapshot of product name at time of sale")
    quantity: int = Field(..., ge=1)
    price: float = Field(..., ge=0, description="Unit selling price at time of sale")
    total: float = Field(..., ge=0, description="quantity * price")


class Invoice(BaseModel):
    invoice_no: str = Field(..., description="Auto-generated invoice number")
    customer_name: Optional[str] = Field(None)
    customer_phone: Optional[str] = Field(None)
    items: List[InvoiceItem] = Field(default_factory=list)
    subtotal: float = Field(..., ge=0)
    discount: float = Field(0.0, ge=0)
    tax_rate: float = Field(0.05, ge=0)
    tax_amount: float = Field(..., ge=0)
    grand_total: float = Field(..., ge=0)
    created_at: Optional[datetime] = None

