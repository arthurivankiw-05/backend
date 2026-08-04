import json
import os
import sqlite3
from contextlib import closing
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from openai import OpenAI
from pydantic import BaseModel, Field

APP_NAME = os.getenv("STORE_NAME", "Loja IA")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "")
META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "v23.0")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
DATABASE_PATH = os.getenv("DATABASE_PATH", "/tmp/lojaia.db")

app = FastAPI(title="Loja IA Backend", version="1.0.0")


class ProductCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    category: str = Field(default="", max_length=80)
    description: str = Field(default="", max_length=1000)
    color: str = Field(default="", max_length=80)
    width_m: float | None = Field(default=None, ge=0)
    price: float = Field(default=0, ge=0)
    stock: int = Field(default=0, ge=0)


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(db_connect()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                color TEXT NOT NULL DEFAULT '',
                width_m REAL,
                price REAL NOT NULL DEFAULT 0,
                stock INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.commit()


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/")
def root() -> dict[str, str]:
    return {"app": APP_NAME, "status": "online", "version": "1.0.0"}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def check_admin(x_admin_secret: str | None) -> None:
    if not ADMIN_SECRET:
        raise HTTPException(500, "ADMIN_SECRET não configurado no Railway.")
    if x_admin_secret != ADMIN_SECRET:
        raise HTTPException(401, "Senha administrativa inválida.")


@app.get("/products")
def list_products(x_admin_secret: str | None = Header(default=None)) -> list[dict[str, Any]]:
    check_admin(x_admin_secret)
    with closing(db_connect()) as conn:
        rows = conn.execute("SELECT * FROM products ORDER BY id DESC").fetchall()
    return [dict(row) for row in rows]


@app.post("/products")
def create_product(
    product: ProductCreate,
    x_admin_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    check_admin(x_admin_secret)
    with closing(db_connect()) as conn:
        cursor = conn.execute(
            """
            INSERT INTO products
            (name, category, description, color, width_m, price, stock)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                product.name,
                product.category,
                product.description,
                product.color,
                product.width_m,
                product.price,
                product.stock,
            ),
        )
        conn.commit()
        product_id = cursor.lastrowid
    return {"created": True, "id": product_id}


@app.delete("/products/{product_id}")
def delete_product(
    product_id: int,
    x_admin_secret: str | None = Header(default=None),
) -> dict[str, bool]:
    check_admin(x_admin_secret)
    with closing(db_connect()) as conn:
        cursor = conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
        conn.commit()
    if cursor.rowcount == 0:
        raise HTTPException(404, "Produto não encontrado.")
    return {"deleted": True}


@app.get("/webhook", response_class=PlainTextResponse)
def verify_webhook(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
) -> str:
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN and hub_challenge:
        return hub_challenge
    raise HTTPException(403, "Token de verificação inválido.")


def extract_whatsapp_messages(payload: dict[str, Any]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    try:
        entries = payload.get("entry", [])
        for entry in entries:
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for message in value.get("messages", []) or []:
                    sender = message.get("from")
                    msg_type = message.get("type")
                    if msg_type == "text":
                        text = message.get("text", {}).get("body", "")
                        if sender and text:
                            result.append({"from": sender, "text": text})
    except (AttributeError, TypeError):
        return []
    return result


def search_products(query: str, limit: int = 8) -> list[dict[str, Any]]:
    terms = [term.lower() for term in query.split() if len(term) >= 3]
    with closing(db_connect()) as conn:
        rows = conn.execute("SELECT * FROM products WHERE stock > 0 ORDER BY id DESC").fetchall()
    products = [dict(row) for row in rows]
    if not terms:
        return products[:limit]

    scored = []
    for product in products:
        haystack = " ".join(
            str(product.get(key, ""))
            for key in ("name", "category", "description", "color", "width_m", "price")
        ).lower()
        score = sum(1 for term in terms if term in haystack)
        if score:
            scored.append((score, product))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [product for _, product in scored[:limit]]


def build_ai_reply(customer_text: str) -> str:
    matches = search_products(customer_text)
    catalog = json.dumps(matches, ensure_ascii=False, indent=2)

    if not OPENAI_API_KEY:
        if matches:
            product = matches[0]
            return (
                f"Encontrei {product['name']}. "
                f"Preço: R$ {product['price']:.2f}. "
                f"Estoque: {product['stock']} unidade(s)."
            )
        return "Recebi sua mensagem. A chave da OpenAI ainda não foi configurada."

    client = OpenAI(api_key=OPENAI_API_KEY)
    response = client.responses.create(
        model=OPENAI_MODEL,
        instructions=(
            f"Você é a atendente virtual da {APP_NAME}. Responda em português do Brasil, "
            "de forma educada e curta. Use somente informações do catálogo fornecido. "
            "Nunca invente preço, estoque, medidas ou prazo. Quando não houver produto "
            "compatível, diga que um atendente humano poderá ajudar. Não faça follow-up."
        ),
        input=(
            f"Mensagem do cliente:\n{customer_text}\n\n"
            f"Produtos possivelmente relacionados:\n{catalog}"
        ),
    )
    return response.output_text.strip()


async def send_whatsapp_text(to: str, text: str) -> dict[str, Any]:
    if not META_ACCESS_TOKEN or not PHONE_NUMBER_ID:
        raise RuntimeError("META_ACCESS_TOKEN ou PHONE_NUMBER_ID não configurado.")

    url = (
        f"https://graph.facebook.com/{GRAPH_API_VERSION}/"
        f"{PHONE_NUMBER_ID}/messages"
    )
    headers = {
        "Authorization": f"Bearer {META_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    body = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": text[:4096]},
    }
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(url, headers=headers, json=body)
    if response.status_code >= 400:
        raise RuntimeError(f"Meta API {response.status_code}: {response.text}")
    return response.json()


@app.post("/webhook")
async def receive_webhook(request: Request) -> dict[str, bool]:
    payload = await request.json()
    messages = extract_whatsapp_messages(payload)

    for message in messages:
        try:
            reply = build_ai_reply(message["text"])
            await send_whatsapp_text(message["from"], reply)
        except Exception as exc:
            print(f"Erro ao responder {message.get('from')}: {exc}")

    return {"received": True}


class TestMessage(BaseModel):
    to: str
    text: str = "Teste enviado pela Loja IA."


@app.post("/admin/test-whatsapp")
async def test_whatsapp(
    data: TestMessage,
    x_admin_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    check_admin(x_admin_secret)
    return await send_whatsapp_text(data.to, data.text)
