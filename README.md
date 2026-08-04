# Loja IA — backend Railway

Servidor FastAPI para receber mensagens do WhatsApp Cloud API, consultar produtos e responder usando a OpenAI.

## Railway

1. Coloque esta pasta em um repositório GitHub.
2. No Railway: New Project > Deploy from GitHub repo.
3. Adicione as variáveis do `.env.example` na aba Variables.
4. Em Settings > Networking, clique em Generate Domain.
5. O webhook será `https://SEU-DOMINIO.up.railway.app/webhook`.
6. Na Meta, use essa URL e o mesmo valor de `VERIFY_TOKEN`.
7. Assine o campo `messages`.

## Volume recomendado

No Railway, crie um Volume montado em `/data`. Assim o catálogo de produtos não desaparece em novos deploys.

## Testes

- `GET /health`
- `GET /` mostra apenas se as integrações estão preenchidas, sem exibir segredos.
- `POST /admin/test-message` exige o header `X-Admin-Secret`.
