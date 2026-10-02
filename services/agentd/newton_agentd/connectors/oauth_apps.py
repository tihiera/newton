"""The OAuth apps behind one-click Connect. These are public values (no secrets): a
GitHub OAuth App's client id (device flow, no client secret), a Notion public
integration's client id, and the URL of the broker that holds Notion's client secret
(services/notion-broker). Empty until the apps are registered: Connect then says it
isn't set up and pasting a token still works. NOTION_REDIRECT_URI is the redirect URI
registered with the Notion integration; empty means agentd's own callback,
http://127.0.0.1:{port}/connectors/notion/callback. Each can be overridden by
environment (NEWTON_GITHUB_CLIENT_ID, NEWTON_NOTION_CLIENT_ID, NEWTON_NOTION_BROKER_URL,
NEWTON_NOTION_REDIRECT_URI), read through Settings."""

GITHUB_CLIENT_ID = ""
NOTION_CLIENT_ID = ""
NOTION_BROKER_URL = ""
NOTION_REDIRECT_URI = ""
