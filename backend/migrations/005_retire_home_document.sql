-- Deploy the backend that no longer uses the home document before applying this
-- migration to an existing installation. This permanently deletes the old shared
-- root text and its retry receipts. Keyed boards and private documents are untouched.
DROP TABLE app.realtime_document_writers;
DROP TABLE app.realtime_documents;
