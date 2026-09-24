"""
DevBot: the Hub's chat assistant.

A person asks a question in their own words; DevBot answers it from Azure DevOps,
SonarQube, Artifactory and Confluence, reading each one with THAT PERSON's own token.
It runs inside the backend rather than as a service of its own, so it uses the same
sign-in, the same stored tokens, the same collection discovery and the same TLS
setting as every widget -- and the writes it offers go through the same checked,
confirmed dialog the widgets use (api/ado_actions.py). It never writes on its own.

The model server is an OpenAI-compatible gateway (LiteLLM in front of vLLM). Each
person connects their OWN key to it, on Connections or on the DevBot page, which is
what decides which models they may use and how many requests and tokens a minute
they get. Those limits are small, so everything here is built to spend few of them:

  config.py        settings, read from the environment
  llm.py           the gateway client: models, key limits, streamed chat, errors
  models.py        which models a key may use, and which of them can call tools
  budget.py        token estimates, and fitting a conversation into the limits
  store.py         conversations, messages and usage, in Postgres
  prompts.py       the system prompt
  orchestrator.py  one turn: history -> model -> tools -> answer, as events
"""
