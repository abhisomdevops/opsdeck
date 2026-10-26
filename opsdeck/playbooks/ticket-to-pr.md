---
id: ticket-to-pr
title: Ticket to PR
description: Read a Jira ticket, find the relevant code or IaC in GitHub, propose a change and open a PR.
starter: "Read this ticket, find the code that needs to change and propose a fix: "
order: 2
---
You are turning a ticket into a code change.

1. Read the ticket (`jira__issue` accepts a key or a full browse URL). Extract the concrete requirement,
   acceptance criteria, affected service names, error messages and any linked Confluence pages.
2. Locate the code: use `github__search_code` with distinctive identifiers (service name, config key, error
   string, Terraform resource names, Helm value keys). Read the candidate files with `github__file` before
   changing anything. Check `github__pulls` for an open PR that already does this.
3. Explain the change you intend to make, file by file, and why, before proposing it.
4. Propose the change with `github__create_pull_request`. Send the **complete new content** of every file you
   change (not a diff, not a fragment). Keep the change minimal and in the repository's existing style.
   Title format: `<TICKET-KEY>: <short description>`. Put the ticket link, the reasoning and a test plan in the
   body.
5. Only create the PR if the user asked for it; otherwise stop after step 3 and ask.

If you cannot find the code with confidence, say so and list what you searched.
