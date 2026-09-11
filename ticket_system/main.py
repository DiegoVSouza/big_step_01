import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import UUID, uuid4

import psycopg
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, EmailStr, Field

DATABASE_URL = os.environ["DATABASE_URL"]
SERVICE_TOKEN = os.environ["SERVICE_TOKEN"]


class CustomerLookup(BaseModel):
    email: EmailStr


class Customer(BaseModel):
    id: UUID
    email: EmailStr
    name: str


class TicketCreate(BaseModel):
    customer_id: UUID
    title: str = Field(min_length=5, max_length=120)
    description: str = Field(min_length=10, max_length=2000)
    priority: str = Field(pattern="^(low|medium|high)$")


class HealthResponse(BaseModel):
    ok: bool


class Ticket(BaseModel):
    id: UUID
    customer_id: UUID
    title: str
    priority: str
    status: str
    created_at: datetime


def connection():
    return psycopg.connect(DATABASE_URL)


def require_token(token: str | None) -> None:
    if token != SERVICE_TOKEN:
        raise HTTPException(status_code=401, detail="invalid service token")


@asynccontextmanager
async def lifespan(_: FastAPI):
    with connection() as conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS customers (
              id UUID PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tickets (
              id UUID PRIMARY KEY, customer_id UUID NOT NULL REFERENCES customers(id),
              title TEXT NOT NULL, description TEXT NOT NULL, priority TEXT NOT NULL,
              status TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL
            );
        """)
        cur.execute(
            "INSERT INTO customers (id, email, name) VALUES (%s, %s, %s) ON CONFLICT (email) DO NOTHING",
            ("00000000-0000-0000-0000-000000000001", "ana@example.com", "Ana Souza"),
        )
    yield


app = FastAPI(title="External Ticket System", lifespan=lifespan)


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/customers/lookup", response_model=Customer)
def lookup_customer(body: CustomerLookup, x_service_token: str | None = Header(default=None)):
    require_token(x_service_token)
    with connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, email, name FROM customers WHERE email = %s", (str(body.email),))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="customer not found")
    return Customer(id=row[0], email=row[1], name=row[2])


@app.post("/tickets", response_model=Ticket, status_code=201)
def create_ticket(body: TicketCreate, x_service_token: str | None = Header(default=None)):
    require_token(x_service_token)
    ticket = Ticket(
        id=uuid4(), customer_id=body.customer_id, title=body.title,
        priority=body.priority, status="open", created_at=datetime.now(timezone.utc),
    )
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tickets (id, customer_id, title, description, priority, status, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (ticket.id, ticket.customer_id, ticket.title, body.description, ticket.priority, ticket.status, ticket.created_at),
        )
    return ticket



