# How we reach each service

Read this before you pick a tool. A row here overrides the default order (connector or API, shell, browser, screen).
Agents: if you learn something that belongs here, say so in your result. The manager decides whether to add it.

This file starts with the two rows that are true for every fleet. The rest is the owner's own: one row for each
service this fleet reaches, and why that route and not another.

| Service | Reach it by | Why |
| --- | --- | --- |
| Google Drive, Docs, Sheets and Gmail | The Google tools (`drive_*`, `gmail_*`) when the owner gave you access; else ask your requester | The hub keeps the owner's Google access and your level |
| A service with an MCP server of the owner | The server's own tools (`mcp__<server>__*`) when the owner gave you that server; else ask your requester | The owner holds the sign-in, and the tools are made for the job |

## Tool notes

- An MCP server of the owner is the first route to its service: its tools are `mcp__<server>__*`, and your start text names the servers that you have. If your start text says that a server is not loaded, say so in your result or ask your requester; do not reach the service another way.
- A note that one agent learned the hard way belongs here, with the task it was learned on, so the next agent does not learn it again.
