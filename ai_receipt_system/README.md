# AI Receipt Ingest v3

This version adds a native hook into `account.journal.create_document_from_attachment`.

Behavior:
- XML / structured invoice imports keep using standard Odoo behavior.
- PDFs and image attachments can be routed through OpenAI extraction.
- When enabled, the hook creates a draft vendor bill and opens it immediately.

Settings added:
- Enable AI replacement for document upload
- Strict replacement mode

Suggested install flow:
1. Replace the existing `ai_receipt_ingest` module folder.
2. Restart Odoo.
3. Upgrade the module from Apps.
4. Set API key, model, default account, and default purchase journal.
5. Enable the replacement hook in Accounting settings.
