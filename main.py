import hashlib
import hmac
import json
import os
import re
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from openai import OpenAI
from pydantic import BaseModel, Field

app = FastAPI(title="Loja IA Webhook", version="1.0.0")

GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "v23.0")
META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
META_APP_SECRET = os.getenv("META_APP_SECRET", "")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "")
STORE_NAME = os.getenv("STORE_NAME", "Loja da Ana Paula")
DATA_DIR = Path(os.getenv("DATA_DIR", "/data" if Path("/data").exists() else "."))
PRODUCTS_PATH = DATA_DIR / "products.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_PRODUCTS = [
    {"id": 1, "name": "Sofá Londres", "category": "Sofás", "description": "Sofá moderno de 3 lugares", "color": "Cinza", "width_cm": 198, "price": 4290, "stock": 3},
    {"id": 2, "name": "Sofá Milano", "category": "Sofás", "description": "Sofá retrátil e reclinável", "color": "Bege", "width_cm": 220, "price": 5190, "stock": 2},
    {"id": 3, "name": "Mesa Aurora", "category": "Mesas", "description": "Mesa de jantar para seis lugares", "color": "Madeira", "width_cm": 180, "price": 2890, "stock": 5},
]


def load_products() -> list[dict[str, Any]]:
    if not PRODUCTS_PATH.exists():
        PRODUCTS_PATH.write_text(json.dumps(DEFAULT_PRODUCTS, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        data = json.loads(PRODUCTS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else DEFAULT_PRODUCTS
    except Exception:
        return DEFAULT_PRODUCTS


def save_products(products: list[dict[str, Any]]) -> None:
    PRODUCTS_PATH.write_text(json.dumps(products, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def search_products(query: str, limit: int = 5) -> list[dict[str, Any]]:
    words = set(normalize(query))
    ranked: list[tuple[int, dict[str, Any]]] = []
    for product in load_products():
        searchable = " ".join(str(product.get(k, "")) for k in ("name", "category", "description", "color", "width_cm", "price"))
        tokens = set(normalize(searchable))
        score = len(words & tokens)
        # Pequeno bônus para termos no nome e produtos disponíveis.
        name_tokens = set(normalize(str(product.get("name", ""))))
        score += 2 * len(words & name_tokens)
        if int(product.get("stock", 0) or 0) > 0:
            score += 1
        if score > 0:
            ranked.append((score, product))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [p for _, p in ranked[:limit]]


def verify_signature(raw_body: bytes, signature: str | None) -> bool:
    if not META_APP_SECRET:
        return True
    if not signature or not signature.startswith("sha256="):
        return False
    expected = hmac.new(META_APP_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature[7:], expected)


def require_admin(x_admin_secret: str | None) -> None:
    if not ADMIN_SECRET or x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="ADMIN_SECRET inválido")


async def send_whatsapp_text(to: str, text: str) -> dict[str, Any]:
    if not META_ACCESS_TOKEN or not PHONE_NUMBER_ID:
        raise RuntimeError("META_ACCESS_TOKEN ou PHONE_NUMBER_ID não configurado")
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": re.sub(r"\D", "", to),
        "type": "text",
        "text": {"preview_url": False, "body": text[:4096]},
    }
    headers = {"Authorization": f"Bearer {META_ACCESS_TOKEN}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(url, headers=headers, json=payload)
    if response.status_code >= 400:
        raise RuntimeError(f"Meta {response.status_code}: {response.text}")
    return response.json()


def build_ai_reply(user_text: str) -> str:
    matches = search_products(user_text)
    catalog = json.dumps(matches, ensure_ascii=False)
    if not OPENAI_API_KEY:
        if matches:
            p = matches[0]
            return f"Encontrei {p.get('name')}: cor {p.get('color')}, {p.get('width_cm')} cm, R$ {float(p.get('price', 0)):,.2f}, estoque {p.get('stock')}."
        return "Olá! Recebi sua mensagem. No momento a IA ainda não está configurada."

    client = OpenAI(api_key=OPENAI_API_KEY)
    instructions = f"""
Você é a atendente virtual da {STORE_NAME}. Responda em português do Brasil, de forma educada, curta e útil.
Use apenas informações de produtos fornecidas no catálogo. Nunca invente preço, medida ou estoque.
Quando faltar informação, faça uma pergunta objetiva. Para agendamento, peça nome, telefone, data, horário e tipo de atendimento.
Não faça follow-up automático. Não diga que é humana.
Catálogo relevante: {catalog}
""".strip()
    response = client.responses.create(
        model=OPENAI_MODEL,
        instructions=instructions,
        input=user_text,
        max_output_tokens=350,
    )
    return (response.output_text or "Desculpe, não consegui responder agora.").strip()


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "service": "Loja IA Webhook",
        "status": "online",
        "configured": {
            "meta_token": bool(META_ACCESS_TOKEN),
            "phone_number_id": bool(PHONE_NUMBER_ID),
            "verify_token": bool(VERIFY_TOKEN),
            "openai": bool(OPENAI_API_KEY),
        },
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/webhook")
def verify_webhook(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
):
    if hub_mode == "subscribe" and VERIFY_TOKEN and hub_verify_token == VERIFY_TOKEN:
        return PlainTextResponse(hub_challenge or "", status_code=200)
    raise HTTPException(status_code=403, detail="Falha na verificação do webhook")


@app.post("/webhook")
async def receive_webhook(request: Request, x_hub_signature_256: str | None = Header(default=None)):
    raw = await request.body()
    if not verify_signature(raw, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="Assinatura inválida")
    payload = json.loads(raw.decode("utf-8"))

    # Responda 200 rapidamente; para protótipo, processamos dentro da requisição.
    try:
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for message in value.get("messages", []) or []:
                    sender = message.get("from", "")
                    msg_type = message.get("type")
                    if msg_type == "text":
                        text = message.get("text", {}).get("body", "").strip()
                    elif msg_type == "button":
                        text = message.get("button", {}).get("text", "")
                    elif msg_type == "interactive":
                        interactive = message.get("interactive", {})
                        text = (interactive.get("button_reply", {}) or interactive.get("list_reply", {})).get("title", "")
                    else:
                        text = "O cliente enviou um conteúdo que ainda não consigo ler. Peça para enviar em texto."
                    if sender and text:
                        reply = build_ai_reply(text)
                        await send_whatsapp_text(sender, reply)
    except Exception as exc:
        # O webhook precisa devolver 200 para evitar retentativas em loop; o erro aparece nos logs do Railway.
        print(f"ERRO_PROCESSAMENTO_WEBHOOK: {exc}", flush=True)
    return JSONResponse({"received": True})


class ProductIn(BaseModel):
    name: str
    category: str = ""
    description: str = ""
    color: str = ""
    width_cm: float | None = None
    price: float = 0
    stock: int = 0


class TestMessage(BaseModel):
    to: str = Field(description="Número com DDI e DDD, somente dígitos")
    text: str = "Teste da Loja IA via Railway."


@app.get("/admin/products")
def list_products(x_admin_secret: str | None = Header(default=None)):
    require_admin(x_admin_secret)
    return load_products()


@app.post("/admin/products")
def add_product(product: ProductIn, x_admin_secret: str | None = Header(default=None)):
    require_admin(x_admin_secret)
    products = load_products()
    next_id = max([int(p.get("id", 0)) for p in products] or [0]) + 1
    record = {"id": next_id, **product.model_dump()}
    products.append(record)
    save_products(products)
    return record


@app.put("/admin/products/{product_id}")
def update_product(product_id: int, product: ProductIn, x_admin_secret: str | None = Header(default=None)):
    require_admin(x_admin_secret)
    products = load_products()
    for index, current in enumerate(products):
        if int(current.get("id", 0)) == product_id:
            products[index] = {"id": product_id, **product.model_dump()}
            save_products(products)
            return products[index]
    raise HTTPException(status_code=404, detail="Produto não encontrado")


@app.delete("/admin/products/{product_id}")
def delete_product(product_id: int, x_admin_secret: str | None = Header(default=None)):
    require_admin(x_admin_secret)
    products = load_products()
    filtered = [p for p in products if int(p.get("id", 0)) != product_id]
    if len(filtered) == len(products):
        raise HTTPException(status_code=404, detail="Produto não encontrado")
    save_products(filtered)
    return {"deleted": True}


@app.post("/admin/test-message")
async def test_message(data: TestMessage, x_admin_secret: str | None = Header(default=None)):
    require_admin(x_admin_secret)
    return await send_whatsapp_text(data.to, data.text)
