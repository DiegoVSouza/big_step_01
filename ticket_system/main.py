import os
import re
import unicodedata
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


class CustomerCreate(BaseModel):
    email: EmailStr
    name: str = Field(min_length=2, max_length=120)


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
        cur.execute(
            "INSERT INTO customers (id, email, name) VALUES (%s, %s, %s) ON CONFLICT (email) DO NOTHING",
            ("00000000-0000-0000-0000-000000000002", "joao@example.com", "João Lima"),
        )
        cur.execute(
            "INSERT INTO customers (id, email, name) VALUES (%s, %s, %s) ON CONFLICT (email) DO NOTHING",
            ("00000000-0000-0000-0000-000000000003", "maria@example.com", "Maria Costa"),
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


@app.post("/customers", response_model=Customer, status_code=201)
def create_customer(body: CustomerCreate, x_service_token: str | None = Header(default=None)):
    require_token(x_service_token)
    customer = Customer(id=uuid4(), email=body.email, name=body.name.strip())
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO customers (id, email, name) VALUES (%s, %s, %s) ON CONFLICT (email) DO UPDATE SET name = EXCLUDED.name RETURNING id, email, name",
            (customer.id, str(customer.email), customer.name),
        )
        row = cur.fetchone()
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





class KnowledgeSearchRequest(BaseModel):
    query: str = Field(min_length=3, max_length=200)


class KnowledgeArticle(BaseModel):
    id: str
    title: str
    summary: str
    tags: list[str]


class KnowledgeSearchResponse(BaseModel):
    articles: list[KnowledgeArticle]


KNOWLEDGE_BASE = [
    KnowledgeArticle(id="KB-001", title="Redefinir acesso ao portal", summary="Confirme o e-mail do cliente, envie o link de redefinição e valide o novo acesso.", tags=["acesso", "senha", "portal"]),
    KnowledgeArticle(id="KB-002", title="Relatório mensal não abre", summary="Verifique o período selecionado, atualize a página e tente gerar o relatório novamente.", tags=["relatório", "portal"]),
    KnowledgeArticle(id="KB-003", title="Portal lento ou indisponível", summary="Registre horário, rota afetada e quantidade de usuários impactados para o suporte investigar.", tags=["indisponibilidade", "lentidão", "portal"]),
]


@app.post("/knowledge/search", response_model=KnowledgeSearchResponse)
def search_knowledge(body: KnowledgeSearchRequest, x_service_token: str | None = Header(default=None)) -> KnowledgeSearchResponse:
    require_token(x_service_token)
    normalized_query = unicodedata.normalize("NFKD", body.query.lower()).encode("ascii", "ignore").decode()
    stop_words = {"a", "o", "as", "os", "de", "do", "da", "dos", "das", "e", "um", "uma", "no", "na", "para", "por", "com", "que", "como", "me", "se", "ao", "saber", "nao", "portal", "problema", "problemas", "sistema"}
    aliases = {"acessar": "acesso", "acessando": "acesso", "lenta": "lento", "lentidão": "lento", "lentidao": "lento"}
    terms = {aliases.get(term, term) for term in re.findall(r"[a-z0-9]+", normalized_query) if term not in stop_words and len(term) > 2}
    scored = []
    for article in KNOWLEDGE_BASE:
        title = unicodedata.normalize("NFKD", article.title.lower()).encode("ascii", "ignore").decode()
        searchable = title + " " + " ".join(article.tags)
        score = sum(2 if term in title else 1 for term in terms if term in searchable)
        if score:
            scored.append((score, article))
    scored.sort(key=lambda item: item[0], reverse=True)
    relevant = [(score, article) for score, article in scored if score >= 2]
    if not relevant and scored:
        relevant = scored[:1]
    return KnowledgeSearchResponse(articles=[article for _, article in relevant[:3]])











