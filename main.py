import os
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime
from bson import ObjectId

from database import db, create_document, get_documents
from schemas import Product as ProductSchema, User as UserSchema, Invoice as InvoiceSchema, InvoiceItem as InvoiceItemSchema

app = FastAPI(title="Oman Store Billing API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

security = HTTPBasic()


# ----- Helpers -----

def collection(name: str):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")
    return db[name]


def generate_invoice_no() -> str:
    now = datetime.utcnow()
    date_part = now.strftime("%Y%m%d")
    seq = collection("invoice").count_documents({"date": date_part}) + 1
    return f"INV-{date_part}-{seq:04d}"


# ----- Auth (simple HTTP Basic for demo) -----

class LoginResponse(BaseModel):
    username: str
    role: str


def authenticate(credentials: HTTPBasicCredentials = Depends(security)) -> LoginResponse:
    users = collection("user")
    user = users.find_one({"username": credentials.username, "is_active": True})
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    # For demo: store password as plaintext hash already; production should use hashing
    if user.get("password_hash") != credentials.password:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return LoginResponse(username=user["username"], role=user.get("role", "cashier"))


# ----- Product Endpoints -----

@app.post("/api/products", response_model=dict)
def add_product(product: ProductSchema, _: LoginResponse = Depends(authenticate)):
    # Ensure unique barcode if provided
    if product.barcode:
        existing = collection("product").find_one({"barcode": product.barcode})
        if existing:
            raise HTTPException(status_code=400, detail="Barcode already exists")
    inserted_id = create_document("product", product)
    return {"_id": inserted_id}


@app.get("/api/products", response_model=List[dict])
def list_products(q: Optional[str] = None, category: Optional[str] = None, barcode: Optional[str] = None, _: LoginResponse = Depends(authenticate)):
    fil = {}
    if q:
        fil["name"] = {"$regex": q, "$options": "i"}
    if category:
        fil["category"] = {"$regex": f"^{category}$", "$options": "i"}
    if barcode:
        fil["barcode"] = barcode
    items = list(collection("product").find(fil).limit(50))
    for it in items:
        it["_id"] = str(it["_id"])
    return items


@app.put("/api/products/{product_id}", response_model=dict)
def update_product(product_id: str, product: ProductSchema, _: LoginResponse = Depends(authenticate)):
    try:
        _id = ObjectId(product_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid product id")
    res = collection("product").update_one({"_id": _id}, {"$set": product.model_dump() | {"updated_at": datetime.utcnow()}})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Product not found")
    return {"updated": True}


@app.delete("/api/products/{product_id}", response_model=dict)
def delete_product(product_id: str, _: LoginResponse = Depends(authenticate)):
    try:
        _id = ObjectId(product_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid product id")
    res = collection("product").delete_one({"_id": _id})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Product not found")
    return {"deleted": True}


# ----- Billing / POS -----

class CartItem(BaseModel):
    product_id: str
    quantity: int


class CreateInvoiceRequest(BaseModel):
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    items: List[CartItem]
    discount: float = 0.0


@app.post("/api/invoices", response_model=dict)
def create_invoice(payload: CreateInvoiceRequest, user: LoginResponse = Depends(authenticate)):
    # Fetch product details and compute totals
    product_ids = [ObjectId(i.product_id) for i in payload.items]
    prod_map = {str(p["_id"]): p for p in collection("product").find({"_id": {"$in": product_ids}})}

    invoice_items: List[InvoiceItemSchema] = []
    subtotal = 0.0
    for ci in payload.items:
        p = prod_map.get(ci.product_id)
        if not p:
            raise HTTPException(status_code=400, detail=f"Product not found: {ci.product_id}")
        if p.get("quantity", 0) < ci.quantity:
            raise HTTPException(status_code=400, detail=f"Insufficient stock for {p['name']}")
        line_total = ci.quantity * float(p.get("selling_price", 0))
        subtotal += line_total
        invoice_items.append(InvoiceItemSchema(
            product_id=str(p["_id"]),
            name=p["name"],
            quantity=ci.quantity,
            price=float(p.get("selling_price", 0)),
            total=line_total,
        ))

    discount = float(payload.discount or 0)
    taxable = max(subtotal - discount, 0)
    tax_rate = 0.05
    tax_amount = round(taxable * tax_rate, 3)
    grand_total = round(taxable + tax_amount, 3)

    inv = InvoiceSchema(
        invoice_no=generate_invoice_no(),
        customer_name=payload.customer_name,
        customer_phone=payload.customer_phone,
        items=[i for i in invoice_items],
        subtotal=round(subtotal, 3),
        discount=round(discount, 3),
        tax_rate=tax_rate,
        tax_amount=tax_amount,
        grand_total=grand_total,
        created_at=datetime.utcnow(),
    )

    inserted_id = create_document("invoice", inv)

    # Reduce stock
    for ci in payload.items:
        collection("product").update_one({"_id": ObjectId(ci.product_id)}, {"$inc": {"quantity": -ci.quantity}})

    return {"_id": inserted_id, "invoice_no": inv.invoice_no}


@app.get("/api/invoices/{invoice_no}")
def get_invoice(invoice_no: str, _: LoginResponse = Depends(authenticate)):
    inv = collection("invoice").find_one({"invoice_no": invoice_no})
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    inv["_id"] = str(inv["_id"])
    for it in inv.get("items", []):
        if isinstance(it.get("product_id"), ObjectId):
            it["product_id"] = str(it["product_id"])
    return inv


# ----- Reports -----

@app.get("/api/reports/daily")
def daily_report(date: Optional[str] = None, _: LoginResponse = Depends(authenticate)):
    # date format YYYY-MM-DD UTC
    if not date:
        date = datetime.utcnow().strftime("%Y-%m-%d")
    start = datetime.fromisoformat(date + "T00:00:00")
    end = datetime.fromisoformat(date + "T23:59:59")
    pipeline = [
        {"$match": {"created_at": {"$gte": start, "$lte": end}}},
        {"$group": {"_id": None, "sales": {"$sum": "$grand_total"}, "subtotal": {"$sum": "$subtotal"}, "discount": {"$sum": "$discount"}, "tax": {"$sum": "$tax_amount"}, "count": {"$sum": 1}}},
    ]
    res = list(collection("invoice").aggregate(pipeline))
    return res[0] if res else {"sales": 0, "subtotal": 0, "discount": 0, "tax": 0, "count": 0}


@app.get("/api/reports/monthly")
def monthly_report(year: Optional[int] = None, month: Optional[int] = None, _: LoginResponse = Depends(authenticate)):
    now = datetime.utcnow()
    y = year or now.year
    m = month or now.month
    start = datetime(y, m, 1)
    end = datetime(y + (1 if m == 12 else 0), 1 if m == 12 else m + 1, 1)
    pipeline = [
        {"$match": {"created_at": {"$gte": start, "$lt": end}}},
        {"$group": {"_id": None, "sales": {"$sum": "$grand_total"}, "subtotal": {"$sum": "$subtotal"}, "discount": {"$sum": "$discount"}, "tax": {"$sum": "$tax_amount"}, "count": {"$sum": 1}}},
    ]
    res = list(collection("invoice").aggregate(pipeline))
    return res[0] if res else {"sales": 0, "subtotal": 0, "discount": 0, "tax": 0, "count": 0}


# ----- Misc & Test -----

@app.get("/")
def read_root():
    return {"message": "Oman Store Billing API"}


@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
    }
    try:
        if db is not None:
            response["database"] = "✅ Connected"
            response["collections"] = db.list_collection_names()
        else:
            response["database"] = "❌ Not Configured"
    except Exception as e:
        response["database"] = f"Error: {str(e)[:80]}"
    return response


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
