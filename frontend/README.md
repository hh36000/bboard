# Frontend

React, TypeScript, Tailwind, and Vite. Follow the [root setup](../README.md#install-and-run) to start both services.

Boards open directly in the live editor and save automatically. `live` and `history` switch views. History entries stay expanded and show UTC timestamps, saved text, and changes. The backend retains every committed revision.

## Commands

```sh
npm run dev
npm run lint
npm run build
```

Run these from `frontend/`. Development uses port 6600. Build checks TypeScript and writes `dist/`.

## Code

- `pages/Home.tsx` selects the editor or history.
- `components/TextEditor.tsx` and `services/document-sync.ts` manage browser edits, retries, and drafts.
- `components/BoardHistory.tsx` reads immutable revisions.
- `services/api.ts` configures HTTP; `shared-document.ts` configures WebSocket sync through `VITE_API_URL`.

Recent board links and theme use localStorage; unsaved editor drafts use board-specific sessionStorage. The board event log lives on the server.

The backend renders initial page content, including history without JavaScript. A static host serves `/app.js`, JavaScript, CSS, and images. Local Vite proxies page requests to the backend. The build removes static `index.html` so it cannot hide the backend's pages, so static preview alone cannot serve the application.

Set `VITE_API_URL` at build time to the backend's public origin. Edit `backend/routers/help_content.json` for the shared guide. See [self-hosting](../backend/README.md#self-hosting).
