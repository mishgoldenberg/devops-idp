# `ai_chatbot` router

File: `backend/app/api/ai_chatbot.py` · Prefix: `/api/ai-chatbot`

## Purpose

In-memory chat store used by the AI assistant widget. Nothing is
persisted, no external model is called — it's scaffolding for a future
internal LLM integration.

## Main endpoints

| Method | Path                               | Description                        |
| ------ | ---------------------------------- | ---------------------------------- |
| GET    | `/api/ai-chatbot/conversations`    | List recent conversations          |
| POST   | `/api/ai-chatbot/conversations`    | Create a new conversation          |
| POST   | `/api/ai-chatbot/conversations/{id}/messages` | Append a message + echo a mock reply |

## Environment variables (reserved)

- `USE_MOCK_AI_CHATBOT` — always `true` today.
- `AI_CHATBOT_API_URL`, `AI_CHATBOT_TOKEN` — reserved.
